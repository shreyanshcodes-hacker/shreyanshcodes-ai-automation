import json
import struct
from pathlib import Path
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient

from app import db, orchestrator, qa, youtube
from app.main import app
from app.schemas import SceneItem, ScenePlan


def _png_bytes(w=1080, h=1920):
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0) + b"\x00\x00\x00\x00"


def _sample_plan(facts_to_verify=None):
    return ScenePlan(
        title="Test Video",
        topic="Python Async",
        format="short",
        target_duration_seconds=30.0,
        synthetic_disclosure=True,
        facts_to_verify=facts_to_verify or [],
        scenes=[
            SceneItem(
                scene_index=1,
                narration="Scene one narration.",
                visual_prompt="Scene one prompt",
                caption_text="Scene 1",
                scene_duration=15.0,
                source_notes="Python official docs",
            ),
            SceneItem(
                scene_index=2,
                narration="Scene two narration.",
                visual_prompt="Scene two prompt",
                caption_text="Scene 2",
                scene_duration=15.0,
                source_notes="Original conceptual explanation",
            ),
        ],
    )


def test_asset_deduplication_and_job_cancellation(tmp_path):
    f1 = tmp_path / "a.png"
    f2 = tmp_path / "b.png"
    data = _png_bytes()
    f1.write_bytes(data)
    f2.write_bytes(data)

    a1 = db.register_asset(job_id="j1", asset_type="image", file_path=f1)
    a2 = db.register_asset(job_id="j2", asset_type="image", file_path=f2)
    assert a1["deduplicated"] is False
    assert a2["deduplicated"] is True
    assert a2["id"] == a1["id"]

    job = db.create_job("Topic", "short", {"topic": "Topic", "format": "short"})
    cancelled = db.request_cancel(job["id"])
    assert cancelled["status"] == "cancelled"


def test_qa_checks_pass_and_fail(tmp_path, monkeypatch):
    video = tmp_path / "vid.mp4"
    video.write_bytes(b"fake-mp4-bytes")
    srt = tmp_path / "subs.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:15,000\nShort caption\n", encoding="utf-8")

    monkeypatch.setattr(qa, "_bin", lambda name: name)

    def fake_run(cmd, capture_output=True, text=True, check=False):
        if cmd[0] == "ffprobe":
            return SimpleNamespace(
                stdout=json.dumps(
                    {
                        "streams": [
                            {"codec_type": "video", "width": 1080, "height": 1920},
                            {"codec_type": "audio", "sample_rate": "44100"},
                        ],
                        "format": {"duration": "30.0"},
                    }
                ),
                stderr="",
            )
        if "blackdetect" in " ".join(cmd):
            return SimpleNamespace(stdout="", stderr="")
        return SimpleNamespace(stdout="", stderr="max_volume: -1.5 dB\nmean_volume: -14.2 dB\n")

    monkeypatch.setattr(qa.subprocess, "run", fake_run)
    res_ok = qa.run_qa_checks(
        video_path=video,
        fmt="short",
        scene_plan=_sample_plan(),
        subtitle_path=srt,
        asset_ids=["asset1"],
        check_duplicate_hash=False,
    )
    assert res_ok.passed is True

    res_fail = qa.run_qa_checks(
        video_path=video,
        fmt="short",
        scene_plan=_sample_plan(facts_to_verify=["Unverified stat 99%"]),
        subtitle_path=srt,
        asset_ids=["asset1"],
        check_duplicate_hash=False,
    )
    assert res_fail.passed is False
    assert res_fail.public_schedule_allowed is False
    assert res_fail.private_preview_allowed is True
    assert any("facts_to_verify" in w for w in res_fail.warnings)


def test_youtube_upload_safety_gates_and_idempotency(tmp_path, monkeypatch):
    video = tmp_path / "final.mp4"
    video.write_bytes(b"video-content")
    job = db.create_job("Topic", "short", {"topic": "Topic", "format": "short"})
    db.update_job(
        job["id"],
        status="completed",
        stage="completed",
        progress_pct=100,
        result={
            "render": {"file": str(video)},
            "qa_report": {"passed": True, "checks": {"factual_sources_present": True}},
        },
    )

    monkeypatch.setattr(youtube, "_get_valid_access_token", lambda: "fake-token")
    captured_metadata = {}

    api_response_status = {"containsSyntheticMedia": False}

    def fake_upload(*, access_token, video_path, metadata, chunk_size=8*1024*1024):
        captured_metadata.update(metadata)
        return {"id": "yt_abc123", "status": dict(api_response_status)}

    monkeypatch.setattr(youtube, "_resumable_upload", fake_upload)

    # 1. Public scheduling disabled & YouTube API does not confirm disclosure -> kept private & synthetic_disclosed=False
    monkeypatch.setenv("YOUTUBE_PUBLIC_SCHEDULING_ENABLED", "false")
    r1 = youtube.upload_job_video(
        job_id=job["id"],
        title="My Short",
        publish_at="2026-11-25T10:00:00Z",
        idempotency_key="idem-1",
    )
    assert r1["privacy_status"] == "private"
    assert r1["publish_at"] is None
    assert captured_metadata["status"]["containsSyntheticMedia"] is True
    assert r1["synthetic_disclosed"] is False
    assert r1["synthetic_disclosure_confirmed"] is False
    assert r1["disclosure_warning"] is not None

    # 2. Idempotent replay returns existing record without re-uploading
    r2 = youtube.upload_job_video(
        job_id=job["id"],
        title="My Short",
        publish_at="2026-11-25T10:00:00Z",
        idempotency_key="idem-1",
    )
    assert r2["idempotent_replay"] is True

    # 3. Owner enables public scheduling, QA passes, and YouTube API confirms disclosure -> publishAt & synthetic_disclosed=True
    api_response_status["containsSyntheticMedia"] = True
    monkeypatch.setenv("YOUTUBE_PUBLIC_SCHEDULING_ENABLED", "true")
    r3 = youtube.upload_job_video(
        job_id=job["id"],
        title="My Short",
        publish_at="2026-11-25T10:00:00Z",
        idempotency_key="idem-2",
    )
    assert r3["publish_at"] == "2026-11-25T10:00:00Z"
    assert r3["synthetic_disclosed"] is True
    assert r3["synthetic_disclosure_confirmed"] is True
    assert r3["disclosure_warning"] is None

    # 4. Kill switch blocks upload
    youtube.set_kill_switch(True)
    with pytest.raises(PermissionError, match="kill switch"):
        youtube.upload_job_video(job_id=job["id"], title="Blocked", idempotency_key="idem-3")


def test_api_endpoints_and_preview_download(tmp_path):
    client = TestClient(app)
    h = client.get("/health")
    assert h.status_code == 200
    assert h.json()["status"] == "ok"

    reg = client.post("/api/auth/register", json={"username": "owner1", "password": "StrongPassword123!"})
    assert reg.status_code == 200
    token = reg.json()["access_token"]
    owner_id = reg.json()["user"]["id"]
    headers = {"Authorization": f"Bearer {token}"}

    video = tmp_path / "storage" / "renders" / "preview.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"mp4-stream-bytes")
    job = db.create_job("Preview Topic", "short", {"topic": "Preview Topic"}, owner_user_id=owner_id)
    db.update_job(
        job["id"],
        status="completed",
        stage="completed",
        progress_pct=100,
        result={"render": {"file": str(video)}, "qa_report": {"passed": True}},
    )

    prev = client.get(f"/api/jobs/{job['id']}/preview", headers=headers)
    assert prev.status_code == 200
    assert prev.content == b"mp4-stream-bytes"

    dl = client.get(f"/api/jobs/{job['id']}/download", headers=headers)
    assert dl.status_code == 200
    assert "attachment" in dl.headers.get("content-disposition", "")

    ks = client.post("/api/youtube/kill-switch", json={"enabled": True}, headers=headers)
    assert ks.status_code == 200
    assert ks.json()["kill_switch"] is True
