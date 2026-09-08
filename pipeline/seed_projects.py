"""Rebuild the LLM seed for data/projects.json, in per-region batches.

One LLM call per region (kimi-cli is single-threaded and large single
calls degrade), asking only for real, well-known, nameable data center
projects/campuses with canonical operators and conservative dates:
`rfs` stays null for operational projects and may only carry a
FUTURE date for announced/under_construction ones. Extended profile
fields (developer/investor/investment/phases/anchor_tenants/power/
announced/construction_start) are filled only when well-known public
fact — null otherwise.

Merge strategy: existing entries that carry story_ids (news-linked) are
kept — after the manual review fixups in _NEWS_ENTRY_FIXUPS /
_NEWS_ENTRY_DROP below; old unlinked seed entries are discarded and
regenerated. Each validated batch is merged with projects.upsert_projects
(same operator + name/city dedupe, LLM arbitration when ambiguous), so
seed entries that match a news-linked project fold into it.

Run with an LLM backend, e.g.:

    LLM_BACKEND=kimi-cli pipeline/.venv/Scripts/python pipeline/seed_projects.py

The script prints the final DB grouped by region — review it and drop
anything you can't verify as real. Fewer real entries beat more
hallucinated ones.
"""
from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone

import process
import projects

log = logging.getLogger(__name__)

# ------------------------------------------------------------------- batches

_REGION_BRIEFS: dict[str, tuple[str, str]] = {
    "southeast-asia": (
        "15-20",
        "Markets: Johor (Sedenak/Iskandar Puteri/Nusajaya), Kuala Lumpur/"
        "Cyberjaya, Singapore, Jakarta/Cibitung, Batam (Nongsa), Bangkok, "
        "Manila. Operators active here include Princeton Digital Group, "
        "STT GDC, AirTrunk, GDS, DayOne, Vantage Data Centers, Keppel Data "
        "Centres, Equinix, NTT Global Data Centers, Digital Edge, BDx, "
        "EdgeConneX, DCI Indonesia, Empyrion DC, Bridge Data Centres, YTL, "
        "Global Switch.",
    ),
    "china": (
        "15-20",
        "Markets: Beijing/Shanghai/Shenzhen metros plus the northern and "
        "western hub clusters (Huailai/Zhangjiakou in Hebei, Ulanqab and "
        "Hohhot in Inner Mongolia, Zhongwei in Ningxia, Qingyang in Gansu, "
        "Guiyang in Guizhou). Operators include GDS, VNET, Chindata, "
        "Bridge Data Centres, Alibaba Cloud, Tencent Cloud, Baidu, Huawei "
        "Cloud, Apple (iCloud campuses run with local partners), Sinnet "
        "(AWS Beijing), NWCD (AWS Ningxia).",
    ),
    "japan": (
        "15-20",
        "Markets: Tokyo metro (Inzai, Shiroi, Tama), Osaka metro (Ibaraki, "
        "Keihanna), emerging Fukuoka/Hokkaido. Operators include AirTrunk, "
        "Equinix, NTT Global Data Centers, Digital Realty, Vantage Data "
        "Centers, STT GDC, Princeton Digital Group, KDDI Telehouse, "
        "SoftBank, Google, Microsoft, AWS, Oracle, Colt DCS, STACK.",
    ),
    "australia": (
        "10-15",
        "Markets: Sydney, Melbourne, Canberra, Perth, Brisbane. Operators "
        "include AirTrunk, NEXTDC, Equinix, CDC Data Centres, Vantage Data "
        "Centers, Digital Realty, Global Switch, STACK, Telstra, DCI/"
        "ISEEK, ResetData.",
    ),
    "korea": (
        "10-15",
        "Markets: Seoul metro (Gasan, Sangam, Incheon), Busan, Gumi. "
        "Operators include Naver, KT, SK Telecom, LG CNS, Equinix, Digital "
        "Realty, Empyrion DC, Kakao, NHN, Samsung SDS.",
    ),
}

_SEED_SYSTEM_TMPL = (
    "You are an analyst maintaining a database of major data center "
    "projects in Asia-Pacific, current as of 2026. List {count} MAJOR, "
    "well-known, REAL data center projects/campuses in the region "
    "\"{region}\" that are announced, under construction or operational. "
    "{brief}\n"
    "STRICT RULES:\n"
    "- Only include projects you are confident actually exist and can "
    "name precisely (e.g. \"AirTrunk TOK1\", \"STT Johor Campus\", "
    "\"Naver Sejong Data Center\"). Do NOT invent plausible-sounding "
    "projects; fewer real entries beat more hallucinated ones.\n"
    "- operator must be the canonical name of the actual data center "
    "operator/developer/hyperscaler (e.g. \"Princeton Digital Group\", "
    "\"STT GDC\", \"AirTrunk\", \"NEXTDC\", \"GDS\", \"VNET\", \"Equinix\", "
    "\"NTT Global Data Centers\", \"Digital Realty\", \"Vantage Data "
    "Centers\", \"DayOne\", \"Keppel Data Centres\", \"Empyrion DC\", "
    "\"Digital Edge\", \"BDx\", \"EdgeConneX\", \"Global Switch\", \"KDDI "
    "Telehouse\", \"SoftBank\", \"Naver\", \"KT\", \"SK Telecom\", \"LG "
    "CNS\", \"Chindata\", \"Bridge Data Centres\", \"DCI Indonesia\", "
    "\"CDC Data Centres\", \"STACK\", \"YTL\"). A property/industrial-"
    "estate developer merely promoting land is NOT an operator — exclude "
    "such projects.\n"
    "- capacity_mw: total planned IT load in MW as a number; null when "
    "genuinely unknown. Never guess.\n"
    "- status: one of " + json.dumps(projects.STATUSES) + " as of 2026.\n"
    "- rfs: expected ready-for-service/go-live date. null for "
    "operational projects, ALWAYS. For announced or under_construction "
    "projects, a FUTURE date only (e.g. \"2027\", \"H2 2026\") — never a "
    "past date; null when unsure.\n"
    "- developer: the project developer when well-known and distinct "
    "from the operator (common for develop-to-core projects), else "
    "null.\n"
    "- investor: capital partner, only when a well-known public fact, "
    "else null.\n"
    "- investment: investment/financing amount as free text, only when "
    "well-known, else null. Never guess.\n"
    "- phases: phase breakdown as free text, only when well-known, "
    "else null.\n"
    "- anchor_tenants: pre-leased anchor customers, only when a "
    "well-known public fact (e.g. a hyperscaler publicly committed to "
    "the campus); up to 4 names, else []. Never guess.\n"
    "- power: power/PPA/PUE details, only when well-known, else null.\n"
    "- announced / construction_start: \"YYYY\" or \"YYYY-MM\" text, "
    "only when well-known, else null.\n"
    "For each project return: name; operator; country; city; region — "
    "exactly \"{region}\"; capacity_mw (number or null); capacity_note — "
    "what the figure means (e.g. \"total planned\", \"phase 1\"), null "
    "when unknown; status; developer; investor; investment; phases; "
    "anchor_tenants; power; announced; construction_start; rfs. Use JSON "
    "null (never the string \"None\") for unknown values. "
    'Reply with JSON: {{"projects": [{{"name": "...", "operator": "...", '
    '"country": "...", "city": "...", "region": "{region}", '
    '"capacity_mw": null, "capacity_note": null, "status": '
    '"under_construction", "developer": null, "investor": null, '
    '"investment": null, "phases": null, "anchor_tenants": [], '
    '"power": null, "announced": null, "construction_start": null, '
    '"rfs": null}}]}}.'
)

# ------------------------------------------------------- manual review edits
#
# The review rules now live in projects.py so the daily extraction path
# enforces them too: projects.DROP_OPERATORS rejects updates from property/
# estate developers (covers the formerly dropped news entries
# "dps-resources-melaka-data-center-development" and "manee-tech-estate",
# whose operators were "DPS Resources" and "Manee Tech Estate"), and
# projects.OPERATOR_ALIASES canonicalizes operator spellings (also covering
# the former ResetData -> CDC Data Centres fixup for
# "resetdata-deployment-at-cdc-data-centres"). Dropping an entry also
# unlinks its story (the link lives only on the project).

_YEAR_RE = re.compile(r"20\d{2}")


# ------------------------------------------------------------------ cleanup

_canonical_operator = projects.canonical_operator


def _fix_rfs(project: dict, current_year: int) -> None:
    """rfs is null for operational projects; announced /
    under_construction entries may only carry a date that is not entirely
    in the past. Anything unsure becomes null."""
    rfs = project.get("rfs")
    if not rfs:
        return
    if project.get("status") == "operational":
        project["rfs"] = None
        return
    years = [int(y) for y in _YEAR_RE.findall(str(rfs))]
    if not years or max(years) < current_year:
        project["rfs"] = None


def _fetch_region_batch(backend, region: str) -> list[dict]:
    count, brief = _REGION_BRIEFS[region]
    system = _SEED_SYSTEM_TMPL.format(count=count, region=region, brief=brief)
    data = process.chat_json(backend, system,
                             f"Return the {region} project list.")
    out = []
    for raw in data.get("projects", []):
        if not isinstance(raw, dict):
            continue
        upd = projects._validate_update({**raw, "story_id": ""})
        if upd is None:
            log.warning("skipping invalid seed entry: %s", raw)
            continue
        if upd["region"] != region:
            log.warning("skipping %s: region %s != %s", upd["name"],
                        upd["region"], region)
            continue
        upd["operator"] = _canonical_operator(upd["operator"])
        out.append(upd)
    log.info("%s: %d valid seed entries from LLM", region, len(out))
    return out


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    backend = process.select_backend()
    if backend is None:
        raise SystemExit("no LLM backend available — cannot seed")

    today = datetime.now(timezone.utc).date().isoformat()
    current_year = int(today[:4])

    # Keep news-linked entries only (with review fixups); old unlinked
    # seeds are regenerated below.
    db = projects.load_projects()
    kept = []
    for p in db.get("projects", []):
        if not p.get("story_ids"):
            continue
        if projects._canon(p.get("operator", "")) in projects.DROP_OPERATORS:
            log.info("dropping %s (%s — curated exclusion, not a DC "
                     "operator)", p["id"], p.get("operator"))
            continue
        p["operator"] = _canonical_operator(p.get("operator", ""))
        kept.append(p)
        log.info("kept news-linked: %s (%s)", p["id"], p["operator"])
    db["projects"] = kept

    for region in _REGION_BRIEFS:
        batch = _fetch_region_batch(backend, region)
        created, updated = projects.upsert_projects(db, batch,
                                                    backend=backend,
                                                    today=today)
        log.info("%s: merged (%d created, %d updated)", region,
                 created, updated)

    # Final pass: seeds keep seed=true, canonical operators, date rules.
    # A seed entry has never been updated by tracked news, so
    # last_updated stays null and status_history stays empty even if the
    # merge above refreshed its fields.
    for p in db["projects"]:
        if not p.get("story_ids"):
            p["seed"] = True
            p["last_updated"] = None
            p["status_history"] = []
        p["operator"] = _canonical_operator(p.get("operator", ""))
        _fix_rfs(p, current_year)

    projects.save_projects(db)

    # Self-review listing (reconfigure stdout: the Windows console default
    # code page can't print names like "Biñan").
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    by_region: dict[str, list[dict]] = {}
    for p in db["projects"]:
        by_region.setdefault(p["region"], []).append(p)
    total = 0
    for region in _REGION_BRIEFS:
        rows = sorted(by_region.get(region, []),
                      key=lambda p: (p.get("operator", ""), p.get("name", "")))
        print(f"\n== {region} ({len(rows)}) ==")
        for p in rows:
            total += 1
            print("  {name} | {op} | {city} | {cap} | {status} | {rfs}".format(
                name=p.get("name"), op=p.get("operator"),
                city=p.get("city") or "-",
                cap=(f"{p['capacity_mw']}MW"
                     if p.get("capacity_mw") is not None else "?MW"),
                status=p.get("status"), rfs=p.get("rfs") or "-"))
    print(f"\nTOTAL: {total} projects "
          f"({sum(1 for p in db['projects'] if p.get('story_ids'))} news-linked)")


if __name__ == "__main__":
    main()
