# News Pipeline

Daily aggregation of APAC data center industry news: fetch RSS/Google News
sources, score/dedup/summarize with an LLM (optional), and write
`data/news/YYYY-MM-DD.json` per the schema in `../SPEC.md`.

## Setup (Windows, Git Bash)

```bash
python -m venv pipeline/.venv
pipeline/.venv/Scripts/pip install -r pipeline/requirements.txt
```

(On macOS/Linux the interpreter is `pipeline/.venv/bin/python`.)

## Configuration

The pipeline runs fine with no configuration — it falls back to a keyword
heuristic (score=null, featured=false, summary=source snippet). LLM
relevance scoring, clustering and enrichment are enabled by either of two
backends; copy `.env.example` to `.env` (or set variables in the
environment / CI secrets):

| Var           | Default                     | Purpose                                   |
| ------------- | --------------------------- | ----------------------------------------- |
| `LLM_BACKEND` | `auto`                      | `api` \| `kimi-cli` \| `auto` (see below)  |
| `LLM_API_KEY` | _(unset)_                   | OpenAI-compatible API key (`api` backend) |
| `LLM_BASE_URL`| `https://api.openai.com/v1` | Chat completions endpoint (`api` only)    |
| `LLM_MODEL`   | `gpt-4o-mini`               | Model for all LLM steps (`api` only)      |

Backend selection:

- `api` — OpenAI-compatible HTTP API; requires `LLM_API_KEY`.
- `kimi-cli` — calls the locally installed, already-logged-in
  [`kimi` CLI](https://www.kimi.com/) via `kimi -p <prompt>` subprocess.
  No API key needed; uses your CLI subscription. Each call spawns a fresh
  session (~15-30s), so prompts are batched aggressively (one relevance
  call per ≤20 items, one clustering call, one enrichment call).
- `auto` (default) — `api` if `LLM_API_KEY` is set, else `kimi-cli` if the
  `kimi` executable is on PATH, else heuristic mode.

## Run

```bash
pipeline/.venv/Scripts/python pipeline/run.py
```

Fetches every source in `sources.yaml` (failures are logged and skipped),
keeps items from the last 48 hours, processes them, and writes
`data/news/<today UTC>.json`. Re-running on the same day merges into the
existing file (existing stories win on id conflict; sources/heat are
updated), so it is safe to run multiple times per day.

Useful piecemeal runs:

```bash
pipeline/.venv/Scripts/python pipeline/fetch.py   # fetch only, prints counts
```
