# jobs-assistant

Minimal local job backlog ingestion assistant.

The active app imports normalized job data, dedupes it, and stores it in a local SQLite backlog.

## What it does

- Initializes a local jobs database.
- Imports jobs from a JSON file or `GET /v1/jobs` feed.
- Normalizes source jobs into the active backlog schema.
- Keeps TheirStack payload/client/sync helpers available as library code.

The old browser/applier implementation is archived and is not part of the active CLI.

## Quick start

```bash
uv run --frozen jobs-assistant init-db
uv run --frozen jobs-assistant import-feed --json-file jobs.json
```

Default database:

```text
data/jobs.sqlite3
```

## Import from an API feed

If a scraper/feed service exposes jobs at `/v1/jobs`:

```bash
uv run --frozen jobs-assistant import-feed --base-url https://your-feed.example
```

Or via environment:

```bash
export JOB_SOURCE_BASE_URL=https://your-feed.example
export JOB_SOURCE_API_KEY=your-key-if-needed
uv run --frozen jobs-assistant import-feed
```

## Commands

| Command | Description |
|---|---|
| `init-db` | Initialize the SQLite jobs/sync database |
| `import-feed` | Import normalized jobs from a JSON fixture or `GET /v1/jobs` feed |

TheirStack is currently helper/library code only; there is no active `sync-theirstack` CLI command yet.

## Help

```bash
uv run --frozen jobs-assistant --help
uv run --frozen jobs-assistant import-feed --help
```

## Development checks

```bash
uv lock --check
uv run --frozen --extra dev python -m pytest
uv run --frozen jobs-assistant --help
```

Container smoke:

```bash
docker compose build
docker compose up -d
docker compose ps
docker compose down
```

Coding-agent practices and project policy live in `AGENTS.md`. Historical snapshots live under `archive/`.
