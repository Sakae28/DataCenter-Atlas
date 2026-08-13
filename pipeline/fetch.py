"""Fetch raw candidate news items from the sources registry.

Reads pipeline/sources.yaml, fetches each source (RSS feed or Google News
RSS search), normalizes entries and keeps only items published within the
last 48 hours. One failing source never kills the run.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.parse import quote_plus

import feedparser
import httpx
import yaml

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
TIMEOUT_S = 15
MAX_AGE = timedelta(hours=48)

SOURCES_PATH = Path(__file__).parent / "sources.yaml"

_TAG_RE = re.compile(r"<[^>]+>")


def load_sources(path: Path = SOURCES_PATH) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)["sources"]


def google_news_url(query: str) -> str:
    return (
        "https://news.google.com/rss/search?q="
        + quote_plus(query)
        + "&hl=en-US&gl=US&ceid=US:en"
    )


def clean_text(html: str | None) -> str:
    if not html:
        return ""
    text = unescape(_TAG_RE.sub(" ", html))
    return re.sub(r"\s+", " ", text).strip()


def parse_published(entry) -> datetime | None:
    """Best-effort parse of an entry's published/updated timestamp to UTC."""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        st = getattr(entry, key, None)
        if st:
            return datetime(*st[:6], tzinfo=timezone.utc)
    for key in ("published", "updated"):
        raw = entry.get(key)
        if raw:
            try:
                from email.utils import parsedate_to_datetime

                dt = parsedate_to_datetime(raw)
                return dt.astimezone(timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def normalize_item(entry, source: dict) -> dict | None:
    title = clean_text(entry.get("title"))
    url = entry.get("link")
    if not title or not url:
        return None
    published = parse_published(entry)
    snippet = clean_text(entry.get("summary") or entry.get("description"))
    if len(snippet) > 500:
        snippet = snippet[:500].rsplit(" ", 1)[0] + "…"
    return {
        "title": title,
        "url": url,
        "source": source["name"],
        "source_type": source["type"],
        "source_weight": source.get("weight", 1),
        "regions": list(source.get("regions", [])),
        "published_at": published.isoformat().replace("+00:00", "Z") if published else None,
        "snippet": snippet,
    }


def fetch_source(source: dict, client: httpx.Client) -> list[dict]:
    url = source["url"] if source["type"] == "rss" else google_news_url(source["query"])
    resp = client.get(url)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    cutoff = datetime.now(timezone.utc) - MAX_AGE
    items = []
    for entry in feed.entries:
        item = normalize_item(entry, source)
        if item is None:
            continue
        # Keep only items published within the last 48h. Items with no
        # parseable timestamp are kept (some feeds omit dates) but flagged.
        if item["published_at"]:
            dt = datetime.fromisoformat(item["published_at"].replace("Z", "+00:00"))
            if dt < cutoff:
                continue
        items.append(item)
    return items


def make_client() -> httpx.Client:
    """HTTP client honoring env-based proxy/TLS settings.

    - FETCH_PROXY: explicit proxy URL (e.g. http://127.0.0.1:7078). When
      unset, httpx's trust_env picks up HTTP_PROXY/HTTPS_PROXY if present.
    - FETCH_INSECURE_TLS=1: skip TLS verification. Needed when a local
      accelerator MITMs HTTPS with its own CA. Never enable in CI.
    """
    kwargs: dict = {
        "headers": {"User-Agent": USER_AGENT},
        "timeout": TIMEOUT_S,
        "follow_redirects": True,
        "trust_env": True,
    }
    proxy = os.environ.get("FETCH_PROXY")
    if proxy:
        kwargs["proxy"] = proxy
    if os.environ.get("FETCH_INSECURE_TLS", "").lower() in ("1", "true", "yes"):
        kwargs["verify"] = False
        log.warning("FETCH_INSECURE_TLS set: TLS verification disabled")
    return httpx.Client(**kwargs)


def fetch_all(sources: list[dict] | None = None) -> tuple[list[dict], list[str]]:
    """Fetch every source. Returns (items, failed_source_names)."""
    if sources is None:
        sources = load_sources()
    items: list[dict] = []
    failed: list[str] = []
    with make_client() as client:
        for source in sources:
            try:
                got = fetch_source(source, client)
                log.info("%s: %d items", source["name"], len(got))
                items.extend(got)
            except Exception as exc:  # noqa: BLE001 - never kill the run
                log.warning("%s: fetch failed: %s", source["name"], exc)
                failed.append(source["name"])
    return items, failed


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    all_items, failures = fetch_all()
    print(f"{len(all_items)} items, {len(failures)} failed sources: {failures}")
