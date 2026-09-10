"""LLM processing: relevance scoring, clustering, enrichment.

Two LLM backends (see LLM_BACKEND below): an OpenAI-compatible HTTP API,
or the local `kimi` CLI via subprocess. Degrades gracefully: with no
backend available (or on any backend failure) the pipeline falls back to
a keyword heuristic, score=None, featured=False, summary=cleaned snippet,
and still computes heat from cheap clustering.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger(__name__)

REGIONS = ["china", "japan", "korea", "australia", "southeast-asia", "global"]
FEATURE_THRESHOLD = 65
BATCH_SIZE = 20
ENRICH_CAP = 20
KIMI_TIMEOUT_S = 300

_KEYWORD_RE = re.compile(r"\bdata[ -]?cent(er|re)\b", re.I)
_TITLE_STRIP_RE = re.compile(r"[^a-z0-9 ]+")


def strip_gn_publisher(title: str) -> tuple[str, str | None]:
    """Split a Google News RSS title "Headline - Publisher" into
    (clean_title, publisher). The outlet brand sometimes appears twice
    (original site title already ended with it, then GN appended its own
    " - Publisher"), so keep stripping while the new tail matches the
    publisher we just removed (case-insensitive). Conservative: only the
    trailing segment(s) matching the publisher are touched, never a " - "
    in the middle of a headline.
    """
    if " - " not in title:
        return title, None
    head, _, tail = title.rpartition(" - ")
    publisher = tail.strip()
    head = head.strip()
    if not publisher or not head:
        return title, None
    while " - " in head:
        h, _, t = head.rpartition(" - ")
        if t.strip().lower() != publisher.lower() or not h.strip():
            break
        head = h.strip()
    return head, publisher


# --------------------------------------------------------------- summary QA

_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_MARKS_RE = re.compile(r"[*_`#>]+")


def markdown_to_text(markdown: str) -> str:
    """Very small markdown -> plain text: drop images, unwrap links, strip
    emphasis/heading markers."""
    text = _MD_IMG_RE.sub(" ", markdown)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _MD_MARKS_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def derive_summary(content: str | None, limit: int = 200) -> str:
    """Fallback summary from full-text content: first substantive paragraph
    (skips headings, images, link/list-only lines), truncated at a word
    boundary near `limit` chars."""
    if not content:
        return ""
    for block in re.split(r"\n\s*\n|\n", content):
        stripped = block.strip()
        if not stripped or stripped.startswith(("#", "!", "|")):
            continue
        text = markdown_to_text(stripped)
        if len(text) < 40:  # caption/link-list line, not a real paragraph
            continue
        if len(text) > limit:
            text = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
        return text
    return ""


def _norm_for_compare(text: str) -> str:
    return re.sub(r"\s+", " ", _TITLE_STRIP_RE.sub(" ", text.lower())).strip()


def summaries_equivalent(title: str, summary: str) -> bool:
    """True when the summary adds nothing beyond the headline: equal after
    normalization, one a prefix of the other, or near-identical (>0.9
    similarity)."""
    t, s = _norm_for_compare(title), _norm_for_compare(summary)
    if not t or not s:
        return False
    if t == s or t.startswith(s) or s.startswith(t):
        return True
    from difflib import SequenceMatcher

    return SequenceMatcher(None, t, s).ratio() > 0.9


# ---------------------------------------------------------------- cheap dedup

def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower() or "https"
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = parts.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, "", ""))


def normalize_title(title: str) -> str:
    text = _TITLE_STRIP_RE.sub(" ", title.lower())
    return re.sub(r"\s+", " ", text).strip()


def _title_tokens(title: str) -> set[str]:
    return set(normalize_title(title).split())


def _similar(a: set[str], b: set[str]) -> bool:
    if not a or not b:
        return False
    jaccard = len(a & b) / len(a | b)
    return jaccard >= 0.5


def cluster_items(items: list[dict]) -> list[dict]:
    """Group items covering the same event: URL dedup, then greedy
    normalized-title similarity. Each cluster keeps all source entries."""
    by_url: dict[str, dict] = {}
    for item in items:
        key = canonical_url(item["url"])
        entry = {"name": item["source"], "url": item["url"], "type": item["source_type"],
                 "weight": item["source_weight"]}
        # Google News RSS titles end with " - <publisher>"; keep the real
        # outlet name so the site can show it instead of the feed name, and
        # strip the suffix so it never leaks into the story title.
        if item["source_type"] == "google-news":
            clean_title, publisher = strip_gn_publisher(item["title"])
            if publisher:
                entry["publisher"] = publisher
                item["title"] = clean_title
        if key in by_url:
            cluster = by_url[key]
            cluster["items"].append(item)
            cluster["source_entries"].append(entry)
        else:
            by_url[key] = {
                "title": item["title"],
                "items": [item],
                "source_entries": [entry],
                "tokens": _title_tokens(item["title"]),
            }

    clusters: list[dict] = []
    for cluster in by_url.values():
        for existing in clusters:
            if _similar(existing["tokens"], cluster["tokens"]):
                existing["items"].extend(cluster["items"])
                existing["source_entries"].extend(cluster["source_entries"])
                existing["tokens"] |= cluster["tokens"]
                break
        else:
            clusters.append(cluster)
    return clusters


def representative(cluster: dict) -> dict:
    """Highest-weight, then earliest-published item in the cluster."""
    def key(item):
        return (
            -item["source_weight"],
            item["published_at"] or "9999",
        )
    return sorted(cluster["items"], key=key)[0]


# ------------------------------------------------------------------- LLM core
#
# Backends, selected via LLM_BACKEND = api | kimi-cli | auto (default auto):
#   api      OpenAI-compatible chat completions (requires LLM_API_KEY).
#   kimi-cli `kimi -p <prompt>` subprocess (uses the local CLI's login).
#   auto     api if LLM_API_KEY is set, else kimi-cli if `kimi` is on PATH,
#            else None (heuristic mode).

_JSON_ONLY = (
    "\n\nIMPORTANT: Reply with ONLY raw JSON — no markdown fences, no "
    "commentary, no explanation. Do not use any tools."
)


def parse_json(text: str):
    """Defensive JSON parse: strip code fences, then extract the first
    {...} or [...] block if extra text surrounds it."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        raise ValueError(f"no JSON found in output: {text[:200]!r}")
    start = min(starts)
    closer = "}" if text[start] == "{" else "]"
    end = text.rfind(closer)
    if end <= start:
        raise ValueError(f"no JSON found in output: {text[:200]!r}")
    return json.loads(text[start:end + 1])


def _make_api_client():
    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        return None
    try:
        from openai import OpenAI
    except ImportError:
        log.warning("openai package not installed")
        return None
    return OpenAI(
        api_key=api_key,
        base_url=os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"),
    )


def _find_kimi() -> str | None:
    """Resolve the kimi CLI: PATH first, then the standard install dir —
    so the pipeline also works from terminals where ~/.kimi-code/bin is
    not on PATH."""
    found = shutil.which("kimi")
    if found:
        return found
    for cand in (os.path.expanduser(r"~\.kimi-code\bin\kimi.exe"),
                 os.path.expanduser("~/.kimi-code/bin/kimi")):
        if os.path.isfile(cand):
            return cand
    return None


def select_backend():
    """Return ('api', client) | ('kimi-cli', None) | None (heuristic mode)."""
    pref = os.environ.get("LLM_BACKEND", "auto").strip().lower()
    kimi_path = _find_kimi()
    if pref == "api":
        client = _make_api_client()
        if client is None:
            log.warning("LLM_BACKEND=api but LLM_API_KEY missing/unusable "
                        "— falling back to heuristic mode")
            return None
        return ("api", client)
    if pref == "kimi-cli":
        if not kimi_path:
            log.warning("LLM_BACKEND=kimi-cli but `kimi` not on PATH "
                        "— falling back to heuristic mode")
            return None
        return ("kimi-cli", None)
    if pref != "auto":
        log.warning("unknown LLM_BACKEND=%r, treating as auto", pref)
    client = _make_api_client()
    if client is not None:
        return ("api", client)
    if kimi_path:
        return ("kimi-cli", None)
    log.info("no LLM backend available — running without LLM (heuristic mode)")
    return None


def _api_chat_json(client, system: str, user: str):
    model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    return parse_json(resp.choices[0].message.content or "")


def _kimi_chat_json(system: str, user: str):
    prompt = system + _JSON_ONLY + "\n\n" + user
    proc = subprocess.run(
        [_find_kimi() or "kimi", "-p", prompt],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=KIMI_TIMEOUT_S,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"kimi exited {proc.returncode}: {proc.stderr.strip()[:300]}")
    out = proc.stdout.strip()
    if not out:
        raise RuntimeError(
            f"kimi returned empty stdout: {proc.stderr.strip()[:300]}")
    return parse_json(out)


def chat_json(backend, system: str, user: str):
    """Single completion expected to return JSON. Raises on failure."""
    kind, client = backend
    if kind == "api":
        return _api_chat_json(client, system, user)
    return _kimi_chat_json(system, user)


# ------------------------------------------------- step 1: relevance+metadata

_RELEVANCE_SYSTEM = (
    "You are an editor for an APAC data center industry news service. "
    "For each candidate news item decide: relevant — true only if it is about "
    "the data center industry AND (concerns China, Japan, Korea, Australia or "
    "Southeast Asia, OR is a global story materially affecting APAC, e.g. "
    "NVIDIA/chip supply, hyperscaler strategy). India is OUT OF SCOPE: stories "
    "only about India are not relevant. Purely US/Europe stories with no APAC "
    "angle (a Kentucky campus, a Pennsylvania power deal) are not relevant "
    "either — 'global' is reserved for stories that materially affect APAC. "
    "score — 0-100 industry relevance/importance (0 if not relevant). "
    "regions — subset of " + json.dumps(REGIONS) + "; use \"global\" only for "
    "global stories materially affecting APAC. "
    "topics — up to 4 free-form lowercase tags (e.g. ai, capacity, power, "
    "investment, hyperscale, colocation, regulation, chips). "
    "title_en — a clean normalized English headline with any trailing "
    "publisher/outlet name suffix (e.g. \" - Reuters\") removed. "
    "companies — up to 6 canonical names of companies/organizations the "
    'story is ABOUT (e.g. "NVIDIA", "STT GDC", "Equinix", "NTT", "AirTrunk"); '
    "omit generic mentions with no substance; empty array when none. "
    'Reply with JSON: {"results": [{"index": int, "relevant": bool, '
    '"score": int, "regions": [...], "topics": [...], "title_en": "...", '
    '"companies": [...]}]}.'
)


def _annotate_batch(backend, batch: list[dict]) -> None:
    """One LLM relevance call for a batch; raises on failure."""
    payload = []
    for i, cluster in enumerate(batch):
        rep = representative(cluster)
        payload.append({
            "index": i,
            "title": rep["title"],
            "snippet": rep["snippet"][:300],
            "source_regions_hint": rep["regions"],
        })
    data = chat_json(backend, _RELEVANCE_SYSTEM, json.dumps(payload, ensure_ascii=False))
    for res in data.get("results", []):
        idx = res.get("index")
        if not isinstance(idx, int) or not (0 <= idx < len(batch)):
            continue
        cluster = batch[idx]
        cluster["relevant"] = bool(res.get("relevant"))
        score = res.get("score")
        cluster["score"] = max(0, min(100, int(score))) if score is not None else None
        regions = [r for r in res.get("regions", []) if r in REGIONS]
        cluster["regions"] = regions or None
        cluster["topics"] = [str(t).lower() for t in res.get("topics", [])][:4]
        title_en = str(res.get("title_en", "")).strip()
        if title_en:
            cluster["title_en"] = title_en
        cluster["companies"] = [str(c).strip()
                                for c in res.get("companies", [])
                                if str(c).strip()][:6]


def _annotate_resilient(backend, batch: list[dict]) -> None:
    """Annotate a batch; on LLM failure (e.g. a 'high risk' 400 triggered by
    a single toxic headline) bisect until the offending items are isolated,
    and degrade only those to heuristic scoring instead of the whole day."""
    try:
        _annotate_batch(backend, batch)
        return
    except Exception as exc:  # noqa: BLE001
        if len(batch) <= 1:
            log.warning("relevance LLM rejected item %r (%s) — heuristic for this item only",
                        representative(batch[0])["title"][:80], exc)
            _heuristic_one(batch[0])
            return
        mid = len(batch) // 2
        log.warning("relevance batch of %d failed (%s) — bisecting", len(batch), exc)
        _annotate_resilient(backend, batch[:mid])
        _annotate_resilient(backend, batch[mid:])


def llm_relevance(backend, clusters: list[dict]) -> bool:
    """Annotate clusters in place with relevant/score/regions/topics/
    title_en/companies. Always returns True: per-batch bisection confines
    failures to individual items."""
    for start in range(0, len(clusters), BATCH_SIZE):
        _annotate_resilient(backend, clusters[start:start + BATCH_SIZE])
    return True


# Heuristic-mode company detection: scan title+snippet against a small
# known-company list (longest names first so multi-word names win).
_KNOWN_COMPANIES = [
    "Princeton Digital Group", "Vantage Data Centers", "Amazon Web Services",
    "Digital Realty", "STT GDC", "Equinix", "AirTrunk", "NTT", "NVIDIA",
    "Microsoft", "Google", "Meta", "Alibaba", "Tencent", "ByteDance",
    "Oracle", "CoreWeave", "Keppel", "Singtel", "SoftBank", "GDS",
    "CyrusOne", "DayOne", "Macquarie", "IBM", "AMD", "Intel", "OpenAI",
    "Broadcom", "TSMC", "Zayo", "Crusoe", "Lightmatter", "JLL",
    "Centuria", "ResetData", "AWS",
]
_KNOWN_COMPANY_RE = re.compile(
    r"(?<![\w])(" + "|".join(re.escape(n) for n in
                            sorted(_KNOWN_COMPANIES, key=len, reverse=True)) +
    r")(?![\w])"
)


def guess_companies(text: str) -> list[str]:
    found = list(dict.fromkeys(_KNOWN_COMPANY_RE.findall(text)))
    return found[:6]


def _heuristic_one(cluster: dict) -> None:
    rep = representative(cluster)
    text = rep["title"] + " " + rep["snippet"]
    cluster["relevant"] = bool(_KEYWORD_RE.search(text))
    cluster["score"] = None
    cluster["regions"] = None  # fall back to source regions at assembly
    cluster["topics"] = []
    cluster["title_en"] = rep["title"]
    cluster["companies"] = guess_companies(text)


def heuristic_relevance(clusters: list[dict]) -> None:
    for cluster in clusters:
        _heuristic_one(cluster)


# ------------------------------------------------------- step 2: LLM merging

_MERGE_SYSTEM = (
    "You are deduplicating a news list. Given numbered headlines, identify "
    "groups that report the SAME underlying event/announcement. "
    'Reply with JSON: {"groups": [[0, 5], [2, 9, 14]]} — only groups of 2+ '
    "indices; omit items with no duplicate. Be conservative: when in doubt, "
    "do not group."
)


def llm_merge_clusters(backend, clusters: list[dict]) -> list[dict]:
    """Optionally merge near-duplicate clusters via one LLM call."""
    if len(clusters) < 2:
        return clusters
    try:
        titles = [{"index": i, "title": c.get("title_en") or c["title"]}
                  for i, c in enumerate(clusters)]
        data = chat_json(backend, _MERGE_SYSTEM, json.dumps(titles, ensure_ascii=False))
        groups = [g for g in data.get("groups", [])
                  if isinstance(g, list) and len(g) >= 2
                  and all(isinstance(i, int) and 0 <= i < len(clusters) for i in g)]
        if not groups:
            return clusters
        merged: dict[int, int] = {}  # member index -> group id
        for gid, group in enumerate(groups):
            for i in group:
                merged.setdefault(i, gid)
        out, group_acc = [], {}
        for i, cluster in enumerate(clusters):
            gid = merged.get(i)
            if gid is None:
                out.append(cluster)
            elif gid in group_acc:
                _absorb(group_acc[gid], cluster)
            else:
                group_acc[gid] = cluster
                out.append(cluster)
        log.info("LLM merge: %d clusters -> %d", len(clusters), len(out))
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("LLM clustering step failed, keeping cheap clusters: %s", exc)
        return clusters


def _absorb(dst: dict, src: dict) -> None:
    dst["items"].extend(src["items"])
    dst["source_entries"].extend(src["source_entries"])
    dst["tokens"] |= src["tokens"]
    for field, better in (("score", lambda a, b: a if (b is None or (a is not None and a >= b)) else b),
                          ("relevant", lambda a, b: a or b)):
        dst[field] = better(dst.get(field), src.get(field))
    for field in ("regions", "topics"):
        combined = list(dict.fromkeys((dst.get(field) or []) + (src.get(field) or [])))
        dst[field] = combined[:4] if field == "topics" else combined
    companies = list(dict.fromkeys((dst.get("companies") or []) +
                                   (src.get("companies") or [])))[:6]
    dst["companies"] = companies


# ------------------------------------------------------ step 3: enrichment

_ENRICH_SYSTEM = (
    "You are an editor for an APAC data center industry news service. For "
    "each story, write: summary — 2-3 sentence factual English summary that "
    "adds information beyond the headline (never restate or copy the title); "
    "why_it_matters — one short editorial paragraph in the style of "
    '"what changes, who is affected" (do NOT restate the summary). '
    'Reply with JSON: {"results": [{"index": int, "summary": "...", '
    '"why_it_matters": "..."}]}.'
)


def _enrich_batch(backend, ranked: list[dict]) -> None:
    payload = [{"index": i,
                "title": c.get("title_en") or c["title"],
                "snippet": representative(c)["snippet"][:400]}
               for i, c in enumerate(ranked)]
    data = chat_json(backend, _ENRICH_SYSTEM, json.dumps(payload, ensure_ascii=False))
    for res in data.get("results", []):
        idx = res.get("index")
        if not isinstance(idx, int) or not (0 <= idx < len(ranked)):
            continue
        ranked[idx]["summary"] = str(res.get("summary", "")).strip()
        ranked[idx]["why_it_matters"] = str(res.get("why_it_matters", "")).strip()


def _enrich_resilient(backend, ranked: list[dict]) -> None:
    """Bisect on failure so one rejected story doesn't strip summaries from
    the whole day (same 'high risk' 400 scenario as relevance)."""
    try:
        _enrich_batch(backend, ranked)
        return
    except Exception as exc:  # noqa: BLE001
        if len(ranked) <= 1:
            log.warning("enrichment LLM rejected item %r (%s) — skipped",
                        (ranked[0].get("title_en") or ranked[0]["title"])[:80], exc)
            return
        mid = len(ranked) // 2
        log.warning("enrichment batch of %d failed (%s) — bisecting", len(ranked), exc)
        _enrich_resilient(backend, ranked[:mid])
        _enrich_resilient(backend, ranked[mid:])


def llm_enrich(backend, clusters: list[dict]) -> None:
    """Add summary/why_it_matters to every featured story (score >=
    FEATURE_THRESHOLD; the site's 精选 set), capped for cost; if nothing
    reaches the threshold, enrich the top 10 instead so the day is never
    bare. The LLM sometimes returns results for only part of a batch; retry
    the missing ones while progress is made."""
    ranked = sorted(
        (c for c in clusters if c.get("score") is not None),
        key=lambda c: c["score"], reverse=True,
    )
    featured = [c for c in ranked if c["score"] >= FEATURE_THRESHOLD]
    ranked = (featured or ranked[:10])[:ENRICH_CAP]
    if not ranked:
        return
    _enrich_resilient(backend, ranked)
    for _ in range(3):  # LLM sometimes returns only part of a batch; retry
        missing = [c for c in ranked if not c.get("why_it_matters")]
        if not missing:
            break
        log.info("enrichment: %d/%d stories missing — retrying",
                 len(missing), len(ranked))
        before = len(missing)
        _enrich_resilient(backend, missing)
        if len([c for c in ranked if not c.get("why_it_matters")]) >= before:
            break  # no progress — stop burning calls


# ------------------------------------------------------------- orchestration

def process_items(items: list[dict]) -> list[dict]:
    """Full processing pipeline. Returns surviving (relevant) clusters,
    annotated with relevant/score/regions/topics/title_en and, when an LLM
    is available, summary/why_it_matters."""
    clusters = cluster_items(items)
    log.info("clustered %d items into %d clusters", len(items), len(clusters))

    backend = select_backend()
    llm_ok = False
    if backend is not None:
        log.info("LLM backend: %s", backend[0])
        llm_ok = llm_relevance(backend, clusters)
    if not llm_ok:
        heuristic_relevance(clusters)

    clusters = [c for c in clusters if c.get("relevant")]
    # Low-signal global noise floor: a story tagged only "global" with a
    # mediocre score (US/Europe-only news the LLM was too generous with)
    # dilutes the digest — drop it. Scored items only: heuristic fallback
    # (score None) keeps its keyword judgment.
    before = len(clusters)
    clusters = [c for c in clusters
                if not (c.get("regions") == ["global"]
                        and c.get("score") is not None
                        and c["score"] < 50)]
    if len(clusters) < before:
        log.info("dropped %d low-score global-only clusters", before - len(clusters))
    log.info("%d relevant clusters after filtering", len(clusters))

    if backend is not None and llm_ok:
        clusters = llm_merge_clusters(backend, clusters)
        llm_enrich(backend, clusters)

    for cluster in clusters:
        rep = representative(cluster)
        title = cluster.get("title_en") or rep["title"]
        # Defensive: the LLM sometimes keeps the outlet suffix it saw in the
        # raw Google News title; strip it when the tail matches a publisher
        # parsed from the same cluster's sources.
        publishers = {e["publisher"].lower() for e in cluster["source_entries"]
                      if e.get("publisher")}
        while " - " in title:
            head, _, tail = title.rpartition(" - ")
            if not head.strip() or tail.strip().lower() not in publishers:
                break
            title = head.strip()
        cluster["title_en"] = title
        summary = cluster.get("summary") or rep["snippet"]
        # Never let the summary repeat the headline (Google News RSS
        # "snippets" are just the headline text). run.py re-derives a summary
        # from full text when extraction succeeds; the site hides empty ones.
        if summaries_equivalent(title, summary):
            summary = ""
        cluster["summary"] = summary
        cluster.setdefault("why_it_matters", "")
    return clusters
