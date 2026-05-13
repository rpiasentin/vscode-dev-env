#!/usr/bin/env python3
"""Local-only Cisco org overlay crawler and document ingester.

This script is designed to stay on the current machine. It builds a local
SQLite database, archives metadata and extracted text, and keeps a portable
runbook/worklog so another agent can continue from the same workspace.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import http.cookiejar
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple


REPO_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_ROOT = REPO_ROOT / "output" / "research" / "cisco-org-overlay"
DEFAULT_DB = OUTPUT_ROOT / "cisco_org_overlay.sqlite3"
DEFAULT_STORAGE_STATE = OUTPUT_ROOT / "storage-state.json"
DEFAULT_REPORT_DIR = OUTPUT_ROOT / "reports"
DEFAULT_WORKLOG = OUTPUT_ROOT / "worklog.md"
DEFAULT_LOG_FILE = OUTPUT_ROOT / "logs" / "crawler.log"
DEFAULT_ARCHIVE_DIR = OUTPUT_ROOT / "archive"
DEFAULT_EXTRACT_DIR = OUTPUT_ROOT / "extracts"
SCHEMA_PATH = Path(__file__).with_name("schema.sql")

DIRECTORY_BASE = "https://directory-gateway.cisco.com/api/directory"
DIRECTORY_PROFILE = DIRECTORY_BASE + "/v2/profile/{alias}?reports=all"
DIRECTORY_DIRECT_REPORTS = (
    DIRECTORY_BASE + "/v2/directReports/{alias}?pageNumber={page}&pageSize={page_size}&reports=all"
)
DIRECTORY_PTO = DIRECTORY_BASE + "/outlook/{alias}/pto"
DIRECTORY_WEBEX = DIRECTORY_BASE + "/webexStatus/{alias}"

DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://directory.cisco.com",
    "Referer": "https://directory.cisco.com/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    ),
}
ACTIVE_HEADERS = dict(DEFAULT_HEADERS)
ACTIVE_COOKIES: List[Dict[str, Any]] = []

LABEL_PATTERNS = {
    "function": {
        "sales": [
            " sales",
            "account executive",
            "account manager",
            "seller",
            "client director",
            "business development",
            "major account",
            "global account",
            "strategic account",
            "territory account",
        ],
        "sales_engineering": [
            "sales engineering",
            "sales engineer",
            "solutions engineer",
            "systems engineer",
            "solution architect",
            "solutions architect",
            "technical solutions architect",
            "technical leader",
            "specialist",
            "tsa",
            "ssa",
            "architectures and engineering",
        ],
        "partner_sales": [
            "partner sales",
            "channel",
            "partner account",
            "alliance",
            "distribution",
        ],
        "marketing": ["marketing"],
        "operations": [" operations", "ops"],
        "finance": ["finance", "financial officer", "cfo"],
        "legal": ["legal", "counsel"],
        "people": ["people", "hr", "human resources"],
        "communications": ["communications"],
    },
    "leader_level": {
        "director_plus": [
            "director",
            "vp",
            "vice president",
            "svp",
            "evp",
            "president",
            "chief",
            "gm",
            "general manager",
            "head of",
        ],
        "vp_plus": ["vp", "vice president", "svp", "evp", "president", "chief"],
        "account_owner_likely": [
            "account executive",
            "account manager",
            "major account",
            "global account",
            "strategic account",
            "client director",
            "gam",
            "sam",
            "named account",
        ],
        "technical_seller_likely": [
            "solutions engineer",
            "sales engineer",
            "systems engineer",
            "solutions architect",
            "specialist",
            "tsa",
            "ssa",
        ],
    },
    "domain": {
        "ai_networking": [
            "ai networking",
            "ai-ready networking",
            "ai ready networking",
            "ai network",
            "networking ai",
        ],
        "ai_datacenter": [
            "ai data center",
            "ai datacenter",
            "ai-ready data center",
            "ai ready data center",
            "ai ready dc",
            "ai-ready dc",
            "ai dc",
        ],
        "ai_security": [
            "ai security",
            "security ai",
        ],
    },
}


class FetchError(RuntimeError):
    def __init__(self, url: str, status: Optional[int], body: str):
        super().__init__(f"request failed for {url} with status {status}")
        self.url = url
        self.status = status
        self.body = body


TRANSIENT_NETWORK_PATTERNS = [
    "nodename nor servname provided",
    "name or service not known",
    "temporary failure in name resolution",
    "timed out",
    "connection reset by peer",
    "remote end closed connection without response",
    "network is unreachable",
    "no route to host",
    "eof occurred in violation of protocol",
    "ssleoferror",
]


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_log(message: str, log_file: Path) -> None:
    ensure_dir(log_file.parent)
    stamped = f"[{utc_now()}] {message}"
    print(stamped)
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(stamped + "\n")


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path: Path, data: Any) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)


def normalize_alias(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if text.endswith("@cisco.com"):
        text = text.split("@", 1)[0]
    if "(" in text and ")" in text:
        inside = re.findall(r"\(([^)]+)\)", text)
        if inside:
            text = inside[-1].strip().lower()
    return re.sub(r"[^a-z0-9._-]+", "", text) or None


def canonical_cisco_email(value: Any, fallback_alias: Optional[str] = None) -> Optional[str]:
    text = safe_text(value)
    if text:
        lowered = text.lower()
        if "@" in lowered:
            return lowered
        normalized = normalize_alias(lowered)
        if normalized:
            return f"{normalized}@cisco.com"
    normalized_fallback = normalize_alias(fallback_alias)
    if normalized_fallback:
        return f"{normalized_fallback}@cisco.com"
    return None


def directory_profile_alias(profile: Dict[str, Any], fallback_alias: Optional[str] = None) -> Optional[str]:
    return normalize_alias(
        first_value(profile, "userId", "alias", "uid", "username", "directoryId") or fallback_alias
    )


def directory_profile_email(profile: Dict[str, Any], fallback_alias: Optional[str] = None) -> Optional[str]:
    explicit = first_value(profile, "email", "mail", "contact.email", "emailAddress")
    return canonical_cisco_email(explicit, fallback_alias=directory_profile_alias(profile, fallback_alias))


def safe_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    return text or None


def db_scalar(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, str, bytes)):
        return value
    return json.dumps(value, ensure_ascii=True, sort_keys=True)


def storage_cookie_to_cookie(source: Dict[str, Any]) -> http.cookiejar.Cookie:
    domain = source.get("domain", "")
    return http.cookiejar.Cookie(
        version=0,
        name=source["name"],
        value=source["value"],
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=bool(domain),
        domain_initial_dot=domain.startswith("."),
        path=source.get("path", "/"),
        path_specified=True,
        secure=bool(source.get("secure")),
        expires=source.get("expires"),
        discard=False,
        comment=None,
        comment_url=None,
        rest={"HttpOnly": source.get("httpOnly", False), "SameSite": source.get("sameSite")},
        rfc2109=False,
    )


def build_opener(storage_state_path: Path) -> urllib.request.OpenerDirector:
    configure_active_cookies(storage_state_path)
    return urllib.request.build_opener()


def configure_active_headers(extra_headers_path: Optional[Path]) -> None:
    global ACTIVE_HEADERS
    ACTIVE_HEADERS = dict(DEFAULT_HEADERS)
    if not extra_headers_path:
        return
    extra_headers = load_json(extra_headers_path)
    if not isinstance(extra_headers, dict):
        raise ValueError(f"extra headers file must contain an object: {extra_headers_path}")
    for key, value in extra_headers.items():
        if value in (None, ""):
            continue
        ACTIVE_HEADERS[str(key)] = str(value)


def configure_active_cookies(storage_state_path: Path) -> None:
    global ACTIVE_COOKIES
    state = load_json(storage_state_path)
    ACTIVE_COOKIES = list(state.get("cookies", []))


def cookie_matches(url: str, cookie: Dict[str, Any]) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    domain = str(cookie.get("domain") or "").lower()
    path = cookie.get("path") or "/"
    if cookie.get("secure") and parsed.scheme != "https":
        return False
    if domain.startswith("."):
        suffix = domain[1:]
        if host != suffix and not host.endswith("." + suffix):
            return False
    else:
        if host != domain:
            return False
    if not parsed.path.startswith(path):
        return False
    return True


def cookie_header_for_url(url: str) -> Optional[str]:
    pairs = []
    for cookie in ACTIVE_COOKIES:
        if cookie_matches(url, cookie):
            name = cookie.get("name")
            value = cookie.get("value")
            if name and value is not None:
                pairs.append(f"{name}={value}")
    if not pairs:
        return None
    return "; ".join(pairs)


def is_transient_network_error(exc: FetchError) -> bool:
    if exc.status is not None:
        return False
    lowered = (exc.body or "").lower()
    return any(pattern in lowered for pattern in TRANSIENT_NETWORK_PATTERNS)


def request_text(
    opener: urllib.request.OpenerDirector,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 60,
    max_attempts: int = 4,
) -> str:
    merged_headers = dict(ACTIVE_HEADERS)
    if headers:
        merged_headers.update(headers)
    cookie_header = cookie_header_for_url(url)
    if cookie_header:
        merged_headers["Cookie"] = cookie_header
    last_error: Optional[FetchError] = None
    for attempt in range(1, max_attempts + 1):
        request = urllib.request.Request(url, headers=merged_headers)
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            raise FetchError(url, exc.code, body) from exc
        except (urllib.error.URLError, socket.timeout) as exc:
            last_error = FetchError(url, None, str(exc))
            if attempt >= max_attempts or not is_transient_network_error(last_error):
                raise last_error from exc
            time.sleep(min(8.0, 0.5 * (2 ** (attempt - 1))))
    if last_error is not None:
        raise last_error
    raise FetchError(url, None, "unknown request failure")


def request_json(
    opener: urllib.request.OpenerDirector,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 60,
) -> Any:
    body = request_text(opener, url, headers=headers, timeout=timeout)
    if not body.strip():
        return None
    return json.loads(body)


def maybe_get_path(obj: Any, path: str) -> Any:
    current = obj
    for piece in path.split("."):
        if current is None:
            return None
        if isinstance(current, list):
            try:
                current = current[int(piece)]
            except (ValueError, IndexError):
                return None
            continue
        if not isinstance(current, dict):
            return None
        current = current.get(piece)
    return current


def first_value(obj: Any, *paths: str) -> Any:
    for path in paths:
        value = maybe_get_path(obj, path)
        if value not in (None, "", [], {}):
            return value
    return None


def iter_scalars(value: Any) -> Iterator[str]:
    if value is None:
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from iter_scalars(item)
        return
    if isinstance(value, list):
        for item in value:
            yield from iter_scalars(item)
        return
    if isinstance(value, (str, int, float, bool)):
        text = str(value).strip()
        if text:
            yield text


def flatten_text(value: Any, limit: int = 50000) -> str:
    pieces: List[str] = []
    total = 0
    seen: set[str] = set()
    for text in iter_scalars(value):
        normalized = re.sub(r"\s+", " ", text)
        if normalized in seen:
            continue
        seen.add(normalized)
        pieces.append(normalized)
        total += len(normalized) + 1
        if total >= limit:
            break
    return "\n".join(pieces)


def infer_company_type(email: Optional[str], source_system: str, existing: Optional[str] = None) -> str:
    if existing and existing != "unknown":
        return existing
    if email and email.lower().endswith("@cisco.com"):
        return "cisco"
    if source_system == "directory":
        return "cisco"
    return "partner_or_external"


def phone_digits(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    digits = re.sub(r"\D+", "", value)
    return digits or None


def parse_direct_counts(value: Any) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    if value is None:
        return None, None, None
    if isinstance(value, int):
        return value, None, None
    if isinstance(value, str):
        try:
            return int(value), None, None
        except ValueError:
            return None, None, None
    if isinstance(value, dict):
        employee = None
        contingent = None
        for key in ("blue", "employee", "employees", "employeeCount", "employeeDirectReports"):
            candidate = value.get(key)
            if isinstance(candidate, (int, float)):
                employee = int(candidate)
                break
        for key in ("red", "contingent", "contingentWorkers", "contingentCount", "contingentDirectReports"):
            candidate = value.get(key)
            if isinstance(candidate, (int, float)):
                contingent = int(candidate)
                break
        total = 0
        has_total = False
        for candidate in (employee, contingent):
            if candidate is not None:
                total += candidate
                has_total = True
        if not has_total:
            for item in value.values():
                if isinstance(item, (int, float)):
                    total += int(item)
                    has_total = True
        return (total if has_total else None), employee, contingent
    return None, None, None


def extract_phone_records(profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    phones: List[Dict[str, Any]] = []

    def add_phone(value: Any, label: Optional[str] = None, kind: Optional[str] = None) -> None:
        text = safe_text(value)
        if not text:
            return
        phones.append(
            {
                "phone_value": text,
                "phone_label": label,
                "phone_kind": kind,
                "normalized_value": phone_digits(text),
                "is_internal": 1 if "internal" in text.lower() else 0,
            }
        )

    for path in (
        "phone",
        "workPhone",
        "internalPhone",
        "mobilePhone",
        "contactPreferences.workPhone",
        "contactPreferences.internalPhone",
        "contactPreferences.mobilePhone",
        "contactPreferences.prefPhone",
        "contact.phone",
        "contact.workPhone",
    ):
        add_phone(first_value(profile, path), path, "string")

    collections = []
    for path in ("phones", "phoneNumbers", "contact.phones", "contact.phoneNumbers"):
        collection = maybe_get_path(profile, path)
        if isinstance(collection, list):
            collections.extend(collection)

    for item in collections:
        if isinstance(item, dict):
            add_phone(
                first_value(item, "value", "phone", "number", "displayValue"),
                safe_text(first_value(item, "label", "type", "kind")),
                safe_text(first_value(item, "type", "kind")),
            )
        else:
            add_phone(item)

    unique: Dict[Tuple[Optional[str], Optional[str], Optional[str]], Dict[str, Any]] = {}
    for record in phones:
        key = (record["phone_value"], record["phone_label"], record["phone_kind"])
        unique[key] = record
    return list(unique.values())


def derive_labels(record: Dict[str, Any], raw_text: str) -> List[Tuple[str, str, str]]:
    labels: List[Tuple[str, str, str]] = []
    title = (record.get("title") or "").lower()
    org_text = (record.get("organization_name") or "").lower()
    location_text = (record.get("location_text") or "").lower()
    joined = "\n".join(part for part in (title, org_text, location_text, raw_text.lower()) if part)

    if record.get("company_type"):
        labels.append(("company_type", record["company_type"], "derived"))
    if record.get("worker_type"):
        labels.append(("worker_type", str(record["worker_type"]), "directory"))

    for key, groups in LABEL_PATTERNS.items():
        for label_value, patterns in groups.items():
            if any(pattern in joined for pattern in patterns):
                labels.append((key, label_value, "derived"))

    if record.get("title"):
        if any(token in title for token in ("director", "vp", "vice president", "svp", "evp", "president", "chief")):
            labels.append(("leader", "true", "derived"))
        if "director" in title or "vp" in title or "vice president" in title or "chief" in title:
            labels.append(("mvp_scope", "director_plus", "derived"))

    if record.get("email"):
        domain = record["email"].split("@", 1)[-1].lower()
        labels.append(("email_domain", domain, "derived"))

    if record.get("country"):
        labels.append(("country", str(record["country"]), "directory"))
    if record.get("state"):
        labels.append(("state", str(record["state"]), "directory"))
    if record.get("city"):
        labels.append(("city", str(record["city"]), "directory"))

    unique = sorted(set(labels))
    return unique


def normalize_profile_bundle(alias: str, bundle: Dict[str, Any]) -> Dict[str, Any]:
    profile = bundle.get("profile") or {}
    pto = bundle.get("pto")
    webex = bundle.get("webex")

    alias = directory_profile_alias(profile, alias) or alias
    email = directory_profile_email(profile, alias)
    full_name = safe_text(first_value(profile, "fullName", "name", "displayName"))
    title = safe_text(first_value(profile, "jobTitle", "title", "work.title"))
    organization_name = safe_text(
        first_value(profile, "organization", "organizationName", "departmentName", "orgName")
    )
    department_name = safe_text(first_value(profile, "departmentName", "department"))
    location_text = safe_text(first_value(profile, "location", "locationText", "officeLocation"))
    if not location_text:
        parts = [
            safe_text(first_value(profile, "building", "buildingName")),
            safe_text(first_value(profile, "city")),
            safe_text(first_value(profile, "state")),
            safe_text(first_value(profile, "country")),
        ]
        location_text = ", ".join(part for part in parts if part) or None

    assistant = first_value(profile, "assistant", "contact.assistant") or {}
    assistant_alias = normalize_alias(first_value(assistant, "userId", "alias", "email"))
    assistant_name = safe_text(first_value(assistant, "fullName", "name", "displayName"))
    assistant_title = safe_text(first_value(assistant, "jobTitle", "title"))

    manager = first_value(profile, "leader", "manager", "reportsTo") or {}
    manager_alias = normalize_alias(first_value(manager, "userId", "alias", "email"))
    manager_name = safe_text(first_value(manager, "fullName", "name", "displayName"))

    phones = extract_phone_records(profile)
    company_type = infer_company_type(email, "directory")

    direct_report_total, employee_direct_count, contingent_direct_count = parse_direct_counts(
        first_value(
            profile,
            "directReportsCount",
            "directReportCount",
            "reportsCount",
            "directReports.count",
        )
    )
    if employee_direct_count is None:
        _, employee_direct_count, _ = parse_direct_counts(
            first_value(profile, "employeeDirectReportsCount", "employeeReportsCount")
        )
    if contingent_direct_count is None:
        _, _, contingent_direct_count = parse_direct_counts(
            first_value(profile, "contingentDirectReportsCount", "contingentReportsCount")
        )

    record = {
        "person_id": alias,
        "alias": alias,
        "email": email,
        "full_name": full_name,
        "title": title,
        "organization_name": organization_name,
        "department_name": department_name,
        "department_id": safe_text(first_value(profile, "departmentId", "orgId")),
        "worker_type": safe_text(first_value(profile, "workerType", "employmentType")),
        "employee_id": safe_text(first_value(profile, "employeeId", "emplid", "employeeNumber")),
        "manager_person_id": manager_alias,
        "manager_alias": manager_alias,
        "manager_name": manager_name,
        "location_text": location_text,
        "city": safe_text(first_value(profile, "city")),
        "state": safe_text(first_value(profile, "state", "province")),
        "country": safe_text(first_value(profile, "country")),
        "timezone_text": safe_text(first_value(profile, "timezone", "timeZone", "timezoneText")),
        "assistant_person_id": assistant_alias,
        "assistant_alias": assistant_alias,
        "assistant_name": assistant_name,
        "assistant_title": assistant_title,
        "direct_report_count": direct_report_total,
        "employee_direct_count": employee_direct_count,
        "contingent_direct_count": contingent_direct_count,
        "phones_json": json.dumps(phones, ensure_ascii=True),
        "pto_json": json.dumps(pto, ensure_ascii=True) if pto is not None else None,
        "webex_status_json": json.dumps(webex, ensure_ascii=True) if webex is not None else None,
        "badges_json": json.dumps(first_value(profile, "badges", "awards") or [], ensure_ascii=True),
        "company_type": company_type,
    }

    raw_text = flatten_text(bundle)
    labels = derive_labels(record, raw_text)
    record["labels"] = labels
    record["label_text"] = " ".join(f"{k}:{v}" for k, v, _ in labels)
    return record


def extract_direct_report_items(payload: Any) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    if payload is None:
        return [], None
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)], None
    if not isinstance(payload, dict):
        return [], None

    items = None
    for key in ("directReports", "reports", "items", "data", "results"):
        candidate = payload.get(key)
        if isinstance(candidate, list):
            items = candidate
            break
    total_pages = None
    for key in ("totalPages", "pageCount", "pages"):
        candidate = payload.get(key)
        if isinstance(candidate, int):
            total_pages = candidate
            break
    return [item for item in (items or []) if isinstance(item, dict)], total_pages


def connect_db(db_path: Path) -> sqlite3.Connection:
    ensure_dir(db_path.parent)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


def start_crawl_run(conn: sqlite3.Connection, mode: str, roots: List[Dict[str, Any]], notes: str) -> int:
    cur = conn.execute(
        """
        INSERT INTO crawl_runs (started_at, mode, status, roots_json, notes)
        VALUES (?, ?, 'running', ?, ?)
        """,
        (utc_now(), mode, json.dumps(roots, ensure_ascii=True), notes),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_crawl_run(conn: sqlite3.Connection, run_id: int, status: str = "completed") -> None:
    conn.execute(
        "UPDATE crawl_runs SET finished_at = ?, status = ? WHERE id = ?",
        (utc_now(), status, run_id),
    )
    conn.commit()


def record_auth_event(conn: sqlite3.Connection, system_name: str, event_type: str, details: str) -> None:
    conn.execute(
        "INSERT INTO auth_events (created_at, system_name, event_type, details) VALUES (?, ?, ?, ?)",
        (utc_now(), system_name, event_type, details),
    )
    conn.commit()


def upsert_person(conn: sqlite3.Connection, record: Dict[str, Any]) -> None:
    existing = conn.execute(
        "SELECT company_type, first_seen_at FROM people WHERE person_id = ?",
        (record["person_id"],),
    ).fetchone()
    company_type = infer_company_type(record.get("email"), "directory", existing["company_type"] if existing else None)
    first_seen = existing["first_seen_at"] if existing else utc_now()
    conn.execute(
        """
        INSERT INTO people (
            person_id, alias, canonical_email, display_name, company_type,
            source_origin, first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(person_id) DO UPDATE SET
            alias = excluded.alias,
            canonical_email = COALESCE(excluded.canonical_email, people.canonical_email),
            display_name = COALESCE(excluded.display_name, people.display_name),
            company_type = CASE
                WHEN people.company_type = 'unknown' THEN excluded.company_type
                ELSE people.company_type
            END,
            source_origin = COALESCE(people.source_origin, excluded.source_origin),
            last_seen_at = excluded.last_seen_at
        """,
        (
            record["person_id"],
            record.get("alias"),
            record.get("email"),
            record.get("full_name"),
            company_type,
            "directory",
            first_seen,
            utc_now(),
        ),
    )


def insert_snapshot(
    conn: sqlite3.Connection,
    crawl_run_id: int,
    record: Dict[str, Any],
    raw_bundle: Dict[str, Any],
) -> int:
    cur = conn.execute(
        """
        INSERT INTO person_snapshots (
            crawl_run_id, person_id, captured_at, source_system, source_ref,
            full_name, title, organization_name, department_name, department_id,
            worker_type, employee_id, manager_person_id, manager_alias, manager_name,
            location_text, city, state, country, timezone_text, email,
            assistant_person_id, assistant_alias, assistant_name, assistant_title,
            direct_report_count, employee_direct_count, contingent_direct_count,
            phones_json, pto_json, webex_status_json, badges_json, label_text,
            raw_json
        ) VALUES (
            ?, ?, ?, 'directory', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            crawl_run_id,
            record["person_id"],
            utc_now(),
            f"directory:{record.get('alias')}",
            db_scalar(record.get("full_name")),
            db_scalar(record.get("title")),
            db_scalar(record.get("organization_name")),
            db_scalar(record.get("department_name")),
            db_scalar(record.get("department_id")),
            db_scalar(record.get("worker_type")),
            db_scalar(record.get("employee_id")),
            db_scalar(record.get("manager_person_id")),
            db_scalar(record.get("manager_alias")),
            db_scalar(record.get("manager_name")),
            db_scalar(record.get("location_text")),
            db_scalar(record.get("city")),
            db_scalar(record.get("state")),
            db_scalar(record.get("country")),
            db_scalar(record.get("timezone_text")),
            db_scalar(record.get("email")),
            db_scalar(record.get("assistant_person_id")),
            db_scalar(record.get("assistant_alias")),
            db_scalar(record.get("assistant_name")),
            db_scalar(record.get("assistant_title")),
            db_scalar(record.get("direct_report_count")),
            db_scalar(record.get("employee_direct_count")),
            db_scalar(record.get("contingent_direct_count")),
            db_scalar(record.get("phones_json")),
            db_scalar(record.get("pto_json")),
            db_scalar(record.get("webex_status_json")),
            db_scalar(record.get("badges_json")),
            db_scalar(record.get("label_text")),
            json.dumps(raw_bundle, ensure_ascii=True),
        ),
    )
    snapshot_id = int(cur.lastrowid)
    conn.execute(
        "UPDATE people SET latest_snapshot_id = ? WHERE person_id = ?",
        (snapshot_id, record["person_id"]),
    )

    phone_rows = json.loads(record.get("phones_json") or "[]")
    for phone in phone_rows:
        conn.execute(
            """
            INSERT INTO person_phones (
                snapshot_id, phone_label, phone_kind, phone_value, normalized_value, is_internal
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                phone.get("phone_label"),
                phone.get("phone_kind"),
                phone.get("phone_value"),
                phone.get("normalized_value"),
                phone.get("is_internal", 0),
            ),
        )

    for label_key, label_value, label_source in record.get("labels", []):
        conn.execute(
            """
            INSERT OR IGNORE INTO person_labels (snapshot_id, label_key, label_value, label_source)
            VALUES (?, ?, ?, ?)
            """,
            (snapshot_id, label_key, label_value, label_source),
        )

    conn.execute("INSERT INTO person_fts(rowid, person_id, alias, full_name, title, organization_name, location_text, label_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            snapshot_id,
            record["person_id"],
            record.get("alias"),
            record.get("full_name"),
            record.get("title"),
            record.get("organization_name"),
            record.get("location_text"),
            record.get("label_text"),
        ),
    )
    return snapshot_id


def insert_person_path(
    conn: sqlite3.Connection,
    crawl_run_id: int,
    root_person_id: str,
    person_id: str,
    manager_person_id: Optional[str],
    depth: int,
    branch_label: Optional[str],
    path_scope: str,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO person_paths (
            crawl_run_id, root_person_id, person_id, manager_person_id, depth, branch_label, path_scope
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (crawl_run_id, root_person_id, person_id, manager_person_id, depth, branch_label, path_scope),
    )


def fetch_profile_bundle(opener: urllib.request.OpenerDirector, alias: str) -> Dict[str, Any]:
    bundle: Dict[str, Any] = {}
    bundle["profile"] = request_json(opener, DIRECTORY_PROFILE.format(alias=alias))

    def fetch_optional(url: str, label: str) -> Any:
        try:
            return request_json(opener, url)
        except FetchError as exc:
            if exc.status in (401, 403, 404, 500, 504):
                return {
                    "_error": {
                        "label": label,
                        "status": exc.status,
                        "url": exc.url,
                        "body_preview": exc.body[:500],
                    }
                }
            raise

    bundle["pto"] = fetch_optional(DIRECTORY_PTO.format(alias=alias), "pto")
    bundle["webex"] = fetch_optional(DIRECTORY_WEBEX.format(alias=alias), "webex")
    return bundle


def fetch_all_direct_reports(
    opener: urllib.request.OpenerDirector,
    alias: str,
    page_size: int = 1000,
) -> List[Dict[str, Any]]:
    page = 0
    items: List[Dict[str, Any]] = []
    total_pages: Optional[int] = None
    seen_aliases: set[str] = set()

    while True:
        payload = request_json(
            opener,
            DIRECTORY_DIRECT_REPORTS.format(alias=alias, page=page, page_size=page_size),
        )
        batch, discovered_pages = extract_direct_report_items(payload)
        if total_pages is None:
            total_pages = discovered_pages
        for item in batch:
            child_alias = normalize_alias(
                first_value(item, "userId", "alias", "uid", "username", "email")
            )
            if child_alias and child_alias in seen_aliases:
                continue
            if child_alias:
                seen_aliases.add(child_alias)
            items.append(item)
        if total_pages is not None and page + 1 >= total_pages:
            break
        if not batch or (total_pages is None and len(batch) < page_size):
            break
        page += 1

    return items


def parse_root_spec(root_spec: str) -> Dict[str, Any]:
    alias, _, depth_spec = root_spec.partition(":")
    alias = normalize_alias(alias)
    if not alias:
        raise ValueError(f"invalid root spec: {root_spec}")
    if not depth_spec:
        max_depth: Optional[int] = None
    elif depth_spec in ("leaf", "all", "full", "bottom"):
        max_depth = None
    else:
        max_depth = int(depth_spec)
    return {"alias": alias, "max_depth": max_depth}


def write_summary_json(
    path: Path,
    crawl_run_id: int,
    people_count: int,
    root_specs: List[Dict[str, Any]],
    notes: Dict[str, Any],
) -> None:
    save_json(
        path,
        {
            "crawl_run_id": crawl_run_id,
            "generated_at": utc_now(),
            "people_count": people_count,
            "roots": root_specs,
            "notes": notes,
        },
    )


def export_latest_people_csv(conn: sqlite3.Connection, crawl_run_id: int, out_path: Path) -> None:
    ensure_dir(out_path.parent)
    rows = conn.execute(
        """
        SELECT
            p.person_id,
            p.alias,
            p.company_type,
            s.full_name,
            s.title,
            s.organization_name,
            s.department_name,
            s.worker_type,
            s.email,
            s.location_text,
            s.city,
            s.state,
            s.country,
            s.manager_alias,
            s.manager_name,
            s.assistant_alias,
            s.assistant_name,
            s.assistant_title,
            s.direct_report_count,
            s.employee_direct_count,
            s.contingent_direct_count,
            s.label_text
        FROM person_snapshots s
        JOIN people p ON p.person_id = s.person_id
        WHERE s.crawl_run_id = ?
        ORDER BY COALESCE(s.full_name, p.alias), p.person_id
        """,
        (crawl_run_id,),
    ).fetchall()
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(rows[0].keys() if rows else [
            "person_id",
            "alias",
            "company_type",
            "full_name",
            "title",
            "organization_name",
            "department_name",
            "worker_type",
            "email",
            "location_text",
            "city",
            "state",
            "country",
            "manager_alias",
            "manager_name",
            "assistant_alias",
            "assistant_name",
            "assistant_title",
            "direct_report_count",
            "employee_direct_count",
            "contingent_direct_count",
            "label_text",
        ])
        for row in rows:
            writer.writerow([row[key] for key in row.keys()])


def export_global_latest_people_csv(conn: sqlite3.Connection, out_path: Path) -> None:
    ensure_dir(out_path.parent)
    rows = conn.execute(
        """
        SELECT
            p.person_id,
            p.alias,
            p.company_type,
            s.full_name,
            s.title,
            s.organization_name,
            s.department_name,
            s.worker_type,
            s.email,
            s.location_text,
            s.city,
            s.state,
            s.country,
            s.manager_alias,
            s.manager_name,
            s.assistant_alias,
            s.assistant_name,
            s.assistant_title,
            s.direct_report_count,
            s.employee_direct_count,
            s.contingent_direct_count,
            s.label_text
        FROM people p
        JOIN person_snapshots s ON s.id = p.latest_snapshot_id
        ORDER BY COALESCE(s.full_name, p.alias), p.person_id
        """
    ).fetchall()
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(rows[0].keys() if rows else [
            "person_id",
            "alias",
            "company_type",
            "full_name",
            "title",
            "organization_name",
            "department_name",
            "worker_type",
            "email",
            "location_text",
            "city",
            "state",
            "country",
            "manager_alias",
            "manager_name",
            "assistant_alias",
            "assistant_name",
            "assistant_title",
            "direct_report_count",
            "employee_direct_count",
            "contingent_direct_count",
            "label_text",
        ])
        for row in rows:
            writer.writerow([row[key] for key in row.keys()])


def latest_children_cache(conn: sqlite3.Connection) -> Dict[str, List[str]]:
    rows = conn.execute(
        """
        SELECT s.manager_person_id AS manager_person_id, p.person_id AS person_id
        FROM people p
        JOIN person_snapshots s ON s.id = p.latest_snapshot_id
        WHERE s.manager_person_id IS NOT NULL AND s.manager_person_id != ''
        """
    ).fetchall()
    children: Dict[str, List[str]] = {}
    for row in rows:
        children.setdefault(row["manager_person_id"], []).append(row["person_id"])
    return children


def latest_direct_count_cache(conn: sqlite3.Connection) -> Dict[str, Optional[int]]:
    rows = conn.execute(
        """
        SELECT p.person_id AS person_id, s.direct_report_count AS direct_report_count
        FROM people p
        JOIN person_snapshots s ON s.id = p.latest_snapshot_id
        """
    ).fetchall()
    return {row["person_id"]: row["direct_report_count"] for row in rows}


def build_frontier_queue(
    conn: sqlite3.Connection,
    source_run_id: int,
    roots: List[Dict[str, Any]],
    discovery_only: bool = False,
) -> Deque[Dict[str, Any]]:
    root_by_alias = {root["alias"]: root for root in roots}
    path_rows = conn.execute(
        """
        SELECT root_person_id, person_id, manager_person_id, depth, branch_label, path_scope
        FROM person_paths
        WHERE crawl_run_id = ?
        ORDER BY root_person_id, path_scope, depth, person_id
        """,
        (source_run_id,),
    ).fetchall()
    processed_keys = {
        (row["root_person_id"], row["path_scope"], row["person_id"])
        for row in path_rows
    }
    children_cache = latest_children_cache(conn)
    direct_count_cache = latest_direct_count_cache(conn)

    queue: Deque[Dict[str, Any]] = deque()
    scheduled: set[Tuple[str, str, str, str]] = set()

    def enqueue(item: Dict[str, Any], item_type: str) -> None:
        key = (item["root_alias"], item["path_scope"], item["alias"], item_type)
        if key in scheduled:
            return
        scheduled.add(key)
        queue.append(item)

    for row in path_rows:
        root_alias = row["root_person_id"]
        root = root_by_alias.get(root_alias)
        if not root:
            continue
        person_id = row["person_id"]
        depth = row["depth"]
        known_children = children_cache.get(person_id, [])

        if not discovery_only:
            for child_alias in known_children:
                child_key = (root_alias, row["path_scope"], child_alias)
                if child_key in processed_keys:
                    continue
                enqueue(
                    {
                        "alias": child_alias,
                        "root_alias": root_alias,
                        "parent_alias": person_id,
                        "depth": depth + 1,
                        "max_depth": root["max_depth"],
                        "path_scope": row["path_scope"],
                        "branch_label": row["branch_label"],
                    },
                    "node",
                )

        direct_count = direct_count_cache.get(person_id)
        if direct_count is not None and len(known_children) < int(direct_count):
            enqueue(
                {
                    "alias": person_id,
                    "root_alias": root_alias,
                    "parent_alias": row["manager_person_id"],
                    "depth": depth,
                    "max_depth": root["max_depth"],
                    "path_scope": row["path_scope"],
                    "branch_label": row["branch_label"],
                    "force_live_expand": True,
                },
                "expand",
            )

    for root in roots:
        root_key = (root["alias"], root["path_scope"], root["alias"])
        if root_key not in processed_keys:
            enqueue(
                {
                    "alias": root["alias"],
                    "root_alias": root["alias"],
                    "parent_alias": None,
                    "depth": 0,
                    "max_depth": root["max_depth"],
                    "path_scope": root["path_scope"],
                    "branch_label": root["branch_label"],
                },
                "node",
            )
    return queue


def path_keys_for_run(conn: sqlite3.Connection, source_run_id: int) -> set[Tuple[str, str, str]]:
    rows = conn.execute(
        """
        SELECT root_person_id, path_scope, person_id
        FROM person_paths
        WHERE crawl_run_id = ?
        """,
        (source_run_id,),
    ).fetchall()
    return {(row["root_person_id"], row["path_scope"], row["person_id"]) for row in rows}


def export_paths_csv(conn: sqlite3.Connection, crawl_run_id: int, out_path: Path) -> None:
    ensure_dir(out_path.parent)
    rows = conn.execute(
        """
        SELECT
            path_scope,
            root_person_id,
            person_id,
            manager_person_id,
            depth,
            branch_label
        FROM person_paths
        WHERE crawl_run_id = ?
        ORDER BY path_scope, root_person_id, depth, person_id
        """,
        (crawl_run_id,),
    ).fetchall()
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(rows[0].keys() if rows else [
            "path_scope", "root_person_id", "person_id", "manager_person_id", "depth", "branch_label"
        ])
        for row in rows:
            writer.writerow([row[key] for key in row.keys()])


def latest_snapshots_for_run(conn: sqlite3.Connection, run_id: int) -> Dict[str, sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT
            p.person_id,
            p.alias,
            s.full_name,
            s.title,
            s.organization_name,
            s.label_text
        FROM people p
        JOIN person_snapshots s ON s.id = p.latest_snapshot_id
        WHERE s.crawl_run_id = ?
        """,
        (run_id,),
    ).fetchall()
    return {row["person_id"]: row for row in rows}


def previous_completed_run(conn: sqlite3.Connection, crawl_run_id: int, mode: str) -> Optional[int]:
    row = conn.execute(
        """
        SELECT id
        FROM crawl_runs
        WHERE mode = ? AND status = 'completed' AND id < ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (mode, crawl_run_id),
    ).fetchone()
    return int(row["id"]) if row else None


def write_change_report(conn: sqlite3.Connection, crawl_run_id: int, out_path: Path) -> None:
    prev_run_id = previous_completed_run(conn, crawl_run_id, "directory")
    current = latest_snapshots_for_run(conn, crawl_run_id)
    ensure_dir(out_path.parent)
    lines = [f"# Crawl Change Report", "", f"- Generated: {utc_now()}", f"- Current crawl: {crawl_run_id}"]

    if prev_run_id is None:
        lines.extend(
            [
                "- Previous crawl: none",
                "",
                "Initial crawl. Title changes and document appearance/disappearance will start on the next completed run.",
            ]
        )
        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return

    previous = latest_snapshots_for_run(conn, prev_run_id)
    lines.append(f"- Previous crawl: {prev_run_id}")
    lines.append("")

    title_changes = []
    for person_id, current_row in sorted(current.items()):
        previous_row = previous.get(person_id)
        if not previous_row:
            continue
        old_title = previous_row["title"] or ""
        new_title = current_row["title"] or ""
        if old_title != new_title:
            title_changes.append(
                f"- {current_row['full_name'] or person_id} ({current_row['alias'] or person_id}): "
                f"`{old_title}` -> `{new_title}`"
            )

    new_people = sorted(set(current) - set(previous))
    departed_people = sorted(set(previous) - set(current))

    lines.append("## Title Changes")
    if title_changes:
        lines.extend(title_changes)
    else:
        lines.append("- No title changes detected.")

    lines.append("")
    lines.append("## Presence Changes")
    if new_people:
        lines.extend(f"- New in crawl: {current[person_id]['full_name'] or person_id} ({person_id})" for person_id in new_people[:200])
    if departed_people:
        lines.extend(
            f"- Missing from current crawl: {previous[person_id]['full_name'] or person_id} ({person_id})"
            for person_id in departed_people[:200]
        )
    if not new_people and not departed_people:
        lines.append("- No person presence changes detected.")

    lines.append("")
    lines.append("## Document Overlay Notes")
    lines.append("- Document appearance/disappearance tracking is wired into the schema but will become meaningful after document ingestion runs.")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def archive_text(path: Path, text: str) -> None:
    ensure_dir(path.parent)
    path.write_text(text, encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_zip_xml_text(path: Path) -> str:
    texts: List[str] = []
    with zipfile.ZipFile(path) as zf:
        names = sorted(
            name
            for name in zf.namelist()
            if name.endswith(".xml")
            and (
                name.startswith("ppt/")
                or name.startswith("doc/")
                or name.startswith("xl/")
            )
        )
        for name in names:
            try:
                root = ET.fromstring(zf.read(name))
            except ET.ParseError:
                continue
            chunk = " ".join(part.strip() for part in root.itertext() if part and part.strip())
            if chunk:
                texts.append(chunk)
    return "\n".join(texts)


def extract_text_from_local_file(path: Path) -> Optional[str]:
    suffix = path.suffix.lower()
    if suffix in (".pptx", ".docx", ".xlsx"):
        return extract_zip_xml_text(path)
    if suffix in (".txt", ".md", ".csv", ".tsv", ".json"):
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".pdf":
        if shutil_which("pdftotext"):
            proc = subprocess.run(
                ["pdftotext", "-layout", str(path), "-"],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode == 0:
                return proc.stdout
        return None
    return None


def shutil_which(command: str) -> Optional[str]:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / command
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def link_document_mentions(conn: sqlite3.Connection, document_id: int, extracted_text: str) -> int:
    if not extracted_text.strip():
        return 0
    lowered = extracted_text.lower()
    aliases = conn.execute(
        "SELECT person_id, alias FROM people WHERE alias IS NOT NULL AND alias != ''"
    ).fetchall()
    count = 0
    for row in aliases:
        alias = row["alias"].lower()
        if len(alias) < 4:
            continue
        pattern = rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])"
        if re.search(pattern, lowered):
            conn.execute(
                """
                INSERT OR IGNORE INTO document_mentions (
                    document_id, person_id, mention_type, mention_text, confidence
                ) VALUES (?, ?, 'alias_match', ?, 0.7)
                """,
                (document_id, row["person_id"], alias),
            )
            count += 1
    return count


def ingest_document_manifest(args: argparse.Namespace) -> None:
    conn = connect_db(args.db)
    init_db(conn)
    entries = load_json(args.manifest)
    if isinstance(entries, dict):
        entries = entries.get("documents", [])
    if not isinstance(entries, list):
        raise ValueError("manifest must be a list or an object with a documents list")

    for entry in entries:
        archived_path = Path(entry["archived_path"]).resolve() if entry.get("archived_path") else None
        extracted_text = None
        extracted_path = None
        sha256 = None
        size_bytes = None
        file_extension = None
        if archived_path and archived_path.exists():
            sha256 = sha256_file(archived_path)
            size_bytes = archived_path.stat().st_size
            file_extension = archived_path.suffix.lower()
            extracted_text = extract_text_from_local_file(archived_path)
            if extracted_text:
                extracted_path = args.extract_dir / f"{sha256}.txt"
                archive_text(extracted_path, extracted_text)

        cur = conn.execute(
            """
            INSERT INTO documents (
                discovered_at, source_system, query_text, external_ref, title, url, owner_name, owner_alias,
                company_type, archived_path, extracted_text_path, mime_type, file_extension, size_bytes, sha256,
                raw_metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(external_ref) DO UPDATE SET
                title = excluded.title,
                url = excluded.url,
                owner_name = excluded.owner_name,
                owner_alias = excluded.owner_alias,
                archived_path = COALESCE(excluded.archived_path, documents.archived_path),
                extracted_text_path = COALESCE(excluded.extracted_text_path, documents.extracted_text_path),
                mime_type = COALESCE(excluded.mime_type, documents.mime_type),
                file_extension = COALESCE(excluded.file_extension, documents.file_extension),
                size_bytes = COALESCE(excluded.size_bytes, documents.size_bytes),
                sha256 = COALESCE(excluded.sha256, documents.sha256),
                raw_metadata = excluded.raw_metadata
            """,
            (
                entry.get("discovered_at") or utc_now(),
                entry.get("source_system") or "manual",
                entry.get("query_text"),
                entry.get("external_ref") or entry.get("url"),
                entry.get("title"),
                entry.get("url"),
                entry.get("owner_name"),
                normalize_alias(entry.get("owner_alias")),
                entry.get("company_type") or "unknown",
                str(archived_path) if archived_path else None,
                str(extracted_path) if extracted_path else None,
                entry.get("mime_type"),
                file_extension,
                size_bytes,
                sha256,
                json.dumps(entry, ensure_ascii=True),
            ),
        )
        document_id = int(cur.lastrowid or conn.execute(
            "SELECT id FROM documents WHERE external_ref = ?",
            (entry.get("external_ref") or entry.get("url"),),
        ).fetchone()["id"])

        conn.execute(
            "INSERT OR REPLACE INTO document_fts(rowid, title, owner_name, owner_alias, url, query_text, extracted_text) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                document_id,
                entry.get("title"),
                entry.get("owner_name"),
                normalize_alias(entry.get("owner_alias")),
                entry.get("url"),
                entry.get("query_text"),
                extracted_text or "",
            ),
        )

        if extracted_text:
            link_document_mentions(conn, document_id, extracted_text)

    conn.commit()


def crawl_directory(args: argparse.Namespace) -> None:
    roots = []
    for root_spec in args.root:
        parsed = parse_root_spec(root_spec)
        parsed["path_scope"] = "sales_full" if parsed["alias"] == "otuszik" and parsed["max_depth"] is None else "company_6"
        parsed["branch_label"] = "sales" if parsed["alias"] == "otuszik" else "company"
        roots.append(parsed)

    conn = connect_db(args.db)
    init_db(conn)
    configure_active_headers(args.extra_headers)
    opener = build_opener(args.storage_state)
    crawl_run_id = start_crawl_run(conn, "directory", roots, args.notes or "")
    write_log(
        f"starting directory crawl run={crawl_run_id} roots={json.dumps(roots, ensure_ascii=True)}",
        args.log_file,
    )

    work_queue: Deque[Dict[str, Any]]
    covered_path_keys: set[Tuple[str, str, str]] = set()
    pending_path_keys: set[Tuple[str, str, str]] = set()
    if args.resume_from_run is not None:
        work_queue = build_frontier_queue(conn, args.resume_from_run, roots, args.discovery_only)
        covered_path_keys = path_keys_for_run(conn, args.resume_from_run)
        for item in work_queue:
            if not item.get("force_live_expand"):
                pending_path_keys.add((item["root_alias"], item["path_scope"], item["alias"]))
        write_log(
            (
                        f"seeded frontier queue from run={args.resume_from_run} items={len(work_queue)} "
                        f"covered_paths={len(covered_path_keys)}"
                    ),
                    args.log_file,
        )
    else:
        work_queue = deque()
        for root in roots:
            work_queue.append(
                {
                    "alias": root["alias"],
                    "root_alias": root["alias"],
                    "parent_alias": None,
                    "depth": 0,
                    "max_depth": root["max_depth"],
                    "path_scope": root["path_scope"],
                    "branch_label": root["branch_label"],
                }
            )
            pending_path_keys.add((root["alias"], root["path_scope"], root["alias"]))

    visited: Dict[str, str] = {}
    if args.resume_existing:
        existing_rows = conn.execute(
            "SELECT person_id, alias FROM people WHERE alias IS NOT NULL AND alias != ''"
        ).fetchall()
        visited = {row["alias"]: row["person_id"] for row in existing_rows}
        write_log(
            f"seeded visited cache from existing database aliases={len(visited)}",
            args.log_file,
        )
    warned_large = False
    people_count = len(visited)
    new_people_count = 0
    processed_count = 0
    auth_failures = 0
    seen_path_keys: set[Tuple[str, str, str]] = set()
    child_cache = latest_children_cache(conn)
    direct_count_cache = latest_direct_count_cache(conn)
    started = time.monotonic()

    try:
        while work_queue:
            item = work_queue.popleft()
            processed_count += 1
            alias = item["alias"]
            root_alias = item["root_alias"]
            parent_alias = item["parent_alias"]
            depth = item["depth"]
            max_depth = item["max_depth"]
            force_live_expand = bool(item.get("force_live_expand"))
            current_path_key = (item["root_alias"], item["path_scope"], alias)
            if not force_live_expand and current_path_key in pending_path_keys:
                pending_path_keys.discard(current_path_key)

            if args.max_people and people_count >= args.max_people and alias not in visited:
                write_log(f"max_people={args.max_people} reached; stopping crawl early", args.log_file)
                break

            if alias not in visited:
                try:
                    bundle = fetch_profile_bundle(opener, alias)
                except FetchError as exc:
                    auth_failures += 1
                    record_auth_event(
                        conn,
                        "directory",
                        "fetch_failure",
                        json.dumps(
                            {
                                "alias": alias,
                                "url": exc.url,
                                "status": exc.status,
                                "body_preview": exc.body[:1000],
                            },
                            ensure_ascii=True,
                        ),
                    )
                    write_log(
                        f"fetch failure alias={alias} status={exc.status} url={exc.url}",
                        args.log_file,
                    )
                    if exc.status in (401, 403) or is_transient_network_error(exc):
                        raise
                    continue

                record = normalize_profile_bundle(alias, bundle)
                upsert_person(conn, record)
                insert_snapshot(conn, crawl_run_id, record, bundle)
                visited[alias] = record["person_id"]
                people_count += 1
                new_people_count += 1
                direct_count_cache[record["person_id"]] = record.get("direct_report_count")
                manager_id = record.get("manager_person_id")
                if manager_id:
                    child_cache.setdefault(manager_id, [])
                    if record["person_id"] not in child_cache[manager_id]:
                        child_cache[manager_id].append(record["person_id"])
                if not warned_large and people_count > args.warn_record_count:
                    warned_large = True
                    write_log(
                        f"warning threshold crossed: people_count={people_count} exceeds warn_record_count={args.warn_record_count}",
                        args.log_file,
                    )

            person_id = visited[alias]
            manager_person_id = visited.get(parent_alias) if parent_alias else None
            path_key = (item["path_scope"], root_alias, person_id)
            if path_key not in seen_path_keys:
                insert_person_path(
                    conn,
                    crawl_run_id,
                    root_alias,
                    person_id,
                    manager_person_id,
                    depth,
                    item["branch_label"],
                    item["path_scope"],
                )
                seen_path_keys.add(path_key)
                covered_path_keys.add((root_alias, item["path_scope"], person_id))

            if processed_count % args.commit_every == 0:
                conn.commit()
                elapsed = time.monotonic() - started
                write_log(
                    (
                        f"checkpoint run={crawl_run_id} processed={processed_count} "
                        f"known_people={people_count} new_people={new_people_count} "
                        f"queue={len(work_queue)} elapsed_sec={elapsed:.1f}"
                    ),
                    args.log_file,
                )

            if max_depth is not None and depth >= max_depth:
                continue

            use_cached_children = False
            cached_child_aliases: List[str] = []
            if args.resume_existing and not force_live_expand:
                cached_child_aliases = child_cache.get(alias, [])
                latest_count = direct_count_cache.get(alias)
                if latest_count is not None and len(cached_child_aliases) >= int(latest_count):
                    use_cached_children = True

            if use_cached_children:
                direct_reports = [{"userId": child_alias} for child_alias in cached_child_aliases]
            else:
                try:
                    direct_reports = fetch_all_direct_reports(opener, alias)
                except FetchError as exc:
                    auth_failures += 1
                    record_auth_event(
                        conn,
                        "directory",
                        "direct_reports_failure",
                        json.dumps(
                            {
                                "alias": alias,
                                "url": exc.url,
                                "status": exc.status,
                                "body_preview": exc.body[:1000],
                            },
                            ensure_ascii=True,
                        ),
                    )
                    write_log(
                        f"direct reports failure alias={alias} status={exc.status} url={exc.url}",
                        args.log_file,
                    )
                    if exc.status in (401, 403) or is_transient_network_error(exc):
                        raise
                    continue

            for report in direct_reports:
                child_alias = normalize_alias(
                    first_value(report, "userId", "alias", "uid", "username", "email")
                )
                if not child_alias:
                    continue
                child_path_key = (root_alias, item["path_scope"], child_alias)
                if child_path_key in covered_path_keys or child_path_key in pending_path_keys:
                    continue
                if args.discovery_only and child_alias in visited:
                    continue
                pending_path_keys.add(child_path_key)
                work_queue.append(
                    {
                        "alias": child_alias,
                        "root_alias": root_alias,
                        "parent_alias": alias,
                        "depth": depth + 1,
                        "max_depth": max_depth,
                        "path_scope": item["path_scope"],
                        "branch_label": item["branch_label"],
                    }
                )

        conn.commit()
        finish_crawl_run(conn, crawl_run_id, "completed")
        write_log(
            (
                f"completed directory crawl run={crawl_run_id} known_people={people_count} "
                f"new_people={new_people_count} auth_failures={auth_failures}"
            ),
            args.log_file,
        )
        write_summary_json(
            args.report_dir / f"crawl-summary-run-{crawl_run_id}.json",
            crawl_run_id,
            people_count,
            roots,
            {
                "auth_failures": auth_failures,
                "warned_large": warned_large,
                "warn_record_count": args.warn_record_count,
                "new_people_count": new_people_count,
                "processed_count": processed_count,
                "resume_existing": args.resume_existing,
            },
        )
        export_latest_people_csv(conn, crawl_run_id, args.report_dir / f"people-latest-run-{crawl_run_id}.csv")
        export_global_latest_people_csv(conn, args.report_dir / "people-latest-global.csv")
        export_paths_csv(conn, crawl_run_id, args.report_dir / f"paths-run-{crawl_run_id}.csv")
        write_change_report(conn, crawl_run_id, args.report_dir / f"changes-run-{crawl_run_id}.md")
    except Exception:
        conn.commit()
        finish_crawl_run(conn, crawl_run_id, "failed")
        raise


def init_worklog(path: Path) -> None:
    if path.exists():
        return
    ensure_dir(path.parent)
    path.write_text(
        "\n".join(
            [
                "# Cisco Org Overlay Worklog",
                "",
                "- Scope: local-only org overlay build rooted at Chuck Robbins, with Oliver Tuszik crawled to leaf nodes.",
                "- Storage: SQLite, archived metadata, extracted text, and reports under this workspace only.",
                "- Auth note: keep a concise note here when a larger re-auth cycle is needed across Cisco internal systems.",
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def cmd_init(args: argparse.Namespace) -> None:
    conn = connect_db(args.db)
    init_db(conn)
    ensure_dir(args.report_dir)
    ensure_dir(args.archive_dir)
    ensure_dir(args.extract_dir)
    ensure_dir(args.log_file.parent)
    init_worklog(args.worklog)
    write_log(f"initialized database at {args.db}", args.log_file)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.set_defaults(func=None)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", type=Path, default=DEFAULT_DB)
    common.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    common.add_argument("--archive-dir", type=Path, default=DEFAULT_ARCHIVE_DIR)
    common.add_argument("--extract-dir", type=Path, default=DEFAULT_EXTRACT_DIR)
    common.add_argument("--worklog", type=Path, default=DEFAULT_WORKLOG)
    common.add_argument("--log-file", type=Path, default=DEFAULT_LOG_FILE)

    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", parents=[common], help="initialize local db and directories")
    init_parser.set_defaults(func=cmd_init)

    crawl_parser = subparsers.add_parser(
        "crawl-directory",
        parents=[common],
        help="crawl the Cisco Directory tree from one or more roots",
    )
    crawl_parser.add_argument("--storage-state", type=Path, default=DEFAULT_STORAGE_STATE)
    crawl_parser.add_argument("--extra-headers", type=Path)
    crawl_parser.add_argument(
        "--root",
        action="append",
        required=True,
        help="root spec as alias:depth, alias:leaf, or alias:full",
    )
    crawl_parser.add_argument("--notes", default="")
    crawl_parser.add_argument("--max-people", type=int, default=0)
    crawl_parser.add_argument("--warn-record-count", type=int, default=30000)
    crawl_parser.add_argument("--commit-every", type=int, default=100)
    crawl_parser.add_argument("--resume-existing", action="store_true")
    crawl_parser.add_argument("--resume-from-run", type=int)
    crawl_parser.add_argument("--discovery-only", action="store_true")
    crawl_parser.set_defaults(func=crawl_directory)

    ingest_parser = subparsers.add_parser(
        "ingest-document-manifest",
        parents=[common],
        help="ingest document metadata and local extracts from a JSON manifest",
    )
    ingest_parser.add_argument("--manifest", type=Path, required=True)
    ingest_parser.set_defaults(func=ingest_document_manifest)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.func is None:
        parser.print_help()
        return 1
    args.report_dir = ensure_dir(args.report_dir)
    args.archive_dir = ensure_dir(args.archive_dir)
    args.extract_dir = ensure_dir(args.extract_dir)
    args.log_file.parent.mkdir(parents=True, exist_ok=True)
    init_worklog(args.worklog)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
