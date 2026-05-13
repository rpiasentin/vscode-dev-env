#!/usr/bin/env python3
"""Local preflight checks before any Cisco org crawler rerun."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import focused_crawl
import search_databases as search


REPO_ROOT = Path(__file__).resolve().parents[2]
SESSION_ROOT = REPO_ROOT / "output" / "research" / "cisco-org-overlay"
SESSION_ARTIFACTS = (
    "storage-state.json",
    "directory-extra-headers.json",
    "session-manifest.json",
)
PROCESS_PATTERNS = (
    "jeetu_focus.py",
    "jeetu_supervisor.py",
    "oliver_focus.py",
    "oliver_supervisor.py",
    "hardened_session.mjs",
)
FOCUSED_V2_COLUMNS = {
    "org_edges": {"first_seen_at", "last_seen_at", "last_run_id", "active"},
    "org_edge_observations": {"id", "parent_alias", "child_alias", "crawl_run_id", "observed_at", "source_json"},
}


@dataclass
class Check:
    name: str
    status: str
    detail: str
    data: Optional[Dict[str, Any]] = None

    def as_dict(self) -> Dict[str, Any]:
        row = {"name": self.name, "status": self.status, "detail": self.detail}
        if self.data:
            row["data"] = self.data
        return row


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", action="append", choices=tuple(search.KNOWN_DATABASES) + ("all",), default=None)
    parser.add_argument("--min-free-gb", type=float, default=5.0)
    parser.add_argument("--max-run-age-days", type=int, default=30)
    parser.add_argument("--require-focused-v2", action="store_true")
    parser.add_argument(
        "--apply-focused-migrations",
        action="store_true",
        help="Apply additive focused schema migrations before checking focused DBs.",
    )
    parser.add_argument("--check-ui", action="store_true", help="Check the local search UI health endpoint.")
    parser.add_argument("--ui-url", default="http://127.0.0.1:8766/api/databases")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    parser.add_argument("--output-json", type=Path, help="Write the full report to this local path.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    checks: List[Check] = []
    checks.extend(check_workspace(args.min_free_gb))
    checks.extend(check_session_artifacts())
    checks.append(check_processes())

    targets = search.resolve_targets(args.db or ["all"])
    for target in targets:
        checks.extend(check_database(target, args))

    if args.check_ui:
        checks.append(check_ui(args.ui_url))

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_root": str(REPO_ROOT),
        "checks": [check.as_dict() for check in checks],
    }
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_report(checks))

    return 1 if any(check.status == "fail" for check in checks) else 0


def check_workspace(min_free_gb: float) -> List[Check]:
    checks = [
        Check("repo_root", "pass" if REPO_ROOT.exists() else "fail", str(REPO_ROOT)),
    ]
    usage = shutil.disk_usage(REPO_ROOT)
    free_gb = usage.free / (1024**3)
    checks.append(
        Check(
            "disk_free",
            "pass" if free_gb >= min_free_gb else "fail",
            f"{free_gb:.1f} GiB free; minimum {min_free_gb:.1f} GiB",
            {"free_gb": round(free_gb, 2), "minimum_gb": min_free_gb},
        )
    )
    return checks


def check_session_artifacts() -> List[Check]:
    checks: List[Check] = []
    for name in SESSION_ARTIFACTS:
        path = SESSION_ROOT / name
        checks.append(Check(f"session:{name}", "pass" if path.exists() else "fail", str(path)))
    profile = SESSION_ROOT / "browser-profile"
    checks.append(Check("session:browser-profile", "pass" if profile.exists() else "warn", str(profile)))
    return checks


def check_processes() -> Check:
    try:
        proc = subprocess.run(["ps", "axo", "pid=,command="], check=True, text=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        return Check("crawler_processes", "fail", exc.stderr.strip() or str(exc))
    matches = []
    current_pid = str(os.getpid())
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid, _, command = stripped.partition(" ")
        if pid == current_pid:
            continue
        if any(pattern in command for pattern in PROCESS_PATTERNS):
            matches.append({"pid": pid, "command": redact_command(command)})
    if matches:
        return Check("crawler_processes", "fail", f"{len(matches)} crawler/session process(es) already running", {"matches": matches})
    return Check("crawler_processes", "pass", "no focused crawler, supervisor, or session helper processes found")


def check_database(target: search.DatabaseTarget, args: argparse.Namespace) -> List[Check]:
    checks: List[Check] = []
    label = target.label
    path = target.path
    if not path.exists():
        return [Check(f"db:{label}:exists", "fail", str(path))]

    checks.append(Check(f"db:{label}:exists", "pass", str(path)))
    try:
        if args.apply_focused_migrations:
            with sqlite3.connect(str(path)) as conn:
                conn.row_factory = sqlite3.Row
                info = search.inspect_database(target)
                if info.schema_kind == "focused":
                    focused_crawl.ensure_focused_schema(conn)
        info = search.inspect_database(target)
        with search.connect_read_only(path) as conn:
            checks.append(check_quick(conn, label))
            checks.append(check_tables(conn, label, info.schema_kind))
            checks.append(check_latest_run(conn, label, info.schema_kind, args.max_run_age_days))
            if info.schema_kind == "focused":
                checks.append(check_focused_v2(conn, label, args.require_focused_v2))
    except Exception as exc:
        checks.append(Check(f"db:{label}:read", "fail", str(exc)))
    return checks


def check_quick(conn: sqlite3.Connection, label: str) -> Check:
    row = conn.execute("PRAGMA quick_check").fetchone()
    result = row[0] if row else "no result"
    return Check(f"db:{label}:quick_check", "pass" if result == "ok" else "fail", result)


def check_tables(conn: sqlite3.Connection, label: str, schema_kind: str) -> Check:
    table_names = default_tables(schema_kind)
    counts = {}
    missing = []
    for table in table_names:
        if not table_exists(conn, table):
            missing.append(table)
            continue
        counts[table] = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    status = "fail" if missing else "pass"
    detail = "missing tables: " + ", ".join(missing) if missing else "table counts captured"
    return Check(f"db:{label}:tables", status, detail, counts)


def check_latest_run(conn: sqlite3.Connection, label: str, schema_kind: str, max_age_days: int) -> Check:
    if not table_exists(conn, "crawl_runs"):
        return Check(f"db:{label}:latest_run", "fail", "crawl_runs table is missing")
    row = conn.execute("SELECT * FROM crawl_runs ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return Check(f"db:{label}:latest_run", "warn", "no crawl run has been recorded")
    finished_at = row["finished_at"] if "finished_at" in row.keys() else None
    started_at = row["started_at"] if "started_at" in row.keys() else None
    timestamp = parse_timestamp(finished_at or started_at)
    age_days = (time.time() - timestamp.timestamp()) / 86400 if timestamp else None
    status = "pass"
    detail = f"latest run id={row['id']} status={row['status']}"
    if row["status"] != "completed":
        status = "warn"
        detail += "; latest run is not completed"
    if age_days is not None and age_days > max_age_days:
        status = "warn"
        detail += f"; age {age_days:.1f} days exceeds {max_age_days}"
    data = {"schema_kind": schema_kind, "run_status": row["status"], "age_days": round(age_days, 1) if age_days is not None else None}
    return Check(f"db:{label}:latest_run", status, detail, data)


def check_focused_v2(conn: sqlite3.Connection, label: str, required: bool) -> Check:
    missing: Dict[str, List[str]] = {}
    for table, columns in FOCUSED_V2_COLUMNS.items():
        if not table_exists(conn, table):
            missing[table] = sorted(columns)
            continue
        actual = table_columns(conn, table)
        absent = sorted(columns - actual)
        if absent:
            missing[table] = absent
    if not missing:
        return Check(f"db:{label}:focused_v2", "pass", "focused graph metadata is present")
    status = "fail" if required else "warn"
    return Check(
        f"db:{label}:focused_v2",
        status,
        "focused graph metadata is missing; run with --apply-focused-migrations before a refresh",
        missing,
    )


def check_ui(url: str) -> Check:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            status = response.status
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return Check("ui_health", "warn", f"{url} did not return database health: {exc}")
    db_count = len(payload.get("databases") or [])
    return Check("ui_health", "pass" if status == 200 else "warn", f"{url} returned HTTP {status} with {db_count} database rows")


def default_tables(schema_kind: str) -> Sequence[str]:
    if schema_kind == "generic":
        return ("crawl_runs", "people", "person_snapshots", "person_paths")
    if schema_kind == "focused":
        return ("crawl_runs", "people", "person_snapshots", "org_edges", "manager_checks", "unresolved_aliases")
    return ("crawl_runs",)


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)).fetchone()
    return row is not None


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")}


def parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def redact_command(command: str) -> str:
    parts = command.split()
    redacted = []
    skip_next = False
    for part in parts:
        if skip_next:
            redacted.append("<redacted>")
            skip_next = False
            continue
        redacted.append(part)
        if part in {"--cookie", "--token", "--authorization", "--headers"}:
            skip_next = True
    return " ".join(redacted)


def render_report(checks: Iterable[Check]) -> str:
    lines = ["Cisco org crawler preflight", ""]
    for check in checks:
        marker = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}.get(check.status, check.status.upper())
        lines.append(f"[{marker}] {check.name}: {check.detail}")
    failures = sum(1 for check in checks if check.status == "fail")
    warnings = sum(1 for check in checks if check.status == "warn")
    lines.extend(["", f"Summary: {failures} failure(s), {warnings} warning(s)."])
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
