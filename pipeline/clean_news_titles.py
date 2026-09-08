"""Maintenance: clean Google News title suffixes and duplicate summaries.

Two data-quality fixes applied to stored stories (no refetching):

1. Titles ending in " - <Publisher>" — the Google News RSS title suffix the
   pipeline used to keep (it is now stripped at ingest, see
   process.strip_gn_publisher). A trailing segment is removed only when it
   matches (case/punctuation-insensitive) one of the story's own
   sources[].publisher values or a known-outlet list, so genuine " - " in a
   headline (e.g. "Phase 1 - 72MW") is never touched. Loops for doubled
   suffixes (" - CHOSUNBIZ - Chosunbiz").
2. Summaries that merely repeat the headline (GN RSS "snippets" are the
   headline text): replaced with the first substantive paragraph of the
   stored full-text content (~200 chars, markdown stripped), or emptied when
   no content exists — the site hides empty/duplicate summaries
   (hasDistinctSummary in site/src/lib/news.ts).

Idempotent — safe to re-run; unchanged stories are left alone.

Usage:
    pipeline/.venv/Scripts/python pipeline/clean_news_titles.py [--dry-run] [day files...]

Defaults to every data/news/*.json.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import process

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GLOB = ROOT / "data/news"

# Well-known outlet brands, mirroring DOMAIN_BRANDS values in
# site/src/lib/news.ts plus observed Google News publisher strings. Compared
# after normalization, so case/spacing variants collapse.
KNOWN_OUTLETS = [
    "NBC News", "The Korea Herald", "The Korea Times", "The Japan Times",
    "Nikkei Asia", "Nikkei", "The Standard", "Inquirer.net",
    "The Jakarta Post", "Tempo", "Seoul Economic Daily", "Chosun Biz",
    "Chosunbiz", "Maeil Business News", "Data Center Dynamics",
    "Data Center Knowledge", "W.Media", "Capacity Media", "Yahoo Finance",
    "Moomoo News", "News On Japan", "Urban Land", "Simply Wall St",
    "Proactive Investors", "MLex", "CRN Asia", "DIGITIMES", "digitimes",
    "Bamboo Works", "The Economic Times", "MarketsandMarkets", "Reuters",
    "Bloomberg", "CNA", "The Straits Times", "South China Morning Post",
    "ZDNET", "TechCrunch", "毎日新聞", "Mainichi Shimbun",
]


def _norm_outlet(text: str) -> str:
    """Aggressive normalization for outlet-name matching (keeps CJK)."""
    return re.sub(r"[^\w]+", "", text.lower())


_KNOWN_OUTLET_NORMS = {_norm_outlet(o) for o in KNOWN_OUTLETS}


def clean_title(story: dict) -> str | None:
    """Return the title with trailing publisher suffix(es) removed, or None
    when nothing should change."""
    title = story.get("title", "")
    own = {_norm_outlet(s["publisher"]) for s in story.get("sources", [])
           if s.get("publisher")}
    allowed = own | _KNOWN_OUTLET_NORMS
    cleaned = title
    while " - " in cleaned:
        head, _, tail = cleaned.rpartition(" - ")
        if not head.strip() or _norm_outlet(tail.strip()) not in allowed:
            break
        cleaned = head.strip()
    return cleaned if cleaned != title else None


def main() -> None:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    paths = [Path(a) for a in args if not a.startswith("--")]
    if not paths:
        paths = sorted(DEFAULT_GLOB.glob("*.json"))

    tot_title = tot_rederived = tot_emptied = 0
    samples: list[str] = []
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        n_title = n_rederived = n_emptied = 0
        for story in data["stories"]:
            changes = []
            new_title = clean_title(story)
            if new_title is not None:
                changes.append(f"title: {story['title']!r} -> {new_title!r}")
                story["title"] = new_title
                n_title += 1
            summary = story.get("summary") or ""
            if summary and process.summaries_equivalent(story["title"], summary):
                derived = process.derive_summary(story.get("content"))
                # The first paragraph can itself be the headline restated;
                # don't keep a "new" summary that still repeats the title.
                if derived and process.summaries_equivalent(story["title"], derived):
                    derived = ""
                story["summary"] = derived
                if story["summary"] != summary:
                    if derived:
                        n_rederived += 1
                    else:
                        n_emptied += 1
                    changes.append(
                        f"summary: {summary[:80]!r} -> {derived[:80]!r}")
            if changes and len(samples) < 10:
                samples.append(f"[{path.name} {story['id']}]\n  "
                               + "\n  ".join(changes))
        if not dry_run and (n_title or n_rederived or n_emptied):
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        tot_title += n_title
        tot_rederived += n_rederived
        tot_emptied += n_emptied
        if n_title or n_rederived or n_emptied:
            print(f"{path.name}: titles-stripped {n_title}, "
                  f"summaries-rederived {n_rederived}, "
                  f"summaries-emptied {n_emptied}")
    print(f"TOTAL{' (dry run)' if dry_run else ''}: titles-stripped {tot_title}, "
          f"summaries-rederived {tot_rederived}, summaries-emptied {tot_emptied}")
    if samples:
        print("\nSAMPLE CHANGES:")
        print("\n".join(samples))


if __name__ == "__main__":
    main()
