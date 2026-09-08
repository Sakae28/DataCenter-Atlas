"""Incremental sync of the DataCenterMap directory into data/projects.json.

DCM updates its listings over time (new facilities, renames, stage flips);
this script re-scans the country/city listing pages periodically and applies
only real changes. Unlike import_external.py (one-off bulk import) this is
a DIFF run:

- Listing pages (country -> market -> facility cards embedded as
  __NEXT_DATA__ JSON) are always re-fetched; they carry the DCM-internal
  facility ``id``, name, operator, market city, category and coordinates,
  but NOT stage/MW.
- Each listing is matched to a local project by its DCM id (stored as
  ``dcm_id`` on first encounter; the initial run links existing projects via
  the same strict name/operator/city matching as import_external.py and
  backfills ``dcm_id`` + a ``dcm_sync`` snapshot of the listing summary).
- Only genuinely NEW facilities and facilities whose LISTING SUMMARY changed
  (compared against the stored ``dcm_sync`` snapshot, so DCM-vs-DCM with no
  matching noise) get their detail page re-fetched (stage, built-out/
  pipeline MW, year of operation, spec sheet). --limit caps the number of
  detail-page fetches per run; the rest are deferred to the next run.
- After the listing diff, a ROTATING DETAIL REFRESH re-fetches the detail
  pages of the N DCM-linked projects whose ``dcm_detail_at`` is oldest
  (never-fetched first; N = --detail-refresh, env SYNC_DETAIL_REFRESH,
  default 150). Listing pages carry no stage/MW, so a pure status flip
  (planned -> live) is invisible to the summary diff — the rotation closes
  that blind spot. Detail-field changes follow the same update rules;
  unchanged projects only get ``dcm_detail_at`` bumped.
- Projects may carry ``dcm_alt_ids``: DCM-side duplicate listings of the
  same facility (a merged-away copy's id, or a confirmed double listing).
  Those listings are skipped outright — no diff, no flag, no fetch.

Field-level updates follow the pipeline's existing policies: status never
regresses (projects._next_status), capacity only grows, coordinates fill
only when missing. A real status change appends
``{"status": ..., "date": <sync day>, "source": "directory_sync"}`` to
status_history (NO story_id — those are reserved for news-driven entries),
and real changes bump ``last_updated`` to the sync date. Unchanged projects
are never touched — no date re-stamping.

Operator changes are NOT applied automatically (they usually mean an
ownership change worth a human look); they are recorded under "flags" in
the sync summary. So are ambiguous first-link matches (several existing
projects fit one listing — the DB contains baxtel/datacentermap duplicate
entries for some facilities): flagged, never auto-created as duplicates.

Every non-dry-run writes a summary to data/directory-sync-YYYY-MM-DD.json
and prints a human-readable digest. run.py calls run_sync() on Sundays (or
when SYNC_DIRECTORIES=1); failures there never break the daily digest.

Usage:
    python sync_directories.py                  # full sync
    python sync_directories.py --dry-run        # report only, no writes
    python sync_directories.py --limit 30       # cap detail-page fetches
    python sync_directories.py --detail-refresh 0   # disable the rotation

Network: honors FETCH_PROXY / FETCH_INSECURE_TLS like fetch.py /
import_external.py (curl_cffi Chrome impersonation passes DCM's Vercel bot
checkpoint; plain httpx gets 429).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import import_external as ie
import projects

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

# Listing-summary fields tracked for change detection (stored in dcm_sync).
SUMMARY_FIELDS = ("name", "operator", "city", "category")


# ------------------------------------------------------------------- scrape

def scrape_listings(fetcher: ie.PoliteFetcher) -> tuple[list[dict], list[str]]:
    """All in-scope DCM facility/campus listings with summary fields.

    Same page walk as import_external._dcm_country (country -> market ->
    mapdata.dcs), but detail pages are NOT fetched here and the DCM-internal
    id/url/category are kept for matching and selective detail fetches.
    Returns (rows, errors); a throttled country is skipped with an error
    entry, its facilities simply show up as "unchanged" this run.

    Raises RuntimeError when the scrape looks truncated: DCM sometimes
    serves bot-variant pages that parse fine but carry empty/partial
    mapdata.dcs (see import_external), and syncing against a partial
    listing would silently skip change detection for most facilities."""
    rows: list[dict] = []
    errors: list[str] = []
    seen_ids: set[int] = set()
    seen_urls: set[str] = set()
    watchdog = [0]  # consecutive failures across countries
    expected = 0  # facilities advertised on the country maps
    aborted = False
    for cslug, (region, country) in ie.DCM_COUNTRIES.items():
        if aborted:
            break
        try:
            data = ie._dcm_next_data(fetcher.get(f"{ie.DCM_BASE}/{cslug}/"))
            if data is None:
                raise ie.TransientHTTPError(f"country page {cslug}: no __NEXT_DATA__")
            geos = data.get("props", {}).get("pageProps", {}) \
                .get("mapdata", {}).get("geos") or []
            markets = [(g["properties"]["link"], g["properties"]["name"])
                       for g in geos if g.get("properties", {}).get("link")]
            expected += sum(g.get("properties", {}).get("datacenters") or 0
                            for g in geos)
        except Exception as exc:  # noqa: BLE001 - whole country unreachable
            errors.append(f"{cslug}: country page failed: {exc}")
            watchdog[0] += 1
            if watchdog[0] >= 15:
                errors.append("aborted: 15 consecutive failures (throttled?)")
                break
            continue
        log.info("datacentermap/%s: %d markets", cslug, len(markets))
        for mlink, mname in markets:
            try:
                cdata = ie._dcm_next_data(
                    fetcher.get(f"{ie.DCM_BASE}/{cslug}/{mlink}/"))
                if cdata is None:
                    raise ValueError("no __NEXT_DATA__ in market page")
                watchdog[0] = 0
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{cslug}/{mlink}: market page failed: {exc}")
                watchdog[0] += 1
                if watchdog[0] >= 15:
                    errors.append("aborted: 15 consecutive failures (throttled?)")
                    aborted = True
                    break
                continue
            dcs = cdata.get("props", {}).get("pageProps", {}) \
                .get("mapdata", {}).get("dcs") or []
            for feat in dcs:
                p = feat.get("properties", {})
                if p.get("listingtype") not in ("Facility", "Campus"):
                    continue
                if (p.get("country") or "") != country:
                    continue  # cross-border neighbor shown on this market map
                dcm_id = p.get("id")
                url = p.get("url") or ""
                name = ie._clean_ws(p.get("name") or "")
                operator = ie._clean_ws(p.get("companyname") or "")
                if not (isinstance(dcm_id, int) and url and name and operator):
                    continue
                if dcm_id in seen_ids or url in seen_urls:
                    continue  # same facility listed in a neighboring market
                seen_ids.add(dcm_id)
                seen_urls.add(url)
                coords = (feat.get("geometry") or {}).get("coordinates") or []
                lon = (round(coords[0], 5) if len(coords) >= 2
                       and isinstance(coords[0], (int, float)) else None)
                lat = (round(coords[1], 5) if len(coords) >= 2
                       and isinstance(coords[1], (int, float)) else None)
                rows.append({
                    "dcm_id": dcm_id,
                    "url": url,
                    "name": name,
                    "operator": operator,
                    "region": region,
                    "country": country,
                    # City stays the MARKET name, matching the import (and
                    # therefore the existing DB entries).
                    "city": ie._clean_ws(mname),
                    "category": ie._clean_ws(p.get("capacitytype") or "") or None,
                    "lat": lat,
                    "lon": lon,
                })
    log.info("datacentermap: %d listings scraped (%d requests, %d errors)",
             len(rows), fetcher.requests, len(errors))
    if expected and len(rows) < 0.6 * expected:
        raise RuntimeError(
            f"scraped {len(rows)} listings but country maps advertise "
            f"{expected} — likely bot-variant pages; aborting without "
            "touching the DB, re-run later")
    return rows, errors


# ------------------------------------------------------------------ matching

def _summary(row: dict) -> dict:
    """The listing-summary snapshot stored on the project as ``dcm_sync``;
    next run's diff compares DCM's new listing against THIS (source-vs-source,
    so name spelling differences vs. news-created projects never trigger)."""
    return {k: row.get(k) or None for k in SUMMARY_FIELDS}


def _summary_diff(project: dict, row: dict) -> dict:
    old = project.get("dcm_sync") or {}
    new = _summary(row)
    return {k: [old.get(k), new[k]] for k in SUMMARY_FIELDS
            if (old.get(k) or None) != new[k]}


def _op_index(plist: list[dict]) -> dict[str, list[dict]]:
    idx: dict[str, list[dict]] = {}
    for p in plist:
        op = projects._canon(projects.canonical_operator(p.get("operator", "")))
        idx.setdefault(op, []).append(p)
    return idx


def _match_project(row: dict, op_index: dict[str, list[dict]]
                   ) -> tuple[dict | None, int]:
    """First-link matching for listings without a stored dcm_id: the same
    strict rules as import_external.merge_updates (same canonical operator,
    subset name match with identical digit tokens, compatible cities).

    Returns (match, candidate_count). match=None with candidate_count >= 2
    means AMBIGUOUS — the caller must flag and skip the listing, never
    create a would-be duplicate. Candidates already linked to a dcm_id stay
    in the pool: when the best match turns out to be linked to a DIFFERENT
    DCM id, the listing is a probable duplicate on DCM's side (renamed
    operator listing kept next to the old one) and the caller flags it
    instead of creating a duplicate project."""
    op = projects._canon(projects.canonical_operator(row["operator"]))
    cands = [p for p in op_index.get(op, [])
             if ie._city_compatible(p.get("city", ""), row["city"])
             and ie._import_name_match(p.get("name", ""), row["name"])]
    if len(cands) == 1:
        return cands[0], 1
    exact = [p for p in cands
             if ie._strong_name_match(p.get("name", ""), row["name"])]
    if len(exact) == 1:
        return exact[0], len(cands)
    if len(exact) > 1:
        # The DB can hold duplicate entries for one facility (e.g. a baxtel
        # and a datacentermap copy); prefer the DCM-sourced one.
        dcm = [p for p in exact if p.get("source") == "datacentermap"]
        if len(dcm) == 1:
            return dcm[0], len(cands)
    return None, len(cands)


# -------------------------------------------------------------------- apply

def _apply_detail(project: dict, row: dict, detail: dict, diff: dict,
                  today: str, flags: list[dict]) -> dict:
    """Apply a re-fetched listing + detail page to an existing project.
    Returns the {field: [old, new]} map of changes actually applied."""
    fields: dict[str, list] = {}
    # Identity fields from the listing summary. The project id (URL slug)
    # never changes; operator changes are flagged, not applied — they usually
    # mean an ownership change that deserves a human look.
    if "name" in diff and row["name"] != project.get("name"):
        fields["name"] = [project.get("name"), row["name"]]
        project["name"] = row["name"]
    if "city" in diff and row["city"] != project.get("city"):
        fields["city"] = [project.get("city"), row["city"]]
        project["city"] = row["city"]
    if "operator" in diff:
        if projects._canon(projects.canonical_operator(row["operator"])) \
                != projects._canon(project.get("operator", "")):
            flags.append({
                "id": project["id"], "name": project.get("name"),
                "field": "operator",
                "old": project.get("operator"), "new": row["operator"],
                "note": "operator change not applied automatically",
            })
    category = detail.get("category") or row.get("category")
    if category and category != project.get("category"):
        fields["category"] = [project.get("category"), category]
        project["category"] = category
    # Status from the detail page: never regresses (same policy as news
    # upserts); a real change is recorded with source="directory_sync" and
    # NO story_id (story_id marks news-driven entries).
    new_status = detail.get("status")
    if new_status in projects.STATUSES:
        old_status = project.get("status", "announced")
        resolved = projects._next_status(old_status, new_status)
        if resolved != old_status:
            project["status"] = resolved
            project.setdefault("status_history", []).append(
                {"status": resolved, "date": today, "source": "directory_sync"})
            fields["status"] = [old_status, resolved]
    # Capacity only grows (a phase figure must not overwrite a larger
    # total-planned one) — same policy as projects._apply_update.
    cap = detail.get("capacity_mw")
    existing_cap = project.get("capacity_mw")
    if (isinstance(cap, (int, float)) and cap != existing_cap
            and (existing_cap is None or cap > existing_cap)):
        fields["capacity_mw"] = [existing_cap, cap]
        project["capacity_mw"] = cap
        if detail.get("capacity_note"):
            project["capacity_note"] = detail["capacity_note"]
    for f in ("rfs", "year_built", "description"):
        val = detail.get(f)
        if val and val != project.get(f):
            fields[f] = [project.get(f), val]
            project[f] = val
    # Spec sheet: merge key-by-key (new values win), without a diff entry —
    # too granular for the summary.
    specs = detail.get("specs")
    if specs:
        target = project.setdefault("specs", {})
        for k, v in specs.items():
            if target.get(k) != v:
                target[k] = v
    # Coordinates fill only when missing (exact pins must not churn).
    for f in ("lat", "lon"):
        if row.get(f) is not None and project.get(f) is None:
            project[f] = row[f]
    if fields:
        # A real, detected change — not a bulk stamp; unchanged projects are
        # never touched.
        project["last_updated"] = today
    return fields


def _create_project(row: dict, detail: dict, today: str,
                    plist: list[dict], taken_ids: set[str]) -> dict | None:
    """Create a project entry for a newly listed DCM facility, following the
    import's conventions (seed=true, source=datacentermap, first_seen=today).
    The initial status_history entry carries source="directory_sync" so the
    weekly report can list it as a first-tracked milestone."""
    raw = {
        "story_id": "",
        "name": row["name"],
        "operator": row["operator"],
        "region": row["region"],
        "country": row["country"],
        "city": row["city"],
        "status": detail.get("status") or "operational",
        "capacity_mw": detail.get("capacity_mw"),
        "capacity_note": detail.get("capacity_note"),
        "rfs": detail.get("rfs"),
        "year_built": detail.get("year_built"),
        "description": detail.get("description"),
        "category": detail.get("category") or row.get("category"),
        "specs": detail.get("specs"),
        "lat": row.get("lat"),
        "lon": row.get("lon"),
        "source": "datacentermap",
    }
    upd = projects._validate_update(raw)
    if upd is None:
        log.warning("rejected new listing: %s",
                    {k: raw.get(k) for k in ("name", "operator", "region")})
        return None
    upd["operator"] = projects.canonical_operator(upd["operator"])
    upd["source"] = "datacentermap"
    project = projects._new_project(upd, today, taken_ids)
    project["seed"] = True  # directory listing, not confirmed by tracked news
    project["dcm_id"] = row["dcm_id"]
    project["dcm_sync"] = _summary(row)
    project["status_history"] = [{"status": project["status"], "date": today,
                                  "source": "directory_sync"}]
    plist.append(project)
    return project


# -------------------------------------------------------------------- sync

def sync(fetcher: ie.PoliteFetcher, db: dict, today: str,
         limit: int = 0, detail_refresh: int = 0) -> dict:
    """Run one sync pass against the loaded project DB (mutated in place).
    Returns the summary dict (also written to data/ by the caller)."""
    rows, errors = scrape_listings(fetcher)
    plist = db.setdefault("projects", [])
    taken_ids = {p["id"] for p in plist}
    op_index = _op_index(plist)
    by_dcm_id = {p["dcm_id"]: p for p in plist
                 if isinstance(p.get("dcm_id"), int)}
    # dcm_alt_ids: DCM-side duplicate listings of an already-linked facility
    # (merged-away copies, confirmed double listings). They resolve to the
    # same project but are skipped below — no diff, no flag, no fetch.
    alt_ids: set[int] = set()
    for p in plist:
        for aid in p.get("dcm_alt_ids") or []:
            if isinstance(aid, int) and aid not in by_dcm_id:
                by_dcm_id[aid] = p
                alt_ids.add(aid)
    rows_by_dcm = {r["dcm_id"]: r for r in rows}

    created: list[dict] = []
    changed: list[dict] = []
    flags: list[dict] = []
    unchanged = linked = 0
    pending: list[tuple[dict, dict | None, dict]] = []  # (row, project, diff)
    matched_projects: set[int] = set()  # id() of projects matched this run
    fetched: set[int] = set()  # id() of projects detail-fetched this run

    for row in rows:
        if row["dcm_id"] in alt_ids:
            unchanged += 1  # known duplicate listing on DCM's side
            continue
        project = by_dcm_id.get(row["dcm_id"])
        first_link = False
        if project is None:
            project, n_cands = _match_project(row, op_index)
            if project is not None and project.get("dcm_id") is not None:
                flags.append({"id": project["id"], "name": row["name"],
                              "field": "dcm_id", "old": project["dcm_id"],
                              "new": row["dcm_id"], "url": row["url"],
                              "lat": row.get("lat"), "lon": row.get("lon"),
                              "note": "listing matches a project already "
                                      "linked to another DCM id — possible "
                                      "duplicate listing"})
                continue
            first_link = project is not None
            if project is None and n_cands >= 2:
                flags.append({"id": None, "name": row["name"],
                              "field": "match", "old": None, "new": row["url"],
                              "lat": row.get("lat"), "lon": row.get("lon"),
                              "note": f"ambiguous: {n_cands} candidate "
                                      "projects — not linked, not created"})
                continue
        if project is None:
            pending.append((row, None, {}))  # new listing
            continue
        if id(project) in matched_projects:
            flags.append({"id": project["id"], "name": project.get("name"),
                          "field": "dcm_id", "old": project.get("dcm_id"),
                          "new": row["dcm_id"], "url": row["url"],
                          "lat": row.get("lat"), "lon": row.get("lon"),
                          "note": "second DCM listing matched the same project"})
            continue
        matched_projects.add(id(project))
        if first_link or not project.get("dcm_sync"):
            # First encounter: adopt the current listing as the snapshot —
            # comparing against project fields here would report fuzzy-match
            # spelling differences as fake "changes".
            project["dcm_id"] = row["dcm_id"]
            project["dcm_sync"] = _summary(row)
            linked += 1
            unchanged += 1
            continue
        diff = _summary_diff(project, row)
        if diff:
            pending.append((row, project, diff))
        else:
            unchanged += 1

    # Existing projects with summary changes first (data quality), then new
    # listings; --limit caps the total number of detail-page fetches.
    pending.sort(key=lambda t: t[1] is None)
    deferred = pending[limit:] if limit else []
    todo = pending[:limit] if limit else pending
    if deferred:
        log.info("detail fetch limit %d: %d of %d pending deferred to next run",
                 limit, len(deferred), len(pending))

    for row, project, diff in todo:
        try:
            detail = ie._dcm_detail(fetcher, row["url"])
        except Exception as exc:  # noqa: BLE001 - throttled/checkpoint/parse
            errors.append(f"detail {row['url']}: {exc}")
            if project is None:
                # New listing with no detail: still create from the listing
                # summary (import behavior when details fail); it just has no
                # MW/stage refinement this round.
                detail = {}
            else:
                # Keep the stale dcm_sync so the change is retried next run.
                continue
        if project is None:
            project = _create_project(row, detail, today, plist, taken_ids)
            if project is None:
                errors.append(f"rejected new listing {row['url']}")
                continue
            fetched.add(id(project))
            if detail:
                project["dcm_detail_at"] = today
            op_index.setdefault(projects._canon(project["operator"]),
                                []).append(project)
            created.append({
                "id": project["id"], "name": project["name"],
                "operator": project["operator"], "city": project["city"],
                "country": project["country"], "status": project["status"],
                "capacity_mw": project.get("capacity_mw"),
                "dcm_id": row["dcm_id"],
            })
            log.info("created %s: %s [%s]", project["id"], row["name"],
                     row["url"])
        else:
            fetched.add(id(project))
            project["dcm_detail_at"] = today
            fields = _apply_detail(project, row, detail, diff, today, flags)
            project["dcm_sync"] = _summary(row)  # change handled; re-baseline
            if fields:
                changed.append({"id": project["id"], "name": project["name"],
                                "dcm_id": row["dcm_id"], "fields": fields})
                log.info("changed %s: %s", project["id"],
                         {k: f"{v[0]} -> {v[1]}" for k, v in fields.items()})
            else:
                unchanged += 1

    # Rotating detail refresh: the listing summary carries no stage/MW, so a
    # pure status flip never shows up in the diff above. Re-fetch the detail
    # pages of the N DCM-linked projects with the oldest dcm_detail_at
    # (never-fetched first), skipping anything already fetched this run.
    detail_checked = 0
    detail_changed: list[str] = []
    if detail_refresh:
        rotation = []
        for p in plist:
            d = p.get("dcm_id")
            if (not isinstance(d, int) or d not in rows_by_dcm
                    or id(p) in fetched):
                continue
            rotation.append((p.get("dcm_detail_at") or "", p["id"], p))
        rotation.sort(key=lambda t: (t[0], t[1]))
        for _, _, project in rotation[:detail_refresh]:
            row = rows_by_dcm[project["dcm_id"]]
            try:
                detail = ie._dcm_detail(fetcher, row["url"])
            except Exception as exc:  # noqa: BLE001
                errors.append(f"detail-refresh {row['url']}: {exc}")
                continue  # no timestamp: retried early next run
            detail_checked += 1
            fetched.add(id(project))
            project["dcm_detail_at"] = today
            fields = _apply_detail(project, row, detail, {}, today, flags)
            if fields:
                detail_changed.append(project["id"])
                changed.append({"id": project["id"], "name": project["name"],
                                "dcm_id": row["dcm_id"], "fields": fields,
                                "via": "detail_refresh"})
                log.info("detail-refresh changed %s: %s", project["id"],
                         {k: f"{v[0]} -> {v[1]}" for k, v in fields.items()})

    return {
        "date": today,
        "source": "datacentermap",
        "listings": len(rows),
        "created": created,
        "changed": changed,
        "unchanged_count": unchanged,
        "linked_count": linked,
        "deferred_count": len(deferred),
        "detail_refresh": {"checked": detail_checked,
                           "changed": len(detail_changed),
                           "changed_ids": detail_changed},
        "flags": flags,
        "errors": errors,
        "requests": fetcher.requests,
    }


def print_summary(summary: dict, dry_run: bool) -> None:
    print(f"\n==== directory sync {summary['date']} (datacentermap) ====")
    print(f"listings scraped : {summary['listings']} "
          f"({summary['requests']} HTTP requests)")
    print(f"unchanged        : {summary['unchanged_count']} "
          f"(first-time dcm_id links: {summary['linked_count']})")
    print(f"created          : {len(summary['created'])}")
    for c in summary["created"][:20]:
        print(f"  + {c['name']} ({c['operator']}, {c['city']}) "
              f"[{c['status']}]")
    if len(summary["created"]) > 20:
        print(f"  ... and {len(summary['created']) - 20} more")
    print(f"changed          : {len(summary['changed'])}")
    for c in summary["changed"][:20]:
        diffs = "; ".join(f"{k}: {v[0]} -> {v[1]}"
                          for k, v in c["fields"].items())
        via = " [detail refresh]" if c.get("via") == "detail_refresh" else ""
        print(f"  ~ {c['name']}{via}: {diffs}")
    if len(summary["changed"]) > 20:
        print(f"  ... and {len(summary['changed']) - 20} more")
    dr = summary.get("detail_refresh")
    if dr:
        print(f"detail refresh   : {dr['checked']} checked, "
              f"{dr['changed']} changed")
    if summary["deferred_count"]:
        print(f"deferred         : {summary['deferred_count']} "
              "(detail limit reached; next run retries)")
    if summary["flags"]:
        print(f"flags            : {len(summary['flags'])}")
        for f in summary["flags"][:10]:
            print(f"  ! {f['name']}: {f['field']} {f.get('old')} -> "
                  f"{f.get('new')} ({f['note']})")
    if summary["errors"]:
        print(f"errors           : {len(summary['errors'])}")
        for e in summary["errors"][:10]:
            print(f"  x {e}")
    if dry_run:
        print("dry-run: projects.json NOT written, no summary file saved")


def run_sync(limit: int = 0, dry_run: bool = False, delay: float = 1.2,
             detail_refresh: int | None = None,
             path: Path = projects.PROJECTS_PATH) -> dict:
    """Programmatic entry (used by run.py's weekly hook and the CLI)."""
    started = time.monotonic()
    today = datetime.now(timezone.utc).date().isoformat()
    if detail_refresh is None:
        detail_refresh = int(os.environ.get("SYNC_DETAIL_REFRESH", "150"))
    session = ie.make_session()
    fetcher = ie.PoliteFetcher(session, delay=delay)
    db = projects.load_projects(path)
    summary = sync(fetcher, db, today, limit=limit,
                   detail_refresh=detail_refresh)
    summary["duration_s"] = round(time.monotonic() - started, 1)
    print_summary(summary, dry_run)
    if not dry_run:
        projects.save_projects(db, path)
        out = DATA_DIR / f"directory-sync-{today}.json"
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        log.info("wrote %s and updated %s", out.name, path.name)
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="scrape and report, do not write projects.json or "
                         "the summary file")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap detail-page fetches this run (0 = no limit); "
                         "deferred listings are retried next run")
    ap.add_argument("--delay", type=float, default=1.2,
                    help="min seconds between HTTP requests (default 1.2)")
    ap.add_argument("--detail-refresh", type=int, default=None,
                    help="detail pages re-fetched for the stalest DCM-linked "
                         "projects (default: env SYNC_DETAIL_REFRESH or 150; "
                         "0 disables the rotation)")
    ap.add_argument("--path", type=Path, default=projects.PROJECTS_PATH)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run_sync(limit=args.limit, dry_run=args.dry_run, delay=args.delay,
             detail_refresh=args.detail_refresh, path=args.path)


if __name__ == "__main__":
    main()
