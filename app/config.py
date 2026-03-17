import os
import json


AGENTMAIL_API_KEY = os.environ["AGENTMAIL_API_KEY"]
AGENTMAIL_INBOX_ID = os.environ["AGENTMAIL_INBOX_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_SHEETS_ID = os.environ["GOOGLE_SHEETS_ID"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

# OAuth credentials for Google Sheets — JSON string containing
# refresh_token, client_id, client_secret, token_uri
GOOGLE_OAUTH_JSON = json.loads(os.environ["GOOGLE_OAUTH_JSON"])
