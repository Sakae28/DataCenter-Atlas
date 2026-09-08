"""Operator-variant cleanup round (2026-08).

Two generalized fixes distilled from user-reported cases:

1. OPERATOR RENAMES — the same company stored under legal-suffix / alternate
   -brand spellings ("iAdvantage Limited" vs "iAdvantage (SUNeVision)",
   "IDC Frontier Inc." vs "IDC Frontier", ~60 groups). The aliases live in
   projects.OPERATOR_ALIASES; this script rewrites the operator field of
   existing rows through canonical_operator().

2. FACILITY MERGES — hand-verified cross-source duplicate pairs that the
   loose merge missed because the two sources named the operator or the
   facility differently. Every pair below was confirmed by shared
   coordinates + matching capacity + description content (see the 2026-08
   coordinate-cluster scan in .scratch/). DCM-primary via merge_fields().
   A merged-away entry's dcm_id survives as a dcm_alt_ids alias on the
   survivor so the weekly directory sync does not re-create the loser.

3. DCM LINKS / ALT IDS — manual dcm_id assignments for listings the strict
   matcher missed (DCM_LINKS), and confirmed DCM-side duplicate listings to
   skip in future syncs (DCM_ALT_IDS). See sync_directories.py.

4. RELINKS / NEW FACILITIES — 2026-08-25 generalized-naming flag review:
   listings named just "Operator + City" that name-match an already-linked
   project but sit 1.8-21.7 km away. Where DCM renamed a listing and
   re-listed the facility the project actually describes under a new id,
   the project follows its identity (DCM_RELINK, with coordinate and
   dcm_sync fixes); the freed old listing and the other genuinely distinct
   facilities get their own entries (NEW_DCM_PROJECTS, same conventions as
   sync_directories._create_project).

Also reassigns the one Baxtel entry whose operator is stale:
"China Mobile: Chon Buri" was listed under Switch (former owner — China
Mobile acquired SUPERNAP Thailand in March 2025).

    python merge_operator_variants.py            # dry-run, report only
    python merge_operator_variants.py --apply    # backup + rewrite
"""

import argparse
import json
import logging
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import projects
from merge_baxtel_loose import merge_fields

log = logging.getLogger("merge_operator_variants")

BACKUP = Path(__file__).parent / ".scratch" / "projects.pre-operator-variants.json"

# (loser name, loser source, survivor name, survivor source). Sources are
# only disambiguators for name resolution, not a match criterion.
PAIRS = [
    # iAdvantage: Baxtel brand names vs DCM "SUNeVision iAdvantage …" names —
    # same buildings (MEGA Gateway 20MW on both, MEGA IDC 180MW on both…).
    ("i-Advantage JUMBO", "baxtel", "SUNeVision iAdvantage JUMBO", "datacentermap"),
    ("iAdvantage MEGA Gateway", "baxtel", "SUNeVision iAdvantage MEGA Gateway", "datacentermap"),
    ("iAdvantage MEGA Two (SHKLC)", "baxtel", "SUNeVision iAdvantage MEGA Two", "datacentermap"),
    ("iAdvantage One", "baxtel", "SUNeVision iAdvantage ONE", "datacentermap"),
    ("iAdvantage TKO MEGA Plus", "baxtel", "SUNeVision iAdvantage MEGA Plus", "datacentermap"),
    ("iAdvantage MEGA-i Hong Kong", "baxtel", "SUNeVision iAdvantage MEGA-i", "datacentermap"),
    ("iAdvantage MEGA IDC", "baxtel", "SUNeVision iAdvantage MEGA IDC", "datacentermap"),
    # Fuchu, Tokyo: GDS/Gaw campus — Baxtel campus-level vs DCM FIP1, same
    # 36MW, ~150m apart, descriptions describe the same campus.
    ("DayOne: Fuchu Intelligent Park", "baxtel", "DayOne Fuchū Intelligent Park FIP1", "datacentermap"),
    # Fuchu: SoftBank's Fuchu DC is operated by subsidiary IDC Frontier —
    # identical coords, same Dec-2020 opening, same ~4,000 racks. One building.
    ("IDC Frontier: Tokyo Fuchu", "baxtel", "IDC Frontier Tokyo Fuchu", "datacentermap"),
    ("SoftBank Tokyo Fuchu", "datacentermap", "IDC Frontier Tokyo Fuchu", "datacentermap"),
    # Leading Edge: Baxtel "LEDC: X" vs DCM "Leading Edge DC X".
    ("LEDC: Dubbo", "baxtel", "Leading Edge DC Dubbo", "datacentermap"),
    ("LEDC: Tamworth", "baxtel", "Leading Edge DC Tamworth", "datacentermap"),
    ("LEDC: Coffs Harbour", "baxtel", "Leading Edge DC Coffs Harbour", "datacentermap"),
    # Single-site renames confirmed by identical/near-identical coordinates.
    ("Telstra: Exhibition", "baxtel", "Telstra Exhibition", "datacentermap"),
    ("Biznet Technovillage", "baxtel", "Biznet Technovillage", "datacentermap"),
    ("Over The Wire: 360 St Pauls Terrace", "baxtel", "Over The Wire - 360 St Pauls Terrace", "datacentermap"),
    ("newCentrIX Medan Centrum", "baxtel", "neuCentrIX Medan", "datacentermap"),
    ("NEXTDC: SC2", "baxtel", "NEXTDC - SC2 Sunshine Coast", "datacentermap"),
    ("Amazon: Delivery Dr", "baxtel", "Amazon AWS MEL - 22 Delivery", "datacentermap"),
    ("EdgeConneX: SYD1 (MPark)", "baxtel", "EdgeConneX Macquarie Park - SYD1", "datacentermap"),
    ("Google: Lok Yang 1", "baxtel", "Google Lok Yang Data Center", "datacentermap"),
    ("Kepple DC Singapore 1 (S25)", "baxtel", "Keppel DC Singapore 1 (SGP1)", "datacentermap"),
    ("Kepple DC Singapore 2 (T25)", "baxtel", "Keppel DC Singapore 2 (SGP2)", "datacentermap"),
    ("Keppel DC Singapore 3 (T27)", "baxtel", "Keppel DC Singapore 3 (SGP3)", "datacentermap"),
    ("Keppel DC Singapore 4", "baxtel", "Keppel DC Singapore 4 (SGP4)", "datacentermap"),
    ("Keppel DC Singapore 5 (Kingsland)", "baxtel", "Keppel DC Singapore 5 (SGP5)", "datacentermap"),
    ("Keppel DC Singapore 7", "baxtel", "Keppel DC Singapore 7 (SGP7)", "datacentermap"),
    ("Keppel DC: Singapore 8", "baxtel", "Keppel DC Singapore 8 (SGP8)", "datacentermap"),
    ("Keppel DC: Singapore 9", "baxtel", "Keppel DC Singapore 9 (SGP9)", "datacentermap"),
    ("NTT Singapore Serangoon SG1", "baxtel", "GDC - Serangoon Data Center", "datacentermap"),
    ("Bridge Data Centres Cyberjaya MY01", "baxtel", "BDC MY01", "datacentermap"),
    ("CSF Computer Exchange 2 (CX2)", "baxtel", "CX2 Data Center", "datacentermap"),
    ("STT Kuala Lumpur 1", "baxtel", "STT Kuala Lumpur 1", "datacentermap"),
    # --- 2026-08-25 directory-sync flag review -------------------------------
    # Evolution DC Bangkok: Baxtel's "Evolution DC: Bangkok" (52MW, JV with
    # Central Pattana per its description) is DCM's "Evolution DC Bangkok
    # (TH01)" — same 52MW, 210m apart, both descriptions name the same JV.
    ("Evolution DC: Bangkok", "baxtel", "Evolution DC Bangkok (TH01)", "datacentermap"),
    # Evolution DC Manila: Baxtel "Evolution DC: Manila" (69MW, Megawide JV
    # per description) and the earlier DCM-imported country-level shell
    # "Evolution DC Philippines" (identical coordinates) are both DCM's
    # "Evolution DC Manila (PH01)" (100MW, Gateway Business Park, Cavite).
    ("Evolution DC: Manila", "baxtel", "Evolution DC Manila (PH01)", "datacentermap"),
    ("Evolution DC Philippines", "datacentermap", "Evolution DC Manila (PH01)", "datacentermap"),
    # SK+AWS Ulsan: one project listed three ways. SK Telecom and AWS broke
    # ground together on the 103MW Mipo Industrial Complex AI DC (DCD,
    # 2026-08-05); the Baxtel entry even names AWS as anchor tenant of the
    # SK Hyper build. DCM-primary survivor keeps the DCM listing's operator.
    ("SK Hyper: Ulsan", "baxtel", "Amazon AWS - Ulsan", "datacentermap"),
]

# Entries whose stored operator is plain wrong (stale ownership).
REASSIGN = {
    "switch-bangkok-china-mobile-chon-buri": "China Mobile",
}

# Id-addressed merges for cases where name resolution is ambiguous (several
# rows share the same name).
ID_PAIRS = [
    # STT Johor: the single Baxtel record (16MW, Construction) was inserted
    # three times by historical import runs (city-string drift defeated the
    # match). One copy folds into DCM's building-level "STT Johor 1" (same
    # 16MW phase-1 building); the other two copies are deleted outright.
    ("stt-johor-campus", "johor-bahru-stt-johor-1"),
    # Baxtel's 120MW campus-level entry + two empty DCM campus shells fold
    # into the real DCM campus entry (120MW, under construction).
    ("iskandar-puteri-johor-stt-johor-campus", "johor-bahru-stt-johor-campus-3"),
    ("johor-bahru-stt-johor-campus", "johor-bahru-stt-johor-campus-3"),
    ("johor-bahru-stt-johor-campus-2", "johor-bahru-stt-johor-campus-3"),
    # 2026-08-25 sync review: the news-created campus entry (Dalby, 1440MW
    # planned, AUD 14.5bn, WDDP Pty Ltd) is the same Western Downs Digital
    # Park that DCM lists as campus + 4 buildings near Kogan QLD (DCM city
    # granularity says "Brisbane" — the market, not the site).
    ("zerra-dc-dalby-western-downs-digital-park", "zerra-dc-brisbane-western-downs-digital-park"),
    # Third copy of the SK+AWS Ulsan AI DC (news-seeded name variant).
    ("sk-telecom-ai-data-center-ulsan", "amazon-aws-ulsan"),
    # Same Gunsan AI DC reported twice on 2026-08-26: "SGC AI Infra to build"
    # (Korean press, the build entity) and "Korea Investment & Securities
    # joins SGC Energy project" (60MW, KRW 872bn). Survivor keeps the richer
    # record; loser contributes its story_id.
    ("sgc-ai-infra-gunsan-gunsan-ai-data-center", "sgc-energy-gunsan-gunsan-ai-data-center"),
]

# Redundant extra copies of the single Baxtel "STT Johor Campus" record.
DELETE_IDS = [
    "stt-johor-campus-2",
    "stt-johor-campus-3",
]

# Post-merge field corrections, with evidence:
#   johor-bahru-stt-johor-1 — DCM lists it "operational" yet its own expected
#   year is 2027, and STT GDC's $1.37B green-loan news says phase 1 (16MW)
#   completes in 2027. It is a construction site today, not a live building.
FIELD_FIXES = {
    "johor-bahru-stt-johor-1": {
        "status": "under_construction",
        "rfs": "2027",
        "year_built": None,
    },
    # amazon-aws-ulsan — SK Telecom, SK Ecoplant and AWS held the
    # groundbreaking ceremony for the Mipo complex on 2025-08-29 (DCD,
    # 2026-08-05); DCM's "announced" and the merged Baxtel copy agree it is
    # a live construction site. The status_history replaces the merged
    # Baxtel artifact (a bogus "operational" 2026-08-17 import entry — the
    # facility completes 2027+) with the corrected progression.
    "amazon-aws-ulsan": {
        "status": "under_construction",
        "status_history": [
            {"status": "announced", "date": "2026-08-25",
             "source": "directory_sync"},
            {"status": "under_construction", "date": "2026-08-25",
             "source": "directory_sync"},
        ],
    },
    # zerra-dc-brisbane-western-downs-digital-park — the campus is a
    # DA-filed greenfield (1.44GW planned, lodged 2026-08; DCD 2026-08-25).
    # DCM's campus listing says "operational" (its stage refers to existing
    # sheds on site), which let the 2026-08-25 sync stamp an "operational"
    # entry on top of the news-driven "announced". Correct to announced and
    # keep only the news-driven history entry.
    "zerra-dc-brisbane-western-downs-digital-park": {
        "status": "announced",
        "status_history": [
            {"status": "announced", "date": "2026-08-24",
             "story_id": "2026-08-24-47c079"},
        ],
    },
}

# DCM listing links the strict matcher missed (word-vs-digit suffixes like
# "Inzai Two" vs "Inzai 2", or annex naming). The linked project keeps no
# dcm_sync yet — the next sync adopts the listing as its baseline.
DCM_LINKS = {
    # DCM listings "Colt Tokyo Inzai Two/Three/Four" (10109/10110/10111)
    # matched the Inzai One project in the 2026-08-25 sync; they are the
    # Baxtel Inzai 2/3/4 buildings.
    "tokyo-colt-inzai-2": 10109,
    "tokyo-colt-inzai-3": 10110,
    "tokyo-colt-inzai-4": 10111,
    # DCM listing "GDC - Jakarta 2 Annex Data Center" (11208) matched the
    # main GDC Jakarta 2 project; it is the Baxtel "NTT: Jakarta 2 Annex".
    "ntt-jakarta-2-annex": 11208,
}

# Confirmed DCM-side duplicate listings: the same facility is listed twice
# by DCM; the alias ids are skipped by sync_directories (no diff, no flag,
# no detail fetch). Populated from flag review where the duplicate listing's
# coordinates match the linked project's.
DCM_ALT_IDS: dict[str, list[int]] = {
    # 2026-08-25 flag review (listing coords from the sync summary flags):
    # "ARTERIA ComSpace III" (10855) sits at the EXACT same coordinates as
    # our linked "ARTERIA ComSpace III (Annex)" (10856) — one site, listed
    # twice (main + annex pin at the same address).
    "tokyo-arteria-comspace-iii-annex": [10855],
    # Vocus Perth: listing 774's URL slug is /australia/perth/amcom-perth1/
    # — Amcom was acquired by Vocus in 2015 and DCM renamed the legacy
    # listing to "Vocus Data Centre - Perth", duplicating listing 3109
    # (pins 0.3km apart, same current name).
    "vocus-perth": [774],
    # Vocus Brisbane: same pattern — listing 873's slug is
    # /australia/brisbane/pipe-networks-brisbane/, a legacy PIPE/TGP-era
    # listing DCM now shows as "Vocus Data Centre - Brisbane" (0.3km apart).
    "vocus-data-centre-brisbane": [873],
}

# --- 2026-08-25 generalized-naming flag review (round 2) -------------------
# Flags where the DCM listing is just "Operator + City" and name-matches an
# already-linked project but sits 1.8-21.7 km away. Evidence per case was
# pulled from both DCM detail pages (.scratch/flag-detail-2026-08-25.json).

# Case (c): the stored project identity (name + description) belongs to the
# NEW listing — DCM renamed the originally linked listing and re-listed the
# described facility under a new id. The project follows its identity:
# dcm_id moves, coordinates move to the new listing's pin (the stored pin
# was the other facility's), and dcm_sync is re-baselined on the new listing
# so the next sync sees no diff. The freed old listing is NOT a duplicate —
# it gets its own entry in NEW_DCM_PROJECTS below.
DCM_RELINK = {
    # huawei-ulanqab-iii: name and description both say "III", but the link
    # and pin were listing 12131 "Huawei Ulanqab" (40.99421, 113.23205).
    # DCM's listing 12684 "Huawei Ulanqab III" is the phase-III site,
    # 7.0 km east (40.98499, 113.31504).
    "huawei-ulanqab-iii": {
        "dcm_id": 12684,
        "lat": 40.98499,
        "lon": 113.31504,
        "dcm_sync": {"name": "Huawei Ulanqab III", "operator": "Huawei",
                     "city": "Ulanqab", "category": "Hyperscaler"},
    },
    # fukuoka-softbank-kitakyushu-e-port-ii: name "…e-PORT II" and the
    # description ("Kyushu e-PORT II Data Center …") are listing 13861's
    # text; the link and pin were listing 13860 "…e-PORT" (the phase-I
    # building, 1.8 km away at 33.87322, 130.796).
    "fukuoka-softbank-kitakyushu-e-port-ii": {
        "dcm_id": 13861,
        "lat": 33.88042,
        "lon": 130.81377,
        "dcm_sync": {"name": "SoftBank Kitakyushu e-PORT II",
                     "operator": "SoftBank", "city": "Fukuoka",
                     "category": "Colocation"},
    },
}

# Case (a): genuinely distinct facilities behind generalized listings.
# Created with the same conventions as sync_directories._create_project
# (seed, source=datacentermap, first_seen=run date, initial status_history
# entry with source="directory_sync"); names carry the district/address
# disambiguator where DCM's listing name alone is ambiguous. Descriptions
# are the DCM detail-page text (600-char truncation, as _dcm_detail).
NEW_DCM_PROJECTS = [
    # "China Telecom Shanghai" (4391): central-Shanghai pin (People's
    # Square area), 13.2 km from the linked Quanhua facility (1664,
    # 31.12311, 121.53368). Both operational colocation listings.
    {
        "dcm_id": 4391,
        "name": "China Telecom Shanghai",
        "operator": "China Telecom Global Ltd",
        "region": "china", "country": "China", "city": "Shanghai",
        "status": "operational", "category": "Colocation",
        "lat": 31.23039, "lon": 121.4737,
        "description": "China Telecom is the largest fixed line service and "
                       "3rd largest mobile telecommunication provider in "
                       "China.",
        "dcm_sync": {"name": "China Telecom Shanghai",
                     "operator": "China Telecom Global Ltd",
                     "city": "Shanghai", "category": "Colocation"},
    },
    # "Huawei Ulanqab" (12131): the phase-I site, freed by the relink of
    # huawei-ulanqab-iii to listing 12684 above.
    {
        "dcm_id": 12131,
        "name": "Huawei Ulanqab",
        "operator": "Huawei",
        "region": "china", "country": "China", "city": "Ulanqab",
        "status": "operational", "category": "Hyperscaler",
        "lat": 40.99421, "lon": 113.23205,
        "description": "Please visit the website of Huawei for further "
                       "details about Huawei Ulanqab.",
        "dcm_sync": {"name": "Huawei Ulanqab", "operator": "Huawei",
                     "city": "Ulanqab", "category": "Hyperscaler"},
    },
    # "China Unicom Hangzhou" (4414): DCM detail page gives city=Yuhang,
    # 21.6 km from the linked 4415 (city=Binjiang). Two districts, two
    # facilities; name carries the district disambiguator.
    {
        "dcm_id": 4414,
        "name": "China Unicom Hangzhou (Yuhang)",
        "operator": "China Unicom",
        "region": "china", "country": "China", "city": "Hangzhou",
        "status": "operational", "category": "Colocation",
        "lat": 30.27606, "lon": 119.98548,
        "description": "China Unicom is ranked as the world's third-biggest "
                       "mobile provider, and at same time provide fixed "
                       "line service after the merger with China Netcom.",
        "dcm_sync": {"name": "China Unicom Hangzhou",
                     "operator": "China Unicom",
                     "city": "Hangzhou", "category": "Colocation"},
    },
    # "China Telecom Wuhu IDC" (12715): 6.8 km from the linked "China
    # Telecom Wuhu" (12678, 31.34721, 118.43375); distinct listing name.
    {
        "dcm_id": 12715,
        "name": "China Telecom Wuhu IDC",
        "operator": "China Telecom Global Ltd",
        "region": "china", "country": "China", "city": "Wuhu",
        "status": "operational", "category": "Colocation",
        "lat": 31.33527, "lon": 118.36315,
        "description": "Please visit the website of China Telecom Global "
                       "Ltd for further details about China Telecom Wuhu "
                       "IDC.",
        "dcm_sync": {"name": "China Telecom Wuhu IDC",
                     "operator": "China Telecom Global Ltd",
                     "city": "Wuhu", "category": "Colocation"},
    },
    # "China Unicom Ordos" (12697): 21.7 km from the linked "China Unicom
    # Ordos Yun Base" (12696, 39.52656, 109.87289); names already differ.
    {
        "dcm_id": 12697,
        "name": "China Unicom Ordos",
        "operator": "China Unicom",
        "region": "china", "country": "China", "city": "Ordos City",
        "status": "operational", "category": "Colocation",
        "lat": 39.71876, "lon": 109.91778,
        "description": "Please visit the website of China Unicom for "
                       "further details about China Unicom Ordos.",
        "dcm_sync": {"name": "China Unicom Ordos",
                     "operator": "China Unicom",
                     "city": "Ordos City", "category": "Colocation"},
    },
    # "TELEHOUSE Hong Kong" (4023): detail-page description is the original
    # Telehouse HK facility — established 2000, Taikoo Place, 27,000 sqft.
    # The linked 5763 "TELEHOUSE Hong Kong CCC" is the purpose-built Tier 3+
    # building in Tseung Kwan O (32,000 sqft whitespace), 3.0 km away.
    {
        "dcm_id": 4023,
        "name": "TELEHOUSE Hong Kong (Taikoo Place)",
        "operator": "KDDI Corporation",
        "region": "china", "country": "China", "city": "Hong Kong",
        "status": "operational", "category": "Colocation",
        "lat": 22.31217, "lon": 114.25668,
        "description": (
            "Established in 2000, Telehouse Hong Kong offers 24/7 support "
            "to customers' server colocation requirements, consulting, "
            "disaster recovery and system back-up services at highest "
            "standards. Located in the high-tech business quarter "
            "Taikoo-Place, Telehouse Hong Kong offers 27,000 square-feet "
            "of first-rate colocation space in the heart of Hong Kong's "
            "financial and business center, which is a key strategic "
            "location for Telehouse and its customers. As a "
            "carrier-neutral colocation site, Telehouse Hong Kong provides "
            "customers choices from a wide range of local and "
            "international carriers, granting customers' access to "
            "telecommunications carriers such as C&W, HKT, New T&T, "
            "Hutchison")[:600],
        "dcm_sync": {"name": "TELEHOUSE Hong Kong",
                     "operator": "KDDI Corporation",
                     "city": "Hong Kong", "category": "Colocation"},
    },
    # "SoftBank Kitakyushu e-PORT" (13860): the phase-I building, freed by
    # the relink of fukuoka-softbank-kitakyushu-e-port-ii to 13861 above.
    {
        "dcm_id": 13860,
        "name": "SoftBank Kitakyushu e-PORT",
        "operator": "SoftBank",
        "region": "japan", "country": "Japan", "city": "Fukuoka",
        "status": "operational", "category": "Colocation",
        "lat": 33.87322, "lon": 130.796,
        "description": (
            "The SoftBank Kyushu e-PORT Data Center is a highly advanced "
            "facility located in Kitakyushu, Japan. Designed with robust "
            "infrastructure, it features dual high-voltage power supply "
            "systems and an internal redundant UPS configuration to ensure "
            "uninterrupted operations. The cooling system employs an "
            "air-cooled, underfloor air distribution method with N+1 "
            "redundancy, optimizing energy efficiency and reliability. "
            "This data center prioritizes safety with nitrogen-based fire "
            "suppression systems and is certified with PAS99, ISO9001, "
            "ISO14001, ISO20000, and ISO27001 standards, ensuring top-tier "
            "quality, environmental management, IT service management")[:600],
        "dcm_sync": {"name": "SoftBank Kitakyushu e-PORT",
                     "operator": "SoftBank", "city": "Fukuoka",
                     "category": "Colocation"},
    },
]


def resolve(plist, name, source):
    cands = [p for p in plist if p.get("name") == name
             and (source is None or p.get("source") == source)]
    return cands


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="backup + rewrite projects.json (default: dry-run)")
    ap.add_argument("--path", type=Path, default=projects.PROJECTS_PATH)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    db = projects.load_projects(args.path)
    plist = db["projects"]

    # 1. Operator renames through the alias table.
    renames: dict[str, tuple[str, int]] = {}
    for p in plist:
        canon = projects.canonical_operator(p.get("operator", ""))
        if canon != p.get("operator"):
            old = p["operator"]
            renames[old] = (canon, renames.get(old, (canon, 0))[1] + 1)
            p["operator"] = canon
    for old, (new, n) in sorted(renames.items()):
        log.info("rename [%d] %s -> %s", n, old, new)
    log.info("%d operator spellings unified", len(renames))

    # 2. Stale-ownership reassignment.
    for pid, new_op in REASSIGN.items():
        for p in plist:
            if p["id"] == pid:
                log.info("reassign operator: %s -> %s (%s)", p["operator"], new_op, p["name"])
                p["operator"] = new_op

    # 3. Verified facility merges (DCM-primary).
    drop_ids = set()
    problems = []
    for loser_name, loser_src, surv_name, surv_src in PAIRS:
        losers = [p for p in resolve(plist, loser_name, loser_src) if p["id"] not in drop_ids]
        survs = resolve(plist, surv_name, surv_src)
        if not losers and len(survs) == 1:
            continue  # already applied in a previous run
        if len(losers) != 1 or len(survs) != 1:
            problems.append((loser_name, len(losers), surv_name, len(survs)))
            continue
        loser, surv = losers[0], survs[0]
        touched = merge_fields(surv, loser)
        drop_ids.add(loser["id"])
        log.info("merge: %s -> %s  (fields: %s)",
                 loser["name"], surv["name"], ", ".join(touched) or "none")

    # 3b. Id-addressed merges (name resolution ambiguous).
    by_id = {p["id"]: p for p in plist}
    for loser_id, surv_id in ID_PAIRS:
        loser, surv = by_id.get(loser_id), by_id.get(surv_id)
        if loser is None and surv is not None:
            continue  # already applied in a previous run
        if loser is None or surv is None or loser_id in drop_ids:
            problems.append((loser_id, -1, surv_id, -1))
            continue
        touched = merge_fields(surv, loser)
        drop_ids.add(loser_id)
        log.info("merge: [%s] %s -> [%s] %s  (fields: %s)",
                 loser_id, loser["name"], surv_id, surv["name"],
                 ", ".join(touched) or "none")

    # 3c. Outright deletes (redundant copies of one source record).
    for pid in DELETE_IDS:
        if pid in by_id and pid not in drop_ids:
            log.info("delete redundant copy: [%s] %s", pid, by_id[pid]["name"])
            drop_ids.add(pid)

    # 3d. Post-merge field corrections.
    for pid, fixes in FIELD_FIXES.items():
        p = by_id.get(pid)
        if p is None or pid in drop_ids:
            problems.append((pid, -1, "FIELD_FIXES", -1))
            continue
        for field, value in fixes.items():
            log.info("fix [%s] %s: %r -> %r", pid, field, p.get(field), value)
            p[field] = value

    # 3e. Manual DCM listing links (strict matcher missed these).
    held_dcm = {p["dcm_id"] for p in plist
                if isinstance(p.get("dcm_id"), int)}
    for pid, dcm_id in DCM_LINKS.items():
        p = by_id.get(pid)
        if p is None or pid in drop_ids:
            problems.append((pid, -1, "DCM_LINKS", -1))
            continue
        if p.get("dcm_id") == dcm_id:
            continue  # already applied in a previous run
        if p.get("dcm_id") is not None:
            problems.append((pid, -1, f"DCM_LINKS dcm_id already {p['dcm_id']}", -1))
            continue
        if dcm_id in held_dcm:
            problems.append((pid, -1, f"DCM_LINKS {dcm_id} held elsewhere", -1))
            continue
        log.info("link [%s] %s -> dcm_id %d", pid, p["name"], dcm_id)
        p["dcm_id"] = dcm_id
        held_dcm.add(dcm_id)

    # 3f. Confirmed DCM-side duplicate listings (aliased, skipped by sync).
    for pid, alt_ids in DCM_ALT_IDS.items():
        p = by_id.get(pid)
        if p is None or pid in drop_ids:
            problems.append((pid, -1, "DCM_ALT_IDS", -1))
            continue
        alts = p.setdefault("dcm_alt_ids", [])
        for aid in alt_ids:
            if aid != p.get("dcm_id") and aid not in alts:
                log.info("alias [%s] %s += dcm_id %d", pid, p["name"], aid)
                alts.append(aid)
        p["dcm_alt_ids"] = sorted(alts)

    # 3g. dcm_id relinks — the project's identity (name/description) belongs
    # to a different listing than the one it was linked to. The project
    # follows its identity; the freed listing gets its own entry in 3h.
    for pid, relink in DCM_RELINK.items():
        p = by_id.get(pid)
        if p is None or pid in drop_ids:
            problems.append((pid, -1, "DCM_RELINK", -1))
            continue
        new_id = relink["dcm_id"]
        if p.get("dcm_id") == new_id:
            continue  # already applied in a previous run
        holder = next((q for q in plist
                       if q is not p and q.get("dcm_id") == new_id), None)
        if holder is not None:
            problems.append((pid, -1, f"DCM_RELINK {new_id} held by "
                                    f"{holder['id']}", -1))
            continue
        log.info("relink [%s] %s: dcm_id %s -> %d (coords %.5f,%.5f)",
                 pid, p["name"], p.get("dcm_id"), new_id,
                 relink["lat"], relink["lon"])
        p["dcm_id"] = new_id
        p["lat"] = relink["lat"]
        p["lon"] = relink["lon"]
        p["dcm_sync"] = dict(relink["dcm_sync"])

    # 3h. Distinct facilities behind generalized "Operator + City" listings:
    # standalone entries, same conventions as sync_directories._create_project
    # (seed, source=datacentermap, first_seen, initial directory_sync
    # status_history, dcm_sync baseline so the next sync sees no diff).
    held_dcm = {p["dcm_id"] for p in plist
                if isinstance(p.get("dcm_id"), int)}
    held_dcm |= {a for p in plist for a in (p.get("dcm_alt_ids") or [])
                 if isinstance(a, int)}
    taken = {p["id"] for p in plist}
    today = datetime.now(timezone.utc).date().isoformat()
    for spec in NEW_DCM_PROJECTS:
        if spec["dcm_id"] in held_dcm:
            continue  # already created in a previous run
        upd = projects._validate_update(spec)
        if upd is None:
            problems.append((spec["name"], -1, "NEW_DCM_PROJECTS", -1))
            continue
        upd["operator"] = projects.canonical_operator(upd["operator"])
        upd["source"] = "datacentermap"
        project = projects._new_project(upd, today, taken)
        project["seed"] = True
        project["dcm_id"] = spec["dcm_id"]
        project["dcm_sync"] = dict(spec["dcm_sync"])
        project["status_history"] = [{"status": project["status"],
                                      "date": today,
                                      "source": "directory_sync"}]
        plist.append(project)
        by_id[project["id"]] = project
        held_dcm.add(spec["dcm_id"])
        log.info("create [%s] %s (dcm_id %d)",
                 project["id"], project["name"], spec["dcm_id"])

    if problems:
        for ln, lc, sn, sc in problems:
            log.warning("UNRESOLVED: loser %r (%d) survivor %r (%d)", ln, lc, sn, sc)

    keep = [p for p in plist if p["id"] not in drop_ids]
    log.info("total %d -> %d (%d merged away)", len(plist), len(keep), len(drop_ids))

    if not args.apply:
        print("dry-run: projects.json NOT written")
        return
    if problems:
        print("unresolved pairs — fix before applying")
        sys.exit(1)
    shutil.copy(args.path, BACKUP)
    db["projects"] = keep
    projects.save_projects(db, args.path)
    print(f"backup: {BACKUP}")
    print(f"written: {args.path} ({len(plist)} -> {len(keep)})")


if __name__ == "__main__":
    sys.exit(main())
