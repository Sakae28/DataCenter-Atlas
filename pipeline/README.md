# News Pipeline

Daily aggregation of APAC data center industry news: fetch the curated RSS
sources in `sources.yaml` (P1 core media, P2 regional, P0 official
newsrooms), score/dedup/summarize with an LLM (optional), and write
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
| `FETCH_FULLTEXT` | `1`                      | `0` skips full-text extraction            |

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
keeps items from the last 48 hours, processes them, extracts full article
text for the surviving stories (trafilatura, populating `content` as
Markdown — headings, lists, blockquotes and images preserved — plus
`content_url` per the schema; Google News redirect links are decoded
best-effort and may keep summary only), and writes
`data/news/<today UTC>.json`. Story ids derive from the primary source URL
(stable across runs). Re-running on the same day merges into the existing
file — stories sharing any source URL are merged (sources and content
combined, more complete copy wins) — and stories whose primary URL was
already reported in the previous ~2 days are skipped (the 48h fetch window
overlaps consecutive days), so it is safe to run multiple times per day.

Set `FETCH_FULLTEXT=0` to skip the full-text extraction step.
`FETCH_MAX_AGE_HOURS` overrides the default 48-hour item-age window.

Missed a few days? `backfill_days.py` recreates the missing digests:
it fetches all sources once with an extended age window, partitions items
by UTC published date and runs the full per-day pipeline for each missing
day, oldest first (existing files are never touched; run `run.py`
afterwards for today):

```bash
FETCH_MAX_AGE_HOURS=120 pipeline/.venv/Scripts/python pipeline/backfill_days.py
# or with explicit dates: ... backfill_days.py 2026-08-15 2026-08-16
```

Useful piecemeal runs:

```bash
pipeline/.venv/Scripts/python pipeline/fetch.py   # fetch only, prints counts
```

`backfill_markdown.py` is a one-off script that re-extracted existing
plain-text `content` as Markdown for older day files; kept for reference.

`heal_data.py` is the maintenance script for existing day files: it merges
duplicate stories sharing any source URL (within and across day files; the
story belongs to the earliest day) and re-extracts every story's content
through the current `fulltext.py`. Run it with the same proxy/TLS env vars
as the pipeline; `--dedup-only` / `--content-only` limit it to one phase.

`polish_content.py` re-applies content polishing (strip a leading duplicate
of the story title, promote standalone bold lead-in lines to `###` headings)
to stored `content` — pure string processing, no refetching.

`backfill_companies.py` fills the optional `companies` field on existing day
files with one batched LLM call per file (id + title + summary in, canonical
company names out). Run with `LLM_BACKEND=kimi-cli` or an API key.

## Project tracking (data/projects.json)

`projects.py` maintains the project pipeline database described in
`../SPEC.md`. After story assembly/merge and full-text extraction (story
ids final), `run.py` makes one batched LLM call over the day's stories to
extract concrete project events (new builds, expansions, campus-tied
financing, commissioning, land/power acquisition — market commentary and
regulation stories are skipped), then upserts `data/projects.json`:

- Matching: same operator (case-insensitive) AND (fuzzy name match OR same
  city+country); a single LLM arbitration call resolves ambiguous cases.
- Updates never regress status (operational → announced is blocked;
  `on_hold`/`cancelled` always accepted) and never shrink capacity (a
  phase figure won't overwrite a larger total-planned one). Real status
  changes are appended to `status_history` with the digest date and story
  id. Story ids are appended deduplicated, so re-running the same day is
  idempotent.
- The extraction also pulls the extended profile fields when a story
  actually states them (`developer`, `investor`, `investment`, `phases`,
  `anchor_tenants`, `power`, `announced`, `construction_start`, `rfs`).
  Merge rule: a field fills in when previously null and is replaced when
  the new value is non-null and different (more specific info wins);
  anchor tenants accumulate (max 4).
- `last_updated` is the date of the last REAL change (new linked story,
  status change, or any field update) — never the pipeline run date, so
  no-op re-runs leave it untouched. Seed entries that were never
  confirmed by tracked news keep `last_updated: null`.
- No match → a new project entry with a slug id (`operator-city-name`,
  deduplicated), initialized with one `status_history` entry. With no
  LLM backend the step is skipped silently and the file is left
  untouched.

`migrate_projects.py` is the one-time migration that brought the existing
DB to the extended schema: `expected` → `rfs` (null for operational;
kept only when not entirely in the past otherwise), new fields added as
null/[], `last_updated` recomputed (latest linked story's date for
news-linked entries, null for seeds), `status_history` initialized. Kept
for reference; safe to delete once the schema change has settled.

`seed_projects.py` (re)builds the seed: one LLM call per region listing
major, well-known, real projects (~15-20 each for southeast-asia/china/
japan, ~10-15 for australia/korea), validated through the same
`_validate_update` checks as news extraction and merged with
`upsert_projects`. Existing entries with `story_ids` (news-linked) are
kept; unlinked seed entries are regenerated with `"seed": true`,
`last_updated: null` and an empty `status_history`. `rfs` is forced null
for operational projects and may only carry a future date otherwise.
Extended profile fields are seeded only when well-known public fact. The
script prints the final DB grouped by region — review it before
committing and drop anything you can't verify as real:

```bash
LLM_BACKEND=kimi-cli pipeline/.venv/Scripts/python pipeline/seed_projects.py
```

## Bulk directory import (import_external.py)

`import_external.py` bulk-imports existing facility inventories from public
directories into `data/projects.json`, so the DB is not limited to projects
that tracked news happens to mention. Two sources:

- **baxtel** — Baxtel publishes its whole site database as a public Mapbox
  vector tileset (token printed on `baxtel.com/map`); one z0 tile contains
  every site worldwide with operator, total power MW, stage and metro. Two
  HTTP requests.
- **datacentermap** — datacentermap.com embeds full facility listings as
  JSON (`__NEXT_DATA__`) on country/city pages; facility detail pages add
  stage/built-out MW/expected year. The site sits behind Vercel bot
  protection, so fetching uses curl_cffi Chrome impersonation with a
  polite delay; throttled responses are retried with long backoff and a
  per-country cache (`.scratch/import_cache_datacentermap.json`) lets an
  interrupted run resume — only countries scraped with zero failures are
  cached.

Imported entries get `seed: true`, a `source` tag ("baxtel"/"datacentermap")
and status `operational` unless the source says otherwise. Matching is
stricter than news upserts: a merge requires same canonical operator, a
subset name match with identical digit tokens and compatible cities —
never city+country alone (operators run many numbered facilities per
metro). Scraped updates are cached under `.scratch/`; `--refresh`
re-scrapes, `--dry-run` reports without writing, `--no-details` skips DCM
detail pages:

```bash
pipeline/.venv/Scripts/python pipeline/import_external.py --dry-run
```

## Directory sync (sync_directories.py)

`sync_directories.py` is the incremental counterpart of the DCM import: DCM
updates its listings over time, so once a week (`run.py` runs it on Sundays,
or any day with `SYNC_DIRECTORIES=1`; failures never break the digest) it
re-fetches the country/market listing pages and diffs them against the DB:

- Matching is by DCM-internal facility id, stored as `dcm_id` on the project.
  The first run links existing projects with the same strict name/operator/
  city matching as the import and backfills `dcm_id` plus a `dcm_sync`
  snapshot of the listing summary (name/operator/city/category).
- Only genuinely NEW listings and listings whose summary changed vs. the
  `dcm_sync` snapshot get their detail page re-fetched (stage, MW, year,
  specs). `--limit N` caps detail fetches per run — the rest are deferred
  to the next run (their stale snapshots re-trigger then).
- Field updates follow the pipeline policies: status never regresses,
  capacity only grows, coordinates fill only when missing. A real status
  change appends `{"status", "date", "source": "directory_sync"}` to
  `status_history` (no story_id — those mark news-driven entries) and real
  changes bump `last_updated`; unchanged projects are never touched.
  Operator changes are flagged in the summary, not applied automatically.
- New facilities are created like import entries (`seed: true`,
  `source: "datacentermap"`, `first_seen` = sync date) with an initial
  directory_sync status_history entry.
- Known limitation: listing pages carry no stage/MW, so a pure stage flip
  (pipeline → live) with an unchanged summary is only caught when something
  in the summary also changes.
- Each non-dry-run writes `data/directory-sync-YYYY-MM-DD.json`
  ({date, created, changed: [{id, name, fields: {field: [old, new]}}],
  unchanged_count, linked_count, deferred_count, flags, errors}) and prints
  the same digest to the console.

```bash
pipeline/.venv/Scripts/python pipeline/sync_directories.py --dry-run --limit 30
```
