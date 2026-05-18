#!/usr/bin/env python3
"""Reusable supervisor for deterministic focused crawlers."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import crawler as base
from focused_crawl import FocusConfig, ensure_focused_schema, focus_counts, latest_run, output_paths, refresh_anchor_from_notes


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_DIR = REPO_ROOT / "tools" / "cisco_org_overlay"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def log(message: str, log_path: Path) -> None:
    ensure_dir(log_path.parent)
    line = f"[{utc_now()}] {message}"
    print(line)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def run_command(
    args: Sequence[str],
    cwd: Path,
    pycache_prefix: str,
    timeout: Optional[int] = None,
    check: bool = False,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(args),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
        env={**os.environ, "PYTHONPYCACHEPREFIX": pycache_prefix},
    )


def pids_for_substring(needle: str) -> list[int]:
    proc = subprocess.run(
        ["ps", "-axo", "pid,command"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        check=False,
    )
    pids: list[int] = []
    for line in proc.stdout.splitlines():
        if needle not in line:
            continue
        try:
            pid = int(line.strip().split(None, 1)[0])
        except (IndexError, ValueError):
            continue
        if pid != os.getpid():
            pids.append(pid)
    return pids


def stop_existing_helpers(config: FocusConfig, log_path: Path) -> None:
    for needle in ("node hardened_session.mjs", config.crawler_script_name):
        for pid in pids_for_substring(needle):
            try:
                os.kill(pid, signal.SIGTERM)
                log(f"stopped process pid={pid} needle={needle}", log_path)
            except ProcessLookupError:
                continue


def probe_directory_access(config: FocusConfig, alias: Optional[str] = None) -> tuple[bool, str]:
    target_alias = alias or config.default_root_alias
    try:
        base.configure_active_headers(config.extra_headers_path)
        opener = base.build_opener(config.storage_state_path)
        payload = base.request_json(opener, base.DIRECTORY_PROFILE.format(alias=target_alias), timeout=20)
        profile = payload or {}
        display_name = profile.get("fullName") or profile.get("name") or profile.get("displayName") or target_alias
        return True, f"profile-ok alias={target_alias} display_name={display_name}"
    except base.FetchError as exc:
        body = (exc.body or "").strip().replace("\n", " ")
        return False, f"profile-failed alias={target_alias} status={exc.status} body={body[:180]}"
    except Exception as exc:  # pragma: no cover - defensive supervisor logging
        return False, f"profile-probe-error alias={target_alias} error={exc}"


def refresh_session(
    config: FocusConfig,
    session_mode: str,
    profile_alias: str,
    timeout: int,
    log_path: Path,
) -> str:
    stop_existing_helpers(config, log_path)
    base_args = [
        "npm",
        "run",
        "hardened-session",
        "--",
        "--auto",
        "--profile-alias",
        profile_alias,
    ]
    attempts: list[tuple[str, list[str]]] = []
    if session_mode == "headless-first":
        attempts = [
            ("headless", base_args + ["--headless"]),
            ("headed", base_args),
        ]
    elif session_mode == "headless":
        attempts = [("headless", base_args + ["--headless"])]
    else:
        attempts = [("headed", base_args)]

    last_error: Optional[str] = None
    for mode_name, cmd in attempts:
        log(
            f"refreshing persistent browser session mode={mode_name} profile_alias={profile_alias}",
            log_path,
        )
        proc = run_command(
            cmd,
            cwd=TOOL_DIR,
            pycache_prefix=config.pycache_prefix,
            timeout=timeout,
            check=False,
        )
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip().splitlines()[-8:]
            stdout = (proc.stdout or "").strip().splitlines()[-8:]
            last_error = (
                f"session refresh failed mode={mode_name} rc={proc.returncode} "
                f"stdout_tail={' | '.join(stdout)} stderr_tail={' | '.join(stderr)}"
            )
            log(last_error, log_path)
            continue

        ok, detail = probe_directory_access(config, profile_alias)
        log(f"session refresh probe mode={mode_name} {detail}", log_path)
        if ok:
            return mode_name
        last_error = f"session refresh completed but probe failed mode={mode_name} detail={detail}"

    raise RuntimeError(last_error or "session refresh failed")


def spawn_crawler(
    config: FocusConfig,
    fresh: bool = False,
    refresh_existing: bool = False,
    resume_refresh: bool = False,
    resume_refresh_from_run: Optional[int] = None,
) -> subprocess.Popen:
    crawler_script = TOOL_DIR / config.crawler_script_name
    cmd = [sys.executable, str(crawler_script)]
    if fresh:
        cmd.append("--fresh")
    elif refresh_existing:
        cmd.append("--refresh-existing")
    elif resume_refresh:
        cmd.append("--resume-refresh")
        if resume_refresh_from_run is not None:
            cmd.extend(["--resume-refresh-from-run", str(resume_refresh_from_run)])
    else:
        cmd.append("--resume")
    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env={**os.environ, "PYTHONPYCACHEPREFIX": config.pycache_prefix},
    )
    return proc


def connect_existing_db(db_path: Path) -> Optional[sqlite3.Connection]:
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    ensure_focused_schema(conn)
    return conn


def read_tail(path: Path, lines: int = 12) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]


def write_status(
    conn: Optional[sqlite3.Connection],
    crawler_proc: Optional[subprocess.Popen],
    config: FocusConfig,
    last_refresh_mode: Optional[str],
    last_refresh_at: Optional[str],
    last_error: Optional[str],
    probe_detail: Optional[str],
) -> None:
    paths = output_paths(config)
    latest = latest_run(conn) if conn else None
    info = focus_counts(conn) if conn else {}
    payload: Dict[str, Any] = {
        "generated_at": utc_now(),
        "crawler_pid": crawler_proc.pid if crawler_proc and crawler_proc.poll() is None else None,
        "latest_run": dict(latest) if latest else None,
        "counts": info,
        "last_refresh_mode": last_refresh_mode,
        "last_refresh_at": last_refresh_at,
        "last_error": last_error,
        "probe_detail": probe_detail,
        "log_tail": read_tail(paths.log_path.parent / "crawler.log"),
    }
    status_json = paths.report_dir / "supervisor-status.json"
    status_md = paths.report_dir / "supervisor-status.md"
    ensure_dir(status_json.parent)
    status_json.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    lines = [
        f"# {config.supervisor_title}",
        "",
        f"- Generated: {payload['generated_at']}",
        f"- Crawler PID: {payload['crawler_pid']}",
        f"- Last refresh mode: {payload['last_refresh_mode'] or 'none'}",
        f"- Last refresh at: {payload['last_refresh_at'] or 'never'}",
        f"- Last probe detail: {payload['probe_detail'] or 'none'}",
        f"- Last error: {payload['last_error'] or 'none'}",
    ]
    if latest:
        lines.extend(
            [
                f"- Latest run: {latest['id']}",
                f"- Latest run status: {latest['status']}",
                f"- Started: {latest['started_at']}",
                f"- Finished: {latest['finished_at'] or 'running'}",
            ]
        )
    if info:
        lines.extend(
            [
                f"- People: {info['people_count']}",
                f"- Edges: {info['edge_count']}",
                f"- Manager checks: {info['manager_check_count']}",
                f"- Unresolved aliases: {info['unresolved_alias_count']}",
                f"- Residual unresolved aliases: {info['residual_unresolved_alias_count']}",
                f"- Deficit managers: {info['deficit_managers']}",
                f"- Missing child profiles: {info['missing_people_from_edges']}",
                f"- Residual missing child profiles: {info['residual_missing_people_from_edges']}",
            ]
        )
    lines.extend(["", "## Log Tail", ""])
    lines.extend(f"- {line}" for line in payload["log_tail"])
    status_md.write_text("\n".join(lines) + "\n", encoding="utf-8")


def should_stop_after_completion(conn: sqlite3.Connection) -> bool:
    info = focus_counts(conn)
    return (
        info["deficit_managers"] == 0
        and info["residual_unresolved_alias_count"] == 0
        and info["residual_missing_people_from_edges"] == 0
    )


def run_activity(conn: sqlite3.Connection, run_id: int) -> Dict[str, int]:
    row = conn.execute(
        """
        SELECT
            (SELECT count(*) FROM person_snapshots WHERE crawl_run_id = ?) AS snapshots,
            (SELECT count(*) FROM manager_checks WHERE crawl_run_id = ?) AS checks,
            (SELECT count(*) FROM org_edge_observations WHERE crawl_run_id = ?) AS edge_observations
        """,
        (run_id, run_id, run_id),
    ).fetchone()
    return dict(row) if row else {"snapshots": 0, "checks": 0, "edge_observations": 0}


def progress_signature(conn: sqlite3.Connection) -> tuple[int, ...]:
    info = focus_counts(conn)
    keys = (
        "people_count",
        "edge_count",
        "inactive_edge_count",
        "manager_check_count",
        "deficit_managers",
        "transient_error_managers",
        "unresolved_alias_count",
        "residual_unresolved_alias_count",
        "missing_people_from_edges",
        "residual_missing_people_from_edges",
    )
    return tuple(int(info.get(key, 0)) for key in keys)


def auth_refresh_anchor_for_run(row: sqlite3.Row) -> Optional[int]:
    return refresh_anchor_from_notes(row["notes"], int(row["id"]))


def run_supervisor_cli(config: FocusConfig, argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=f"{config.crawl_label} supervisor.")
    parser.add_argument("--poll-interval", type=int, default=60)
    parser.add_argument("--status-interval", type=int, default=1800)
    parser.add_argument("--refresh-timeout", type=int, default=240)
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument(
        "--refresh-existing",
        action="store_true",
        help="Start one refresh pass that refetches existing focused people without deleting the database.",
    )
    parser.add_argument(
        "--resume-refresh",
        action="store_true",
        help="Continue a failed refresh-existing pass without deleting the database.",
    )
    parser.add_argument(
        "--resume-refresh-from-run",
        type=int,
        help="Anchor run id for --resume-refresh. Defaults to the latest incomplete refresh run.",
    )
    parser.add_argument(
        "--max-no-progress-restarts",
        type=int,
        default=3,
        help="Stop after this many completed restarts that make no aggregate progress.",
    )
    parser.add_argument(
        "--session-mode",
        choices=("headless-first", "headless", "headed"),
        default="headless-first",
    )
    parser.add_argument("--profile-alias", default=config.default_root_alias)
    args = parser.parse_args(argv)

    paths = output_paths(config)
    supervisor_log = paths.output_root / "logs" / "supervisor.log"
    pid_file = paths.output_root / "supervisor.pid"

    ensure_dir(paths.output_root / "reports")
    ensure_dir(paths.output_root / "logs")
    pid_file.write_text(str(os.getpid()), encoding="utf-8")

    crawler_proc: Optional[subprocess.Popen] = None
    last_status_at = 0.0
    last_refresh_mode: Optional[str] = None
    last_refresh_at: Optional[str] = None
    last_error: Optional[str] = None
    probe_detail: Optional[str] = None
    refresh_existing_pending = args.refresh_existing
    resume_refresh_pending = args.resume_refresh
    resume_refresh_from_run = args.resume_refresh_from_run
    no_progress_restarts = 0
    last_progress_signature: Optional[tuple[int, ...]] = None

    while True:
        conn = connect_existing_db(paths.db_path)
        try:
            if crawler_proc is not None and crawler_proc.poll() is not None:
                log(f"crawler exited rc={crawler_proc.returncode}", supervisor_log)
                crawler_proc = None
                latest_after_exit = latest_run(conn) if conn else None
                if latest_after_exit and latest_after_exit["status"] == "auth_expired":
                    anchor = auth_refresh_anchor_for_run(latest_after_exit)
                    if anchor is not None:
                        resume_refresh_pending = True
                        resume_refresh_from_run = resume_refresh_from_run or anchor
                        log(
                            f"crawler paused for auth refresh; will resume refresh from run={resume_refresh_from_run}",
                            supervisor_log,
                        )
                    else:
                        log("crawler paused for auth refresh; will restart in resume mode", supervisor_log)
                    no_progress_restarts = 0
                elif latest_after_exit and latest_after_exit["status"] == "completed":
                    if should_stop_after_completion(conn):
                        no_progress_restarts = 0
                    else:
                        activity = run_activity(conn, int(latest_after_exit["id"]))
                        signature = progress_signature(conn)
                        made_activity = any(activity.values())
                        made_aggregate_progress = last_progress_signature is None or signature != last_progress_signature
                        if made_activity and made_aggregate_progress:
                            no_progress_restarts = 0
                        else:
                            no_progress_restarts += 1
                            log(
                                "completed crawler made no aggregate progress "
                                f"restart_count={no_progress_restarts} activity={activity}",
                                supervisor_log,
                            )
                        last_progress_signature = signature
                        if no_progress_restarts >= args.max_no_progress_restarts:
                            last_error = (
                                "stopped after "
                                f"{no_progress_restarts} no-progress restarts with completion blockers remaining"
                            )
                            write_status(
                                conn,
                                crawler_proc,
                                config,
                                last_refresh_mode,
                                last_refresh_at,
                                last_error,
                                probe_detail,
                            )
                            log(last_error, supervisor_log)
                            return 0

            latest = latest_run(conn) if conn else None
            if (
                latest
                and latest["status"] == "completed"
                and should_stop_after_completion(conn)
                and not refresh_existing_pending
                and not resume_refresh_pending
            ):
                write_status(conn, crawler_proc, config, last_refresh_mode, last_refresh_at, last_error, probe_detail)
                log(f"{config.display_name}-focused crawl reached completion criteria", supervisor_log)
                return 0

            if crawler_proc is None:
                try:
                    last_refresh_mode = refresh_session(
                        config=config,
                        session_mode=args.session_mode,
                        profile_alias=args.profile_alias,
                        timeout=args.refresh_timeout,
                        log_path=supervisor_log,
                    )
                    last_refresh_at = utc_now()
                    ok, probe_detail = probe_directory_access(config, args.profile_alias)
                    if not ok:
                        raise RuntimeError(f"directory probe failed after refresh: {probe_detail}")
                    last_error = None
                    spawn_refresh_existing = refresh_existing_pending
                    spawn_resume_refresh = resume_refresh_pending and not spawn_refresh_existing
                    crawler_proc = spawn_crawler(
                        config,
                        fresh=args.fresh and latest is None,
                        refresh_existing=spawn_refresh_existing,
                        resume_refresh=spawn_resume_refresh,
                        resume_refresh_from_run=resume_refresh_from_run if spawn_resume_refresh else None,
                    )
                    refresh_existing_pending = False
                    resume_refresh_pending = False
                    log(
                        "started focused crawler: " + " ".join([sys.executable, str(TOOL_DIR / config.crawler_script_name)]),
                        supervisor_log,
                    )
                except Exception as exc:
                    last_error = str(exc)
                    log(f"supervisor failed to refresh/start crawler: {exc}", supervisor_log)

            now = time.time()
            if last_status_at == 0 or now - last_status_at >= args.status_interval:
                write_status(conn, crawler_proc, config, last_refresh_mode, last_refresh_at, last_error, probe_detail)
                last_status_at = now
                log("wrote supervisor status report", supervisor_log)
        finally:
            if conn:
                conn.close()

        time.sleep(args.poll_interval)
