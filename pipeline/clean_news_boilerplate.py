"""Maintenance: strip boilerplate from stored story content (no refetching).

Re-applies fulltext.polish_content (leading duplicate-title H1, trailing
promo/bio/comment/related-article rails, base64 placeholder images, bold
lead-in promotion) to every story's stored `content`. Stories whose
(polished) content is only a paywall teaser or a bot-wall page get their
`content`/`content_url` removed entirely, so the detail page falls back to
the AI summary.

Idempotent — safe to re-run; unchanged stories are left alone.

Usage:
    pipeline/.venv/Scripts/python pipeline/clean_news_boilerplate.py [--dry-run] [day files...]

Defaults to every data/news/*.json.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import fulltext

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GLOB = ROOT / "data/news"


def main() -> None:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    paths = [Path(a) for a in args if not a.startswith("--")]
    if not paths:
        paths = sorted(DEFAULT_GLOB.glob("*.json"))

    tot_polished = tot_paywall = tot_botwall = tot_unchanged = 0
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        polished = paywall = botwall = unchanged = 0
        for story in data["stories"]:
            content = story.get("content")
            if not content:
                continue
            new = fulltext.polish_content(content, story)
            if fulltext.is_paywall_teaser(new):
                story.pop("content", None)
                story.pop("content_url", None)
                paywall += 1
                continue
            if fulltext._BOT_WALL_RE.search(new):
                story.pop("content", None)
                story.pop("content_url", None)
                botwall += 1
                continue
            if new != content:
                story["content"] = new
                polished += 1
            else:
                unchanged += 1
        if not dry_run and (polished or paywall or botwall):
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        tot_polished += polished
        tot_paywall += paywall
        tot_botwall += botwall
        tot_unchanged += unchanged
        if polished or paywall or botwall:
            print(f"{path.name}: polished {polished}, paywall-dropped {paywall}, "
                  f"botwall-dropped {botwall}, unchanged {unchanged}")
    print(f"TOTAL{' (dry run)' if dry_run else ''}: polished {tot_polished}, "
          f"paywall-dropped {tot_paywall}, botwall-dropped {tot_botwall}, "
          f"unchanged {tot_unchanged}")


if __name__ == "__main__":
    main()
