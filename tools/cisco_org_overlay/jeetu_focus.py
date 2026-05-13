#!/usr/bin/env python3
"""Deterministic Jeetu-focused org crawler."""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from focused_crawl import FocusConfig, run_cli


REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = FocusConfig(
    slug="jeetu",
    display_name="Jeetu",
    default_root_alias="jeetup",
    output_root=REPO_ROOT / "output" / "research" / "cisco-org-jeetu",
)


def main(argv: Optional[Sequence[str]] = None) -> int:
    return run_cli(CONFIG, argv)


if __name__ == "__main__":
    raise SystemExit(main())
