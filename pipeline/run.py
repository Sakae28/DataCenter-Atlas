"""Orchestrator: fetch -> process -> assemble -> write data/news/YYYY-MM-DD.json."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import fetch
import process

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "news"


def load_env() -> None:
    try:
        from dotenv import load_dotenv as _load
        _load(Path(__file__).parent / ".env")
    except ImportError:
        pass


def story_id(date: str, title: str) -> str:
    digest = hashlib.sha1(process.normalize_title(title).encode("utf-8")).hexdigest()
    return f"{date}-{digest[:6]}"


def assemble_story(date: str, cluster: dict, generated_at: str) -> dict:
    items = cluster["items"]
    published = sorted(i["published_at"] for i in items if i["published_at"])
    regions = cluster.get("regions")
    if not regions:
        seen = []
        for item in items:
            for r in item["regions"]:
                if r in process.REGIONS and r not in seen:
                    seen.append(r)
        regions = seen or ["global"]
    # Dedup source entries by URL, keep the schema shape {name, url, type}.
    sources, seen_urls = [], set()
    for entry in sorted(cluster["source_entries"], key=lambda e: -e["weight"]):
        key = process.canonical_url(entry["url"])
        if key not in seen_urls:
            seen_urls.add(key)
            sources.append({"name": entry["name"], "url": entry["url"], "type": entry["type"]})
    score = cluster.get("score")
    return {
        "id": story_id(date, cluster["title_en"]),
        "title": cluster["title_en"],
        "summary": cluster.get("summary", ""),
        "why_it_matters": cluster.get("why_it_matters", ""),
        "score": score,
        "heat": len({s["name"] for s in sources}),
        "regions": regions,
        "topics": (cluster.get("topics") or [])[:4],
        "published_at": published[0] if published else generated_at,
        "featured": score is not None and score >= process.FEATURE_THRESHOLD,
        "sources": sources,
    }


def merge_stories(existing: list[dict], new: list[dict]) -> list[dict]:
    """Existing stories win on id conflict; merge sources and recompute heat."""
    by_id = {s["id"]: s for s in existing}
    for story in new:
        if story["id"] in by_id:
            target = by_id[story["id"]]
            known = {s["url"] for s in target["sources"]}
            for src in story["sources"]:
                if src["url"] not in known:
                    target["sources"].append(src)
                    known.add(src["url"])
            target["heat"] = len({s["name"] for s in target["sources"]})
        else:
            by_id[story["id"]] = story
    stories = sorted(by_id.values(), key=lambda s: s["published_at"], reverse=True)
    return stories


def hot_ids(stories: list[dict]) -> list[str]:
    ranked = sorted(
        stories,
        key=lambda s: (s["heat"], s["score"] if s["score"] is not None else -1),
        reverse=True,
    )
    return [s["id"] for s in ranked[:5]]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_env()

    now = datetime.now(timezone.utc)
    date = now.date().isoformat()
    generated_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    items, failed = fetch.fetch_all()
    log.info("fetched %d raw items (%d sources failed: %s)",
             len(items), len(failed), failed or "none")

    clusters = process.process_items(items)
    stories = [assemble_story(date, c, generated_at) for c in clusters]
    stories.sort(key=lambda s: s["published_at"], reverse=True)

    out_path = DATA_DIR / f"{date}.json"
    if out_path.exists():
        existing = json.loads(out_path.read_text(encoding="utf-8"))
        stories = merge_stories(existing.get("stories", []), stories)
        log.info("merged with existing %s -> %d stories", out_path.name, len(stories))

    payload = {
        "date": date,
        "generated_at": generated_at,
        "hot": hot_ids(stories),
        "stories": stories,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    log.info("wrote %s (%d stories, %d hot)", out_path, len(stories), len(payload["hot"]))


if __name__ == "__main__":
    main()
