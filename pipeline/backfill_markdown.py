"""One-off backfill: re-extract story content as Markdown.

Re-fetches every story in the given day files that already has full-text
content (plain text from the old txt extractor) and replaces it with the
Markdown extraction introduced in fulltext.py. The story's `content_url` is
used as the fetch URL when present (it avoids re-decoding Google News
links); otherwise the primary source URL is used. Stories whose page fails
to fetch or extract (e.g. DCD pages return Cloudflare 403) keep their
existing content untouched.

Usage:
    FETCH_PROXY=http://127.0.0.1:7078 FETCH_INSECURE_TLS=1 \
        pipeline/.venv/Scripts/python pipeline/backfill_markdown.py [day files...]

Defaults to data/news/2026-08-13.json and data/news/2026-08-14.json.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import trafilatura

from fetch import make_client
from fulltext import MIN_CONTENT_CHARS, clean_text

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FILES = [
    ROOT / "data/news/2026-08-13.json",
    ROOT / "data/news/2026-08-14.json",
]


def fetch_markdown(client, url: str) -> str | None:
    """Fetch url and extract the article body as Markdown. None on failure."""
    import httpx

    try:
        resp = client.get(url)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.info("fetch failed for %s: %s", url, exc)
        return None
    html = trafilatura.load_html(resp.content)
    if html is None:
        return None
    text = trafilatura.extract(
        html,
        output_format="markdown",
        include_comments=False,
        include_tables=False,
        include_links=False,
        include_images=True,
    )
    if not text:
        return None
    content = clean_text(text)
    if len(content) < MIN_CONTENT_CHARS:
        return None
    return content


def backfill_file(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    stories = data["stories"]
    stats = {"total": len(stories), "had_content": 0, "updated": 0, "failed": 0, "no_content": 0}
    with make_client() as client:
        for story in stories:
            if not story.get("content") and not story.get("content_url"):
                stats["no_content"] += 1
                continue
            stats["had_content"] += 1
            url = story.get("content_url") or story["sources"][0]["url"]
            try:
                content = fetch_markdown(client, url)
            except Exception as exc:  # noqa: BLE001 - one story never kills the run
                log.info("backfill failed for %s: %s", story.get("id"), exc)
                content = None
            if content:
                story["content"] = content
                stats["updated"] += 1
            else:
                stats["failed"] += 1
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return stats


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    files = [Path(p) for p in sys.argv[1:]] or DEFAULT_FILES
    for path in files:
        stats = backfill_file(path)
        print(
            f"{path.name}: {stats['total']} stories, {stats['had_content']} with content, "
            f"{stats['updated']} re-extracted as markdown, {stats['failed']} kept old content "
            f"(fetch/extract failed), {stats['no_content']} without content untouched"
        )


if __name__ == "__main__":
    main()
