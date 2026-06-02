"""
Phase 0 — Reconcile mode.

Rebuilds the Reading Library Sheet mirror from canonical data:

    * index.json    (Drive — set of captured URLs with date_captured + filename)
    * markdown files in the Reading Library folder (Phase 2 frontmatter)

Idempotent: every Sheet row is matched against `canonical_url`, so running
this repeatedly only ever inserts the rows that are missing. Also detects
and removes stray non-article rows (the Sheet's own URL, the AgentMail
inbox address, and any other single-cell rows where the only value is a
URL or email — typical "I pasted my own bookmark into the sheet" stray).

Usage (production — Railway env injected):

    railway run --service reading-list-agent -- python -m app.reconcile
    railway run --service reading-list-agent -- python -m app.reconcile --dry-run

Exits 0 on success and prints a JSON summary:

    {
      "rows_before": N,
      "stray_rows_found": [r1, r2, ...],
      "stray_rows_deleted": K,
      "rows_to_append": M,
      "rows_appended": M,
      "rows_after": N - K + M,
      "skipped_existing_urls": X,
      "dry_run": false
    }
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from typing import Optional

from app.config import (
    READING_LIBRARY_FOLDER_ID,
    READING_LIBRARY_SHEET_ID,
    READING_LIBRARY_SHEET_TAB,
)
from app.drive_writer import (
    _get_drive_service,
    canonical_url,
    load_index,
)
from app.digest import _parse_frontmatter
from app.sheets_writer import (
    SHEET_COLUMNS,  # noqa: F401 (kept for downstream callers)
    _get_sheets_service,
    _row_from_capture,
    append_rows,
    delete_rows,
    read_sheet_rows,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("reconcile")


# ---------------------------------------------------------------------------
# Drive helpers
# ---------------------------------------------------------------------------

def _list_markdown_files(drive) -> dict[str, str]:
    """Return {filename: file_id} for every markdown in the library folder."""
    out: dict[str, str] = {}
    page_token: Optional[str] = None
    while True:
        params = dict(
            q=(
                f"'{READING_LIBRARY_FOLDER_ID}' in parents "
                "and mimeType='text/markdown' "
                "and trashed=false"
            ),
            fields="nextPageToken, files(id, name)",
            pageSize=100,
        )
        if page_token:
            params["pageToken"] = page_token
        res = drive.files().list(**params).execute()
        for f in res.get("files", []):
            out[f["name"]] = f["id"]
        page_token = res.get("nextPageToken")
        if not page_token:
            break
    return out


def _read_frontmatter(drive, file_id: str) -> dict:
    """Read a markdown file from Drive and return its parsed frontmatter (or {})."""
    try:
        content_bytes = drive.files().get_media(fileId=file_id).execute()
        content = content_bytes.decode("utf-8", errors="replace")
        fm, _body = _parse_frontmatter(content)
        return fm
    except Exception as e:
        logger.warning("frontmatter read failed for file_id=%s: %s", file_id, e)
        return {}


def _title_from(fm: dict, entry: dict) -> str:
    """Best title for a backfill row."""
    # Index entries don't carry titles; filenames are "YYYY-MM-DD Title — Source.md".
    filename = entry.get("filename", "")
    if filename:
        name = filename.removesuffix(".md")
        if len(name) > 11 and name[10] == " ":
            name = name[11:]
        if " — " in name:
            name = name.split(" — ")[0]
        return name.strip()
    return entry.get("title", "") or ""


# ---------------------------------------------------------------------------
# Sheet inspection
# ---------------------------------------------------------------------------

URL_RX = re.compile(r"https?://[^\s<>\"']+")
EMAIL_RX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _existing_canonical_urls(sheet_rows: list[list[str]]) -> set[str]:
    """Canonicalised URLs currently present in column E (index 4), skipping the header."""
    existing: set[str] = set()
    for row in sheet_rows[1:]:
        if len(row) > 4 and row[4]:
            existing.add(canonical_url(row[4]))
    return existing


def _stray_row_indices(sheet_rows: list[list[str]]) -> list[int]:
    """
    Identify rows that are not real article captures. Returns 1-based row numbers.

    A row is "stray" when it has at most one non-empty cell AND that cell is
    either a URL pointing at the Sheet itself, the AgentMail inbox address,
    or any other lone URL/email that's clearly a misplaced manual paste.

    Real article rows always have at least Title (col B) AND something in
    columns C–E populated, so they are never matched.
    """
    strays: list[int] = []
    sheet_id_lower = (READING_LIBRARY_SHEET_ID or "").lower()
    for i, row in enumerate(sheet_rows[1:], start=2):  # data begins on row 2
        non_empty = [c for c in row if (c or "").strip()]
        if len(non_empty) != 1:
            continue
        val = non_empty[0].strip()
        val_low = val.lower()

        looks_like_sheet_url = (
            sheet_id_lower
            and "docs.google.com/spreadsheets" in val_low
            and sheet_id_lower in val_low
        )
        looks_like_inbox = "importantlocation345@agentmail.to" in val_low
        looks_like_lone_url = bool(URL_RX.fullmatch(val))
        looks_like_lone_email = bool(EMAIL_RX.fullmatch(val))

        if looks_like_sheet_url or looks_like_inbox or looks_like_lone_url or looks_like_lone_email:
            strays.append(i)
    return strays


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------

def run(dry_run: bool = False) -> dict:
    drive = _get_drive_service()
    sheets = _get_sheets_service()

    index = load_index(drive)
    entries = list(index.get("entries", []))

    files_by_name = _list_markdown_files(drive)
    sheet_rows = read_sheet_rows(sheets)
    rows_before = max(0, len(sheet_rows) - 1)
    existing = _existing_canonical_urls(sheet_rows)

    # ---- 1. Stray rows ----
    strays = _stray_row_indices(sheet_rows)

    # ---- 2. Missing rows (in index.json, not in Sheet) ----
    to_append: list[list[str]] = []
    skipped_existing = 0
    for e in entries:
        url = (e.get("url") or "").strip()
        if not url:
            continue
        if canonical_url(url) in existing:
            skipped_existing += 1
            continue
        # Read frontmatter from the canonical markdown file when we can.
        file_id = files_by_name.get(e.get("filename", ""))
        fm = _read_frontmatter(drive, file_id) if file_id else {}

        title = _title_from(fm, e)
        source = (fm.get("source_name") or e.get("source") or "").strip()
        author = (fm.get("author") or "").strip()
        # For backfill we have no original sender — leave blank rather than guess.
        forwarded_by = ""
        # date_captured from index.json is YYYY-MM-DD; that's enough for "Date Added".
        date_added = (e.get("date_captured") or fm.get("date_captured") or "").strip() or None

        row = _row_from_capture(
            title=title,
            author=author,
            source=source,
            url=url,
            forwarded_by=forwarded_by,
            frontmatter=fm,
            date_added=date_added,
        )
        to_append.append(row)

    # Append in chronological order so the Sheet reads naturally.
    to_append.sort(key=lambda r: r[0])

    summary = {
        "rows_before": rows_before,
        "stray_rows_found": strays,
        "stray_rows_deleted": 0,
        "rows_to_append": len(to_append),
        "rows_appended": 0,
        "rows_after": rows_before,
        "skipped_existing_urls": skipped_existing,
        "dry_run": dry_run,
    }

    if dry_run:
        logger.info(
            "DRY RUN — would delete %d stray rows %s and append %d new rows",
            len(strays), strays, len(to_append),
        )
        return summary

    # ---- 3. Apply changes ----
    if strays:
        summary["stray_rows_deleted"] = delete_rows(strays, service=sheets)

    if to_append:
        summary["rows_appended"] = append_rows(to_append, service=sheets)

    # Re-read for final count.
    after_rows = read_sheet_rows(sheets)
    summary["rows_after"] = max(0, len(after_rows) - 1)

    return summary


def main():
    p = argparse.ArgumentParser(
        description="Rebuild the Reading Library Sheet from canonical data.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute the diff but don't modify the Sheet.",
    )
    args = p.parse_args()

    summary = run(dry_run=args.dry_run)
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
