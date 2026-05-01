import os
import json

AGENTMAIL_API_KEY = os.environ["AGENTMAIL_API_KEY"].strip()
AGENTMAIL_INBOX_ID = os.environ["AGENTMAIL_INBOX_ID"].strip()
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"].strip()
READING_LIBRARY_FOLDER_ID = os.environ["READING_LIBRARY_FOLDER_ID"].strip()
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"].strip()

# JSON blob: {token, refresh_token, token_uri, client_id, client_secret, scopes}
GOOGLE_DRIVE_OAUTH_JSON = json.loads(os.environ["GOOGLE_DRIVE_OAUTH_JSON"].strip())
