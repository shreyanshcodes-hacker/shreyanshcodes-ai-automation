import json
import struct
from pathlib import Path
from types import SimpleNamespace
import pytest

from app import production


def _make_png(width: int, height: int) -> bytes:
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_len = struct.pack(">I", 13)
    ihdr_type = b"IHDR"
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return sig + ihdr_len + ihdr_type + ihdr_data + b"\x00\x00\x00\x00"


def test_replicate_input_schema_presets():
    flux_in = production.build_replicate_input("a cat", fmt="short", version="black-forest-labs/flux-schnell")
    assert flux_in["aspect_ratio"] == "9:16"
    assert flux_in["output_format"] == "png"

    sdxl_in = production.build_replicate_input("a cat", fmt="long", version="stability-ai/sdxl:123")
    assert sdxl_in["width"] == 1344 and sdxl_in["height"] == 768

    generic_in = production.build_replicate_input("a cat", fmt="short", version="custom-v1")
    assert generic_in == {"prompt": "a cat"}


def test_inspect_image_bytes_valid_and_invalid():
    png = _make_png(1080, 1920)
    mime, w, h = production.inspect_image_bytes(png, "image/png")
    assert (mime, w, h) == ("image/png", 1080, 1920)

    with pytest.raises(ValueError, match="MIME mismatch"):
        production.inspect_image_bytes(png, "image/jpeg")

    with pytest.raises(ValueError, match="Unsupported"):
        production.inspect_image_bytes(b"not-an-image-at-all-1234567890")


def test_hinglish_normalization_and_chunking():
    norm, warnings = production.normalize_hinglish_for_tts("FastAPI aur SQL API vs CLI")
    assert "A.P.I." in norm
    assert "S.Q.L." in norm
    assert "versus" in norm
    assert warnings == []

    long_text = " ".join(f"Sentence number {i} hai ye." for i in range(120))
    chunks = production.chunk_narration(long_text, max_chars=300)
    assert len(chunks) > 3
    assert all(len(c) <= 300 for c in chunks)


def test_voice_authorization_and_retry(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "voice_unauthorized")

    # 1. Missing / false consent -> fail closed
    monkeypatch.setenv("ELEVENLABS_VOICE_CONSENT_VERIFIED", "false")
    monkeypatch.setenv("ELEVENLABS_AUTHORIZED_VOICE_IDS", "voice_ok")
    with pytest.raises(PermissionError, match="ELEVENLABS_VOICE_CONSENT_VERIFIED"):
        production.create_voiceover("Hello Hinglish")

    # 2. Invalid consent value -> fail closed
    monkeypatch.setenv("ELEVENLABS_VOICE_CONSENT_VERIFIED", "maybe")
    with pytest.raises(PermissionError, match="ELEVENLABS_VOICE_CONSENT_VERIFIED"):
        production.create_voiceover("Hello Hinglish")

    # 3. Empty allowlist -> fail closed (must NEVER disable authorization)
    monkeypatch.setenv("ELEVENLABS_VOICE_CONSENT_VERIFIED", "true")
    monkeypatch.setenv("ELEVENLABS_AUTHORIZED_VOICE_IDS", "")
    with pytest.raises(PermissionError, match="allowlist is empty"):
        production.create_voiceover("Hello Hinglish")

    # 4. Unauthorized voice -> rejected
    monkeypatch.setenv("ELEVENLABS_AUTHORIZED_VOICE_IDS", "voice_ok")
    with pytest.raises(PermissionError, match="not in ELEVENLABS_AUTHORIZED_VOICE_IDS"):
        production.create_voiceover("Hello Hinglish")

    # 5. Authorized voice + consent -> succeeds with 429 retry
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "voice_ok")
    calls = {"n": 0}

    def fake_req(method, url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return SimpleNamespace(status_code=429, headers={"Retry-After": "0"}, content=b"")
        return SimpleNamespace(status_code=200, headers={}, content=b"ID3" + b"\x00" * 200, raise_for_status=lambda: None)

    monkeypatch.setattr(production.requests, "request", fake_req)
    monkeypatch.setattr(production.time, "sleep", lambda _s: None)
    out = production.create_voiceover("Namaste developers!", output_name="test_vo.mp3")
    assert Path(out).is_file()
    assert calls["n"] == 2


def test_replicate_polling_url_validation():
    assert (
        production.validate_replicate_poll_url("https://api.replicate.com/v1/predictions/abc")
        == "https://api.replicate.com/v1/predictions/abc"
    )
    with pytest.raises(ValueError, match="HTTPS"):
        production.validate_replicate_poll_url("http://api.replicate.com/v1/predictions/abc")
    with pytest.raises(ValueError, match="Untrusted polling URL host"):
        production.validate_replicate_poll_url("https://evil.example.com/steal-token")


def test_render_slideshow_builds_ffmpeg_pipeline(tmp_path, monkeypatch):
    img1 = tmp_path / "s1.png"
    img2 = tmp_path / "s2.png"
    audio = tmp_path / "vo.mp3"
    img1.write_bytes(_make_png(1080, 1920))
    img2.write_bytes(_make_png(1080, 1920))
    audio.write_bytes(b"ID3" + b"\x00" * 200)

    monkeypatch.setattr(production, "_bin", lambda name: name)
    cmds = []

    def fake_run(cmd, capture_output=True, text=False, check=True):
        cmds.append(cmd)
        if cmd[0] == "ffprobe" and "format=duration" in cmd and "-of" in cmd and "json" not in cmd:
            return SimpleNamespace(stdout="20.0\n", stderr="")
        if cmd[0] == "ffprobe" and "json" in cmd:
            return SimpleNamespace(
                stdout=json.dumps(
                    {
                        "streams": [
                            {"codec_type": "video", "width": 1080, "height": 1920},
                            {"codec_type": "audio", "sample_rate": "44100"},
                        ],
                        "format": {"duration": "20.0"},
                    }
                ),
                stderr="",
            )
        # Create dummy output file for ffmpeg
        Path(cmd[-1]).write_bytes(b"mp4data")
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(production.subprocess, "run", fake_run)
    res = production.render_slideshow(
        [str(img1), str(img2)],
        str(audio),
        fmt="short",
        output_name="out.mp4",
        scene_durations=[5.0, 15.0],
        captions=["Scene 1", "Scene 2"],
    )
    assert res["qa_passed"] is True
    assert res["width"] == 1080 and res["height"] == 1920
    assert Path(res["subtitle_file"]).is_file()
    ffmpeg_cmd = " ".join(cmds[1])
    assert "xfade=transition=fade" in ffmpeg_cmd
    assert "loudnorm=" in ffmpeg_cmd
    assert "subtitles=" in ffmpeg_cmd
