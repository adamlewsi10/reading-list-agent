#!/usr/bin/env python3
"""
Re-issue the reading-list-agent's GOOGLE_DRIVE_OAUTH_JSON with the
spreadsheets scope added (in addition to drive + gmail.send + gmail.readonly).

Background: the original token (issued 2026-05-01) only covers
[drive, gmail.send, gmail.readonly]. The Phase 0 Sheet mirror needs
`spreadsheets`. Google refresh tokens are pinned to the scope set they
were issued with, so we have to run the OAuth flow again with the wider
scope list — this script does exactly that.

Usage (run locally on Adam's mac, NOT inside Railway):

    # Option A — re-use the existing OAuth client (recommended):
    #   The existing client_id/secret live inside Railway's GOOGLE_DRIVE_OAUTH_JSON.
    #   Pull them down and write a temp credentials.json, then re-auth.
    cd ~/reading-list-agent
    railway run --service reading-list-agent -- python3 -c \\
        'import os, json; o=json.loads(os.environ["GOOGLE_DRIVE_OAUTH_JSON"]); \\
         print(json.dumps({"installed": {"client_id": o["client_id"], \\
            "client_secret": o["client_secret"], \\
            "auth_uri": "https://accounts.google.com/o/oauth2/auth", \\
            "token_uri": "https://oauth2.googleapis.com/token", \\
            "redirect_uris": ["http://localhost"]}}, indent=2))' \\
        > /tmp/reading-list-credentials.json
    python3 scripts/reauth_oauth.py --credentials /tmp/reading-list-credentials.json

    # Option B — use a credentials.json from elsewhere (e.g. personal-google-mcp):
    python3 scripts/reauth_oauth.py --credentials ~/personal-google-mcp/credentials.json

The script prints the new JSON blob. Copy it and set it on Railway:

    railway variables set GOOGLE_DRIVE_OAUTH_JSON='<paste here>'

Sign in as ops@adamlewis.info when the browser opens.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    print(
        "ERROR: google_auth_oauthlib not installed.\n"
        "  pip install google-auth-oauthlib",
        file=sys.stderr,
    )
    sys.exit(1)


SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--credentials",
        required=True,
        type=Path,
        help="Path to an OAuth client credentials.json (Installed App type).",
    )
    p.add_argument(
        "--port",
        type=int,
        default=0,
        help="Port for the local OAuth callback server (0 = auto-pick).",
    )
    args = p.parse_args()

    if not args.credentials.exists():
        print(f"ERROR: credentials file not found: {args.credentials}", file=sys.stderr)
        sys.exit(2)

    flow = InstalledAppFlow.from_client_secrets_file(str(args.credentials), SCOPES)
    creds = flow.run_local_server(port=args.port)

    # Compose the JSON blob in the same shape config.py reads.
    payload = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or SCOPES),
    }

    print("\n" + "=" * 60)
    print("NEW GOOGLE_DRIVE_OAUTH_JSON  (set this on Railway):")
    print("=" * 60)
    print(json.dumps(payload, indent=2))
    print("=" * 60)
    print("\nTo apply:")
    print("  railway variables set GOOGLE_DRIVE_OAUTH_JSON='<paste the JSON above>'")
    print()


if __name__ == "__main__":
    main()
