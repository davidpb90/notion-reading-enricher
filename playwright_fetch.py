"""Optional Playwright-based fetch for JS-heavy pages (install Playwright separately)."""

from __future__ import annotations

import logging

import trafilatura

from fetch_article import ArticleContent, DEFAULT_TIMEOUT, DEFAULT_USER_AGENT, MAX_DOWNLOAD_BYTES

LOGGER = logging.getLogger(__name__)


def fetch_article_text_playwright(
    url: str,
    *,
    max_chars: int,
    timeout_ms: float = DEFAULT_TIMEOUT * 1000,
) -> ArticleContent:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=DEFAULT_USER_AGENT)
        page = context.new_page()
        page.goto(url, wait_until="networkidle", timeout=int(timeout_ms))
        html = page.content()
        browser.close()

    if len(html) > MAX_DOWNLOAD_BYTES:
        html = html[:MAX_DOWNLOAD_BYTES]
        LOGGER.warning("Playwright HTML truncated for %s", url)

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
    title = (metadata.title or "").strip() if metadata else ""
    language = metadata.language if metadata else None

    truncated = False
    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True
        LOGGER.info("Playwright article text truncated to %s characters for %s", max_chars, url)

    return ArticleContent(
        url=url,
        title=title,
        text=text,
        language=language,
        truncated=truncated,
    )
