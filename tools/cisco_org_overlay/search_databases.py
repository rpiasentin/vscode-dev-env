#!/usr/bin/env python3
"""Read-only ad hoc search across the local Cisco org SQLite databases."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote


REPO_ROOT = Path(__file__).resolve().parents[2]
RESEARCH_ROOT = REPO_ROOT / "output" / "research"
DEFAULT_LIMIT = 25
GLOBAL_SCOPES = (
    "people",
    "snapshots",
    "documents",
    "manager_checks",
    "unresolved_aliases",
    "org_edges",
)
KNOWN_DATABASES = {
    "generic": RESEARCH_ROOT / "cisco-org-overlay" / "cisco_org_overlay.sqlite3",
    "jeetu": RESEARCH_ROOT / "cisco-org-jeetu" / "jeetu_focus.sqlite3",
    "oliver": RESEARCH_ROOT / "cisco-org-oliver" / "oliver_focus.sqlite3",
}
SCOPE_PRIORITIES = {
    "people": 60,
    "snapshots": 35,
    "documents": 25,
    "manager_checks": 30,
    "unresolved_aliases": 30,
    "org_edges": 20,
}


@dataclass(frozen=True)
class DatabaseTarget:
    label: str
    path: Path


@dataclass(frozen=True)
class DatabaseInfo:
    target: DatabaseTarget
    schema_kind: str
    tables: frozenset[str]


@dataclass(frozen=True)
class SearchArgs:
    query_text: str
    query_lower: str
    terms: Tuple[str, ...]
    mode: str
    deep: bool
    limit: int
    scopes: Tuple[str, ...]


@dataclass
class SearchHit:
    db_label: str
    db_path: str
    schema_kind: str
    scope: str
    record_id: str
    score: int
    summary: str
    snippet: str
    details: Dict[str, Any]


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "query",
        nargs="*",
        help="Free-text query. Quote multi-word phrases in the shell if needed.",
    )
    parser.add_argument(
        "--db",
        action="append",
        default=[],
        metavar="DB",
        help=(
            "Database alias or absolute path. Known aliases: generic, jeetu, oliver, all. "
            "Repeat to target multiple databases."
        ),
    )
    parser.add_argument(
        "--table",
        dest="tables",
        action="append",
        choices=GLOBAL_SCOPES,
        help="Restrict search to one or more logical scopes.",
    )
    parser.add_argument(
        "--match",
        choices=("all", "any", "whole", "phrase"),
        default="all",
        help="How query terms are combined. Default: %(default)s.",
    )
    parser.add_argument(
        "--deep",
        action="store_true",
        help="Include raw JSON and metadata blobs in the search text.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help="Maximum number of results to print. Default: %(default)s.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of human-readable text.",
    )
    parser.add_argument(
        "--list-dbs",
        action="store_true",
        help="List the known local database targets and exit.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Run preflight checks on the selected databases before searching.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        targets = resolve_targets(args.db)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.list_dbs:
        return render_database_listing(targets, as_json=args.json)

    if args.limit <= 0:
        print("error: --limit must be greater than 0", file=sys.stderr)
        return 2

    query_text = " ".join(args.query).strip()

    try:
        infos = [inspect_database(target) for target in targets]
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.check:
        render_preflight(infos, as_json=args.json)

    if not query_text:
        if args.check:
            return 0
        print("error: a query is required unless --list-dbs or --check is used", file=sys.stderr)
        return 2

    try:
        search_args = build_search_args(
            query_text=query_text,
            mode=args.match,
            deep=args.deep,
            limit=args.limit,
            scopes=tuple(args.tables or ()),
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        hits = search_databases(infos, search_args)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps([hit_to_dict(hit) for hit in hits], indent=2))
    else:
        render_human_hits(hits, infos, search_args)
    return 0


def resolve_targets(raw_values: Sequence[str]) -> List[DatabaseTarget]:
    requested = list(raw_values or ["all"])
    resolved: List[DatabaseTarget] = []
    seen: set[Path] = set()
    for value in requested:
        if value == "all":
            for label, path in KNOWN_DATABASES.items():
                if path not in seen:
                    resolved.append(DatabaseTarget(label=label, path=path))
                    seen.add(path)
            continue
        known_path = KNOWN_DATABASES.get(value)
        if known_path is not None:
            if known_path not in seen:
                resolved.append(DatabaseTarget(label=value, path=known_path))
                seen.add(known_path)
            continue
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError(
                f"database path '{value}' is not a known alias and is not an absolute path"
            )
        if path not in seen:
            resolved.append(DatabaseTarget(label=path.stem, path=path))
            seen.add(path)
    return resolved


def render_database_listing(targets: Sequence[DatabaseTarget], as_json: bool) -> int:
    rows = database_listing_rows(targets)
    if as_json:
        print(json.dumps(rows, indent=2))
        return 0
    for row in rows:
        status = "ok" if row["exists"] else "missing"
        print(f"{row['label']}: {status} schema={row['schema_kind']} path={row['path']}")
    return 0


def inspect_database(target: DatabaseTarget) -> DatabaseInfo:
    preflight_path(target.path)
    with connect_read_only(target.path) as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            ).fetchall()
        }
    if {"people", "person_snapshots", "crawl_runs", "documents", "person_labels"}.issubset(tables):
        schema_kind = "generic"
    elif {"people", "person_snapshots", "crawl_runs", "org_edges", "manager_checks"}.issubset(
        tables
    ):
        schema_kind = "focused"
    else:
        raise RuntimeError(
            f"{target.path} does not look like a supported Cisco org database; found tables: "
            + ", ".join(sorted(tables))
        )
    return DatabaseInfo(target=target, schema_kind=schema_kind, tables=frozenset(tables))


def preflight_path(path: Path) -> None:
    if not path.exists():
        raise RuntimeError(f"database does not exist: {path}")
    if not path.is_file():
        raise RuntimeError(f"database path is not a regular file: {path}")
    if path.stat().st_size == 0:
        raise RuntimeError(f"database file is empty: {path}")
    if not path.stat().st_mode:
        raise RuntimeError(f"database metadata could not be read: {path}")


def connect_read_only(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.create_function("whole_term_match", 2, whole_term_match)
    conn.execute("PRAGMA query_only = ON")
    return conn


def normalize_terms(query_text: str) -> List[str]:
    return re.findall(r"[A-Za-z0-9_.@+-]+", query_text.lower())


def build_search_args(
    query_text: str,
    mode: str = "all",
    deep: bool = False,
    limit: int = DEFAULT_LIMIT,
    scopes: Sequence[str] = (),
) -> SearchArgs:
    normalized_query = query_text.strip()
    if not normalized_query:
        raise ValueError("query is required")
    if mode not in {"all", "any", "whole", "phrase"}:
        raise ValueError(f"unsupported match mode: {mode}")
    if limit <= 0:
        raise ValueError("limit must be greater than 0")
    terms = tuple(term for term in normalize_terms(normalized_query) if term)
    if mode != "phrase" and not terms:
        raise ValueError("query does not contain any searchable terms")
    return SearchArgs(
        query_text=normalized_query,
        query_lower=normalized_query.lower(),
        terms=terms,
        mode=mode,
        deep=deep,
        limit=limit,
        scopes=tuple(scopes),
    )


def database_listing_rows(targets: Sequence[DatabaseTarget]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for target in targets:
        schema_kind = "unknown"
        error = None
        if target.path.exists() and target.path.is_file() and target.path.stat().st_size > 0:
            try:
                schema_kind = inspect_database(target).schema_kind
            except RuntimeError as exc:
                schema_kind = "unreadable"
                error = str(exc)
        rows.append(
            {
                "label": target.label,
                "path": str(target.path),
                "exists": target.path.exists(),
                "readable": target.path.exists() and target.path.is_file(),
                "schema_kind": schema_kind,
                "error": error,
            }
        )
    return rows


def preflight_rows(infos: Sequence[DatabaseInfo]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for info in infos:
        table_counts: Dict[str, int] = {}
        with connect_read_only(info.target.path) as conn:
            for table_name in sorted(relevant_tables(info)):
                table_counts[table_name] = conn.execute(
                    f"SELECT COUNT(*) AS count FROM {table_name}"
                ).fetchone()["count"]
        rows.append(
            {
                "label": info.target.label,
                "path": str(info.target.path),
                "schema_kind": info.schema_kind,
                "table_counts": table_counts,
            }
        )
    return rows


def render_preflight(infos: Sequence[DatabaseInfo], as_json: bool) -> None:
    rows = preflight_rows(infos)
    if as_json:
        print(json.dumps(rows, indent=2))
        return
    for row in rows:
        counts = " ".join(f"{name}={count}" for name, count in row["table_counts"].items())
        print(f"preflight {row['label']}: schema={row['schema_kind']} {counts} path={row['path']}")


def relevant_tables(info: DatabaseInfo) -> Iterable[str]:
    if info.schema_kind == "generic":
        return ("crawl_runs", "people", "person_snapshots", "documents", "person_labels")
    return ("crawl_runs", "people", "person_snapshots", "manager_checks", "unresolved_aliases", "org_edges")


def search_databases(infos: Sequence[DatabaseInfo], args: SearchArgs) -> List[SearchHit]:
    requested_scopes = set(args.scopes)
    available_scope_count = 0
    hits: List[SearchHit] = []
    for info in infos:
        scope_names = supported_scopes(info.schema_kind)
        if requested_scopes:
            scope_names = tuple(scope for scope in scope_names if scope in requested_scopes)
        else:
            scope_names = default_scopes(info.schema_kind)
        available_scope_count += len(scope_names)
        if not scope_names:
            continue
        with connect_read_only(info.target.path) as conn:
            for scope_name in scope_names:
                hits.extend(run_scope_search(conn, info, scope_name, args))
    if requested_scopes and available_scope_count == 0:
        raise RuntimeError(
            "requested scopes are not available for the selected database set: "
            + ", ".join(sorted(requested_scopes))
        )
    hits.sort(key=lambda hit: (-hit.score, hit.db_label, hit.scope, hit.record_id))
    return hits[: args.limit]


def supported_scopes(schema_kind: str) -> Tuple[str, ...]:
    if schema_kind == "generic":
        return ("people", "snapshots", "documents")
    return ("people", "snapshots", "manager_checks", "unresolved_aliases", "org_edges")


def default_scopes(schema_kind: str) -> Tuple[str, ...]:
    if schema_kind == "generic":
        return ("people", "documents")
    return ("people", "manager_checks", "unresolved_aliases")


def run_scope_search(
    conn: sqlite3.Connection,
    info: DatabaseInfo,
    scope_name: str,
    args: SearchArgs,
) -> List[SearchHit]:
    if info.schema_kind == "generic":
        if scope_name == "people":
            rows = query_generic_people(conn, args)
        elif scope_name == "snapshots":
            rows = query_generic_snapshots(conn, args)
        elif scope_name == "documents":
            rows = query_generic_documents(conn, args)
        else:
            rows = []
    else:
        if scope_name == "people":
            rows = query_focused_people(conn, args)
        elif scope_name == "snapshots":
            rows = query_focused_snapshots(conn, args)
        elif scope_name == "manager_checks":
            rows = query_focused_manager_checks(conn, args)
        elif scope_name == "unresolved_aliases":
            rows = query_focused_unresolved_aliases(conn, args)
        elif scope_name == "org_edges":
            rows = query_focused_org_edges(conn, args)
        else:
            rows = []

    hits: List[SearchHit] = []
    for row in rows:
        summary = build_summary(scope_name, row)
        summary_lower = summary.lower()
        search_text = row["search_text"] or ""
        score = score_row(scope_name, summary_lower, search_text, args)
        if score <= 0:
            continue
        hits.append(
            SearchHit(
                db_label=info.target.label,
                db_path=str(info.target.path),
                schema_kind=info.schema_kind,
                scope=scope_name,
                record_id=str(row["record_id"]),
                score=score,
                summary=summary,
                snippet=build_snippet(search_text, args),
                details=row_to_details(row),
            )
        )
    return hits


def query_generic_people(conn: sqlite3.Connection, args: SearchArgs) -> List[sqlite3.Row]:
    fields = [
        "p.alias",
        "p.person_id",
        "p.canonical_email",
        "p.display_name",
        "p.company_type",
        "p.source_origin",
        "s.full_name",
        "s.title",
        "s.organization_name",
        "s.department_name",
        "s.manager_alias",
        "s.manager_name",
        "s.location_text",
        "s.city",
        "s.state",
        "s.country",
        "s.email",
        "s.assistant_alias",
        "s.assistant_name",
        "s.label_text",
        "s.phones_json",
    ]
    if args.deep:
        fields.append("s.raw_json")
    sql = f"""
        WITH search_space AS (
            SELECT
                p.alias AS record_id,
                p.alias,
                p.person_id,
                p.display_name,
                s.full_name,
                s.title,
                s.organization_name,
                s.department_name,
                COALESCE(s.email, p.canonical_email) AS email,
                s.manager_alias,
                s.manager_name,
                s.location_text,
                s.city,
                s.state,
                s.country,
                s.assistant_alias,
                s.assistant_name,
                s.label_text,
                p.company_type,
                p.source_origin,
                s.captured_at,
                {concat_sql(fields)} AS search_text
            FROM people p
            LEFT JOIN person_snapshots s ON s.id = p.latest_snapshot_id
        )
        SELECT *
        FROM search_space
        WHERE {where_clause('search_text', args)}
        LIMIT ?
    """
    params = match_params(args) + [candidate_limit(args.limit)]
    return conn.execute(sql, params).fetchall()


def query_generic_snapshots(conn: sqlite3.Connection, args: SearchArgs) -> List[sqlite3.Row]:
    fields = [
        "p.alias",
        "ps.person_id",
        "p.canonical_email",
        "ps.full_name",
        "ps.title",
        "ps.organization_name",
        "ps.department_name",
        "ps.manager_alias",
        "ps.manager_name",
        "ps.location_text",
        "ps.city",
        "ps.state",
        "ps.country",
        "ps.email",
        "ps.assistant_alias",
        "ps.assistant_name",
        "ps.label_text",
        "ps.phones_json",
    ]
    if args.deep:
        fields.append("ps.raw_json")
    sql = f"""
        WITH search_space AS (
            SELECT
                CAST(ps.id AS TEXT) AS record_id,
                ps.id,
                COALESCE(p.alias, ps.person_id) AS alias,
                ps.person_id,
                ps.captured_at,
                ps.full_name,
                ps.title,
                ps.organization_name,
                ps.department_name,
                COALESCE(ps.email, p.canonical_email) AS email,
                ps.manager_alias,
                ps.manager_name,
                ps.location_text,
                ps.city,
                ps.state,
                ps.country,
                ps.assistant_alias,
                ps.assistant_name,
                ps.label_text,
                {concat_sql(fields)} AS search_text
            FROM person_snapshots ps
            LEFT JOIN people p ON p.person_id = ps.person_id
        )
        SELECT *
        FROM search_space
        WHERE {where_clause('search_text', args)}
        LIMIT ?
    """
    params = match_params(args) + [candidate_limit(args.limit)]
    return conn.execute(sql, params).fetchall()


def query_generic_documents(conn: sqlite3.Connection, args: SearchArgs) -> List[sqlite3.Row]:
    fields = [
        "d.title",
        "d.owner_name",
        "d.owner_alias",
        "d.query_text",
        "d.url",
        "d.external_ref",
        "d.mime_type",
        "d.file_extension",
    ]
    if args.deep:
        fields.append("d.raw_metadata")
    sql = f"""
        WITH search_space AS (
            SELECT
                CAST(d.id AS TEXT) AS record_id,
                d.id,
                d.title,
                d.owner_name,
                d.owner_alias,
                d.query_text,
                d.url,
                d.external_ref,
                d.mime_type,
                d.file_extension,
                d.discovered_at,
                {concat_sql(fields)} AS search_text
            FROM documents d
        )
        SELECT *
        FROM search_space
        WHERE {where_clause('search_text', args)}
        LIMIT ?
    """
    params = match_params(args) + [candidate_limit(args.limit)]
    return conn.execute(sql, params).fetchall()


def query_focused_people(conn: sqlite3.Connection, args: SearchArgs) -> List[sqlite3.Row]:
    fields = [
        "alias",
        "full_name",
        "title",
        "organization_name",
        "email",
        "manager_alias",
        "location_text",
        "assistant_alias",
        "assistant_name",
        "worker_type",
        "phones_json",
    ]
    if args.deep:
        fields.append("raw_json")
    sql = f"""
        WITH search_space AS (
            SELECT
                alias AS record_id,
                alias,
                full_name,
                title,
                organization_name,
                email,
                manager_alias,
                direct_report_count,
                employee_direct_count,
                contingent_direct_count,
                location_text,
                assistant_alias,
                assistant_name,
                worker_type,
                first_seen_at,
                last_seen_at,
                last_run_id,
                {concat_sql(fields)} AS search_text
            FROM people
        )
        SELECT *
        FROM search_space
        WHERE {where_clause('search_text', args)}
        LIMIT ?
    """
    params = match_params(args) + [candidate_limit(args.limit)]
    return conn.execute(sql, params).fetchall()


def query_focused_snapshots(conn: sqlite3.Connection, args: SearchArgs) -> List[sqlite3.Row]:
    fields = [
        "alias",
        "full_name",
        "title",
        "organization_name",
        "email",
        "manager_alias",
        "location_text",
        "assistant_alias",
        "assistant_name",
        "worker_type",
        "phones_json",
    ]
    if args.deep:
        fields.append("raw_json")
    sql = f"""
        WITH search_space AS (
            SELECT
                CAST(id AS TEXT) AS record_id,
                id,
                alias,
                captured_at,
                full_name,
                title,
                organization_name,
                email,
                manager_alias,
                direct_report_count,
                employee_direct_count,
                contingent_direct_count,
                location_text,
                assistant_alias,
                assistant_name,
                worker_type,
                {concat_sql(fields)} AS search_text
            FROM person_snapshots
        )
        SELECT *
        FROM search_space
        WHERE {where_clause('search_text', args)}
        LIMIT ?
    """
    params = match_params(args) + [candidate_limit(args.limit)]
    return conn.execute(sql, params).fetchall()


def query_focused_manager_checks(conn: sqlite3.Connection, args: SearchArgs) -> List[sqlite3.Row]:
    sql = f"""
        WITH search_space AS (
            SELECT
                alias AS record_id,
                alias,
                expected_directs,
                discovered_directs,
                status,
                note,
                last_checked_at,
                {concat_sql(['alias', 'status', 'note'])} AS search_text
            FROM manager_checks
        )
        SELECT *
        FROM search_space
        WHERE {where_clause('search_text', args)}
        LIMIT ?
    """
    params = match_params(args) + [candidate_limit(args.limit)]
    return conn.execute(sql, params).fetchall()


def query_focused_unresolved_aliases(conn: sqlite3.Connection, args: SearchArgs) -> List[sqlite3.Row]:
    sql = f"""
        WITH search_space AS (
            SELECT
                alias AS record_id,
                alias,
                first_seen_at,
                last_seen_at,
                source_parent_alias,
                source_run_id,
                status,
                note,
                {concat_sql(['alias', 'source_parent_alias', 'status', 'note'])} AS search_text
            FROM unresolved_aliases
        )
        SELECT *
        FROM search_space
        WHERE {where_clause('search_text', args)}
        LIMIT ?
    """
    params = match_params(args) + [candidate_limit(args.limit)]
    return conn.execute(sql, params).fetchall()


def query_focused_org_edges(conn: sqlite3.Connection, args: SearchArgs) -> List[sqlite3.Row]:
    fields = ["parent_alias", "child_alias"]
    if args.deep:
        fields.append("source_json")
    sql = f"""
        WITH search_space AS (
            SELECT
                parent_alias || '->' || child_alias AS record_id,
                parent_alias,
                child_alias,
                crawl_run_id,
                discovered_at,
                {concat_sql(fields)} AS search_text
            FROM org_edges
        )
        SELECT *
        FROM search_space
        WHERE {where_clause('search_text', args)}
        LIMIT ?
    """
    params = match_params(args) + [candidate_limit(args.limit)]
    return conn.execute(sql, params).fetchall()


def concat_sql(fields: Sequence[str]) -> str:
    return "trim(" + " || ' ' || ".join(f"coalesce({field}, '')" for field in fields) + ")"


def where_clause(field_name: str, args: SearchArgs) -> str:
    if args.mode == "phrase":
        return f"lower({field_name}) LIKE ? ESCAPE '\\'"
    if args.mode == "whole":
        return " AND ".join(f"whole_term_match({field_name}, ?)" for _ in args.terms)
    operator = " AND " if args.mode == "all" else " OR "
    return operator.join(f"lower({field_name}) LIKE ? ESCAPE '\\'" for _ in args.terms)


def match_params(args: SearchArgs) -> List[str]:
    if args.mode == "phrase":
        return [like_pattern(args.query_lower)]
    if args.mode == "whole":
        return list(args.terms)
    return [like_pattern(term) for term in args.terms]


def whole_term_match(search_text: Any, term: Any) -> int:
    if search_text is None or term is None:
        return 0
    return 1 if whole_term_present(str(search_text), str(term)) else 0


def whole_term_present(text: str, term: str) -> bool:
    if not term:
        return False
    pattern = rf"(?<![A-Za-z0-9]){re.escape(term.lower())}(?![A-Za-z0-9])"
    return re.search(pattern, text.lower()) is not None


def like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    return "%" + escaped + "%"


def candidate_limit(limit: int) -> int:
    return max(limit * 4, 50)


def build_summary(scope_name: str, row: sqlite3.Row) -> str:
    if scope_name == "people":
        return " | ".join(
            compact_parts(
                [
                    row["alias"],
                    row_value(row, "full_name") or row_value(row, "display_name"),
                    row["title"],
                    row["organization_name"],
                    row["email"],
                ]
            )
        )
    if scope_name == "snapshots":
        return " | ".join(
            compact_parts(
                [
                    row["alias"],
                    row["captured_at"],
                    row["full_name"],
                    row["title"],
                    row["organization_name"],
                ]
            )
        )
    if scope_name == "documents":
        return " | ".join(
            compact_parts(
                [
                    row["title"] or f"document#{row['record_id']}",
                    row["owner_alias"] or row["owner_name"],
                    row["mime_type"] or row["file_extension"],
                    row["discovered_at"],
                ]
            )
        )
    if scope_name == "manager_checks":
        return (
            f"{row['alias']} | status={row['status']} | "
            f"expected={row['expected_directs']} discovered={row['discovered_directs']}"
        )
    if scope_name == "unresolved_aliases":
        return (
            f"{row['alias']} | status={row['status']} | "
            f"parent={row['source_parent_alias'] or '-'}"
        )
    if scope_name == "org_edges":
        return (
            f"{row['parent_alias']} -> {row['child_alias']} | "
            f"discovered_at={row['discovered_at']}"
        )
    return str(row["record_id"])


def compact_parts(parts: Sequence[Optional[Any]]) -> List[str]:
    return [str(part).strip() for part in parts if part not in (None, "", "None")]


def score_row(scope_name: str, summary_lower: str, search_text: str, args: SearchArgs) -> int:
    search_text_lower = search_text.lower()
    score = SCOPE_PRIORITIES.get(scope_name, 0)
    if args.mode == "whole":
        if all(whole_term_present(summary_lower, term) for term in args.terms):
            score += 250
        if all(whole_term_present(search_text_lower, term) for term in args.terms):
            score += 125
    else:
        if args.query_lower in summary_lower:
            score += 250
        if args.query_lower in search_text_lower:
            score += 125
    terms = args.terms if args.mode != "phrase" else tuple(normalize_terms(args.query_text))
    for term in terms:
        if args.mode == "whole":
            if whole_term_present(summary_lower, term):
                score += 35
            if whole_term_present(search_text_lower, term):
                score += 10
        else:
            if term in summary_lower:
                score += 35
            if term in search_text_lower:
                score += 10
    return score


def build_snippet(search_text: str, args: SearchArgs, width: int = 180) -> str:
    text = re.sub(r"\s+", " ", search_text).strip()
    if not text:
        return ""
    text_lower = text.lower()
    needles = [args.query_lower] if args.mode == "phrase" else list(args.terms)
    index = -1
    needle_len = 0
    for needle in needles:
        if not needle:
            continue
        if args.mode == "whole":
            match = re.search(rf"(?<![A-Za-z0-9]){re.escape(needle)}(?![A-Za-z0-9])", text_lower)
            idx = match.start() if match else -1
        else:
            idx = text_lower.find(needle)
        if idx >= 0:
            index = idx
            needle_len = len(needle)
            break
    if index < 0:
        return ellipsize(text, width)
    start = max(0, index - width // 3)
    end = min(len(text), index + max(width // 2, needle_len + 20))
    snippet = text[start:end]
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet = snippet + "..."
    return snippet


def ellipsize(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[: max(width - 3, 0)] + "..."


def row_to_details(row: sqlite3.Row) -> Dict[str, Any]:
    details = {}
    for key in row.keys():
        if key == "search_text":
            continue
        details[key] = row[key]
    return details


def row_value(row: sqlite3.Row, key: str) -> Any:
    return row[key] if key in row.keys() else None


def hit_to_dict(hit: SearchHit) -> Dict[str, Any]:
    return {
        "db_label": hit.db_label,
        "db_path": hit.db_path,
        "schema_kind": hit.schema_kind,
        "scope": hit.scope,
        "record_id": hit.record_id,
        "score": hit.score,
        "summary": hit.summary,
        "snippet": hit.snippet,
        "details": hit.details,
    }


def render_human_hits(
    hits: Sequence[SearchHit],
    infos: Sequence[DatabaseInfo],
    args: SearchArgs,
) -> None:
    print(
        f"query={args.query_text!r} mode={args.mode} deep={str(args.deep).lower()} "
        f"dbs={','.join(info.target.label for info in infos)}"
    )
    if not hits:
        print("no matches")
        return
    print(f"matches={len(hits)} showing_up_to={args.limit}")
    current_db = None
    for hit in hits:
        if hit.db_label != current_db:
            current_db = hit.db_label
            print("")
            print(f"[{hit.db_label}] {hit.db_path}")
        print(f"- {hit.scope}: {hit.summary}")
        if hit.snippet:
            print(f"  snippet: {hit.snippet}")


if __name__ == "__main__":
    raise SystemExit(main())
