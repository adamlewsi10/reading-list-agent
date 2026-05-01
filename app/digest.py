"""
Phase 3 — Friday Reading Digest.

Scans Reading Library in Drive, selects and ranks articles from the last 7 days,
generates a personalised email via Claude Sonnet, and sends it to Stav.
Also handles reply write-back: when Stav replies with a my_take, it's written
back into the article's markdown file.
"""

from __future__ import annotations

import logging
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import anthropic
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

from app.config import (
    ANTHROPIC_API_KEY,
    GOOGLE_DRIVE_OAUTH_JSON,
    READING_LIBRARY_FOLDER_ID,
)

logger = logging.getLogger(__name__)

DIGEST_RECIPIENT = "stavg@me.com"
DIGEST_SENDER_NAME = "Reading Library"

_claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ---------------------------------------------------------------------------
# Drive helpers
# ---------------------------------------------------------------------------

def _get_drive_service():
    oauth = GOOGLE_DRIVE_OAUTH_JSON
    creds = Credentials(
        token=oauth.get("token"),
        refresh_token=oauth["refresh_token"],
        token_uri=oauth.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=oauth["client_id"],
        client_secret=oauth["client_secret"],
        scopes=oauth.get("scopes", ["https://www.googleapis.com/auth/drive"]),
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("drive", "v3", credentials=creds)


def _get_gmail_service():
    oauth = GOOGLE_DRIVE_OAUTH_JSON
    # Always use the full scope set that includes gmail.send
    scopes = [
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.readonly",
    ]
    creds = Credentials(
        token=None,  # Force refresh so we always get an access token with all scopes
        refresh_token=oauth["refresh_token"],
        token_uri=oauth.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=oauth["client_id"],
        client_secret=oauth["client_secret"],
        scopes=scopes,
    )
    creds.refresh(Request())
    return build("gmail", "v1", credentials=creds)


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------

def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """
    Parse YAML-ish frontmatter from a markdown file.
    Returns (fm_dict, body_text).
    """
    if not content.startswith("---"):
        return {}, content

    end = content.find("\n---", 3)
    if end == -1:
        return {}, content

    fm_block = content[3:end].strip()
    body = content[end + 4:].strip()

    fm: dict = {}
    current_list_key: Optional[str] = None
    current_list: list = []

    for line in fm_block.splitlines():
        if line.startswith("  - "):
            # List item
            val = line[4:].strip().strip('"')
            current_list.append(val)
            continue

        # Flush previous list
        if current_list_key and current_list:
            fm[current_list_key] = current_list
            current_list_key = None
            current_list = []

        if ": " in line:
            key, _, val = line.partition(": ")
            key = key.strip()
            val = val.strip()
            if val == "[]":
                fm[key] = []
                current_list_key = None
            elif val == "null":
                fm[key] = None
            elif val == '""':
                fm[key] = ""
            else:
                # Could be start of multiline list (value empty after colon)
                if val == "":
                    current_list_key = key
                    current_list = []
                else:
                    fm[key] = val.strip('"')
        elif line.endswith(":"):
            current_list_key = line[:-1].strip()
            current_list = []

    # Flush trailing list
    if current_list_key and current_list:
        fm[current_list_key] = current_list

    return fm, body


# ---------------------------------------------------------------------------
# Scan Reading Library
# ---------------------------------------------------------------------------

def scan_reading_library(days: int = 7) -> list[dict]:
    """
    List all markdown files in Reading Library, parse frontmatter,
    and return those captured in the last `days` days.
    """
    service = _get_drive_service()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")

    results = []
    page_token = None

    while True:
        params = dict(
            q=(
                f"'{READING_LIBRARY_FOLDER_ID}' in parents "
                "and mimeType='text/markdown' "
                "and trashed=false"
            ),
            fields="nextPageToken, files(id, name, createdTime)",
            pageSize=100,
        )
        if page_token:
            params["pageToken"] = page_token

        res = service.files().list(**params).execute()
        files = res.get("files", [])

        for f in files:
            name = f["name"]
            # Filename starts with YYYY-MM-DD
            date_prefix = name[:10] if len(name) >= 10 else ""
            if date_prefix < cutoff:
                continue

            try:
                content_bytes = service.files().get_media(fileId=f["id"]).execute()
                content = content_bytes.decode("utf-8", errors="replace")
                fm, _ = _parse_frontmatter(content)

                if not fm.get("source_url"):
                    continue

                results.append({
                    "file_id": f["id"],
                    "filename": name,
                    "date_captured": fm.get("date_captured", date_prefix),
                    "source_url": fm.get("source_url", ""),
                    "source_name": fm.get("source_name", ""),
                    "author": fm.get("author", ""),
                    "title": _filename_to_title(name),
                    "concepts": fm.get("concepts") or [],
                    "entities_mentioned": fm.get("entities_mentioned") or [],
                    "key_claims": fm.get("key_claims") or [],
                    "relevance_to": fm.get("relevance_to") or [],
                    "my_take": fm.get("my_take", ""),
                    "fetch_status": fm.get("fetch_status", "ok"),
                    "raw_frontmatter": fm,
                    "raw_content": content,
                })
            except Exception as e:
                logger.warning("Failed to read %s: %s", name, e)

        page_token = res.get("nextPageToken")
        if not page_token:
            break

    logger.info("Scanned %d articles from last %d days", len(results), days)
    return results


def _filename_to_title(filename: str) -> str:
    """Extract a readable title from filename like '2026-05-01 Title — Source.md'"""
    name = filename.removesuffix(".md")
    # Strip leading date
    if len(name) > 11 and name[10] == " ":
        name = name[11:]
    # Strip source suffix after ' — '
    if " — " in name:
        name = name.split(" — ")[0]
    return name.strip()


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

def rank_articles(articles: list[dict]) -> list[dict]:
    """
    Score each article. Higher = more interesting.
    Factors: relevance_to count, concept richness, key_claims count, recency.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for a in articles:
        score = 0
        score += len(a.get("relevance_to", [])) * 3
        score += min(len(a.get("concepts", [])), 5) * 2
        score += min(len(a.get("key_claims", [])), 4) * 1
        # Recency bonus: +2 if captured today or yesterday
        date = a.get("date_captured", "")
        if date >= today[:8]:  # same month roughly
            score += 1
        # Penalise fetch failures
        if a.get("fetch_status") == "failed":
            score -= 2
        a["_score"] = score

    return sorted(articles, key=lambda x: x["_score"], reverse=True)


def detect_patterns(articles: list[dict]) -> list[str]:
    """
    Find recurring concepts, authors, or themes across the week's articles.
    Returns a list of plain-English observations.
    """
    patterns = []

    concept_counts: Counter = Counter()
    for a in articles:
        for c in a.get("concepts", []):
            concept_counts[c.lower()] += 1

    repeated = [(c, n) for c, n in concept_counts.items() if n >= 3]
    for concept, n in sorted(repeated, key=lambda x: -x[1])[:3]:
        patterns.append(f"{n} articles touched on \"{concept}\"")

    # Recurring projects
    project_counts: Counter = Counter()
    for a in articles:
        for p in a.get("relevance_to", []):
            project_counts[p] += 1
    for project, n in project_counts.most_common(3):
        if n >= 2:
            patterns.append(f"{n} articles linked to {project}")

    # Recurring sources
    source_counts: Counter = Counter()
    for a in articles:
        src = a.get("source_name", "").strip()
        if src:
            source_counts[src] += 1
    for src, n in source_counts.most_common(2):
        if n >= 2:
            patterns.append(f"{n} pieces from {src}")

    return patterns


# ---------------------------------------------------------------------------
# Sonnet email generation
# ---------------------------------------------------------------------------

DIGEST_PROMPT = """\
You are writing a personalised weekly reading digest for Stav. Stav is a founder and builder who tracks his active projects carefully. He reads articles on AI, product, strategy, sales, and personal productivity.

Today is {today}.
Articles captured this week ({article_count} total, showing {highlight_count} highlights + {also_count} also-captured):

HIGHLIGHTS (ranked by relevance and richness):
{highlights_section}

ALSO CAPTURED:
{also_section}

PATTERNS THIS WEEK:
{patterns_section}

Write an email digest. Rules:
- Subject line format: "Reading Digest — {week_label}" (e.g. "Reading Digest — 28 Apr – 2 May")
- Open with 1 punchy sentence on the week's reading theme (derived from patterns/concepts — make it feel personal, not generic)
- For each HIGHLIGHT: title as a markdown link, source, author if known. Then 2–3 sentences: what it argues + why it might matter to Stav given his projects. Do NOT invent claims — use only the key_claims provided.
- "Also captured" section: a compact bullet list — just title as link + source. No commentary.
- Patterns: weave the most interesting pattern into the opening, and optionally mention one more inline.
- Close with a single line inviting Stav to reply with his take ("Reply with your thoughts to add them to your notes.")
- Tone: direct, smart, no fluff. Like a smart colleague summarising what you missed.
- Format: plain markdown (will be sent as HTML email). Use ## for section headers.
- Do NOT include the subject line in the body.

Output the subject on line 1 as: SUBJECT: <subject>
Then a blank line.
Then the email body in markdown.
"""


def build_digest_email(articles: list[dict], patterns: list[str]) -> tuple[str, str]:
    """
    Returns (subject, html_body).
    """
    ranked = rank_articles(articles)
    highlights = ranked[:4]
    also = ranked[4:]

    today = datetime.now(timezone.utc)
    week_start = (today - timedelta(days=6)).strftime("%-d %b")
    week_end = today.strftime("%-d %b")
    week_label = f"{week_start} – {week_end}"

    def fmt_highlight(a: dict) -> str:
        claims = a.get("key_claims") or []
        relevance = a.get("relevance_to") or []
        lines = [
            f"### [{a['title']}]({a['source_url']})",
            f"*{a.get('source_name', '')}*" + (f" · {a['author']}" if a.get("author") and a["author"] not in ("Unknown", "") else ""),
            f"Relevance: {', '.join(relevance) if relevance else 'none detected'}",
        ]
        if claims:
            lines.append("Key claims:")
            for c in claims[:4]:
                lines.append(f"- {c}")
        return "\n".join(lines)

    def fmt_also(a: dict) -> str:
        return f"- [{a['title']}]({a['source_url']}) — {a.get('source_name', '')}"

    highlights_section = "\n\n".join(fmt_highlight(a) for a in highlights)
    also_section = "\n".join(fmt_also(a) for a in also) if also else "(none)"
    patterns_section = "\n".join(f"- {p}" for p in patterns) if patterns else "(no clear patterns this week)"

    prompt = DIGEST_PROMPT.format(
        today=today.strftime("%A, %-d %B %Y"),
        article_count=len(articles),
        highlight_count=len(highlights),
        also_count=len(also),
        highlights_section=highlights_section,
        also_section=also_section,
        patterns_section=patterns_section,
        week_label=week_label,
    )

    logger.info("Calling Sonnet for digest email (%d articles)", len(articles))
    response = _claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()

    # Parse subject from first line
    lines = raw.splitlines()
    subject = "Reading Digest"
    body_start = 0
    for i, line in enumerate(lines):
        if line.startswith("SUBJECT:"):
            subject = line[8:].strip()
            body_start = i + 2  # skip blank line after
            break

    body_md = "\n".join(lines[body_start:]).strip()
    html_body = _md_to_html(body_md)

    return subject, html_body


def _md_to_html(md: str) -> str:
    """Minimal markdown → HTML for email. Handles ##, **bold**, [link](url), - bullets, paragraphs."""
    lines = md.splitlines()
    html_parts = []
    in_ul = False

    for line in lines:
        # Headers
        if line.startswith("### "):
            if in_ul:
                html_parts.append("</ul>")
                in_ul = False
            html_parts.append(f"<h3>{_inline_md(line[4:])}</h3>")
        elif line.startswith("## "):
            if in_ul:
                html_parts.append("</ul>")
                in_ul = False
            html_parts.append(f"<h2>{_inline_md(line[3:])}</h2>")
        elif line.startswith("# "):
            if in_ul:
                html_parts.append("</ul>")
                in_ul = False
            html_parts.append(f"<h1>{_inline_md(line[2:])}</h1>")
        elif line.startswith("- "):
            if not in_ul:
                html_parts.append("<ul>")
                in_ul = True
            html_parts.append(f"<li>{_inline_md(line[2:])}</li>")
        elif line.strip() == "":
            if in_ul:
                html_parts.append("</ul>")
                in_ul = False
            html_parts.append("")
        else:
            if in_ul:
                html_parts.append("</ul>")
                in_ul = False
            html_parts.append(f"<p>{_inline_md(line)}</p>")

    if in_ul:
        html_parts.append("</ul>")

    # Wrap in minimal email style
    body_inner = "\n".join(html_parts)
    return f"""<html><body style="font-family:Georgia,serif;max-width:640px;margin:0 auto;color:#222;line-height:1.6">
{body_inner}
<hr style="margin-top:40px;border:none;border-top:1px solid #ddd">
<p style="font-size:12px;color:#888">Reply to this email to add your take to any article. It will be saved to your notes.</p>
</body></html>"""


def _inline_md(text: str) -> str:
    """Convert inline markdown (bold, links, italic) to HTML."""
    # Links [text](url)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', text)
    # Bold **text**
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    # Italic *text*
    text = re.sub(r'\*(.+?)\*', r'<em>\1</em>', text)
    return text


# ---------------------------------------------------------------------------
# Send via Gmail API
# ---------------------------------------------------------------------------

def send_digest_email(subject: str, html_body: str) -> str:
    """
    Send the digest email using the Gmail API.
    Returns the sent message ID.
    """
    import base64
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{DIGEST_SENDER_NAME} <me>"
    msg["To"] = DIGEST_RECIPIENT
    msg.attach(MIMEText(html_body, "html"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    service = _get_gmail_service()
    sent = service.users().messages().send(
        userId="me",
        body={"raw": raw},
    ).execute()

    logger.info("Digest email sent: messageId=%s to=%s subject=%r", sent["id"], DIGEST_RECIPIENT, subject)
    return sent["id"]


# ---------------------------------------------------------------------------
# Reply write-back (my_take)
# ---------------------------------------------------------------------------

def handle_digest_reply(subject: str, text_body: str) -> bool:
    """
    When Stav replies to a digest, extract his take and write it back
    to all articles linked in the reply (or the whole digest if ambiguous).

    Returns True if at least one article was updated.
    """
    my_take = _extract_take_from_reply(text_body)
    if not my_take:
        logger.info("Digest reply received but no take extracted")
        return False

    # Find articles referenced in the reply (by URL or title fragment)
    service = _get_drive_service()
    week_articles = scan_reading_library(days=8)  # slightly wider window

    updated = []
    urls_in_reply = re.findall(r'https?://[^\s<>"\']+', text_body)

    if urls_in_reply:
        # Write my_take to specifically mentioned articles
        for article in week_articles:
            for url in urls_in_reply:
                if _urls_match(article["source_url"], url):
                    if _write_my_take(service, article, my_take):
                        updated.append(article["title"])
    else:
        # No specific article — apply take to the whole digest (unlikely but handle gracefully)
        logger.info("Reply has no URLs — my_take recorded but not attributed to specific article")
        return False

    logger.info("my_take written back to %d articles: %s", len(updated), updated)
    return len(updated) > 0


def _extract_take_from_reply(text_body: str) -> str:
    """
    Strip quoted reply text and return the new content.
    """
    lines = text_body.splitlines()
    take_lines = []
    for line in lines:
        stripped = line.strip()
        # Gmail/Apple Mail quote markers
        if stripped.startswith(">") or stripped.startswith("On ") and "wrote:" in stripped:
            break
        take_lines.append(line)

    take = "\n".join(take_lines).strip()
    # Remove digest footer if accidentally included
    take = re.sub(r'Reply to this email.*$', '', take, flags=re.DOTALL | re.IGNORECASE).strip()
    return take


def _urls_match(a: str, b: str) -> bool:
    """Loose URL comparison — strip trailing slashes and fragments."""
    def normalise(u: str) -> str:
        u = u.lower().rstrip("/").split("#")[0].split("?")[0]
        return u
    return normalise(a) == normalise(b)


def _write_my_take(service, article: dict, my_take: str) -> bool:
    """
    Update the my_take field in the article's markdown file in Drive.
    Returns True on success.
    """
    try:
        content = article["raw_content"]
        fm = article["raw_frontmatter"]

        # Replace my_take line in the frontmatter block
        # The rendered line is always: my_take: "" or my_take: "..."
        escaped = my_take.replace('"', '\\"').replace('\n', ' ')
        new_fm_line = f'my_take: "{escaped}"'

        if 'my_take: ""' in content:
            updated_content = content.replace('my_take: ""', new_fm_line, 1)
        elif re.search(r'my_take: ".*?"', content):
            updated_content = re.sub(r'my_take: ".*?"', new_fm_line, content, count=1)
        else:
            # Inject before closing ---
            updated_content = content.replace("\n---\n", f"\n{new_fm_line}\n---\n", 1)

        media = MediaInMemoryUpload(
            updated_content.encode("utf-8"),
            mimetype="text/markdown",
        )
        service.files().update(
            fileId=article["file_id"],
            media_body=media,
        ).execute()

        logger.info("my_take written to: %s", article["filename"])
        return True
    except Exception as e:
        logger.error("Failed to write my_take to %s: %s", article.get("filename"), e)
        return False


# ---------------------------------------------------------------------------
# Top-level entrypoint
# ---------------------------------------------------------------------------

def run_digest(days: int = 7) -> dict:
    """
    Full digest pipeline: scan → rank → generate → send.
    Returns a summary dict.
    """
    articles = scan_reading_library(days=days)

    if not articles:
        logger.info("No articles in last %d days — digest skipped", days)
        return {"status": "skipped", "reason": "no articles", "count": 0}

    patterns = detect_patterns(articles)
    subject, html_body = build_digest_email(articles, patterns)
    message_id = send_digest_email(subject, html_body)

    return {
        "status": "sent",
        "message_id": message_id,
        "article_count": len(articles),
        "subject": subject,
        "patterns": patterns,
    }
