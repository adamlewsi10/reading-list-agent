import re
import logging
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Domains to skip when extracting URLs
SKIP_DOMAINS = {
    "unsubscribe", "manage", "tracking", "click", "list-manage",
    "mailchimp", "sendgrid", "campaign-archive",
}

# Patterns for links to skip
SKIP_PATTERNS = [
    r"unsubscribe",
    r"manage.preferences",
    r"tracking",
    r"view.in.browser",
    r"pixel",
    r"beacon",
    r"open\.gif",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


@dataclass
class ArticleContent:
    url: str
    title: str = "Unknown"
    author: str = "Unknown"
    source: str = "Unknown"
    body_text: str = ""
    extra_urls: list[str] = field(default_factory=list)
    is_pdf: bool = False
    fetch_failed: bool = False


def extract_urls(text: str) -> list[str]:
    """Extract URLs from email text, filtering out tracking/unsubscribe links."""
    url_pattern = r'https?://[^\s<>\"\')}\]]+'
    raw_urls = re.findall(url_pattern, text)

    filtered = []
    for url in raw_urls:
        # Strip trailing punctuation
        url = url.rstrip(".,;:!?")
        parsed = urlparse(url)
        domain = parsed.netloc.lower()

        # Skip tracking/unsubscribe domains
        if any(skip in domain for skip in SKIP_DOMAINS):
            continue

        # Skip tracking patterns in path
        path = parsed.path.lower() + parsed.query.lower()
        if any(re.search(p, path) for p in SKIP_PATTERNS):
            continue

        if url not in filtered:
            filtered.append(url)

    return filtered


def fetch_article(url: str) -> ArticleContent:
    """Fetch and extract article content from a URL."""
    article = ArticleContent(url=url)
    parsed = urlparse(url)
    article.source = parsed.netloc.replace("www.", "")

    # Handle PDFs
    if url.lower().endswith(".pdf"):
        article.is_pdf = True
        article.title = url.split("/")[-1]
        article.body_text = "PDF document — content not extracted"
        return article

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning(f"Failed to fetch {url}: {e}")
        article.fetch_failed = True
        article.body_text = "Content unavailable — fetch failed"
        return article

    soup = BeautifulSoup(resp.text, "html.parser")

    # Extract title
    if soup.title and soup.title.string:
        article.title = soup.title.string.strip()
    elif soup.find("h1"):
        article.title = soup.find("h1").get_text(strip=True)

    # Extract author from meta tags
    author_meta = (
        soup.find("meta", attrs={"name": "author"})
        or soup.find("meta", attrs={"property": "article:author"})
        or soup.find("meta", attrs={"name": "twitter:creator"})
    )
    if author_meta and author_meta.get("content"):
        article.author = author_meta["content"].strip()
    else:
        # Try byline class patterns
        byline = soup.find(class_=re.compile(r"byline|author", re.I))
        if byline:
            article.author = byline.get_text(strip=True)

    # Extract main body text
    # Prefer <article> tag, fall back to largest text block
    article_tag = soup.find("article")
    if article_tag:
        article.body_text = article_tag.get_text(separator="\n", strip=True)
    else:
        # Remove script/style/nav elements
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()
        article.body_text = soup.get_text(separator="\n", strip=True)

    # Truncate very long content
    if len(article.body_text) > 10000:
        article.body_text = article.body_text[:10000]

    return article


def process_email(subject: str, text_body: str, html_body: str | None = None) -> ArticleContent:
    """Process an email to extract article content.

    Returns the primary article content. If no URLs found, treats the
    email body itself as the content (for forwarded newsletters).
    """
    # Try to extract URLs from text body first, then HTML
    body = text_body or ""
    urls = extract_urls(body)

    if not urls and html_body:
        urls = extract_urls(html_body)

    if not urls:
        # No URLs — treat email body as the content (forwarded newsletter)
        return ArticleContent(
            url="",
            title=subject or "Forwarded content",
            body_text=body[:10000] if body else "No content extracted",
            source="Email forward",
        )

    # Fetch the first/primary URL
    primary = fetch_article(urls[0])

    # If title extraction failed, use email subject
    if primary.title == "Unknown" and subject:
        primary.title = subject

    # Note additional URLs
    if len(urls) > 1:
        primary.extra_urls = urls[1:]

    return primary
