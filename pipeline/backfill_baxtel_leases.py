"""Backfill the Baxtel "This site is leased from <Op> in the <Host> building"
notice into the baxtel details cache.

The notice sits outside the blurb we originally scraped, and it links
directly to the host facility's Baxtel page — so we capture the host's slug
too, which makes folding a leased deployment into its host exact (no fuzzy
matching needed).

Resumable: cache entries that already carry a "lease" key (null or object)
are skipped; failed fetches leave the key absent so the next run retries.

    python backfill_baxtel_leases.py [--delay 1.2] [--limit N]
"""

import argparse
import html as html_mod
import json
import logging
import re
from pathlib import Path

from import_external import make_session, PoliteFetcher, _clean_ws

log = logging.getLogger("backfill_baxtel_leases")

CACHE_DIR = Path(__file__).parent / ".scratch"
UPDATES_CACHE = CACHE_DIR / "import_cache_baxtel.json"
DETAILS_CACHE = CACHE_DIR / "import_cache_baxtel_details.json"

_BAXTEL_LEASE_RE = re.compile(
    r"This site is leased from\s*(?P<who>.*?)\s*in the\s*"
    r"<a[^>]*href=\"/data-center/(?P<slug>[^\"]+)\"[^>]*>"
    r"(?P<site>[^<]+)</a>\s*building",
    re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def parse_lease(html: str) -> dict | None:
    m = _BAXTEL_LEASE_RE.search(html)
    if not m:
        return None
    who = _clean_ws(html_mod.unescape(_TAG_RE.sub(" ", m.group("who"))))
    site = _clean_ws(html_mod.unescape(m.group("site")))
    return {"operator": who or None, "site": site or None, "slug": m.group("slug")}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--delay", type=float, default=1.2)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    updates = json.loads(UPDATES_CACHE.read_text(encoding="utf-8"))
    details = json.loads(DETAILS_CACHE.read_text(encoding="utf-8"))
    slugs = [u["_slug"] for u in updates if u.get("_slug")]
    pending = [s for s in slugs if "lease" not in details.get(s, {})]
    if args.limit:
        pending = pending[: args.limit]
    log.info("%d slugs total, %d pending", len(slugs), len(pending))

    fetcher = PoliteFetcher(make_session(), delay=args.delay)
    failures = 0
    for i, slug in enumerate(pending):
        try:
            html = fetcher.get(f"https://baxtel.com/data-center/{slug}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            log.warning("%s failed: %s", slug, exc)
            if failures >= 15:
                log.error("%d consecutive failures — aborting; re-run resumes",
                          failures)
                break
            continue
        failures = 0
        entry = details.setdefault(slug, {"year_built": None, "description": None})
        entry["lease"] = parse_lease(html)
        if (i + 1) % 100 == 0:
            log.info("%d/%d crawled", i + 1, len(pending))
            DETAILS_CACHE.write_text(
                json.dumps(details, ensure_ascii=False, indent=1),
                encoding="utf-8")

    DETAILS_CACHE.write_text(
        json.dumps(details, ensure_ascii=False, indent=1), encoding="utf-8")
    leased = sum(1 for e in details.values() if e.get("lease"))
    checked = sum(1 for e in details.values() if "lease" in e)
    print(f"checked {checked}/{len(slugs)}, leased sites found: {leased}")
    print(f"requests: {fetcher.requests}")


if __name__ == "__main__":
    main()
