"""Weekly/monthly report generator: data/news/*.json -> data/reports/*.json.

Aggregates the stories of an ISO week or calendar month, computes stats
locally, asks the LLM (same backend as process.py: API or kimi CLI) for a
title/lede/theme structure, and extracts project milestones from
data/projects.json without an LLM. Degrades gracefully: when no LLM backend
is available a region-grouped fallback report is written instead, so the
nightly run never hard-fails on this step.

CLI:
    python report.py weekly 2026-W34
    python report.py monthly 2026-08
    python report.py --auto        # Mon -> last ISO week; 1st -> last month
"""
from __future__ import annotations

import argparse
import calendar
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import process

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
NEWS_DIR = ROOT / "data" / "news"
REPORTS_DIR = ROOT / "data" / "reports"
PROJECTS_PATH = ROOT / "data" / "projects.json"

MAX_THEMES = 6
MIN_THEME_STORIES = 2
SUMMARY_CHARS = 240  # per-story input budget for the LLM prompt

_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")
_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")


# ------------------------------------------------------------- period math

def period_range(kind: str, period: str) -> tuple[date, date]:
    """Inclusive [start, end] dates for '2026-W34' (ISO Mon-Sun) or '2026-08'."""
    if kind == "weekly":
        m = _WEEK_RE.match(period)
        if not m:
            raise ValueError(f"bad weekly period {period!r} (want YYYY-Www)")
        start = date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
        return start, start + timedelta(days=6)
    m = _MONTH_RE.match(period)
    if not m:
        raise ValueError(f"bad monthly period {period!r} (want YYYY-MM)")
    year, month = int(m.group(1)), int(m.group(2))
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def period_id(kind: str, start: date) -> str:
    if kind == "weekly":
        iso = start.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    return start.strftime("%Y-%m")


def auto_periods(today: date) -> list[tuple[str, str]]:
    """Periods due today: last ISO week on Mondays, last month on the 1st."""
    out: list[tuple[str, str]] = []
    if today.weekday() == 0:
        start = today - timedelta(days=7)
        out.append(("weekly", period_id("weekly", start)))
    if today.day == 1:
        end = today - timedelta(days=1)
        out.append(("monthly", period_id("monthly", end.replace(day=1))))
    return out


# ----------------------------------------------------------- data gathering

def load_news_days() -> dict[date, list[dict]]:
    days: dict[date, list[dict]] = {}
    if not NEWS_DIR.exists():
        return days
    for path in sorted(NEWS_DIR.glob("*.json")):
        try:
            day = date.fromisoformat(path.stem)
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, json.JSONDecodeError, OSError) as exc:
            log.warning("skipping %s: %s", path.name, exc)
            continue
        days[day] = data.get("stories", [])
    return days


def period_stories(days: dict[date, list[dict]], start: date, end: date) -> list[dict]:
    """Stories whose published_at date falls inside [start, end] (the file
    date is the fallback when published_at is missing/unparseable). A story
    can sit in a day file outside its publish date, so scan every day."""
    seen: dict[str, dict] = {}
    for file_date, stories in days.items():
        for story in stories:
            try:
                pub = date.fromisoformat(str(story.get("published_at", ""))[:10])
            except ValueError:
                pub = file_date
            if start <= pub <= end and story.get("id") not in seen:
                seen[story["id"]] = story
    return sorted(seen.values(), key=lambda s: s.get("published_at", ""))


def period_complete(days: dict[date, list[dict]], start: date, end: date, today: date) -> bool:
    """A period is complete when it lies fully in the past and every day of
    it has a news file (days before the archive's first file count as gaps —
    the archive started 2026-08-13, earlier weeks are partial)."""
    if not days or end >= today or start < min(days):
        return False
    day = start
    while day <= end:
        if day not in days:
            return False
        day += timedelta(days=1)
    return True


def compute_stats(stories: list[dict]) -> dict:
    sources: set[str] = set()
    dates: set[str] = set()
    for story in stories:
        for src in story.get("sources", []):
            sources.add(src.get("publisher") or src.get("name", ""))
        dates.add(str(story.get("published_at", ""))[:10])
    return {
        "stories": len(stories),
        "sources": len(sources - {""}),
        "days": len(dates - {""}),
        "read_minutes": 0,  # filled in after the report text exists
    }


def project_milestones(start: date, end: date) -> list[dict]:
    """Status changes from projects.json whose history date falls in range.
    Entries carrying a story_id are news-driven events; entries with
    source == "directory_sync" come from the weekly DataCenterMap
    re-scan (sync_directories.py). The UI shows only the status badge(s) and
    date — provenance (news vs directory) is not surfaced. Other history rows
    without a story_id are bulk-import backfill stamps — untrustworthy as
    "events" and ignored here."""
    if not PROJECTS_PATH.exists():
        return []
    try:
        projects = json.loads(PROJECTS_PATH.read_text(encoding="utf-8")).get("projects", [])
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("could not read projects.json for milestones: %s", exc)
        return []
    out: list[dict] = []
    for p in projects:
        history = [h for h in (p.get("status_history") or []) if h.get("date")]
        history.sort(key=lambda h: h["date"])
        for i, entry in enumerate(history):
            origin = ("directory_sync"
                      if entry.get("source") == "directory_sync" else "news")
            if not entry.get("story_id") and origin != "directory_sync":
                continue
            try:
                day = date.fromisoformat(entry["date"][:10])
            except ValueError:
                continue
            if not (start <= day <= end):
                continue
            status_raw = str(entry.get("status", ""))
            status = status_raw.replace("_", " ")
            prev_raw: str | None = None
            if i == 0:
                change = f"First tracked as {status}"
            else:
                prev_raw = str(history[i - 1].get("status", ""))
                prev = prev_raw.replace("_", " ")
                change = f"Status: {prev} → {status}"
            out.append({
                "project_id": p.get("id", ""),
                "name": p.get("name", ""),
                "operator": p.get("operator", ""),
                "date": entry["date"][:10],
                "kind": "first_tracked" if prev_raw is None else "status_change",
                "origin": origin,
                "status": status_raw,
                "prev_status": prev_raw,
                "change": change,
                "story_id": entry.get("story_id"),
            })
    out.sort(key=lambda m: m["date"])
    return out


# ------------------------------------------------------------------ LLM part

_SYSTEM = (
    "You are the editor of DataCenter Atlas, an APAC data center industry "
    "intelligence service, writing its {kind} report for {start} to {end}. "
    "From the period's stories (JSON array; each has id, title, summary, "
    "publisher, regions, date) produce the report as JSON: "
    '{{"title": "...", "lede": "...", "themes": [...]}}. '
    "title — one short English headline (max 8 words) capturing the period's "
    "main thread, e.g. \"Johor's build-out accelerates\". "
    "lede — one 120-200 word paragraph narrating the period's main storyline. "
    "themes — 4 to 6 themes, each "
    '{{"heading": "max 8 words", "narrative": "2-4 sentences", '
    '"story_ids": ["..."]}}. Every story_id must be copied verbatim from the '
    "input; never invent ids. Each theme needs at least 2 stories; a story "
    "may appear in at most one theme. Factual newsroom English, no hype."
)


def llm_report(backend, kind: str, start: date, end: date, stories: list[dict]) -> dict:
    # The kimi CLI takes the prompt as a command-line argument, so the whole
    # payload must stay well under the Windows 32k-char command-line limit:
    # send the highest-score stories first and shrink/drop until it fits.
    ranked = sorted(stories, key=lambda s: s.get("score") or 0, reverse=True)
    summary_cap = SUMMARY_CHARS
    while True:
        payload = []
        for s in ranked:
            src = (s.get("sources") or [{}])[0]
            payload.append({
                "id": s["id"],
                "title": s.get("title", ""),
                "summary": str(s.get("summary", ""))[:summary_cap],
                "publisher": src.get("publisher") or src.get("name", ""),
                "regions": s.get("regions", []),
                "date": str(s.get("published_at", ""))[:10],
            })
        body = json.dumps(payload, ensure_ascii=False)
        if len(body) <= 22000:
            break
        if len(ranked) > 40:
            ranked = ranked[:max(40, int(len(ranked) * 0.85))]
        elif summary_cap > 120:
            summary_cap = 120  # same stories, shorter summaries
        elif len(ranked) > 10:
            ranked = ranked[:int(len(ranked) * 0.8)]
        else:
            raise ValueError("could not shrink LLM payload below the CLI limit")
    if len(ranked) < len(stories):
        log.info("LLM input trimmed to top %d of %d stories", len(ranked), len(stories))
    system = _SYSTEM.format(kind=kind, start=start.isoformat(), end=end.isoformat())
    data = process.chat_json(backend, system, body)
    if not isinstance(data, dict):
        raise ValueError("LLM report output is not an object")
    return data


def fallback_report(stories: list[dict]) -> dict:
    """No-LLM report: title/lede from the top stories, themes = regions."""
    ranked = sorted(stories, key=lambda s: s.get("score") or 0, reverse=True)
    title = ranked[0]["title"] if ranked else "Data center roundup"
    if len(title.split()) > 8:
        title = " ".join(title.split()[:8])
    lede = " ".join(
        str(s.get("summary", "")).strip() for s in ranked[:3] if s.get("summary")
    )
    by_region: dict[str, list[str]] = {}
    for s in ranked:
        region = (s.get("regions") or ["global"])[0]
        by_region.setdefault(region, []).append(s["id"])
    themes = [
        {"heading": f"{region.replace('-', ' ').title()} developments",
         "narrative": "", "story_ids": ids}
        for region, ids in by_region.items()
        if len(ids) >= MIN_THEME_STORIES
    ]
    return {"title": title, "lede": lede, "themes": themes[:MAX_THEMES]}


def validate_report(data: dict, stories: list[dict]) -> dict:
    """Enforce the contract: drop hallucinated story ids, empty themes,
    over-long theme lists; guarantee non-empty title/lede."""
    known = {s["id"] for s in stories}
    title = str(data.get("title", "")).strip()
    lede = str(data.get("lede", "")).strip()
    themes = []
    claimed: set[str] = set()
    for theme in data.get("themes") or []:
        if not isinstance(theme, dict):
            continue
        ids = [i for i in theme.get("story_ids") or []
               if isinstance(i, str) and i in known and i not in claimed]
        if len(ids) < MIN_THEME_STORIES:
            continue
        claimed.update(ids)
        themes.append({
            "heading": str(theme.get("heading", "")).strip()[:80],
            "narrative": str(theme.get("narrative", "")).strip(),
            "story_ids": ids,
        })
        if len(themes) >= MAX_THEMES:
            break
    if not title:
        title = "APAC data center roundup"
    if not lede:
        lede = " ".join(
            str(s.get("summary", "")).strip()
            for s in sorted(stories, key=lambda s: s.get("score") or 0, reverse=True)[:3]
            if s.get("summary")
        )
    return {"title": title, "lede": lede, "themes": themes}


def read_minutes(text: str) -> int:
    words = len(text.split())
    return max(1, round(words / 200))


# ---------------------------------------------------------------- generation

def generate(kind: str, period: str, today: date | None = None) -> Path | None:
    today = today or datetime.now(timezone.utc).date()
    start, end = period_range(kind, period)
    days = load_news_days()
    stories = period_stories(days, start, end)
    if not stories:
        log.warning("%s %s: no stories in %s..%s — skipping", kind, period, start, end)
        return None

    backend = process.select_backend()
    if backend is not None:
        try:
            raw = llm_report(backend, kind, start, end, stories)
        except Exception as exc:  # noqa: BLE001
            log.warning("LLM report step failed, using fallback: %s", exc)
            raw = fallback_report(stories)
    else:
        log.info("no LLM backend — writing fallback report")
        raw = fallback_report(stories)
    body = validate_report(raw, stories)

    stats = compute_stats(stories)
    stats["read_minutes"] = read_minutes(
        " ".join([body["title"], body["lede"],
                  *[t["heading"] + " " + t["narrative"] for t in body["themes"]]])
    )
    report = {
        "type": kind,
        "period": period,
        "date_range": {"start": start.isoformat(), "end": end.isoformat()},
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "complete": period_complete(days, start, end, today),
        "stats": stats,
        "title": body["title"],
        "lede": body["lede"],
        "themes": body["themes"],
        "project_milestones": project_milestones(start, end),
    }
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / f"{kind}-{period}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log.info("wrote %s (%d stories, %d themes, %d milestones, complete=%s)",
             out.name, stats["stories"], len(body["themes"]),
             len(report["project_milestones"]), report["complete"])
    return out


def auto_generate(today: date | None = None) -> list[Path]:
    """Generate whatever is due today (called by run.py after the digest)."""
    today = today or datetime.now(timezone.utc).date()
    made = []
    for kind, period in auto_periods(today):
        out = generate(kind, period, today=today)
        if out:
            made.append(out)
    if not made:
        log.info("reports: nothing due today")
    return made


def refresh_milestones() -> None:
    """Recompute project_milestones in every existing report JSON, without
    re-running the LLM. Use after changing the milestone schema/logic."""
    if not REPORTS_DIR.exists():
        return
    for path in sorted(REPORTS_DIR.glob("*.json")):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
            start = date.fromisoformat(report["date_range"]["start"])
            end = date.fromisoformat(report["date_range"]["end"])
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            log.warning("skipping %s: %s", path.name, exc)
            continue
        report["project_milestones"] = project_milestones(start, end)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        log.info("%s: %d milestones", path.name, len(report["project_milestones"]))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", nargs="?", choices=["weekly", "monthly"])
    parser.add_argument("period", nargs="?")
    parser.add_argument("--auto", action="store_true",
                        help="generate the period(s) due today")
    parser.add_argument("--refresh-milestones", action="store_true",
                        help="recompute milestones in existing reports (no LLM)")
    args = parser.parse_args()
    if args.auto:
        auto_generate()
        return
    if args.refresh_milestones:
        refresh_milestones()
        return
    if not args.kind or not args.period:
        parser.error("give KIND PERIOD (e.g. weekly 2026-W34) or --auto")
    generate(args.kind, args.period)


if __name__ == "__main__":
    main()
