"""Backfill `companies` on existing day files without re-running the pipeline.

Loads each day file, sends all stories (id + title + summary) in ONE batched
LLM call per file, and writes the returned canonical company names back onto
each story (max 6, per SPEC.md). Reuses process.py's backend selection and
JSON parsing.

Usage:
    LLM_BACKEND=kimi-cli pipeline/.venv/Scripts/python pipeline/backfill_companies.py [day files...]

Defaults to data/news/2026-08-13.json and data/news/2026-08-14.json.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import process

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FILES = [
    ROOT / "data/news/2026-08-13.json",
    ROOT / "data/news/2026-08-14.json",
]

_SYSTEM = (
    "You are an analyst for an APAC data center industry news service. For "
    "each story, list the companies/organizations the story is ABOUT, using "
    'canonical names (e.g. "NVIDIA", "STT GDC", "Equinix", "NTT", '
    '"AirTrunk"). Max 6 per story. Omit generic mentions with no substance; '
    "use an empty array when none apply. "
    'Reply with JSON: {"results": [{"id": "...", "companies": [...]}]} — one '
    "entry per input story, echoing its id."
)


def backfill_file(backend, path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    stories = data["stories"]
    payload = [{"id": s["id"], "title": s["title"],
                "summary": (s.get("summary") or "")[:400]}
               for s in stories]
    res = process.chat_json(backend, _SYSTEM, json.dumps(payload, ensure_ascii=False))
    by_id = {}
    for entry in res.get("results", []):
        comps = entry.get("companies")
        if isinstance(entry.get("id"), str) and isinstance(comps, list):
            by_id[entry["id"]] = [str(c).strip() for c in comps if str(c).strip()][:6]
    filled = 0
    for story in stories:
        comps = by_id.get(story["id"])
        if comps:
            story["companies"] = comps
            filled += 1
        else:
            story.pop("companies", None)
            log.info("no companies returned for %s", story["id"])
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    print(f"{path.name}: {filled}/{len(stories)} stories with companies")
    for story in stories[:3]:
        print(f"  {story['id']}: {story.get('companies')}")


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    paths = [Path(a) for a in sys.argv[1:]] or DEFAULT_FILES
    backend = process.select_backend()
    if backend is None:
        sys.exit("no LLM backend available (set LLM_BACKEND / LLM_API_KEY)")
    log.info("LLM backend: %s", backend[0])
    for path in paths:
        backfill_file(backend, path)


if __name__ == "__main__":
    main()
