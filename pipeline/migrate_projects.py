"""One-time migration of data/projects.json to the extended schema.

Adds the extended profile fields (developer/investor/investment/phases/
anchor_tenants/power/announced/construction_start/rfs/status_history) to
every entry, renames `expected` -> `rfs` under the existing date rule
(operational -> null; otherwise keep only dates not entirely in the past),
and fixes `last_updated` semantics: it is now the date of the last REAL
change — the latest linked story's date for news-linked entries, null for
never-updated seeds. Seeds also start with an empty status_history;
news-linked entries get one entry recording the status as of their latest
linked story.

    pipeline/.venv/Scripts/python pipeline/migrate_projects.py
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

import projects

log = logging.getLogger(__name__)

_YEAR_RE = re.compile(r"20\d{2}")
_STORY_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-")

# Schema key order per ../SPEC.md.
_KEY_ORDER = [
    "id", "name", "operator", "region", "country", "city",
    "capacity_mw", "capacity_note", "status",
    "developer", "investor", "investment", "phases", "anchor_tenants",
    "power", "announced", "construction_start", "rfs", "status_history",
    "story_ids", "first_seen", "last_updated", "seed",
]
_TEXT_FIELDS = ["developer", "investor", "investment", "phases", "power",
                "announced", "construction_start"]


def _migrate_rfs(project: dict, current_year: int) -> str | None:
    """expected -> rfs: null for operational; otherwise keep only dates
    with a year >= current year."""
    expected = project.get("expected")
    if not expected or project.get("status") == "operational":
        return None
    years = [int(y) for y in _YEAR_RE.findall(str(expected))]
    return expected if years and max(years) >= current_year else None


def _story_date(story_id: str) -> str | None:
    m = _STORY_DATE_RE.match(story_id or "")
    return m.group(1) if m else None


def migrate(db: dict, current_year: int) -> dict:
    out = []
    for p in db.get("projects", []):
        story_ids = p.get("story_ids") or []
        dates = [d for d in (_story_date(s) for s in story_ids) if d]
        migrated = {
            "id": p["id"],
            "name": p.get("name", ""),
            "operator": p.get("operator", ""),
            "region": p.get("region", ""),
            "country": p.get("country", ""),
            "city": p.get("city", ""),
            "capacity_mw": p.get("capacity_mw"),
            "status": p.get("status", "announced"),
            "anchor_tenants": list(p.get("anchor_tenants") or [])[:4],
            "rfs": _migrate_rfs(p, current_year),
            "story_ids": story_ids,
            "first_seen": p.get("first_seen"),
            "seed": bool(p.get("seed")) and not story_ids,
        }
        if p.get("capacity_note"):
            migrated["capacity_note"] = p["capacity_note"]
        for field in _TEXT_FIELDS:
            migrated.setdefault(field, p.get(field) or None)
        if story_ids:
            # News-linked: last real change = latest linked story; record
            # the current status as of that story.
            latest = max(dates) if dates else p.get("last_updated")
            migrated["last_updated"] = latest
            migrated["status_history"] = [
                {"status": migrated["status"], "date": latest,
                 "story_id": story_ids[-1]}]
        else:
            # Never updated by tracked news.
            migrated["last_updated"] = None
            migrated["status_history"] = []
        out.append({k: migrated.get(k) for k in _KEY_ORDER
                    if k in migrated or k not in ("capacity_note",)})
    return {"projects": out}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    db = projects.load_projects()
    current_year = datetime.now(timezone.utc).year
    before = db.get("projects", [])
    migrated = migrate(db, current_year)["projects"]

    linked = sum(1 for p in migrated if p["story_ids"])
    rfs_kept = sum(1 for p in migrated if p["rfs"])
    rfs_dropped = sum(1 for p in before
                      if p.get("expected")) - rfs_kept
    projects.save_projects({"projects": migrated})
    log.info("migrated %d projects (%d news-linked, %d seeds); "
             "rfs kept: %d, rfs nulled: %d; last_updated set on %d, "
             "null on %d",
             len(migrated), linked, len(migrated) - linked,
             rfs_kept, rfs_dropped,
             linked, len(migrated) - linked)


if __name__ == "__main__":
    main()
