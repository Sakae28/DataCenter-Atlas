"""One-off: upgrade date-only stories (published_precision == "day") to the
real publish time exposed by the article page's metadata.

The feeds for archive/search-style sources (DCD archive, Edge Malaysia
search, AFR topic, ...) carry only a date, but the article pages themselves
usually have a full timestamp (article:published_time, JSON-LD
datePublished). For every stored story still marked day-only we fetch the
page, read trafilatura's metadata date, and — when it has a time-of-day
whose UTC date agrees with the feed date — replace the noon-pinned
published_at and drop the precision flag.

Usage: FETCH_PROXY=http://127.0.0.1:7078 python backfill_times.py [YYYY-MM-DD ...]
Defaults to every data/news/*.json. Safe to re-run: stories already
upgraded are skipped, mismatched metadata is rejected by apply_publish_time.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import trafilatura

import fulltext
from fetch import make_client

log = logging.getLogger("backfill_times")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "news"


def main() -> None:
    dates = sys.argv[1:]
    paths = [DATA_DIR / f"{d}.json" for d in dates] if dates else sorted(DATA_DIR.glob("*.json"))
    total_upgraded = total_left = 0
    with make_client() as client:
        for path in paths:
            data = json.loads(path.read_text(encoding="utf-8"))
            targets = [s for s in data.get("stories", []) if s.get("published_precision") == "day"]
            if not targets:
                continue
            upgraded = 0
            for story in targets:
                url = story.get("content_url") or story["sources"][0]["url"]
                if "news.google.com" in url:
                    decoded = fulltext.resolve_google_news(url)
                    if not decoded:
                        continue
                    url = decoded
                try:
                    raw = fulltext.fetch_page(client, url)
                    iso = fulltext.html_publish_iso(fulltext.decode_html(raw))
                    if not iso:
                        html = trafilatura.load_html(fulltext.decode_html(raw))
                        meta = trafilatura.extract_metadata(html) if html is not None else None
                        iso = fulltext.meta_publish_iso(meta)
                except Exception as exc:  # noqa: BLE001 - one story never kills the run
                    log.info("time backfill failed %s: %s", story["id"], exc)
                    continue
                if fulltext.apply_publish_time(story, iso):
                    upgraded += 1
            if upgraded:
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                                encoding="utf-8")
            total_upgraded += upgraded
            total_left += len(targets) - upgraded
            print(f"{path.name}: {upgraded}/{len(targets)} upgraded")
    print(f"TOTAL: {total_upgraded} upgraded, {total_left} still date-only")


if __name__ == "__main__":
    main()
