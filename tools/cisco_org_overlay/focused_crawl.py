#!/usr/bin/env python3
"""Reusable deterministic focused org crawler."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import traceback
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple

import crawler as base


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SESSION_ROOT = REPO_ROOT / "output" / "research" / "cisco-org-overlay"


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS crawl_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    root_alias TEXT NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS people (
    alias TEXT PRIMARY KEY,
    full_name TEXT,
    title TEXT,
    organization_name TEXT,
    email TEXT,
    manager_alias TEXT,
    direct_report_count INTEGER,
    employee_direct_count INTEGER,
    contingent_direct_count INTEGER,
    location_text TEXT,
    assistant_alias TEXT,
    assistant_name TEXT,
    worker_type TEXT,
    phones_json TEXT,
    raw_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_run_id INTEGER
);

CREATE TABLE IF NOT EXISTS person_snapshots (
    id INTEGER PRIMARY KEY,
    crawl_run_id INTEGER NOT NULL REFERENCES crawl_runs(id) ON DELETE CASCADE,
    alias TEXT NOT NULL REFERENCES people(alias) ON DELETE CASCADE,
    captured_at TEXT NOT NULL,
    full_name TEXT,
    title TEXT,
    organization_name TEXT,
    email TEXT,
    manager_alias TEXT,
    direct_report_count INTEGER,
    employee_direct_count INTEGER,
    contingent_direct_count INTEGER,
    location_text TEXT,
    assistant_alias TEXT,
    assistant_name TEXT,
    worker_type TEXT,
    phones_json TEXT,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS org_edges (
    parent_alias TEXT NOT NULL,
    child_alias TEXT NOT NULL,
    crawl_run_id INTEGER NOT NULL REFERENCES crawl_runs(id) ON DELETE CASCADE,
    discovered_at TEXT NOT NULL,
    first_seen_at TEXT,
    last_seen_at TEXT,
    last_run_id INTEGER,
    active INTEGER NOT NULL DEFAULT 1,
    source_json TEXT,
    PRIMARY KEY (parent_alias, child_alias)
);

CREATE TABLE IF NOT EXISTS org_edge_observations (
    id INTEGER PRIMARY KEY,
    parent_alias TEXT NOT NULL,
    child_alias TEXT NOT NULL,
    crawl_run_id INTEGER NOT NULL REFERENCES crawl_runs(id) ON DELETE CASCADE,
    observed_at TEXT NOT NULL,
    source_json TEXT
);

CREATE TABLE IF NOT EXISTS manager_checks (
    alias TEXT PRIMARY KEY,
    crawl_run_id INTEGER NOT NULL REFERENCES crawl_runs(id) ON DELETE CASCADE,
    expected_directs INTEGER,
    discovered_directs INTEGER,
    status TEXT NOT NULL,
    note TEXT,
    last_checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS unresolved_aliases (
    alias TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    source_parent_alias TEXT,
    source_run_id INTEGER,
    status TEXT NOT NULL,
    note TEXT
);

CREATE INDEX IF NOT EXISTS idx_org_edges_parent_active
    ON org_edges(parent_alias, active, child_alias);

CREATE INDEX IF NOT EXISTS idx_org_edges_child_active
    ON org_edges(child_alias, active, parent_alias);

CREATE INDEX IF NOT EXISTS idx_org_edge_observations_run_parent
    ON org_edge_observations(crawl_run_id, parent_alias, child_alias);
"""

AUTH_EXPIRED_EXIT_CODE = 75
AUTH_EXPIRED_STATUSES = {401}
SKIPPABLE_MANAGER_STATUSES = ("complete", "leaf", "fetch_404")


class AuthExpiredError(RuntimeError):
    """Raised when the crawler should stop cleanly so the supervisor can refresh auth."""

    def __init__(self, stage: str, alias: str, status: Optional[int], url: str) -> None:
        super().__init__(f"auth expired during {stage} fetch alias={alias} status={status} url={url}")
        self.stage = stage
        self.alias = alias
        self.status = status
        self.url = url


@dataclass(frozen=True)
class FocusConfig:
    slug: str
    display_name: str
    default_root_alias: str
    output_root: Path
    session_root: Path = DEFAULT_SESSION_ROOT

    @property
    def db_filename(self) -> str:
        return f"{self.slug}_focus.sqlite3"

    @property
    def crawler_script_name(self) -> str:
        return f"{self.slug}_focus.py"

    @property
    def supervisor_script_name(self) -> str:
        return f"{self.slug}_supervisor.py"

    @property
    def status_title(self) -> str:
        return f"{self.display_name} Focus Status"

    @property
    def supervisor_title(self) -> str:
        return f"{self.display_name} Supervisor Status"

    @property
    def crawl_label(self) -> str:
        return f"{self.display_name}-focused deterministic crawl"

    @property
    def pycache_prefix(self) -> str:
        return f"/tmp/cisco-org-{self.slug}-pyc"

    @property
    def storage_state_path(self) -> Path:
        return self.session_root / "storage-state.json"

    @property
    def extra_headers_path(self) -> Path:
        return self.session_root / "directory-extra-headers.json"


@dataclass(frozen=True)
class OutputPaths:
    output_root: Path
    db_path: Path
    log_path: Path
    report_dir: Path
    status_md: Path
    status_json: Path
    deficit_csv: Path
    unresolved_csv: Path
    missing_child_csv: Path
    people_csv: Path


def output_paths(config: FocusConfig, output_root: Optional[Path] = None) -> OutputPaths:
    resolved_root = output_root or config.output_root
    report_dir = resolved_root / "reports"
    return OutputPaths(
        output_root=resolved_root,
        db_path=resolved_root / config.db_filename,
        log_path=resolved_root / "logs" / "crawler.log",
        report_dir=report_dir,
        status_md=report_dir / "status.md",
        status_json=report_dir / "status.json",
        deficit_csv=report_dir / "manager-deficits.csv",
        unresolved_csv=report_dir / "unresolved-aliases.csv",
        missing_child_csv=report_dir / "missing-child-profiles.csv",
        people_csv=report_dir / "people-latest.csv",
    )


def utc_now() -> str:
    return base.utc_now()


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def log(message: str, paths: OutputPaths) -> None:
    ensure_dir(paths.log_path.parent)
    line = f"[{utc_now()}] {message}"
    print(line)
    with paths.log_path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def connect_db(paths: OutputPaths) -> sqlite3.Connection:
    ensure_dir(paths.db_path.parent)
    conn = sqlite3.connect(str(paths.db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    ensure_focused_schema(conn)
    return conn


def ensure_focused_schema(conn: sqlite3.Connection) -> None:
    """Apply additive focused-schema migrations needed by newer crawler runs."""
    columns = table_columns(conn, "org_edges")
    needs_migration = False
    if "first_seen_at" not in columns:
        conn.execute("ALTER TABLE org_edges ADD COLUMN first_seen_at TEXT")
        needs_migration = True
    if "last_seen_at" not in columns:
        conn.execute("ALTER TABLE org_edges ADD COLUMN last_seen_at TEXT")
        needs_migration = True
    if "last_run_id" not in columns:
        conn.execute("ALTER TABLE org_edges ADD COLUMN last_run_id INTEGER")
        needs_migration = True
    if "active" not in columns:
        conn.execute("ALTER TABLE org_edges ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        needs_migration = True
    needs_migration = needs_migration or not table_exists(conn, "org_edge_observations")
    needs_migration = needs_migration or not index_exists(conn, "idx_org_edges_parent_active")
    needs_migration = needs_migration or not index_exists(conn, "idx_org_edges_child_active")
    needs_migration = needs_migration or not index_exists(conn, "idx_org_edge_observations_run_parent")
    if not needs_migration:
        return
    conn.execute(
        """
        UPDATE org_edges
        SET
            first_seen_at = COALESCE(first_seen_at, discovered_at),
            last_seen_at = COALESCE(last_seen_at, discovered_at),
            last_run_id = COALESCE(last_run_id, crawl_run_id),
            active = COALESCE(active, 1)
        """
    )
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS org_edge_observations (
            id INTEGER PRIMARY KEY,
            parent_alias TEXT NOT NULL,
            child_alias TEXT NOT NULL,
            crawl_run_id INTEGER NOT NULL REFERENCES crawl_runs(id) ON DELETE CASCADE,
            observed_at TEXT NOT NULL,
            source_json TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_org_edges_parent_active
            ON org_edges(parent_alias, active, child_alias);

        CREATE INDEX IF NOT EXISTS idx_org_edges_child_active
            ON org_edges(child_alias, active, parent_alias);

        CREATE INDEX IF NOT EXISTS idx_org_edge_observations_run_parent
            ON org_edge_observations(crawl_run_id, parent_alias, child_alias);
        """
    )
    conn.commit()


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")}


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def index_exists(conn: sqlite3.Connection, index_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
        (index_name,),
    ).fetchone()
    return row is not None


def maybe_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    return text or None


def pick(obj: Any, *paths: str) -> Any:
    return base.first_value(obj, *paths)


def normalize_profile(alias: str, bundle: Dict[str, Any]) -> Dict[str, Any]:
    profile = bundle.get("profile") or {}
    alias = base.directory_profile_alias(profile, alias) or alias
    first_name = maybe_text(pick(profile, "firstName"))
    last_name = maybe_text(pick(profile, "lastName"))
    full_name = (
        maybe_text(pick(profile, "fullName", "name", "displayName"))
        or " ".join(part for part in (first_name, last_name) if part)
        or alias
    )
    title = maybe_text(
        pick(profile, "preferredJobTitle", "ciscoJobTitle", "jobTitle", "title")
    )
    organization_name = maybe_text(
        pick(profile, "orgName", "organizationName", "organization", "departmentName", "deptName")
    )
    email = base.directory_profile_email(profile, alias)
    manager_alias = base.normalize_alias(
        pick(
            profile,
            "manager.userId",
            "leader.userId",
            "reportsTo.userId",
            "leader.alias",
            "managerAlias",
            "mgrUserId",
        )
    )
    assistant_alias = base.normalize_alias(
        pick(profile, "assistant.userId", "assistant.alias", "assistant.email")
    )
    assistant_name = maybe_text(
        pick(profile, "assistant.fullName", "assistant.name", "assistant.displayName")
    )
    direct_total, employee_directs, contingent_directs = base.parse_direct_counts(
        pick(
            profile,
            "directReportsCount",
            "directReportCount",
            "reportsCount",
            "reportingCount",
        )
    )
    phones = base.extract_phone_records(profile)
    location_text = maybe_text(
        pick(profile, "location", "locationText", "officeLocation", "geoRole")
    )
    worker_type = maybe_text(pick(profile, "workerType", "employmentType", "type"))
    return {
        "alias": alias,
        "full_name": full_name,
        "title": title,
        "organization_name": organization_name,
        "email": email,
        "manager_alias": manager_alias,
        "direct_report_count": direct_total,
        "employee_direct_count": employee_directs,
        "contingent_direct_count": contingent_directs,
        "location_text": location_text,
        "assistant_alias": assistant_alias,
        "assistant_name": assistant_name,
        "worker_type": worker_type,
        "phones_json": json.dumps(phones, ensure_ascii=True),
        "raw_json": json.dumps(bundle, ensure_ascii=True),
    }


def fetch_profile_bundle(opener: Any, alias: str) -> Dict[str, Any]:
    profile = base.request_json(opener, base.DIRECTORY_PROFILE.format(alias=alias))
    return {"profile": profile}


def fetch_direct_reports(opener: Any, alias: str) -> List[Dict[str, Any]]:
    return base.fetch_all_direct_reports(opener, alias)


def is_auth_expired(exc: base.FetchError) -> bool:
    return exc.status in AUTH_EXPIRED_STATUSES


def raise_auth_expired(stage: str, alias: str, exc: base.FetchError, paths: OutputPaths) -> None:
    log(f"auth expired during {stage} fetch alias={alias} status={exc.status} url={exc.url}", paths)
    raise AuthExpiredError(stage=stage, alias=alias, status=exc.status, url=exc.url) from exc


def start_run(conn: sqlite3.Connection, root_alias: str, notes: str) -> int:
    cur = conn.execute(
        "INSERT INTO crawl_runs (started_at, status, root_alias, notes) VALUES (?, 'running', ?, ?)",
        (utc_now(), root_alias, notes),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, status: str) -> None:
    conn.execute(
        "UPDATE crawl_runs SET finished_at = ?, status = ? WHERE id = ?",
        (utc_now(), status, run_id),
    )
    conn.commit()


def latest_run(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM crawl_runs ORDER BY id DESC LIMIT 1").fetchone()


def refresh_anchor_from_notes(notes: Optional[str], fallback_run_id: Optional[int] = None) -> Optional[int]:
    if not notes:
        return fallback_run_id
    match = re.search(r"refresh-resume from run=(\d+)", notes)
    if match:
        return int(match.group(1))
    if "refresh-existing" in notes:
        return fallback_run_id
    return None


def latest_incomplete_refresh_anchor(conn: sqlite3.Connection) -> Optional[int]:
    row = conn.execute(
        """
        SELECT id, notes
        FROM crawl_runs
        WHERE status IN ('auth_expired', 'failed', 'running')
          AND (
              notes LIKE '%refresh-existing%'
              OR notes LIKE '%refresh-resume from run=%'
          )
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return None
    return refresh_anchor_from_notes(row["notes"], int(row["id"]))


def upsert_person(conn: sqlite3.Connection, run_id: int, person: Dict[str, Any]) -> None:
    existing = conn.execute("SELECT first_seen_at FROM people WHERE alias = ?", (person["alias"],)).fetchone()
    first_seen = existing["first_seen_at"] if existing else utc_now()
    conn.execute(
        """
        INSERT INTO people (
            alias, full_name, title, organization_name, email, manager_alias,
            direct_report_count, employee_direct_count, contingent_direct_count,
            location_text, assistant_alias, assistant_name, worker_type, phones_json,
            raw_json, first_seen_at, last_seen_at, last_run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(alias) DO UPDATE SET
            full_name = excluded.full_name,
            title = excluded.title,
            organization_name = excluded.organization_name,
            email = excluded.email,
            manager_alias = excluded.manager_alias,
            direct_report_count = excluded.direct_report_count,
            employee_direct_count = excluded.employee_direct_count,
            contingent_direct_count = excluded.contingent_direct_count,
            location_text = excluded.location_text,
            assistant_alias = excluded.assistant_alias,
            assistant_name = excluded.assistant_name,
            worker_type = excluded.worker_type,
            phones_json = excluded.phones_json,
            raw_json = excluded.raw_json,
            last_seen_at = excluded.last_seen_at,
            last_run_id = excluded.last_run_id
        """,
        (
            person["alias"],
            person.get("full_name"),
            person.get("title"),
            person.get("organization_name"),
            person.get("email"),
            person.get("manager_alias"),
            person.get("direct_report_count"),
            person.get("employee_direct_count"),
            person.get("contingent_direct_count"),
            person.get("location_text"),
            person.get("assistant_alias"),
            person.get("assistant_name"),
            person.get("worker_type"),
            person.get("phones_json"),
            person.get("raw_json"),
            first_seen,
            utc_now(),
            run_id,
        ),
    )
    conn.execute(
        """
        INSERT INTO person_snapshots (
            crawl_run_id, alias, captured_at, full_name, title, organization_name, email,
            manager_alias, direct_report_count, employee_direct_count, contingent_direct_count,
            location_text, assistant_alias, assistant_name, worker_type, phones_json, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            person["alias"],
            utc_now(),
            person.get("full_name"),
            person.get("title"),
            person.get("organization_name"),
            person.get("email"),
            person.get("manager_alias"),
            person.get("direct_report_count"),
            person.get("employee_direct_count"),
            person.get("contingent_direct_count"),
            person.get("location_text"),
            person.get("assistant_alias"),
            person.get("assistant_name"),
            person.get("worker_type"),
            person.get("phones_json"),
            person.get("raw_json"),
        ),
    )


def upsert_edge(conn: sqlite3.Connection, parent_alias: str, child_alias: str, run_id: int, source_json: str) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO org_edges (
            parent_alias, child_alias, crawl_run_id, discovered_at,
            first_seen_at, last_seen_at, last_run_id, active, source_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
        ON CONFLICT(parent_alias, child_alias) DO UPDATE SET
            crawl_run_id = excluded.crawl_run_id,
            discovered_at = excluded.discovered_at,
            last_seen_at = excluded.last_seen_at,
            last_run_id = excluded.last_run_id,
            active = 1,
            source_json = excluded.source_json
        """,
        (parent_alias, child_alias, run_id, now, now, now, run_id, source_json),
    )
    conn.execute(
        """
        INSERT INTO org_edge_observations (
            parent_alias, child_alias, crawl_run_id, observed_at, source_json
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (parent_alias, child_alias, run_id, now, source_json),
    )


def mark_inactive_child_edges(conn: sqlite3.Connection, parent_alias: str, run_id: int) -> None:
    conn.execute(
        """
        UPDATE org_edges
        SET active = 0,
            last_seen_at = COALESCE(last_seen_at, discovered_at)
        WHERE parent_alias = ?
          AND COALESCE(last_run_id, crawl_run_id) != ?
          AND COALESCE(active, 1) = 1
        """,
        (parent_alias, run_id),
    )


def upsert_manager_check(
    conn: sqlite3.Connection,
    alias: str,
    run_id: int,
    expected_directs: Optional[int],
    discovered_directs: int,
    status: str,
    note: str,
) -> None:
    conn.execute(
        """
        INSERT INTO manager_checks (
            alias, crawl_run_id, expected_directs, discovered_directs, status, note, last_checked_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(alias) DO UPDATE SET
            crawl_run_id = excluded.crawl_run_id,
            expected_directs = excluded.expected_directs,
            discovered_directs = excluded.discovered_directs,
            status = excluded.status,
            note = excluded.note,
            last_checked_at = excluded.last_checked_at
        """,
        (alias, run_id, expected_directs, discovered_directs, status, note, utc_now()),
    )


def upsert_unresolved_alias(
    conn: sqlite3.Connection,
    alias: str,
    run_id: int,
    parent_alias: Optional[str],
    status: str,
    note: str,
) -> None:
    existing = conn.execute(
        "SELECT first_seen_at FROM unresolved_aliases WHERE alias = ?",
        (alias,),
    ).fetchone()
    first_seen = existing["first_seen_at"] if existing else utc_now()
    conn.execute(
        """
        INSERT INTO unresolved_aliases (
            alias, first_seen_at, last_seen_at, source_parent_alias, source_run_id, status, note
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(alias) DO UPDATE SET
            last_seen_at = excluded.last_seen_at,
            source_parent_alias = COALESCE(excluded.source_parent_alias, unresolved_aliases.source_parent_alias),
            source_run_id = excluded.source_run_id,
            status = excluded.status,
            note = excluded.note
        """,
        (alias, first_seen, utc_now(), parent_alias, run_id, status, note),
    )


def clear_unresolved_alias(conn: sqlite3.Connection, alias: str) -> None:
    conn.execute("DELETE FROM unresolved_aliases WHERE alias = ?", (alias,))


def known_aliases(conn: sqlite3.Connection) -> set[str]:
    return {row["alias"] for row in conn.execute("SELECT alias FROM people")}


def checked_aliases(conn: sqlite3.Connection) -> set[str]:
    placeholders = ",".join("?" for _ in SKIPPABLE_MANAGER_STATUSES)
    return {
        row["alias"]
        for row in conn.execute(
            f"SELECT alias FROM manager_checks WHERE status IN ({placeholders})",
            SKIPPABLE_MANAGER_STATUSES,
        )
    }


def refreshed_aliases_since(conn: sqlite3.Connection, run_floor: int) -> set[str]:
    return {
        row["alias"]
        for row in conn.execute(
            "SELECT alias FROM people WHERE COALESCE(last_run_id, 0) >= ?",
            (run_floor,),
        )
    }


def checked_aliases_since(conn: sqlite3.Connection, run_floor: int) -> set[str]:
    placeholders = ",".join("?" for _ in SKIPPABLE_MANAGER_STATUSES)
    return {
        row["alias"]
        for row in conn.execute(
            f"""
            SELECT alias
            FROM manager_checks
            WHERE COALESCE(crawl_run_id, 0) >= ?
              AND status IN ({placeholders})
            """,
            (run_floor, *SKIPPABLE_MANAGER_STATUSES),
        )
    }


def focus_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    row = conn.execute(
        """
        SELECT
            (SELECT count(*) FROM people) AS people_count,
            (SELECT count(*) FROM org_edges WHERE COALESCE(active,1)=1) AS edge_count,
            (SELECT count(*) FROM org_edges WHERE COALESCE(active,1)=0) AS inactive_edge_count,
            (SELECT count(*) FROM manager_checks) AS manager_check_count,
            (SELECT count(*) FROM manager_checks WHERE status='complete') AS complete_managers,
            (SELECT count(*) FROM manager_checks WHERE status='deficit') AS deficit_managers,
            (SELECT count(*) FROM manager_checks WHERE status='leaf') AS leaf_managers,
            (SELECT count(*) FROM manager_checks WHERE status='transient_error') AS transient_error_managers,
            (SELECT count(*) FROM manager_checks WHERE status='fetch_404') AS fetch_404_managers,
            (SELECT count(*) FROM unresolved_aliases) AS unresolved_alias_count,
            (SELECT count(*) FROM unresolved_aliases WHERE status='profile_404') AS unresolved_profile_404_count,
            (SELECT count(*) FROM unresolved_aliases WHERE status!='profile_404') AS residual_unresolved_alias_count,
            (SELECT count(*) FROM org_edges e LEFT JOIN people p ON p.alias=e.child_alias WHERE COALESCE(e.active,1)=1 AND p.alias IS NULL) AS missing_people_from_edges,
            (
                SELECT count(*)
                FROM org_edges e
                LEFT JOIN people p ON p.alias=e.child_alias
                LEFT JOIN unresolved_aliases u ON u.alias=e.child_alias
                WHERE COALESCE(e.active,1)=1
                  AND p.alias IS NULL
                  AND COALESCE(u.status, '') != 'profile_404'
            ) AS residual_missing_people_from_edges
        """
    ).fetchone()
    return dict(row) if row else {
        "people_count": 0,
        "edge_count": 0,
        "inactive_edge_count": 0,
        "manager_check_count": 0,
        "complete_managers": 0,
        "deficit_managers": 0,
        "leaf_managers": 0,
        "transient_error_managers": 0,
        "fetch_404_managers": 0,
        "unresolved_alias_count": 0,
        "unresolved_profile_404_count": 0,
        "residual_unresolved_alias_count": 0,
        "missing_people_from_edges": 0,
        "residual_missing_people_from_edges": 0,
    }


def render_reports(conn: sqlite3.Connection, paths: OutputPaths, config: FocusConfig) -> None:
    ensure_dir(paths.report_dir)
    counts = focus_counts(conn)
    deficits = conn.execute(
        """
        SELECT
            p.alias,
            p.full_name,
            p.title,
            m.expected_directs,
            m.discovered_directs,
            m.status,
            m.note,
            (COALESCE(m.expected_directs,0) - COALESCE(m.discovered_directs,0)) AS deficit
        FROM manager_checks m
        LEFT JOIN people p ON p.alias = m.alias
        WHERE m.status='deficit'
        ORDER BY deficit DESC, m.expected_directs DESC, p.alias
        """
    ).fetchall()
    unresolved = conn.execute(
        """
        SELECT alias, status, note, source_parent_alias, first_seen_at, last_seen_at
        FROM unresolved_aliases
        ORDER BY
            CASE status WHEN 'profile_404' THEN 1 ELSE 0 END,
            alias
        """
    ).fetchall()
    missing_children = conn.execute(
        """
        SELECT
            e.parent_alias,
            pp.full_name AS parent_full_name,
            e.child_alias,
            u.status AS unresolved_status,
            u.note AS unresolved_note
        FROM org_edges e
        LEFT JOIN people p ON p.alias = e.child_alias
        LEFT JOIN people pp ON pp.alias = e.parent_alias
        LEFT JOIN unresolved_aliases u ON u.alias = e.child_alias
        WHERE COALESCE(e.active,1)=1
          AND p.alias IS NULL
        ORDER BY e.parent_alias, e.child_alias
        """
    ).fetchall()

    write = {
        "generated_at": utc_now(),
        "root_alias": latest_run(conn)["root_alias"] if latest_run(conn) else config.default_root_alias,
        **counts,
        "deficit_count": len(deficits),
    }
    base.save_json(paths.status_json, write)

    with paths.deficit_csv.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["alias", "full_name", "title", "expected_directs", "discovered_directs", "deficit", "status", "note"])
        for row in deficits:
            writer.writerow([
                row["alias"],
                row["full_name"],
                row["title"],
                row["expected_directs"],
                row["discovered_directs"],
                (row["expected_directs"] or 0) - (row["discovered_directs"] or 0),
                row["status"],
                row["note"],
            ])

    with paths.unresolved_csv.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["alias", "status", "source_parent_alias", "first_seen_at", "last_seen_at", "note"])
        for row in unresolved:
            writer.writerow([
                row["alias"],
                row["status"],
                row["source_parent_alias"],
                row["first_seen_at"],
                row["last_seen_at"],
                row["note"],
            ])

    with paths.missing_child_csv.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["parent_alias", "parent_full_name", "child_alias", "unresolved_status", "unresolved_note"])
        for row in missing_children:
            writer.writerow([
                row["parent_alias"],
                row["parent_full_name"],
                row["child_alias"],
                row["unresolved_status"],
                row["unresolved_note"],
            ])

    people_rows = conn.execute(
        """
        SELECT alias, full_name, title, organization_name, email, manager_alias,
               direct_report_count, employee_direct_count, contingent_direct_count,
               location_text, assistant_alias, assistant_name, worker_type
        FROM people
        ORDER BY COALESCE(full_name, alias), alias
        """
    ).fetchall()
    with paths.people_csv.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([key for key in people_rows[0].keys()] if people_rows else [
            "alias", "full_name", "title", "organization_name", "email", "manager_alias",
            "direct_report_count", "employee_direct_count", "contingent_direct_count",
            "location_text", "assistant_alias", "assistant_name", "worker_type",
        ])
        for row in people_rows:
            writer.writerow([row[key] for key in row.keys()])

    lines = [
        f"# {config.status_title}",
        "",
        f"- Generated: {utc_now()}",
        f"- Root alias: {write['root_alias']}",
        f"- People: {counts['people_count']}",
        f"- Edges: {counts['edge_count']}",
        f"- Inactive edges: {counts['inactive_edge_count']}",
        f"- Manager checks: {counts['manager_check_count']}",
        f"- Complete managers: {counts['complete_managers']}",
        f"- Leaf managers: {counts['leaf_managers']}",
        f"- Deficit managers: {counts['deficit_managers']}",
        f"- Transient-error managers: {counts['transient_error_managers']}",
        f"- 404 managers: {counts['fetch_404_managers']}",
        f"- Unresolved aliases: {counts['unresolved_alias_count']}",
        f"- Unresolved profile-404 aliases: {counts['unresolved_profile_404_count']}",
        f"- Residual unresolved aliases: {counts['residual_unresolved_alias_count']}",
        f"- Missing child profiles from edges: {counts['missing_people_from_edges']}",
        f"- Residual missing child profiles: {counts['residual_missing_people_from_edges']}",
        "",
        "## Largest Deficits",
        "",
    ]
    if deficits:
        for row in deficits[:20]:
            deficit = (row["expected_directs"] or 0) - (row["discovered_directs"] or 0)
            lines.append(
                f"- {row['full_name'] or row['alias']} ({row['alias']}): expected {row['expected_directs']}, discovered {row['discovered_directs']}, deficit {deficit}"
            )
    else:
        lines.append("- None")

    lines.extend(["", "## Unresolved Aliases", ""])
    if unresolved:
        for row in unresolved[:20]:
            lines.append(
                f"- {row['alias']}: status={row['status']}, parent={row['source_parent_alias'] or 'unknown'}, note={row['note']}"
            )
    else:
        lines.append("- None")

    lines.extend(["", "## Missing Child Profiles", ""])
    if missing_children:
        for row in missing_children[:20]:
            lines.append(
                f"- parent={row['parent_alias']} child={row['child_alias']} unresolved_status={row['unresolved_status'] or 'none'}"
            )
    else:
        lines.append("- None")

    paths.status_md.write_text("\n".join(lines) + "\n", encoding="utf-8")


def clear_output(paths: OutputPaths) -> None:
    for path in [
        paths.db_path,
        paths.status_md,
        paths.status_json,
        paths.deficit_csv,
        paths.unresolved_csv,
        paths.missing_child_csv,
        paths.people_csv,
    ]:
        if path.exists():
            path.unlink()
    if paths.log_path.exists():
        paths.log_path.unlink()


def build_resume_queue(conn: sqlite3.Connection, root_alias: str) -> Tuple[Deque[str], set[str], Dict[str, Optional[str]]]:
    seen = known_aliases(conn)
    queue: Deque[str] = deque()
    enqueued: set[str] = set()
    parent_map: Dict[str, Optional[str]] = {root_alias: None}
    permanently_unresolved = {
        row["alias"]
        for row in conn.execute(
            "SELECT alias FROM unresolved_aliases WHERE status='profile_404'"
        )
    }

    def add(alias: Optional[str], parent_alias: Optional[str] = None) -> None:
        if not alias:
            return
        if parent_alias is not None:
            parent_map.setdefault(alias, parent_alias)
        if alias in enqueued:
            return
        enqueued.add(alias)
        queue.append(alias)

    for row in conn.execute(
        """
        SELECT child_alias, MIN(parent_alias) AS parent_alias
        FROM org_edges
        WHERE COALESCE(active,1)=1
          AND child_alias NOT IN (SELECT alias FROM people)
        GROUP BY child_alias
        ORDER BY child_alias
        """
    ):
        if row["child_alias"] in permanently_unresolved:
            continue
        add(row["child_alias"], row["parent_alias"])

    for row in conn.execute(
        """
        SELECT alias FROM manager_checks
        WHERE status IN ('deficit','transient_error')
        ORDER BY alias
        """
    ):
        add(row["alias"])

    for row in conn.execute(
        """
        SELECT alias FROM people
        WHERE alias NOT IN (SELECT alias FROM manager_checks)
        ORDER BY alias
        """
    ):
        add(row["alias"])

    if not queue:
        add(root_alias)

    return queue, seen, parent_map


def build_resume_refresh_queue(
    conn: sqlite3.Connection,
    root_alias: str,
    run_floor: int,
) -> Tuple[Deque[str], set[str], set[str], Dict[str, Optional[str]]]:
    """Resume a failed refresh pass, only skipping entities refreshed since the anchor run."""
    seen = refreshed_aliases_since(conn, run_floor)
    checked = checked_aliases_since(conn, run_floor)
    queue: Deque[str] = deque()
    enqueued: set[str] = set()
    parent_map: Dict[str, Optional[str]] = {root_alias: None}
    permanently_unresolved = {
        row["alias"]
        for row in conn.execute(
            "SELECT alias FROM unresolved_aliases WHERE status='profile_404'"
        )
    }

    def add(alias: Optional[str], parent_alias: Optional[str] = None) -> None:
        if not alias or alias in permanently_unresolved:
            return
        if parent_alias is not None:
            parent_map.setdefault(alias, parent_alias)
        if alias in enqueued:
            return
        enqueued.add(alias)
        queue.append(alias)

    if root_alias not in seen or root_alias not in checked:
        add(root_alias)

    for row in conn.execute(
        """
        SELECT child_alias, MIN(parent_alias) AS parent_alias
        FROM org_edges
        WHERE COALESCE(active,1)=1
          AND child_alias NOT IN (SELECT alias FROM people)
        GROUP BY child_alias
        ORDER BY child_alias
        """
    ):
        add(row["child_alias"], row["parent_alias"])

    for row in conn.execute(
        """
        SELECT alias FROM manager_checks
        WHERE status IN ('deficit','transient_error','unchecked')
        ORDER BY alias
        """
    ):
        add(row["alias"])

    placeholders = ",".join("?" for _ in SKIPPABLE_MANAGER_STATUSES)
    for row in conn.execute(
        f"""
        SELECT p.alias
        FROM people p
        LEFT JOIN manager_checks m
          ON m.alias = p.alias
         AND COALESCE(m.crawl_run_id, 0) >= ?
         AND m.status IN ({placeholders})
        WHERE COALESCE(p.last_run_id, 0) < ?
           OR m.alias IS NULL
        ORDER BY p.alias
        """,
        (run_floor, *SKIPPABLE_MANAGER_STATUSES, run_floor),
    ):
        add(row["alias"])

    if not queue:
        add(root_alias)

    return queue, seen, checked, parent_map


def crawl(
    config: FocusConfig,
    root_alias: Optional[str] = None,
    fresh: bool = False,
    resume: bool = False,
    refresh_existing: bool = False,
    resume_refresh: bool = False,
    resume_refresh_from_run: Optional[int] = None,
    output_root: Optional[Path] = None,
) -> int:
    effective_root = root_alias or config.default_root_alias
    paths = output_paths(config, output_root)
    if fresh:
        clear_output(paths)
    conn = connect_db(paths)
    base.configure_active_headers(config.extra_headers_path)
    opener = base.build_opener(config.storage_state_path)

    refresh_anchor_run_id: Optional[int] = None
    if resume_refresh and not fresh and not refresh_existing:
        refresh_anchor_run_id = resume_refresh_from_run or latest_incomplete_refresh_anchor(conn)
        if refresh_anchor_run_id is None:
            raise RuntimeError("no incomplete refresh run found; pass --resume-refresh-from-run")

    if refresh_existing and not fresh:
        mode = "refresh-existing"
    elif resume_refresh and not fresh:
        mode = f"refresh-resume from run={refresh_anchor_run_id}"
    elif resume and not fresh:
        mode = "resume"
    else:
        mode = "fresh"
    run_id = start_run(conn, effective_root, f"{config.crawl_label} ({mode})")
    log(
        f"starting {config.slug}-focused crawl run={run_id} root={effective_root} mode={mode} output_root={paths.output_root}",
        paths,
    )

    checked = checked_aliases(conn) if resume and not fresh and not refresh_existing and not resume_refresh else set()
    if resume_refresh and not fresh and not refresh_existing:
        assert refresh_anchor_run_id is not None
        queue, seen, checked, parent_map = build_resume_refresh_queue(conn, effective_root, refresh_anchor_run_id)
        log(
            f"resuming failed refresh from run={refresh_anchor_run_id} queue={len(queue)} refreshed_people={len(seen)} checked={len(checked)}",
            paths,
        )
    elif resume and not fresh and not refresh_existing:
        queue, seen, parent_map = build_resume_queue(conn, effective_root)
    else:
        seen = set() if refresh_existing else known_aliases(conn)
        queue = deque([effective_root])
        parent_map = {effective_root: None}
    enqueued = set(queue)
    processed = 0
    new_people = 0
    retry_counts: Dict[Tuple[str, str], int] = {}

    try:
        while queue:
            alias = queue.popleft()
            processed += 1
            if alias not in seen:
                log(f"fetching profile alias={alias}", paths)
                try:
                    bundle = fetch_profile_bundle(opener, alias)
                except base.FetchError as exc:
                    key = ("profile", alias)
                    retry_counts[key] = retry_counts.get(key, 0) + 1
                    if is_auth_expired(exc):
                        raise_auth_expired("profile", alias, exc, paths)
                    if exc.status == 404:
                        upsert_unresolved_alias(conn, alias, run_id, parent_map.get(alias), "profile_404", "profile endpoint returned 404")
                        log(f"profile 404 alias={alias}", paths)
                        continue
                    if base.is_transient_network_error(exc) and retry_counts[key] <= 3:
                        log(f"transient profile fetch failure alias={alias} retry={retry_counts[key]} url={exc.url}", paths)
                        queue.append(alias)
                        continue
                    log(f"fetch failure alias={alias} status={exc.status} url={exc.url}", paths)
                    raise
                person = normalize_profile(alias, bundle)
                upsert_person(conn, run_id, person)
                clear_unresolved_alias(conn, alias)
                seen.add(alias)
                new_people += 1
            else:
                row = conn.execute(
                    "SELECT alias, full_name, title, organization_name, email, manager_alias, direct_report_count FROM people WHERE alias = ?",
                    (alias,),
                ).fetchone()
                person = dict(row) if row else {"alias": alias, "direct_report_count": None}

            if alias in checked:
                continue

            try:
                log(f"fetching direct reports alias={alias}", paths)
                reports = fetch_direct_reports(opener, alias)
                discovered = 0
                for report in reports:
                    child_alias = base.normalize_alias(base.first_value(report, "userId", "alias", "uid", "username", "email"))
                    if not child_alias:
                        continue
                    discovered += 1
                    upsert_edge(conn, alias, child_alias, run_id, json.dumps(report, ensure_ascii=True))
                    if child_alias not in enqueued:
                        parent_map.setdefault(child_alias, alias)
                        queue.append(child_alias)
                        enqueued.add(child_alias)
                mark_inactive_child_edges(conn, alias, run_id)
                expected = person.get("direct_report_count")
                status = "complete"
                note = ""
                if expected is None:
                    status = "unchecked"
                elif discovered < int(expected):
                    status = "deficit"
                    note = f"expected {expected}, discovered {discovered}"
                elif discovered == 0:
                    status = "leaf"
                upsert_manager_check(conn, alias, run_id, expected, discovered, status, note)
                checked.add(alias)
            except base.FetchError as exc:
                if is_auth_expired(exc):
                    raise_auth_expired("direct-reports", alias, exc, paths)
                if exc.status == 404:
                    upsert_manager_check(conn, alias, run_id, person.get("direct_report_count"), 0, "fetch_404", "directReports endpoint returned 404")
                    checked.add(alias)
                    log(f"direct reports 404 alias={alias}", paths)
                    continue
                key = ("direct", alias)
                retry_counts[key] = retry_counts.get(key, 0) + 1
                if base.is_transient_network_error(exc) and retry_counts[key] <= 3:
                    upsert_manager_check(conn, alias, run_id, person.get("direct_report_count"), 0, "transient_error", f"retry {retry_counts[key]} for {exc.url}")
                    log(f"transient direct reports failure alias={alias} retry={retry_counts[key]} url={exc.url}", paths)
                    queue.append(alias)
                    continue
                log(f"fetch failure alias={alias} status={exc.status} url={exc.url}", paths)
                raise

            if processed % 25 == 0:
                conn.commit()
                render_reports(conn, paths, config)
                log(
                    f"checkpoint run={run_id} processed={processed} people={len(seen)} new_people={new_people} queue={len(queue)} checked={len(checked)}",
                    paths,
                )

        conn.commit()
        render_reports(conn, paths, config)
        finish_run(conn, run_id, "completed")
        log(f"completed {config.slug}-focused crawl run={run_id} people={len(seen)} new_people={new_people}", paths)
        return 0
    except AuthExpiredError as exc:
        conn.commit()
        render_reports(conn, paths, config)
        finish_run(conn, run_id, "auth_expired")
        log(f"paused {config.slug}-focused crawl run={run_id} for auth refresh: {exc}", paths)
        return AUTH_EXPIRED_EXIT_CODE
    except Exception:
        log("focused crawl failed with exception:\n" + traceback.format_exc(), paths)
        conn.commit()
        render_reports(conn, paths, config)
        finish_run(conn, run_id, "failed")
        raise
    finally:
        conn.close()


def build_parser(config: FocusConfig) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"{config.crawl_label}.")
    parser.add_argument("--root", default=config.default_root_alias)
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--refresh-existing",
        action="store_true",
        help="Refetch known people and manager checks without deleting the existing database.",
    )
    parser.add_argument(
        "--resume-refresh",
        action="store_true",
        help="Continue a failed refresh-existing pass without treating older manager checks as fresh.",
    )
    parser.add_argument(
        "--resume-refresh-from-run",
        type=int,
        help="Anchor run id for --resume-refresh. Defaults to the latest incomplete refresh run.",
    )
    parser.add_argument("--output-root", type=Path, default=config.output_root)
    return parser


def run_cli(config: FocusConfig, argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser(config)
    args = parser.parse_args(argv)
    return crawl(
        config=config,
        root_alias=args.root,
        fresh=args.fresh,
        resume=args.resume,
        refresh_existing=args.refresh_existing,
        resume_refresh=args.resume_refresh,
        resume_refresh_from_run=args.resume_refresh_from_run,
        output_root=args.output_root,
    )
