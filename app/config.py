import os
import json

AGENTMAIL_API_KEY = os.environ["AGENTMAIL_API_KEY"].strip()
AGENTMAIL_INBOX_ID = os.environ["AGENTMAIL_INBOX_ID"].strip()
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"].strip()
READING_LIBRARY_FOLDER_ID = os.environ["READING_LIBRARY_FOLDER_ID"].strip()
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"].strip()

# JSON blob: {token, refresh_token, token_uri, client_id, client_secret, scopes}
GOOGLE_DRIVE_OAUTH_JSON = json.loads(os.environ["GOOGLE_DRIVE_OAUTH_JSON"].strip())

# Phase 0 — Sheet mirror.
#
# The markdown file + index.json remain canonical. The Sheet is an
# append-only, human-readable mirror; nothing reads it back into the
# system. A Sheet-write failure must NOT block a capture.
#
# Default points at the existing Reading Library sheet so a missing env
# var doesn't accidentally silently disable the mirror. Set
# READING_LIBRARY_SHEET_ID to an empty string explicitly to disable it.
READING_LIBRARY_SHEET_ID = os.environ.get(
    "READING_LIBRARY_SHEET_ID",
    "1EDdRTlb7nLbIQ6ekipEM_a_2VfsCup_WX37bstDBEsY",
).strip()
READING_LIBRARY_SHEET_TAB = os.environ.get("READING_LIBRARY_SHEET_TAB", "Sheet1").strip()
