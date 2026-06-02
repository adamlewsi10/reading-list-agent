"""
Phase 0 — Reading Library Sheet mirror.

The markdown file in Drive + index.json are canonical. The Google Sheet is
a one-way, append-only, human-readable mirror. Nothing in the system reads
the Sheet back. If a Sheet append fails (transient API error, missing
OAuth scope, sheet renamed, anything) we log loudly and return False — the
capture pipeline must NOT be blocked.

Tab is "Sheet1" (matching the existing Reading Library Sheet) with the
11-column header:

    Date Added | Title | Author | Source | URL |
    Topic | Subtopic | Summary | Tags | Forwarded By | Notes

Topic / Subtopic / Summary / Tags are best-effort filled from Phase 2
frontmatter (concepts, key_claims, entities_mentioned). Leaving them blank
is acceptable — the brief is explicit about not blocking on them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build

from app.config import (
    GOOGLE_DRIVE_OAUTH_JSON,
    READING_LIBRARY_SHEET_ID,
    READING_LIBRARY_SHEET_TAB,
)

logger = logging.getLogger(__name__)

# Column order MUST match the Sheet's header row.
SHEET_COLUMNS = [
    "Date Added",
    "Title",
    "Author",
    "Source",
    "URL",
    "Topic",
    "Subtopic",
    "Summary",
    "Tags",
    "Forwarded By",
    "Notes",
]

# Full scope set the mirror needs. Spreadsheets is the addition vs. the
# rest of the agent. If the refresh token wasn't issued with this scope,
# `_get_sheets_service` will raise RefreshError(invalid_scope); we catch
# that one explicitly so the operator sees a clear "re-auth required"
# message rather than a noisy stack trace per capture.
SHEETS_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]


class SheetsScopeError(RuntimeError):
    """Raised when the OAuth token lacks the spreadsheets scope."""


def _get_sheets_service():
    """
    Build a Sheets v4 service from GOOGLE_DRIVE_OAUTH_JSON.

    Raises:
        SheetsScopeError: token doesn't include the spreadsheets scope.
            Run scripts/reauth_oauth.py and update GOOGLE_DRIVE_OAUTH_JSON
            on Railway.
    """
    oauth = GOOGLE_DRIVE_OAUTH_JSON
    creds = Credentials(
        token=None,  # force a refresh so the access token honours all scopes
        refresh_token=oauth["refresh_token"],
        token_uri=oauth.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=oauth["client_id"],
        client_secret=oauth["client_secret"],
        scopes=SHEETS_SCOPES,
    )
    try:
        creds.refresh(Request())
    except RefreshError as e:
        msg = str(e).lower()
        if "invalid_scope" in msg or "invalid scope" in msg:
            raise SheetsScopeError(
                "GOOGLE_DRIVE_OAUTH_JSON refresh token does not include the "
                "spreadsheets scope — re-auth required "
                "(see scripts/reauth_oauth.py)."
            ) from e
        raise
    return build("sheets", "v4", credentials=creds)


def _now_str() -> str:
    """Date Added string. Matches the Sheet's existing pattern (UTC date + HH:MM)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")


def _row_from_capture(
    *,
    title: str,
    author: str,
    source: str,
    url: str,
    forwarded_by: str,
    frontmatter: Optional[dict],
    notes: str = "",
    date_added: Optional[str] = None,
) -> list[str]:
    """
    Build the 11-column row in the order matching SHEET_COLUMNS.

    Topic / Subtopic / Summary / Tags are derived from Phase 2 frontmatter:

        Topic       = concepts[0]
        Subtopic    = concepts[1]
        Summary     = key_claims[0]   (Phase 2 doesn't generate prose summaries)
        Tags        = remaining concepts + entities_mentioned (comma-joined)
    """
    fm = frontmatter or {}
    concepts = list(fm.get("concepts") or [])
    entities = list(fm.get("entities_mentioned") or [])
    key_claims = list(fm.get("key_claims") or [])
    fetch_status = fm.get("fetch_status", "ok")

    topic = (concepts[0] if concepts else "").strip()
    subtopic = (concepts[1] if len(concepts) > 1 else "").strip()
    summary = (key_claims[0] if key_claims else "").strip()
    tag_terms = [c.strip() for c in concepts[2:] if c and c.strip()]
    tag_terms += [e.strip() for e in entities if e and e.strip()]
    tags = ", ".join(dict.fromkeys(tag_terms))  # dedupe, preserve order

    composite_notes = notes or ""
    if fetch_status != "ok":
        prefix = f"fetch_status: {fetch_status}"
        composite_notes = prefix if not composite_notes else f"{prefix}; {composite_notes}"

    return [
        date_added or _now_str(),
        title or "",
        author or "",
        source or "",
        url or "",
        topic,
        subtopic,
        summary,
        tags,
        forwarded_by or "",
        composite_notes,
    ]


def append_capture_row(
    *,
    title: str,
    author: str,
    source: str,
    url: str,
    forwarded_by: str,
    frontmatter: Optional[dict] = None,
    notes: str = "",
) -> bool:
    """
    Append one mirror row for a fresh capture.

    Never raises. Returns True on success, False on any failure (including
    a missing spreadsheets scope). The capture pipeline must call this
    AFTER the canonical markdown write has succeeded.
    """
    if not READING_LIBRARY_SHEET_ID:
        logger.info("Sheet mirror disabled (READING_LIBRARY_SHEET_ID is empty) — skipping append")
        return False

    row = _row_from_capture(
        title=title,
        author=author,
        source=source,
        url=url,
        forwarded_by=forwarded_by,
        frontmatter=frontmatter,
        notes=notes,
    )

    try:
        service = _get_sheets_service()
        result = service.spreadsheets().values().append(
            spreadsheetId=READING_LIBRARY_SHEET_ID,
            range=f"{READING_LIBRARY_SHEET_TAB}!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()
        updated_range = result.get("updates", {}).get("updatedRange", "?")
        logger.info("Sheet mirror appended: %s", updated_range)
        return True
    except SheetsScopeError as e:
        logger.error(
            "Sheet mirror DISABLED — %s "
            "Capture not affected; markdown + index.json still canonical.",
            e,
        )
        return False
    except Exception as e:
        logger.error(
            "Sheet mirror append failed (capture not affected): %s",
            e,
            exc_info=True,
        )
        return False


# ---------------------------------------------------------------------------
# Reconcile-mode helpers (used by app/reconcile.py)
# ---------------------------------------------------------------------------

def read_sheet_rows(service=None) -> list[list[str]]:
    """Return the Sheet's current rows (incl. header) as a list of lists."""
    svc = service or _get_sheets_service()
    res = svc.spreadsheets().values().get(
        spreadsheetId=READING_LIBRARY_SHEET_ID,
        range=f"{READING_LIBRARY_SHEET_TAB}!A:K",
    ).execute()
    return res.get("values", [])


def append_rows(rows: list[list[str]], service=None) -> int:
    """Bulk-append rows to the Sheet. Returns the number appended."""
    if not rows:
        return 0
    svc = service or _get_sheets_service()
    svc.spreadsheets().values().append(
        spreadsheetId=READING_LIBRARY_SHEET_ID,
        range=f"{READING_LIBRARY_SHEET_TAB}!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()
    return len(rows)


def get_tab_sheet_id(service=None) -> Optional[int]:
    """
    Resolve the numeric sheetId for READING_LIBRARY_SHEET_TAB.
    Needed for deleteDimension requests (which take a numeric sheetId, not the tab name).
    """
    svc = service or _get_sheets_service()
    meta = svc.spreadsheets().get(spreadsheetId=READING_LIBRARY_SHEET_ID).execute()
    for s in meta.get("sheets", []):
        props = s.get("properties", {})
        if props.get("title") == READING_LIBRARY_SHEET_TAB:
            return props.get("sheetId")
    return None


def delete_rows(row_numbers_1based: list[int], service=None) -> int:
    """
    Delete rows by their 1-based row number in the Sheet. Empty list is a no-op.
    Deletes from the bottom up so earlier indices stay valid.
    """
    if not row_numbers_1based:
        return 0
    svc = service or _get_sheets_service()
    tab_id = get_tab_sheet_id(svc)
    if tab_id is None:
        raise RuntimeError(
            f"Could not resolve numeric sheetId for tab {READING_LIBRARY_SHEET_TAB!r}"
        )
    requests = []
    for rn in sorted(set(row_numbers_1based), reverse=True):
        requests.append({
            "deleteDimension": {
                "range": {
                    "sheetId": tab_id,
                    "dimension": "ROWS",
                    "startIndex": rn - 1,
                    "endIndex": rn,
                }
            }
        })
    svc.spreadsheets().batchUpdate(
        spreadsheetId=READING_LIBRARY_SHEET_ID,
        body={"requests": requests},
    ).execute()
    return len(requests)
