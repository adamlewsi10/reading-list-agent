import logging
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from svix.webhooks import Webhook
from agentmail import AgentMail
import json
import os

from app.config import AGENTMAIL_API_KEY, AGENTMAIL_INBOX_ID, WEBHOOK_SECRET
from app.article_fetcher import process_email
from app.drive_writer import write_article
from app.frontmatter import generate_frontmatter
from app.digest import run_digest, handle_digest_reply

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Reading List Agent")
mail_client = AgentMail(api_key=AGENTMAIL_API_KEY)

DIGEST_TRIGGER_SECRET = os.environ.get("DIGEST_TRIGGER_SECRET", "")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/trigger-digest")
async def trigger_digest(request: Request, background_tasks: BackgroundTasks):
    """Manually trigger the Friday digest. Protected by DIGEST_TRIGGER_SECRET."""
    if DIGEST_TRIGGER_SECRET:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] != DIGEST_TRIGGER_SECRET:
            raise HTTPException(status_code=401, detail="Unauthorized")
    background_tasks.add_task(_run_digest_task)
    return {"status": "accepted"}


def _run_digest_task():
    try:
        result = run_digest(days=7)
        logger.info("Digest result: %s", result)
    except Exception as e:
        logger.error("Digest failed: %s", e, exc_info=True)


@app.post("/webhooks")
async def webhook_handler(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    headers = dict(request.headers)

    payload = None
    svix_headers = {k.lower(): v for k, v in headers.items() if k.lower().startswith(("svix-", "webhook-"))}

    try:
        wh = Webhook(WEBHOOK_SECRET)
        payload = wh.verify(body, svix_headers)
        logger.info("Webhook signature verified")
    except Exception as e:
        logger.warning("Webhook verification failed (%s), falling back to raw parse", e)
        try:
            payload = json.loads(body)
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid signature")

    event_type = payload.get("event_type") or payload.get("type")
    if event_type != "message.received":
        return {"status": "ignored", "event_type": event_type}

    background_tasks.add_task(process_message, payload)
    return {"status": "accepted"}


def process_message(payload: dict):
    try:
        message = payload.get("message") or payload.get("data", {})
        message_id = message.get("message_id") or message.get("id", "")
        inbox_id = message.get("inbox_id", AGENTMAIL_INBOX_ID)
        subject = message.get("subject", "").strip()
        text_body = message.get("text") or message.get("text_body") or ""
        html_body = message.get("html") or message.get("html_body") or ""
        from_addresses = message.get("from_") or message.get("from") or []

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

        logger.info("Processing message: %r (from %s)", subject, forwarded_by)

        # Digest reply: Stav replied to a digest with his take
        if _is_digest_reply(subject):
            logger.info("Routing as digest reply: %r", subject)
            handle_digest_reply(subject, text_body)
            try:
                mail_client.inboxes.messages.update(
                    inbox_id=inbox_id,
                    message_id=message_id,
                    add_labels=["digest-reply"],
                )
            except Exception as e:
                logger.warning("Failed to label digest reply: %s", e)
            return

        # Normal article capture
        article = process_email(subject, text_body, html_body)

        if not article.url:
            logger.info("No URL found in message — skipping (no-URL entries not stored)")
            return

        fm = generate_frontmatter(
            url=article.url,
            title=article.title,
            source=article.source,
            author=article.author,
            body_text=article.body_text,
            fetch_failed=article.fetch_failed,
        )

        was_written = write_article(
            url=article.url,
            title=article.title,
            source=article.source,
            author=article.author,
            body_text=article.body_text,
            fetch_failed=article.fetch_failed,
            frontmatter=fm,
        )

        if was_written:
            logger.info("Written to Drive: %s", article.title[:80])
        else:
            logger.info("Duplicate — skipped: %s", article.url[:80])

        try:
            mail_client.inboxes.messages.update(
                inbox_id=inbox_id,
                message_id=message_id,
                add_labels=["processed"],
            )
        except Exception as e:
            logger.warning("Failed to label message: %s", e)

    except Exception as e:
        logger.error("Processing failed: %s", e, exc_info=True)


def _is_digest_reply(subject: str) -> bool:
    """True if this email is a reply to a Reading Digest."""
    s = subject.lower().strip()
    return s.startswith("re:") and "reading digest" in s
