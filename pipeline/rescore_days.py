"""Re-run LLM annotation (score/regions/topics/companies) and enrichment
(summary/why_it_matters) on existing day files that were processed while
the LLM backend was down (heuristic fallback leaves score/topics/companies
empty and skips enrichment).

Unlike a full re-run, this touches NO fetching: it reuses the stories
already in the day file and only fills in the LLM-derived fields, then
rebuilds `featured` flags and the `hot` list.

Usage:
    pipeline/.venv/Scripts/python pipeline/rescore_days.py data/news/2026-09-20.json [...]
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import process
import run

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def story_as_cluster(story: dict) -> dict:
    """Wrap an assembled story in the minimal cluster shape the LLM steps
    expect (representative() reads items[].title/snippet/source_weight/
    published_at)."""
    snippet = story.get("summary") or ""
    if not snippet and story.get("content"):
        snippet = process.markdown_to_text(story["content"])[:400]
    return {
        "items": [{
            "title": story["title"],
            "snippet": snippet,
            "published_at": story.get("published_at"),
            "regions": story.get("regions") or [],
            "source_weight": 1,
        }],
        "source_entries": [],
        "_story": story,
    }


def rescore(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    stories = data.get("stories", [])
    if not stories:
        log.info("%s: empty, skipped", path.name)
        return
    backend = process.select_backend()
    if backend is None:
        log.error("no LLM backend available — aborting (would be a no-op)")
        sys.exit(1)

    clusters = [story_as_cluster(s) for s in stories]
    process.llm_relevance(backend, clusters)
    process.llm_enrich(backend, clusters)

    for cluster in clusters:
        story = cluster["_story"]
        if cluster.get("score") is not None:
            story["score"] = cluster["score"]
        if cluster.get("regions"):
            story["regions"] = cluster["regions"]
        if cluster.get("topics"):
            story["topics"] = cluster["topics"]
        if cluster.get("companies"):
            story["companies"] = cluster["companies"]
        # Enrichment only fills empty fields — never overwrite an existing
        # summary/why_it_matters from a healthy run.
        if cluster.get("summary") and not story.get("summary"):
            story["summary"] = cluster["summary"]
        if cluster.get("why_it_matters") and not story.get("why_it_matters"):
            story["why_it_matters"] = cluster["why_it_matters"]
        story["featured"] = (
            story.get("score") is not None
            and story["score"] >= process.FEATURE_THRESHOLD
        )

    data["hot"] = run.hot_ids(stories)[:5]
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    scored = sum(1 for s in stories if s.get("score") is not None)
    wim = sum(1 for s in stories if s.get("why_it_matters"))
    log.info("%s: %d stories — %d scored, %d with why_it_matters, hot=%d",
             path.name, len(stories), scored, wim, len(data["hot"]))


def main() -> None:
    run.load_env()
    for arg in sys.argv[1:]:
        rescore(Path(arg))


if __name__ == "__main__":
    main()
