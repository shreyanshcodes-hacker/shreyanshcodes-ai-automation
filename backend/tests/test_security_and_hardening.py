from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient

from app import config, db, qa, youtube
from app.main import app
from app.schemas import ClaimProvenance, SceneItem, ScenePlan
from app.security import rate_limiter, resolve_safe_storage_path


def test_auth_csrf_user_isolation_and_rate_limit():
    client = TestClient(app)

    # Unauthenticated access rejected
    assert client.get("/api/jobs").status_code == 401
    assert client.get("/api/settings").status_code == 401

    # Register first user -> owner
    r_owner = client.post("/api/auth/register", json={"username": "owner_u", "password": "Password123!"})
    assert r_owner.status_code == 200
    owner_token = r_owner.json()["access_token"]

    # Register second user -> regular user
    r_user = client.post("/api/auth/register", json={"username": "regular_u", "password": "Password123!"})
    assert r_user.status_code == 200
    user_token = r_user.json()["access_token"]

    # Non-owner cannot access owner-only endpoints
    assert client.get("/api/settings", headers={"Authorization": f"Bearer {user_token}"}).status_code == 403
    assert (
        client.post(
            "/api/youtube/kill-switch",
            json={"enabled": True},
            headers={"Authorization": f"Bearer {user_token}"},
        ).status_code
        == 403
    )

    # Owner creates a job; regular user cannot read/cancel it
    j = client.post(
        "/api/jobs",
        json={"topic": "Owner secret topic", "format": "short"},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert j.status_code == 200
    job_id = j.json()["id"]
    assert client.get(f"/api/jobs/{job_id}", headers={"Authorization": f"Bearer {user_token}"}).status_code == 403
    assert client.post(f"/api/jobs/{job_id}/cancel", headers={"Authorization": f"Bearer {user_token}"}).status_code == 403

    # Cookie auth requires CSRF token on state-changing requests
    login_res = client.post("/api/auth/login", json={"username": "regular_u", "password": "Password123!"})
    csrf = login_res.json()["csrf_token"]
    # Without X-CSRF-Token -> 403
    client.cookies.set("sc_session", login_res.json()["access_token"])
    client.cookies.set("sc_csrf", csrf)
    assert client.post("/api/jobs", json={"topic": "No CSRF", "format": "short"}).status_code == 403
    # With X-CSRF-Token -> 200
    ok_csrf = client.post(
        "/api/jobs",
        json={"topic": "With CSRF", "format": "short"},
        headers={"X-CSRF-Token": csrf},
    )
    assert ok_csrf.status_code == 200

    # Rate limit check
    rate_limiter.reset()
    for _ in range(15):
        rate_limiter.check("test_scope:u1", limit=15, window_seconds=60)
    with pytest.raises(Exception) as exc:
        rate_limiter.check("test_scope:u1", limit=15, window_seconds=60)
    assert "429" in str(exc.value)


def test_oauth_state_security_single_use_and_session_bound(monkeypatch):
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "cid")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "csec")
    monkeypatch.setattr(
        youtube.requests,
        "post",
        lambda *a, **kw: SimpleNamespace(
            status_code=200,
            raise_for_status=lambda: None,
            json=lambda: {"access_token": "tok", "refresh_token": "ref", "expires_in": 3600},
        ),
    )

    url = youtube.get_oauth_authorization_url(user_id="user_1")
    state = url.split("state=")[1].split("&")[0]
    assert state != "studio"
    assert len(state) >= 20

    # Wrong user cannot consume state
    with pytest.raises(PermissionError, match="OAuth state"):
        youtube.exchange_oauth_code(code="code123", state=state, user_id="user_2")

    # Valid user consumes state once
    res = youtube.exchange_oauth_code(code="code123", state=state, user_id="user_1")
    assert res["connected"] is True

    # Reusing same state fails
    with pytest.raises(PermissionError, match="OAuth state"):
        youtube.exchange_oauth_code(code="code123", state=state, user_id="user_1")


def test_path_traversal_and_absolute_path_blocked(tmp_path):
    with pytest.raises(Exception) as e1:
        resolve_safe_storage_path("../../etc/passwd")
    assert "403" in str(e1.value)

    with pytest.raises(Exception) as e2:
        resolve_safe_storage_path(str(tmp_path / "outside.mp4"))
    assert "403" in str(e2.value)


def test_cors_and_production_config_validation():
    bad_cors = config.Settings(frontend_origins="*")
    with pytest.raises(RuntimeError, match="Wildcard"):
        config.validate_cors_config(bad_cors)

    bad_prod = config.Settings(app_env="production", frontend_origins="http://localhost:5173")
    with pytest.raises(RuntimeError, match="HTTPS"):
        config.validate_production_settings(bad_prod)


def test_durable_job_claim_and_crash_recovery():
    j1 = db.create_job("Topic 1", "short", {"topic": "Topic 1"})
    claimed_a = db.claim_next_job("worker-a", lease_seconds=60)
    claimed_b = db.claim_next_job("worker-b", lease_seconds=60)
    assert claimed_a is not None and claimed_a["id"] == j1["id"]
    assert claimed_b is None

    # Simulate worker crash with expired lease
    expired = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    db.update_job(j1["id"], lease_expires_at=expired)
    recovered = db.recover_stale_jobs()
    assert recovered == 1
    after = db.get_job(j1["id"])
    assert after["status"] == "queued"


def test_provenance_pending_vs_verified_gates_public_scheduling(tmp_path, monkeypatch):
    video = tmp_path / "storage" / "renders" / "v.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"mp4")
    monkeypatch.setattr(qa, "_bin", lambda n: n)
    monkeypatch.setattr(
        qa.subprocess,
        "run",
        lambda cmd, **kw: SimpleNamespace(
            stdout='{"streams":[{"codec_type":"video","width":1080,"height":1920},{"codec_type":"audio","sample_rate":"44100"}],"format":{"duration":"30.0"}}'
            if cmd[0] == "ffprobe"
            else "",
            stderr="" if "blackdetect" in " ".join(cmd) else "max_volume: -2.0 dB\nmean_volume: -14.0 dB\n",
        ),
    )
    plan = ScenePlan(
        title="Test",
        topic="AI",
        format="short",
        target_duration_seconds=30.0,
        scenes=[
            SceneItem(
                scene_index=1,
                narration="Test narration.",
                visual_prompt="Test prompt",
                caption_text="Test",
                scene_duration=30.0,
                source_notes="Official docs",
            )
        ],
        provenance_records=[
            ClaimProvenance(
                claim_text="Python 3.13 released",
                source_title="Python.org",
                source_url="https://docs.python.org/3/",
                retrieved_at="2026-11-21T00:00:00Z",
                supporting_excerpt="Python 3.13 release notes.",
                verification_status="pending_review",
            )
        ],
    )
    res = qa.run_qa_checks(
        video_path=video, fmt="short", scene_plan=plan, asset_ids=["a1"], check_duplicate_hash=False
    )
    assert res.private_preview_allowed is True
    assert res.public_schedule_allowed is False
