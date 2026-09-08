"""Import public data center directory listings into data/projects.json.

One-off / occasional bulk import of existing facility inventories so the
project DB is not limited to what tracked news happens to mention.

Sources
-------
baxtel
    Baxtel (baxtel.com) exposes its full site database as a public Mapbox
    vector tileset (``ericbell.baxtel_sites``); the access token is published
    on https://baxtel.com/map. We fetch the ~120 z5 tiles covering APAC
    (finer coordinate grid than the z0 world tile, ~300 m precision; the
    tileset's Y axis is flipped, see _tile_px_to_lonlat). Each site carries
    site_name, company_name, total_power_mw, primary_stage, layer_stage
    ("Prospective Expansion", "Land Bank", ...), category, company_type and
    metro region_slug. With --baxtel-details, each site's page
    (/data-center/<slug>) adds year_built and a short description.

datacentermap
    DataCenterMap (datacentermap.com) server-renders country/city directory
    pages with the full facility listing embedded as JSON (__NEXT_DATA__).
    Facility detail pages add stage (1=pipeline, 2=live), built-out/pipeline
    MW and expected year of operation. Fetched politely (delay between
    requests); the site sits behind Vercel bot protection, which curl_cffi's
    Chrome impersonation passes — plain httpx/curl gets a 429 checkpoint.

Both sources describe EXISTING inventory, so entries default to
status=operational unless the source says otherwise. Everything imported is
marked seed=true (not confirmed by a tracked story) and tagged with a
``source`` field ("baxtel" / "datacentermap").

Merging is deliberately stricter than projects.upsert_projects: a directory
entry only merges into an existing project on a fuzzy NAME match (plus same
canonical operator). Same operator + same city is NOT enough — operators run
many distinct facilities per metro, and merging them would corrupt real data.

Usage:
    python import_external.py                     # both sources, full run
    python import_external.py --sources baxtel    # one source only
    python import_external.py --no-details        # skip DCM detail pages
    python import_external.py --baxtel-details    # crawl Baxtel site pages too
    python import_external.py --dry-run           # scrape + report, no write
    python import_external.py --refresh           # ignore scrape cache

Network: honors FETCH_PROXY / FETCH_INSECURE_TLS like fetch.py. Scraped
updates are cached under .scratch/ so re-runs (e.g. after merge tweaks) do
not re-hit the sources; use --refresh to re-scrape.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import projects

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = Path(__file__).resolve().parent / ".scratch"

# Baxtel publishes its Mapbox token on https://baxtel.com/map; this is only a
# fallback if the page structure changes.
BAXTEL_MAP_URL = "https://baxtel.com/map"
BAXTEL_TOKEN_FALLBACK = (
    "pk.eyJ1IjoiZXJpY2JlbGwiLCJhIjoiY2swcHB1ZWh3MDBkZDNtbXFjdHVpdWo3cCJ9."
    "L0X1OXz8U_D4fD1hHeViwg"
)
BAXTEL_TILESET = "ericbell.baxtel_sites"

DCM_BASE = "https://www.datacentermap.com"

# Countries in scope: DCM slug -> (our region, country name)
DCM_COUNTRIES = {
    "china": ("china", "China"),
    "hong-kong": ("china", "Hong Kong"),
    "japan": ("japan", "Japan"),
    "south-korea": ("korea", "South Korea"),
    "australia": ("australia", "Australia"),
    "singapore": ("southeast-asia", "Singapore"),
    "malaysia": ("southeast-asia", "Malaysia"),
    "indonesia": ("southeast-asia", "Indonesia"),
    "thailand": ("southeast-asia", "Thailand"),
    "vietnam": ("southeast-asia", "Vietnam"),
    "philippines": ("southeast-asia", "Philippines"),
}

# Baxtel metro/region slugs in scope -> (our region, country, city).
# City may be a state/province name for non-metro slugs, "" for
# country-level buckets. Out-of-scope slugs (Taiwan, New Zealand, Guam,
# Mongolia, Cambodia, Myanmar, ...) are skipped and logged.
BAXTEL_REGIONS = {
    # Japan
    "tokyo": ("japan", "Japan", "Tokyo"),
    "osaka": ("japan", "Japan", "Osaka"),
    "osaka-prefecture": ("japan", "Japan", "Osaka"),
    "chiba": ("japan", "Japan", "Chiba"),
    "hokkaido": ("japan", "Japan", "Hokkaido"),
    "kyushu-okinawa": ("japan", "Japan", ""),
    "japan": ("japan", "Japan", ""),
    "aws-asia-pacific-tokyo-ap-northeast-1": ("japan", "Japan", "Tokyo"),
    "aws-asia-pacific-osaka": ("japan", "Japan", "Osaka"),
    # South Korea
    "seoul": ("korea", "South Korea", "Seoul"),
    "busan": ("korea", "South Korea", "Busan"),
    "gwangju": ("korea", "South Korea", "Gwangju"),
    "cheongju": ("korea", "South Korea", "Cheongju"),
    "daegu": ("korea", "South Korea", "Daegu"),
    "south-korea": ("korea", "South Korea", ""),
    "north-jeolla-jeonbuk": ("korea", "South Korea", "North Jeolla"),
    # Australia
    "sydney": ("australia", "Australia", "Sydney"),
    "melbourne": ("australia", "Australia", "Melbourne"),
    "brisbane": ("australia", "Australia", "Brisbane"),
    "perth": ("australia", "Australia", "Perth"),
    "canberra": ("australia", "Australia", "Canberra"),
    "adelaide": ("australia", "Australia", "Adelaide"),
    "burnie": ("australia", "Australia", "Burnie"),
    "new-south-wales": ("australia", "Australia", "New South Wales"),
    "northern-territory": ("australia", "Australia", "Northern Territory"),
    "tasmania": ("australia", "Australia", "Tasmania"),
    "western-australia": ("australia", "Australia", "Western Australia"),
    "queensland": ("australia", "Australia", "Queensland"),
    "victoria": ("australia", "Australia", "Victoria"),
    "south-australia": ("australia", "Australia", "South Australia"),
    "aws-asia-pacific-sydney-ap-southeast-2": ("australia", "Australia", "Sydney"),
    # China (incl. Hong Kong)
    "hong-kong": ("china", "Hong Kong", "Hong Kong"),
    "shanghai": ("china", "China", "Shanghai"),
    "beijing": ("china", "China", "Beijing"),
    "guangzhou-foshan": ("china", "China", "Guangzhou"),
    "nanjing": ("china", "China", "Nanjing"),
    "xi-an": ("china", "China", "Xi'an"),
    "nantong": ("china", "China", "Nantong"),
    "harbin": ("china", "China", "Harbin"),
    "chengdu-chongqing": ("china", "China", ""),
    "inner-mongolia": ("china", "China", "Inner Mongolia"),
    "hebei": ("china", "China", "Hebei"),
    "shanxi": ("china", "China", "Shanxi"),
    "guizhou": ("china", "China", "Guizhou"),
    "hubei": ("china", "China", "Hubei"),
    "zhejiang": ("china", "China", "Zhejiang"),
    "jiangsu": ("china", "China", "Jiangsu"),
    "guangdong-province": ("china", "China", "Guangdong"),
    "china": ("china", "China", ""),
    "aws-china-beijing-cn-north-1": ("china", "China", "Beijing"),
    "aws-china-ningxia-cn-northwest-1": ("china", "China", "Zhongwei"),
    # Southeast Asia
    "singapore": ("southeast-asia", "Singapore", "Singapore"),
    "kuala-lumpur": ("southeast-asia", "Malaysia", "Kuala Lumpur"),
    "johor": ("southeast-asia", "Malaysia", "Johor"),
    "malacca": ("southeast-asia", "Malaysia", "Malacca"),
    "malaysia": ("southeast-asia", "Malaysia", ""),
    "sarawak": ("southeast-asia", "Malaysia", "Sarawak"),
    "sabah": ("southeast-asia", "Malaysia", "Sabah"),
    "jakarta": ("southeast-asia", "Indonesia", "Jakarta"),
    "batam": ("southeast-asia", "Indonesia", "Batam"),
    "surabaya": ("southeast-asia", "Indonesia", "Surabaya"),
    "bandung": ("southeast-asia", "Indonesia", "Bandung"),
    "pekanbaru": ("southeast-asia", "Indonesia", "Pekanbaru"),
    "bintan-island": ("southeast-asia", "Indonesia", "Bintan"),
    "indonesia": ("southeast-asia", "Indonesia", ""),
    "kalimantan": ("southeast-asia", "Indonesia", "Kalimantan"),
    "sumatra": ("southeast-asia", "Indonesia", "Sumatra"),
    "bali": ("southeast-asia", "Indonesia", "Bali"),
    "bangkok-krung-thep": ("southeast-asia", "Thailand", "Bangkok"),
    "thailand-prathet-thai": ("southeast-asia", "Thailand", ""),
    "ho-chi-minh-city": ("southeast-asia", "Vietnam", "Ho Chi Minh City"),
    "hanoi": ("southeast-asia", "Vietnam", "Hanoi"),
    "da-nang-ba80c72e-a9da-4658-90f2-5f85b2bcc0d3": (
        "southeast-asia", "Vietnam", "Da Nang"),
    "manila": ("southeast-asia", "Philippines", "Manila"),
    "cebu": ("southeast-asia", "Philippines", "Cebu"),
    "philippines": ("southeast-asia", "Philippines", ""),
    "pampanga": ("southeast-asia", "Philippines", "Pampanga"),
}

# Baxtel primary_stage -> our status. "default" rows carry no stage info and
# are skipped; landbank counts as announced (site secured, nothing built).
BAXTEL_STAGE_MAP = {
    "operational": "operational",
    "construction": "under_construction",
    "planned": "announced",
    "landbank": "announced",
    "withdrawn": "cancelled",
    "decommissioned": "cancelled",
    "indoubt": "on_hold",
}
# Rank for collapsing multi-stage duplicate points of the same site.
_BAXTEL_STAGE_RANK = {
    "operational": 5, "construction": 4, "planned": 3, "landbank": 2,
    "indoubt": 1, "withdrawn": 0, "decommissioned": 0,
}

# Baxtel categories that are real data center inventory. Dropped:
# "Real Estate" (land/estate plays, cf. DROP_OPERATORS policy), "Carrier"
# is kept (telco-owned DCs like SK Hyper Ulsan) but obvious cable landing
# stations / POPs are filtered by name below.
BAXTEL_DROP_CATEGORIES = {"Real Estate"}
_BAXTEL_JUNK_NAME_RE = re.compile(
    r"\b(CLS|cable landing|landing station|POP)\b", re.IGNORECASE)
# Baxtel anonymizes some sites as "<Category> Operator" / "<Category> Data
# Center" placeholders — they carry no usable operator and can never match
# tracked news, so they are dropped.
_BAXTEL_PLACEHOLDER_OP_RE = re.compile(
    r"(carrier-neutral|hyperscale|hpc|government|enterprise|subscale|crypto|"
    r"colocation|msp|energy)\s+operator$", re.IGNORECASE)

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
_VERCEL_CHECKPOINT_RE = re.compile(r"Vercel Security Checkpoint")
_BAXTEL_TOKEN_RE = re.compile(
    r'data-maps--spatial-map-mapbox-token-value="(pk\.[^"]+)"')

_WS_RE = re.compile(r"\s+")


def _clean_ws(text: str) -> str:
    return _WS_RE.sub(" ", text or "").strip()


# --------------------------------------------------------------------- http

class TransientHTTPError(Exception):
    """Retryable HTTP failure (rate limit, 5xx, bot checkpoint page)."""


def make_session():
    """curl_cffi session honoring the same env conventions as fetch.py."""
    from curl_cffi import requests as cr

    kwargs = {"impersonate": "chrome", "verify": True}
    proxy = os.environ.get("FETCH_PROXY")
    if proxy:
        kwargs["proxies"] = {"http": proxy, "https": proxy}
    if os.environ.get("FETCH_INSECURE_TLS", "").lower() in ("1", "true", "yes"):
        kwargs["verify"] = False
        log.warning("FETCH_INSECURE_TLS set: TLS verification disabled")
    return cr.Session(**kwargs)


class PoliteFetcher:
    """GET wrapper: minimum delay between requests, long backoff on 429/5xx/
    bot-checkpoint responses (DataCenterMap sits behind Vercel protection
    that throttles bursts; backing off 15-60s usually recovers)."""

    def __init__(self, session, delay: float = 1.2):
        self.session = session
        self.delay = delay
        self._last = 0.0
        self.requests = 0

    def get(self, url: str, timeout: int = 40) -> str:
        last_exc: Exception | None = None
        for attempt in range(4):
            wait = self._last + self.delay - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                resp = self.session.get(url, timeout=timeout)
                self._last = time.monotonic()
                self.requests += 1
                if resp.status_code in (429, 500, 502, 503):
                    raise TransientHTTPError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                if _VERCEL_CHECKPOINT_RE.search(resp.text):
                    raise TransientHTTPError("Vercel bot checkpoint")
                return resp.text
            except TransientHTTPError as exc:
                last_exc = exc
                backoff = 15 * (attempt + 1)
                log.warning("%s: %s — backing off %ds (attempt %d/4)",
                            url, exc, backoff, attempt + 1)
                time.sleep(backoff)
            except Exception as exc:  # noqa: BLE001 - transient, retry
                last_exc = exc
                self._last = time.monotonic()
                time.sleep(self.delay * (attempt + 1) * 2)
        raise last_exc

    def get_bytes(self, url: str, timeout: int = 60) -> bytes:
        wait = self._last + self.delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        resp = self.session.get(url, timeout=timeout)
        self._last = time.monotonic()
        self.requests += 1
        resp.raise_for_status()
        return resp.content


# --------------------------------------------------------------------- baxtel

def _baxtel_token(fetcher: PoliteFetcher) -> str:
    try:
        html = fetcher.get(BAXTEL_MAP_URL)
        m = _BAXTEL_TOKEN_RE.search(html)
        if m:
            return m.group(1)
        log.warning("baxtel: token not found on map page, using fallback")
    except Exception as exc:  # noqa: BLE001
        log.warning("baxtel: map page fetch failed (%s), using fallback token",
                    exc)
    return BAXTEL_TOKEN_FALLBACK


def _deg2tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    import math
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    lat_r = math.radians(lat)
    y = int((1 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2 * n)
    return x, y


def _tile_px_to_lonlat(px: float, py: float, z: int, tx: int, ty: int,
                       extent: int = 4096) -> tuple[float, float]:
    """Tile pixel -> lon/lat. NOTE: the baxtel_sites tileset is encoded with
    the Y axis flipped, so callers must pass (extent - py), not py."""
    import math
    n = 2 ** z
    lon = (tx + px / extent) / n * 360 - 180
    yy = (ty + py / extent) / n
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy))))
    return lon, lat


# z5 tiles covering the APAC scope bbox (lon 68..180, lat -48..56): ~300 m
# per tile pixel, precise enough for map pins. (The old z0 single-tile trick
# also carries every feature but snaps coordinates to a ~10 km grid.)
BAXTEL_TILE_Z = 5
_BAXTEL_SCOPE = (68.0, 180.0, -48.0, 56.0)  # lon_min, lon_max, lat_min, lat_max


def _baxtel_scope_tiles() -> list[tuple[int, int]]:
    lon0, lon1, lat0, lat1 = _BAXTEL_SCOPE
    x0, y0 = _deg2tile(lon0, lat1, BAXTEL_TILE_Z)  # north-west
    x1, y1 = _deg2tile(lon1, lat0, BAXTEL_TILE_Z)  # south-east
    return [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def scrape_baxtel(fetcher: PoliteFetcher) -> list[dict]:
    """Return project updates from Baxtel's public Mapbox tileset.

    Fetches the z5 tiles covering APAC (finer coordinate grid than the z0
    world tile) and keeps the per-site profile fields the tileset carries:
    exact-ish coordinates, category, company_type and the verbatim
    layer_stage ("Prospective Expansion", "Land Bank", ...)."""
    import mapbox_vector_tile

    token = _baxtel_token(fetcher)
    tiles = _baxtel_scope_tiles()
    log.info("baxtel: fetching %d z%d tiles for the APAC bbox",
             len(tiles), BAXTEL_TILE_Z)

    by_pid: dict[str, dict] = {}  # features repeat in tile buffers: dedupe
    for tx, ty in tiles:
        tile_url = (f"https://a.tiles.mapbox.com/v4/{BAXTEL_TILESET}"
                    f"/{BAXTEL_TILE_Z}/{tx}/{ty}.vector.pbf"
                    f"?access_token={token}")
        try:
            data = fetcher.get_bytes(tile_url)
        except Exception as exc:  # noqa: BLE001 - some edge tiles 404/empty
            log.info("baxtel: tile %d/%d skipped (%s)", tx, ty, exc)
            continue
        try:
            decoded = mapbox_vector_tile.decode(data)
        except Exception as exc:  # noqa: BLE001
            log.info("baxtel: tile %d/%d decode failed (%s)", tx, ty, exc)
            continue
        layer = decoded.get(BAXTEL_TILESET.split(".")[1])
        if not layer:
            continue
        extent = layer.get("extent", 4096)
        for f in layer["features"]:
            p = f["properties"]
            pid = p.get("public_id")
            if not pid:
                continue
            if pid in by_pid:
                # The same site point is emitted once per layer_stage (same
                # public_id) — e.g. an Operational pin plus a "Prospective
                # Expansion" pin. Dedupe keeps the first; remember the PE one.
                if p.get("layer_stage") == "Prospective Expansion":
                    by_pid[pid]["_pe_point"] = True
                continue
            geom = f.get("geometry") or {}
            coords = geom.get("coordinates") or []
            if geom.get("type") != "Point" or len(coords) < 2:
                continue
            # Tileset Y axis is flipped — see _tile_px_to_lonlat docstring.
            lon, lat = _tile_px_to_lonlat(coords[0], extent - coords[1],
                                          BAXTEL_TILE_Z, tx, ty, extent)
            p = dict(p)
            p["_lon"], p["_lat"] = round(lon, 4), round(lat, 4)
            by_pid[pid] = p
    log.info("baxtel: %d unique sites in APAC tiles", len(by_pid))

    skipped: dict[str, int] = {}
    best: dict[tuple, dict] = {}
    for p in by_pid.values():
        stage = p.get("primary_stage") or "default"
        if stage not in BAXTEL_STAGE_MAP:
            skipped[f"stage:{stage}"] = skipped.get(f"stage:{stage}", 0) + 1
            continue
        slug = p.get("region_slug") or ""
        if slug not in BAXTEL_REGIONS:
            skipped[f"region:{slug}"] = skipped.get(f"region:{slug}", 0) + 1
            continue
        category = p.get("category")
        if category in BAXTEL_DROP_CATEGORIES:
            skipped[f"category:{category}"] = skipped.get(f"category:{category}", 0) + 1
            continue
        name = _clean_ws(p.get("site_name") or "")
        operator = _clean_ws(p.get("company_name") or "")
        if (not name or not operator or _BAXTEL_JUNK_NAME_RE.search(name)
                or _BAXTEL_PLACEHOLDER_OP_RE.search(operator)):
            skipped["junk-name"] = skipped.get("junk-name", 0) + 1
            continue
        region, country, city = BAXTEL_REGIONS[slug]
        cap = p.get("total_power_mw")
        try:
            cap = float(cap) if cap else None
            if cap is not None and cap <= 0:
                cap = None
            if cap is not None and cap == int(cap):
                cap = int(cap)
        except (TypeError, ValueError):
            cap = None
        upd = {
            "story_id": "",
            "name": name,
            "operator": operator,
            "region": region,
            "country": country,
            "city": city,
            "capacity_mw": cap,
            "capacity_note": "total power per Baxtel" if cap is not None else None,
            "status": BAXTEL_STAGE_MAP[stage],
            "source": "baxtel",
            "lat": p["_lat"],
            "lon": p["_lon"],
            "category": _clean_ws(category or "") or None,
            "company_type": _clean_ws(p.get("company_type") or "") or None,
            # Source-verbatim stage label ("Operational", "Construction",
            # "Planned", "Prospective Expansion", "Land Bank", ...).
            "stage_detail": _clean_ws(p.get("layer_stage") or "") or None,
            "_slug": _baxtel_slug(name),  # for the optional detail crawl
            "_pe": (p.get("layer_stage") == "Prospective Expansion"
                    or bool(p.get("_pe_point"))),
        }
        # Same site can appear once per stage (operational + planned phases);
        # keep the most advanced stage's point. A "Prospective Expansion"
        # sibling row marks the site as expanding even when the kept row is
        # the operational one (Baxtel draws it as a separate map marker; we
        # flag the project instead of duplicating it).
        key = (projects._canon(name), projects._canon(operator), slug)
        cur = best.get(key)
        if cur is None or _BAXTEL_STAGE_RANK[stage] > _BAXTEL_STAGE_RANK[
                cur["_stage"]]:
            upd["_stage"] = stage
            upd["_pe_sibling"] = upd["_pe"] or (cur.get("_pe_sibling") if cur else False)
            best[key] = upd
        else:
            cur["_pe_sibling"] = cur.get("_pe_sibling") or p.get("layer_stage") == "Prospective Expansion"
    for upd in best.values():
        upd.pop("_stage", None)
        if upd.pop("_pe", False) or upd.pop("_pe_sibling", False):
            upd["expansion_planned"] = True
    log.info("baxtel: %d APAC sites after filtering (skipped: %s)",
             len(best), dict(sorted(skipped.items(),
                                    key=lambda kv: -kv[1])[:12]))
    return list(best.values())


def _baxtel_slug(name: str) -> str:
    """baxtel.com/data-center/<slug> — the slug is the site name lowercased,
    non-alphanumerics collapsed to dashes (verified ~90% hit rate)."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


_BAXTEL_YEAR_RE = re.compile(r"Year Built:\s*(\d{4})")
_BAXTEL_DESC_RE = re.compile(r"<p[^>]*>([^<]{60,800})</p>")


def baxtel_enrich_details(fetcher: PoliteFetcher, updates: list[dict],
                          cache: dict) -> None:
    """Crawl baxtel.com/data-center/<slug> for the two fields the tileset
    lacks: year_built and a short site description (first body paragraph).

    ``cache`` maps slug -> {"year_built": ..., "description": ...} (value may
    have nulls; a PRESENT key means "already crawled"). Saved by the caller's
    save_cache; results are merged into ``updates`` in place."""
    pending = [u for u in updates if u.get("_slug") and u["_slug"] not in cache]
    log.info("baxtel details: %d cached, %d to crawl",
             len(updates) - len(pending), len(pending))
    failures = 0
    for i, upd in enumerate(pending):
        slug = upd["_slug"]
        try:
            html = fetcher.get(f"https://baxtel.com/data-center/{slug}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            log.warning("baxtel detail %s failed: %s", slug, exc)
            if failures >= 15:
                log.error("baxtel details: %d consecutive failures — aborting; "
                          "re-run later to resume (cache kept)", failures)
                break
            continue
        failures = 0
        entry: dict = {"year_built": None, "description": None}
        m = _BAXTEL_YEAR_RE.search(html)
        if m:
            entry["year_built"] = m.group(1)
        # First substantial <p> on the page is the site's own blurb (the
        # company profile and news teasers follow it).
        body = html[html.find("<body"):]
        m = _BAXTEL_DESC_RE.search(body)
        if m:
            import html as html_mod
            desc = html_mod.unescape(_clean_ws(m.group(1)))
            entry["description"] = desc[:600] or None
        cache[slug] = entry
        if (i + 1) % 100 == 0:
            log.info("baxtel details: %d/%d crawled", i + 1, len(pending))
    for upd in updates:
        entry = cache.get(upd.get("_slug") or "")
        if entry:
            upd["year_built"] = entry.get("year_built")
            if entry.get("description"):
                upd["description"] = entry["description"]


# ------------------------------------------------------------- datacentermap

def _dcm_next_data(html: str) -> dict | None:
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _num(value) -> float | int | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return int(v) if v == int(v) else v


_CONSTRUCTION_RE = re.compile(
    r"under construction|being built|broke ground|construction", re.IGNORECASE)


def _dcm_specs(dc: dict) -> dict[str, str]:
    """Curated flat label -> value map from DCM's meta_* spec blocks (the
    "Specs" table on facility pages). Only non-empty values are kept."""
    out: dict[str, str] = {}
    metric = dc.get("metaunits") != "imperial"
    area_unit = "m²" if metric else "sq ft"

    cap = dc.get("meta_capacity") or {}
    size = _num(cap.get("buildingsize"))
    if size is not None:
        out["Building size"] = f"{size:,} {area_unit}"
    white = _num(cap.get("whitespace_builtout") or cap.get("whitespace_referenced"))
    if white is not None:
        out["Whitespace"] = f"{white:,} {area_unit}"

    bld = dc.get("meta_building") or {}
    floors = bld.get("floors")
    if floors:
        out["Floors"] = str(floors)
    if bld.get("construction"):
        out["Construction"] = str(bld["construction"])

    power = dc.get("meta_power") or {}
    if power.get("kw_rack"):
        out["Power per rack"] = f"{power['kw_rack']} kW"
    for key, label in (("ups_redundancy", "UPS redundancy"),
                       ("cooling_redundancy", "Cooling redundancy"),
                       ("standby_redundancy", "Standby redundancy")):
        if power.get(key):
            out[label] = str(power[key])

    std = dc.get("meta_standards") or {}
    tier = std.get("tier_designed")
    if tier and str(tier).isdigit():
        out["Tier (designed)"] = {1: "I", 2: "II", 3: "III", 4: "IV"}.get(int(tier), str(tier))
    certs = [name for key, name in (("pci", "PCI DSS"), ("soc1", "SOC 1"),
                                    ("soc2", "SOC 2"), ("soc3", "SOC 3"),
                                    ("iso9001", "ISO 9001"), ("iso14001", "ISO 14001"),
                                    ("iso27001", "ISO 27001"), ("iso50001", "ISO 50001"))
             if str(std.get(key)) == "1"]
    if certs:
        out["Certifications"] = " · ".join(certs)

    sec = dc.get("meta_security") or {}
    feats = []
    for key, label in (("staff", "24/7 staff"), ("guards", "24/7 guards"),
                       ("cctv", "CCTV"), ("keycard", "keycard"),
                       ("biometric", "biometric"),
                       ("firesupression", "fire suppression")):
        v = sec.get(key)
        if v == 1 or (isinstance(v, str) and v.lower().startswith("yes")):
            feats.append(label)
    if feats:
        out["Security"] = " · ".join(feats)
    return out


def _dcm_detail(fetcher: PoliteFetcher, url: str) -> dict:
    """Fetch one DCM facility page; return {status, capacity_mw,
    capacity_note, rfs, year_built, description} overrides. Raises when the
    page has no usable data (throttle/checkpoint) so the caller can count it
    as a failure."""
    import html as html_mod

    data = _dcm_next_data(fetcher.get(DCM_BASE + url))
    if not data:
        raise ValueError("no __NEXT_DATA__ in facility page")
    dc = data.get("props", {}).get("pageProps", {}).get("dc") or {}
    out: dict = {}
    stage = dc.get("stage")
    desc = _clean_ws(html_mod.unescape(re.sub(r"<[^>]+>", " ",
                                            dc.get("description") or "")))
    if desc:
        out["description"] = desc[:600]
    if stage == 2:
        out["status"] = "operational"
    elif stage == 1:
        out["status"] = ("under_construction" if _CONSTRUCTION_RE.search(desc)
                         else "announced")
    cap = dc.get("meta_capacity") or {}
    mw = _num(cap.get("mw_builtout"))
    if mw is not None:
        out["capacity_mw"] = mw
        out["capacity_note"] = "built-out capacity per DataCenterMap"
    else:
        mw = _num(cap.get("mw_pipeline"))
        if mw is not None:
            out["capacity_mw"] = mw
            out["capacity_note"] = "pipeline capacity per DataCenterMap"
    year = (dc.get("meta_building") or {}).get("year_operational")
    if year and str(year).isdigit():
        if stage == 1:
            out["rfs"] = str(year)          # expected year of operation
        elif stage == 2:
            out["year_built"] = str(year)   # year commissioned
    specs = _dcm_specs(dc)
    if specs:
        out["specs"] = specs
    if dc.get("capacitytype"):
        out["category"] = str(dc["capacitytype"])
    return out


class ScrapeAborted(Exception):
    """Too many consecutive failures — almost certainly being throttled.
    Cached countries are kept; re-run later to resume."""


def _dcm_country(fetcher: PoliteFetcher, cslug: str, region: str,
                 country: str, details: bool,
                 watchdog: list[int]) -> tuple[list[dict], int]:
    """Scrape all markets of one DCM country. Returns (updates, failures);
    the caller caches the country only when failures == 0."""
    updates: dict[str, dict] = {}  # keyed by facility URL (cross-market dupes)
    failures = 0
    data = _dcm_next_data(fetcher.get(f"{DCM_BASE}/{cslug}/"))
    if data is None:
        raise TransientHTTPError(f"country page {cslug}: no __NEXT_DATA__")
    geos = data.get("props", {}).get("pageProps", {}) \
        .get("mapdata", {}).get("geos") or []
    cities = [(g["properties"]["link"], g["properties"]["name"])
              for g in geos if g.get("properties", {}).get("link")]
    log.info("datacentermap/%s: %d markets", cslug, len(cities))
    for clink, cname in cities:
        try:
            cdata = _dcm_next_data(fetcher.get(f"{DCM_BASE}/{cslug}/{clink}/"))
            if cdata is None:
                raise ValueError("no __NEXT_DATA__ in city page")
            watchdog[0] = 0
        except Exception as exc:  # noqa: BLE001
            failures += 1
            watchdog[0] += 1
            log.warning("datacentermap/%s/%s: city page failed: %s",
                        cslug, clink, exc)
            if watchdog[0] >= 15:
                raise ScrapeAborted(f"{watchdog[0]} consecutive failures")
            continue
        dcs = cdata.get("props", {}).get("pageProps", {}) \
            .get("mapdata", {}).get("dcs") or []
        for feat in dcs:
            p = feat.get("properties", {})
            if p.get("listingtype") not in ("Facility", "Campus"):
                continue
            if (p.get("country") or "") != country:
                continue  # cross-border neighbor shown on this market map
            name = _clean_ws(p.get("name") or "")
            operator = _clean_ws(p.get("companyname") or "")
            url = p.get("url") or ""
            if not (name and operator and url) or url in updates:
                continue
            # Exact facility coordinates from the market map geometry (some
            # features carry [null, null] — treat as unknown).
            coords = (feat.get("geometry") or {}).get("coordinates") or []
            lon = (round(coords[0], 5) if len(coords) >= 2
                   and isinstance(coords[0], (int, float)) else None)
            lat = (round(coords[1], 5) if len(coords) >= 2
                   and isinstance(coords[1], (int, float)) else None)
            upd = {
                "story_id": "",
                "name": name,
                "operator": operator,
                "region": region,
                "country": country,
                # City stays the market name: re-imports must keep matching
                # the entries created by earlier runs (precise location is
                # carried by lat/lon, not by the city string).
                "city": _clean_ws(cname),
                "capacity_mw": None,
                "capacity_note": None,
                "status": "operational",
                "source": "datacentermap",
                "lat": lat,
                "lon": lon,
            }
            if details:
                try:
                    upd.update(_dcm_detail(fetcher, url))
                    watchdog[0] = 0
                except Exception as exc:  # noqa: BLE001
                    failures += 1
                    watchdog[0] += 1
                    log.warning("datacentermap detail %s failed: %s", url, exc)
                    if watchdog[0] >= 15:
                        raise ScrapeAborted(
                            f"{watchdog[0]} consecutive failures")
            updates[url] = upd
    expected = sum(g.get("properties", {}).get("datacenters") or 0 for g in geos)
    if expected > 0 and not updates:
        # DCM sometimes serves bot-variant pages that parse fine but carry
        # empty mapdata.dcs. Never cache such a country — fail it so a later
        # run re-fetches (the country cache only stores zero-failure results,
        # and an empty cache entry would look "complete").
        raise TransientHTTPError(
            f"{cslug}: 0 facilities scraped but {expected} advertised on the "
            "country map — likely bot-variant pages")
    log.info("datacentermap/%s: %d facilities, %d failures",
             cslug, len(updates), failures)
    return list(updates.values()), failures


def scrape_datacentermap(fetcher: PoliteFetcher, details: bool = True,
                         cache: dict | None = None) -> list[dict]:
    """Return project updates from DataCenterMap country/city directories.

    ``cache`` maps country slug -> updates from a previous run; countries
    already in the cache are not re-fetched. A country is added to the cache
    only when it scraped with zero failures, so a throttled run can simply
    be re-run later to fill the gaps. The cache dict is saved after every
    country."""
    if cache is None:
        cache = {}
    watchdog = [0]  # consecutive fetch failures across countries
    for cslug, (region, country) in DCM_COUNTRIES.items():
        if cslug in cache:
            log.info("datacentermap/%s: %d facilities from cache",
                     cslug, len(cache[cslug]))
            continue
        try:
            updates, failures = _dcm_country(fetcher, cslug, region, country,
                                             details, watchdog)
        except ScrapeAborted as exc:
            log.error("datacentermap: aborting scrape (%s) — likely being "
                      "throttled. Completed countries are cached; re-run "
                      "later to resume.", exc)
            break
        except Exception as exc:  # noqa: BLE001 - country page itself failed
            log.warning("datacentermap/%s: country scrape failed: %s",
                        cslug, exc)
            continue
        if failures == 0:
            cache[cslug] = updates
            save_cache("datacentermap", cache)
        else:
            log.warning("datacentermap/%s: %d failures — NOT cached, re-run "
                        "later to retry this country", cslug, failures)
    return [u for ups in cache.values() for u in ups]


# -------------------------------------------------------------------- merge

def _strong_name_match(a: str, b: str) -> bool:
    return projects._canon(a) == projects._canon(b)


def _digit_tokens(name: str) -> set[str]:
    """Tokens containing digits identify individual buildings/phases
    ("tok1", "dc7", "3") — two facilities whose digit tokens differ are
    different sites even when the rest of the name overlaps."""
    return {t for t in projects._name_tokens(name)
            if any(c.isdigit() for c in t)}


def _import_name_match(a: str, b: str) -> bool:
    """Strict directory-import name match: one name's tokens must be a subset
    of the other's AND the digit-bearing tokens must be identical. Plain
    Jaccard is too loose here — "MC Digital Tokyo Connected Campus" vs
    "MC Digital Osaka Connected Campus" scores 0.5, and "China Mobile
    Chongqing 5" vs "China Mobile Hohhot 5" scores 0.6."""
    ta, tb = projects._name_tokens(a), projects._name_tokens(b)
    if not ta or not tb or not (ta <= tb or tb <= ta):
        return False
    return _digit_tokens(a) == _digit_tokens(b)


def _city_compatible(a: str, b: str) -> bool:
    """Cities agree when one contains the other ("Johor" vs "Johor Bahru
    (Iskandar Puteri)"); two different non-empty cities block a merge."""
    a, b = (a or "").lower().strip(), (b or "").lower().strip()
    if not a or not b:
        return True
    return a in b or b in a


def merge_updates(db: dict, updates: list[dict],
                  today: str | None = None) -> tuple[int, int]:
    """Strict-merge directory updates into the project DB.

    Looser matching (projects.upsert_projects) is fine for news, where one
    story usually describes one project, but directory rows are one facility
    each and operators run many numbered facilities per metro. A merge here
    requires the same canonical operator, a subset name match with identical
    digit tokens, and compatible cities. Created entries are marked seed=true
    (directory data is not confirmed by tracked news).
    """
    today = today or datetime.now(timezone.utc).date().isoformat()
    plist = db.setdefault("projects", [])
    created = updated = 0
    for raw in updates:
        upd = projects._validate_update(raw)
        if upd is None:
            log.warning("rejected invalid update: %s",
                        {k: raw.get(k) for k in ("name", "operator", "region")})
            continue
        upd["source"] = raw.get("source")
        upd["operator"] = projects.canonical_operator(upd["operator"])
        op = projects._canon(upd["operator"])
        matches = [p for p in plist
                   if projects._canon(projects.canonical_operator(p.get("operator", ""))) == op
                   and _import_name_match(p.get("name", ""), upd["name"])
                   and _city_compatible(p.get("city", ""), upd["city"])]
        match = None
        if len(matches) == 1:
            match = matches[0]
        elif len(matches) > 1:
            exact = [p for p in matches
                     if _strong_name_match(p.get("name", ""), upd["name"])]
            if len(exact) == 1:
                match = exact[0]
        if match is not None:
            # Directory data must never bump last_updated — that date marks
            # the last news-linked change only.
            projects._apply_update(match, upd, today, bump_updated=False)
            updated += 1
            log.info("merged into %s: %s [%s]", match["id"], upd["name"],
                     upd["source"])
        else:
            project = projects._new_project(upd, today,
                                            {p["id"] for p in plist})
            project["seed"] = True  # directory import, not news-confirmed
            plist.append(project)
            created += 1
    return created, updated


# -------------------------------------------------------------------- cache

def _cache_path(source: str) -> Path:
    return CACHE_DIR / f"import_cache_{source}.json"


def load_cached(source: str) -> list[dict] | None:
    path = _cache_path(source)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_cache(source: str, updates) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(source).write_text(
        json.dumps(updates, ensure_ascii=False, indent=1), encoding="utf-8")


# --------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sources", default="baxtel,datacentermap",
                    help="comma-separated: baxtel, datacentermap")
    ap.add_argument("--no-details", action="store_true",
                    help="datacentermap: skip facility detail pages (faster, "
                         "no MW/status detail)")
    ap.add_argument("--baxtel-details", action="store_true",
                    help="baxtel: crawl each site page for year_built and a "
                         "short description (slow, ~1 request/site)")
    ap.add_argument("--delay", type=float, default=1.2,
                    help="min seconds between HTTP requests (default 1.2)")
    ap.add_argument("--refresh", action="store_true",
                    help="ignore the scrape cache and re-fetch")
    ap.add_argument("--dry-run", action="store_true",
                    help="scrape and report, do not write projects.json")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap updates per source (testing)")
    ap.add_argument("--path", type=Path, default=projects.PROJECTS_PATH)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    session = make_session()
    fetcher = PoliteFetcher(session, delay=args.delay)

    wanted = [s.strip() for s in args.sources.split(",") if s.strip()]
    all_updates: list[dict] = []
    for source in wanted:
        cached = None if args.refresh else load_cached(source)
        if source == "baxtel":
            if cached is None:
                updates = scrape_baxtel(fetcher)
                save_cache(source, updates)
            else:
                log.info("baxtel: loaded %d updates from cache", len(cached))
                updates = cached
            if args.baxtel_details:
                details_cache = load_cached("baxtel_details") or {}
                if not isinstance(details_cache, dict):
                    details_cache = {}
                baxtel_enrich_details(fetcher, updates, details_cache)
                save_cache("baxtel_details", details_cache)
        elif source == "datacentermap":
            # Per-country cache: a throttled run keeps completed countries
            # and re-fetches only the gaps on the next run.
            cache = cached if isinstance(cached, dict) else {}
            updates = scrape_datacentermap(
                fetcher, details=not args.no_details, cache=cache)
        else:
            raise SystemExit(f"unknown source: {source}")
        if args.limit:
            updates = updates[: args.limit]
        all_updates.extend(updates)

    if args.dry_run:
        db = projects.load_projects(args.path)
        import copy
        db = copy.deepcopy(db)
    else:
        db = projects.load_projects(args.path)
    before = len(db["projects"])
    created, updated = merge_updates(db, all_updates)
    total = len(db["projects"])

    # ---- stats
    from collections import Counter
    by_src = Counter(u.get("source") for u in all_updates)
    by_src_region = Counter((u.get("source"), u["region"]) for u in all_updates)
    by_status = Counter(u["status"] for u in all_updates)
    with_mw = sum(1 for u in all_updates if u.get("capacity_mw") is not None)
    print("\n==== import stats ====")
    print(f"HTTP requests made : {fetcher.requests}")
    print(f"updates by source  : {dict(by_src)}")
    print(f"updates by status  : {dict(by_status)}")
    print(f"updates with MW    : {with_mw}/{len(all_updates)}")
    print("updates by source/region:")
    for (src, reg), n in sorted(by_src_region.items()):
        print(f"  {src:14s} {reg:15s} {n}")
    print(f"DB: {before} projects -> {total} "
          f"(created {created}, merged/updated {updated})")
    if args.dry_run:
        print("dry-run: projects.json NOT written")
    else:
        projects.save_projects(db, args.path)
        print(f"written: {args.path}")


if __name__ == "__main__":
    main()
