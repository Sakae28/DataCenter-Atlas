"""Orchestrator: fetch -> process -> assemble -> write data/news/YYYY-MM-DD.json."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fetch
import fulltext
import process
import projects
import report

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "news"


def load_env() -> None:
    try:
        from dotenv import load_dotenv as _load
        _load(Path(__file__).parent / ".env")
    except ImportError:
        pass


def story_id(date: str, url: str) -> str:
    """Id from the primary source URL — stable across runs, unlike the
    LLM-normalized title, whose wording can vary between runs and produce
    duplicate ids for the same story."""
    digest = hashlib.sha1(process.canonical_url(url).encode("utf-8")).hexdigest()
    return f"{date}-{digest[:6]}"


def assemble_story(date: str, cluster: dict, generated_at: str) -> dict:
    items = cluster["items"]
    published = sorted(i["published_at"] for i in items if i["published_at"])
    regions = cluster.get("regions")
    if not regions:
        # LLM undecided. A source configured with a long region list (e.g.
        # W.Media covering all of APAC) must not tag every story with every
        # region — only inherit when the items agree on exactly one region,
        # otherwise tag "global".
        seen = []
        for item in items:
            for r in item["regions"]:
                if r in process.REGIONS and r not in seen:
                    seen.append(r)
        regions = seen if len(seen) == 1 else ["global"]
    # Dedup source entries: one entry per source NAME (a Google News query
    # feed can surface the same story under several redirect URLs — listing
    # the same source repeatedly is meaningless to readers). First wins,
    # sorted by weight.
    sources, seen_names = [], set()
    for entry in sorted(cluster["source_entries"], key=lambda e: -e["weight"]):
        if entry["name"] not in seen_names:
            seen_names.add(entry["name"])
            src = {"name": entry["name"], "url": entry["url"], "type": entry["type"]}
            if entry.get("publisher"):
                src["publisher"] = entry["publisher"]
            sources.append(src)
    score = cluster.get("score")
    story = {
        "id": story_id(date, sources[0]["url"]),
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
    companies = (cluster.get("companies") or [])[:6]
    if companies:
        story["companies"] = companies
    # Date-only sources: surface the precision marker so the site renders a
    # bare date instead of a fabricated time of day.
    if published:
        for item in items:
            if item["published_at"] == published[0] and item.get("published_precision"):
                story["published_precision"] = item["published_precision"]
                break
    return story


def story_urls(story: dict) -> set[str]:
    return {process.canonical_url(s["url"]) for s in story["sources"]}


def prefer(a: dict, b: dict) -> dict:
    """Pick the more complete copy of a story: content first, then score."""
    if bool(a.get("content")) != bool(b.get("content")):
        return a if a.get("content") else b
    sa = a.get("score") if a.get("score") is not None else -1
    sb = b.get("score") if b.get("score") is not None else -1
    return a if sa >= sb else b


def absorb(winner: dict, loser: dict) -> None:
    """Merge loser's sources into winner and backfill content/companies."""
    known_names = {s["name"] for s in winner["sources"]}
    known_urls = {s["url"] for s in winner["sources"]}
    for src in loser["sources"]:
        if src["name"] not in known_names and src["url"] not in known_urls:
            winner["sources"].append(src)
            known_names.add(src["name"])
            known_urls.add(src["url"])
    winner["heat"] = len({s["name"] for s in winner["sources"]})
    for field in ("content", "content_url", "published_precision"):
        if not winner.get(field) and loser.get(field):
            winner[field] = loser[field]
    companies = list(dict.fromkeys((winner.get("companies") or []) +
                                   (loser.get("companies") or [])))[:6]
    if companies:
        winner["companies"] = companies


def merge_stories(existing: list[dict], new: list[dict]) -> list[dict]:
    """Two stories are the same story when ids match OR they share any
    source URL. The more complete copy wins (content, then score); sources
    are merged and content fields backfilled from the loser."""
    by_id = {s["id"]: s for s in existing}
    url_index: dict[str, str] = {}  # canonical source URL -> story id
    for s in existing:
        for u in story_urls(s):
            url_index[u] = s["id"]
    for story in new:
        target_id = story["id"] if story["id"] in by_id else None
        if target_id is None:
            for u in story_urls(story):
                if u in url_index:
                    target_id = url_index[u]
                    break
        if target_id is None:
            by_id[story["id"]] = story
            for u in story_urls(story):
                url_index[u] = story["id"]
            continue
        target = by_id[target_id]
        if prefer(target, story) is target:
            absorb(target, story)
            for u in story_urls(target):
                url_index[u] = target_id
        else:
            absorb(story, target)
            del by_id[target_id]
            by_id[story["id"]] = story
            for u in story_urls(story):
                url_index[u] = story["id"]
    stories = sorted(by_id.values(), key=lambda s: s["published_at"], reverse=True)
    return stories


def hot_ids(stories: list[dict]) -> list[str]:
    ranked = sorted(
        stories,
        key=lambda s: (s["heat"], s["score"] if s["score"] is not None else -1),
        reverse=True,
    )
    return [s["id"] for s in ranked[:5]]


def title_fingerprint(title: str) -> str:
    """Normalized title for cross-day duplicate detection: lowercase,
    US/UK spelling unified, punctuation stripped. Exact-fingerprint matches
    across different URLs are the same wire story re-run on another day."""
    t = title.lower().replace("centre", "center")
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# Fingerprints shorter than this are too generic to trust as dup signals.
MIN_FINGERPRINT_LEN = 40


def recently_reported_urls(today: str, lookback_days: int = 2) -> set[str]:
    """Canonical source URLs from the previous days' files. The 48h fetch
    window overlaps consecutive days, so a story already reported yesterday
    must not re-enter today's file."""
    seen: set[str] = set()
    day = datetime.strptime(today, "%Y-%m-%d").date()
    for back in range(1, lookback_days + 1):
        path = DATA_DIR / f"{day - timedelta(days=back)}.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("could not read %s for cross-day dedup: %s", path, exc)
            continue
        for story in data.get("stories", []):
            seen |= story_urls(story)
    return seen


def recently_reported_index(today: str, lookback_days: int = 14):
    """Load previous days' stories for cross-day dedup. Returns
    (by_url, by_title, loaded): canonical URL / title fingerprint map to
    (day_path, story); `loaded` maps day_path -> full day data so that
    absorb() mutations can be written back."""
    by_url: dict[str, tuple[Path, dict]] = {}
    by_title: dict[str, tuple[Path, dict]] = {}
    loaded: dict[Path, dict] = {}
    day = datetime.strptime(today, "%Y-%m-%d").date()
    for back in range(1, lookback_days + 1):
        path = DATA_DIR / f"{day - timedelta(days=back)}.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("could not read %s for cross-day dedup: %s", path, exc)
            continue
        loaded[path] = data
        for story in data.get("stories", []):
            for u in story_urls(story):
                by_url.setdefault(u, (path, story))
            fp = title_fingerprint(story.get("title", ""))
            if len(fp) >= MIN_FINGERPRINT_LEN:
                by_title.setdefault(fp, (path, story))
    return by_url, by_title, loaded


def merge_into_previous(by_url, by_title, story: dict) -> Path | None:
    """If `story` duplicates a previous day's story (shared canonical URL or
    identical title fingerprint), absorb it into that story and return the
    path of the day file that needs rewriting; otherwise None."""
    hit: tuple[Path, dict] | None = None
    for u in story_urls(story):
        if u in by_url:
            hit = by_url[u]
            break
    if hit is None:
        fp = title_fingerprint(story.get("title", ""))
        if len(fp) >= MIN_FINGERPRINT_LEN:
            hit = by_title.get(fp)
    if hit is None:
        return None
    path, orig = hit
    absorb(orig, story)
    # Register the new story's keys so later duplicates today hit the
    # already-merged original too.
    for u in story_urls(story):
        by_url.setdefault(u, (path, orig))
    fp = title_fingerprint(story.get("title", ""))
    if len(fp) >= MIN_FINGERPRINT_LEN:
        by_title.setdefault(fp, (path, orig))
    return path


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

    # Cross-day dedup: stories whose primary URL or normalized title already
    # ran in the last 14 days don't re-enter today's file — the new source is
    # merged into the original day's story (heat goes up, one canonical
    # entry per event). The old 2-day URL-only drop missed same-event
    # re-reports from different outlets.
    by_url, by_title, loaded_days = recently_reported_index(date)
    dirty_days: dict[Path, None] = {}
    if by_url or by_title:
        fresh = []
        for s in stories:
            dirty = merge_into_previous(by_url, by_title, s)
            if dirty is not None:
                dirty_days[dirty] = None
                log.info("cross-day merge: %r -> %s", s["title"][:60], dirty.name)
            else:
                fresh.append(s)
        if len(fresh) < len(stories):
            log.info("cross-day dedup: merged %d already-reported stories into previous days",
                     len(stories) - len(fresh))
        stories = fresh

    if fulltext.enabled():
        ok, failed = fulltext.enrich_stories(stories)
        log.info("full-text: %d extracted, %d without content", ok, failed)
    else:
        log.info("FETCH_FULLTEXT=0: skipping full-text extraction")

    out_path = DATA_DIR / f"{date}.json"
    if out_path.exists():
        existing = json.loads(out_path.read_text(encoding="utf-8"))
        stories = merge_stories(existing.get("stories", []), stories)
        log.info("merged with existing %s -> %d stories", out_path.name, len(stories))

    # Summary QA: stories whose summary repeats the headline (e.g. Google
    # News snippet fallbacks) get one derived from the extracted full text;
    # without content the summary stays empty and the site hides it.
    for story in stories:
        summary = story.get("summary", "")
        if not summary or process.summaries_equivalent(story["title"], summary):
            derived = process.derive_summary(story.get("content"))
            # The first paragraph can itself be the headline restated; don't
            # keep a "new" summary that still repeats the title.
            if derived and process.summaries_equivalent(story["title"], derived):
                derived = ""
            story["summary"] = derived

    # Project tracking: story ids are final at this point. Skipped silently
    # when no LLM backend is available.
    backend = process.select_backend()
    projects.update_projects_step(backend, stories, today=date)

    # Empty-digest guard: a run that produced zero stories is a failure
    # (accelerator down, sources unreachable, ...), not a "quiet news day".
    # Never write/publish an empty day file; exit non-zero so the caller
    # (daily_run.bat) skips the commit and the failure shows up in the log.
    if not stories:
        log.error("EMPTY DIGEST: 0 stories after processing — refusing to "
                  "write %s. Check fetch failures above.", out_path)
        sys.exit(2)

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

    # Rewrite previous-day files that absorbed cross-day duplicates (sources
    # merged in memory by merge_into_previous; persist + refresh hot list).
    for path in dirty_days:
        day_data = loaded_days[path]
        day_data["stories"].sort(key=lambda s: s["published_at"], reverse=True)
        day_data["hot"] = hot_ids(day_data["stories"])[:5]
        path.write_text(json.dumps(day_data, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        log.info("updated %s after cross-day source merge", path.name)

    # Reports: on Mondays / the 1st, compile last week's / last month's report.
    # Failures here must never break the digest itself.
    try:
        report.auto_generate(today=now.date())
    except Exception as exc:  # noqa: BLE001
        log.warning("report auto-generation failed: %s", exc)

    # Directory sync: on Sundays (or when SYNC_DIRECTORIES=1) re-scan the
    # DataCenterMap listings for new/changed facilities. Never breaks the
    # daily digest.
    try:
        if now.weekday() == 6 or os.environ.get("SYNC_DIRECTORIES") == "1":
            import sync_directories
            sync_directories.run_sync()
    except Exception as exc:  # noqa: BLE001
        log.warning("directory sync failed: %s", exc)


if __name__ == "__main__":
    main()
