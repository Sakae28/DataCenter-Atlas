"""Maintenance: re-apply content polishing to stored day files.

Pure string processing — NO refetching. Applies fulltext.polish_content
(strip a leading heading/line duplicating the story title; promote
standalone fully-bold lead-in lines to `###` headings) to every story's
stored `content`, then prints verification counts.

Usage:
    pipeline/.venv/Scripts/python pipeline/polish_content.py [day files...]

Defaults to data/news/2026-08-13.json and data/news/2026-08-14.json.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import fulltext

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FILES = [
    ROOT / "data/news/2026-08-13.json",
    ROOT / "data/news/2026-08-14.json",
]


def starts_with_duplicate_title(story: dict) -> bool:
    """True when the first content line still fuzzy-matches the title."""
    content = story.get("content") or ""
    for line in content.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        text = stripped.lstrip("#").strip() if stripped.startswith("#") else stripped
        return fulltext._titles_match(text, story.get("title", ""))
    return False


def main() -> None:
    paths = [Path(a) for a in sys.argv[1:]] or DEFAULT_FILES
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        stories = data["stories"]
        stripped = promoted = unchanged = 0
        for story in stories:
            content = story.get("content")
            if not content:
                continue
            new = fulltext.polish_content(content, story)
            if new == content:
                unchanged += 1
                continue
            if fulltext.strip_leading_title(content, story.get("title")) != content:
                stripped += 1
            if fulltext.promote_bold_leadins(content) != content:
                promoted += 1
            story["content"] = new
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        dup = sum(1 for s in stories if starts_with_duplicate_title(s))
        with_h3 = sum(1 for s in stories
                      if any(l.startswith("### ")
                             for l in (s.get("content") or "").split("\n")))
        with_companies = sum(1 for s in stories if s.get("companies"))
        print(f"{path.name}: {len(stories)} stories | title stripped: {stripped}, "
              f"bold lead-ins promoted: {promoted}, unchanged: {unchanged}")
        print(f"  after: leading duplicate title: {dup}, "
              f"with ### headings: {with_h3}, with companies: {with_companies}")


if __name__ == "__main__":
    main()
