#!/usr/bin/env python3
"""Parse a markdown speaker script into per-slide text files."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys


SLIDE_HEADER_RE = re.compile(r"^\s{0,3}#{1,6}\s*Slide\s*(\d+)\b", re.IGNORECASE)


def die(message: str, code: int = 1) -> None:
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(code)


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def parse_markdown(md_text: str) -> dict[int, str]:
    sections: dict[int, list[str]] = {}
    current_slide: int | None = None

    for line in md_text.splitlines():
        header_match = SLIDE_HEADER_RE.match(line)
        if header_match:
            current_slide = int(header_match.group(1))
            sections.setdefault(current_slide, [])
            continue
        if current_slide is not None:
            sections[current_slide].append(line)

    parsed: dict[int, str] = {}
    for slide_num, lines in sections.items():
        content = clean_text("\n".join(lines))
        if content:
            parsed[slide_num] = content
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create slideXX.txt files from markdown headings like '## Slide 1 ...'."
    )
    parser.add_argument("--md", required=True, help="Path to markdown speaker script")
    parser.add_argument("--out-dir", required=True, help="Output directory for slideXX.txt")
    parser.add_argument(
        "--expected-slides",
        type=int,
        default=0,
        help="If >0, require exactly this many slide sections",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    md_path = Path(args.md).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()

    if not md_path.exists():
        die(f"Markdown file not found: {md_path}")
    if not md_path.is_file():
        die(f"Markdown path is not a file: {md_path}")

    text = md_path.read_text(encoding="utf-8")
    sections = parse_markdown(text)
    if not sections:
        die("No slide sections found. Expected headings like '## Slide 1 - ...'")

    slide_numbers = sorted(sections.keys())
    missing = [n for n in range(slide_numbers[0], slide_numbers[-1] + 1) if n not in sections]
    if missing:
        die(f"Missing slide sections in markdown: {missing}")

    if args.expected_slides > 0 and len(slide_numbers) != args.expected_slides:
        die(
            f"Markdown slide section count mismatch: found {len(slide_numbers)} "
            f"but expected {args.expected_slides}"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    for slide_num in slide_numbers:
        out_path = out_dir / f"slide{slide_num:02d}.txt"
        out_path.write_text(sections[slide_num].strip() + "\n", encoding="utf-8")

    index_lines = []
    total_words = 0
    for slide_num in slide_numbers:
        words = len([w for w in sections[slide_num].split() if w.strip()])
        total_words += words
        index_lines.append(f"slide{slide_num:02d}.txt\twords={words}")
    index_lines.append(f"TOTAL\twords={total_words}")
    (out_dir / "index.txt").write_text("\n".join(index_lines) + "\n", encoding="utf-8")

    print(f"Wrote {len(slide_numbers)} slide scripts to {out_dir}")


if __name__ == "__main__":
    main()
