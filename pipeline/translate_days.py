"""Backfill Simplified Chinese fields (title_zh / summary_zh / why_zh) onto
existing day files, and title_zh/lede_zh onto report files. Only fills
missing fields — safe to re-run; new daily runs produce these inline.

Usage:
    pipeline/.venv/Scripts/python pipeline/translate_days.py [data/news/2026-10-11.json ...]
With no arguments, processes every data/news/*.json and data/reports/*.json.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import process

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

ROOT = Path(__file__).resolve().parent.parent
NEWS_DIR = ROOT / "data" / "news"
REPORTS_DIR = ROOT / "data" / "reports"

_TITLE_SYSTEM = (
    "Translate each English data-center-industry headline into Simplified "
    "Chinese. Keep company names, product names, project codenames and "
    "industry terms (MW, GPU, hyperscale, colocation, AI) in their original "
    "English form. Output natural news-style Chinese, not word-for-word "
    'calque. Reply with JSON: {"results": [{"index": int, "title_zh": "..."}]}.'
)

_TEXT_SYSTEM = (
    "Translate each English text into Simplified Chinese for a data-center "
    "industry news service. Keep company names, project codenames and "
    "industry terms (MW, GPU, hyperscale, colocation, AI) in English. "
    "Preserve meaning and tone; do not add or drop facts. "
    'Reply with JSON: {"results": [{"index": int, "zh": "..."}]}.'
)


def _fill(backend, items: list[dict], system: str, payload_key: str,
          out_field: str, batch: int) -> int:
    """Translate items[i][payload_key] -> items[i][out_field]. Returns count."""
    todo = [(i, it) for i, it in enumerate(items)
            if it.get(payload_key) and not it.get(out_field)]
    done = 0
    for start in range(0, len(todo), batch):
        chunk = todo[start:start + batch]
        payload = [{"index": i, "text": it[payload_key][:800]}
                   for i, (_, it) in enumerate(chunk)]
        try:
            data = process.chat_json(backend, system,
                                     json.dumps(payload, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            if len(chunk) > 1:
                # split and retry each half — one bad item can break a batch
                mid = len(chunk) // 2
                log.warning("batch failed (%s) — splitting %d items",
                            exc, len(chunk))
                done += _fill(backend, [it for _, it in chunk[:mid]],
                              system, payload_key, out_field, max(1, mid))
                done += _fill(backend, [it for _, it in chunk[mid:]],
                              system, payload_key, out_field,
                              max(1, len(chunk) - mid))
            else:
                log.warning("batch failed (%s) — skipping 1 item", exc)
            continue
        for res in data.get("results", []):
            idx = res.get("index")
            if not isinstance(idx, int) or not (0 <= idx < len(chunk)):
                continue
            zh = str(res.get("zh") or res.get("title_zh") or "").strip()
            if zh:
                chunk[idx][1][out_field] = zh
                done += 1
    return done


def translate_day(backend, path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    stories = data.get("stories", [])
    n = 0
    n += _fill(backend, stories, _TITLE_SYSTEM, "title", "title_zh", 30)
    n += _fill(backend, stories, _TEXT_SYSTEM, "summary", "summary_zh", 15)
    n += _fill(backend, stories, _TEXT_SYSTEM, "why_it_matters", "why_zh", 15)
    if n:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    log.info("%s: %d stories, %d zh fields added", path.name, len(stories), n)


def translate_report(backend, path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    wrapper = [data]
    n = _fill(backend, wrapper, _TITLE_SYSTEM, "title", "title_zh", 30)
    n += _fill(backend, wrapper, _TEXT_SYSTEM, "lede", "lede_zh", 15)
    # theme sections: heading + narrative
    themes = data.get("themes") or []
    n += _fill(backend, themes, _TEXT_SYSTEM, "heading", "heading_zh", 20)
    n += _fill(backend, themes, _TEXT_SYSTEM, "narrative", "narrative_zh", 10)
    # milestone/status-change lines inside reports, when present
    for key in ("project_milestones", "milestones"):
        entries = data.get(key) or []
        n += _fill(backend, entries, _TEXT_SYSTEM, "note", "note_zh", 20)
    if n:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    log.info("%s: %d zh fields added", path.name, n)


def main(argv: list[str]) -> None:
    import run as run_mod  # for load_env
    run_mod.load_env()
    backend = process.select_backend()
    if argv:
        paths = [Path(a) for a in argv]
    else:
        paths = (sorted(NEWS_DIR.glob("????-??-??.json")) +
                 sorted(REPORTS_DIR.glob("*.json")))
    for path in paths:
        if not path.exists():
            log.warning("missing: %s", path)
            continue
        if path.parent.name == "reports":
            translate_report(backend, path)
        else:
            translate_day(backend, path)


if __name__ == "__main__":
    main(sys.argv[1:])
