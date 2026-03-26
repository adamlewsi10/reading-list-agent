import json
import re
import logging

import anthropic

from app.config import ANTHROPIC_API_KEY

logger = logging.getLogger(__name__)

# Validate API key at startup
if not ANTHROPIC_API_KEY:
    logger.error("ANTHROPIC_API_KEY is empty — categorisation will fail")
elif len(ANTHROPIC_API_KEY) < 20:
    logger.error("ANTHROPIC_API_KEY looks truncated (%d chars) — check Railway env var", len(ANTHROPIC_API_KEY))
else:
    logger.info("ANTHROPIC_API_KEY loaded (%d chars, starts with %s...)", len(ANTHROPIC_API_KEY), ANTHROPIC_API_KEY[:7])

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

CATEGORISE_PROMPT = """You are a content categoriser. Given an article, extract metadata and categorise it.

Title: {title}
Source: {source}
URL: {url}
Content (first 3000 chars): {content}

Respond in JSON only, no preamble, no markdown fences:
{{
  "title": "cleaned/corrected article title",
  "author": "author name or 'Unknown'",
  "source": "publication/website name",
  "topic": "one of: AI & Agents, HubSpot & CRM, Sales & GTM, Marketing, Leadership, Technology, Business Strategy, Data & Privacy, Events, Other",
  "subtopic": "more specific category within the topic",
  "summary": "2-3 sentence summary of key points",
  "tags": "comma-separated relevant tags, max 5"
}}"""

# Minimal prompt for when article content is unavailable
CATEGORISE_MINIMAL_PROMPT = """You are a content categoriser. Given only a title and URL, infer the most likely category.

Title: {title}
Source: {source}
URL: {url}

Respond in JSON only, no preamble, no markdown fences:
{{
  "title": "cleaned/corrected article title",
  "author": "Unknown",
  "source": "publication/website name",
  "topic": "one of: AI & Agents, HubSpot & CRM, Sales & GTM, Marketing, Leadership, Technology, Business Strategy, Data & Privacy, Events, Other",
  "subtopic": "more specific category within the topic",
  "summary": "Article content unavailable — categorised from title/URL only",
  "tags": "comma-separated relevant tags, max 5"
}}"""


def _extract_json(text: str) -> dict:
    """Extract JSON from a response that may contain markdown fencing or preamble."""
    text = text.strip()

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try stripping markdown code fences
    fenced = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Try finding the first { ... } block
    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except json.JSONDecodeError:
            pass

    raise json.JSONDecodeError("No JSON found in response", text, 0)


def categorise_article(
    title: str, content: str, url: str, source: str, author: str
) -> dict:
    """Call Claude Haiku to categorise an article. Returns structured metadata."""
    # Ensure content is a string
    content = content or ""

    # Choose prompt based on content availability
    has_content = len(content.strip()) > 50
    if has_content:
        prompt = CATEGORISE_PROMPT.format(
            title=title or "Unknown",
            source=source or "Unknown",
            url=url or "",
            content=content[:3000],
        )
    else:
        logger.warning("No article content available — using minimal categorisation prompt for: %s", url)
        prompt = CATEGORISE_MINIMAL_PROMPT.format(
            title=title or "Unknown",
            source=source or "Unknown",
            url=url or "",
        )

    try:
        logger.info("Calling Claude API for categorisation: title=%r, url=%r, content_len=%d", title, url, len(content))
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_text = response.content[0].text
        logger.info("Claude API response received (%d chars)", len(raw_text))
        logger.debug("Raw response: %s", raw_text[:500])

        result = _extract_json(raw_text)
        logger.info("Categorised as topic=%r, subtopic=%r", result.get("topic"), result.get("subtopic"))

        # Preserve the fetched author if Claude returns Unknown but we have one
        if result.get("author", "Unknown") == "Unknown" and author != "Unknown":
            result["author"] = author

        return result

    except anthropic.AuthenticationError as e:
        logger.error("Anthropic API authentication failed — check ANTHROPIC_API_KEY env var: %s", e)
    except anthropic.RateLimitError as e:
        logger.error("Anthropic API rate limited: %s", e)
    except anthropic.APIStatusError as e:
        logger.error("Anthropic API error (status %s): %s", e.status_code, e.message)
    except json.JSONDecodeError as e:
        logger.error("Failed to parse JSON from Claude response: %s (response was: %s)", e, raw_text[:300] if 'raw_text' in dir() else "N/A")
    except Exception as e:
        logger.error("Categorisation failed: %s: %s", type(e).__name__, e, exc_info=True)

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
