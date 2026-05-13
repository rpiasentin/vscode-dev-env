#!/usr/bin/env python3
"""Backfill normalized phone/email fields from stored raw profile JSON."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import crawler as base
from focused_crawl import FocusConfig, output_paths, render_reports


REPO_ROOT = Path(__file__).resolve().parents[2]
RESEARCH_ROOT = REPO_ROOT / "output" / "research"
GENERIC_DB = RESEARCH_ROOT / "cisco-org-overlay" / "cisco_org_overlay.sqlite3"

FOCUSED_CONFIGS = [
    FocusConfig(
        slug="jeetu",
        display_name="Jeetu",
        default_root_alias="jeetup",
        output_root=RESEARCH_ROOT / "cisco-org-jeetu",
    ),
    FocusConfig(
        slug="oliver",
        display_name="Oliver",
        default_root_alias="otuszik",
        output_root=RESEARCH_ROOT / "cisco-org-oliver",
    ),
]


@dataclass
class BackfillSummary:
    db_path: Path
    rows_seen: int = 0
    email_populated: int = 0
    phones_populated: int = 0


def load_profile(raw_json: str) -> Dict[str, Any]:
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    profile = payload.get("profile")
    return profile if isinstance(profile, dict) else {}


def normalize_contact_fields(profile: Dict[str, Any], fallback_alias: Optional[str]) -> tuple[Optional[str], str]:
    alias = base.directory_profile_alias(profile, fallback_alias) or fallback_alias
    email = base.directory_profile_email(profile, alias)
    phones_json = json.dumps(base.extract_phone_records(profile), ensure_ascii=True)
    return email, phones_json


def backfill_focused_db(config: FocusConfig) -> BackfillSummary:
    paths = output_paths(config)
    summary = BackfillSummary(db_path=paths.db_path)
    if not paths.db_path.exists():
        return summary

    conn = sqlite3.connect(paths.db_path)
    conn.row_factory = sqlite3.Row
    try:
        people_rows = conn.execute("SELECT alias, raw_json FROM people").fetchall()
        for row in people_rows:
            profile = load_profile(row["raw_json"])
            if not profile:
                continue
            email, phones_json = normalize_contact_fields(profile, row["alias"])
            conn.execute(
                "UPDATE people SET email = ?, phones_json = ? WHERE alias = ?",
                (email, phones_json, row["alias"]),
            )
            summary.rows_seen += 1
            if email:
                summary.email_populated += 1
            if phones_json != "[]":
                summary.phones_populated += 1

        snapshot_rows = conn.execute("SELECT id, alias, raw_json FROM person_snapshots").fetchall()
        for row in snapshot_rows:
            profile = load_profile(row["raw_json"])
            if not profile:
                continue
            email, phones_json = normalize_contact_fields(profile, row["alias"])
            conn.execute(
                "UPDATE person_snapshots SET email = ?, phones_json = ? WHERE id = ?",
                (email, phones_json, row["id"]),
            )

        conn.commit()
        render_reports(conn, paths, config)
        conn.commit()
        return summary
    finally:
        conn.close()


def backfill_generic_db(db_path: Path) -> BackfillSummary:
    summary = BackfillSummary(db_path=db_path)
    if not db_path.exists():
        return summary

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        snapshot_rows = conn.execute(
            """
            SELECT id, person_id, raw_json
            FROM person_snapshots
            WHERE source_system = 'directory'
            """
        ).fetchall()
        for row in snapshot_rows:
            profile = load_profile(row["raw_json"])
            if not profile:
                continue
            email, phones_json = normalize_contact_fields(profile, row["person_id"])
            conn.execute(
                "UPDATE person_snapshots SET email = ?, phones_json = ? WHERE id = ?",
                (email, phones_json, row["id"]),
            )
            summary.rows_seen += 1
            if email:
                summary.email_populated += 1
            if phones_json != "[]":
                summary.phones_populated += 1

        conn.execute(
            """
            UPDATE people
            SET canonical_email = COALESCE(
                (SELECT s.email FROM person_snapshots s WHERE s.id = people.latest_snapshot_id),
                CASE
                    WHEN COALESCE(people.alias, people.person_id) IS NOT NULL
                     AND COALESCE(people.alias, people.person_id) != ''
                    THEN lower(COALESCE(people.alias, people.person_id)) || '@cisco.com'
                    ELSE canonical_email
                END
            )
            """
        )
        conn.commit()
        return summary
    finally:
        conn.close()


def main() -> int:
    summaries = [backfill_generic_db(GENERIC_DB)]
    summaries.extend(backfill_focused_db(config) for config in FOCUSED_CONFIGS)

    for summary in summaries:
        print(
            json.dumps(
                {
                    "db_path": str(summary.db_path),
                    "rows_seen": summary.rows_seen,
                    "email_populated": summary.email_populated,
                    "phones_populated": summary.phones_populated,
                },
                ensure_ascii=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
