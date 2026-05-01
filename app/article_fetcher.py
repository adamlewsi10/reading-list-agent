import re
import logging
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# All patterns are checked case-insensitively against the full URL
BLOCKED_URL_PATTERNS = [
    "/e3t/",
    "/ctc/",
    "hs-sales-engage.com",
    "track.hubspot.com",
    "email-tracking.",
    "hsenc=",
    "hs_email=",
    "unsubscribe",
    "mailto:",
    "tel:",
    "list-manage.com",
    "campaign-archive",
    "/hs/cta/",
    "t.sidekickopen",
    "go.pardot.com",
    "sendgrid.net/wf/",
    "click.convertkit-mail",
]

SKIP_DOMAINS = {
    "unsubscribe", "manage", "tracking", "click", "list-manage",
    "mailchimp", "sendgrid", "campaign-archive",
    "hs-sales-engage", "hubspot-links", "track.hubspot",
    "go.pardot", "click.convertkit-mail", "email.mg",
    "email-tracking", "t.sidekickopen",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    # Omit br: requests doesn't decode brotli without the optional brotli package
    "Accept-Encoding": "gzip, deflate",
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


def is_valid_article_url(url: str) -> bool:
    """Check if a URL looks like a real article (not a tracking/junk link)."""
    url_lower = url.lower()

    # Block known tracking URL patterns (all patterns are already lowercase)
    if any(pattern in url_lower for pattern in BLOCKED_URL_PATTERNS):
        logger.debug("Blocked by pattern: %s", url[:80])
        return False

    parsed = urlparse(url)
    domain = parsed.netloc.lower()

    if any(skip in domain for skip in SKIP_DOMAINS):
        return False

    path_query = (parsed.path + parsed.query).lower()
    skip_path_patterns = [
        r"unsubscribe", r"manage.preferences", r"view.in.browser",
        r"pixel", r"beacon", r"open\.gif",
    ]
    if any(re.search(p, path_query) for p in skip_path_patterns):
        return False

    return True


def resolve_tracking_url(url: str) -> str | None:
    """Follow a redirect URL to find its final destination."""
    try:
        resp = requests.head(url, headers=HEADERS, timeout=10, allow_redirects=True)
        final = resp.url
        if final != url and is_valid_article_url(final):
            logger.info("Resolved tracking URL → %s", final[:80])
            return final
    except requests.RequestException as e:
        logger.debug("Could not resolve tracking URL %s: %s", url[:60], e)
    return None


def extract_urls(text: str) -> list[str]:
    """Extract article URLs from email text, filtering tracking/junk links."""
    raw = re.findall(r"https?://[^\s<>\"')}\]]+", text)

    valid, tracking = [], []
    for url in raw:
        url = url.rstrip(".,;:!?")
        if is_valid_article_url(url):
            if url not in valid:
                valid.append(url)
        else:
            tracking.append(url)

    if not valid and tracking:
        logger.info("All URLs were tracking — attempting to resolve first: %s", tracking[0][:80])
        resolved = resolve_tracking_url(tracking[0])
        if resolved:
            valid.append(resolved)

    return valid


def _fetch_with_retry(url: str, retries: int = 2) -> requests.Response | None:
    """GET with simple retry on transient errors."""
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
            if resp.status_code < 500:
                return resp
            logger.warning("HTTP %d on attempt %d: %s", resp.status_code, attempt + 1, url[:60])
        except requests.RequestException as e:
            logger.warning("Fetch error attempt %d: %s — %s", attempt + 1, url[:60], e)
        if attempt < retries:
            time.sleep(1.5)
    return None


def fetch_article(url: str) -> ArticleContent:
    """Fetch and parse article content from a URL."""
    article = ArticleContent(url=url)
    parsed = urlparse(url)
    article.source = parsed.netloc.replace("www.", "")

    # Detect PDF by extension or URL path
    if url.lower().endswith(".pdf") or "/pdf/" in url.lower():
        article.is_pdf = True
        article.title = url.split("/")[-1].replace(".pdf", "")
        article.body_text = "PDF document — content not extracted"
        return article

    resp = _fetch_with_retry(url)
    if resp is None or not resp.ok:
        logger.warning("Fetch failed for: %s", url[:80])
        article.fetch_failed = True
        article.body_text = "Content unavailable — fetch failed"
        article.title = parsed.netloc.replace("www.", "") + " — fetch failed"
        return article

    # Check content-type for PDF
    ct = resp.headers.get("Content-Type", "")
    if "application/pdf" in ct:
        article.is_pdf = True
        article.body_text = "PDF document — content not extracted"
        return article

    soup = BeautifulSoup(resp.text, "html.parser")

    # Title: prefer og:title (cleaner, no site suffix) then <title> then <h1>
    og_title = soup.find("meta", attrs={"property": "og:title"})
    h1 = soup.find("h1")

    if og_title and og_title.get("content", "").strip():
        article.title = og_title["content"].strip()
    elif soup.title and soup.title.string and soup.title.string.strip():
        article.title = soup.title.string.strip()
    elif h1:
        article.title = h1.get_text(strip=True)

    # Author
    for attr in [
        {"name": "author"},
        {"property": "article:author"},
        {"name": "twitter:creator"},
    ]:
        meta = soup.find("meta", attrs=attr)
        if meta and meta.get("content", "").strip():
            article.author = meta["content"].strip()
            break
    if article.author == "Unknown":
        byline = soup.find(class_=re.compile(r"byline|author", re.I))
        if byline:
            article.author = byline.get_text(strip=True)[:100]

    # Body text: prefer <article> tag
    article_tag = soup.find("article")
    if article_tag:
        article.body_text = article_tag.get_text(separator="\n", strip=True)
    else:
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()
        article.body_text = soup.get_text(separator="\n", strip=True)

    if len(article.body_text) > 12000:
        article.body_text = article.body_text[:12000]

    return article


def process_email(subject: str, text_body: str, html_body: str | None = None) -> ArticleContent:
    """Extract and fetch the primary article from an email."""
    body = text_body or ""
    urls = extract_urls(body)

    if not urls and html_body:
        urls = extract_urls(html_body)

    if not urls:
        return ArticleContent(
            url="",
            title=subject or "Forwarded content",
            body_text=body[:12000] if body else "No content extracted",
            source="Email forward",
            fetch_failed=True,
        )

    primary = fetch_article(urls[0])

    if primary.title == "Unknown" and subject:
        primary.title = subject

    if len(urls) > 1:
        primary.extra_urls = urls[1:]

    return primary
