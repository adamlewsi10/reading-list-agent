# Reading List Agent

Webhook-driven service that receives forwarded articles via [AgentMail](https://agentmail.to), categorises them with Claude (Haiku), and stores them in a Google Sheet.

## How it works

1. Forward an article/link/newsletter to `importantlocation345@agentmail.to`
2. AgentMail fires a webhook to this service
3. Service extracts URLs, fetches article content
4. Claude Haiku categorises and summarises the article
5. Row is appended to the "Reading Library" Google Sheet
6. Message is labelled as "processed" in AgentMail

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
