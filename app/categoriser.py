import json
import logging

import anthropic

from app.config import ANTHROPIC_API_KEY

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

CATEGORISE_PROMPT = """Categorise this article for a reading library.

Title: {title}
Source: {source}
URL: {url}
Content (first 3000 chars): {content}

Respond in JSON only, no preamble:
{{
  "title": "cleaned/corrected article title",
  "author": "author name or 'Unknown'",
  "source": "publication/website name",
  "topic": "one of: AI & Agents, HubSpot & CRM, Sales & GTM, Marketing, Leadership, Technology, Business Strategy, Data & Privacy, Events, Other",
  "subtopic": "more specific category within the topic",
  "summary": "2-3 sentence summary of key points",
  "tags": "comma-separated relevant tags, max 5"
}}"""


def categorise_article(
    title: str, content: str, url: str, source: str, author: str
) -> dict:
    """Call Claude Haiku to categorise an article. Returns structured metadata."""
    prompt = CATEGORISE_PROMPT.format(
        title=title,
        source=source,
        url=url,
        content=content[:3000],
    )

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        result = json.loads(response.content[0].text)

        # Preserve the fetched author if Claude returns Unknown but we have one
        if result.get("author", "Unknown") == "Unknown" and author != "Unknown":
            result["author"] = author

        return result

    except (json.JSONDecodeError, anthropic.APIError) as e:
        logger.error(f"Categorisation failed: {e}")
        # Return minimal fallback
        return {
            "title": title,
            "author": author,
            "source": source,
            "topic": "Uncategorised",
            "subtopic": "",
            "summary": "Categorisation failed — review manually",
            "tags": "",
        }
