# APAC Data Center Intelligence Platform — MVP Spec

## Goal
Daily automated aggregation of data center industry news across APAC
(China, Japan, Korea, Australia, Southeast Asia), modeled on the
information architecture of https://aihot.virxact.com/ :
multi-source ingestion → LLM scoring/dedup/summarization → static site
with a top "hot list" and a day-grouped timeline. English-first content.

## Architecture

```
pipeline/  (Python 3.11, runs daily via local Task Scheduler — see
           daily_run.bat; .github/workflows/daily.yml also exists but runs
           without the accelerator, so it only reaches direct sources)
  sources.yaml      source registry (incl. html-list archive sources with
                    page_url/pages pagination for multi-day backfills)
  fetch.py          RSS/feed fetching -> raw candidates
  process.py        LLM relevance scoring, clustering, summarization
  run.py            orchestrator: fetch -> process -> write data/
  daily_run.bat     local daily entrypoint: probes the accelerator, sets
                    FETCH_PROXY only when it answers, appends to logs/daily.log
  import_external.py  one-off bulk import of public facility directories
                      (Baxtel, DataCenterMap) into data/projects.json
  sync_directories.py weekly incremental re-scan of the DataCenterMap
                      directory: new facilities + listing-summary changes
                      trigger detail re-fetches; writes
                      data/directory-sync-YYYY-MM-DD.json
  requirements.txt

data/                pipeline output, consumed by the site build
  news/YYYY-MM-DD.json   one file per day (schema below)

site/    (Astro static site, reads data/ at build time)

.github/workflows/daily.yml   cron: fetch + build + deploy artifact
```

## Data contract: data/news/YYYY-MM-DD.json
```jsonc
{
  "date": "2026-08-13",
  "generated_at": "2026-08-13T07:00:00Z",
  "hot": ["story-id-1", "story-id-2"],   // ids of top-heat stories, max 5
  "stories": [
    {
      "id": "2026-08-13-a1b2c3",          // stable slug, date + short hash
      "title": "...",                     // English, LLM-normalized
      "summary": "...",                   // 2-3 sentence English summary
      "why_it_matters": "...",            // one short editorial paragraph
      "score": 74,                        // 0-100 AI relevance/importance
      "heat": 3,                          // number of sources reporting it
      "regions": ["southeast-asia"],      // subset of REGIONS below
      "topics": ["ai", "capacity"],       // free-form lowercase tags, max 4
      "companies": ["NVIDIA", "STT GDC"], // optional: canonical company names
                                          // mentioned in the story, max 6
      "published_at": "2026-08-13T01:57:00Z",  // earliest source timestamp
      "featured": true,                   // score >= FEATURE_THRESHOLD
      "sources": [
        { "name": "Data Center Dynamics", "url": "https://...", "type": "rss" }
        // Google News entries may also carry "publisher": the real outlet
        // name parsed from the RSS title's " - <publisher>" suffix, so the
        // site can show "The Korea Herald" instead of the feed name.
      ],
      // Optional, added by the full-text step (absent or "" when extraction
      // failed, e.g. Google News redirect links that can't be resolved):
      "content": "...",                   // article body as MARKDOWN (headings,
                                          // lists, blockquotes preserved); plain
                                          // paragraphs in older files render fine
      "content_url": "https://..."        // canonical URL the content came from
    }
  ]
}
```

REGIONS = china | japan | korea | australia | southeast-asia | global
("global" only for stories that materially affect APAC, e.g. NVIDIA supply)

Stories are sorted by published_at descending in the file.
`hot` ranks by heat desc, then score desc.

## LLM usage
OpenAI-compatible chat completions API, configured by env vars:
`LLM_API_KEY`, `LLM_BASE_URL` (default https://api.openai.com/v1),
`LLM_MODEL` (default gpt-4o-mini).
Pipeline must run WITHOUT an API key: skip LLM steps, keep raw items
with score=null, featured=false, heat computed from URL/title dedup only.
This keeps CI and first-time setup testable.

## Data contract: data/projects.json

The project pipeline database, updated by the daily run (LLM extraction
from surviving stories) plus a one-time LLM seed. One object per data
center project/campus:

```jsonc
{
  "updated_at": "2026-08-14T07:00:00Z",
  "projects": [
    {
      "id": "stt-johor-campus",            // stable slug: operator-city-name
      "name": "STT Johor Campus",
      "operator": "STT GDC",               // canonical company name
      "region": "southeast-asia",          // REGIONS enum (not "global")
      "country": "Malaysia",
      "city": "Johor",
      "capacity_mw": 500,                  // number or null when unknown
      "capacity_note": "total planned",    // optional: what the figure means
      "status": "under_construction",      // announced | under_construction |
                                           // operational | on_hold | cancelled
      // --- Extended profile (all nullable; filled from news over time) ---
      "developer": "ESR",                  // developer if distinct from operator
      "investor": "Macquarie",             // capital partner, or null
      "investment": "US$1.37B green loan", // free text amount+currency, or null
      "phases": "Phase 1: 72MW (2026); total planned 500MW",  // or null
      "anchor_tenants": ["ByteDance"],     // pre-leased customers, [] ok
      "power": "100% renewable via PPA; PUE target 1.3",      // or null
      "announced": "2025-06",              // YYYY or YYYY-MM text, or null
      "construction_start": null,
      "rfs": "2027",                       // expected ready-for-service, or null
      // --- Directory-sourced profile (baxtel/datacentermap imports) ---
      "lat": 1.7034,                       // exact facility coordinates, or null
      "lon": 103.4135,                     //   (city-level fallback on the site)
      "category": "Colocation",            // Baxtel facility category, or null
      "company_type": "Carrier-Neutral",   // Baxtel operator type, or null
      "description": "Purpose-built ...",  // directory description, or null
      "year_built": "2020",                // year commissioned, or null
      "stage_detail": "Operational",       // source-verbatim stage label
                                           // (Baxtel layer_stage: Operational |
                                           //  Construction | Planned |
                                           //  Prospective Expansion | Land Bank |
                                           //  In Doubt | ...); null for DCM/news
      "expansion_planned": true,           // optional: Baxtel lists a
                                           // "Prospective Expansion" sibling
                                           // point for this site
      "leased_from": "Equinix Osaka OS1",  // optional: this entry is a tenant's
                                           // leased deployment inside a host
                                           // facility we do not track; flagged
                                           // in the UI, excluded from MW rollups
      "specs": {                           // optional: curated spec sheet from
        "Building size": "100,495 m²",     // DCM facility pages (label -> value)
        "Power per rack": "70 kW",
        "Tier (designed)": "III",
        "Certifications": "ISO 9001 · ISO 27001"
      }
      "status_history": [                  // appended on real status changes
        { "status": "announced", "date": "2026-08-14", "story_id": "..." }
        // News-driven entries carry story_id. Entries detected by the
        // weekly directory sync carry "source": "directory_sync" instead
        // (no story_id). Bulk-import backfill rows carry neither and are
        // ignored by reports.
      ],
      "story_ids": ["2026-08-14-a1b2c3"],  // linked digest stories, newest last
      // --- Directory sync bookkeeping (sync_directories.py; datacentermap) ---
      "dcm_id": 9844,                      // DCM-internal facility id, the
                                           // stable match key for re-syncs;
                                           // null until linked
      "dcm_sync": {                        // snapshot of the DCM listing summary
        "name": "...",                     // at last sync; next run diffs the
        "operator": "...",                 // fresh listing against THIS (never
        "city": "...",                     // against project fields) to decide
        "category": "..."                  // whether to re-fetch the detail page
      },
      "dcm_alt_ids": [13859],              // optional: DCM-side DUPLICATE
                                           // listings of this same facility
                                           // (merged-away copies, confirmed
                                           // double listings) — skipped by sync
      "dcm_detail_at": "2026-08-25",       // optional: last detail-page fetch;
                                           // drives the rotating detail refresh
                                           // (stalest first, SYNC_DETAIL_REFRESH)
      "first_seen": "2026-08-14",
      // last_updated = date of the last REAL change (new linked story, status
      // or field update) — NOT the pipeline run date. Null for seeds that
      // have never been updated by tracked news; the site shows "—".
      "last_updated": null,
      "seed": false,                       // true = seeded manually/LLM or
                                           // imported from a public directory,
                                           // not yet confirmed by a tracked story
      "source": "baxtel"                   // optional: provenance for bulk
                                           // directory imports ("baxtel" |
                                           // "datacentermap"); absent on
                                           // news-tracked entries
    }
  ]
}
```

A story ↔ project link is stored ONLY on the project (story_ids); the site
builds the reverse index. Matching new stories to projects: same operator
AND (name or city) fuzzy match; LLM arbitration when ambiguous.

## Non-goals for MVP
- No accounts, no newsletter
