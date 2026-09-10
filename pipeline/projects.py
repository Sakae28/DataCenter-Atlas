"""Project pipeline database: data/projects.json.

Tracks individual data center projects/campuses across APAC. Each daily
run extracts concrete project events (new builds, expansions, financings
tied to a specific campus, commissioning, land/power acquisition) from
the surviving stories, matches them against known projects and upserts
data/projects.json per the schema in ../SPEC.md. Degrades gracefully:
with no LLM backend the step is skipped silently.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import process

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
PROJECTS_PATH = ROOT / "data" / "projects.json"

REGIONS = [r for r in process.REGIONS if r != "global"]
STATUSES = ["announced", "under_construction", "operational", "on_hold", "cancelled"]
_STATUS_RANK = {"announced": 0, "under_construction": 1, "operational": 2}

_ALNUM_RE = re.compile(r"[^a-z0-9]+")


# ------------------------------------------------------------- curated rules

# Curated exclusions: extraction updates whose operator canonicalizes to one
# of these are rejected outright — they are property/industrial-estate
# developers promoting land or ambition, not data center operators.
DROP_OPERATORS = {
    "maneetechestate",  # Manee Tech Estate
    "dpsresources",     # DPS Resources
}

# Canonical operator spellings, keyed by _canon(operator). Applied to every
# extracted update before matching so daily news and seed data share one
# spelling and match the same canonical entries.
OPERATOR_ALIASES = {
    "airtrunk": "AirTrunk",
    # Aliases such as "alibabacloud" or "sttelemediaglobaldatacentres" cover
    # spellings common in public facility directories (Baxtel, DataCenterMap)
    # so imported entries match the canonical ones.
    "alibaba": "Alibaba Cloud",
    "alibabacloud": "Alibaba Cloud",
    "amazonaws": "AWS",
    "aws": "AWS",
    "bdx": "BDx",
    "bdxdatacenters": "BDx",
    "bigdataexchange": "BDx",
    "bigdataexchangebdx": "BDx",
    "bridgedatacentres": "Bridge Data Centres",
    "cdc": "CDC Data Centres",
    "cdcdatacentres": "CDC Data Centres",
    "canberradatacentres": "CDC Data Centres",
    "canberradatacentrescdc": "CDC Data Centres",
    "centrin": "Centrin",
    "centrindatasystem": "Centrin",
    "centrinonline": "Centrin",
    "chinamobile": "China Mobile",
    "chinamobileinternational": "China Mobile",
    "chinamobilelimited": "China Mobile",
    "chinatelecom": "China Telecom",
    "chinatelecomglobal": "China Telecom",
    "chinatelecomgloballtd": "China Telecom",
    "chinaunicom": "China Unicom",
    "chindata": "Chindata",
    "chindatagroup": "Chindata",
    "dayone": "DayOne",
    "dciindonesia": "DCI Indonesia",
    "digitaledge": "Digital Edge",
    "digitaledgedc": "Digital Edge",
    "digitalrealty": "Digital Realty",
    "digitalrealtybersama": "Digital Realty",
    "digitalrealtytrust": "Digital Realty",
    "edgeconnex": "EdgeConneX",
    "empyriondc": "Empyrion DC",
    "empyriondigital": "Empyrion DC",
    "equinix": "Equinix",
    "gds": "GDS",
    "gdsholdings": "GDS",
    "globalswitch": "Global Switch",
    "huawei": "Huawei Cloud",
    "huaweicloud": "Huawei Cloud",
    "kddi": "KDDI Telehouse",
    "kddicorporation": "KDDI Telehouse",
    "kdditelehouse": "KDDI Telehouse",
    "keppeldatacenterreit": "Keppel Data Centres",
    "keppeldatacentres": "Keppel Data Centres",
    "kt": "KT",
    "ktclouddc": "KT",
    "ktidc": "KT",
    "lgcns": "LG CNS",
    "microsoft": "Microsoft",
    "microsoftazure": "Microsoft",
    "naver": "Naver",
    "nextdc": "NEXTDC",
    "ntt": "NTT Global Data Centers",
    "nttcommunications": "NTT Global Data Centers",
    "nttdata": "NTT Global Data Centers",
    "nttdatainc": "NTT Global Data Centers",
    "nttglobaldatacenters": "NTT Global Data Centers",
    # NTT regional subsidiaries as listed by DataCenterMap — same facility
    # family, needed so cross-source facility matching sees one operator.
    "nttmscsdnbhd": "NTT Global Data Centers",
    "nttsmartconnectcorporation": "NTT Global Data Centers",
    "princetondigital": "Princeton Digital Group",
    "princetondigitalgroup": "Princeton Digital Group",
    # Caveat: ResetData is a data center operator in its own right, but in
    # the stories tracked so far its only appearance is as a tenant deploying
    # capacity at a CDC Data Centres facility (CDC is the operator there).
    # If ResetData later operates its own facility, this alias must be
    # removed or scoped to that specific story context.
    "resetdata": "CDC Data Centres",
    "sktelecom": "SK Telecom",
    "softbank": "SoftBank",
    "spacedc": "SpaceDC",
    "spacedcpteltd": "SpaceDC",
    "stack": "STACK",
    "stackinfrastructure": "STACK",
    "sttelemedia": "STT GDC",
    "sttelemediaglobaldatacentres": "STT GDC",
    "sttelemediaglobaldatacentressttgdc": "STT GDC",
    "stt": "STT GDC",
    "sttgdc": "STT GDC",
    "telehouse": "KDDI Telehouse",
    "telstra": "Telstra",
    "tencent": "Tencent Cloud",
    "tencentcloud": "Tencent Cloud",
    "vantage": "Vantage Data Centers",
    "vantagedatacenters": "Vantage Data Centers",
    "vnet": "VNET",
    "ytl": "YTL",
    "ytldatacenter": "YTL",
    "ytldatacenterholdingspteltd": "YTL",
    "ytldatacenters": "YTL",
    # --- Corporate-suffix / branding variants (2026-08 cleanup round) -------
    # Same company listed under legal-suffix or alternate-brand spellings by
    # the directories; these aliases both unify existing rows (via the
    # cleanup script) and keep future imports from re-splitting them.
    "5gnetworks5gn": "5G Networks",
    "adwjohnson": "ADW Johnson",
    "adwjohnsonptyltd": "ADW Johnson",
    "aimsdatacentre": "AIMS Data Centre",
    "aimsdatacentresdnbhd": "AIMS Data Centre",
    "area31": "Area31",
    "ascenixpteltd": "Ascenix Pte Ltd",
    "ascenixpteptd": "Ascenix Pte Ltd",
    "attokyocorporation": "AT TOKYO (@Tokyo)",
    "attokyotokyo": "AT TOKYO (@Tokyo)",
    "basisbay": "Basis Bay",
    "basisbaysdnbhd": "Basis Bay",
    "beijinghaoyangclouddatatechnologycoltd": "Beijing Haoyang Cloud&Data Technology Co., Ltd",
    "biznet": "Biznet",
    "biznetdatacenter": "Biznet",
    "biznetnetworks": "Biznet",
    "brightray": "BrightRay DC",
    "brightraydc": "BrightRay DC",
    "canonitsolutions": "Canon IT Solutions",
    "canonitsolutionsinc": "Canon IT Solutions",
    "cdccyberdatacenterinternational": "Cyber Data Center International",
    "chinaunicomglobal": "China Unicom",
    "cyberdatacenterinternational": "Cyber Data Center International",
    "digitalhalo": "Digital Halo",
    "digitalhalodatacenter": "Digital Halo",
    "ditotelecommunity": "DITO Telecommunity",
    "ditotelecommunitycorporation": "DITO Telecommunity",
    "dreammark1": "DreamMark1",
    "dreammark1coltd": "DreamMark1",
    "elitery": "Elitery",
    "eliterydatacenter": "Elitery",
    "eliteryptdatasinergitamajayatbk": "Elitery",
    "epsilontelecommunicationslimited": "Epsilon Telecommunications",
    "exitrasdnbhd": "Exitra Sdn Bhd",
    "fujitsuaustralia": "Fujitsu",
    "gabia": "Gabia",
    "gabiainc": "Gabia",
    "gdsservicesltd": "GDS",
    "globaldatacentregdc": "Global Data Centre (GDC)",
    "globaldatacentresdnbhdgdc": "Global Data Centre (GDC)",
    "goodman": "Goodman Group",
    "gsa": "GSA",
    "gsadatacenter": "GSA",
    "hkbnhongkongbroadbandnetworklimited": "HKBN",
    "hongkongbroadbandnetworklimited": "HKBN",
    "iadvantage": "iAdvantage (SUNeVision)",
    "iadvantagelimited": "iAdvantage (SUNeVision)",
    "iadvantagesunevision": "iAdvantage (SUNeVision)",
    "idcfrontier": "IDC Frontier",
    "idcfrontierinc": "IDC Frontier",
    "indonesiasupercorridorisc": "Indonesia Super Corridor (ISC)",
    "interactiveptyltd": "Interactive",
    "internetthailandinet": "Internet Thailand (INET)",
    "internetthailandpubliccompanylimited": "Internet Thailand (INET)",
    "iren": "IREN",
    "irenisenergy": "IREN",
    "irixsdnbhd": "Irix Sdn Bhd",
    "ironmountain": "Iron Mountain",
    "ironmountaindatacenters": "Iron Mountain",
    "itblock": "IT Block",
    "itblockpteltd": "IT Block",
    "itechnetworksolutionssdnbhd": "i-Tech Network Solutions",
    "japandisplayinc": "Japan Display Inc.",
    "japandisplayincjdi": "Japan Display Inc.",
    "jastelnetwork": "JasTel Network",
    "jastelnetworkcompanylimited": "JasTel Network",
    "kakao": "Kakao",
    "kakaocorp": "Kakao",
    "m1limited": "M1",
    "m1netlimitedmobileone": "M1",
    "marunouchidirectaccess": "Marunouchi Direct Access",
    "marunouchidirectaccessltd": "Marunouchi Direct Access",
    "mcdigitalrealty": "Digital Realty",
    "megaspeed": "MegaSpeed",
    "megaspeedai": "MegaSpeed",
    "micron21": "Micron21",
    "micron21melbournedatacentre": "Micron21",
    "micron21ptyltd": "Micron21",
    "navercloud": "Naver",
    "navercorp": "Naver",
    "ndcnusantaradatacenter": "Nusantara Data Center (NDC)",
    "nhncloud": "NHN",
    "nusantaradatacenter": "Nusantara Data Center (NDC)",
    "okestro": "Okestro",
    "okestrocloud": "Okestro",
    "optage": "Optage",
    "optageinc": "Optage",
    "ovh": "OVH",
    "ovhgroup": "OVH",
    "overthewire": "Over the Wire",
    "overthewireholdingsltd": "Over the Wire",
    "ptindonesiasupercorridor": "Indonesia Super Corridor (ISC)",
    "rackscentral": "Racks Central",
    "rackscentralpteltd": "Racks Central",
    "rackspace": "Rackspace",
    "rackspacehosting": "Rackspace",
    "regalorionsdnbhd": "Regal Orion",
    "safehousebyitechnetworksolutionssdnbhd": "i-Tech Network Solutions",
    "skbroadband": "SK Broadband",
    "skbroadbandsktelecom": "SK Broadband",
    "strateqsdnbhd": "Strateq Sdn Bhd",
    "sttgdcphilippines": "STT GDC",
    "summit": "Summit",
    "summitformerlydeft": "Summit",
    "tcctechnology": "TCC Technology",
    "tcctechnologytcct": "TCC Technology",
    "telstrainfraco": "Telstra",
    "telstrainternational": "Telstra",
    "telstrapbs": "Telstra",
    "viridisgreendatacentres": "VIRIDIS Green Data Centres",
    "viridisgreendatacentreslimited": "VIRIDIS Green Data Centres",
    "vnetgroupinc": "VNET",
    "ycocloud": "YCO Cloud",
    "ycocloudcenters": "YCO Cloud",
    "zdatatechnologies": "ZDATA Technologies",
    "zdatatechnologiescoltd": "ZDATA Technologies",
    "zettagridptyltd": "Zettagrid",
}


# ------------------------------------------------------------------- load/save

def load_projects(path: Path = PROJECTS_PATH) -> dict:
    if not path.exists():
        return {"projects": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("could not read %s, starting empty: %s", path, exc)
        return {"projects": []}


def save_projects(db: dict, path: Path = PROJECTS_PATH) -> None:
    db["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(db, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


# ------------------------------------------------------------------ matching

def _canon(text: str) -> str:
    return _ALNUM_RE.sub("", (text or "").lower())


def canonical_operator(operator: str) -> str:
    return OPERATOR_ALIASES.get(_canon(operator), operator)


# Sovereign-state normalization: directories list some territories as their
# own "country"; we roll them up so filtering and the heat map reflect one
# China (city keeps the territory name, e.g. city="Hong Kong").
COUNTRY_ALIASES = {
    "hongkong": "China",
    "hongkongsar": "China",
    "macau": "China",
    "macao": "China",
}


def canonical_country(country: str) -> str:
    return COUNTRY_ALIASES.get(_canon(country), country)


# Coverage scope: China / Japan / Korea / Australia / Southeast Asia only.
# The LLM occasionally mis-tags an out-of-scope story (e.g. an India project
# labeled "southeast-asia"); the region check alone can't catch that, so the
# country whitelist is the hard guard. Empty country still passes (the region
# field is already validated).
TRACKED_COUNTRIES = {
    "China", "Japan", "South Korea", "Australia", "Taiwan",
    "Indonesia", "Malaysia", "Singapore", "Thailand", "Vietnam",
    "Philippines",
}


def slugify(*parts: str) -> str:
    return _ALNUM_RE.sub("-", "-".join(p for p in parts if p).lower()).strip("-")


def _name_tokens(name: str) -> set[str]:
    return set(process.normalize_title(name).split())


def _name_match(a: str, b: str) -> bool:
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return False
    jaccard = len(ta & tb) / len(ta | tb)
    return jaccard >= 0.5 or ta <= tb or tb <= ta


def _candidates(projects: list[dict], update: dict) -> list[dict]:
    """Same operator (canonical) AND (fuzzy name match OR same city+country)."""
    op = _canon(update.get("operator", ""))
    out = []
    for p in projects:
        if op and _canon(p.get("operator", "")) != op:
            continue
        if _name_match(p.get("name", ""), update.get("name", "")):
            out.append(p)
            continue
        if (update.get("city") and update.get("country")
                and p.get("city", "").lower() == update["city"].lower()
                and p.get("country", "").lower() == update["country"].lower()):
            out.append(p)
    return out


_ARBITRATE_SYSTEM = (
    "You maintain a database of data center projects. Given a new project "
    "update extracted from a news story and a list of candidate existing "
    "projects, decide which candidate (if any) is the SAME real-world "
    "project/campus. Be conservative: different campuses of the same "
    "operator in the same metro are NOT the same project. "
    'Reply with JSON: {"match": int} — the candidate index, or -1 if none.'
)


def _arbitrate(backend, update: dict, candidates: list[dict]) -> dict | None:
    """One LLM call to pick among ambiguous candidates. Falls back to the
    best name match (or None) when arbitration fails."""
    try:
        payload = {
            "update": {k: update.get(k) for k in
                       ("name", "operator", "country", "city", "capacity_mw")},
            "candidates": [
                {"index": i, "id": p["id"], "name": p.get("name"),
                 "city": p.get("city"), "country": p.get("country"),
                 "capacity_mw": p.get("capacity_mw"), "status": p.get("status")}
                for i, p in enumerate(candidates)
            ],
        }
        data = process.chat_json(backend, _ARBITRATE_SYSTEM,
                                 json.dumps(payload, ensure_ascii=False))
        idx = data.get("match")
        if isinstance(idx, int) and 0 <= idx < len(candidates):
            return candidates[idx]
        if idx == -1:
            return None
    except Exception as exc:  # noqa: BLE001
        log.warning("project arbitration failed: %s", exc)
    named = [p for p in candidates
             if _name_match(p.get("name", ""), update.get("name", ""))]
    return named[0] if len(named) == 1 else None


# ------------------------------------------------------------------- upsert

def _next_status(existing: str, new: str) -> str:
    """Status progression: never regress (operational -> announced); on_hold
    and cancelled are event-driven and always accepted."""
    if new in ("on_hold", "cancelled"):
        return new
    if existing in ("on_hold", "cancelled"):
        return new
    if _STATUS_RANK.get(new, 0) > _STATUS_RANK.get(existing, 0):
        return new
    return existing


def _apply_update(project: dict, update: dict, today: str,
                  bump_updated: bool = True) -> bool:
    """Merge one story's update into a project. Returns True when anything
    actually changed (status, field, or newly linked story).

    ``bump_updated=False`` (directory imports, seed backfills) applies field
    changes without touching last_updated — that date is reserved for
    NEWS-LINKED changes only."""
    changed = False
    new_status = update.get("status")
    if new_status in STATUSES:
        old_status = project.get("status", "announced")
        resolved = _next_status(old_status, new_status)
        if resolved != old_status:
            project["status"] = resolved
            project.setdefault("status_history", []).append(
                {"status": resolved, "date": today,
                 "story_id": update.get("story_id")})
            changed = True
    cap = update.get("capacity_mw")
    existing_cap = project.get("capacity_mw")
    # Only grow capacity: a phase figure must not overwrite a larger
    # total-planned figure extracted earlier.
    if (isinstance(cap, (int, float)) and cap != existing_cap
            and (existing_cap is None or cap >= existing_cap)):
        project["capacity_mw"] = cap
        changed = True
        if update.get("capacity_note"):
            project["capacity_note"] = update["capacity_note"]
    elif update.get("capacity_note") and not project.get("capacity_note"):
        project["capacity_note"] = update["capacity_note"]
        changed = True
    if update.get("rfs") and update["rfs"] != project.get("rfs"):
        project["rfs"] = update["rfs"]
        changed = True
    # Extended profile: fill when previously null, replace when the new
    # value is non-null and different (more specific info wins).
    for field in ("developer", "investor", "investment", "phases", "power",
                  "announced", "construction_start"):
        val = update.get(field)
        if val and val != project.get(field):
            project[field] = val
            changed = True
    # Directory profile (baxtel/datacentermap imports): coordinates fill only
    # when missing (exact source pins should not be churned by re-imports);
    # text fields follow the extended-profile rule above.
    for field in ("lat", "lon"):
        val = update.get(field)
        if val is not None and project.get(field) is None:
            project[field] = val
            changed = True
    for field in ("category", "company_type", "description", "year_built",
                  "stage_detail"):
        val = update.get(field)
        if val and val != project.get(field):
            project[field] = val
            changed = True
    if update.get("expansion_planned") and not project.get("expansion_planned"):
        project["expansion_planned"] = True
        changed = True
    specs = update.get("specs")
    if specs:
        # Spec sheet: merge key-by-key (new values win on conflict).
        existing_specs = project.get("specs") or {}
        project["specs"] = existing_specs
        for k, v in specs.items():
            if existing_specs.get(k) != v:
                existing_specs[k] = v
                changed = True
    tenants = update.get("anchor_tenants") or []
    if tenants:
        existing = project.setdefault("anchor_tenants", [])
        known = {t.lower() for t in existing}
        for t in tenants:
            if t.lower() not in known and len(existing) < 4:
                existing.append(t)
                known.add(t.lower())
                changed = True
    story_id = update.get("story_id")
    if story_id and story_id not in project.setdefault("story_ids", []):
        project["story_ids"].append(story_id)
        project["seed"] = False  # now confirmed by a tracked story
        changed = True
    # Provenance of directory imports (e.g. "baxtel", "datacentermap"):
    # fill only when not already set.
    if update.get("source") and not project.get("source"):
        project["source"] = update["source"]
        changed = True
    # last_updated marks the last REAL NEWS-LINKED change — a no-op re-run
    # must not touch it, and neither may directory-import/seed field fills.
    if changed and bump_updated:
        project["last_updated"] = today
    return changed


def _new_project(update: dict, today: str, taken_ids: set[str]) -> dict:
    operator = update.get("operator", "")
    city = update.get("city", "")
    name = update.get("name", "")
    # Avoid stuttering slugs: when the name already contains the operator
    # ("STT Johor Campus" / "STT GDC"), slug from city+name alone.
    op_first = operator.split()[0].lower() if operator.split() else ""
    if op_first and op_first in name.lower():
        base = slugify(name) if city.lower() in name.lower() else slugify(city, name)
    else:
        base = slugify(operator, city, name)
    base = base or "project"
    slug, n = base, 2
    while slug in taken_ids:
        slug = f"{base}-{n}"
        n += 1
    taken_ids.add(slug)
    story_id = update.get("story_id")
    status = (update.get("status") if update.get("status") in STATUSES
              else "announced")
    project = {
        "id": slug,
        "name": name,
        "operator": operator,
        "region": update["region"],
        "country": update.get("country", ""),
        "city": city,
        "capacity_mw": update.get("capacity_mw"),
        "status": status,
        "developer": update.get("developer"),
        "investor": update.get("investor"),
        "investment": update.get("investment"),
        "phases": update.get("phases"),
        "anchor_tenants": list(update.get("anchor_tenants") or []),
        "power": update.get("power"),
        "announced": update.get("announced"),
        "construction_start": update.get("construction_start"),
        "rfs": update.get("rfs"),
        "lat": update.get("lat"),
        "lon": update.get("lon"),
        "category": update.get("category"),
        "company_type": update.get("company_type"),
        "description": update.get("description"),
        "year_built": update.get("year_built"),
        "stage_detail": update.get("stage_detail"),
        "expansion_planned": update.get("expansion_planned"),
        "specs": (dict(update["specs"]) if update.get("specs") else None),
        "status_history": ([{"status": status, "date": today,
                             "story_id": story_id}] if story_id else []),
        "story_ids": [story_id] if story_id else [],
        "first_seen": today,
        # Created from a story = a real change; a bare seed entry has never
        # been updated by tracked news, so it stays null.
        "last_updated": today if story_id else None,
        "seed": False,
    }
    if update.get("capacity_note"):
        project["capacity_note"] = update["capacity_note"]
    if update.get("source"):
        project["source"] = update["source"]
    return project


def upsert_projects(db: dict, updates: list[dict], backend=None,
                    today: str | None = None) -> tuple[int, int]:
    """Match each update to an existing project and apply it, or create a
    new entry. Returns (created, updated) counts."""
    today = today or datetime.now(timezone.utc).date().isoformat()
    projects = db.setdefault("projects", [])
    created = updated = 0
    for upd in updates:
        # Canonicalize the operator before matching so daily news ("GDS
        # Holdings", "ResetData", ...) matches the canonical entries
        # ("GDS", "CDC Data Centres") instead of spawning duplicates.
        upd["operator"] = canonical_operator(upd.get("operator", ""))
        candidates = _candidates(projects, upd)
        if len(candidates) == 1:
            match = candidates[0]
        elif len(candidates) > 1:
            match = _arbitrate(backend, upd, candidates) if backend else None
        else:
            match = None
        if match is not None:
            if upd.get("story_id") and upd["story_id"] in match.get("story_ids", []):
                continue  # already applied (idempotent re-runs)
            before = {k: (list(v) if isinstance(v, list) else v)
                      for k, v in match.items()}
            _apply_update(match, upd, today)
            updated += 1
            changed_keys = [k for k, v in match.items()
                            if before.get(k) != v and k != "last_updated"]
            log.info("project updated: %s (story %s)%s", match["id"],
                     upd.get("story_id"),
                     " — changed: " + ", ".join(changed_keys)
                     if changed_keys else " — no field changes")
        else:
            project = _new_project(upd, today, {p["id"] for p in projects})
            projects.append(project)
            created += 1
            log.info("project created: %s (story %s)", project["id"],
                     upd.get("story_id"))
    return created, updated


# --------------------------------------------------------------- extraction

_EXTRACT_SYSTEM = (
    "You track data center construction projects in APAC for an industry "
    "news service. Given numbered news stories (id, title, summary, "
    "companies), decide which describe a CONCRETE data center PROJECT event "
    "at a specific site/campus: a new build, an expansion, financing tied to "
    "a specific campus, commissioning/go-live, or land/power acquisition "
    "for a specific site. SKIP stories that are pure market commentary, "
    "regulation, company results, or technology news with no specific "
    "project. For each matching story return: "
    "story_id — the story's id, copied exactly; "
    "name — the project/campus name (e.g. \"STT Johor Campus\"); "
    "operator — canonical operating company (e.g. \"STT GDC\", \"AirTrunk\"); "
    "country, city — location of the site; "
    "region — exactly one of " + json.dumps(REGIONS) + "; "
    "capacity_mw — IT capacity in MW as a number, or null when unknown; "
    "capacity_note — what the figure means (e.g. \"total planned\", "
    "\"phase 1\"), null when unknown; "
    "status — one of " + json.dumps(STATUSES) + "; "
    "developer — the developer when named and distinct from the operator, "
    "else null; "
    "investor — capital partner/investor named in the story, else null; "
    "investment — investment or financing amount as free text "
    "(e.g. \"US$1.37B green loan\"), only when stated, else null; "
    "phases — phase breakdown as free text (e.g. \"Phase 1: 72MW (2026); "
    "total planned 500MW\"), only when stated, else null; "
    "anchor_tenants — pre-leased/anchor customers named in the story, "
    "a list of up to 4 names, or []; "
    "power — power/PPA/PUE details mentioned (e.g. \"100% renewable via "
    "PPA; PUE target 1.3\"), else null; "
    "announced — when the project was announced, \"YYYY\" or \"YYYY-MM\" "
    "text, only when stated, else null; "
    "construction_start — construction start date text, only when stated, "
    "else null; "
    "rfs — expected ready-for-service/go-live date as free text "
    "(e.g. \"2027\", \"Q3 2026\") or null. "
    "operator must be a specific named company — if the story does not "
    "name the project operator/developer, OMIT the story. Use JSON null "
    "(never the string \"None\") for unknown values; most extended fields "
    "will be null — only fill what the story actually states. "
    'Reply with JSON: {"results": [{"story_id": "...", "name": "...", '
    '"operator": "...", "country": "...", "city": "...", "region": "...", '
    '"capacity_mw": null, "capacity_note": null, "status": "announced", '
    '"developer": null, "investor": null, "investment": null, '
    '"phases": null, "anchor_tenants": [], "power": null, '
    '"announced": null, "construction_start": null, "rfs": null}]} — '
    "omit stories with no concrete project."
)


_PLACEHOLDER_STRINGS = {"", "none", "null", "n/a", "na", "unknown", "tbd", "-"}


def _clean(text) -> str:
    """Normalize LLM output: placeholder strings (\"None\", \"unknown\" ...)
    become empty."""
    t = str(text or "").strip()
    return "" if t.lower() in _PLACEHOLDER_STRINGS else t


_POWER_RE = re.compile(
    r"\b(mw|gw|kw|kilowatt|megawatt|gigawatt|pue|ppa|power|renewable|solar|"
    r"wind|grid|ups|generator|battery|cooling|hvdc|substation)\b", re.I)


def _clean_power(text) -> str | None:
    """The power field must read like power/capacity infrastructure data; the
    LLM occasionally drops unrelated specs (GPU platforms, network gear) in
    here. Reject values without any power keyword."""
    t = _clean(text)
    if not t:
        return None
    return t if _POWER_RE.search(t) else None


def _validate_update(raw: dict) -> dict | None:
    story_id = str(raw.get("story_id", "")).strip()
    name = _clean(raw.get("name"))
    operator = _clean(raw.get("operator"))
    region = str(raw.get("region", "")).strip()
    if not (name and operator) or region not in REGIONS:
        return None
    country = canonical_country(_clean(raw.get("country")))
    if country and country not in TRACKED_COUNTRIES:
        return None  # out of coverage scope (e.g. India mis-tagged as SEA)
    if operator.lower().startswith(("unnamed", "unknown", "undisclosed")):
        return None  # no canonical operator -> can never be matched reliably
    if _canon(operator) in DROP_OPERATORS:
        return None  # curated exclusion: property/estate play, not a DC operator
    cap = raw.get("capacity_mw")
    try:
        cap = float(cap) if cap is not None else None
        if cap is not None and cap == int(cap):
            cap = int(cap)
    except (TypeError, ValueError):
        cap = None
    status = str(raw.get("status", "")).strip()
    note = _clean(raw.get("capacity_note")) or None
    tenants_raw = raw.get("anchor_tenants")
    tenants = []
    if isinstance(tenants_raw, list):
        for t in tenants_raw:
            t = _clean(t)
            if t and t.lower() not in {x.lower() for x in tenants}:
                tenants.append(t)
            if len(tenants) >= 4:
                break
    return {
        "story_id": story_id,
        "name": name,
        "operator": operator,
        "country": country,
        "city": _clean(raw.get("city")),
        "region": region,
        "capacity_mw": cap,
        "capacity_note": note,
        "status": status if status in STATUSES else "announced",
        "developer": _clean(raw.get("developer")) or None,
        "investor": _clean(raw.get("investor")) or None,
        "investment": _clean(raw.get("investment")) or None,
        "phases": _clean(raw.get("phases")) or None,
        "anchor_tenants": tenants,
        # Sanity gate: the power field must read like power data (MW/GW/kW,
        # PUE, PPA, renewable...); the LLM occasionally stuffs GPU platform
        # notes or other specs in here — those drop to None instead.
        "power": _clean_power(raw.get("power")),
        "announced": _clean(raw.get("announced")) or None,
        "construction_start": _clean(raw.get("construction_start")) or None,
        "rfs": _clean(raw.get("rfs")) or None,
        # Directory-sourced profile fields (import_external.py):
        "lat": _num_coord(raw.get("lat")),
        "lon": _num_coord(raw.get("lon")),
        "category": _clean(raw.get("category")) or None,
        "company_type": _clean(raw.get("company_type")) or None,
        "description": _clean(raw.get("description")) or None,
        "year_built": _clean(raw.get("year_built")) or None,
        "stage_detail": _clean(raw.get("stage_detail")) or None,
        "expansion_planned": True if raw.get("expansion_planned") else None,
        "specs": _clean_specs(raw.get("specs")),
    }


def _clean_specs(raw) -> dict[str, str] | None:
    """Curated spec sheet (label -> value strings) from DCM facility pages."""
    if not isinstance(raw, dict):
        return None
    out = {}
    for k, v in raw.items():
        k, v = _clean(k), _clean(v)
        if k and v:
            out[k] = v
    return out or None


def _num_coord(value) -> float | None:
    """Latitude/longitude as a plain float, or None."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if -180 <= v <= 180 else None


# Windows CreateProcess caps the command line near 32k chars and the kimi
# CLI receives the prompt via argv — keep each batched call well under that.
_MAX_PROMPT_CHARS = 20000


def extract_project_updates(backend, stories: list[dict]) -> list[dict]:
    """LLM calls over the day's stories, batched to stay under the Windows
    command-line limit. Returns validated project updates; [] on failure."""
    if not stories:
        return []
    story_ids = {s["id"] for s in stories}
    out: list[dict] = []
    batch: list[dict] = []
    batch_len = 0

    def flush() -> None:
        nonlocal batch, batch_len
        if not batch:
            return
        data = process.chat_json(backend, _EXTRACT_SYSTEM,
                                 json.dumps(batch, ensure_ascii=False))
        for raw in data.get("results", []):
            if not isinstance(raw, dict):
                continue
            upd = _validate_update(raw)
            if upd and upd["story_id"] in story_ids:
                out.append(upd)
        batch, batch_len = [], 0

    for s in stories:
        item = {"story_id": s["id"], "title": s.get("title", ""),
                "summary": (s.get("summary") or "")[:300],
                "companies": s.get("companies") or []}
        item_len = len(json.dumps(item, ensure_ascii=False)) + 1
        if batch and batch_len + item_len > _MAX_PROMPT_CHARS:
            flush()
        batch.append(item)
        batch_len += item_len
    flush()
    return out


# ------------------------------------------------------------ orchestration

def update_projects_step(backend, stories: list[dict],
                         path: Path = PROJECTS_PATH,
                         today: str | None = None) -> dict | None:
    """Load data/projects.json, extract project updates from the day's
    stories, upsert, save. Returns counts dict, or None when skipped
    (no backend). Never raises: on LLM failure the file is left untouched."""
    if backend is None:
        log.info("no LLM backend — skipping project tracking step")
        return None
    try:
        updates = extract_project_updates(backend, stories)
    except Exception as exc:  # noqa: BLE001
        log.warning("project extraction failed, projects.json untouched: %s",
                    exc)
        return None
    if not updates:
        log.info("project tracking: no project updates in today's stories")
        return {"extracted": 0, "created": 0, "updated": 0}
    db = load_projects(path)
    created, updated = upsert_projects(db, updates, backend=backend,
                                       today=today)
    save_projects(db, path)
    log.info("project tracking: %d updates extracted, %d created, %d updated "
             "(%d projects total)", len(updates), created, updated,
             len(db["projects"]))
    return {"extracted": len(updates), "created": created, "updated": updated}
