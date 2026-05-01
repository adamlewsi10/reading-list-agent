import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

from app.config import READING_LIBRARY_FOLDER_ID, GOOGLE_DRIVE_OAUTH_JSON

logger = logging.getLogger(__name__)

INDEX_FILENAME = "index.json"


def _get_drive_service():
    """Build a fresh Drive service each call — avoids stale httplib2 connections."""
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


def canonical_url(url: str) -> str:
    url = url.strip().lower().rstrip("/")
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}{p.path}"


def url_hash(url: str) -> str:
    return hashlib.sha256(canonical_url(url).encode()).hexdigest()[:16]


def sanitize_filename(s: str, max_len: int = 60) -> str:
    s = re.sub(r'[<>:"/\\|?*\n\r\t]', "", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > max_len:
        s = s[:max_len].rsplit(" ", 1)[0]
    return s


def _get_index_file_id(service) -> str | None:
    res = service.files().list(
        q=f"name='{INDEX_FILENAME}' and '{READING_LIBRARY_FOLDER_ID}' in parents and trashed=false",
        fields="files(id)",
    ).execute()
    files = res.get("files", [])
    return files[0]["id"] if files else None


def load_index(service=None) -> dict:
    """Load index.json with retry. Raises on persistent failure (don't bypass dedup)."""
    for attempt in range(3):
        try:
            svc = _get_drive_service()  # Fresh connection each attempt
            file_id = _get_index_file_id(svc)
            if not file_id:
                return {"version": 1, "entries": []}
            content = svc.files().get_media(fileId=file_id).execute()
            return json.loads(content.decode("utf-8"))
        except Exception as e:
            if attempt < 2:
                logger.warning("index.json load attempt %d failed: %s — retrying", attempt + 1, e)
                time.sleep(1.5)
            else:
                logger.error("index.json load failed after 3 attempts — aborting write to prevent duplicates")
                raise


def save_index(service, index: dict) -> None:
    media = MediaInMemoryUpload(
        json.dumps(index, indent=2).encode("utf-8"),
        mimetype="application/json",
    )
    file_id = _get_index_file_id(service)
    if file_id:
        service.files().update(fileId=file_id, media_body=media).execute()
    else:
        meta = {"name": INDEX_FILENAME, "parents": [READING_LIBRARY_FOLDER_ID]}
        service.files().create(body=meta, media_body=media, fields="id").execute()
    logger.info("index.json saved (%d entries)", len(index.get("entries", [])))


def is_duplicate(url: str, index: dict) -> bool:
    if not url:
        return False
    h = url_hash(url)
    return any(e.get("url_hash") == h for e in index.get("entries", []))


def add_to_index(url: str, filename: str, index: dict) -> None:
    index.setdefault("entries", []).append({
        "url": url,
        "url_hash": url_hash(url),
        "filename": filename,
        "date_captured": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    })


def _build_markdown(url: str, title: str, source: str, author: str,
                    body_text: str, fetch_status: str) -> str:
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"""# {title}

source_url: {url}
source_name: {source}
author: {author}
date_captured: {date}
fetch_status: {fetch_status}

---

{body_text or "[No content extracted]"}
"""


def write_article(url: str, title: str, source: str, author: str,
                  body_text: str, fetch_failed: bool) -> bool:
    """
    Write one article as a Drive markdown file and update index.json.
    Returns True if written, False if duplicate.
    """
    service = _get_drive_service()
    index = load_index(service)

    if is_duplicate(url, index):
        logger.info("Duplicate — skipped: %s", url[:80])
        return False

    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fetch_status = "failed" if fetch_failed else "ok"

    title_safe = sanitize_filename(title)
    source_safe = sanitize_filename(source)
    filename = f"{date} {title_safe} — {source_safe}.md"

    content = _build_markdown(url, title, source, author, body_text, fetch_status)

    meta = {"name": filename, "parents": [READING_LIBRARY_FOLDER_ID]}
    media = MediaInMemoryUpload(content.encode("utf-8"), mimetype="text/markdown")
    service.files().create(body=meta, media_body=media, fields="id").execute()

    add_to_index(url, filename, index)
    save_index(service, index)

    logger.info("Article written: %s", filename[:80])
    return True
