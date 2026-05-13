#!/usr/bin/env python3
"""Local watchdog for the Cisco org overlay pipeline.

This keeps everything on the current machine. It can:
- flush the local DB/reports/logs for a fresh run
- refresh the persistent browser session automatically
- start or resume the crawler
- emit a status report every 30 minutes by default
"""

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
from typing import Any, Dict, List, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_ROOT = REPO_ROOT / "output" / "research" / "cisco-org-overlay"
DB_PATH = OUTPUT_ROOT / "cisco_org_overlay.sqlite3"
REPORT_DIR = OUTPUT_ROOT / "reports"
LOG_DIR = OUTPUT_ROOT / "logs"
WATCHDOG_LOG = LOG_DIR / "watchdog.log"
STATUS_JSON = REPORT_DIR / "pipeline-status.json"
STATUS_MD = REPORT_DIR / "pipeline-status.md"
PID_FILE = OUTPUT_ROOT / "watchdog.pid"
CRAWLER_PID_FILE = OUTPUT_ROOT / "crawler.pid"
SEED_MANIFEST = OUTPUT_ROOT / "seed-documents.json"
CRAWLER = REPO_ROOT / "tools" / "cisco_org_overlay" / "crawler.py"
TOOL_DIR = REPO_ROOT / "tools" / "cisco_org_overlay"
SESSION_MANIFEST = OUTPUT_ROOT / "session-manifest.json"
EXTRA_HEADERS = OUTPUT_ROOT / "directory-extra-headers.json"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def log(message: str) -> None:
    ensure_dir(WATCHDOG_LOG.parent)
    line = f"[{utc_now()}] {message}"
    print(line)
    with WATCHDOG_LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def write_json(path: Path, data: Any) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)


def connect_db() -> Optional[sqlite3.Connection]:
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def current_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    row = conn.execute(
        """
        SELECT
            (SELECT count(*) FROM people) AS people_count,
            (SELECT count(*) FROM person_snapshots) AS snapshot_count,
            (SELECT count(*) FROM person_paths) AS path_count,
            (SELECT count(*) FROM auth_events) AS auth_event_count
        """
    ).fetchone()
    return dict(row) if row else {
        "people_count": 0,
        "snapshot_count": 0,
        "path_count": 0,
        "auth_event_count": 0,
    }


def latest_run(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM crawl_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()


def read_tail(path: Path, lines: int = 12) -> List[str]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return text[-lines:]


def find_active_crawler_pids() -> List[int]:
    proc = subprocess.run(
        ["ps", "-axo", "pid,command"],
        capture_output=True,
        text=True,
        check=False,
    )
    pids: List[int] = []
    for line in proc.stdout.splitlines():
        if "tools/cisco_org_overlay/crawler.py crawl-directory" not in line:
            continue
        if "watchdog.py" in line:
            continue
        try:
            pid_text = line.strip().split(None, 1)[0]
            pids.append(int(pid_text))
        except (ValueError, IndexError):
            continue
    return pids


def stop_active_crawlers() -> None:
    pids = find_active_crawler_pids()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            log(f"stopped active crawler pid={pid}")
        except ProcessLookupError:
            continue


def clear_fresh_state() -> None:
    stop_active_crawlers()
    for path in [
        DB_PATH,
        OUTPUT_ROOT / "test.sqlite3",
        OUTPUT_ROOT / "cisco_org_overlay.sqlite3-journal",
        CRAWLER_PID_FILE,
        STATUS_JSON,
        STATUS_MD,
    ]:
        if path.exists():
            path.unlink()
    for directory in [REPORT_DIR, LOG_DIR]:
        if directory.exists():
            for child in directory.iterdir():
                if child.is_file():
                    child.unlink()
    ensure_dir(REPORT_DIR)
    ensure_dir(LOG_DIR)
    log("cleared local DB, reports, and logs for fresh start")


def run_command(
    args: Sequence[str],
    cwd: Path,
    timeout: Optional[int] = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(args),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
        env={**os.environ, "PYTHONPYCACHEPREFIX": "/tmp/cisco-org-overlay-pyc"},
    )


def refresh_session() -> None:
    log("refreshing persistent browser session")
    proc = run_command(
        ["npm", "run", "hardened-session", "--", "--auto"],
        cwd=TOOL_DIR,
        timeout=240,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"hardened session refresh failed rc={proc.returncode} stderr={proc.stderr[-1000:]}"
        )
    log("persistent browser session refreshed")


def init_db() -> None:
    run_command([sys.executable, str(CRAWLER), "init"], cwd=REPO_ROOT, timeout=120)
    if SEED_MANIFEST.exists():
        run_command(
            [
                sys.executable,
                str(CRAWLER),
                "ingest-document-manifest",
                "--manifest",
                str(SEED_MANIFEST),
            ],
            cwd=REPO_ROOT,
            timeout=120,
        )
    log("database initialized")


def choose_resume_run(conn: sqlite3.Connection) -> Optional[int]:
    row = conn.execute(
        "SELECT id FROM crawl_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return int(row["id"]) if row else None


def crawler_command(conn: Optional[sqlite3.Connection]) -> List[str]:
    base = [
        sys.executable,
        str(CRAWLER),
        "crawl-directory",
        "--extra-headers",
        str(EXTRA_HEADERS),
        "--root",
        "crobbins:6",
        "--root",
        "otuszik:leaf",
        "--commit-every",
        "100",
    ]
    if conn is None:
        return base

    counts = current_counts(conn)
    if counts["people_count"] <= 0:
        return base

    resume_run = choose_resume_run(conn)
    if resume_run is None:
        return base

    return base + [
        "--resume-existing",
        "--resume-from-run",
        str(resume_run),
        "--discovery-only",
    ]


def spawn_crawler(conn: Optional[sqlite3.Connection]) -> subprocess.Popen:
    cmd = crawler_command(conn)
    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env={**os.environ, "PYTHONPYCACHEPREFIX": "/tmp/cisco-org-overlay-pyc"},
    )
    CRAWLER_PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    log("started crawler: " + " ".join(cmd))
    return proc


def status_payload(conn: Optional[sqlite3.Connection], crawler_proc: Optional[subprocess.Popen]) -> Dict[str, Any]:
    latest = latest_run(conn) if conn else None
    counts = current_counts(conn) if conn else {
        "people_count": 0,
        "snapshot_count": 0,
        "path_count": 0,
        "auth_event_count": 0,
    }
    payload = {
        "generated_at": utc_now(),
        "db_path": str(DB_PATH),
        "session_manifest": str(SESSION_MANIFEST),
        "crawler_process_pid": crawler_proc.pid if crawler_proc and crawler_proc.poll() is None else None,
        "active_crawler_pids": find_active_crawler_pids(),
        "latest_run": dict(latest) if latest else None,
        "counts": counts,
        "log_tail": read_tail(LOG_DIR / "crawler.log", 12),
    }
    return payload


def write_status(conn: Optional[sqlite3.Connection], crawler_proc: Optional[subprocess.Popen]) -> None:
    payload = status_payload(conn, crawler_proc)
    write_json(STATUS_JSON, payload)
    lines = [
        "# Pipeline Status",
        "",
        f"- Generated: {payload['generated_at']}",
        f"- Crawler PID: {payload['crawler_process_pid']}",
        f"- Active crawler PIDs: {', '.join(str(pid) for pid in payload['active_crawler_pids']) or 'none'}",
    ]
    latest = payload["latest_run"]
    if latest:
        lines.extend(
            [
                f"- Latest run: {latest['id']}",
                f"- Latest run status: {latest['status']}",
                f"- Started: {latest['started_at']}",
                f"- Finished: {latest['finished_at'] or 'running'}",
            ]
        )
    counts = payload["counts"]
    lines.extend(
        [
            f"- People: {counts['people_count']}",
            f"- Snapshots: {counts['snapshot_count']}",
            f"- Paths: {counts['path_count']}",
            f"- Auth events: {counts['auth_event_count']}",
            "",
            "## Log Tail",
            "",
        ]
    )
    lines.extend(f"- {line}" for line in payload["log_tail"])
    ensure_dir(STATUS_MD.parent)
    STATUS_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fresh-reset", action="store_true")
    parser.add_argument("--status-interval", type=int, default=1800, help="seconds between status reports")
    parser.add_argument("--poll-interval", type=int, default=60, help="seconds between watchdog checks")
    args = parser.parse_args(argv)

    ensure_dir(REPORT_DIR)
    ensure_dir(LOG_DIR)
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")

    if args.fresh_reset:
        clear_fresh_state()
        init_db()

    crawler_proc: Optional[subprocess.Popen] = None
    last_status_at = 0.0

    while True:
        conn = connect_db()
        now = time.time()

        if crawler_proc is not None and crawler_proc.poll() is not None:
            log(f"crawler process exited rc={crawler_proc.returncode}")
            crawler_proc = None

        active_pids = find_active_crawler_pids()
        if crawler_proc is None and active_pids:
            log(f"detected external crawler pids={active_pids}; watchdog will not start a duplicate process")
        elif crawler_proc is None and not active_pids:
            try:
                refresh_session()
                conn = connect_db()
                crawler_proc = spawn_crawler(conn)
            except Exception as exc:
                log(f"watchdog failed to refresh/start crawler: {exc}")

        if now - last_status_at >= args.status_interval or last_status_at == 0:
            write_status(conn, crawler_proc)
            last_status_at = now
            log("wrote pipeline status report")

        if conn is not None:
            conn.close()
        time.sleep(args.poll_interval)


if __name__ == "__main__":
    raise SystemExit(main())
