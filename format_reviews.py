#!/usr/bin/env python3
"""Convert ported review .md files into Hugo-formatted reviews.

Input files (exported from elsewhere) look like:

    07/16/2023

    Acquire (Renegade)

    John Kaufeld and Someone Else

    INFO BOX

    Age range: 12 and up

    Play time: 90 minutes

    \\# of Players: 2-6

    Price point: $50.00

    <intro paragraphs, hard-wrapped>

    **A Bold Header**

    <body paragraphs>

    **Verdict**

    <closing paragraphs ending in a recommend / not-recommend>

This script adds YAML front matter matching the existing reviews (see
content/reviews/solar-gardens/index.md), converts **bold** headers to H2,
strips the <span dir="rtl">...</span> apostrophe artifacts, unwraps the
hard-wrapped paragraphs, and appends a Recommended! / Not recommended.
callout based on the verdict text.

Usage:
    python3 format_reviews.py                 # process content/reviews/*.md
    python3 format_reviews.py file1.md ...     # process specific files
    python3 format_reviews.py --dry-run [...]  # print, don't write

Files that already start with front matter (---) or whose basename starts
with an underscore are skipped.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_DIR = REPO_ROOT / "content" / "reviews"
DATA_AUTHORS = REPO_ROOT / "data" / "authors"
CONTENT_AUTHORS = REPO_ROOT / "content" / "authors"

# Date is stamped at noon, fixed EST offset, to match the existing reviews.
TIME_SUFFIX = "T12:00:00-05:00"

SPAN_RE = re.compile(r"<span\b[^>]*>(.*?)</span>", re.DOTALL)
# Curly punctuation seen inside the span artifacts -> straight ASCII.
SPAN_INNER_TRANS = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "″": '"',
})
HEADER_RE = re.compile(r"^\*\*(.+?)\*\*$")
DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")

NOT_REC_PATTERNS = [
    "not recommend",
    "not recommended",
    "don't recommend",
    "do not recommend",
    "would not recommend",
    "wouldn't recommend",
    "cannot recommend",
    "can't recommend",
    "hard to recommend",
    "give it a pass",
    "pass on this",
    "take a pass",
]


def yq(s: str) -> str:
    """Escape a string for use inside a double-quoted YAML scalar."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def slugify_author(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def split_authors(line: str) -> list[str]:
    # Handles "A and B", "A, B and C", "A, B, and C".
    parts = re.split(r"\s*,\s*|\s+and\s+", line.strip())
    return [p for p in (p.strip() for p in parts) if p]


def clean_text(text: str) -> str:
    """Strip the <span> artifacts, keeping the inner mark normalized to ASCII."""
    return SPAN_RE.sub(lambda m: m.group(1).translate(SPAN_INNER_TRANS), text)


def blocks_of(raw: str) -> list[str]:
    """Split into blank-line-separated paragraphs, each unwrapped to one line."""
    out = []
    for chunk in re.split(r"\n\s*\n", raw):
        joined = " ".join(line.strip() for line in chunk.splitlines() if line.strip())
        if joined:
            out.append(joined)
    return out


def parse_playtime(value: str) -> tuple[str, str]:
    """'30-45 minutes' -> ('30','45'); '90 minutes' -> ('90','90').

    Some files combine setup + play, e.g. 'Set-up/Play time: 5 to set up,
    30 minutes to play'. Drop the set-up clause before reading numbers.
    """
    segments = re.split(r"[;,]", value)
    kept = [s for s in segments if "set" not in s.lower()] or segments
    text = " ".join(kept)
    nums = [int(n) for n in re.findall(r"\d+", text)]
    if not nums:
        return ("", "")
    if "hour" in text.lower():
        nums = [n * 60 for n in nums]
    lo, hi = nums[0], nums[1] if len(nums) > 1 else nums[0]
    return (str(lo), str(hi))


def parse_players(value: str) -> tuple[str, str]:
    """'2-6' -> ('2','6'); '4' -> ('4','4')."""
    nums = re.findall(r"\d+", value)
    if not nums:
        return ("", "")
    if len(nums) == 1:
        return (nums[0], nums[0])
    return (nums[0], nums[1])


def first_sentence(text: str) -> str:
    m = re.match(r"^(.*?[.!?])(?:\s|$)", text)
    return (m.group(1) if m else text).strip()


def find_info(blocks: list[str], *prefixes: str) -> tuple[int, str]:
    """Return (index, value) for the first block matching any prefix.

    Tolerates the line being wrapped in bold (**...**), an escaped leading
    '\\#', and trailing review notes like '<<< PLEASE VERIFY' or '&lt;&lt;...'.
    """
    for i, b in enumerate(blocks):
        stripped = b.strip().strip("*").lstrip("\\").strip()
        low = stripped.lower()
        for prefix in prefixes:
            if low.startswith(prefix.lower()):
                value = stripped[len(prefix):].lstrip(": ").strip()
                value = value.rstrip("*").strip()
                value = re.sub(r"\s*(?:&lt;|<){2,}.*$", "", value).strip()
                return i, value
    return -1, ""


def detect_recommendation(verdict_text: str) -> tuple[str, bool]:
    """Return (callout, confident)."""
    low = verdict_text.lower()
    not_rec = "\n> [!danger] Not recommended.\n{icon=\"xmark\"}"
    rec = "\n> [!success] Recommended!"
    for pat in NOT_REC_PATTERNS:
        if pat in low:
            return not_rec, True
    if "recommend" in low:
        return rec, True
    # No signal at all -> default to recommended but flag it.
    return rec, False


def ensure_author(name: str, created: set[str]) -> str:
    slug = slugify_author(name)
    json_path = DATA_AUTHORS / f"{slug}.json"
    if not json_path.exists():
        DATA_AUTHORS.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(
                {
                    "name": name,
                    "image": "img/profile_300x300.jpg?DOESNT_EXIST_YET",
                    "bio": "Placeholder bio.",
                    "social": [],
                },
                indent=4,
            )
            + "\n",
            encoding="utf-8",
        )
        created.add(f"data/authors/{slug}.json")
    index_path = CONTENT_AUTHORS / slug / "_index.md"
    if not index_path.exists():
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(
            f'---\ntitle: "{name}"\n---\n\nPlaceholder author bio.\n',
            encoding="utf-8",
        )
        created.add(f"content/authors/{slug}/_index.md")
    return slug


def convert(path: Path, created_authors: set[str], warnings: list[str]) -> str | None:
    raw = path.read_text(encoding="utf-8")
    if raw.lstrip().startswith("---"):
        return None  # already has front matter
    raw = clean_text(raw)
    blocks = blocks_of(raw)
    if len(blocks) < 8:
        warnings.append(f"{path.name}: too few blocks ({len(blocks)}); skipped")
        return None

    # --- header blocks -------------------------------------------------
    blocks[0] = re.sub(r"/+", "/", blocks[0]).strip()  # tolerate '01/24//2018'
    date_m = DATE_RE.match(blocks[0])
    if not date_m:
        warnings.append(f"{path.name}: first block is not a date ('{blocks[0][:30]}'); skipped")
        return None
    mm, dd, yyyy = date_m.groups()
    date_str = f"{yyyy}-{int(mm):02d}-{int(dd):02d}{TIME_SUFFIX}"

    title = blocks[1].strip()
    author_names = split_authors(blocks[2])
    author_slugs = [ensure_author(n, created_authors) for n in author_names]

    # --- info box ------------------------------------------------------
    _, age_val = find_info(blocks, "Age range")
    _, playtime_val = find_info(blocks, "Play time", "Set-up/Play time",
                                "Setup/Play time", "Play/set-up time")
    _, players_val = find_info(blocks, "# of Players", "Number of Players")
    price_i, price_val = find_info(blocks, "Price point", "Price")
    if price_i < 0:
        warnings.append(f"{path.name}: no 'Price point' block; skipped")
        return None
    if re.search(r"verify|&lt;|<<", price_val, re.I):
        warnings.append(f"{path.name}: price had a review note; verify '{price_val}'")

    age_num = (re.findall(r"\d+", age_val) or [""])[0]
    pt_min, pt_max = parse_playtime(playtime_val)
    pl_min, pl_max = parse_players(players_val)
    for label, val in (("age", age_num), ("playtime", pt_min), ("players", pl_min)):
        if not val:
            warnings.append(f"{path.name}: could not parse {label}; check tags")

    body_blocks = blocks[price_i + 1:]
    if not body_blocks:
        warnings.append(f"{path.name}: no body content; skipped")
        return None

    # --- summary -------------------------------------------------------
    intro_sentence = first_sentence(body_blocks[0])
    facts = []
    if age_num:
        facts.append(f"{age_num}+")
    if pl_min:
        facts.append(f"{players_val} players")
    if pt_min:
        facts.append(playtime_val)
    summary = (intro_sentence + (" " + ", ".join(facts) if facts else "")).strip()

    # --- front matter --------------------------------------------------
    fm = ['---', f'title: "Review: {yq(title)}"',
          f'description: "A review of {yq(title)}"', f"date: {date_str}"]
    if author_slugs:
        fm.append("authors:")
        fm += [f'    - "{s}"' for s in author_slugs]
    fm.append(f'summary: "{yq(summary)}"')
    fm.append("topics: ")
    fm.append('    - "review"')
    fm.append("tags:")
    if age_num:
        fm.append(f'    - "Age: {age_num}+"')
    if pt_min:
        fm.append(f'    - "Min Playtime: {pt_min}m"')
        fm.append(f'    - "Max Playtime: {pt_max}m"')
    if pl_min:
        fm.append(f'    - "Min Players: {pl_min}"')
        fm.append(f'    - "Max Players: {pl_max}"')
    fm.append("---")

    # --- quick facts ---------------------------------------------------
    quick = [
        "",
        "> [!info]+ Quick Facts",
        f"> Age range: {age_val}\\",
        f"> Play time: {playtime_val}\\",
        f"> \\# of Players: {players_val}\\",
        f"> Price point: {price_val}",
        "",
    ]

    # --- body ----------------------------------------------------------
    body_lines: list[str] = []
    verdict_started = False
    verdict_text_parts: list[str] = []
    for b in body_blocks:
        hm = HEADER_RE.match(b)
        if hm:
            heading = hm.group(1).strip()
            if "verdict" in heading.lower():
                verdict_started = True
            body_lines.append(f"## {heading}")
        else:
            body_lines.append(b)
            if verdict_started:
                verdict_text_parts.append(b)
        body_lines.append("")  # blank line between blocks

    # If there was no explicit Verdict header, fall back to the last block.
    verdict_text = " ".join(verdict_text_parts) if verdict_text_parts else body_blocks[-1]
    callout, confident = detect_recommendation(verdict_text)
    if not confident:
        warnings.append(f"{path.name}: could not detect recommendation; defaulted to Recommended!")

    # Trim trailing blank lines from body, then add the callout.
    while body_lines and body_lines[-1] == "":
        body_lines.pop()

    out = "\n".join(fm) + "\n" + "\n".join(quick) + "\n" + "\n".join(body_lines) + "\n" + callout + "\n"
    return out


def iter_targets(args: list[str]) -> list[Path]:
    paths: list[Path] = []
    raw_args = [a for a in args if not a.startswith("--")]
    sources = [Path(a) for a in raw_args] if raw_args else [DEFAULT_DIR]
    for src in sources:
        if src.is_dir():
            paths.extend(sorted(src.glob("*.md")))
        elif src.exists():
            paths.append(src)
        else:
            print(f"  ! not found: {src}", file=sys.stderr)
    # Skip underscore-prefixed files (e.g. _index.md).
    return [p for p in paths if not p.name.startswith("_")]


def main() -> int:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    created_authors: set[str] = set()
    warnings: list[str] = []
    converted = skipped = 0

    for path in iter_targets(args):
        result = convert(path, created_authors, warnings)
        if result is None:
            skipped += 1
            continue
        if dry_run:
            print(f"===== {path} =====")
            print(result)
        else:
            path.write_text(result, encoding="utf-8")
        converted += 1

    print(f"\nConverted: {converted}   Skipped: {skipped}")
    if created_authors:
        print("Created author placeholders:")
        for a in sorted(created_authors):
            print(f"  + {a}")
    if warnings:
        print("\nWarnings (review these manually):")
        for w in warnings:
            print(f"  ! {w}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
