"""Fold Baxtel "leased deployment" entries into their host facilities.

A leased deployment (Baxtel: "This site is leased from <Op> in the <Host>
building") is not a separate data center — it is a tenant renting capacity
inside someone else's facility. Counting it as its own project double-counts
capacity (Equinix OS1's 6 MW + Amazon NRT51's 5 MW are ONE 6 MW building).

For every Baxtel entry whose detail page carries the lease notice (backfilled
by backfill_baxtel_leases.py into the details cache):

  host found in DB   -> add the tenant to the host's ``anchor_tenants``
                        ("AWS (Amazon NRT51, ~5 MW leased)"), transfer news
                        linkage, delete the leased entry
  host not in DB     -> keep the entry but tag it ``leased_from`` so the UI
                        can flag it and capacity tooling can exclude it

    python fold_leased_sites.py            # dry-run, report only
    python fold_leased_sites.py --apply    # backup + rewrite projects.json
"""

import argparse
import json
import logging
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import projects
import import_external as ie

log = logging.getLogger("fold_leased_sites")

CACHE_DIR = Path(__file__).parent / ".scratch"
UPDATES_CACHE = CACHE_DIR / "import_cache_baxtel.json"
DETAILS_CACHE = CACHE_DIR / "import_cache_baxtel_details.json"
BACKUP = CACHE_DIR / "projects.pre-lease-fold.json"
REPORT = CACHE_DIR / "lease-fold-report.txt"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="backup + rewrite projects.json (default: dry-run)")
    ap.add_argument("--path", type=Path, default=projects.PROJECTS_PATH)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    updates = json.loads(UPDATES_CACHE.read_text(encoding="utf-8"))
    details = json.loads(DETAILS_CACHE.read_text(encoding="utf-8"))
    lease_by_slug = {s: e["lease"] for s, e in details.items() if e.get("lease")}
    name_by_slug = {u["_slug"]: u for u in updates if u.get("_slug")}
    log.info("%d leased baxtel sites", len(lease_by_slug))

    db = projects.load_projects(args.path)
    plist = db["projects"]

    # DB baxtel entries by their baxtel slug (slug derived from the name the
    # same way the scraper does it).
    db_by_slug = {}
    for p in plist:
        if p.get("source") == "baxtel":
            db_by_slug.setdefault(ie._baxtel_slug(p["name"]), p)

    folded, kept_leased, skipped = [], [], []
    for slug, lease in lease_by_slug.items():
        entry = db_by_slug.get(slug)
        if entry is None:
            skipped.append((slug, "baxtel entry no longer in DB "
                                 "(absorbed earlier)"))
            continue
        tenant = projects.canonical_operator(entry.get("operator", ""))
        cap = entry.get("capacity_mw")
        tenant_note = f"{tenant} ({entry['name']}" + \
            (f", ~{cap:g} MW leased)" if cap else ", leased)")
        host_upd = name_by_slug.get(lease["slug"])
        host = db_by_slug.get(lease["slug"])
        # Fallback: the host may sit in the DB under a DCM name. Match on the
        # lease notice's own site/operator text (used verbatim when Baxtel
        # does not list the host as its own entry).
        ref = host_upd or {"name": lease.get("site") or "",
                           "operator": lease.get("operator") or "",
                           "city": entry.get("city", "")}
        if host is None and ref["name"]:
            op = projects._canon(projects.canonical_operator(ref["operator"]))
            cands = [p for p in plist
                     if projects._canon(projects.canonical_operator(p.get("operator", ""))) == op
                     and ie._import_name_match(p.get("name", ""), ref["name"])
                     and ie._city_compatible(p.get("city", ""), ref.get("city", ""))]
            if len(cands) == 1:
                host = cands[0]
        if host is not None and host["id"] != entry["id"]:
            # Same operator family ("STT Tokyo 2 leased in STT Tokyo 1&2") is
            # an intra-company building relation, not a tenancy — fold the
            # duplicate without adding a self-referential anchor tenant.
            same_op = projects._canon(projects.canonical_operator(
                host.get("operator", ""))) == projects._canon(tenant)
            if not same_op:
                tenants = host.setdefault("anchor_tenants", [])
                if tenant_note not in tenants:
                    tenants.append(tenant_note)
            for field in ("story_ids", "status_history"):
                merged = list(host.get(field) or [])
                for item in entry.get(field) or []:
                    if item not in merged:
                        merged.append(item)
                if merged:
                    host[field] = merged
            folded.append((entry, host, tenant_note))
            entry["_folded"] = host["id"]
        else:
            # Host not tracked: keep the deployment but flag it so capacity
            # rollups and the UI can treat it as a lease, not a facility.
            entry["leased_from"] = lease.get("site") or lease.get("operator")
            kept_leased.append((entry, lease))

    folded_ids = {e["id"] for e, _, _ in folded}
    keep = [p for p in plist if p["id"] not in folded_ids]

    lines = []
    out = lines.append
    out(f"lease-fold report — {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")
    out(f"total {len(plist)} -> keep {len(keep)} "
        f"(folded {len(folded)}, kept-as-leased {len(kept_leased)}, "
        f"skipped {len(skipped)})")
    out("\n== folded into host (entry removed) ==")
    for entry, host, note in folded:
        out(f"  {entry['name']} ({entry.get('city')})  ->  "
            f"{host['name']} [{host['id']}]  tenant: {note}")
    out("\n== host not tracked (kept, flagged leased_from) ==")
    for entry, lease in kept_leased:
        out(f"  {entry['name']}  | host: {lease.get('site')} "
            f"({lease.get('operator')})")
    if skipped:
        out("\n== skipped ==")
        for slug, why in skipped:
            out(f"  {slug}: {why}")
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:4]))
    print(f"report: {REPORT}")

    if not args.apply:
        print("dry-run: projects.json NOT written")
        return
    shutil.copy(args.path, BACKUP)
    db["projects"] = keep
    projects.save_projects(db, args.path)
    print(f"backup: {BACKUP}")
    print(f"written: {args.path} ({len(plist)} -> {len(keep)})")


if __name__ == "__main__":
    sys.exit(main())
