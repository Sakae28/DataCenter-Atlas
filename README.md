# DataCenter Atlas

An automated Asia-Pacific data center industry tracker:
[https://sakae28.github.io/DataCenter-Atlas/](https://sakae28.github.io/DataCenter-Atlas/)

- **Latest** — daily AI-curated news digest (industry press + company newsrooms), clustered, scored and summarized
- **Pipeline** — 2,800+ tracked data center projects/facilities with status, capacity, investment, specs and an interactive map
- **Companies** — operator directory aggregated from the project database
- **Reports** — auto-generated weekly/monthly digests

## Architecture

```
pipeline/   Python: fetch -> process (LLM) -> assemble -> data/news/*.json
site/       Astro static site, built from data/ and deployed to GitHub Pages
data/       news digests (per-day JSON), projects.json, generated reports
.github/    Actions: build+deploy on push; daily data-freshness check
```

Daily flow: a local scheduled task (`pipeline/daily_run.bat`) fetches, scores
(LLM via kimi), dedups (cross-day URL + title-fingerprint merge), extracts
full text, updates the project database, then commits and pushes. GitHub
Actions rebuilds and deploys the site on every push. A separate scheduled
workflow (`data-check.yml`) opens an issue when a day's digest is missing or
empty.

## Local development

```sh
# Pipeline (Python 3.11+, deps in a venv)
cd pipeline && python -m venv .venv && .venv/Scripts/activate
pip install -r requirements.txt
python run.py                 # fetch + process today's digest

# Site
cd site && npm install
npm run dev                   # dev server
npm run build                 # static build -> dist/
```

The site reads `data/` from the repo root; if the pipeline has never run it
falls back to bundled fixtures in `site/fixtures/`.

## Content & data policy

- News stories are credited and linked to the original publisher. Mirrored
  article text is collapsed by default and stripped of images; set
  `fulltext: none` on a source in `pipeline/sources.yaml` to opt it out of
  mirroring entirely (summary + outbound link only).
- Scores, summaries and "why it matters" notes are AI-generated heuristics,
  not editorial judgment.
- Facility/project data combines tracked news with public directory
  listings (DataCenterMap, Baxtel); treat single-source directory data as
  indicative.
- Coverage scope: China, Japan, Korea, Australia, Southeast Asia, plus
  global stories that materially affect APAC. India is out of scope.

## License

Code is MIT (see LICENSE). News article text and third-party data remain the
property of their respective publishers and are reproduced here for
archival/reference with attribution; a takedown request via a GitHub issue
is honored promptly.
