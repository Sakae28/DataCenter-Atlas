"""Fetch raw candidate news items from the sources registry.

Reads pipeline/sources.yaml, fetches each source (RSS feed, Google News
RSS search, or an HTML list page), normalizes entries and keeps only items
published within the last 48 hours. One failing source never kills the run.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.parse import quote_plus, urljoin

import feedparser
import httpx
import yaml

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
TIMEOUT_S = 15
# Item age window. Default 48h; backfills override via FETCH_MAX_AGE_HOURS.
MAX_AGE = timedelta(hours=int(os.environ.get("FETCH_MAX_AGE_HOURS", "48")))

SOURCES_PATH = Path(__file__).parent / "sources.yaml"

_TAG_RE = re.compile(r"<[^>]+>")


def load_sources(path: Path = SOURCES_PATH) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)["sources"]


def google_news_url(query: str, hl: str = "en-US", gl: str = "US", ceid: str = "US:en") -> str:
    return (
        "https://news.google.com/rss/search?q="
        + quote_plus(query)
        + f"&hl={hl}&gl={gl}&ceid={ceid}"
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
    if source["type"] == "html-list":
        return fetch_html_list(source, client)
    url = source["url"] if source["type"] == "rss" else google_news_url(
        source["query"],
        hl=source.get("hl", "en-US"),
        gl=source.get("gl", "US"),
        ceid=source.get("ceid", "US:en"),
    )
    resp = client.get(url)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    items = []
    for entry in feed.entries:
        item = normalize_item(entry, source)
        if item is None:
            continue
        if fresh_enough(item):
            items.append(item)
    return items


def fresh_enough(item: dict) -> bool:
    """Keep only items published within the last 48h. Items with no
    parseable timestamp are kept (some feeds/pages omit dates); for
    html-list sources the longer cross-day dedup in run.py is the real
    repeat guard."""
    if not item["published_at"]:
        return True
    cutoff = datetime.now(timezone.utc) - MAX_AGE
    dt = datetime.fromisoformat(item["published_at"].replace("Z", "+00:00"))
    return dt >= cutoff


def make_item(title: str, url: str, source: dict,
              published: datetime | None = None, snippet: str = "") -> dict | None:
    title = clean_text(title)
    if not title or not url:
        return None
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


_DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y%m%d", "%d %B %Y")


def parse_date_raw(raw: str | None) -> datetime | None:
    """Parse a date scraped off an HTML page: epoch ms/s, ISO, or one of
    _DATE_FORMATS. Naive values are assumed UTC."""
    if not raw:
        return None
    raw = raw.strip()
    if re.fullmatch(r"\d{13}", raw):
        return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)
    if re.fullmatch(r"\d{10}", raw):
        return datetime.fromtimestamp(int(raw), tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def decode_body(resp: httpx.Response, forced: str | None = None) -> str:
    """Decode a response body, honoring an explicit per-source encoding or
    a <meta charset> (e.g. GBK on older Chinese sites) over HTTP headers."""
    if forced:
        return resp.content.decode(forced, errors="replace")
    head = resp.content[:4096].decode("ascii", errors="ignore").lower()
    m = re.search(r'charset=["\']?([a-z0-9_-]+)', head)
    if m:
        enc = m.group(1)
        if enc in ("gbk", "gb2312"):
            enc = "gb18030"
        try:
            return resp.content.decode(enc, errors="replace")
        except (LookupError, ValueError):
            pass
    return resp.text


def _json_unescape(text: str) -> str:
    """Decode JSON-style escapes (\\u2019, \\") in strings scraped from
    embedded JSON blobs."""
    try:
        return json.loads(f'"{text}"')
    except ValueError:
        return text


def decode_turbo_stream(text: str) -> list[dict]:
    """Decode React Router / Remix turbo-stream payloads embedded in an
    SSR page and return every object that has string "title" and "link"
    fields (i.e. article cards). The article list of such pages exists
    only inside window.__reactRouterContext.streamController.enqueue(...)
    chunks: a JSON array where objects are stored as {"_k": v} index
    references into the array itself."""
    out: list[dict] = []
    chunks = re.findall(r'streamController\.enqueue\("((?:[^"\\]|\\.)*)"\)', text)
    for chunk in chunks:
        try:
            arr = json.loads(json.loads(f'"{chunk}"'))
        except ValueError:
            continue
        if not isinstance(arr, list):
            continue
        memo: dict[int, object] = {}

        def resolve(idx):
            if idx in memo:
                return memo[idx]
            v = arr[idx]
            if isinstance(v, dict):
                r = {}
                memo[idx] = r  # before recursion, in case of cycles
                for k, v2 in v.items():
                    key = arr[int(k[1:])] if k.startswith("_") else k
                    r[key] = resolve(v2) if isinstance(v2, int) and v2 >= 0 else None
            elif isinstance(v, list):
                r = [resolve(x) if isinstance(x, int) and 0 <= x < len(arr) else None
                     for x in v]
                memo[idx] = r
            else:
                return v
            return r

        def walk(o):
            if isinstance(o, dict):
                if isinstance(o.get("title"), str) and isinstance(o.get("link"), str):
                    out.append(o)
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        try:
            walk(resolve(0))
        except (RecursionError, IndexError, KeyError, ValueError):
            continue
    return out


def fetch_html_list(source: dict, client: httpx.Client) -> list[dict]:
    """Scrape a list/topic page for article links + titles (+dates when
    available). Two extraction styles, configured per source in
    sources.yaml:

    - link_pattern: regex over the raw body with named groups (url) or
      (id) + optional (title)/(date); for pages whose list only exists in
      embedded JSON, not in the DOM. url_template builds the URL from id.
    - turbo_stream: decode React Router SSR stream payloads and collect
      embedded {title, link} article objects (optional url_pattern
      whitelist on the link).
    - otherwise DOM mode: item_selector containers, link_selector anchors,
      url_pattern whitelist on the absolute href, optional date_selector
      element or date_from: url + date_pattern.
    """
    resp = client.get(source["url"])
    resp.raise_for_status()
    texts = [decode_body(resp, source.get("encoding"))]
    # Optional pagination: page_url carries a {page} placeholder; pages is
    # the total page count including page 1 (e.g. pages: 3 -> url, then
    # page_url with page=2 and page=3). For archive-style sources whose RSS
    # window is shorter than a long weekend.
    if source.get("page_url") and source.get("pages", 1) > 1:
        for page in range(2, int(source["pages"]) + 1):
            try:
                r = client.get(source["page_url"].format(page=page))
                r.raise_for_status()
                texts.append(decode_body(r, source.get("encoding")))
            except Exception as exc:  # noqa: BLE001
                log.warning("%s: page %d fetch failed: %s",
                            source["name"], page, exc)
                break
    base = source["url"]
    found: dict[str, dict] = {}  # url -> item (first/longest title wins)

    def add(url: str | None, title: str | None, date_raw: str | None,
            snippet: str = "") -> None:
        if not url:
            return
        url = urljoin(base, url)
        title = clean_text(title or "")
        old = found.get(url)
        if old is not None and len(old["title"]) >= len(title):
            return
        item = make_item(title, url, source, parse_date_raw(date_raw),
                         clean_text(_json_unescape(snippet)) if snippet else "")
        if item is not None:
            found[url] = item

    for text in texts:
        if source.get("turbo_stream"):
            url_re = re.compile(source.get("url_pattern", "."))
            for obj in decode_turbo_stream(text):
                if url_re.search(obj["link"]):
                    add(obj["link"], obj["title"], obj.get("date"))
        elif source.get("link_pattern"):
            pattern = re.compile(source["link_pattern"])
            for m in pattern.finditer(text):
                gd = m.groupdict()
                url = gd.get("url")
                if not url and gd.get("id") and source.get("url_template"):
                    url = source["url_template"].format(id=gd["id"])
                title = _json_unescape(gd["title"]) if gd.get("title") else None
                add(url, title, gd.get("date"), gd.get("snippet") or "")
        else:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(text, "html.parser")
            containers = (soup.select(source["item_selector"])
                          if source.get("item_selector") else [soup])
            url_re = re.compile(source.get("url_pattern", "."))
            date_from_url = source.get("date_from") == "url"
            date_re = re.compile(source["date_pattern"]) if source.get("date_pattern") else None
            for container in containers:
                date_raw = None
                if source.get("date_selector"):
                    el = container.select_one(source["date_selector"])
                    if el is not None:
                        date_raw = el.get("datetime") or el.get_text(" ", strip=True)
                for a in container.select(source.get("link_selector", "a")):
                    href = a.get("href")
                    if not href:
                        continue
                    abs_url = urljoin(base, href)
                    if not url_re.search(abs_url):
                        continue
                    if date_from_url and date_re:
                        dm = date_re.search(abs_url)
                        add(abs_url, a.get_text(" ", strip=True) or a.get("title"),
                            dm.group(1) if dm else None)
                    else:
                        add(abs_url, a.get_text(" ", strip=True) or a.get("title"), date_raw)

    return [item for item in found.values() if fresh_enough(item)]


def make_client(direct: bool = False) -> httpx.Client:
    """HTTP client honoring env-based proxy/TLS settings.

    - FETCH_PROXY: explicit proxy URL (e.g. http://127.0.0.1:7078). When
      unset, httpx's trust_env picks up HTTP_PROXY/HTTPS_PROXY if present.
    - FETCH_INSECURE_TLS=1: skip TLS verification. Needed when a local
      accelerator MITMs HTTPS with its own CA. Never enable in CI.
    - direct=True: bypass all proxies (including env vars). Used for
      mainland-China sources that must not go through the accelerator.
    """
    kwargs: dict = {
        "headers": {"User-Agent": USER_AGENT},
        "timeout": TIMEOUT_S,
        "follow_redirects": True,
        "trust_env": not direct,
    }
    proxy = os.environ.get("FETCH_PROXY")
    if proxy and not direct:
        kwargs["proxy"] = proxy
    if os.environ.get("FETCH_INSECURE_TLS", "").lower() in ("1", "true", "yes"):
        kwargs["verify"] = False
        log.warning("FETCH_INSECURE_TLS set: TLS verification disabled")
    return httpx.Client(**kwargs)


def fetch_all(sources: list[dict] | None = None) -> tuple[list[dict], list[str]]:
    """Fetch every source. Returns (items, failed_source_names).

    Sources with `proxy: false` in sources.yaml are fetched through a
    direct client that ignores FETCH_PROXY/HTTP(S)_PROXY; everything else
    uses the env-based client.
    """
    if sources is None:
        sources = load_sources()
    items: list[dict] = []
    failed: list[str] = []
    clients: dict[bool, httpx.Client] = {}

    def client_for(source: dict) -> httpx.Client:
        direct = source.get("proxy") is False
        if direct not in clients:
            clients[direct] = make_client(direct=direct)
        return clients[direct]

    try:
        for source in sources:
            try:
                got = fetch_source(source, client_for(source))
                log.info("%s: %d items", source["name"], len(got))
                items.extend(got)
            except Exception as exc:  # noqa: BLE001 - never kill the run
                log.warning("%s: fetch failed: %s", source["name"], exc)
                failed.append(source["name"])
    finally:
        for client in clients.values():
            client.close()
    return items, failed


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    all_items, failures = fetch_all()
    print(f"{len(all_items)} items, {len(failures)} failed sources: {failures}")
