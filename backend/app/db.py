"""SQLite persistence for async jobs, asset provenance/deduplication, and YouTube publish records."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import settings

_LOCK = threading.RLock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path() -> Path:
    path = Path(settings.database_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db() -> None:
    with _LOCK:
        with _connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress_pct INTEGER NOT NULL DEFAULT 0,
                    format TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY,
                    job_id TEXT,
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
                    idempotency_key TEXT NOT NULL UNIQUE,
                    youtube_video_id TEXT,
                    privacy_status TEXT NOT NULL,
                    publish_at TEXT,
                    qa_passed INTEGER NOT NULL DEFAULT 0,
                    qa_report_json TEXT,
                    synthetic_disclosed INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )


def sha256_file(file_path: str | Path) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _row_to_job(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    d["payload"] = json.loads(d.pop("payload_json") or "{}")
    d["result"] = json.loads(d.pop("result_json")) if d.get("result_json") else None
    d["cancel_requested"] = bool(d["cancel_requested"])
    return d


def create_job(
    topic: str,
    fmt: str,
    payload: dict[str, Any],
    max_attempts: int = 3,
    job_id: str | None = None,
) -> dict[str, Any]:
    init_db()
    jid = job_id or uuid.uuid4().hex
    now = _utc_now()
    with _LOCK:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (id, status, stage, progress_pct, format, topic, payload_json, attempts, max_attempts, cancel_requested, created_at, updated_at)
                VALUES (?, 'queued', 'queued', 0, ?, ?, ?, 0, ?, 0, ?, ?)
                """,
                (jid, fmt, topic, json.dumps(payload), max_attempts, now, now),
            )
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
    return _row_to_job(row)  # type: ignore[return-value]


def get_job(job_id: str) -> dict[str, Any] | None:
    init_db()
    with _LOCK:
        with _connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_job(row)


def list_jobs(limit: int = 50) -> list[dict[str, Any]]:
    init_db()
    with _LOCK:
        with _connect() as conn:
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
    error: str | None = None,
    attempts: int | None = None,
    cancel_requested: bool | None = None,
) -> dict[str, Any] | None:
    init_db()
    fields: list[str] = ["updated_at = ?"]
    values: list[Any] = [_utc_now()]
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
    if error is not None:
        fields.append("error = ?")
        values.append(error)
    if attempts is not None:
        fields.append("attempts = ?")
        values.append(attempts)
    if cancel_requested is not None:
        fields.append("cancel_requested = ?")
        values.append(1 if cancel_requested else 0)

    values.append(job_id)
    with _LOCK:
        with _connect() as conn:
            conn.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?", values)
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_job(row)


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
    now = _utc_now()
    prov = provenance or {"source": "generated", "license": "channel-owned"}
    with _LOCK:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO assets (id, job_id, asset_type, file_path, sha256_hash, mime_type, width, height, duration_seconds, provenance_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    aid,
                    job_id,
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


def list_assets(job_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    init_db()
    with _LOCK:
        with _connect() as conn:
            if job_id:
                rows = conn.execute(
                    "SELECT * FROM assets WHERE job_id = ? ORDER BY created_at DESC LIMIT ?",
                    (job_id, limit),
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
    error: str | None = None,
) -> dict[str, Any]:
    init_db()
    now = _utc_now()
    existing = get_publish_by_idempotency(idempotency_key)
    with _LOCK:
        with _connect() as conn:
            if existing:
                conn.execute(
                    """
                    UPDATE publish_records
                    SET youtube_video_id = ?, privacy_status = ?, publish_at = ?, qa_passed = ?,
                        qa_report_json = ?, synthetic_disclosed = ?, status = ?, error = ?, updated_at = ?
                    WHERE idempotency_key = ?
                    """,
                    (
                        youtube_video_id,
                        privacy_status,
                        publish_at,
                        1 if qa_passed else 0,
                        json.dumps(qa_report) if qa_report else None,
                        1 if synthetic_disclosed else 0,
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
                    INSERT INTO publish_records (id, job_id, idempotency_key, youtube_video_id, privacy_status, publish_at, qa_passed, qa_report_json, synthetic_disclosed, status, error, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pid,
                        job_id,
                        idempotency_key,
                        youtube_video_id,
                        privacy_status,
                        publish_at,
                        1 if qa_passed else 0,
                        json.dumps(qa_report) if qa_report else None,
                        1 if synthetic_disclosed else 0,
                        status,
                        error,
                        now,
                        now,
                    ),
                )
    return get_publish_by_idempotency(idempotency_key)  # type: ignore[return-value]


def list_publish_records(limit: int = 50) -> list[dict[str, Any]]:
    init_db()
    with _LOCK:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT * FROM publish_records ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["qa_passed"] = bool(d["qa_passed"])
        d["synthetic_disclosed"] = bool(d["synthetic_disclosed"])
        d["qa_report"] = json.loads(d.pop("qa_report_json")) if d.get("qa_report_json") else None
        out.append(d)
    return out
