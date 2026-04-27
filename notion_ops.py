"""Notion database query, page property parsing, and PATCH helpers."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Iterable

from notion_client import Client

LOGGER = logging.getLogger(__name__)

NOTION_TEXT_SEGMENT = 2000

NON_FILLABLE_TYPES = frozenset(
    {
        "formula",
        "rollup",
        "relation",
        "people",
        "files",
        "created_time",
        "created_by",
        "last_edited_time",
        "last_edited_by",
        "unique_id",
        "verification",
        "button",
    }
)


def notion_client(auth_token: str) -> Client:
    return Client(auth_token=auth_token)


def retrieve_database(client: Client, database_id: str) -> dict[str, Any]:
    return client.databases.retrieve(database_id=database_id)


def query_all_pages(
    client: Client,
    database_id: str,
    *,
    sleep_seconds: float,
    filter_body: dict[str, Any] | None = None,
    on_page_batch: Callable[[list[dict[str, Any]]], None] | None = None,
) -> list[dict[str, Any]]:
    """Paginate database query until no more results."""
    pages: list[dict[str, Any]] = []
    start_cursor: str | None = None
    while True:
        kwargs: dict[str, Any] = {"database_id": database_id, "page_size": 100}
        if start_cursor:
            kwargs["start_cursor"] = start_cursor
        if filter_body is not None:
            kwargs["filter"] = filter_body
        response = client.databases.query(**kwargs)
        batch = response.get("results") or []
        pages.extend(batch)
        if on_page_batch:
            on_page_batch(batch)
        if not response.get("has_more"):
            break
        start_cursor = response.get("next_cursor")
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    return pages


def _rich_text_plain(prop: dict[str, Any]) -> str:
    parts = prop.get("rich_text") or []
    return "".join((p.get("plain_text") or "") for p in parts)


def _title_plain(prop: dict[str, Any]) -> str:
    parts = prop.get("title") or []
    return "".join((p.get("plain_text") or "") for p in parts)


def is_property_empty(prop_type: str, prop: dict[str, Any]) -> bool:
    if prop_type == "title":
        return not _title_plain(prop).strip()
    if prop_type == "rich_text":
        return not _rich_text_plain(prop).strip()
    if prop_type == "number":
        return prop.get("number") is None
    if prop_type == "checkbox":
        # Notion always returns true/false; treat as never "empty" unless you use --overwrite.
        return False
    if prop_type == "url":
        v = prop.get("url")
        return v is None or not str(v).strip()
    if prop_type == "email":
        v = prop.get("email")
        return v is None or not str(v).strip()
    if prop_type == "phone_number":
        v = prop.get("phone_number")
        return v is None or not str(v).strip()
    if prop_type == "date":
        return prop.get("date") is None
    if prop_type == "select":
        return prop.get("select") is None
    if prop_type == "status":
        return prop.get("status") is None
    if prop_type == "multi_select":
        return not (prop.get("multi_select") or [])
    return True


def page_title_plain(page: dict[str, Any]) -> str:
    props = page.get("properties") or {}
    for _name, meta in props.items():
        if meta.get("type") == "title":
            return _title_plain(meta).strip()
    return ""


def page_url(props: dict[str, Any], url_property_name: str) -> str | None:
    raw = props.get(url_property_name)
    if not raw or raw.get("type") != "url":
        return None
    u = raw.get("url")
    if u is None or not str(u).strip():
        return None
    return str(u).strip()


def property_schema(database: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map property name -> Notion schema object for that property."""
    out: dict[str, dict[str, Any]] = {}
    for name, spec in (database.get("properties") or {}).items():
        out[name] = spec
    return out


def fillable_targets(
    page: dict[str, Any],
    schema: dict[str, dict[str, Any]],
    *,
    url_property: str,
    skip_properties: set[str],
    overwrite: bool,
) -> dict[str, dict[str, Any]]:
    """
    Return subset of schema for properties we may fill on this page:
    empty (unless overwrite), fillable type, not URL column, not skipped.
    """
    props = page.get("properties") or {}
    targets: dict[str, dict[str, Any]] = {}
    for name, spec in schema.items():
        if name == url_property:
            continue
        if name in skip_properties:
            continue
        ptype = spec.get("type") or ""
        if ptype in NON_FILLABLE_TYPES:
            continue
        if ptype == "checkbox" and not overwrite:
            continue
        raw = props.get(name)
        if not raw:
            continue
        if not overwrite and not is_property_empty(ptype, raw):
            continue
        targets[name] = spec
    return targets


def build_properties_payload(
    schema: dict[str, dict[str, Any]],
    updates: dict[str, Any],
) -> dict[str, Any]:
    """Convert flat LLM values into Notion API property update objects."""
    payload: dict[str, Any] = {}
    for name, value in updates.items():
        if value is None:
            continue
        spec = schema.get(name)
        if not spec:
            LOGGER.warning("Unknown property in LLM output, skipping: %s", name)
            continue
        ptype = spec.get("type")
        built = _single_property_update(ptype, spec, value)
        if built is not None:
            payload[name] = built
    return payload


def _single_property_update(
    ptype: str | None,
    spec: dict[str, Any],
    value: Any,
) -> dict[str, Any] | None:
    if ptype == "title":
        text = str(value).strip()
        if not text:
            return None
        segments = _text_segments(text)
        return {"title": [{"text": {"content": seg}} for seg in segments]}
    if ptype == "rich_text":
        text = str(value).strip()
        if not text:
            return None
        segments = _text_segments(text)
        return {"rich_text": [{"text": {"content": seg}} for seg in segments]}
    if ptype == "number":
        if isinstance(value, bool):
            return None
        try:
            num = float(value)
        except (TypeError, ValueError):
            return None
        return {"number": num}
    if ptype == "checkbox":
        if isinstance(value, bool):
            return {"checkbox": value}
        if isinstance(value, str):
            low = value.strip().lower()
            if low in {"true", "yes", "1"}:
                return {"checkbox": True}
            if low in {"false", "no", "0"}:
                return {"checkbox": False}
        return None
    if ptype == "url":
        text = str(value).strip()
        if not text:
            return None
        return {"url": text}
    if ptype == "email":
        text = str(value).strip()
        if not text:
            return None
        return {"email": text}
    if ptype == "phone_number":
        text = str(value).strip()
        if not text:
            return None
        return {"phone_number": text}
    if ptype == "date":
        if isinstance(value, dict):
            start = value.get("start")
            end = value.get("end")
            if start:
                out: dict[str, Any] = {"start": str(start)[:32]}
                if end:
                    out["end"] = str(end)[:32]
                return {"date": out}
        text = str(value).strip()
        if not text:
            return None
        return {"date": {"start": text[:32]}}
    if ptype == "select":
        options = _select_options(spec)
        name = _match_option(str(value).strip(), options)
        if not name:
            return None
        return {"select": {"name": name}}
    if ptype == "status":
        options = _status_options(spec)
        name = _match_option(str(value).strip(), options)
        if not name:
            return None
        return {"status": {"name": name}}
    if ptype == "multi_select":
        options = _multi_options(spec)
        names: list[str] = []
        if isinstance(value, str):
            parts = [p.strip() for p in value.replace(";", ",").split(",")]
            candidates = [p for p in parts if p]
        elif isinstance(value, list):
            candidates = [str(v).strip() for v in value if str(v).strip()]
        else:
            return None
        for c in candidates:
            m = _match_option(c, options)
            if m and m not in names:
                names.append(m)
        if not names:
            return None
        return {"multi_select": [{"name": n} for n in names]}
    return None


def _select_options(spec: dict[str, Any]) -> list[str]:
    sel = spec.get("select") or {}
    opts = sel.get("options") or []
    return [o.get("name") for o in opts if o.get("name")]


def _status_options(spec: dict[str, Any]) -> list[str]:
    st = spec.get("status") or {}
    opts = st.get("options") or []
    return list(dict.fromkeys(o.get("name") for o in opts if o.get("name")))


def _multi_options(spec: dict[str, Any]) -> list[str]:
    ms = spec.get("multi_select") or {}
    opts = ms.get("options") or []
    return [o.get("name") for o in opts if o.get("name")]


def _match_option(raw: str, options: Iterable[str]) -> str | None:
    opts = list(options)
    if not opts:
        return raw or None
    raw_clean = raw.strip()
    if raw_clean in opts:
        return raw_clean
    low = raw_clean.lower()
    for o in opts:
        if o.lower() == low:
            return o
    return None


def _text_segments(text: str, *, max_len: int = NOTION_TEXT_SEGMENT) -> list[str]:
    return [text[i : i + max_len] for i in range(0, len(text), max_len)]


def patch_page(
    client: Client,
    page_id: str,
    properties: dict[str, Any],
    *,
    sleep_seconds: float,
) -> None:
    if not properties:
        return
    client.pages.update(page_id=page_id, properties=properties)
    if sleep_seconds > 0:
        time.sleep(sleep_seconds)
