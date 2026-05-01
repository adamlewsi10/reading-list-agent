"""
Phase 2 frontmatter generation.

After an article is fetched, call Claude Haiku to extract structured metadata,
then compute relevance_to by matching entities/concepts against active-projects.json
and aliases.json in Drive.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

import anthropic
from googleapiclient.discovery import build

from app.config import ANTHROPIC_API_KEY, GOOGLE_DRIVE_OAUTH_JSON

logger = logging.getLogger(__name__)

ACTIVE_PROJECTS_FILE_ID = "1kSLyrPLYCAlXf4aT1UsdW5qEJNTuCh1s"
KG_FOLDER_ID = "1hs9m0AJEiUbGltkMAxLqz8VosfdZTxj6"

_claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Simple in-process cache: (data, loaded_at)
_projects_cache: Optional[tuple] = None
_aliases_cache: Optional[tuple] = None
CACHE_TTL_SECONDS = 3600


def _get_drive_service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    oauth = GOOGLE_DRIVE_OAUTH_JSON
    creds = Credentials(
        token=oauth.get("token"),
        refresh_token=oauth["refresh_token"],
        token_uri=oauth.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=oauth["client_id"],
        client_secret=oauth["client_secret"],
        scopes=oauth.get("scopes", ["https://www.googleapis.com/auth/drive"]),
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("drive", "v3", credentials=creds)


def _load_json_file(service, file_id: str) -> Optional[dict]:
    try:
        content = service.files().get_media(fileId=file_id).execute()
        return json.loads(content.decode("utf-8"))
    except Exception as e:
        logger.warning("Failed to load Drive file %s: %s", file_id, e)
        return None


def _load_aliases_from_drive(service) -> dict:
    res = service.files().list(
        q=f"name='aliases.json' and '{KG_FOLDER_ID}' in parents and trashed=false",
        fields="files(id)",
        pageSize=5,
    ).execute()
    files = res.get("files", [])
    if not files:
        return {}
    data = _load_json_file(service, files[0]["id"])
    return data or {}


def _get_active_projects() -> list[dict]:
    global _projects_cache
    now = time.time()
    if _projects_cache and (now - _projects_cache[1]) < CACHE_TTL_SECONDS:
        return _projects_cache[0]

    try:
        service = _get_drive_service()
        data = _load_json_file(service, ACTIVE_PROJECTS_FILE_ID)
        projects = data.get("projects", []) if isinstance(data, dict) else []
    except Exception as e:
        logger.warning("Failed to load active-projects.json: %s", e)
        projects = []

    _projects_cache = (projects, now)
    return projects


def _get_aliases() -> dict:
    global _aliases_cache
    now = time.time()
    if _aliases_cache and (now - _aliases_cache[1]) < CACHE_TTL_SECONDS:
        return _aliases_cache[0]

    try:
        service = _get_drive_service()
        data = _load_aliases_from_drive(service)
    except Exception as e:
        logger.warning("Failed to load aliases.json: %s", e)
        data = {}

    _aliases_cache = (data, now)
    return data


def _build_canonical_terms(projects: list[dict], aliases: dict) -> dict[str, str]:
    """
    Build a flat map of lowercased terms → project name.
    Includes: project names, project aliases, canonical people/tools names.
    """
    term_map: dict[str, str] = {}

    for p in projects:
        name = p.get("name", "")
        term_map[name.lower()] = name
        for alias in p.get("aliases", []):
            term_map[alias.lower()] = name

    # People/tools canonical values — map to whichever project they're most relevant to
    # For now, just add them to the lookup without project association
    # (relevance_to matches project names, not individual people)
    return term_map


def _compute_relevance(entities: list[str], concepts: list[str],
                       projects: list[dict]) -> list[str]:
    """
    Match entities and concepts against project names and aliases.
    Returns sorted list of matching project names.
    """
    if not projects:
        return []

    matched = set()
    terms = [t.lower() for t in (entities + concepts)]

    for project in projects:
        name = project.get("name", "")
        all_aliases = [name.lower()] + [a.lower() for a in project.get("aliases", [])]

        for alias in all_aliases:
            for term in terms:
                if alias in term or term in alias:
                    matched.add(name)
                    break

    return sorted(matched)


def _extract_json(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        try:
            return json.loads(brace.group(0))
        except json.JSONDecodeError:
            pass
    raise ValueError(f"No JSON found in response: {text[:200]}")


HAIKU_PROMPT = """\
You are extracting structured metadata from an article to build a personal reading library.

Article title: {title}
Source: {source}
URL: {url}
Content (first 4000 chars):
{content}

Return JSON only, no preamble, no markdown fences:
{{
  "concepts": ["2-5 key topics or ideas from this article, as short noun phrases"],
  "entities_mentioned": ["people, companies, or products explicitly named"],
  "key_claims": ["2-4 short bullet claims: what does this article actually argue?"],
  "publication_date": "YYYY-MM-DD or null if not found",
  "author": "Author name if found, or null"
}}

Rules:
- concepts: what intellectual territory does this cover (e.g. "context engineering", "CRM adoption", "agentic UX")
- entities_mentioned: proper nouns only — no generic terms
- key_claims: the actual arguments made, not summaries — max 15 words each
- my_take is NOT your job — leave it out of the response entirely
"""

HAIKU_MINIMAL_PROMPT = """\
Extract metadata from this article title and URL only.

Title: {title}
Source: {source}
URL: {url}

Return JSON only, no preamble:
{{
  "concepts": ["1-3 inferred topics from the title"],
  "entities_mentioned": ["any people or companies visible in the title"],
  "key_claims": [],
  "publication_date": null,
  "author": null
}}
"""


def _call_haiku(title: str, source: str, url: str, body_text: str) -> dict:
    """Call Claude Haiku to extract article metadata."""
    has_content = len(body_text.strip()) > 100
    if has_content:
        prompt = HAIKU_PROMPT.format(
            title=title,
            source=source,
            url=url,
            content=body_text[:4000],
        )
    else:
        prompt = HAIKU_MINIMAL_PROMPT.format(title=title, source=source, url=url)
        logger.info("No body text — using minimal frontmatter prompt for: %s", url[:60])

    response = _claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text
    logger.debug("Haiku raw response: %s", raw[:400])
    return _extract_json(raw)


def generate_frontmatter(url: str, title: str, source: str, author: str,
                         body_text: str, fetch_failed: bool) -> dict:
    """
    Generate structured frontmatter for an article.
    Always returns a valid dict even if Haiku call fails.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Base frontmatter (always set)
    fm = {
        "type": "reading",
        "source_url": url,
        "source_name": source,
        "author": author,
        "publication_date": None,
        "date_captured": today,
        "concepts": [],
        "entities_mentioned": [],
        "key_claims": [],
        "my_take": "",
        "relevance_to": [],
    }

    # Skip Haiku if fetch failed (no useful content)
    if fetch_failed:
        logger.info("Fetch failed — generating minimal frontmatter for: %s", url[:60])
        return fm

    try:
        haiku_data = _call_haiku(title, source, url, body_text or "")

        fm["concepts"] = haiku_data.get("concepts") or []
        fm["entities_mentioned"] = haiku_data.get("entities_mentioned") or []
        fm["key_claims"] = haiku_data.get("key_claims") or []

        if haiku_data.get("publication_date"):
            fm["publication_date"] = haiku_data["publication_date"]

        # Author: prefer Haiku if article_fetcher returned Unknown
        if haiku_data.get("author") and author in ("Unknown", "", None):
            fm["author"] = haiku_data["author"]

        # Compute relevance_to
        projects = _get_active_projects()
        fm["relevance_to"] = _compute_relevance(
            fm["entities_mentioned"], fm["concepts"], projects
        )

        logger.info(
            "Frontmatter generated: %d concepts, %d entities, %d relevant projects",
            len(fm["concepts"]), len(fm["entities_mentioned"]), len(fm["relevance_to"])
        )

    except Exception as e:
        logger.error("Frontmatter generation failed for %s: %s", url[:60], e)
        # Return what we have — don't block the write

    return fm


def render_frontmatter(fm: dict) -> str:
    """Render a frontmatter dict as a YAML front-matter block."""

    def yaml_str(v) -> str:
        if v is None:
            return "null"
        # Quote strings that contain special YAML chars
        if isinstance(v, str):
            needs_quoting = any(c in v for c in ':#{}[]|>&*!,?') or v == "" or v.startswith(" ")
            return f'"{v}"' if needs_quoting else v
        return str(v)

    def yaml_list(lst: list) -> str:
        if not lst:
            return "[]"
        items = "\n".join(f'  - {yaml_str(item)}' for item in lst)
        return "\n" + items

    lines = ["---"]
    for key in ["type", "source_url", "source_name", "author", "publication_date",
                "date_captured", "fetch_status"]:
        lines.append(f"{key}: {yaml_str(fm.get(key))}")

    for key in ["concepts", "entities_mentioned", "key_claims", "relevance_to"]:
        val = fm.get(key) or []
        rendered = yaml_list(val)
        if rendered == "[]":
            lines.append(f"{key}: []")
        else:
            lines.append(f"{key}:{rendered}")

    lines.append(f'my_take: ""')
    lines.append("---")
    return "\n".join(lines)
