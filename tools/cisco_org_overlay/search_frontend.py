#!/usr/bin/env python3
"""Local-only web UI for searching the Cisco org SQLite databases."""

from __future__ import annotations

import argparse
import errno
import json
import re
import sqlite3
import subprocess
import time
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlparse

import search_databases as search


STATIC_ROOT = Path(__file__).with_name("search_ui")
MAX_BODY_BYTES = 128 * 1024
MAX_LIMIT = 250
MAX_COLLECTED_SEARCH_RESULTS = 250_000
MAX_CHART_SELECTIONS = 24
MAX_CHART_DIRECT_EXPANSIONS = 12
MAX_PEERS_PER_MANAGER = 600
MAX_DIRECT_REPORTS_PER_EXPANSION = 600
MAX_ANCESTOR_DEPTH = 64
MAX_UI_DETAIL_STRING_CHARS = 6000
MAX_UI_SNIPPET_CHARS = 600
MATCH_MODES = ("all", "any", "whole", "phrase")
FOCUSED_PROGRESS_DBS = ("jeetu", "oliver")
FOCUSED_ROOTS = {
    "jeetu": {
        "alias": "jeetup",
        "label": "Jeetu Patel",
    },
    "oliver": {
        "alias": "otuszik",
        "label": "Oliver Tuszik",
    },
}
PROCESS_LABELS = {
    "crawler": ("jeetu_focus.py", "oliver_focus.py"),
    "supervisor": ("jeetu_supervisor.py", "oliver_supervisor.py"),
    "session_helper": ("hardened_session.mjs",),
}
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/static/styles.css": ("styles.css", "text/css; charset=utf-8"),
}
CHECKPOINT_RE = re.compile(
    r"checkpoint run=(?P<run_id>\d+) processed=(?P<processed>\d+) people=(?P<people>\d+) "
    r"new_people=(?P<new_people>\d+) queue=(?P<queue>\d+) checked=(?P<checked>\d+)"
)
COMPLETED_RE = re.compile(
    r"completed (?P<label>[a-z]+)-focused crawl run=(?P<run_id>\d+) people=(?P<people>\d+) "
    r"new_people=(?P<new_people>\d+)"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Interface to bind. Default: %(default)s.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Local port to serve on. Default: %(default)s.",
    )
    return parser.parse_args()


def ensure_static_files() -> None:
    missing = [STATIC_ROOT / name for name, _ in STATIC_FILES.values() if not (STATIC_ROOT / name).exists()]
    if missing:
        missing_text = ", ".join(str(path) for path in missing)
        raise SystemExit(f"missing UI assets: {missing_text}")


def database_payload() -> Dict[str, Any]:
    targets = search.resolve_targets(["all"])
    listing_rows = search.database_listing_rows(targets)
    payload_rows: List[Dict[str, Any]] = []
    for row in listing_rows:
        item = dict(row)
        item["supported_scopes"] = []
        item["default_scopes"] = []
        item["table_counts"] = {}
        if row["schema_kind"] in {"generic", "focused"}:
            target = search.DatabaseTarget(label=row["label"], path=Path(row["path"]))
            info = search.inspect_database(target)
            item["supported_scopes"] = list(search.supported_scopes(info.schema_kind))
            item["default_scopes"] = list(search.default_scopes(info.schema_kind))
            item["table_counts"] = search.preflight_rows([info])[0]["table_counts"]
        payload_rows.append(item)
    return {
        "databases": payload_rows,
        "global_scopes": list(search.GLOBAL_SCOPES),
        "match_modes": list(MATCH_MODES),
        "chart_roots": FOCUSED_ROOTS,
        "generated_at": time.time(),
    }


def crawler_progress_payload() -> Dict[str, Any]:
    process_rows = crawler_process_rows()
    crawlers = []
    for db_label in FOCUSED_PROGRESS_DBS:
        db_path = search.KNOWN_DATABASES[db_label]
        output_root = db_path.parent
        crawler_pattern = f"{db_label}_focus.py"
        supervisor_pattern = f"{db_label}_supervisor.py"
        crawler_processes = [row for row in process_rows if crawler_pattern in row["command"]]
        supervisor_processes = [row for row in process_rows if supervisor_pattern in row["command"]]
        helper_processes = [
            row
            for row in process_rows
            if "hardened_session.mjs" in row["command"] and FOCUSED_ROOTS[db_label]["alias"] in row["command"]
        ]
        db_progress = focused_progress_from_db(db_path)
        latest_run = db_progress.get("latest_run") or {}
        latest_run_id = latest_run.get("id")
        crawlers.append(
            {
                "db_label": db_label,
                "root_alias": FOCUSED_ROOTS[db_label]["alias"],
                "root_label": FOCUSED_ROOTS[db_label]["label"],
                "db_path": str(db_path),
                "running": bool(crawler_processes),
                "crawler_processes": crawler_processes,
                "supervisor_processes": supervisor_processes,
                "session_helper_processes": helper_processes,
                "database": db_progress,
                "log": focused_progress_from_log(output_root / "logs" / "crawler.log", latest_run_id),
            }
        )
    return {
        "generated_at": time.time(),
        "crawlers": crawlers,
    }


def crawler_process_rows() -> List[Dict[str, Any]]:
    try:
        proc = subprocess.run(
            ["ps", "axo", "pid=,etime=,pcpu=,pmem=,command="],
            check=True,
            text=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    rows: List[Dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 4)
        if len(parts) < 5:
            continue
        pid, elapsed, pcpu, pmem, command = parts
        if not any(pattern in command for patterns in PROCESS_LABELS.values() for pattern in patterns):
            continue
        if "rg " in command and "jeetu_focus.py" in command:
            continue
        rows.append(
            {
                "pid": pid,
                "elapsed": elapsed,
                "pcpu": pcpu,
                "pmem": pmem,
                "command_label": command_label(command),
                "command": redact_process_command(command),
            }
        )
    return rows


def command_label(command: str) -> str:
    if "jeetu_focus.py" in command:
        return "Jeetu crawler"
    if "oliver_focus.py" in command:
        return "Oliver crawler"
    if "jeetu_supervisor.py" in command:
        return "Jeetu supervisor"
    if "oliver_supervisor.py" in command:
        return "Oliver supervisor"
    if "hardened_session.mjs" in command:
        return "Session helper"
    return "Process"


def redact_process_command(command: str) -> str:
    safe_parts = []
    redact_next = False
    for part in command.split():
        if redact_next:
            safe_parts.append("<redacted>")
            redact_next = False
            continue
        safe_parts.append(part)
        if part in {"--cookie", "--token", "--authorization", "--headers"}:
            redact_next = True
    return " ".join(safe_parts)


def focused_progress_from_db(db_path: Path) -> Dict[str, Any]:
    if not db_path.exists():
        return {"ok": False, "error": "database missing"}
    try:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        latest = conn.execute(
            "SELECT id, started_at, finished_at, status, root_alias, notes FROM crawl_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        run_id = latest["id"] if latest else None
        counts = focused_progress_counts(conn, run_id)
        conn.close()
        return {
            "ok": True,
            "latest_run": dict(latest) if latest else None,
            "counts": counts,
        }
    except sqlite3.Error as exc:
        return {"ok": False, "error": str(exc)}


def focused_progress_counts(conn: sqlite3.Connection, run_id: Optional[int]) -> Dict[str, int]:
    has_active = has_column(conn, "org_edges", "active")
    has_observations = table_exists(conn, "org_edge_observations")
    active_expr = "COALESCE(active,1)=1" if has_active else "1=1"
    inactive_expr = "COALESCE(active,1)=0" if has_active else "0=1"
    counts: Dict[str, int] = {}
    counts["people"] = scalar_count(conn, "SELECT count(*) FROM people")
    counts["active_edges"] = scalar_count(conn, f"SELECT count(*) FROM org_edges WHERE {active_expr}")
    counts["inactive_edges"] = scalar_count(conn, f"SELECT count(*) FROM org_edges WHERE {inactive_expr}")
    counts["manager_checks"] = scalar_count(conn, "SELECT count(*) FROM manager_checks")
    counts["unresolved_aliases"] = scalar_count(conn, "SELECT count(*) FROM unresolved_aliases")
    counts["observations"] = (
        scalar_count(conn, "SELECT count(*) FROM org_edge_observations") if has_observations else 0
    )
    if run_id is not None:
        counts["snapshots_this_run"] = scalar_count(conn, "SELECT count(*) FROM person_snapshots WHERE crawl_run_id = ?", run_id)
        counts["checks_this_run"] = scalar_count(conn, "SELECT count(*) FROM manager_checks WHERE crawl_run_id = ?", run_id)
        counts["deficits_this_run"] = scalar_count(
            conn,
            "SELECT count(*) FROM manager_checks WHERE crawl_run_id = ? AND status = 'deficit'",
            run_id,
        )
        counts["transient_this_run"] = scalar_count(
            conn,
            "SELECT count(*) FROM manager_checks WHERE crawl_run_id = ? AND status = 'transient_error'",
            run_id,
        )
        counts["observations_this_run"] = (
            scalar_count(conn, "SELECT count(*) FROM org_edge_observations WHERE crawl_run_id = ?", run_id)
            if has_observations
            else 0
        )
    return counts


def scalar_count(conn: sqlite3.Connection, sql: str, *params: Any) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row else 0


def focused_progress_from_log(log_path: Path, expected_run_id: Optional[int]) -> Dict[str, Any]:
    if not log_path.exists():
        return {"ok": False, "error": "crawler log missing"}
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]
    last_checkpoint: Optional[Dict[str, Any]] = None
    last_completion: Optional[Dict[str, Any]] = None
    last_warning: Optional[str] = None
    for line in lines:
        checkpoint = CHECKPOINT_RE.search(line)
        if checkpoint:
            checkpoint_run_id = int(checkpoint.group("run_id"))
            if expected_run_id is not None and checkpoint_run_id != int(expected_run_id):
                continue
            last_checkpoint = {key: int(value) for key, value in checkpoint.groupdict().items()}
            last_checkpoint["line"] = redact_log_line(line)
            continue
        completion = COMPLETED_RE.search(line)
        if completion:
            completion_run_id = int(completion.group("run_id"))
            if expected_run_id is not None and completion_run_id != int(expected_run_id):
                continue
            last_completion = {
                "label": completion.group("label"),
                "run_id": completion_run_id,
                "people": int(completion.group("people")),
                "new_people": int(completion.group("new_people")),
                "line": redact_log_line(line),
            }
            continue
        if "failed" in line.lower() or "transient" in line.lower() or "exception" in line.lower():
            last_warning = redact_log_line(line)
    return {
        "ok": True,
        "last_checkpoint": last_checkpoint,
        "last_completion": last_completion,
        "last_warning": last_warning,
        "updated_at": log_path.stat().st_mtime,
    }


def redact_log_line(line: str) -> str:
    line = re.sub(r"alias=[A-Za-z0-9_.-]+", "alias=<redacted>", line)
    line = re.sub(r"profile alias=[A-Za-z0-9_.-]+", "profile alias=<redacted>", line)
    line = re.sub(r"direct reports alias=[A-Za-z0-9_.-]+", "direct reports alias=<redacted>", line)
    return line


def build_org_chart_payload(raw_selections: Any, raw_direct_report_expansions: Any = None) -> Dict[str, Any]:
    selected_by_db, warnings = parse_chart_selections(raw_selections)
    expanded_by_db, expansion_warnings = parse_chart_direct_report_expansions(raw_direct_report_expansions)
    warnings.extend(expansion_warnings)
    for db_label, aliases in expanded_by_db.items():
        selected_aliases = selected_by_db.setdefault(db_label, [])
        for alias in aliases:
            if alias not in selected_aliases:
                selected_aliases.append(alias)
    charts = []
    for db_label, aliases in selected_by_db.items():
        try:
            target = search.resolve_targets([db_label])[0]
            info = search.inspect_database(target)
            with search.connect_read_only(info.target.path) as conn:
                if info.schema_kind == "focused":
                    root_info = FOCUSED_ROOTS[db_label]
                    charts.append(
                        build_focused_org_chart(
                            conn=conn,
                            db_label=db_label,
                            db_path=str(info.target.path),
                            root_alias=root_info["alias"],
                            root_label=root_info["label"],
                            selected_aliases=aliases,
                            expanded_aliases=expanded_by_db.get(db_label, []),
                        )
                    )
                elif info.schema_kind == "generic":
                    charts.append(
                        build_generic_org_chart(
                            conn=conn,
                            db_label=db_label,
                            db_path=str(info.target.path),
                            selected_aliases=aliases,
                            expanded_aliases=expanded_by_db.get(db_label, []),
                        )
                    )
                else:
                    warnings.append(f"{db_label} is not a supported org chart database, so it was skipped.")
        except RuntimeError as exc:
            warnings.append(f"{db_label} could not be opened: {exc}")
    if not charts and not warnings:
        warnings.append("No chartable focused people were selected.")
    return {
        "charts": charts,
        "warnings": warnings,
        "generated_at": time.time(),
    }


def hit_to_ui_dict(hit: search.SearchHit) -> Dict[str, Any]:
    item = search.hit_to_dict(hit)
    item["snippet"] = truncate_string(item.get("snippet") or "", MAX_UI_SNIPPET_CHARS)
    item["details"] = trim_details_for_ui(item.get("details") or {})
    return item


def trim_details_for_ui(details: Dict[str, Any]) -> Dict[str, Any]:
    trimmed: Dict[str, Any] = {}
    truncated_fields: List[str] = []
    for key, value in details.items():
        if isinstance(value, str) and len(value) > MAX_UI_DETAIL_STRING_CHARS:
            trimmed[key] = truncate_string(value, MAX_UI_DETAIL_STRING_CHARS)
            truncated_fields.append(key)
        else:
            trimmed[key] = value
    if truncated_fields:
        trimmed["_truncated_fields"] = truncated_fields
    return trimmed


def truncate_string(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    remaining = len(value) - max_chars
    return value[:max_chars] + f"... [truncated {remaining} chars for browser safety]"


def parse_chart_selections(raw_selections: Any) -> tuple[Dict[str, List[str]], List[str]]:
    return parse_chart_alias_items(
        raw_selections,
        field_name="selections",
        max_items=MAX_CHART_SELECTIONS,
        required=True,
    )


def parse_chart_direct_report_expansions(raw_expansions: Any) -> tuple[Dict[str, List[str]], List[str]]:
    return parse_chart_alias_items(
        raw_expansions,
        field_name="direct_report_expansions",
        max_items=MAX_CHART_DIRECT_EXPANSIONS,
        required=False,
    )


def parse_chart_alias_items(
    raw_items: Any,
    *,
    field_name: str,
    max_items: int,
    required: bool,
) -> tuple[Dict[str, List[str]], List[str]]:
    if raw_items is None and not required:
        return {}, []
    if not isinstance(raw_items, list):
        raise ValueError(f"{field_name} must be a list")
    if len(raw_items) > max_items:
        raise ValueError(f"{field_name} accepts at most {max_items} people for one chart request")

    selected_by_db: Dict[str, List[str]] = {}
    warnings: List[str] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            warnings.append(f"Skipped a {field_name} item that was not an object.")
            continue
        db_label = normalize_text(item.get("db_label"))
        alias = normalize_alias(item.get("alias") or item.get("record_id"))
        if not db_label or not alias:
            warnings.append(f"Skipped a {field_name} item missing db_label or alias.")
            continue
        if db_label not in search.KNOWN_DATABASES:
            warnings.append(f"{db_label}:{alias} is not a known local org database.")
            continue
        key = (db_label, alias)
        if key in seen:
            continue
        seen.add(key)
        selected_by_db.setdefault(db_label, []).append(alias)
    return selected_by_db, warnings


def build_focused_org_chart(
    *,
    conn: Any,
    db_label: str,
    db_path: str,
    root_alias: str,
    root_label: str,
    selected_aliases: Sequence[str],
    expanded_aliases: Sequence[str],
) -> Dict[str, Any]:
    nodes: Dict[str, Dict[str, Any]] = {}
    edges: Dict[str, Dict[str, str]] = {}
    peer_groups: Dict[str, Dict[str, Any]] = {}
    direct_report_groups: Dict[str, Dict[str, Any]] = {}
    warnings: List[str] = []
    person_cache: Dict[str, Dict[str, Any]] = {}

    def person(alias: str) -> Dict[str, Any]:
        if alias not in person_cache:
            person_cache[alias] = fetch_person(conn, alias)
        return person_cache[alias]

    def add_node(alias: str, role: str, selected: bool = False) -> None:
        alias = normalize_alias(alias) or alias
        existing = nodes.get(alias)
        if existing is None:
            record = person(alias)
            existing = {
                **record,
                "roles": set(),
                "selected": False,
                "depth": None,
            }
            nodes[alias] = existing
        existing["roles"].add(role)
        if selected:
            existing["selected"] = True

    def add_edge(parent_alias: Optional[str], child_alias: Optional[str], kind: str = "reports_to") -> None:
        parent = normalize_alias(parent_alias)
        child = normalize_alias(child_alias)
        if not parent or not child or parent == child:
            return
        key = f"{parent}->{child}"
        edges[key] = {
            "parent_alias": parent,
            "child_alias": child,
            "kind": kind,
        }

    add_node(root_alias, "root")

    for selected_alias in selected_aliases:
        selected_alias = normalize_alias(selected_alias) or selected_alias
        selected_person = person(selected_alias)
        add_node(selected_alias, "selected", selected=True)
        if selected_person.get("missing"):
            warnings.append(f"{db_label}:{selected_alias} was not found in people; using edge context only.")

        parent_aliases = fetch_parent_aliases(conn, selected_alias)
        if len(parent_aliases) > 1:
            warnings.append(
                f"{db_label}:{selected_alias} has multiple parent edges; using {parent_aliases[0]} for the chart."
            )
        parent_alias = parent_aliases[0] if parent_aliases else None

        if parent_alias:
            add_node(parent_alias, "manager")
            add_edge(parent_alias, selected_alias, "reports_to")
            chain = fetch_ancestor_chain(conn, parent_alias, root_alias)
            if chain and chain[-1] != root_alias:
                warnings.append(
                    f"{db_label}:{selected_alias} manager chain stops at {chain[-1]}, not {root_alias}."
                )
            add_chain(nodes, edges, person, chain, root_alias)

            peers = fetch_children(conn, parent_alias, MAX_PEERS_PER_MANAGER + 1)
            truncated = len(peers) > MAX_PEERS_PER_MANAGER
            if truncated:
                peers = peers[:MAX_PEERS_PER_MANAGER]
                warnings.append(
                    f"{db_label}:{parent_alias} has more than {MAX_PEERS_PER_MANAGER} direct reports; chart was capped."
                )
            for peer_alias in peers:
                add_node(peer_alias, "peer", selected=peer_alias == selected_alias)
                add_edge(parent_alias, peer_alias, "peer_group")
            group = peer_groups.setdefault(
                parent_alias,
                {
                    "manager_alias": parent_alias,
                    "manager_name": person(parent_alias).get("full_name") or parent_alias,
                    "selected_aliases": [],
                    "peer_count": len(peers),
                    "truncated": truncated,
                },
            )
            if selected_alias not in group["selected_aliases"]:
                group["selected_aliases"].append(selected_alias)
            group["peer_count"] = max(group["peer_count"], len(peers))
            group["truncated"] = bool(group["truncated"] or truncated)
        elif selected_alias == root_alias:
            warnings.append(f"{db_label}:{selected_alias} is the configured root; no same-manager peer group exists.")
        else:
            warnings.append(f"{db_label}:{selected_alias} has no parent edge, so peers could not be inferred.")

    for expanded_alias in expanded_aliases:
        expanded_alias = normalize_alias(expanded_alias) or expanded_alias
        add_node(expanded_alias, "expanded", selected=expanded_alias in selected_aliases)
        reports = fetch_children(conn, expanded_alias, MAX_DIRECT_REPORTS_PER_EXPANSION + 1)
        truncated = len(reports) > MAX_DIRECT_REPORTS_PER_EXPANSION
        if truncated:
            reports = reports[:MAX_DIRECT_REPORTS_PER_EXPANSION]
            warnings.append(
                f"{db_label}:{expanded_alias} has more than {MAX_DIRECT_REPORTS_PER_EXPANSION} direct reports; expansion was capped."
            )
        for child_alias in reports:
            add_node(child_alias, "direct_report", selected=child_alias in selected_aliases)
            add_edge(expanded_alias, child_alias, "direct_report_expansion")
        expanded_person = person(expanded_alias)
        direct_report_groups[expanded_alias] = {
            "manager_alias": expanded_alias,
            "manager_name": expanded_person.get("full_name") or expanded_alias,
            "direct_report_count": len(reports),
            "captured_direct_report_count": expanded_person.get("direct_report_count"),
            "truncated": truncated,
        }

    assign_depths(nodes, edges, root_alias)
    serializable_nodes = []
    for node in nodes.values():
        roles = sorted(node.pop("roles"))
        node["roles"] = roles
        node["primary_role"] = primary_role(roles, bool(node.get("selected")))
        serializable_nodes.append(node)
    serializable_nodes.sort(
        key=lambda item: (
            item["depth"] if item["depth"] is not None else 999,
            item.get("primary_role") != "selected",
            (item.get("full_name") or item["alias"]).lower(),
            item["alias"],
        )
    )
    edge_rows = sorted(edges.values(), key=lambda edge: (edge["parent_alias"], edge["child_alias"], edge["kind"]))
    return {
        "db_label": db_label,
        "db_path": db_path,
        "root_alias": root_alias,
        "root_label": root_label,
        "selected_aliases": list(selected_aliases),
        "nodes": serializable_nodes,
        "edges": edge_rows,
        "peer_groups": sorted(peer_groups.values(), key=lambda group: group["manager_alias"]),
        "direct_report_groups": sorted(direct_report_groups.values(), key=lambda group: group["manager_alias"]),
        "warnings": warnings,
    }


def build_generic_org_chart(
    *,
    conn: Any,
    db_label: str,
    db_path: str,
    selected_aliases: Sequence[str],
    expanded_aliases: Sequence[str],
) -> Dict[str, Any]:
    nodes: Dict[str, Dict[str, Any]] = {}
    edges: Dict[str, Dict[str, str]] = {}
    peer_groups: Dict[str, Dict[str, Any]] = {}
    direct_report_groups: Dict[str, Dict[str, Any]] = {}
    warnings: List[str] = []
    person_cache: Dict[str, Dict[str, Any]] = {}
    selected_rows: List[Dict[str, Any]] = []
    root_alias = "generic"
    root_label = "Generic org path"

    def person_by_id(person_id: str) -> Dict[str, Any]:
        if person_id not in person_cache:
            person_cache[person_id] = fetch_generic_person(conn, person_id)
        return person_cache[person_id]

    def add_node(person_id: Optional[str], role: str, selected: bool = False) -> Optional[str]:
        if not person_id:
            return None
        record = person_by_id(person_id)
        alias = normalize_alias(record.get("alias")) or person_id
        existing = nodes.get(alias)
        if existing is None:
            existing = {
                **record,
                "roles": set(),
                "selected": False,
                "depth": None,
            }
            nodes[alias] = existing
        existing["roles"].add(role)
        if selected:
            existing["selected"] = True
        return alias

    def add_edge(parent_alias: Optional[str], child_alias: Optional[str], kind: str = "reports_to") -> None:
        parent = normalize_alias(parent_alias)
        child = normalize_alias(child_alias)
        if not parent or not child or parent == child:
            return
        edges[f"{parent}->{child}"] = {
            "parent_alias": parent,
            "child_alias": child,
            "kind": kind,
        }

    for selected_alias in selected_aliases:
        context = fetch_generic_path_context(conn, selected_alias)
        if context is None:
            warnings.append(f"{db_label}:{selected_alias} has no generic path row, so peers could not be inferred.")
            continue
        selected_rows.append(context)
        if len(selected_rows) == 1:
            root_alias = context["root_alias"] or context["root_person_id"]
            root_label = context["root_label"] or root_alias

        selected_node_alias = add_node(context["person_id"], "selected", selected=True)
        add_node(context["root_person_id"], "root")

        current = context
        seen_person_ids: set[str] = set()
        while current and current.get("person_id") not in seen_person_ids:
            seen_person_ids.add(current["person_id"])
            child_alias = add_node(current["person_id"], "selected" if current["person_id"] == context["person_id"] else "manager")
            parent_person_id = current.get("manager_person_id")
            if not parent_person_id:
                break
            parent_alias = add_node(
                parent_person_id,
                "root" if parent_person_id == context["root_person_id"] else "manager",
            )
            add_edge(parent_alias, child_alias, "ancestor_chain")
            if parent_person_id == context["root_person_id"]:
                break
            current = fetch_generic_path_by_person(
                conn,
                person_id=parent_person_id,
                crawl_run_id=context["crawl_run_id"],
                root_person_id=context["root_person_id"],
                path_scope=context["path_scope"],
            )

        manager_person_id = context.get("manager_person_id")
        if not manager_person_id:
            warnings.append(f"{db_label}:{selected_alias} is a root or has no manager in the generic path.")
            continue
        manager_alias = add_node(manager_person_id, "manager")
        peers = fetch_generic_peer_person_ids(
            conn,
            crawl_run_id=context["crawl_run_id"],
            root_person_id=context["root_person_id"],
            path_scope=context["path_scope"],
            manager_person_id=manager_person_id,
            limit=MAX_PEERS_PER_MANAGER + 1,
        )
        truncated = len(peers) > MAX_PEERS_PER_MANAGER
        if truncated:
            peers = peers[:MAX_PEERS_PER_MANAGER]
            warnings.append(
                f"{db_label}:{manager_alias} has more than {MAX_PEERS_PER_MANAGER} direct reports in the generic path; chart was capped."
            )
        for peer_person_id in peers:
            peer_alias = add_node(peer_person_id, "peer", selected=peer_person_id == context["person_id"])
            add_edge(manager_alias, peer_alias, "peer_group")
        group = peer_groups.setdefault(
            manager_alias or manager_person_id,
            {
                "manager_alias": manager_alias or manager_person_id,
                "manager_name": person_by_id(manager_person_id).get("full_name") or manager_alias or manager_person_id,
                "selected_aliases": [],
                "peer_count": len(peers),
                "truncated": truncated,
            },
        )
        if selected_node_alias and selected_node_alias not in group["selected_aliases"]:
            group["selected_aliases"].append(selected_node_alias)

    for expanded_alias in expanded_aliases:
        context = fetch_generic_path_context(conn, expanded_alias)
        if context is None:
            warnings.append(f"{db_label}:{expanded_alias} has no generic path row, so direct reports could not be inferred.")
            continue
        selected_rows.append(context)
        if root_alias == "generic":
            root_alias = context["root_alias"] or context["root_person_id"]
            root_label = context["root_label"] or root_alias
        manager_alias = add_node(context["person_id"], "expanded", selected=expanded_alias in selected_aliases)
        reports = fetch_generic_peer_person_ids(
            conn,
            crawl_run_id=context["crawl_run_id"],
            root_person_id=context["root_person_id"],
            path_scope=context["path_scope"],
            manager_person_id=context["person_id"],
            limit=MAX_DIRECT_REPORTS_PER_EXPANSION + 1,
        )
        truncated = len(reports) > MAX_DIRECT_REPORTS_PER_EXPANSION
        if truncated:
            reports = reports[:MAX_DIRECT_REPORTS_PER_EXPANSION]
            warnings.append(
                f"{db_label}:{manager_alias or expanded_alias} has more than {MAX_DIRECT_REPORTS_PER_EXPANSION} direct reports in the generic path; expansion was capped."
            )
        for child_person_id in reports:
            child_alias = add_node(child_person_id, "direct_report")
            add_edge(manager_alias, child_alias, "direct_report_expansion")
        expanded_person = person_by_id(context["person_id"])
        direct_report_groups[manager_alias or expanded_alias] = {
            "manager_alias": manager_alias or expanded_alias,
            "manager_name": expanded_person.get("full_name") or manager_alias or expanded_alias,
            "direct_report_count": len(reports),
            "captured_direct_report_count": expanded_person.get("direct_report_count"),
            "truncated": truncated,
        }

    if not selected_rows:
        return {
            "db_label": db_label,
            "db_path": db_path,
            "root_alias": root_alias,
            "root_label": root_label,
            "selected_aliases": list(selected_aliases),
            "nodes": [],
            "edges": [],
            "peer_groups": [],
            "direct_report_groups": [],
            "warnings": warnings,
        }

    assign_depths(nodes, edges, root_alias)
    serializable_nodes = []
    for node in nodes.values():
        roles = sorted(node.pop("roles"))
        node["roles"] = roles
        node["primary_role"] = primary_role(roles, bool(node.get("selected")))
        serializable_nodes.append(node)
    serializable_nodes.sort(
        key=lambda item: (
            item["depth"] if item["depth"] is not None else 999,
            item.get("primary_role") != "selected",
            (item.get("full_name") or item["alias"]).lower(),
            item["alias"],
        )
    )
    return {
        "db_label": db_label,
        "db_path": db_path,
        "root_alias": root_alias,
        "root_label": root_label,
        "selected_aliases": list(selected_aliases),
        "nodes": serializable_nodes,
        "edges": sorted(edges.values(), key=lambda edge: (edge["parent_alias"], edge["child_alias"], edge["kind"])),
        "peer_groups": sorted(peer_groups.values(), key=lambda group: group["manager_alias"]),
        "direct_report_groups": sorted(direct_report_groups.values(), key=lambda group: group["manager_alias"]),
        "warnings": warnings,
    }


def add_chain(
    nodes: Dict[str, Dict[str, Any]],
    edges: Dict[str, Dict[str, str]],
    person: Any,
    chain: Sequence[str],
    root_alias: str,
) -> None:
    for alias in chain:
        role = "root" if alias == root_alias else "manager"
        if alias not in nodes:
            nodes[alias] = {
                **person(alias),
                "roles": {role},
                "selected": False,
                "depth": None,
            }
        else:
            nodes[alias]["roles"].add(role)
    for child_alias, parent_alias in zip(chain, chain[1:]):
        key = f"{parent_alias}->{child_alias}"
        edges[key] = {
            "parent_alias": parent_alias,
            "child_alias": child_alias,
            "kind": "ancestor_chain",
        }


def fetch_person(conn: Any, alias: str) -> Dict[str, Any]:
    row = conn.execute(
        """
        SELECT
            alias,
            full_name,
            title,
            organization_name,
            email,
            direct_report_count,
            employee_direct_count,
            contingent_direct_count,
            location_text,
            worker_type,
            first_seen_at,
            last_seen_at
        FROM people
        WHERE alias = ?
        """,
        (alias,),
    ).fetchone()
    if row is None:
        return {
            "alias": alias,
            "full_name": alias,
            "missing": True,
        }
    return {key: row[key] for key in row.keys()}


def fetch_generic_person(conn: Any, person_id: str) -> Dict[str, Any]:
    row = conn.execute(
        """
        SELECT
            p.person_id,
            COALESCE(p.alias, p.person_id) AS alias,
            COALESCE(s.full_name, p.display_name, p.alias, p.person_id) AS full_name,
            s.title,
            s.organization_name,
            COALESCE(s.email, p.canonical_email) AS email,
            s.direct_report_count,
            s.employee_direct_count,
            s.contingent_direct_count,
            s.location_text,
            s.worker_type,
            p.first_seen_at,
            p.last_seen_at
        FROM people p
        LEFT JOIN person_snapshots s ON s.id = p.latest_snapshot_id
        WHERE p.person_id = ?
        """,
        (person_id,),
    ).fetchone()
    if row is None:
        return {
            "alias": person_id,
            "full_name": person_id,
            "missing": True,
        }
    return {key: row[key] for key in row.keys()}


def fetch_generic_path_context(conn: Any, alias: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        """
        SELECT
            pp.crawl_run_id,
            pp.root_person_id,
            COALESCE(root.alias, pp.root_person_id) AS root_alias,
            COALESCE(root_snapshot.full_name, root.display_name, root.alias, pp.root_person_id) AS root_label,
            pp.person_id,
            COALESCE(p.alias, pp.person_id) AS alias,
            pp.manager_person_id,
            pp.depth,
            pp.path_scope,
            pp.branch_label
        FROM people p
        JOIN person_paths pp ON pp.person_id = p.person_id
        LEFT JOIN people root ON root.person_id = pp.root_person_id
        LEFT JOIN person_snapshots root_snapshot ON root_snapshot.id = root.latest_snapshot_id
        WHERE lower(p.alias) = ? OR lower(p.canonical_email) = ?
        ORDER BY
            pp.crawl_run_id DESC,
            CASE COALESCE(root.alias, pp.root_person_id)
                WHEN 'jeetup' THEN 0
                WHEN 'otuszik' THEN 1
                WHEN 'crobbins' THEN 2
                ELSE 3
            END,
            pp.depth DESC
        LIMIT 1
        """,
        (alias, f"{alias}@cisco.com"),
    ).fetchone()
    return dict(row) if row else None


def fetch_generic_path_by_person(
    conn: Any,
    *,
    person_id: str,
    crawl_run_id: int,
    root_person_id: str,
    path_scope: str,
) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        """
        SELECT
            pp.crawl_run_id,
            pp.root_person_id,
            pp.person_id,
            pp.manager_person_id,
            pp.depth,
            pp.path_scope,
            pp.branch_label
        FROM person_paths pp
        WHERE pp.person_id = ?
          AND pp.crawl_run_id = ?
          AND pp.root_person_id = ?
          AND pp.path_scope = ?
        LIMIT 1
        """,
        (person_id, crawl_run_id, root_person_id, path_scope),
    ).fetchone()
    return dict(row) if row else None


def fetch_generic_peer_person_ids(
    conn: Any,
    *,
    crawl_run_id: int,
    root_person_id: str,
    path_scope: str,
    manager_person_id: str,
    limit: int,
) -> List[str]:
    rows = conn.execute(
        """
        SELECT pp.person_id
        FROM person_paths pp
        JOIN people p ON p.person_id = pp.person_id
        LEFT JOIN person_snapshots s ON s.id = p.latest_snapshot_id
        WHERE pp.crawl_run_id = ?
          AND pp.root_person_id = ?
          AND pp.path_scope = ?
          AND pp.manager_person_id = ?
        ORDER BY lower(COALESCE(s.full_name, p.display_name, p.alias, pp.person_id)), pp.person_id
        LIMIT ?
        """,
        (crawl_run_id, root_person_id, path_scope, manager_person_id, limit),
    ).fetchall()
    return [row["person_id"] for row in rows]


def fetch_parent_aliases(conn: Any, child_alias: str) -> List[str]:
    active_clause = "AND COALESCE(active, 1) = 1" if has_column(conn, "org_edges", "active") else ""
    rows = conn.execute(
        f"""
        SELECT parent_alias
        FROM org_edges
        WHERE child_alias = ?
          {active_clause}
        ORDER BY discovered_at DESC, crawl_run_id DESC, parent_alias
        """,
        (child_alias,),
    ).fetchall()
    aliases: List[str] = []
    seen: set[str] = set()
    for row in rows:
        alias = normalize_alias(row["parent_alias"])
        if alias and alias not in seen:
            aliases.append(alias)
            seen.add(alias)
    return aliases


def fetch_children(conn: Any, parent_alias: str, limit: int) -> List[str]:
    active_clause = "AND COALESCE(e.active, 1) = 1" if has_column(conn, "org_edges", "active") else ""
    rows = conn.execute(
        f"""
        SELECT child_alias
        FROM org_edges e
        LEFT JOIN people p ON p.alias = e.child_alias
        WHERE e.parent_alias = ?
          {active_clause}
        ORDER BY lower(COALESCE(p.full_name, e.child_alias)), e.child_alias
        LIMIT ?
        """,
        (parent_alias, limit),
    ).fetchall()
    return [row["child_alias"] for row in rows]


def has_column(conn: Any, table_name: str, column_name: str) -> bool:
    return any(row["name"] == column_name for row in conn.execute(f"PRAGMA table_info({table_name})"))


def table_exists(conn: Any, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def fetch_ancestor_chain(conn: Any, start_alias: str, root_alias: str) -> List[str]:
    chain: List[str] = []
    current = normalize_alias(start_alias)
    seen: set[str] = set()
    while current and current not in seen and len(chain) < MAX_ANCESTOR_DEPTH:
        chain.append(current)
        if current == root_alias:
            break
        seen.add(current)
        parents = fetch_parent_aliases(conn, current)
        current = parents[0] if parents else None
    return chain


def assign_depths(
    nodes: Dict[str, Dict[str, Any]],
    edges: Dict[str, Dict[str, str]],
    root_alias: str,
) -> None:
    children_by_parent: Dict[str, List[str]] = {}
    for edge in edges.values():
        children_by_parent.setdefault(edge["parent_alias"], []).append(edge["child_alias"])
    queue: deque[tuple[str, int]] = deque([(root_alias, 0)])
    seen: set[str] = set()
    while queue:
        alias, depth = queue.popleft()
        if alias in seen:
            continue
        seen.add(alias)
        if alias in nodes:
            nodes[alias]["depth"] = depth
        for child_alias in children_by_parent.get(alias, []):
            queue.append((child_alias, depth + 1))
    fallback_depth = max((node["depth"] for node in nodes.values() if node["depth"] is not None), default=0) + 1
    for node in nodes.values():
        if node["depth"] is None:
            node["depth"] = fallback_depth


def primary_role(roles: Sequence[str], selected: bool) -> str:
    if selected:
        return "selected"
    for role in ("root", "expanded", "manager", "direct_report", "peer"):
        if role in roles:
            return role
    return roles[0] if roles else "person"


def normalize_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def normalize_alias(value: Any) -> Optional[str]:
    text = normalize_text(value)
    if not text:
        return None
    return text.lower()


class SearchUIHandler(BaseHTTPRequestHandler):
    server_version = "CiscoOrgSearchUI/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in STATIC_FILES:
            filename, content_type = STATIC_FILES[parsed.path]
            self.serve_static_file(STATIC_ROOT / filename, content_type)
            return
        if parsed.path == "/api/databases":
            try:
                payload = database_payload()
            except Exception as exc:  # pragma: no cover - defensive handler path
                self.send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": str(exc)},
                )
                return
            self.send_json(HTTPStatus.OK, payload)
            return
        if parsed.path == "/api/crawler-progress":
            try:
                payload = crawler_progress_payload()
            except Exception as exc:  # pragma: no cover - defensive handler path
                self.send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": str(exc)},
                )
                return
            self.send_json(HTTPStatus.OK, payload)
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/search":
            self.handle_search()
            return
        if parsed.path == "/api/org-chart":
            self.handle_org_chart()
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def handle_search(self) -> None:
        try:
            payload = self.read_json_body()
            query = expect_string(payload.get("query"), "query")
            databases = payload.get("databases") or ["all"]
            scopes = payload.get("scopes") or []
            mode = payload.get("mode") or "all"
            deep = bool(payload.get("deep"))
            page_size = parse_limit(payload.get("limit", search.DEFAULT_LIMIT))
            page = parse_page(payload.get("page", 1))
            validate_string_list(databases, "databases")
            validate_string_list(scopes, "scopes")
            if mode not in MATCH_MODES:
                raise ValueError(f"unsupported match mode: {mode}")
            targets = search.resolve_targets(databases)
            infos = [search.inspect_database(target) for target in targets]
            search_args = search.build_search_args(
                query_text=query,
                mode=mode,
                deep=deep,
                limit=MAX_COLLECTED_SEARCH_RESULTS,
                scopes=scopes,
            )
            started = time.perf_counter()
            hits = search.search_databases(infos, search_args)
            total_count = len(hits)
            total_pages = ((total_count - 1) // page_size + 1) if total_count else 0
            if total_pages and page > total_pages:
                page = total_pages
            offset = (page - 1) * page_size if total_count else 0
            page_hits = hits[offset : offset + page_size]
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            self.send_json(
                HTTPStatus.OK,
                {
                    "query": search_args.query_text,
                    "mode": search_args.mode,
                    "deep": search_args.deep,
                    "limit": page_size,
                    "page_size": page_size,
                    "page": page,
                    "offset": offset,
                    "start": offset + 1 if page_hits else 0,
                    "end": offset + len(page_hits),
                    "total_pages": total_pages,
                    "has_previous": page > 1,
                    "has_next": bool(total_pages and page < total_pages),
                    "scopes": list(search_args.scopes),
                    "databases": [info.target.label for info in infos],
                    "results": [hit_to_ui_dict(hit) for hit in page_hits],
                    "count": total_count,
                    "result_count": len(page_hits),
                    "max_results_reached": total_count >= MAX_COLLECTED_SEARCH_RESULTS,
                    "duration_ms": duration_ms,
                },
            )
        except ValueError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except RuntimeError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:  # pragma: no cover - defensive handler path
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def handle_org_chart(self) -> None:
        try:
            payload = self.read_json_body()
            started = time.perf_counter()
            chart_payload = build_org_chart_payload(
                payload.get("selections"),
                payload.get("direct_report_expansions"),
            )
            chart_payload["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
            self.send_json(HTTPStatus.OK, chart_payload)
        except ValueError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:  # pragma: no cover - defensive handler path
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def read_json_body(self) -> Dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("missing Content-Length header")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("invalid Content-Length header") from exc
        if length <= 0:
            raise ValueError("request body is empty")
        if length > MAX_BODY_BYTES:
            raise ValueError(f"request body exceeds {MAX_BODY_BYTES} bytes")
        body = self.rfile.read(length)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ValueError("request body is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def serve_static_file(self, path: Path, content_type: str) -> None:
        if not path.exists() or not path.is_file():
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "static asset not found"})
            return
        content = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, status: HTTPStatus, payload: Dict[str, Any]) -> None:
        content = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)


def expect_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def validate_string_list(values: Sequence[Any], field_name: str) -> None:
    if not isinstance(values, list):
        raise ValueError(f"{field_name} must be a list")
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must contain only non-empty strings")


def parse_limit(value: Any) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    if limit <= 0:
        raise ValueError("limit must be greater than 0")
    if limit > MAX_LIMIT:
        raise ValueError(f"limit must be at most {MAX_LIMIT}")
    return limit


def parse_page(value: Any) -> int:
    try:
        page = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("page must be an integer") from exc
    if page <= 0:
        raise ValueError("page must be greater than 0")
    return page


def bind_server(host: str, port: int, attempts: int = 20) -> tuple[ThreadingHTTPServer, int]:
    last_error: OSError | None = None
    for candidate in range(port, port + attempts):
        try:
            server = ThreadingHTTPServer((host, candidate), SearchUIHandler)
            server.daemon_threads = True
            return server, candidate
        except OSError as exc:
            last_error = exc
            if exc.errno != errno.EADDRINUSE:
                raise
    if last_error is not None:
        raise last_error
    raise OSError("unable to bind search UI server")


def main() -> int:
    args = parse_args()
    ensure_static_files()
    server, bound_port = bind_server(args.host, args.port)
    url = f"http://{args.host}:{bound_port}"
    print(f"Serving Cisco org search UI at {url}")
    if bound_port != args.port:
        print(f"Requested port {args.port} was busy, so the UI moved to {bound_port}.")
    print("Open that URL manually in a local browser. No browser window will be opened automatically.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down search UI.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
