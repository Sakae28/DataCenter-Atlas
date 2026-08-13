# APAC Data Center Intelligence Platform — MVP Spec

## Goal
Daily automated aggregation of data center industry news across APAC
(China, Japan, Korea, Australia, Southeast Asia), modeled on the
information architecture of https://aihot.virxact.com/ :
multi-source ingestion → LLM scoring/dedup/summarization → static site
with a top "hot list" and a day-grouped timeline. English-first content.

## Architecture

```
pipeline/  (Python 3.11, runs daily via GitHub Actions)
  sources.yaml      source registry
  fetch.py          RSS/feed fetching -> raw candidates
  process.py        LLM relevance scoring, clustering, summarization
  run.py            orchestrator: fetch -> process -> write data/
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
      "published_at": "2026-08-13T01:57:00Z",  // earliest source timestamp
      "featured": true,                   // score >= FEATURE_THRESHOLD
      "sources": [
        { "name": "Data Center Dynamics", "url": "https://...", "type": "rss" }
      ]
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

## Non-goals for MVP
- No project database (but raw structured fields preserved in pipeline
  intermediate files for later reuse)
- No accounts, no newsletter, no search
