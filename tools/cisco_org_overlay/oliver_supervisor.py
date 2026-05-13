#!/usr/bin/env python3
"""Supervisor for the deterministic Oliver-focused crawler."""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from focused_crawl import FocusConfig
from focused_supervisor import run_supervisor_cli


REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = FocusConfig(
    slug="oliver",
    display_name="Oliver",
    default_root_alias="otuszik",
    output_root=REPO_ROOT / "output" / "research" / "cisco-org-oliver",
)


def main(argv: Optional[Sequence[str]] = None) -> int:
    return run_supervisor_cli(CONFIG, argv)


if __name__ == "__main__":
    raise SystemExit(main())
