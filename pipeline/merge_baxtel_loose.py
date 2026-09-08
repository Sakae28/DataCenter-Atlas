"""Loose-merge Baxtel-only project entries into DataCenterMap-covered ones.

Background: the strict directory merge (import_external.merge_updates) misses
cross-source duplicates when the two directories name the same facility
differently ("PDG JH1" vs "Princeton Digital JH1") or use different
granularity (Baxtel campus-level "STT Johor Campus" vs DCM building-level
"STT Johor 1/2/3"). DataCenterMap is the richer/primary source, so this
script folds leftover Baxtel-only entries into DCM-covered projects:

  R1  same canonical operator + compatible city + identical non-empty digit
      tokens (e.g. "jh1")            -> 1:1 merge, DCM fields win conflicts
  R2  same operator + city, neither name has digit tokens, and >= 2 shared
      distinctive tokens ("google jurong west")  -> 1:1 merge
  R3  Baxtel campus-level entry (no digit tokens, contains "campus"/"park"
      or its tokens are covered by the DCM group) with same operator + city
      -> redundant, dropped (the DCM buildings cover it)

Anything else is left untouched and reported as genuinely Baxtel-unique.

    python merge_baxtel_loose.py            # dry-run, report only
    python merge_baxtel_loose.py --apply    # backup + rewrite projects.json
"""

import argparse
import json
import logging
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import projects
import import_external as ie

log = logging.getLogger("merge_baxtel_loose")

DCM_CACHE = Path(__file__).parent / ".scratch" / "import_cache_datacentermap.json"
BACKUP = Path(__file__).parent / ".scratch" / "projects.pre-loose-merge.json"
REPORT = Path(__file__).parent / ".scratch" / "loose-merge-report.txt"

# Words that carry no identity in a facility name.
STOP_TOKENS = {"data", "center", "centre", "dc", "datacenter", "datacentre",
               "campus", "park"}


# Manually reviewed false-positive pairs (bx name, dcm project id): adjacent
# place tokens in the Baxtel name contradict the DCM site but share brand
# tokens, so no automatic rule catches them without catching true positives
# (e.g. "Fo Tank" = "Fotan") as well.
MANUAL_EXCLUDE = {
    ("PLDT VITRO: Sta. Rosa 2", "manila-epldt-vitro-makati-2"),
}


def digit_tokens(name: str) -> set[str]:
    return ie._digit_tokens(name)


def distinctive(name: str) -> set[str]:
    return {t for t in projects._name_tokens(name) if t not in STOP_TOKENS}


def site_tokens(name: str, operator: str, *geo: str) -> set[str]:
    """Distinctive name tokens that identify the SITE itself — operator-name
    tokens ("telehouse") and geography tokens ("osaka", "japan") are stripped,
    so a match requires a shared site-level identifier ("jurong west")."""
    exclude = set()
    for text in (operator, *geo):
        exclude |= projects._name_tokens(text or "")
    return distinctive(name) - exclude


def dcm_covered_ids(db_projects: list[dict], dcm_updates: list[dict]) -> set[str]:
    """Ids of DB projects matched by the strict DCM import merge."""
    covered = set()
    for raw in dcm_updates:
        upd = projects._validate_update(raw)
        if upd is None:
            continue
        upd["operator"] = projects.canonical_operator(upd["operator"])
        op = projects._canon(upd["operator"])
        # Canonicalize the DB side too: entries created before an alias
        # existed still store the raw subsidiary name.
        matches = [p for p in db_projects
                   if projects._canon(projects.canonical_operator(p.get("operator", ""))) == op
                   and ie._import_name_match(p.get("name", ""), upd["name"])
                   and ie._city_compatible(p.get("city", ""), upd["city"])]
        hit = None
        if len(matches) == 1:
            hit = matches[0]
        elif len(matches) > 1:
            exact = [p for p in matches
                     if ie._strong_name_match(p.get("name", ""), upd["name"])]
            if len(exact) == 1:
                hit = exact[0]
        if hit:
            covered.add(hit["id"])
    return covered


def merge_fields(dcm: dict, bx: dict) -> list[str]:
    """Fill DCM project's MISSING fields from the Baxtel entry; DCM values
    always win conflicts (DCM-primary). Capacity follows the usual only-grow
    rule. News linkage is unioned. Returns the list of fields touched."""
    touched = []
    for field in ("lat", "lon", "description", "category", "company_type",
                  "year_built", "stage_detail", "investment", "investor",
                  "developer", "phases", "power", "announced",
                  "construction_start", "rfs", "anchor_tenants"):
        if dcm.get(field) is None and bx.get(field) is not None:
            dcm[field] = bx[field]
            touched.append(field)
    cap, existing = bx.get("capacity_mw"), dcm.get("capacity_mw")
    if isinstance(cap, (int, float)) and (existing is None or cap > existing):
        dcm["capacity_mw"] = cap
        if bx.get("capacity_note"):
            dcm["capacity_note"] = bx["capacity_note"]
        touched.append("capacity_mw")
    if bx.get("expansion_planned") and not dcm.get("expansion_planned"):
        dcm["expansion_planned"] = True
        touched.append("expansion_planned")
    if bx.get("specs"):
        specs = dcm.setdefault("specs", {})
        for k, v in bx["specs"].items():
            if k not in specs:
                specs[k] = v
                touched.append(f"specs.{k}")
    for field in ("story_ids", "status_history"):
        merged = list(dcm.get(field) or [])
        for item in bx.get(field) or []:
            if item not in merged:
                merged.append(item)
                touched.append(field)
        if merged:
            dcm[field] = merged
    # DCM linkage: the loser's dcm_id must survive as an alias on the
    # survivor — the facility is still listed on DCM under that id, and
    # without the alias the next directory sync would re-create the loser
    # (sync_directories skips listings in dcm_alt_ids as known duplicates).
    loser_dcm = bx.get("dcm_id")
    if isinstance(loser_dcm, int) and loser_dcm != dcm.get("dcm_id"):
        alts = dcm.setdefault("dcm_alt_ids", [])
        if loser_dcm not in alts:
            alts.append(loser_dcm)
            touched.append("dcm_alt_ids")
    for aid in bx.get("dcm_alt_ids") or []:
        if isinstance(aid, int) and aid != dcm.get("dcm_id"):
            alts = dcm.setdefault("dcm_alt_ids", [])
            if aid not in alts:
                alts.append(aid)
                touched.append("dcm_alt_ids")
    if dcm.get("dcm_alt_ids"):
        dcm["dcm_alt_ids"] = sorted(dcm["dcm_alt_ids"])
    return touched


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="backup + rewrite projects.json (default: dry-run)")
    ap.add_argument("--path", type=Path, default=projects.PROJECTS_PATH)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    db = projects.load_projects(args.path)
    plist = db["projects"]
    dcm_updates = [u for ups in json.loads(
        DCM_CACHE.read_text(encoding="utf-8")).values() for u in ups]
    covered = dcm_covered_ids(plist, dcm_updates)
    by_id = {p["id"]: p for p in plist}

    baxtel_only = [p for p in plist
                   if p["id"] not in covered and p.get("source") == "baxtel"]
    dcm_pool = [p for p in plist if p["id"] in covered]
    log.info("total %d | DCM-covered %d | baxtel-only %d",
             len(plist), len(dcm_pool), len(baxtel_only))

    merged_r1, merged_r2, merged_r4, review_r3, unique = [], [], [], [], []
    for bx in baxtel_only:
        op = projects._canon(projects.canonical_operator(bx.get("operator", "")))
        cands = [d for d in dcm_pool
                 if projects._canon(projects.canonical_operator(d.get("operator", ""))) == op
                 and projects._canon(d.get("country", "")) == projects._canon(bx.get("country", ""))
                 and ie._city_compatible(d.get("city", ""), bx.get("city", ""))]
        if not cands:
            unique.append((bx, "no same-operator DCM facility in the city"))
            continue
        bx_digits = digit_tokens(bx["name"])
        hit = None
        if bx_digits:
            bx_site = site_tokens(bx["name"], bx.get("operator", ""),
                                  bx.get("city", ""), bx.get("country", ""))
            bx_site_nd = {t for t in bx_site if not any(c.isdigit() for c in t)}
            r1 = []
            for d in cands:
                if digit_tokens(d["name"]) != bx_digits:
                    continue
                # Same digits is not enough when BOTH names carry site
                # identifiers and they disagree ("Tai Seng 2" vs "Defu 2",
                # "SYD1 East" vs "SYD1 West"). One side being bare (just
                # operator + number) never contradicts the other.
                d_site_nd = {t for t in site_tokens(d["name"], d.get("operator", ""),
                                                    d.get("city", ""), d.get("country", ""))
                             if not any(c.isdigit() for c in t)}
                if bx_site_nd and d_site_nd and not (bx_site_nd & d_site_nd):
                    continue
                # Baxtel city empty -> the metro gate could not constrain the
                # match; then the DCM entry's city must not contradict the
                # Baxtel name ("Sta. Rosa 2" is not "Makati 2").
                if not (bx.get("city") or "").strip():
                    d_city = {t for t in projects._name_tokens(d.get("city", ""))
                              if t not in STOP_TOKENS}
                    if d_city and bx_site_nd and not (d_city & bx_site_nd):
                        continue
                r1.append(d)
            if len(r1) == 1 and (bx["name"], r1[0]["id"]) not in MANUAL_EXCLUDE:
                hit = r1[0]
                merged_r1.append((bx, hit))
            elif not r1:
                # R4: operator-internal codes ("NTT OS2 (Telepark Dojima
                # Building 2)") make digit sets differ from the DCM name
                # ("NTT Telepark Dojima Building 2"). Merge when at least one
                # digit is shared AND one side's non-digit site identity fully
                # contains the other's.
                r4 = []
                for d in cands:
                    if not (bx_digits & digit_tokens(d["name"])):
                        continue
                    d_site_nd = {t for t in site_tokens(d["name"], d.get("operator", ""),
                                                        d.get("city", ""), d.get("country", ""))
                                 if not any(c.isdigit() for c in t)}
                    if (d_site_nd and d_site_nd <= bx_site_nd) or \
                            (bx_site_nd and bx_site_nd <= d_site_nd):
                        r4.append(d)
                if len(r4) == 1 and (bx["name"], r4[0]["id"]) not in MANUAL_EXCLUDE:
                    hit = r4[0]
                    merged_r4.append((bx, hit))
                else:
                    unique.append((bx, "no DCM entry with matching digits"))
            else:
                unique.append((bx, f"ambiguous ({len(r1)} R1 candidates)"))
        else:
            bx_site = site_tokens(bx["name"], bx.get("operator", ""),
                                  bx.get("city", ""), bx.get("country", ""))
            r2 = [d for d in cands
                  if not digit_tokens(d["name"])
                  and len(bx_site & site_tokens(d["name"], d.get("operator", ""),
                                                d.get("city", ""), d.get("country", ""))) >= 2]
            if len(r2) == 1:
                hit = r2[0]
                merged_r2.append((bx, hit))
            elif not r2:
                # Campus-level Baxtel entry whose site identity is fully
                # covered by the DCM group. REPORTED for manual review but
                # KEPT — containment alone cannot tell "campus dupe" from
                # "different building of the same complex" (Hoa Lac vs
                # Hoa Lac 2), so these are never auto-dropped.
                union = set().union(*(site_tokens(d["name"], d.get("operator", ""),
                                                  d.get("city", ""), d.get("country", ""))
                                      for d in cands))
                if bx_site and bx_site <= union:
                    review_r3.append((bx, cands))
                    continue
                unique.append((bx, "name does not match any DCM entry"))
                continue
            else:
                unique.append((bx, f"ambiguous ({len(r2)} R2 candidates)"))
                continue
        if hit is not None:
            merge_fields(hit, bx)
            hit["_absorbs"] = bx["id"]  # marker, removed before save
            bx["_merged_into"] = hit["id"]

    absorbed = {bx["id"] for bx, _ in merged_r1 + merged_r2 + merged_r4}
    keep = [p for p in plist if p["id"] not in absorbed]

    lines = []
    out = lines.append
    out(f"loose-merge report — {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")
    out(f"total {len(plist)} -> keep {len(keep)} "
        f"(R1 merged {len(merged_r1)}, R2 merged {len(merged_r2)}, "
        f"R4 internal-code merged {len(merged_r4)}, "
        f"R3 campus-review {len(review_r3)}, unique kept {len(unique)})")
    out("\n== R1: same digits, renamed (1:1) ==")
    for bx, d in merged_r1:
        out(f"  {bx['name']}  ->  {d['name']}  [{d['id']}]")
    out("\n== R2: no digits, shared site tokens (1:1) ==")
    for bx, d in merged_r2:
        out(f"  {bx['name']}  ->  {d['name']}  [{d['id']}]")
    out("\n== R4: operator-internal code, site identity contained (1:1) ==")
    for bx, d in merged_r4:
        out(f"  {bx['name']}  ->  {d['name']}  [{d['id']}]")
    out("\n== R3: probable campus dupes (KEPT, review manually) ==")
    for bx, cands in review_r3:
        out(f"  {bx['name']}  ({bx.get('city')})  ~  "
            f"{len(cands)} DCM entries e.g. {cands[0]['name']}")
    out("\n== unique: genuinely Baxtel-only (kept) ==")
    for bx, why in sorted(unique, key=lambda t: -(t[0].get("capacity_mw") or 0)):
        out(f"  {bx['name']}  | {bx.get('operator')} | {bx.get('city')}, "
            f"{bx.get('country')} | {bx.get('capacity_mw') or '?'} MW | {why}")
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:6]))
    print(f"report: {REPORT}")

    if not args.apply:
        print("dry-run: projects.json NOT written")
        return
    shutil.copy(args.path, BACKUP)
    for p in keep:
        p.pop("_absorbs", None)
    db["projects"] = keep
    projects.save_projects(db, args.path)
    print(f"backup: {BACKUP}")
    print(f"written: {args.path} ({len(plist)} -> {len(keep)})")


if __name__ == "__main__":
    sys.exit(main())
