#!/usr/bin/env python3
"""CLI: enrich Notion reading-list rows from fetched article text + LLM."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from typing import Any, Callable

from dotenv import load_dotenv
from openai import OpenAI

from fetch_article import fetch_article_text
from llm_extract import extract_fields
from notion_ops import (
    build_properties_payload,
    fillable_targets,
    notion_client,
    page_title_plain,
    page_url,
    patch_page,
    property_schema,
    query_all_pages,
    retrieve_database,
)

LOGGER = logging.getLogger(__name__)


def _parse_hints(raw: str) -> dict[str, str]:
    if not raw.strip():
        return {}
    out: dict[str, str] = {}
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if "=" in chunk:
            key, _, val = chunk.partition("=")
            out[key.strip()] = val.strip()
    return out


def _timestamp_since_filter(since: str | None) -> dict[str, Any] | None:
    if not since:
        return None
    return {
        "timestamp": "last_edited_time",
        "last_edited_time": {"on_or_after": since.strip()},
    }


def _merge_filters(*parts: dict[str, Any] | None) -> dict[str, Any] | None:
    clauses = [p for p in parts if p]
    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"and": clauses}


def _parse_env_json(name: str) -> dict[str, Any] | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{name} must be valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"{name} must be a JSON object")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fill empty Notion database properties using article text + LLM.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not PATCH Notion.")
    parser.add_argument("--limit", type=int, default=0, help="Max pages to process (0=all).")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite non-empty fields.")
    parser.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        help="Only pages last edited on or after this date (ISO).",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument(
        "--failure-log",
        metavar="PATH",
        help="Append CSV rows for failures (page_id,url,error).",
    )
    parser.add_argument(
        "--use-playwright",
        action="store_true",
        help="Render page with Playwright (install: pip install playwright && playwright install chromium).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    load_dotenv()

    token = os.environ.get("NOTION_TOKEN")
    database_id = os.environ.get("NOTION_DATABASE_ID")
    url_prop = os.environ.get("NOTION_URL_PROPERTY", "Link").strip()
    skip_raw = os.environ.get("NOTION_SKIP_PROPERTIES", "")
    skip_props = {s.strip() for s in skip_raw.split(",") if s.strip()}
    hints = _parse_hints(os.environ.get("NOTION_PROPERTY_HINTS", ""))

    sleep_s = float(os.environ.get("NOTION_SLEEP_SECONDS", "0.35"))
    max_chars = int(os.environ.get("LLM_MAX_ARTICLE_CHARS", "12000"))
    llm_model = os.environ.get("LLM_MODEL", "llama3.2").strip()
    llm_base = os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1").strip()
    llm_key = os.environ.get("LLM_API_KEY", "").strip() or "ollama"
    llm_temp = float(os.environ.get("LLM_TEMPERATURE", "0.2"))

    if not token or not database_id:
        LOGGER.error("Set NOTION_TOKEN and NOTION_DATABASE_ID (see .env.example).")
        return 1

    extra_filter = _parse_env_json("NOTION_QUERY_FILTER")
    filter_body = _merge_filters(_timestamp_since_filter(args.since), extra_filter)

    client_notion = notion_client(token)
    database = retrieve_database(client_notion, database_id)
    schema_full = property_schema(database)

    if url_prop not in schema_full:
        LOGGER.error(
            "URL property %r not found on database. Columns: %s",
            url_prop,
            ", ".join(sorted(schema_full.keys())),
        )
        return 1
    if schema_full[url_prop].get("type") != "url":
        LOGGER.error("Property %r must be of type URL in Notion.", url_prop)
        return 1

    llm = OpenAI(base_url=llm_base, api_key=llm_key)

    failures_f = None
    failures_writer = None
    if args.failure_log:
        new_file = not os.path.exists(args.failure_log)
        failures_f = open(args.failure_log, "a", encoding="utf-8", newline="")
        failures_writer = csv.writer(failures_f)
        if new_file:
            failures_writer.writerow(["page_id", "url", "error"])

    processed = 0
    skipped = 0
    updated = 0
    fetch_fn: Callable[..., Any] = fetch_article_text
    if args.use_playwright:
        try:
            from playwright_fetch import fetch_article_text_playwright

            def _pw(url: str, *, max_chars: int):
                return fetch_article_text_playwright(url, max_chars=max_chars)

            fetch_fn = _pw
        except ImportError as exc:
            LOGGER.error(
                "Playwright optional dependency missing: %s. "
                "Install with: pip install playwright && playwright install chromium",
                exc,
            )
            return 1

    pages = query_all_pages(
        client_notion,
        database_id,
        sleep_seconds=min(sleep_s, 0.2),
        filter_body=filter_body,
    )

    examined = 0
    for page in pages:
        if args.limit and examined >= args.limit:
            break
        examined += 1
        page_id = page.get("id") or ""
        props = page.get("properties") or {}

        targets = fillable_targets(
            page,
            schema_full,
            url_property=url_prop,
            skip_properties=skip_props,
            overwrite=args.overwrite,
        )
        url = page_url(props, url_prop)
        if not url:
            LOGGER.info("Skip page %s: no URL in %r", page_id, url_prop)
            skipped += 1
            continue
        if not targets:
            LOGGER.debug("Skip page %s: nothing to fill", page_id)
            skipped += 1
            continue

        title_guess = page_title_plain(page)

        try:
            article = fetch_fn(url, max_chars=max_chars)
        except Exception as exc:
            LOGGER.warning("Fetch failed for %s: %s", url, exc)
            if failures_writer:
                failures_writer.writerow([page_id, url, f"fetch:{exc}"])
                failures_f.flush()
            skipped += 1
            continue

        try:
            extracted = extract_fields(
                llm,
                llm_model,
                temperature=llm_temp,
                page_title=title_guess,
                article=article,
                targets=targets,
                property_hints=hints,
            )
        except Exception as exc:
            LOGGER.warning("LLM failed for %s: %s", url, exc)
            if failures_writer:
                failures_writer.writerow([page_id, url, f"llm:{exc}"])
                failures_f.flush()
            skipped += 1
            continue

        payload = build_properties_payload(schema_full, extracted)
        if not payload:
            LOGGER.info("No mapped fields for page %s (%s)", page_id, url)
            skipped += 1
            processed += 1
            continue

        if args.dry_run:
            LOGGER.info(
                "[dry-run] Would update %s %s -> %s",
                page_id,
                url,
                json.dumps(payload, ensure_ascii=False)[:500],
            )
            updated += 1
            processed += 1
            continue

        try:
            patch_page(client_notion, page_id, payload, sleep_seconds=sleep_s)
            LOGGER.info("Updated page %s (%s)", page_id, url)
            updated += 1
        except Exception as exc:
            LOGGER.warning("PATCH failed for %s: %s", page_id, exc)
            if failures_writer:
                failures_writer.writerow([page_id, url, f"notion:{exc}"])
                failures_f.flush()
            skipped += 1
        processed += 1

    if failures_f:
        failures_f.close()

    LOGGER.info(
        "Done. processed=%s updates_applied_or_dry=%s skipped_or_fetch_fail=%s",
        processed,
        updated,
        skipped,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
