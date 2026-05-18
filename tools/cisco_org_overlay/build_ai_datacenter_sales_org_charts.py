#!/usr/bin/env python3
"""Build local org-chart artifacts for AI datacenter infrastructure sales teams."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from search_frontend import build_focused_org_chart


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "output" / "research" / "cisco-org-ai-datacenter-sales"

DB_CONFIGS = {
    "jeetu": {
        "db_path": REPO_ROOT / "output" / "research" / "cisco-org-jeetu" / "jeetu_focus.sqlite3",
        "root_alias": "jeetup",
        "root_label": "Jeetu Patel",
    },
    "oliver": {
        "db_path": REPO_ROOT / "output" / "research" / "cisco-org-oliver" / "oliver_focus.sqlite3",
        "root_alias": "otuszik",
        "root_label": "Oliver Tuszik",
    },
}

SALES_MARKERS = {
    "sales": r"\bsales\b",
    "account": r"\baccount\b",
    "gtm": r"\bgtm\b|go[- ]?to[- ]?market",
    "partner": r"\bpartner\b",
    "channel": r"\bchannel\b",
    "field": r"\bfield\b",
    "commercial": r"\bcommercial\b",
    "enterprise": r"\benterprise\b",
    "pre-sales": r"pre[- ]?sales",
    "solution-engineering": r"solutions? engineer|solutions? architect|customer solutions?",
}

AI_MARKERS = {
    "ai": r"\bai\b",
    "artificial-intelligence": r"artificial intelligence",
    "genai": r"gen[- ]?ai|generative ai",
    "gpu": r"\bgpu\b",
    "nvidia": r"\bnvidia\b",
    "accelerated": r"\baccelerated\b",
}

DATACENTER_MARKERS = {
    "data-center": r"data ?center|datacenter|data centre",
    "infrastructure": r"\binfrastructure\b",
    "compute": r"\bcompute\b",
    "networking": r"\bnetworking\b",
    "nexus": r"\bnexus\b",
    "ucs": r"\bucs\b",
    "hyperscaler": r"\bhyperscaler\b",
}


@dataclass(frozen=True)
class Candidate:
    db_label: str
    alias: str
    full_name: Optional[str]
    title: Optional[str]
    organization_name: Optional[str]
    manager_alias: str
    sales_matches: List[str]
    ai_matches: List[str]
    datacenter_matches: List[str]

    @property
    def score(self) -> int:
        return len(self.sales_matches) + len(self.ai_matches) + len(self.datacenter_matches)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def compile_markers(markers: Dict[str, str]) -> Dict[str, re.Pattern[str]]:
    return {label: re.compile(pattern, re.IGNORECASE) for label, pattern in markers.items()}


def marker_hits(patterns: Dict[str, re.Pattern[str]], text: str) -> List[str]:
    return [label for label, pattern in patterns.items() if pattern.search(text)]


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def fetch_manager_alias(conn: sqlite3.Connection, alias: str) -> Optional[str]:
    row = conn.execute(
        """
        SELECT parent_alias
        FROM org_edges
        WHERE child_alias = ?
          AND COALESCE(active, 1) = 1
        ORDER BY discovered_at DESC, crawl_run_id DESC, parent_alias
        LIMIT 1
        """,
        (alias,),
    ).fetchone()
    return row["parent_alias"] if row else None


def find_candidates(conn: sqlite3.Connection, db_label: str) -> List[Candidate]:
    sales_patterns = compile_markers(SALES_MARKERS)
    ai_patterns = compile_markers(AI_MARKERS)
    datacenter_patterns = compile_markers(DATACENTER_MARKERS)
    candidates: List[Candidate] = []
    rows = conn.execute(
        """
        SELECT alias, full_name, title, organization_name
        FROM people
        ORDER BY alias
        """
    )
    for row in rows:
        text = " ".join(
            str(value or "")
            for value in (
                row["title"],
                row["organization_name"],
            )
        )
        sales_hits = marker_hits(sales_patterns, text)
        ai_hits = marker_hits(ai_patterns, text)
        datacenter_hits = marker_hits(datacenter_patterns, text)
        if not (sales_hits and ai_hits and datacenter_hits):
            continue
        manager_alias = fetch_manager_alias(conn, row["alias"])
        if not manager_alias:
            continue
        candidates.append(
            Candidate(
                db_label=db_label,
                alias=row["alias"],
                full_name=row["full_name"],
                title=row["title"],
                organization_name=row["organization_name"],
                manager_alias=manager_alias,
                sales_matches=sales_hits,
                ai_matches=ai_hits,
                datacenter_matches=datacenter_hits,
            )
        )
    return candidates


def group_by_manager(candidates: Iterable[Candidate]) -> Dict[str, List[Candidate]]:
    groups: Dict[str, List[Candidate]] = {}
    for candidate in candidates:
        groups.setdefault(candidate.manager_alias, []).append(candidate)
    for rows in groups.values():
        rows.sort(key=lambda item: (-item.score, (item.full_name or item.alias).lower(), item.alias))
    return dict(sorted(groups.items(), key=lambda item: (-len(item[1]), item[0])))


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip().lower()).strip("-")
    return slug or "unknown"


def mermaid_node_id(alias: str) -> str:
    return "n_" + re.sub(r"[^a-zA-Z0-9_]", "_", alias)


def mermaid_label(node: Dict[str, Any]) -> str:
    lines = [
        node.get("full_name") or node.get("alias") or "unknown",
        node.get("title") or "",
        node.get("alias") or "",
    ]
    text = "<br/>".join(line for line in lines if line)
    return text.replace('"', "'")


def chart_to_mermaid(chart: Dict[str, Any]) -> str:
    nodes = {node["alias"]: node for node in chart.get("nodes", [])}
    lines = ["flowchart TD"]
    for alias, node in nodes.items():
        shape_open, shape_close = ("([", "])") if node.get("selected") else ("[", "]")
        lines.append(f'  {mermaid_node_id(alias)}{shape_open}"{mermaid_label(node)}"{shape_close}')
    for edge in chart.get("edges", []):
        parent = edge["parent_alias"]
        child = edge["child_alias"]
        if parent in nodes and child in nodes:
            lines.append(f"  {mermaid_node_id(parent)} --> {mermaid_node_id(child)}")
    if any(node.get("selected") for node in nodes.values()):
        lines.append("  classDef selected fill:#fff3bf,stroke:#b7791f,stroke-width:2px;")
        for node in nodes.values():
            if node.get("selected"):
                lines.append(f"  class {mermaid_node_id(node['alias'])} selected;")
    return "\n".join(lines)


def write_candidate_csv(path: Path, rows: Sequence[Candidate]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "db_label",
                "alias",
                "full_name",
                "title",
                "organization_name",
                "manager_alias",
                "score",
                "sales_matches",
                "ai_matches",
                "datacenter_matches",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.db_label,
                    row.alias,
                    row.full_name,
                    row.title,
                    row.organization_name,
                    row.manager_alias,
                    row.score,
                    ";".join(row.sales_matches),
                    ";".join(row.ai_matches),
                    ";".join(row.datacenter_matches),
                ]
            )


def markdown_link(label: str, path: Path, root: Path) -> str:
    return f"[{label}]({path.relative_to(root).as_posix()})"


def write_team_markdown(
    *,
    path: Path,
    chart: Dict[str, Any],
    candidates: Sequence[Candidate],
    generated_at: str,
) -> None:
    lines = [
        f"# AI Datacenter Infrastructure Sales Org Chart: {chart['db_label']} / {candidates[0].manager_alias}",
        "",
        f"- Generated: {generated_at}",
        f"- Database: {chart['db_label']}",
        f"- Root: {chart['root_label']} ({chart['root_alias']})",
        f"- Manager alias: {candidates[0].manager_alias}",
        f"- Matched people under manager: {len(candidates)}",
        f"- Chart nodes: {len(chart.get('nodes', []))}",
        f"- Chart edges: {len(chart.get('edges', []))}",
        "",
        "## Chart",
        "",
        "```mermaid",
        chart_to_mermaid(chart),
        "```",
        "",
        "## Matched People",
        "",
        "| Alias | Name | Title | Organization | Evidence |",
        "| --- | --- | --- | --- | --- |",
    ]
    for candidate in candidates:
        evidence = ", ".join(
            [
                f"sales={';'.join(candidate.sales_matches)}",
                f"ai={';'.join(candidate.ai_matches)}",
                f"dc={';'.join(candidate.datacenter_matches)}",
            ]
        )
        lines.append(
            "| "
            + " | ".join(
                escape_table_cell(value)
                for value in (
                    candidate.alias,
                    candidate.full_name or "",
                    candidate.title or "",
                    candidate.organization_name or "",
                    evidence,
                )
            )
            + " |"
        )
    warnings = chart.get("warnings") or []
    if warnings:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {warning}" for warning in warnings)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def escape_table_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def build_artifacts(output_root: Path, db_labels: Sequence[str]) -> Dict[str, Any]:
    generated_at = utc_now()
    output_root.mkdir(parents=True, exist_ok=True)
    charts_dir = output_root / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)
    all_candidates: List[Candidate] = []
    team_rows: List[Dict[str, Any]] = []
    summaries: Dict[str, Any] = {}

    for db_label in db_labels:
        config = DB_CONFIGS[db_label]
        with connect(config["db_path"]) as conn:
            candidates = find_candidates(conn, db_label)
            all_candidates.extend(candidates)
            groups = group_by_manager(candidates)
            summaries[db_label] = {
                "candidate_count": len(candidates),
                "team_count": len(groups),
                "db_path": str(config["db_path"]),
            }
            for manager_alias, group_candidates in groups.items():
                selected_aliases = [candidate.alias for candidate in group_candidates]
                chart = build_focused_org_chart(
                    conn=conn,
                    db_label=db_label,
                    db_path=str(config["db_path"]),
                    root_alias=config["root_alias"],
                    root_label=config["root_label"],
                    selected_aliases=selected_aliases,
                    expanded_aliases=[manager_alias],
                )
                chart_payload = {
                    "generated_at": generated_at,
                    "manager_alias": manager_alias,
                    "candidate_aliases": selected_aliases,
                    "candidates": [candidate.__dict__ for candidate in group_candidates],
                    "chart": chart,
                }
                stem = f"{db_label}_{safe_slug(manager_alias)}"
                json_path = charts_dir / f"{stem}.json"
                md_path = charts_dir / f"{stem}.md"
                json_path.write_text(json.dumps(chart_payload, indent=2, sort_keys=True), encoding="utf-8")
                write_team_markdown(
                    path=md_path,
                    chart=chart,
                    candidates=group_candidates,
                    generated_at=generated_at,
                )
                direct_report_count = sum(
                    group.get("direct_report_count", 0)
                    for group in chart.get("direct_report_groups", [])
                )
                team_rows.append(
                    {
                        "db_label": db_label,
                        "manager_alias": manager_alias,
                        "candidate_count": len(group_candidates),
                        "direct_report_count": direct_report_count,
                        "node_count": len(chart.get("nodes", [])),
                        "edge_count": len(chart.get("edges", [])),
                        "warning_count": len(chart.get("warnings", [])),
                        "markdown_path": str(md_path),
                        "json_path": str(json_path),
                    }
                )

    write_candidate_csv(output_root / "matched-people.csv", all_candidates)
    write_team_csv(output_root / "teams.csv", team_rows)
    write_summary(output_root, generated_at, summaries, team_rows)
    manifest = {
        "generated_at": generated_at,
        "definition": {
            "required_marker_groups": ["sales", "ai", "datacenter"],
            "sales_markers": sorted(SALES_MARKERS),
            "ai_markers": sorted(AI_MARKERS),
            "datacenter_markers": sorted(DATACENTER_MARKERS),
        },
        "summaries": summaries,
        "team_count": len(team_rows),
        "candidate_count": len(all_candidates),
        "outputs": {
            "summary_md": str(output_root / "summary.md"),
            "teams_csv": str(output_root / "teams.csv"),
            "matched_people_csv": str(output_root / "matched-people.csv"),
            "charts_dir": str(charts_dir),
        },
    }
    (output_root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def write_team_csv(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        fieldnames = [
            "db_label",
            "manager_alias",
            "candidate_count",
            "direct_report_count",
            "node_count",
            "edge_count",
            "warning_count",
            "markdown_path",
            "json_path",
        ]
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_summary(
    output_root: Path,
    generated_at: str,
    summaries: Dict[str, Any],
    team_rows: Sequence[Dict[str, Any]],
) -> None:
    lines = [
        "# AI Datacenter Infrastructure Sales Org Charts",
        "",
        f"- Generated: {generated_at}",
        "- Scope: focused Jeetu and Oliver org databases.",
        "- Match rule: person title/organization must contain at least one sales/GTM marker, one AI/accelerated-compute marker, and one datacenter/infrastructure marker.",
        "- Sensitive directory details remain local in this output folder.",
        "",
        "## Aggregate Counts",
        "",
        "| Database | Matched people | Team charts |",
        "| --- | ---: | ---: |",
    ]
    for db_label, summary in summaries.items():
        lines.append(f"| {db_label} | {summary['candidate_count']} | {summary['team_count']} |")
    lines.extend(
        [
            "",
            "## Files",
            "",
            f"- Matched people CSV: {markdown_link('matched-people.csv', output_root / 'matched-people.csv', output_root)}",
            f"- Team index CSV: {markdown_link('teams.csv', output_root / 'teams.csv', output_root)}",
            f"- Machine manifest: {markdown_link('manifest.json', output_root / 'manifest.json', output_root)}",
            "",
            "## Team Charts",
            "",
            "| Database | Manager alias | Matches | Direct reports in chart | Nodes | Warnings | Chart |",
            "| --- | --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for row in sorted(team_rows, key=lambda item: (-int(item["candidate_count"]), item["db_label"], item["manager_alias"])):
        md_path = Path(row["markdown_path"])
        lines.append(
            "| "
            + " | ".join(
                [
                    escape_table_cell(row["db_label"]),
                    escape_table_cell(row["manager_alias"]),
                    str(row["candidate_count"]),
                    str(row["direct_report_count"]),
                    str(row["node_count"]),
                    str(row["warning_count"]),
                    markdown_link(md_path.name, md_path, output_root),
                ]
            )
            + " |"
        )
    (output_root / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument(
        "--db",
        choices=sorted(DB_CONFIGS),
        action="append",
        help="Focused DB to scan. Repeat for multiple DBs. Defaults to all focused DBs.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db_labels = args.db or sorted(DB_CONFIGS)
    manifest = build_artifacts(args.output_root, db_labels)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
