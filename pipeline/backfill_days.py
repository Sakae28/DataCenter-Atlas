"""Backfill missing day files after a gap in running the pipeline.

Fetches every source once with an extended age window
(FETCH_MAX_AGE_HOURS, e.g. 120 for a 4-day gap), partitions items by their
UTC published date, then runs the full per-day pipeline (process ->
assemble -> cross-day dedup -> full-text -> projects) for each missing
day, oldest first — reproducing what daily runs would have produced.

Existing day files are never touched; run the normal `run.py` afterwards
for today. Items with no parseable timestamp are skipped here (today's
run picks them up while they're still in the feeds).

Usage:
    FETCH_MAX_AGE_HOURS=120 python pipeline/backfill_days.py \
        2026-08-15 2026-08-16 2026-08-17 2026-08-18

With no date arguments, defaults to every missing day between the newest
existing file and yesterday (UTC).
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fetch
import fulltext
import process
import projects
import run

log = logging.getLogger(__name__)

DATA_DIR = run.DATA_DIR


def default_missing_days() -> list[str]:
    existing = {p.stem for p in DATA_DIR.glob("????-??-??.json")}
    if not existing:
        log.error("no existing day files; pass explicit dates")
        return []
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    # Scan the fetch window for holes, not just days after the newest file:
    # a day run between gaps must not mask older missing days.
    hours = int(os.environ.get("FETCH_MAX_AGE_HOURS", "48"))
    window = max(2, hours // 24 + 1)
    days = []
    day = yesterday - timedelta(days=window)
    while day <= yesterday:
        if day.isoformat() not in existing:
            days.append(day.isoformat())
        day += timedelta(days=1)
    return days


def main(argv: list[str]) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run.load_env()

    days = argv or default_missing_days()
    days = [d for d in days if not (DATA_DIR / f"{d}.json").exists()]
    if not days:
        log.info("nothing to backfill")
        return
    log.info("backfilling %d days: %s", len(days), ", ".join(days))
    wanted = set(days)

    items, failed = fetch.fetch_all()
    log.info("fetched %d raw items (%d sources failed: %s)",
             len(items), len(failed), failed or "none")

    # Partition items by UTC published date; undated items are left for
    # today's normal run.
    by_day: dict[str, list[dict]] = {d: [] for d in days}
    undated = 0
    for item in items:
        if not item["published_at"]:
            undated += 1
            continue
        day = item["published_at"][:10]
        if day in wanted:
            by_day[day].append(item)
    log.info("partitioned: %s; %d undated items skipped",
             {d: len(v) for d, v in by_day.items()}, undated)

    backend = process.select_backend()
    now = datetime.now(timezone.utc)
    generated_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    for date in days:
        day_items = by_day[date]
        if not day_items:
            log.warning("%s: no items in window — writing empty digest", date)
        clusters = process.process_items(day_items)
        stories = [run.assemble_story(date, c, generated_at) for c in clusters]
        stories.sort(key=lambda s: s["published_at"], reverse=True)

        # Same cross-day dedup as run.py: drop stories whose primary source
        # URL was already reported in the previous ~2 days.
        reported = run.recently_reported_urls(date)
        if reported:
            fresh = [s for s in stories
                     if process.canonical_url(s["sources"][0]["url"]) not in reported]
            if len(fresh) < len(stories):
                log.info("%s: cross-day dedup dropped %d already-reported stories",
                         date, len(stories) - len(fresh))
            stories = fresh

        if fulltext.enabled():
            ok, no_content = fulltext.enrich_stories(stories)
            log.info("%s: full-text: %d extracted, %d without content",
                     date, ok, no_content)

        projects.update_projects_step(backend, stories, today=date)

        payload = {
            "date": date,
            "generated_at": generated_at,
            "hot": run.hot_ids(stories),
            "stories": stories,
        }
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        out_path = DATA_DIR / f"{date}.json"
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
        log.info("%s: wrote %d stories (%d hot)", date, len(stories), len(payload["hot"]))


if __name__ == "__main__":
    main(sys.argv[1:])
