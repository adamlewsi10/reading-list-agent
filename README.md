# Reading List Agent

Webhook-driven service that receives forwarded articles via [AgentMail](https://agentmail.to), categorises them with Claude (Haiku), and stores them in a Google Sheet.

## How it works

1. Forward an article/link/newsletter to `importantlocation345@agentmail.to`
2. AgentMail fires a webhook to this service
3. Service extracts URLs from the email, filtering out tracking/junk links (HubSpot, Mailchimp, etc.)
4. Checks for duplicate URLs against existing sheet entries
5. Fetches article content (with proper User-Agent, redirect handling)
6. Extracts title (og:title > title tag > h1 > email subject)
7. Claude Haiku categorises into topics/subtopics, generates summary and tags
8. Row is appended to the "Reading Library" Google Sheet
9. Message is labelled as "processed" in AgentMail

## Environment variables

| Variable | Description |
|---|---|
| `AGENTMAIL_API_KEY` | AgentMail API key |
| `AGENTMAIL_INBOX_ID` | AgentMail inbox ID |
| `ANTHROPIC_API_KEY` | Anthropic API key for Claude |
| `GOOGLE_SHEETS_ID` | Google Sheet ID for Reading Library |
| `GOOGLE_OAUTH_JSON` | JSON with `refresh_token`, `client_id`, `client_secret`, `token_uri` |
| `WEBHOOK_SECRET` | Svix webhook secret from AgentMail webhook registration |

## Deploy

Deployed on Railway. Connect this repo and set the environment variables above.

## Local development

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```
