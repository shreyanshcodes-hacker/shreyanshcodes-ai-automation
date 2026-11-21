"""Authentication, password hashing, JWT/HMAC sessions, CSRF, rate limiting, and safe path resolution."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException, Request

from . import db
from .config import settings

SESSION_COOKIE_NAME = "sc_session"
CSRF_COOKIE_NAME = "sc_csrf"


# ---------------------------------------------------------------------------
# Password hashing (salted scrypt)
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters long.")
    salt = os.urandom(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(derived).decode()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algo, salt_b64, dk_b64 = stored_hash.split("$", 2)
        if algo != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=len(expected))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Signed JWT-compatible HMAC-SHA256 tokens
# ---------------------------------------------------------------------------

def _b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(text: str) -> bytes:
    pad = "=" * ((4 - len(text) % 4) % 4)
    return base64.urlsafe_b64decode(text + pad)


def create_access_token(user_id: str, username: str, role: str, ttl_seconds: int | None = None) -> str:
    now = int(time.time())
    ttl = ttl_seconds or settings.session_ttl_seconds
    header = _b64u_encode(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64u_encode(
        json.dumps(
            {
                "sub": user_id,
                "username": username,
                "role": role,
                "iat": now,
                "exp": now + ttl,
                "jti": secrets.token_hex(12),
            },
            separators=(",", ":"),
        ).encode()
    )
    signing_input = f"{header}.{payload}".encode("ascii")
    sig = hmac.new(settings.auth_secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64u_encode(sig)}"


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError("Malformed token")
        header_b, payload_b, sig_b = parts
        signing_input = f"{header_b}.{payload_b}".encode("ascii")
        expected_sig = hmac.new(
            settings.auth_secret.encode("utf-8"), signing_input, hashlib.sha256
        ).digest()
        actual_sig = _b64u_decode(sig_b)
        if not hmac.compare_digest(actual_sig, expected_sig):
            raise ValueError("Invalid token signature")
        payload = json.loads(_b64u_decode(payload_b).decode("utf-8"))
        if int(payload.get("exp", 0)) < int(time.time()):
            raise ValueError("Token expired")
        return payload
    except Exception as exc:
        raise HTTPException(401, f"Invalid or expired authentication token: {exc}") from exc


# ---------------------------------------------------------------------------
# Request authentication, CSRF, and role/ownership checks
# ---------------------------------------------------------------------------

def get_current_user(request: Request) -> dict[str, Any]:
    # Explicit development-only bypass (impossible in production)
    if (
        settings.app_env == "development"
        and settings.allow_dev_auth_bypass
        and request.headers.get("X-Dev-User")
    ):
        return {
            "id": request.headers["X-Dev-User"],
            "username": request.headers["X-Dev-User"],
            "role": "owner",
            "auth_mode": "dev_bypass",
        }

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        payload = decode_access_token(token)
        return {
            "id": payload["sub"],
            "username": payload["username"],
            "role": payload.get("role", "user"),
            "auth_mode": "bearer",
        }

    cookie_token = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_token:
        # Enforce CSRF on state-changing cookie-authenticated requests
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            csrf_cookie = request.cookies.get(CSRF_COOKIE_NAME, "")
            csrf_header = request.headers.get("X-CSRF-Token", "")
            if not csrf_cookie or not csrf_header or not hmac.compare_digest(csrf_cookie, csrf_header):
                raise HTTPException(403, "CSRF token missing or invalid")
        payload = decode_access_token(cookie_token)
        return {
            "id": payload["sub"],
            "username": payload["username"],
            "role": payload.get("role", "user"),
            "auth_mode": "cookie",
        }

    raise HTTPException(401, "Authentication required")


def require_owner_or_admin(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    if user.get("role") not in ("owner", "admin"):
        raise HTTPException(403, "Owner or admin role required")
    return user


def assert_job_access(job: dict[str, Any] | None, user: dict[str, Any]) -> dict[str, Any]:
    if not job:
        raise HTTPException(404, "Job not found")
    if user.get("role") in ("owner", "admin"):
        return job
    if job.get("owner_user_id") != user.get("id"):
        raise HTTPException(403, "Not authorized to access this job")
    return job


# ---------------------------------------------------------------------------
# Sliding-window rate limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, limit: int, window_seconds: int = 60) -> None:
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            q = self._hits[key]
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= limit:
                raise HTTPException(429, "Rate limit exceeded; please retry later.")
            q.append(now)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


rate_limiter = RateLimiter()


def rate_limit(scope: str, limit: int = 20, window_seconds: int = 60):
    def _dep(request: Request, user: dict[str, Any] = Depends(get_current_user)) -> None:
        key = f"{scope}:{user['id']}"
        rate_limiter.check(key, limit=limit, window_seconds=window_seconds)
    return _dep


# ---------------------------------------------------------------------------
# Safe storage path resolution (prevents ../ traversal & arbitrary FS reads/writes)
# ---------------------------------------------------------------------------

def resolve_safe_storage_path(
    raw_path: str,
    *,
    allowed_exts: set[str] | None = None,
    must_exist: bool = False,
    max_bytes: int | None = None,
) -> Path:
    if not raw_path or "\x00" in raw_path:
        raise HTTPException(400, "Invalid file path")

    storage_root = settings.storage_root
    storage_root.mkdir(parents=True, exist_ok=True)

    candidate = Path(raw_path)
    if not candidate.is_absolute():
        resolved = (storage_root / candidate).resolve()
    else:
        resolved = candidate.resolve()

    try:
        resolved.relative_to(storage_root)
    except ValueError as exc:
        raise HTTPException(403, "Path escapes allowed storage root") from exc

    if allowed_exts and resolved.suffix.lower() not in allowed_exts:
        raise HTTPException(400, f"Unsupported file extension: {resolved.suffix}")

    if must_exist:
        if not resolved.is_file():
            raise HTTPException(404, "File not found")
        if max_bytes is not None and resolved.stat().st_size > max_bytes:
            raise HTTPException(400, "File exceeds maximum allowed size")

    return resolved
