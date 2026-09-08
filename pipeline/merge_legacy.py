"""Merge legacy no-source seed entries into their directory-sourced twins.

The ~40 oldest seed entries (source=null, hand-curated before the directory
imports) duplicate Baxtel/DCM facilities under slightly different names
("DayOne Nusajaya Data Center Campus" vs DCM "DayOne Nusajaya Tech Park
Campus"). The directory entry is always richer, so the legacy entry folds
into it: capacity follows the only-grow rule, text fields fill gaps, news
linkage unions, and the legacy entry is removed.

Matching reuses the loose-merge rules (subset / digit / site-token / internal
-code) against ALL sourced entries, plus a MANUAL_MERGE list of pairs
verified by hand that no safe rule catches (city names disagree across
metros, e.g. "Saitama" vs "Tokyo").

    python merge_legacy.py            # dry-run, report only
    python merge_legacy.py --apply    # backup + rewrite projects.json
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
from merge_baxtel_loose import site_tokens, digit_tokens, merge_fields

log = logging.getLogger("merge_legacy")

CACHE_DIR = Path(__file__).parent / ".scratch"
BACKUP = CACHE_DIR / "projects.pre-legacy-merge.json"
REPORT = CACHE_DIR / "legacy-merge-report.txt"

# Hand-verified NON-duplicates the auto rules get wrong (the place name sits
# in the city field, blinding site-token checks): Telehouse Tamachi (central
# Tokyo) is NOT Telehouse Tama (western suburb).
MANUAL_EXCLUDE = {
    ("KDDI Telehouse TOKYO Tamachi 2", "kddi-telehouse-tokyo-telehouse-tokyo-tama-2"),
}

# Hand-verified duplicates no safe rule catches (city/metro naming differs).
# legacy id -> target id
MANUAL_MERGE = {
    # DayOne (GDS intl) Nusajaya campus: 168 MW total planned (legacy) vs
    # DCM phase-1 69.5 MW operational — same Tech Park campus.
    "nusajaya-iskandar-puteri-johor-dayone-nusajaya-data-center-campus":
        "johor-bahru-dayone-nusajaya-tech-park-campus",
    # YTL Green DC Park (Kulai) == DCM YTL Johor Data Center Park.
    # (id resolved at runtime by name below)
    # PDG TY1 is in Saitama (Tokyo metro); DCM lists it under "Tokyo".
    # NTT Shiroi is Chiba (Tokyo metro); DCM lists it under "Tokyo".
    # Samsung SDS Sangam, LG CNS Busan, Digital Seoul 1, Empyrion Gangnam:
    # same facility, marketing-name variants.
}
MANUAL_MERGE_BY_NAME = {
    "YTL Green Data Center Park": "YTL Johor Data Center Park",
    "Princeton Digital Group TY1": "Princeton Digital TY1",
    "NTT Global Data Centers Shiroi 1 Data Center": "GDC - Shiroi 1 Data Centers",
    "Samsung SDS Sangam Data Center": "Samsung SDS: Sangam ICT",
    "LG CNS Busan Global Data Center": "LG CNS Busan Global Cloud DC",
    "Digital Realty Digital Seoul 1": "Digital Seoul 1 (ICN10)",
    "Empyrion DC Seoul (Gangnam)": "Empyrion: Gangnam KR1",
    "China Unicom Hohhot Cloud Computing Base": "China Unicom Hohhot Yun",
    "STT Bangkok One": "STT Bangkok 1",
}


def auto_match(legacy: dict, pool: list[dict]) -> dict | None:
    op = projects._canon(projects.canonical_operator(legacy.get("operator", "")))
    cands = [d for d in pool
             if projects._canon(projects.canonical_operator(d.get("operator", ""))) == op
             and projects._canon(d.get("country", "")) == projects._canon(legacy.get("country", ""))
             and ie._city_compatible(d.get("city", ""), legacy.get("city", ""))]
    if not cands:
        return None
    # R0: strict subset name match (aliases added after the directory import
    # unblock pairs the import itself missed, e.g. China Mobile Hohhot).
    strict = [d for d in cands
              if ie._import_name_match(d.get("name", ""), legacy["name"])]
    if len(strict) == 1:
        return strict[0]
    if len(strict) > 1:
        exact = [d for d in strict
                 if ie._strong_name_match(d.get("name", ""), legacy["name"])]
        if len(exact) == 1:
            return exact[0]
        return None
    # R1/R4: digit-token equality, or shared digit + site-token containment.
    lx_digits = digit_tokens(legacy["name"])
    lx_site = site_tokens(legacy["name"], legacy.get("operator", ""),
                          legacy.get("city", ""), legacy.get("country", ""))
    lx_site_nd = {t for t in lx_site if not any(c.isdigit() for c in t)}
    hits = []
    for d in cands:
        d_digits = digit_tokens(d["name"])
        d_site_nd = {t for t in site_tokens(d["name"], d.get("operator", ""),
                                            d.get("city", ""), d.get("country", ""))
                     if not any(c.isdigit() for c in t)}
        if lx_digits and lx_digits == d_digits:
            if not (lx_site_nd and d_site_nd and not (lx_site_nd & d_site_nd)):
                hits.append(d)
        elif lx_digits and (lx_digits & d_digits):
            if (d_site_nd and d_site_nd <= lx_site_nd) or \
                    (lx_site_nd and lx_site_nd <= d_site_nd):
                hits.append(d)
    return hits[0] if len(hits) == 1 else None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="backup + rewrite projects.json (default: dry-run)")
    ap.add_argument("--path", type=Path, default=projects.PROJECTS_PATH)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    db = projects.load_projects(args.path)
    plist = db["projects"]
    by_id = {p["id"]: p for p in plist}
    by_name = {p["name"]: p for p in plist}
    legacy = [p for p in plist if not p.get("source")]
    pool = [p for p in plist if p.get("source")]
    log.info("%d legacy entries, %d sourced", len(legacy), len(pool))

    merged, unmatched = [], []
    for leg in legacy:
        target = by_id.get(MANUAL_MERGE.get(leg["id"], ""))
        how = "manual"
        if target is None and leg["name"] in MANUAL_MERGE_BY_NAME:
            target = by_name.get(MANUAL_MERGE_BY_NAME[leg["name"]])
        if target is None:
            target = auto_match(leg, pool)
            how = "auto"
            if target is not None and (leg["name"], target["id"]) in MANUAL_EXCLUDE:
                target = None
        if target is None or target["id"] == leg["id"]:
            unmatched.append(leg)
            continue
        merge_fields(target, leg)
        merged.append((leg, target, how))
        leg["_folded"] = target["id"]

    folded_ids = {leg["id"] for leg, _, _ in merged}
    keep = [p for p in plist if p["id"] not in folded_ids]

    lines = []
    out = lines.append
    out(f"legacy-merge report — {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")
    out(f"total {len(plist)} -> keep {len(keep)} "
        f"(merged {len(merged)}, unmatched kept {len(unmatched)})")
    out("\n== merged (legacy removed) ==")
    for leg, target, how in merged:
        out(f"  [{how}] {leg['name']} ({leg.get('city')})  ->  "
            f"{target['name']} [{target['id']}]")
    out("\n== unmatched (kept) ==")
    for leg in unmatched:
        out(f"  {leg['name']}  | {leg.get('operator')} | {leg.get('city')}, "
            f"{leg.get('country')} | {leg.get('capacity_mw') or '?'} MW")
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:5]))
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
