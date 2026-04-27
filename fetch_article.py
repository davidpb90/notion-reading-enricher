"""Fetch a URL and extract readable article text."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
import trafilatura

LOGGER = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; NotionReadingEnricher/1.0; +https://example.local) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 25.0
MAX_DOWNLOAD_BYTES = 5_000_000


@dataclass
class ArticleContent:
    url: str
    title: str
    text: str
    language: str | None
    truncated: bool


def fetch_article_text(
    url: str,
    *,
    max_chars: int,
    timeout: float = DEFAULT_TIMEOUT,
    user_agent: str = DEFAULT_USER_AGENT,
) -> ArticleContent:
    headers = {"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml"}
    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers=headers,
    ) as client:
        response = client.get(url)
        response.raise_for_status()
        raw = response.content
        if len(raw) > MAX_DOWNLOAD_BYTES:
            LOGGER.warning(
                "Response truncated from %s bytes to %s for URL %s",
                len(raw),
                MAX_DOWNLOAD_BYTES,
                url,
            )
            raw = raw[:MAX_DOWNLOAD_BYTES]

    html = raw.decode("utf-8", errors="replace")
    extracted = trafilatura.extract(
        html,
        url=url,
        include_comments=False,
        include_tables=True,
        no_fallback=False,
        output_format="txt",
    )
    text = (extracted or "").strip()
    metadata = trafilatura.extract_metadata(html, default_url=url)
    title = ""
    language = None
    if metadata:
        title = (metadata.title or "").strip()
        language = metadata.language

    truncated = False
    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True
        LOGGER.info("Article text truncated to %s characters for %s", max_chars, url)

    return ArticleContent(
        url=url,
        title=title,
        text=text,
        language=language,
        truncated=truncated,
    )
