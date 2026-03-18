import logging
from datetime import datetime, timezone

from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from svix.webhooks import Webhook, WebhookVerificationError
from agentmail import AgentMail

from app.config import AGENTMAIL_API_KEY, AGENTMAIL_INBOX_ID, WEBHOOK_SECRET
from app.article_fetcher import process_email
from app.categoriser import categorise_article
from app.sheets_writer import write_to_sheet

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Reading List Agent")
mail_client = AgentMail(api_key=AGENTMAIL_API_KEY)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/webhooks")
async def webhook_handler(request: Request, background_tasks: BackgroundTasks):
    """Receive AgentMail webhook events."""
    body = await request.body()
    headers = dict(request.headers)

    # Verify webhook signature via Svix, fall back to raw parsing
    import json
    payload = None

    # Svix expects specific header names — normalize from various webhook providers
    svix_headers = {}
    for k, v in headers.items():
        kl = k.lower()
        if kl.startswith("svix-") or kl.startswith("webhook-"):
            svix_headers[kl] = v

    try:
        wh = Webhook(WEBHOOK_SECRET)
        payload = wh.verify(body, svix_headers)
        logger.info("Webhook signature verified successfully")
    except Exception as e:
        logger.warning("Webhook verification failed (%s), falling back to raw parse", e)
        try:
            payload = json.loads(body)
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid signature")

    event_type = payload.get("event_type") or payload.get("type")
    if event_type != "message.received":
        return {"status": "ignored", "event_type": event_type}

    # Process in background so we return 200 fast
    background_tasks.add_task(process_message, payload)
    return {"status": "accepted"}


def process_message(payload: dict):
    """Full processing pipeline for a received message."""
    try:
        message = payload.get("message") or payload.get("data", {})
        message_id = message.get("message_id") or message.get("id", "")
        inbox_id = message.get("inbox_id", AGENTMAIL_INBOX_ID)
        subject = message.get("subject", "").strip()
        text_body = message.get("text") or message.get("text_body") or ""
        html_body = message.get("html") or message.get("html_body") or ""
        from_addresses = message.get("from_") or message.get("from") or []

        # Normalize from_addresses to a list
        if isinstance(from_addresses, str):
            from_addresses = [from_addresses]
        elif isinstance(from_addresses, dict):
            from_addresses = [from_addresses]

        forwarded_by = "Unknown"
        if from_addresses:
            sender = from_addresses[0]
            if isinstance(sender, dict):
                forwarded_by = sender.get("email") or sender.get("address") or sender.get("name") or str(sender)
            elif isinstance(sender, str):
                forwarded_by = sender
        forwarded_by = forwarded_by.strip()

        logger.info(f"Processing message: {subject!r} (from {forwarded_by})")
        logger.debug(f"Message keys: {list(message.keys())}")

        # Step 1: Extract and fetch article content
        article = process_email(subject, text_body, html_body)

        # Step 2: Categorise with Claude
        metadata = categorise_article(
            title=article.title,
            content=article.body_text,
            url=article.url,
            source=article.source,
            author=article.author,
        )

        # Step 3: Build notes
        notes_parts = []
        if article.extra_urls:
            notes_parts.append(f"Additional URLs: {', '.join(article.extra_urls[:3])}")
        if article.is_pdf:
            notes_parts.append("PDF — content not extracted")
        if article.fetch_failed:
            notes_parts.append("Article fetch failed")

        # Step 4: Write to Google Sheet
        row_data = {
            "date_added": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "title": metadata.get("title", article.title),
            "author": metadata.get("author", article.author),
            "source": metadata.get("source", article.source),
            "url": article.url,
            "topic": metadata.get("topic", "Uncategorised"),
            "subtopic": metadata.get("subtopic", ""),
            "summary": metadata.get("summary", ""),
            "tags": metadata.get("tags", ""),
            "forwarded_by": forwarded_by,
            "notes": "; ".join(notes_parts) if notes_parts else "",
        }
        write_to_sheet(row_data)

        # Step 5: Label message as processed
        try:
            mail_client.inboxes.messages.update(
                inbox_id=inbox_id,
                message_id=message_id,
                add_labels=["processed"],
            )
            logger.info(f"Labelled message {message_id} as processed")
        except Exception as e:
            logger.warning(f"Failed to label message: {e}")

        logger.info(f"Successfully processed: {metadata.get('title', subject)}")

    except Exception as e:
        logger.error(f"Processing failed for message: {e}", exc_info=True)
