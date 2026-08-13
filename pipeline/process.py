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
ENRICH_CAP = 15
KIMI_TIMEOUT_S = 300

_KEYWORD_RE = re.compile(r"\bdata[ -]?cent(er|re)\b", re.I)
_TITLE_STRIP_RE = re.compile(r"[^a-z0-9 ]+")


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


def select_backend():
    """Return ('api', client) | ('kimi-cli', None) | None (heuristic mode)."""
    pref = os.environ.get("LLM_BACKEND", "auto").strip().lower()
    kimi_path = shutil.which("kimi")
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
        ["kimi", "-p", prompt],
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
    "NVIDIA/chip supply, hyperscaler strategy). "
    "score — 0-100 industry relevance/importance (0 if not relevant). "
    "regions — subset of " + json.dumps(REGIONS) + "; use \"global\" only for "
    "global stories materially affecting APAC. "
    "topics — up to 4 free-form lowercase tags (e.g. ai, capacity, power, "
    "investment, hyperscale, colocation, regulation, chips). "
    "title_en — a clean normalized English headline. "
    'Reply with JSON: {"results": [{"index": int, "relevant": bool, '
    '"score": int, "regions": [...], "topics": [...], "title_en": "..."}]}.'
)


def llm_relevance(backend, clusters: list[dict]) -> bool:
    """Annotate clusters in place with relevant/score/regions/topics/title_en.
    Returns True on success; on failure leaves clusters untouched."""
    try:
        for start in range(0, len(clusters), BATCH_SIZE):
            batch = clusters[start:start + BATCH_SIZE]
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
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("LLM relevance step failed, falling back to heuristic: %s", exc)
        return False


def heuristic_relevance(clusters: list[dict]) -> None:
    for cluster in clusters:
        rep = representative(cluster)
        text = rep["title"] + " " + rep["snippet"]
        cluster["relevant"] = bool(_KEYWORD_RE.search(text))
        cluster["score"] = None
        cluster["regions"] = None  # fall back to source regions at assembly
        cluster["topics"] = []
        cluster["title_en"] = rep["title"]


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


# ------------------------------------------------------ step 3: enrichment

_ENRICH_SYSTEM = (
    "You are an editor for an APAC data center industry news service. For "
    "each story, write: summary — 2-3 sentence factual English summary; "
    "why_it_matters — one short editorial paragraph in the style of "
    '"what changes, who is affected" (do NOT restate the summary). '
    'Reply with JSON: {"results": [{"index": int, "summary": "...", '
    '"why_it_matters": "..."}]}.'
)


def llm_enrich(backend, clusters: list[dict]) -> None:
    """Add summary/why_it_matters to the top clusters by score, in ONE call
    (fresh CLI sessions are expensive, and it bounds API cost too)."""
    ranked = sorted(
        (c for c in clusters if c.get("score") is not None),
        key=lambda c: c["score"], reverse=True,
    )[:ENRICH_CAP]
    if not ranked:
        return
    try:
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
    except Exception as exc:  # noqa: BLE001
        log.warning("LLM enrichment step failed, using snippets: %s", exc)


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
    log.info("%d relevant clusters after filtering", len(clusters))

    if backend is not None and llm_ok:
        clusters = llm_merge_clusters(backend, clusters)
        llm_enrich(backend, clusters)

    for cluster in clusters:
        rep = representative(cluster)
        cluster.setdefault("title_en", rep["title"])
        if not cluster.get("summary"):
            cluster["summary"] = rep["snippet"]
        cluster.setdefault("why_it_matters", "")
    return clusters
