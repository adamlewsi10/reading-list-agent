import logging

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


def write_to_sheet(row_data: dict) -> None:
    """Append a single row to the Reading Library sheet."""
    service = _get_sheets_service()

    row = [
        row_data.get("date_added", ""),
        row_data.get("title", ""),
        row_data.get("author", ""),
        row_data.get("source", ""),
        row_data.get("url", ""),
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

    logger.info(f"Row written to sheet: {row_data.get('title', 'unknown')}")
