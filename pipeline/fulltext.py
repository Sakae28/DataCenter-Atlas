"""Extract full article text for assembled stories.

Runs AFTER LLM processing/assembly (only stories that survived filtering)
and BEFORE writing the daily JSON. For each story the primary source URL
is fetched with the same proxy/TLS conventions as fetch.py and the article
body is extracted with trafilatura as Markdown (headings, lists, quotes and
images preserved). Failures never kill the run: the story
simply keeps no `content`/`content_url` fields.

Google News URLs (news.google.com/rss/articles/...) are redirect links that
do not resolve via plain HTTP redirects. Decoding is best-effort via the
`googlenewsdecoder` package (retried with a short sleep — Google
rate-limits); when it fails or the decoded page won't extract, the story
keeps its summary only — an acceptable outcome.

Pages that block plain httpx with a 403 fall back first to `curl_cffi`
(Chrome TLS-fingerprint impersonation — beats Cloudflare's managed
challenge, e.g. Data Center Dynamics), then to `cloudscraper` (older IUAM
challenges); extractions containing U+FFFD mojibake (wrong declared
charset) are retried after re-decoding the raw bytes with
charset_normalizer.

Extracted Markdown is polished before storing (polish_content): a leading
heading/line duplicating the story title is stripped (the detail page shows
the title in its H1), trailing boilerplate is cut (follow-us/newsletter
promos, author bios, comment-login prompts, "Read more" related-article
rails, base64 placeholder images), and standalone fully-bold lead-in lines
like `**Key Highlights:**` are promoted to `###` headings. Extractions
that are only a teaser plus a paywall pitch (DIGITIMES) are dropped
entirely so the detail page falls back to the summary. The same polishing
is re-applied to stored content by polish_content.py and
clean_news_boilerplate.py (no refetch needed).

Skip the whole step with FETCH_FULLTEXT=0.
"""
from __future__ import annotations

import logging
import os
import re
import time

import httpx
import trafilatura

from fetch import make_client

log = logging.getLogger(__name__)

MAX_CONTENT_CHARS = 20000
MIN_CONTENT_CHARS = 200  # shorter than this is usually nav junk, not an article
GN_DECODE_ATTEMPTS = 3   # Google rate-limits the decoder endpoint
GN_DECODE_DELAY_S = 2.0

# Bot-wall interstitials that come back with HTTP 200 (NBC, Cloudflare
# "checking your browser", etc.) — trafilatura happily extracts them as if
# they were the article. Discard content matching these markers instead of
# storing the challenge text.
_BOT_WALL_RE = re.compile(
    r"just a moment|getting your experience ready|verify you are human|"
    r"checking your browser|checking if the site connection is secure|"
    r"are you a robot|please enable javascript|"
    r"your browser is out of date|please update your browser",
    re.I,
)


def enabled() -> bool:
    return os.environ.get("FETCH_FULLTEXT", "1").lower() not in ("0", "false", "no")


def clean_text(text: str) -> str:
    """Normalize extracted markdown: collapse runs of blank lines to one
    (structure — headings, lists, blockquotes — is preserved), cap at
    MAX_CONTENT_CHARS on a paragraph (blank-line) boundary."""
    text = re.sub(r"\n{3,}", "\n\n", text.strip())
    if len(text) > MAX_CONTENT_CHARS:
        cut = text[:MAX_CONTENT_CHARS]
        para = cut.rfind("\n\n")
        text = cut[:para] if para > MAX_CONTENT_CHARS // 2 else cut
        text = text.rstrip() + "…"
    return text


# ---------------------------------------------------------- content polishing
#
# Two pure string post-processing steps, applied to freshly extracted
# content in extract_story and reused by polish_content.py to heal stored
# content without refetching.

_MATCH_NORM_RE = re.compile(r"[^a-z0-9]+")


def _match_norm(text: str) -> str:
    """Normalize for fuzzy comparison: lowercase, drop punctuation."""
    return _MATCH_NORM_RE.sub(" ", text.lower()).strip()


def _titles_match(line: str, title: str) -> bool:
    a, b = _match_norm(line), _match_norm(title)
    if not a or not b:
        return False
    if a == b:
        return True
    from difflib import SequenceMatcher

    return SequenceMatcher(None, a, b).ratio() >= 0.85


def strip_leading_title(content: str, title: str | None, meta_title: str | None = None) -> str:
    """Remove a leading markdown heading or first line that fuzzy-matches the
    story title (or trafilatura's metadata title) — the detail page already
    shows the title in its H1, so a duplicate inside the body reads badly.
    A leading H1 (`# ...`) is dropped unconditionally: it is the source
    page's own article title almost by definition, and LLM-rewritten story
    headlines never fuzzy-match it."""
    lines = content.split("\n")
    candidates = [t for t in (title, meta_title) if t]
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue  # skip leading blank lines
        if stripped.startswith("# "):
            del lines[i]
            return "\n".join(lines).lstrip("\n")
        text = stripped.lstrip("#").strip() if stripped.startswith("#") else stripped
        if any(_titles_match(text, t) for t in candidates):
            del lines[i]
            return "\n".join(lines).lstrip("\n")
        break  # only the first non-empty line is a candidate
    return content


_BOLD_LINE_RE = re.compile(r"^(?:\*\*(?P<star>.+?)\*\*|__(?P<under>.+?)__)$")
_LIST_QUOTE_RE = re.compile(r"^\s*(?:>|[-*+]\s|\d+[.)]\s)")
_MAX_HEADING_CHARS = 80


def promote_bold_leadins(content: str) -> str:
    """Promote a standalone fully-bold line (``**Key Highlights:**``,
    ``__Expansion Plans__`` — some sources use these instead of headings) to
    a ``###`` heading. Skips list items and blockquotes, lines over
    _MAX_HEADING_CHARS, and lines ending in sentence punctuation (a trailing
    colon is dropped)."""
    out = []
    for line in content.split("\n"):
        stripped = line.strip()
        m = _BOLD_LINE_RE.match(stripped)
        if m and not _LIST_QUOTE_RE.match(stripped):
            text = (m.group("star") or m.group("under")).strip()
            if (len(text) <= _MAX_HEADING_CHARS
                    and not re.search(r"[.!?;,]$", text)
                    and "://" not in text and not text.startswith("www.")):
                line = "### " + text.rstrip(":").strip()
        out.append(line)
    return "\n".join(out)


# ------------------------------------------------------ boilerplate stripping
#
# Trailing junk that trafilatura's recall bias pulls in along with the
# article: follow-us/newsletter promos, author bios, comment-login prompts
# and "Read more" related-article rails. All of it sits at the END of the
# extraction, so stripping cuts from the first marker line to the end of
# the content. Markers must sit past _TAIL_MIN_FRACTION of the text so a
# phrase quoted mid-article can't delete real content; the TheInvestor
# "Read More" rail additionally requires its distinctive related-article
# dateline as corroboration.

_TAIL_MIN_FRACTION = 0.4
_READ_MORE_MIN_FRACTION = 0.15

_TAIL_MARKER_RES = [
    # Future PLC (TechRadar & co.): "Follow TechRadar on Google News ..."
    # promo, author bio and comment-login prompt, in that order.
    re.compile(r"follow\b.{0,60}\bon google news\b", re.I),
    # "add us as a preferred source" (Future PLC) and
    # "To add **Benzinga News** as your preferred source ..." (Benzinga).
    re.compile(r"\badd\b.{0,40}\bas\b.{0,12}\bpreferred source\b", re.I),
    re.compile(r"you must confirm your public display name before commenting", re.I),
    re.compile(r"please log\s*out and then log\s*in again", re.I),
    re.compile(r"\bhas been writing about (technology|tech)\b", re.I),
    # Benzinga copyright/disclaimer tail.
    re.compile(r"\bdoes not provide investment advice\b", re.I),
    # Tempo (Indonesia): "Read: <other article>" pointer plus
    # "Click here to get the latest news updates from Tempo on Google News".
    re.compile(r"^\s*#{0,3}\s*\*{0,2}read:[\s*]", re.I),
    re.compile(r"\bget the latest news updates from\b", re.I),
    # TechInsights: platform teaser trailer
    # ("This summary outlines the analysis found on the TechInsights'
    # Platform. Some analyses may only be available with a paid
    # subscription.").
    re.compile(r"\bthis summary outlines the analysis\b", re.I),
]

# TheInvestor (Vietnam): a "- Read More" line introduces a rail of other
# articles' thumbnails/headlines; every entry carries a dateline like
# "Economy - Wed, August 19, 2026 | 4:27 pm GMT+7".
_READ_MORE_RE = re.compile(r"^\s*(?:[-*+]\s*)?read more\s*:?\s*$", re.I)
_RELATED_DATELINE_RE = re.compile(
    r"^[A-Z][\w &'-]+ - (?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), \w+ \d{1,2}, \d{4} \| "
    r"\d{1,2}:\d{2} [ap]m GMT"
)

_IMG_ONLY_RE = re.compile(r"^!\[[^\]]*\]\([^)]*\)$")


def _tail_start(lines: list[str], content_len: int) -> int | None:
    """Line index where the trailing-junk block begins, or None."""
    offset = 0
    read_more_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped:
            if offset >= content_len * _TAIL_MIN_FRACTION and any(
                r.search(stripped) for r in _TAIL_MARKER_RES
            ):
                return i
            if offset >= content_len * _READ_MORE_MIN_FRACTION:
                if read_more_idx is None and _READ_MORE_RE.match(stripped):
                    read_more_idx = i
                elif read_more_idx is not None and _RELATED_DATELINE_RE.match(stripped):
                    return read_more_idx
        offset += len(line) + 1
    return None


def strip_boilerplate(content: str) -> str:
    """Remove trailing promo/bio/comment/related-article blocks and base64
    placeholder images from extracted markdown."""
    # 1x1 tracking/placeholder pixels inlined as data URIs (TheInvestor).
    lines = [l for l in content.split("\n") if "data:image/" not in l]
    cut = _tail_start(lines, len(content))
    if cut is not None:
        # Pull preceding blank / image-only lines into the cut too (e.g.
        # TechRadar's "Click to follow" logo right above the promo).
        while cut > 0 and (
            not lines[cut - 1].strip() or _IMG_ONLY_RE.match(lines[cut - 1].strip())
        ):
            cut -= 1
        lines = lines[:cut]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines).strip())


# Paywall templates: the extraction captured only the teaser plus the
# site's subscription pitch (DIGITIMES: "The article requires paid
# subscription. Subscribe Now / Create your free account ..."). There is
# no real full text — drop the content entirely so the detail page falls
# back to the summary.
_PAYWALL_RE = re.compile(
    r"the article requires paid subscription|"
    r"subscribe now to (?:continue|keep) reading",
    re.I,
)
_PAYWALL_SHORT_RE = re.compile(r"create your free account", re.I)
_PAYWALL_SHORT_MAX = 3000


def is_paywall_teaser(content: str) -> bool:
    """True when the extraction is just a teaser plus a paywall pitch."""
    if _PAYWALL_RE.search(content):
        return True
    # "Create your free account" is only a paywall signal on short
    # extractions — a long article quoting the phrase keeps its content.
    return len(content) < _PAYWALL_SHORT_MAX and bool(
        _PAYWALL_SHORT_RE.search(content)
    )


def polish_content(content: str, story: dict | None = None, meta_title: str | None = None) -> str:
    """strip_leading_title + strip_boilerplate + promote_bold_leadins, in
    order. Pure string processing — safe to re-run on stored content
    (polish_content.py, clean_news_boilerplate.py)."""
    content = strip_leading_title(content, (story or {}).get("title"), meta_title)
    content = strip_boilerplate(content)
    return promote_bold_leadins(content)


def resolve_google_news(url: str) -> str | None:
    """Best-effort decode of a news.google.com redirect URL. None on failure.
    Retries with a short sleep: Google rate-limits the decode endpoint."""
    from googlenewsdecoder import gnewsdecoder

    # gnewsdecoder uses `requests`, which only honors HTTP(S)_PROXY env
    # vars — bridge our FETCH_PROXY setting so decoding works behind a
    # local accelerator.
    proxy = os.environ.get("FETCH_PROXY")
    if proxy:
        os.environ.setdefault("HTTPS_PROXY", proxy)
        os.environ.setdefault("HTTP_PROXY", proxy)
    for attempt in range(GN_DECODE_ATTEMPTS):
        try:
            result = gnewsdecoder(url)
            if result.get("status") and result.get("decoded_url"):
                return result["decoded_url"]
            log.info("googlenewsdecoder attempt %d failed for %s: %s",
                     attempt + 1, url, result.get("message"))
        except Exception as exc:  # noqa: BLE001 - best effort, never kill the run
            log.info("googlenewsdecoder attempt %d failed for %s: %s",
                     attempt + 1, url, exc)
        if attempt < GN_DECODE_ATTEMPTS - 1:
            time.sleep(GN_DECODE_DELAY_S)
    return None


def _insecure_tls() -> bool:
    return os.environ.get("FETCH_INSECURE_TLS", "").lower() in ("1", "true", "yes")


def _requests_proxies() -> dict | None:
    """Proxy dict for requests-style clients, bridging FETCH_PROXY (they
    otherwise only honor HTTP(S)_PROXY env vars)."""
    proxy = os.environ.get("FETCH_PROXY")
    if proxy:
        os.environ.setdefault("HTTPS_PROXY", proxy)
        os.environ.setdefault("HTTP_PROXY", proxy)
        return {"http": proxy, "https": proxy}
    return None


def fetch_with_curl_cffi(url: str) -> bytes | None:
    """403 bypass via curl_cffi's Chrome TLS-fingerprint impersonation.
    Needed for Cloudflare *managed* challenges (e.g. Data Center Dynamics),
    which are triggered by the client fingerprint and cannot be solved by
    cloudscraper's old IUAM Javascript-challenge solver. None on failure."""
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        log.info("curl_cffi not installed; cannot impersonate Chrome for %s", url)
        return None
    try:
        resp = cffi_requests.get(
            url,
            impersonate="chrome",
            proxies=_requests_proxies(),
            # The local accelerator MITMs HTTPS with its own CA; only skip
            # verification when FETCH_INSECURE_TLS explicitly allows it.
            verify=not _insecure_tls(),
            timeout=30,
        )
        resp.raise_for_status()
        return resp.content
    except Exception as exc:  # noqa: BLE001 - best effort fallback
        log.info("curl_cffi failed for %s: %s", url, exc)
        return None


def fetch_with_cloudscraper(url: str) -> bytes | None:
    """Second-chance 403 bypass for older Cloudflare IUAM challenges.
    None on failure."""
    try:
        import cloudscraper
    except ImportError:
        log.info("cloudscraper not installed; cannot bypass 403 for %s", url)
        return None
    _requests_proxies()
    # verify=False alone breaks urllib3 ("Cannot set verify_mode ... when
    # check_hostname is enabled"), so pair it with a hostname-check-free
    # SSL context.
    insecure = _insecure_tls()
    ssl_context = None
    if insecure:
        import ssl

        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
    try:
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "desktop": True},
            ssl_context=ssl_context,
        )
        resp = scraper.get(url, timeout=30, verify=not insecure)
        resp.raise_for_status()
        return resp.content
    except Exception as exc:  # noqa: BLE001 - best effort fallback
        log.info("cloudscraper failed for %s: %s", url, exc)
        return None


def fetch_page(client: httpx.Client, url: str) -> bytes:
    """Fetch raw page bytes; on a 403 try curl_cffi (Chrome impersonation),
    then cloudscraper."""
    request = response = None
    try:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.content
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 403:
            raise
        # `exc` is deleted when the except block exits — capture what the
        # re-raise below needs.
        request, response = exc.request, exc.response
        log.info("403 from %s — trying curl_cffi/cloudscraper", url)
    raw = fetch_with_curl_cffi(url) or fetch_with_cloudscraper(url)
    if raw is None:
        raise httpx.HTTPStatusError(
            "403 and all fallbacks failed", request=request, response=response
        )
    return raw


def extract_markdown(html) -> str | None:
    return trafilatura.extract(
        html,
        output_format="markdown",
        include_comments=False,
        include_tables=False,
        include_links=False,
        # Markdown image syntax comes out with absolute URLs on the sources
        # we track, so images render standalone on the detail pages.
        include_images=True,
        # Prefer pulling in more of the article over aggressive pruning —
        # detail pages felt incomplete with the default precision bias.
        favor_recall=True,
    )


def redecode_extract(raw: bytes) -> str | None:
    """Re-extract from bytes decoded with charset_normalizer's best guess.
    Used when the page declares a wrong charset and the first extraction
    contains U+FFFD replacement chars."""
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(raw).best()
        if best is None:
            return None
        html = trafilatura.load_html(str(best))
        if html is None:
            return None
        return extract_markdown(html)
    except Exception:  # noqa: BLE001 - best effort
        return None


def decode_html(raw: bytes) -> str:
    """Decode raw page bytes before handing them to trafilatura. Its own
    byte sniffing can lock onto a legacy CJK codec (e.g. GBK) when a page
    omits/ misdeclares its charset, turning curly quotes, dashes and
    degree signs into U+FFFD mojibake. Try UTF-8 first (the overwhelming
    majority of sources), then charset_normalizer's best guess."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(raw).best()
        if best is not None:
            return str(best)
    except Exception:  # noqa: BLE001 - best effort
        pass
    return raw.decode("utf-8", errors="replace")


def extract_text(raw: bytes) -> tuple[str | None, object]:
    """Extract article markdown from raw page bytes. If the result contains
    U+FFFD mojibake, retry with a charset_normalizer re-decode and keep
    whichever extraction has fewer replacement chars.
    Returns (text, html_tree_for_metadata)."""
    html = trafilatura.load_html(decode_html(raw))
    if html is None:
        return None, None
    text = extract_markdown(html)
    if text and "\ufffd" in text:
        alt = redecode_extract(raw)
        if alt and alt.count("\ufffd") < text.count("\ufffd"):
            log.info("mojibake retry: %d -> %d replacement chars",
                     text.count("\ufffd"), alt.count("\ufffd"))
            text = alt
    return text, html


def extract_story(client: httpx.Client, story: dict, url: str | None = None) -> tuple[str, str] | None:
    """Fetch and extract the body for one story. Returns (content, content_url)
    or None when extraction fails. `url` overrides the fetch target (healing
    passes pass the story's content_url to avoid re-decoding GN links)."""
    url = url or story["sources"][0]["url"]
    if "news.google.com" in url:
        decoded = resolve_google_news(url)
        if not decoded:
            return None
        url = decoded
    raw = fetch_page(client, url)
    text, html = extract_text(raw)
    if not text:
        return None
    meta = None
    try:
        if html is not None:
            meta = trafilatura.extract_metadata(html)
    except Exception:  # noqa: BLE001 - metadata is nice-to-have
        pass
    content = clean_text(text)
    content = polish_content(content, story, meta_title=meta.title if meta else None)
    if is_paywall_teaser(content):
        log.info("paywall teaser in extraction, keeping summary only")
        return None
    if _BOT_WALL_RE.search(content):
        log.info("bot-wall interstitial in extraction, keeping summary only")
        return None
    if len(content) < MIN_CONTENT_CHARS:
        return None
    content_url = url
    if meta and meta.url:
        content_url = meta.url
    return content, content_url


def enrich_stories(stories: list[dict]) -> tuple[int, int]:
    """Add `content`/`content_url` to each story in place where extraction
    succeeds. Returns (succeeded, failed) counts."""
    ok = failed = 0
    with make_client() as client:
        for story in stories:
            try:
                got = extract_story(client, story)
            except Exception as exc:  # noqa: BLE001 - one story never kills the run
                log.info("fulltext failed for %s: %s", story.get("id"), exc)
                got = None
            if got:
                story["content"], story["content_url"] = got
                ok += 1
            else:
                story.pop("content", None)
                story.pop("content_url", None)
                failed += 1
    return ok, failed
