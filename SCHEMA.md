# Notion enrichment configuration

Set up a virtual environment, install dependencies, then run `notion_enrich.py` (see `.env.example`).

```bash
cd notion-reading-enricher
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env, then:
python notion_enrich.py --dry-run --limit 3
```

This tool reads your database schema from Notion and fills **only empty** properties (unless `--overwrite`). You control behavior with environment variables.

## Required

| Variable | Description |
|----------|-------------|
| `NOTION_TOKEN` | Integration secret from Notion (internal integration). |
| `NOTION_DATABASE_ID` | Database ID from the Notion URL (32 hex chars, with or without dashes). |

## URL column

| Variable | Default | Description |
|----------|---------|-------------|
| `NOTION_URL_PROPERTY` | `Link` | Name of the **URL** type property holding the article link. If your column is named differently (e.g. `URL`), set this. |

## Columns the LLM may fill

By default, every **empty** fillable property is eligible except:

- The URL property itself (always skipped for LLM fill).
- Properties listed in `NOTION_SKIP_PROPERTIES` (comma-separated Notion property names).

Fillable types (when empty): `title`, `rich_text`, `number`, `select`, `multi_select`, `status`, `date`, `checkbox`, `url`, `email`, `phone_number`.

Skipped types (cannot or should not auto-fill): `formula`, `rollup`, `relation`, `people`, `files`, `created_time`, `created_by`, `last_edited_time`, `last_edited_by`, `unique_id`, `verification`.

## Optional hints for the model

| Variable | Description |
|----------|-------------|
| `NOTION_PROPERTY_HINTS` | Semicolon-separated `PropertyName=hint` pairs. Example: `Summary=One paragraph overview;Topics=comma-separated keywords` |

## Advanced filtering

| Variable | Description |
|----------|-------------|
| `NOTION_QUERY_FILTER` | JSON object passed to Notion’s database query `filter`. Combined with `--since` using an `and` wrapper when both are set. Example: `{"property":"Status","select":{"equals":"Pending"}}`. |

## Optional Playwright fetch

For JavaScript-heavy sites where plain HTTP returns little content, install Playwright and run:

```bash
pip install playwright
playwright install chromium
python notion_enrich.py --use-playwright ...
```

Add `playwright` to your environment if it is not already installed (see `requirements-playwright.txt`).

## Safety

- Use `--dry-run` first to see proposed updates without writing.
- Defaults preserve existing values; use `--overwrite` only when you want to replace non-empty fields.

## Checkbox fields

Checkboxes are only updated when `--overwrite` is set (Notion does not expose an “unset” checkbox state).
