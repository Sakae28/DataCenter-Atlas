"""Maintenance: heal existing day files — dedup stories + refresh content.

Phase A (dedup): stories that share ANY canonical source URL — within one
day file or across the two most recent files — are the same story. The
merged story belongs to the EARLIEST day it appeared; the surviving copy is
the more complete one (content first, then higher score) with sources and
content fields merged in from the losers. Ids are regenerated from the
primary source URL (run.story_id) and each file's hot list is rebuilt.

Phase B (content): every story is re-extracted through the current
fulltext.py (favor_recall, 20k cap, mojibake retry, cloudscraper 403
fallback, GN decode retry). Stories with a content_url are fetched via that
URL to avoid re-decoding Google News links. A failed re-extraction never
removes existing content.

Usage:
    FETCH_PROXY=http://127.0.0.1:7078 FETCH_INSECURE_TLS=1 \
        pipeline/.venv/Scripts/python pipeline/heal_data.py [--dedup-only|--content-only] [day files...]

Defaults to data/news/2026-08-13.json and data/news/2026-08-14.json.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import run
import fulltext
from fetch import make_client
from process import canonical_url

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FILES = [
    ROOT / "data/news/2026-08-13.json",
    ROOT / "data/news/2026-08-14.json",
]


# ------------------------------------------------------------- phase A: dedup

def find_groups(stories: list[tuple[str, dict]]) -> list[list[int]]:
    """Union-find over (file_date, story) pairs: two stories sharing any
    canonical source URL belong to one group."""
    parent = list(range(len(stories)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        parent[find(i)] = find(j)

    by_url: dict[str, int] = {}
    for i, (_, story) in enumerate(stories):
        for src in story["sources"]:
            key = canonical_url(src["url"])
            if key in by_url:
                union(i, by_url[key])
            else:
                by_url[key] = i
    groups: dict[int, list[int]] = {}
    for i in range(len(stories)):
        groups.setdefault(find(i), []).append(i)
    return [g for g in groups.values() if len(g) > 1]


def dedup_files(paths: list[Path]) -> dict[str, dict]:
    files = {p.name[:10]: json.loads(p.read_text(encoding="utf-8")) for p in paths}
    stories = [(date, s) for date in sorted(files) for s in files[date]["stories"]]
    groups = find_groups(stories)
    merged = 0
    remove: dict[str, set[int]] = {date: set() for date in files}  # id() of losers
    move: dict[str, list[dict]] = {date: [] for date in files}     # winners moving to earlier day
    for group in groups:
        copies = [stories[i] for i in group]
        winner_date = min(date for date, _ in copies)
        winner = copies[0][1]
        for _, copy in copies[1:]:
            if run.prefer(winner, copy) is copy:
                run.absorb(copy, winner)
                winner = copy
            else:
                run.absorb(winner, copy)
        for date, copy in copies:
            if copy is not winner:
                remove[date].add(id(copy))
                merged += 1
            elif date != winner_date:
                remove[date].add(id(copy))
                move[winner_date].append(copy)
    for date, data in files.items():
        data["stories"] = [s for s in data["stories"] if id(s) not in remove[date]]
        data["stories"].extend(move[date])
    # Regenerate ids from the primary source URL and rebuild hot lists.
    for date, data in files.items():
        for story in data["stories"]:
            story["id"] = run.story_id(date, story["sources"][0]["url"])
        data["stories"].sort(key=lambda s: s["published_at"], reverse=True)
        data["hot"] = run.hot_ids(data["stories"])
    return {"files": files, "groups": len(groups), "removed": merged}


# ---------------------------------------------------------- phase B: content

def heal_content(paths: list[Path], files: dict[str, dict] | None = None) -> dict[str, dict]:
    loaded = files or {p.name[:10]: json.loads(p.read_text(encoding="utf-8")) for p in paths}
    with make_client() as client:
        for date in sorted(loaded):
            for story in loaded[date]["stories"]:
                had = bool(story.get("content"))
                url = story.get("content_url") or story["sources"][0]["url"]
                try:
                    got = fulltext.extract_story(client, story, url=url)
                except Exception as exc:  # noqa: BLE001 - one story never kills the run
                    log.info("heal failed for %s (%s): %s", story["id"], url, exc)
                    got = None
                if got:
                    story["content"], story["content_url"] = got
                    tag = "refreshed" if had else "GAINED"
                    print(f"  {tag}: {story['id']} ({len(got[0])} chars) {story['title'][:60]}")
                elif not had:
                    print(f"  STILL NO CONTENT: {story['id']} {url[:80]} | {story['title'][:50]}")
    return loaded


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "WARNING").upper())
    args = [a for a in sys.argv[1:]]
    dedup_only = "--dedup-only" in args
    content_only = "--content-only" in args
    paths = [Path(a) for a in args if not a.startswith("--")] or DEFAULT_FILES

    files = None
    if not content_only:
        before = {}
        for p in paths:
            data = json.loads(p.read_text(encoding="utf-8"))
            before[p.name] = (len(data["stories"]),
                              sum(1 for s in data["stories"] if s.get("content")))
        result = dedup_files(paths)
        files = result["files"]
        print(f"dedup: {result['groups']} duplicate groups merged, "
              f"{result['removed']} copies removed")
        for p in paths:
            data = files[p.name[:10]]
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
            after = (len(data["stories"]),
                     sum(1 for s in data["stories"] if s.get("content")))
            print(f"  {p.name}: {before[p.name][0]} -> {after[0]} stories, "
                  f"with content {before[p.name][1]} -> {after[1]}")

    if not dedup_only:
        print("content healing:")
        files = heal_content(paths, files)
        for p in paths:
            data = files[p.name[:10]]
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
            total = len(data["stories"])
            withc = sum(1 for s in data["stories"] if s.get("content"))
            fffd = sum(1 for s in data["stories"] if "\ufffd" in s.get("content", ""))
            print(f"  {p.name}: {total} stories, {withc} with content, {fffd} with mojibake")


if __name__ == "__main__":
    main()
