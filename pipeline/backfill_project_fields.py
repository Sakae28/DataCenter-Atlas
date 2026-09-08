"""One-off backfill: populate the extended profile fields of seed projects
in data/projects.json from the LLM's public knowledge.

The 67 seed entries carry only name/operator/location/capacity/status; the
extended fields (developer, investor, investment, phases, anchor_tenants,
power, announced, construction_start, rfs) are almost all null, leaving the
site's detail pages empty. These are well-known projects (STT Nusajaya,
AirTrunk TOK1/SYD1, PDG JH1, GDS campuses, NEXTDC S3, Equinix TY15, ...),
so we ask the LLM — in region-grouped batches — for the fields it can back
with HIGH-CONFIDENCE public facts, then merge conservatively:

- only fields that are currently null are filled (news-sourced data wins);
- each returned update passes through projects._validate_update, plus extra
  plausibility checks (dates must contain a year, investment must look like
  an amount, rfs only for announced/under_construction, ...);
- story_ids, status, status_history, first_seen, seed and last_updated are
  never touched — a backfill is not a "real update".

Usage:  python backfill_project_fields.py
Writes data/projects.json (backup first at data/projects.json.bak2) and
prints per-field fill counts plus example profiles.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path

import process
import projects

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
PROJECTS_PATH = ROOT / "data" / "projects.json"
BACKUP_PATH = ROOT / "data" / "projects.json.bak2"

BATCH_SIZE = 12  # ~6 batches for 68 projects, grouped by region

FIELDS = ("developer", "investor", "investment", "phases", "power",
          "announced", "construction_start", "rfs")

_BACKFILL_SYSTEM = (
    "You maintain a database of data center projects/campuses in APAC. "
    "Below is a list of well-known projects (id, name, operator, city, "
    "country, capacity_mw, status). For each project, return the extended "
    "profile fields you know with HIGH CONFIDENCE from widely-reported "
    "public information. STRICT RULES: "
    "only fill a field when it is backed by a well-known public fact; when "
    "you are not confident, use JSON null. NEVER guess investment amounts "
    "or tenant names. "
    "Fields: "
    "developer — the developer only when genuinely distinct from the "
    "operator and well-known, else null; "
    "investor — capital partner / financing backer when well-known, else "
    "null; "
    "investment — amount + currency + short qualifier (e.g. \"US$1.37B "
    "green loan\"), null when unknown; "
    "phases — short text (e.g. \"Phase 1: 72MW (2026); total planned "
    "500MW\"), null when unknown; "
    "anchor_tenants — only publicly confirmed pre-lets/anchor customers "
    "(e.g. named hyperscaler commitments), up to 4 names, else []; "
    "power — only notable public commitments (renewable PPA, PUE targets), "
    "else null; "
    "announced — \"YYYY\" or \"YYYY-MM\" when well-known, else null; "
    "construction_start — \"YYYY\" or \"YYYY-MM\" when well-known, else "
    "null; "
    "rfs — expected ready-for-service date (e.g. \"2027\", \"Q3 2026\") "
    "only for announced/under_construction projects with a known future "
    "date; null for operational projects and when unknown. "
    'Reply with JSON: {"results": [{"id": "<project id, copied exactly>", '
    '"developer": null, "investor": null, "investment": null, '
    '"phases": null, "anchor_tenants": [], "power": null, '
    '"announced": null, "construction_start": null, "rfs": null}]}.'
)

_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_INVESTMENT_RE = re.compile(
    r"([$€£¥]|\b(USD|SGD|AUD|JPY|KRW|CNY|RMB|MYR|IDR)\b|"
    r"\b(billion|million|bn|loan|financing|investment)\b|"
    r"\b\d+(\.\d+)?\s*[BM]\b)", re.I)


class Drop(Exception):
    """Raised internally when an LLM value is judged implausible."""


def _plausible(field: str, value, project: dict):
    """Extra sanity checks beyond projects._validate_update. Raises Drop
    with a reason when the value looks implausible."""
    if value is None:
        return None
    if field == "anchor_tenants":
        out = []
        op = projects._canon(project.get("operator", ""))
        for t in value:
            ct = projects.canonical_operator(t)
            if projects._canon(ct) == op:
                raise Drop(f"tenant {t!r} is the operator itself")
            out.append(ct)
        return out
    if field in ("developer", "investor"):
        return projects.canonical_operator(value)
    if field in ("announced", "construction_start"):
        if not _YEAR_RE.search(value):
            raise Drop(f"{field} {value!r} has no year")
        return value
    if field == "rfs":
        if project.get("status") == "operational":
            raise Drop("rfs set on an operational project")
        if not (_YEAR_RE.search(value) or re.search(r"\b[QH][12]\b", value)):
            raise Drop(f"rfs {value!r} has no recognizable date")
        return value
    if field == "investment":
        if not (_INVESTMENT_RE.search(value) or re.search(r"\d", value)):
            raise Drop(f"investment {value!r} has no amount")
        return value
    return value


def _batched(items: list[dict], size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    backend = process.select_backend()
    if backend is None:
        raise SystemExit("no LLM backend available — aborting (file untouched)")
    log.info("LLM backend: %s", backend[0])

    db = projects.load_projects(PROJECTS_PATH)
    plist = db.get("projects", [])
    by_id = {p["id"]: p for p in plist}

    # Region-grouped batches so each LLM call has a coherent context.
    ordered: list[dict] = []
    for region in projects.REGIONS:
        ordered.extend(sorted((p for p in plist if p.get("region") == region),
                              key=lambda p: p["id"]))
    batches = list(_batched(ordered, BATCH_SIZE))
    log.info("%d projects in %d batches", len(ordered), len(batches))

    fills: dict[str, int] = {f: 0 for f in FIELDS + ("anchor_tenants",)}
    dropped: list[str] = []
    examples: list[dict] = []

    for bi, batch in enumerate(batches, 1):
        payload = [
            {"id": p["id"], "name": p.get("name"),
             "operator": p.get("operator"), "city": p.get("city"),
             "country": p.get("country"), "capacity_mw": p.get("capacity_mw"),
             "status": p.get("status")}
            for p in batch
        ]
        try:
            data = process.chat_json(backend, _BACKFILL_SYSTEM,
                                     json.dumps(payload, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            log.warning("batch %d failed, skipped: %s", bi, exc)
            continue
        results = data.get("results", [])
        log.info("batch %d: %d projects -> %d results", bi, len(batch),
                 len(results))
        for raw in results:
            if not isinstance(raw, dict):
                continue
            pid = str(raw.get("id", "")).strip()
            project = by_id.get(pid)
            if project is None:
                log.warning("batch %d: unknown id %r ignored", bi, pid)
                continue
            # Route through the same validation as news extraction: inject
            # the project's own identity fields, take the LLM's extended
            # fields only.
            raw.update({"story_id": "", "name": project["name"],
                        "operator": project["operator"],
                        "region": project["region"],
                        "status": project["status"],
                        "capacity_mw": None})
            upd = projects._validate_update(raw)
            if upd is None:
                continue
            changed_here = []
            for field in FIELDS:
                val = upd.get(field)
                if not val or project.get(field):
                    continue  # never overwrite a non-null value
                try:
                    val = _plausible(field, val, project)
                except Drop as why:
                    dropped.append(f"{pid}.{field}: {why}")
                    continue
                project[field] = val
                fills[field] += 1
                changed_here.append(field)
            tenants = upd.get("anchor_tenants") or []
            if tenants and not project.get("anchor_tenants"):
                try:
                    tenants = _plausible("anchor_tenants", tenants, project)
                except Drop as why:
                    dropped.append(f"{pid}.anchor_tenants: {why}")
                    tenants = []
                if tenants:
                    project["anchor_tenants"] = tenants
                    fills["anchor_tenants"] += 1
                    changed_here.append("anchor_tenants")
            if changed_here:
                examples.append({"id": pid, "name": project["name"],
                                 "fields": {f: project[f]
                                            for f in changed_here}})

    n_filled = sum(1 for p in plist
                   if any(p.get(f) for f in FIELDS + ("anchor_tenants",)))
    log.info("backfill: %d/%d projects now have extended data",
             n_filled, len(plist))

    if n_filled:
        shutil.copyfile(PROJECTS_PATH, BACKUP_PATH)
        projects.save_projects(db, PROJECTS_PATH)
        log.info("wrote %s (backup at %s)", PROJECTS_PATH, BACKUP_PATH)
    else:
        log.info("nothing filled — file left untouched")

    print("\n=== FILL COUNTS (projects per field) ===")
    for f in FIELDS + ("anchor_tenants",):
        print(f"  {f:<20} {fills[f]}")
    print(f"  projects with any extended data: {n_filled}/{len(plist)}")

    print("\n=== EXAMPLE FILLED PROFILES ===")
    for ex in examples[:3]:
        print(f"  {ex['name']} ({ex['id']})")
        for k, v in ex["fields"].items():
            print(f"    {k}: {v}")

    print("\n=== DROPPED AS IMPLAUSIBLE ===")
    if dropped:
        for d in dropped:
            print(f"  {d}")
    else:
        print("  (none)")


if __name__ == "__main__":
    main()
