import logging
from urllib.parse import urlparse

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from app.config import GOOGLE_SHEETS_ID, GOOGLE_OAUTH_JSON

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _get_sheets_service():
    """Build an authenticated Sheets API service using OAuth refresh token."""
    creds = Credentials(
        token=None,
        refresh_token=GOOGLE_OAUTH_JSON["refresh_token"],
        token_uri=GOOGLE_OAUTH_JSON["token_uri"],
        client_id=GOOGLE_OAUTH_JSON["client_id"],
        client_secret=GOOGLE_OAUTH_JSON["client_secret"],
        scopes=SCOPES,
    )
    return build("sheets", "v4", credentials=creds)


def _normalize_url(url: str) -> str:
    """Normalize a URL for dedup comparison (strip trailing slash, lowercase)."""
    url = url.strip().lower().rstrip("/")
    # Remove common tracking query params
    parsed = urlparse(url)
    clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return clean


def get_existing_urls() -> set[str]:
    """Read existing URLs from column E of the sheet for dedup."""
    try:
        service = _get_sheets_service()
        result = service.spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEETS_ID,
            range="Sheet1!E:E",
        ).execute()
        values = result.get("values", [])
        urls = set()
        for row in values:
            if row and row[0]:
                urls.add(_normalize_url(row[0]))
        logger.info("Loaded %d existing URLs from sheet for dedup", len(urls))
        return urls
    except Exception as e:
        logger.warning("Failed to read existing URLs for dedup: %s", e)
        return set()


def is_duplicate(url: str) -> bool:
    """Check if a URL already exists in the sheet."""
    if not url:
        return False
    existing = get_existing_urls()
    normalized = _normalize_url(url)
    return normalized in existing


def write_to_sheet(row_data: dict) -> bool:
    """Append a single row to the Reading Library sheet. Returns False if duplicate."""
    url = row_data.get("url", "")

    # Check for duplicates
    if url and is_duplicate(url):
        logger.info("Duplicate — skipped: %s", url)
        return False

    service = _get_sheets_service()

    row = [
        row_data.get("date_added", ""),
        row_data.get("title", ""),
        row_data.get("author", ""),
        row_data.get("source", ""),
        url,
        row_data.get("topic", ""),
        row_data.get("subtopic", ""),
        row_data.get("summary", ""),
        row_data.get("tags", ""),
        row_data.get("forwarded_by", ""),
        row_data.get("notes", ""),
    ]

    service.spreadsheets().values().append(
        spreadsheetId=GOOGLE_SHEETS_ID,
        range="Sheet1!A:K",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()

    logger.info("Row written to sheet: %s", row_data.get("title", "unknown"))
    return True
