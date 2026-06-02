# Reading List Agent

Webhook-driven service that receives forwarded articles via [AgentMail](https://agentmail.to), generates Phase 2 structured frontmatter via Claude Haiku, writes a canonical markdown file to Google Drive, mirrors the row to a human-readable Google Sheet, and sends a weekly Friday digest via Claude Sonnet.

## How it works (Phase 0)

1. Forward an article/link/newsletter to `importantlocation345@agentmail.to`.
2. AgentMail fires a Svix-signed webhook at `POST /webhooks`.
3. The service extracts URLs from the email (filters out HubSpot/Mailchimp tracking links).
4. Looks the canonical URL up against `index.json` in Drive — duplicates are dropped.
5. Fetches article content (User-Agent, redirect handling, brotli-safe).
6. Extracts title (og:title > title tag > h1 > email subject).
7. Claude Haiku generates Phase 2 frontmatter (`concepts`, `entities_mentioned`, `key_claims`, `publication_date`); `relevance_to` is matched against `active-projects.json`.
8. **Canonical write:** the article is saved as `[YYYY-MM-DD] Title — Source.md` in `Second Brain/Reading Library/` and `index.json` is updated.
9. **Mirror write (Phase 0):** one row is appended to the Reading Library Sheet (tab `Sheet1`). This is best-effort — a Sheet write failure is logged but does not affect the canonical capture.
10. The message is labelled `processed` in AgentMail.

The Friday 14:00 UK digest scans the last 7 days of the Reading Library, ranks by relevance density, and emails Stav via Gmail. Replying to a digest with a take + the article URL writes that take back into the markdown file's `my_take` field.

## Mirror rule

The markdown file + `index.json` are **canonical**. The Sheet is an append-only, human-readable mirror; nothing in the system ever reads it back. If the Sheet and the canonical data disagree, the canonical wins — run `python -m app.reconcile` to rebuild the Sheet from scratch.

## Environment variables

| Variable | Description |
|---|---|
| `AGENTMAIL_API_KEY` | AgentMail API key |
| `AGENTMAIL_INBOX_ID` | AgentMail inbox ID (e.g. `importantlocation345@agentmail.to`) |
| `WEBHOOK_SECRET` | Svix webhook secret from the AgentMail webhook registration |
| `ANTHROPIC_API_KEY` | Anthropic API key for Claude (Haiku + Sonnet) |
| `READING_LIBRARY_FOLDER_ID` | Drive folder ID for `Reading Library/` |
| `GOOGLE_DRIVE_OAUTH_JSON` | OAuth JSON with `token`, `refresh_token`, `token_uri`, `client_id`, `client_secret`, `scopes`. Must include `drive`, `gmail.send`, `gmail.readonly`, **and `spreadsheets`** (the last is required for the Sheet mirror — see `scripts/reauth_oauth.py`). |
| `READING_LIBRARY_SHEET_ID` | (optional) Google Sheet ID for the mirror. Defaults to the production sheet (`1EDdRTlb7nLbIQ6ekipEM_a_2VfsCup_WX37bstDBEsY`). Set to empty string to disable the mirror entirely. |
| `READING_LIBRARY_SHEET_TAB` | (optional) Tab name. Defaults to `Sheet1`. |
| `DIGEST_TRIGGER_SECRET` | (optional) Bearer secret for `POST /trigger-digest`. If unset, the endpoint is open. |

## Reconcile mode

`reconcile` rebuilds the Sheet mirror from canonical data. It is idempotent (matches existing rows by canonical URL) and cleans up stray non-article rows (the Sheet's own URL, the AgentMail address, lone URLs/emails pasted by hand).

```bash
# Dry run — print the diff without writing
railway run --service reading-list-agent -- python -m app.reconcile --dry-run

# Apply
railway run --service reading-list-agent -- python -m app.reconcile
```

Output is a JSON summary: `rows_before`, `stray_rows_found`, `stray_rows_deleted`, `rows_to_append`, `rows_appended`, `rows_after`, `skipped_existing_urls`.

## Re-auth (one-time, for the Sheet mirror)

The original `GOOGLE_DRIVE_OAUTH_JSON` (issued 2026-05-01) only covers `drive + gmail.send + gmail.readonly`. The Sheet mirror needs `spreadsheets` added. Google refresh tokens are pinned to the scope set they were issued with, so you have to re-run the OAuth flow once:

```bash
# Pull the existing client_id/secret out of Railway and write a temp credentials.json
railway run --service reading-list-agent -- python3 -c \
    'import os, json; o=json.loads(os.environ["GOOGLE_DRIVE_OAUTH_JSON"]); \
     print(json.dumps({"installed": {"client_id": o["client_id"], "client_secret": o["client_secret"], \
        "auth_uri": "https://accounts.google.com/o/oauth2/auth", \
        "token_uri": "https://oauth2.googleapis.com/token", \
        "redirect_uris": ["http://localhost"]}}, indent=2))' \
    > /tmp/reading-list-credentials.json

# Run the re-auth flow (opens a browser; sign in as ops@adamlewis.info)
python3 scripts/reauth_oauth.py --credentials /tmp/reading-list-credentials.json

# Copy the printed JSON and update Railway
railway variables set GOOGLE_DRIVE_OAUTH_JSON='<paste the JSON>'
```

Until this is done, the Sheet mirror is disabled with a clear log line (`Sheet mirror DISABLED — … re-auth required`). Canonical capture is unaffected.

## Deploy

Deployed on Railway as service `reading-list-agent` in project `mail-agent-reading-list`. Public URL: `https://web-production-bd094.up.railway.app`.

## Local development

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```
