"""SQLite persistence with migrations, user accounts, OAuth states, durable job leases, and provenance."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import settings

_LOCK = threading.RLock()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: datetime | None = None) -> str:
    return (dt or _utc_now()).isoformat()


def _db_path() -> Path:
    path = Path(settings.database_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db() -> None:
    with _LOCK:
        with _connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'user',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS oauth_states (
                    state TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    owner_user_id TEXT,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress_pct INTEGER NOT NULL DEFAULT 0,
                    format TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    result_json TEXT,
                    disclosure_json TEXT,
                    provenance_json TEXT,
                    error TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    worker_id TEXT,
                    lease_expires_at TEXT,
                    heartbeat_at TEXT,
                    next_run_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY,
                    job_id TEXT,
                    owner_user_id TEXT,
                    asset_type TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    sha256_hash TEXT NOT NULL,
                    mime_type TEXT,
                    width INTEGER,
                    height INTEGER,
                    duration_seconds REAL,
                    provenance_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_assets_sha256 ON assets(sha256_hash);
                CREATE INDEX IF NOT EXISTS idx_assets_job_id ON assets(job_id);

                CREATE TABLE IF NOT EXISTS publish_records (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    owner_user_id TEXT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    youtube_video_id TEXT,
                    privacy_status TEXT NOT NULL,
                    publish_at TEXT,
                    qa_passed INTEGER NOT NULL DEFAULT 0,
                    qa_report_json TEXT,
                    synthetic_disclosed INTEGER NOT NULL DEFAULT 0,
                    disclosure_json TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            # Safe migrations for pre-existing studio.db files
            _ensure_column(conn, "jobs", "owner_user_id", "TEXT")
            _ensure_column(conn, "jobs", "disclosure_json", "TEXT")
            _ensure_column(conn, "jobs", "provenance_json", "TEXT")
            _ensure_column(conn, "jobs", "worker_id", "TEXT")
            _ensure_column(conn, "jobs", "lease_expires_at", "TEXT")
            _ensure_column(conn, "jobs", "heartbeat_at", "TEXT")
            _ensure_column(conn, "jobs", "next_run_at", "TEXT")
            _ensure_column(conn, "assets", "owner_user_id", "TEXT")
            _ensure_column(conn, "publish_records", "owner_user_id", "TEXT")
            _ensure_column(conn, "publish_records", "disclosure_json", "TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status_next_run ON jobs(status, next_run_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_owner ON jobs(owner_user_id)"
            )


def sha256_file(file_path: str | Path) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def count_users() -> int:
    init_db()
    with _LOCK:
        with _connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
            return int(row["c"]) if row else 0


def create_user(username: str, password_hash: str, role: str = "user") -> dict[str, Any]:
    init_db()
    uid = uuid.uuid4().hex
    now = _utc_iso()
    with _LOCK:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO users (id, username, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
                (uid, username, password_hash, role, now),
            )
            row = conn.execute("SELECT id, username, role, created_at FROM users WHERE id = ?", (uid,)).fetchone()
    return dict(row)  # type: ignore[arg-type]


def get_user_by_username(username: str) -> dict[str, Any] | None:
    init_db()
    with _LOCK:
        with _connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# OAuth State Store (single-use, short-lived, session-bound)
# ---------------------------------------------------------------------------

def create_oauth_state(state: str, user_id: str, ttl_seconds: int = 600) -> None:
    init_db()
    now = _utc_now()
    expires = now + timedelta(seconds=ttl_seconds)
    with _LOCK:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO oauth_states (state, user_id, expires_at, used, created_at) VALUES (?, ?, ?, 0, ?)",
                (state, user_id, _utc_iso(expires), _utc_iso(now)),
            )


def consume_oauth_state(state: str, user_id: str) -> bool:
    """Atomically validate and mark an OAuth state single-use."""
    init_db()
    now_iso = _utc_iso()
    with _LOCK:
        with _connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM oauth_states WHERE state = ?", (state,)
            ).fetchone()
            if not row:
                return False
            if int(row["used"]) != 0:
                return False
            if row["user_id"] != user_id:
                return False
            if row["expires_at"] < now_iso:
                return False
            conn.execute("UPDATE oauth_states SET used = 1 WHERE state = ?", (state,))
            return True


# ---------------------------------------------------------------------------
# Jobs & Durable Worker Leases
# ---------------------------------------------------------------------------

def _row_to_job(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    d["payload"] = json.loads(d.pop("payload_json") or "{}")
    d["result"] = json.loads(d.pop("result_json")) if d.get("result_json") else None
    d["disclosure"] = json.loads(d.pop("disclosure_json")) if d.get("disclosure_json") else None
    d["provenance"] = json.loads(d.pop("provenance_json")) if d.get("provenance_json") else None
    d["cancel_requested"] = bool(d["cancel_requested"])
    return d


def create_job(
    topic: str,
    fmt: str,
    payload: dict[str, Any],
    max_attempts: int = 3,
    job_id: str | None = None,
    owner_user_id: str | None = None,
) -> dict[str, Any]:
    init_db()
    jid = job_id or uuid.uuid4().hex
    now = _utc_iso()
    with _LOCK:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (id, owner_user_id, status, stage, progress_pct, format, topic, payload_json, attempts, max_attempts, cancel_requested, next_run_at, created_at, updated_at)
                VALUES (?, ?, 'queued', 'queued', 0, ?, ?, ?, 0, ?, 0, ?, ?, ?)
                """,
                (jid, owner_user_id, fmt, topic, json.dumps(payload), max_attempts, now, now, now),
            )
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
    return _row_to_job(row)  # type: ignore[return-value]


def get_job(job_id: str) -> dict[str, Any] | None:
    init_db()
    with _LOCK:
        with _connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_job(row)


def list_jobs(limit: int = 50, owner_user_id: str | None = None) -> list[dict[str, Any]]:
    init_db()
    with _LOCK:
        with _connect() as conn:
            if owner_user_id is not None:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE owner_user_id = ? ORDER BY created_at DESC LIMIT ?",
                    (owner_user_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
    return [_row_to_job(r) for r in rows if r is not None]


def update_job(
    job_id: str,
    *,
    status: str | None = None,
    stage: str | None = None,
    progress_pct: int | None = None,
    result: dict[str, Any] | None = None,
    disclosure: dict[str, Any] | None = None,
    provenance: list[dict[str, Any]] | None = None,
    error: str | None = None,
    attempts: int | None = None,
    cancel_requested: bool | None = None,
    worker_id: str | None = None,
    lease_expires_at: str | None = None,
    next_run_at: str | None = None,
) -> dict[str, Any] | None:
    init_db()
    fields: list[str] = ["updated_at = ?"]
    values: list[Any] = [_utc_iso()]
    if status is not None:
        fields.append("status = ?")
        values.append(status)
    if stage is not None:
        fields.append("stage = ?")
        values.append(stage)
    if progress_pct is not None:
        fields.append("progress_pct = ?")
        values.append(progress_pct)
    if result is not None:
        fields.append("result_json = ?")
        values.append(json.dumps(result))
    if disclosure is not None:
        fields.append("disclosure_json = ?")
        values.append(json.dumps(disclosure))
    if provenance is not None:
        fields.append("provenance_json = ?")
        values.append(json.dumps(provenance))
    if error is not None:
        fields.append("error = ?")
        values.append(error)
    if attempts is not None:
        fields.append("attempts = ?")
        values.append(attempts)
    if cancel_requested is not None:
        fields.append("cancel_requested = ?")
        values.append(1 if cancel_requested else 0)
    if worker_id is not None:
        fields.append("worker_id = ?")
        values.append(worker_id)
    if lease_expires_at is not None:
        fields.append("lease_expires_at = ?")
        values.append(lease_expires_at)
    if next_run_at is not None:
        fields.append("next_run_at = ?")
        values.append(next_run_at)

    values.append(job_id)
    with _LOCK:
        with _connect() as conn:
            conn.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?", values)
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_job(row)


def claim_next_job(worker_id: str, lease_seconds: int = 120) -> dict[str, Any] | None:
    """Atomically claim one eligible queued job with a worker lease."""
    init_db()
    now = _utc_now()
    now_iso = _utc_iso(now)
    lease_iso = _utc_iso(now + timedelta(seconds=lease_seconds))
    with _LOCK:
        with _connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM jobs
                WHERE status = 'queued'
                  AND cancel_requested = 0
                  AND (next_run_at IS NULL OR next_run_at <= ?)
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (now_iso,),
            ).fetchone()
            if not row:
                return None
            jid = row["id"]
            new_attempts = int(row["attempts"]) + 1
            conn.execute(
                """
                UPDATE jobs
                SET status = 'running', stage = 'claimed', attempts = ?, worker_id = ?,
                    lease_expires_at = ?, heartbeat_at = ?, updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (new_attempts, worker_id, lease_iso, now_iso, now_iso, jid),
            )
            claimed = conn.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
    return _row_to_job(claimed)


def renew_job_lease(job_id: str, worker_id: str, lease_seconds: int = 120) -> None:
    init_db()
    now = _utc_now()
    lease_iso = _utc_iso(now + timedelta(seconds=lease_seconds))
    now_iso = _utc_iso(now)
    with _LOCK:
        with _connect() as conn:
            conn.execute(
                """
                UPDATE jobs SET lease_expires_at = ?, heartbeat_at = ?, updated_at = ?
                WHERE id = ? AND worker_id = ? AND status = 'running'
                """,
                (lease_iso, now_iso, now_iso, job_id, worker_id),
            )


def recover_stale_jobs() -> int:
    """Recover interrupted running jobs whose worker lease expired (e.g. after crash/restart)."""
    init_db()
    now_iso = _utc_iso()
    recovered = 0
    with _LOCK:
        with _connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            stale = conn.execute(
                """
                SELECT id, attempts, max_attempts FROM jobs
                WHERE status = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?
                """,
                (now_iso,),
            ).fetchall()
            for r in stale:
                if int(r["attempts"]) >= int(r["max_attempts"]):
                    conn.execute(
                        "UPDATE jobs SET status = 'failed', stage = 'lease_expired', error = 'Worker lease expired after max attempts', updated_at = ? WHERE id = ?",
                        (now_iso, r["id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE jobs SET status = 'queued', stage = 'recovered', worker_id = NULL, lease_expires_at = NULL, next_run_at = ?, updated_at = ? WHERE id = ?",
                        (now_iso, now_iso, r["id"]),
                    )
                recovered += 1
    return recovered


def request_cancel(job_id: str) -> dict[str, Any] | None:
    job = get_job(job_id)
    if not job:
        return None
    if job["status"] in ("completed", "failed", "cancelled"):
        return job
    if job["status"] == "queued":
        return update_job(
            job_id,
            status="cancelled",
            stage="cancelled",
            cancel_requested=True,
            error="Cancelled before execution",
        )
    return update_job(job_id, cancel_requested=True)


def is_cancel_requested(job_id: str) -> bool:
    job = get_job(job_id)
    return bool(job and job.get("cancel_requested"))


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------

def get_asset_by_hash(sha256_hash: str) -> dict[str, Any] | None:
    init_db()
    with _LOCK:
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM assets WHERE sha256_hash = ? ORDER BY created_at DESC LIMIT 1",
                (sha256_hash,),
            ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["provenance"] = json.loads(d.pop("provenance_json") or "{}")
    return d


def register_asset(
    *,
    job_id: str | None,
    asset_type: str,
    file_path: str | Path,
    owner_user_id: str | None = None,
    mime_type: str | None = None,
    width: int | None = None,
    height: int | None = None,
    duration_seconds: float | None = None,
    provenance: dict[str, Any] | None = None,
    deduplicate: bool = True,
) -> dict[str, Any]:
    init_db()
    p = Path(file_path).resolve()
    file_hash = sha256_file(p)
    if deduplicate:
        existing = get_asset_by_hash(file_hash)
        if existing and Path(existing["file_path"]).is_file():
            return {**existing, "deduplicated": True}

    aid = uuid.uuid4().hex
    now = _utc_iso()
    prov = provenance or {"source": "generated", "license": "channel-owned"}
    with _LOCK:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO assets (id, job_id, owner_user_id, asset_type, file_path, sha256_hash, mime_type, width, height, duration_seconds, provenance_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    aid,
                    job_id,
                    owner_user_id,
                    asset_type,
                    str(p),
                    file_hash,
                    mime_type,
                    width,
                    height,
                    duration_seconds,
                    json.dumps(prov),
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM assets WHERE id = ?", (aid,)).fetchone()
    d = dict(row)  # type: ignore[arg-type]
    d["provenance"] = json.loads(d.pop("provenance_json") or "{}")
    d["deduplicated"] = False
    return d


def list_assets(
    job_id: str | None = None,
    owner_user_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    init_db()
    with _LOCK:
        with _connect() as conn:
            if job_id and owner_user_id:
                rows = conn.execute(
                    "SELECT * FROM assets WHERE job_id = ? AND owner_user_id = ? ORDER BY created_at DESC LIMIT ?",
                    (job_id, owner_user_id, limit),
                ).fetchall()
            elif job_id:
                rows = conn.execute(
                    "SELECT * FROM assets WHERE job_id = ? ORDER BY created_at DESC LIMIT ?",
                    (job_id, limit),
                ).fetchall()
            elif owner_user_id:
                rows = conn.execute(
                    "SELECT * FROM assets WHERE owner_user_id = ? ORDER BY created_at DESC LIMIT ?",
                    (owner_user_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM assets ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["provenance"] = json.loads(d.pop("provenance_json") or "{}")
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Publish Records
# ---------------------------------------------------------------------------

def get_publish_by_idempotency(idempotency_key: str) -> dict[str, Any] | None:
    init_db()
    with _LOCK:
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM publish_records WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["qa_passed"] = bool(d["qa_passed"])
    d["synthetic_disclosed"] = bool(d["synthetic_disclosed"])
    d["qa_report"] = json.loads(d.pop("qa_report_json")) if d.get("qa_report_json") else None
    d["disclosure"] = json.loads(d.pop("disclosure_json")) if d.get("disclosure_json") else None
    return d


def upsert_publish_record(
    *,
    job_id: str,
    idempotency_key: str,
    youtube_video_id: str | None,
    privacy_status: str,
    publish_at: str | None,
    qa_passed: bool,
    qa_report: dict[str, Any] | None,
    synthetic_disclosed: bool,
    status: str,
    owner_user_id: str | None = None,
    disclosure: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    init_db()
    now = _utc_iso()
    existing = get_publish_by_idempotency(idempotency_key)
    with _LOCK:
        with _connect() as conn:
            if existing:
                conn.execute(
                    """
                    UPDATE publish_records
                    SET youtube_video_id = ?, privacy_status = ?, publish_at = ?, qa_passed = ?,
                        qa_report_json = ?, synthetic_disclosed = ?, disclosure_json = ?, status = ?, error = ?, updated_at = ?
                    WHERE idempotency_key = ?
                    """,
                    (
                        youtube_video_id,
                        privacy_status,
                        publish_at,
                        1 if qa_passed else 0,
                        json.dumps(qa_report) if qa_report else None,
                        1 if synthetic_disclosed else 0,
                        json.dumps(disclosure) if disclosure else None,
                        status,
                        error,
                        now,
                        idempotency_key,
                    ),
                )
            else:
                pid = uuid.uuid4().hex
                conn.execute(
                    """
                    INSERT INTO publish_records (id, job_id, owner_user_id, idempotency_key, youtube_video_id, privacy_status, publish_at, qa_passed, qa_report_json, synthetic_disclosed, disclosure_json, status, error, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pid,
                        job_id,
                        owner_user_id,
                        idempotency_key,
                        youtube_video_id,
                        privacy_status,
                        publish_at,
                        1 if qa_passed else 0,
                        json.dumps(qa_report) if qa_report else None,
                        1 if synthetic_disclosed else 0,
                        json.dumps(disclosure) if disclosure else None,
                        status,
                        error,
                        now,
                        now,
                    ),
                )
    return get_publish_by_idempotency(idempotency_key)  # type: ignore[return-value]


def list_publish_records(limit: int = 50, owner_user_id: str | None = None) -> list[dict[str, Any]]:
    init_db()
    with _LOCK:
        with _connect() as conn:
            if owner_user_id is not None:
                rows = conn.execute(
                    "SELECT * FROM publish_records WHERE owner_user_id = ? ORDER BY created_at DESC LIMIT ?",
                    (owner_user_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM publish_records ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["qa_passed"] = bool(d["qa_passed"])
        d["synthetic_disclosed"] = bool(d["synthetic_disclosed"])
        d["qa_report"] = json.loads(d.pop("qa_report_json")) if d.get("qa_report_json") else None
        d["disclosure"] = json.loads(d.pop("disclosure_json")) if d.get("disclosure_json") else None
        out.append(d)
    return out
