# notion-reading-enricher

CLI tool that reads a Notion database of saved links, downloads each page, extracts article text, and uses an OpenAI-compatible LLM (e.g. Ollama, Groq) to fill empty Notion properties.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` with your Notion integration token, database ID, and LLM settings. Then:

```bash
python notion_enrich.py --dry-run --limit 3 -v
```

See [SCHEMA.md](SCHEMA.md) for all environment variables and optional Playwright support.

## Requirements

- Python 3.10+
- A [Notion integration](https://developers.notion.com/docs/create-a-notion-integration) with access to your database
- An LLM endpoint (local Ollama or any OpenAI-compatible API)

## License

MIT
