"""OpenAI-compatible LLM calls for structured field extraction."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from openai import OpenAI
from pydantic import BaseModel, Field

from fetch_article import ArticleContent

LOGGER = logging.getLogger(__name__)


class ExtractionEnvelope(BaseModel):
    """Validated outer shape from the model."""

    fields: dict[str, Any] = Field(default_factory=dict)


def _build_user_message(
    *,
    page_title: str,
    article: ArticleContent,
    targets: dict[str, dict[str, Any]],
    property_hints: dict[str, str],
) -> str:
    lines = [
        "Extract metadata for this saved reading item. Use only the article text and URL.",
        f"Notion page title (may be placeholder): {page_title or '(empty)'}",
        f"URL: {article.url}",
    ]
    if article.title:
        lines.append(f"Detected page title from HTML: {article.title}")
    if article.language:
        lines.append(f"Detected language (if known): {article.language}")
    lines.append("")
    lines.append("Fill these Notion properties. Omit a key if you cannot infer a value.")
    lines.append("")
    for name, spec in targets.items():
        ptype = spec.get("type")
        hint = property_hints.get(name, "")
        option_help = ""
        if ptype == "select":
            opts = _select_names(spec)
            option_help = f" Must be exactly one of: {opts}."
        elif ptype == "status":
            opts = _status_names(spec)
            option_help = f" Must be exactly one of: {opts}."
        elif ptype == "multi_select":
            opts = _multi_names(spec)
            option_help = f" Use only these option names (comma-separated in JSON string or array): {opts}."
        lines.append(f"- {name} (type {ptype}){option_help}")
        if hint:
            lines.append(f"  Hint: {hint}")
    lines.append("")
    lines.append("Article text:")
    lines.append(article.text or "(no extractable text)")
    return "\n".join(lines)


def _select_names(spec: dict[str, Any]) -> list[str]:
    sel = spec.get("select") or {}
    return [o["name"] for o in sel.get("options") or [] if o.get("name")]


def _status_names(spec: dict[str, Any]) -> list[str]:
    st = spec.get("status") or {}
    return [o["name"] for o in st.get("options") or [] if o.get("name")]


def _multi_names(spec: dict[str, Any]) -> list[str]:
    ms = spec.get("multi_select") or {}
    return [o["name"] for o in ms.get("options") or [] if o.get("name")]


def _system_prompt() -> str:
    return (
        "You help organize a personal reading list in Notion. "
        "Return a single JSON object with a top-level key 'fields' whose value is an object "
        "mapping Notion property names to values. "
        "Use ISO date format (YYYY-MM-DD) for date fields. "
        "For rich text and title fields use plain strings. "
        "Do not invent authors or dates that are not supported by the text; leave those keys out. "
        "Respond with JSON only, no markdown fences."
    )


_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def _parse_json_object(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    fence = _JSON_FENCE.search(raw)
    if fence:
        raw = fence.group(1).strip()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("LLM output is not a JSON object")
    return data


def extract_fields(
    client: OpenAI,
    model: str,
    *,
    temperature: float,
    page_title: str,
    article: ArticleContent,
    targets: dict[str, dict[str, Any]],
    property_hints: dict[str, str],
    max_retries: int = 2,
) -> dict[str, Any]:
    if not targets:
        return {}
    user_msg = _build_user_message(
        page_title=page_title,
        article=article,
        targets=targets,
        property_hints=property_hints,
    )
    last_error: Exception | None = None
    last_content = ""
    for attempt in range(max_retries):
        try:
            completion = client.chat.completions.create(
                model=model,
                temperature=temperature,
                messages=[
                    {"role": "system", "content": _system_prompt()},
                    {"role": "user", "content": user_msg},
                ],
            )
            choice = completion.choices[0]
            last_content = (choice.message.content or "").strip()
            data = _parse_json_object(last_content)
            allowed = set(targets.keys())
            inner: dict[str, Any]
            if isinstance(data.get("fields"), dict):
                inner = data["fields"]
            else:
                inner = {k: v for k, v in data.items() if k in allowed}
            envelope = ExtractionEnvelope.model_validate({"fields": inner})
            filtered = {k: v for k, v in envelope.fields.items() if k in allowed}
            return filtered
        except Exception as exc:
            last_error = exc
            LOGGER.warning("LLM attempt %s failed: %s", attempt + 1, exc)
    if last_content:
        try:
            repaired = repair_json_once(
                client,
                model,
                temperature=temperature,
                broken=last_content,
            )
            allowed = set(targets.keys())
            return {k: v for k, v in repaired.items() if k in allowed}
        except Exception as repair_exc:
            LOGGER.warning("JSON repair failed: %s", repair_exc)
    if last_error:
        raise last_error
    return {}


def repair_json_once(
    client: OpenAI,
    model: str,
    *,
    temperature: float,
    broken: str,
) -> dict[str, Any]:
    completion = client.chat.completions.create(
        model=model,
        temperature=min(temperature, 0.0),
        messages=[
            {
                "role": "system",
                "content": "Output valid JSON only: a single object with key 'fields' mapping strings to values.",
            },
            {"role": "user", "content": f"Fix this into valid JSON only:\n\n{broken}"},
        ],
    )
    content = (completion.choices[0].message.content or "").strip()
    data = _parse_json_object(content)
    if "fields" in data and isinstance(data["fields"], dict):
        return data["fields"]
    if isinstance(data, dict):
        return data
    return {}
