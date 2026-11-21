"""Faceless AI production adapters and upgraded FFmpeg video renderer."""
from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from .config import settings

_ELEVENLABS_SEM = threading.BoundedSemaphore(max(1, settings.elevenlabs_max_concurrency))
_REPLICATE_SEM = threading.BoundedSemaphore(max(1, settings.replicate_max_concurrency))
_TRUSTED_REPLICATE_POLL_HOSTS = frozenset({"api.replicate.com"})
_TRUSTED_REPLICATE_OUTPUT_HOSTS = frozenset({"replicate.delivery", "pbxt.replicate.delivery"})


def _storage_dirs() -> tuple[Path, Path]:
    root = Path(os.getenv("OUTPUT_DIR", settings.output_dir)).resolve()
    assets = root / "assets"
    renders = root / "renders"
    assets.mkdir(parents=True, exist_ok=True)
    renders.mkdir(parents=True, exist_ok=True)
    return assets, renders


def _bin(name: str) -> str:
    value = shutil.which(name)
    if not value:
        raise RuntimeError(f"{name} is not installed or not on PATH")
    return value


# ---------------------------------------------------------------------------
# Milestone 4: Hinglish pronunciation, voice auth, chunking, rate-limit retry
# ---------------------------------------------------------------------------

_HINGLISH_PRONUNCIATION_MAP: dict[str, str] = {
    r"\bAPI\b": "A.P.I.",
    r"\bAPIs\b": "A.P.I.s",
    r"\bCLI\b": "C.L.I.",
    r"\bUI\b": "U.I.",
    r"\bUX\b": "U.X.",
    r"\bSQL\b": "S.Q.L.",
    r"\bJSON\b": "J-son",
    r"\bFFmpeg\b": "F.F.mpeg",
    r"\bOAuth\b": "O-Auth",
    r"\bvs\b": "versus",
}


def normalize_hinglish_for_tts(text: str) -> tuple[str, list[str]]:
    """Normalize Hinglish/tech text for clearer TTS pronunciation and return warnings."""
    warnings: list[str] = []
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        raise ValueError("Narration text cannot be empty")
    if re.search(r"[\u0900-\u097F]", cleaned):
        warnings.append("Contains Devanagari script; verify multilingual voice pronunciation.")
    for pattern, replacement in _HINGLISH_PRONUNCIATION_MAP.items():
        cleaned = re.sub(pattern, replacement, cleaned)
    return cleaned, warnings


def verify_voice_authorized(voice_id: str) -> None:
    """Fail-closed check: consent must be verified and voice_id must match a non-empty allowlist."""
    if not voice_id or not voice_id.strip():
        raise PermissionError("Voice ID is required.")
    consent_raw = os.getenv("ELEVENLABS_VOICE_CONSENT_VERIFIED")
    if consent_raw is None:
        consent_raw = str(settings.elevenlabs_voice_consent_verified)
    consent_norm = consent_raw.strip().lower()
    if consent_norm not in ("1", "true", "yes"):
        raise PermissionError(
            "Voice generation blocked: ELEVENLABS_VOICE_CONSENT_VERIFIED must be explicitly 'true'."
        )

    auth_raw = os.getenv("ELEVENLABS_AUTHORIZED_VOICE_IDS")
    if auth_raw is None:
        auth_raw = settings.elevenlabs_authorized_voice_ids
    allowed = {v.strip() for v in auth_raw.split(",") if v.strip()}
    if not allowed:
        raise PermissionError(
            "Voice generation blocked: ELEVENLABS_AUTHORIZED_VOICE_IDS allowlist is empty."
        )
    if voice_id.strip() not in allowed:
        raise PermissionError(
            f"Voice ID '{voice_id}' is not in ELEVENLABS_AUTHORIZED_VOICE_IDS."
        )


def chunk_narration(text: str, max_chars: int = 2400) -> list[str]:
    """Split long narration into sentence-aligned chunks under max_chars."""
    cleaned = re.sub(r"\s+", " ", text).strip()
    if len(cleaned) <= max_chars:
        return [cleaned]
    sentences = re.split(r"(?<=[.!?।])\s+", cleaned)
    chunks: list[str] = []
    current = ""
    for sent in sentences:
        if len(sent) > max_chars:
            words = sent.split(" ")
            for w in words:
                if len(current) + len(w) + 1 > max_chars:
                    if current:
                        chunks.append(current.strip())
                    current = w
                else:
                    current = f"{current} {w}".strip()
        elif len(current) + len(sent) + 1 > max_chars:
            if current:
                chunks.append(current.strip())
            current = sent
        else:
            current = f"{current} {sent}".strip()
    if current.strip():
        chunks.append(current.strip())
    return chunks


def _request_with_backoff(
    method: str,
    url: str,
    *,
    max_retries: int = 4,
    base_delay: float = 1.5,
    **kwargs: Any,
) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.request(method, url, **kwargs)
            if resp.status_code in (429, 500, 502, 503, 504):
                if attempt == max_retries:
                    resp.raise_for_status()
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else base_delay * (2**attempt)
                time.sleep(min(delay, 30.0))
                continue
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            if attempt == max_retries:
                raise
            time.sleep(min(base_delay * (2**attempt), 30.0))
    raise RuntimeError(f"Request failed after retries: {last_exc}")


def _concatenate_audio_files(chunk_paths: list[Path], target: Path) -> None:
    if len(chunk_paths) == 1:
        target.write_bytes(chunk_paths[0].read_bytes())
        return
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        concat_list = target.parent / f"audio_concat_{uuid.uuid4().hex}.txt"
        concat_list.write_text(
            "".join(f"file '{p.as_posix()}'\n" for p in chunk_paths),
            encoding="utf-8",
        )
        try:
            subprocess.run(
                [
                    ffmpeg_path,
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(concat_list),
                    "-c:a",
                    "libmp3lame",
                    "-b:a",
                    "192k",
                    str(target),
                ],
                capture_output=True,
                check=True,
            )
            return
        finally:
            concat_list.unlink(missing_ok=True)
    # Fallback: raw MP3 stream concatenation when ffmpeg is unavailable
    with open(target, "wb") as out:
        for p in chunk_paths:
            out.write(p.read_bytes())


def create_voiceover(
    text: str,
    output_name: str = "voiceover.mp3",
    voice_id: str | None = None,
) -> str:
    """ElevenLabs AI voice adapter with chunking, rate-limit backoff, Hinglish normalization, and voice auth."""
    assets, _ = _storage_dirs()
    key = os.getenv("ELEVENLABS_API_KEY", settings.elevenlabs_api_key)
    voice = voice_id or os.getenv("ELEVENLABS_VOICE_ID", settings.elevenlabs_voice_id)
    if not key or not voice:
        raise RuntimeError("Configure ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID")
    verify_voice_authorized(voice)

    normalized_text, _ = normalize_hinglish_for_tts(text)
    chunks = chunk_narration(normalized_text, max_chars=2400)
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice}"
    model_id = os.getenv("ELEVENLABS_MODEL_ID", settings.elevenlabs_model_id)

    chunk_files: list[Path] = []
    try:
        with _ELEVENLABS_SEM:
            for idx, chunk in enumerate(chunks):
                r = _request_with_backoff(
                    "POST",
                    url,
                    headers={
                        "xi-api-key": key,
                        "Content-Type": "application/json",
                        "Accept": "audio/mpeg",
                    },
                    json={
                        "text": chunk,
                        "model_id": model_id,
                        "voice_settings": {"stability": 0.48, "similarity_boost": 0.78},
                    },
                    timeout=180,
                )
                if not r.content or len(r.content) < 100:
                    raise RuntimeError(f"ElevenLabs returned empty/invalid audio for chunk {idx + 1}")
                part = assets / f"tts_chunk_{uuid.uuid4().hex}.mp3"
                part.write_bytes(r.content)
                chunk_files.append(part)

        target = assets / Path(output_name).name
        _concatenate_audio_files(chunk_files, target)
        return str(target)
    finally:
        for p in chunk_files:
            p.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Milestone 3: Replicate image adapter + MIME & dimension validation
# ---------------------------------------------------------------------------

def generate_visual_prompt(topic: str, scene: str, fmt: str) -> str:
    from .ai import generate_text

    ratio = "vertical 9:16" if fmt == "short" else "horizontal 16:9"
    return generate_text(
        f"""Create one detailed original AI image-generation prompt for a faceless tech explainer.
Topic: {topic}. Scene: {scene}. Composition: {ratio}. Cinematic educational visual, clean focal subject.
No logos, watermarks, copyrighted characters or readable text. Return only the prompt."""
    ).strip()


def build_replicate_input(prompt: str, fmt: str = "short", version: str = "", preset: str = "auto") -> dict[str, Any]:
    """Adapt input payload to the selected Replicate model/version schema."""
    effective_preset = preset if preset != "auto" else _detect_replicate_preset(version)
    aspect_ratio = "9:16" if fmt == "short" else "16:9"
    width, height = (1080, 1920) if fmt == "short" else (1920, 1080)

    if effective_preset == "flux":
        return {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "output_format": "png",
            "safety_tolerance": 2,
        }
    if effective_preset == "sdxl":
        return {
            "prompt": prompt,
            "width": 768 if fmt == "short" else 1344,
            "height": 1344 if fmt == "short" else 768,
            "num_outputs": 1,
        }
    if effective_preset == "imagen":
        return {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "output_format": "png",
        }
    return {"prompt": prompt}


def _detect_replicate_preset(version: str) -> str:
    v = version.lower()
    if "flux" in v:
        return "flux"
    if "sdxl" in v or "stable-diffusion" in v:
        return "sdxl"
    if "imagen" in v:
        return "imagen"
    return "prompt_only"


def inspect_image_bytes(data: bytes, content_type: str | None = None) -> tuple[str, int, int]:
    """Validate image magic bytes and extract (mime_type, width, height) in pure Python."""
    if not data or len(data) < 24:
        raise ValueError("Image payload is empty or too small")

    mime: str | None = None
    width = height = 0

    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        mime = "image/png"
        width, height = struct.unpack(">II", data[16:24])
    elif data.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
        width, height = _parse_jpeg_dimensions(data)
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        mime = "image/webp"
        width, height = _parse_webp_dimensions(data)
    else:
        raise ValueError(f"Unsupported or invalid image binary format (header={content_type})")

    if content_type:
        ct_clean = content_type.split(";")[0].strip().lower()
        if ct_clean.startswith("image/") and ct_clean != mime and not (ct_clean == "image/jpg" and mime == "image/jpeg"):
            raise ValueError(f"Image MIME mismatch: header={ct_clean}, magic={mime}")

    if width <= 0 or height <= 0:
        raise ValueError(f"Invalid image dimensions: {width}x{height}")

    return mime, width, height


def _parse_jpeg_dimensions(data: bytes) -> tuple[int, int]:
    idx = 2
    n = len(data)
    while idx + 9 < n:
        if data[idx] != 0xFF:
            idx += 1
            continue
        marker = data[idx + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):
            height, width = struct.unpack(">HH", data[idx + 5 : idx + 9])
            return width, height
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            idx += 2
            continue
        seg_len = struct.unpack(">H", data[idx + 2 : idx + 4])[0]
        if seg_len < 2:
            break
        idx += 2 + seg_len
    return 0, 0


def _parse_webp_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 30:
        return 0, 0
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        w = 1 + int.from_bytes(data[24:27], "little")
        h = 1 + int.from_bytes(data[27:30], "little")
        return w, h
    if chunk == b"VP8 " and len(data) >= 30:
        w, h = struct.unpack("<HH", data[26:30])
        return w & 0x3FFF, h & 0x3FFF
    if chunk == b"VP8L" and len(data) >= 25:
        b0, b1, b2, b3 = data[21:25]
        bits = b0 | (b1 << 8) | (b2 << 16) | (b3 << 24)
        w = (bits & 0x3FFF) + 1
        h = ((bits >> 14) & 0x3FFF) + 1
        return w, h
    return 0, 0


def validate_replicate_poll_url(url: str) -> str:
    """Validate that a Replicate polling URL uses HTTPS and points to a trusted Replicate API host."""
    if not url or not isinstance(url, str):
        raise ValueError("Polling URL is missing or invalid.")
    parsed = urlparse(url.strip())
    if parsed.scheme.lower() != "https":
        raise ValueError(f"Untrusted polling URL scheme '{parsed.scheme}'; HTTPS is required.")
    host = (parsed.hostname or "").lower()
    if host not in _TRUSTED_REPLICATE_POLL_HOSTS:
        raise ValueError(f"Untrusted polling URL host '{host}'; refusing to send bearer token.")
    return url.strip()


def validate_replicate_output_url(url: str) -> str:
    """Validate that a Replicate output download URL uses HTTPS and a trusted Replicate host."""
    if not url or not isinstance(url, str):
        raise ValueError("Output URL is missing or invalid.")
    parsed = urlparse(url.strip())
    if parsed.scheme.lower() != "https":
        raise ValueError(f"Untrusted output URL scheme '{parsed.scheme}'; HTTPS is required.")
    host = (parsed.hostname or "").lower()
    if host not in _TRUSTED_REPLICATE_OUTPUT_HOSTS and not host.endswith(".replicate.delivery"):
        raise ValueError(f"Untrusted Replicate output URL host '{host}'.")
    return url.strip()


def generate_image(
    prompt: str,
    output_name: str = "scene.png",
    fmt: str = "short",
) -> str:
    """Replicate adapter with schema adaptation, SSRF/polling URL validation, and MIME/dimension checks."""
    assets, _ = _storage_dirs()
    token = os.getenv("REPLICATE_API_TOKEN", settings.replicate_api_token)
    version = os.getenv("REPLICATE_MODEL_VERSION", settings.replicate_model_version)
    preset = os.getenv("REPLICATE_SCHEMA_PRESET", settings.replicate_schema_preset)
    if not token or not version:
        raise RuntimeError("Configure REPLICATE_API_TOKEN and REPLICATE_MODEL_VERSION")

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    input_payload = build_replicate_input(prompt, fmt=fmt, version=version, preset=preset)
    with _REPLICATE_SEM:
        r = _request_with_backoff(
            "POST",
            "https://api.replicate.com/v1/predictions",
            headers=headers,
            json={"version": version, "input": input_payload},
            timeout=60,
        )
        prediction = r.json()
        poll = validate_replicate_poll_url(prediction.get("urls", {}).get("get", ""))

        for _ in range(90):
            state = _request_with_backoff(
                "GET", poll, headers={"Authorization": f"Bearer {token}"}, timeout=30
            ).json()
            if state.get("status") == "succeeded":
                out = state.get("output")
                raw_url = out[0] if isinstance(out, list) else out
                safe_url = validate_replicate_output_url(raw_url)
                img = _request_with_backoff("GET", safe_url, timeout=90)
                inspect_image_bytes(img.content, img.headers.get("Content-Type"))
                target = assets / Path(output_name).name
                target.write_bytes(img.content)
                return str(target)
            if state.get("status") in ("failed", "canceled"):
                raise RuntimeError(str(state.get("error")))
            time.sleep(4)
    raise TimeoutError("Image generation timed out")


# ---------------------------------------------------------------------------
# Milestone 5: Upgraded FFmpeg renderer (per-scene durations, xfade, SRT, loudnorm)
# ---------------------------------------------------------------------------

def _format_srt_time(seconds: float) -> str:
    ms_total = max(0, int(round(seconds * 1000)))
    hrs = ms_total // 3_600_000
    mins = (ms_total % 3_600_000) // 60_000
    secs = (ms_total % 60_000) // 1000
    ms = ms_total % 1000
    return f"{hrs:02d}:{mins:02d}:{secs:02d},{ms:03d}"


def write_srt_subtitles(
    captions: list[str],
    durations: list[float],
    target_path: Path,
) -> Path:
    """Write an SRT subtitle file aligned with per-scene durations."""
    lines: list[str] = []
    cursor = 0.0
    idx = 1
    for cap, dur in zip(captions, durations):
        start = cursor
        end = cursor + max(dur, 0.5)
        cursor = end
        text = cap.strip()
        if not text:
            continue
        lines.append(f"{idx}\n{_format_srt_time(start)} --> {_format_srt_time(end)}\n{text}\n")
        idx += 1
    target_path.write_text("\n".join(lines), encoding="utf-8")
    return target_path


def _scale_durations_to_audio(
    scene_durations: list[float] | None,
    scene_count: int,
    audio_duration: float,
) -> list[float]:
    if not scene_durations or len(scene_durations) != scene_count:
        each = max(audio_duration / scene_count, 0.5)
        return [each] * scene_count
    total_requested = sum(max(d, 0.5) for d in scene_durations)
    factor = audio_duration / total_requested if total_requested > 0 else 1.0
    return [max(d * factor, 0.5) for d in scene_durations]


def _escape_ffmpeg_subtitles_path(path: Path) -> str:
    # Escape backslashes and colons for FFmpeg filtergraph on Windows/Linux
    s = path.as_posix()
    return s.replace(":", "\\:")


def render_slideshow(
    image_paths: list[str],
    audio_path: str,
    fmt: str = "short",
    output_name: str = "final.mp4",
    *,
    scene_durations: list[float] | None = None,
    captions: list[str] | None = None,
    transition_seconds: float = 0.35,
    burn_subtitles: bool = True,
    normalize_audio: bool = True,
) -> dict[str, Any]:
    """Assemble scenes with per-scene durations, xfade transitions, SRT burn-in, and loudness normalization."""
    _, renders = _storage_dirs()
    ffmpeg, ffprobe = _bin("ffmpeg"), _bin("ffprobe")
    if not image_paths:
        raise ValueError("At least one scene image is required")
    for path in list(image_paths) + [audio_path]:
        if not Path(path).is_file():
            raise FileNotFoundError(path)

    width, height = (1080, 1920) if fmt == "short" else (1920, 1080)
    p = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            audio_path,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    audio_duration = max(float(p.stdout.strip()), 1.0)
    durations = _scale_durations_to_audio(scene_durations, len(image_paths), audio_duration)

    # Effective transition cannot exceed half of shortest scene
    min_scene = min(durations)
    xfade_dur = min(transition_seconds, max(0.0, min_scene * 0.4)) if len(image_paths) > 1 else 0.0

    # Write subtitles if captions provided
    srt_path: Path | None = None
    if burn_subtitles and captions and len(captions) == len(image_paths) and any(c.strip() for c in captions):
        srt_path = renders / f"subs_{uuid.uuid4().hex}.srt"
        write_srt_subtitles(captions, durations, srt_path)

    target = renders / Path(output_name).name

    # Build FFmpeg filter_complex with per-scene inputs + xfade + optional subtitle burn-in
    cmd: list[str] = [ffmpeg, "-y"]
    filter_parts: list[str] = []

    for i, (img, dur) in enumerate(zip(image_paths, durations)):
        # Add extra overlap duration for xfade on non-last scenes
        clip_dur = dur + (xfade_dur if (xfade_dur > 0 and i < len(image_paths) - 1) else 0.0)
        cmd.extend(["-loop", "1", "-t", f"{clip_dur:.3f}", "-i", img])
        filter_parts.append(
            f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p[v{i}]"
        )

    audio_input_index = len(image_paths)
    cmd.extend(["-i", audio_path])

    if len(image_paths) == 1:
        video_label = "[v0]"
    elif xfade_dur > 0:
        prev_label = "[v0]"
        offset = durations[0] - xfade_dur
        for i in range(1, len(image_paths)):
            out_label = f"[vx{i}]"
            filter_parts.append(
                f"{prev_label}[v{i}]xfade=transition=fade:duration={xfade_dur:.3f}:offset={max(0.0, offset):.3f}{out_label}"
            )
            prev_label = out_label
            offset += durations[i]
        video_label = prev_label
    else:
        concat_inputs = "".join(f"[v{i}]" for i in range(len(image_paths)))
        filter_parts.append(f"{concat_inputs}concat=n={len(image_paths)}:v=1:a=0[vcat]")
        video_label = "[vcat]"

    if srt_path is not None:
        escaped_srt = _escape_ffmpeg_subtitles_path(srt_path)
        if fmt == "short":
            style = "FontSize=18,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=3,Outline=2,MarginV=140,MarginL=60,MarginR=60,Alignment=2"
        else:
            style = "FontSize=20,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=3,Outline=2,MarginV=60,MarginL=80,MarginR=80,Alignment=2"
        filter_parts.append(f"{video_label}subtitles='{escaped_srt}':force_style='{style}'[vfinal]")
        video_label = "[vfinal]"

    audio_label = f"{audio_input_index}:a:0"
    if normalize_audio:
        filter_parts.append(f"[{audio_input_index}:a:0]loudnorm=I=-14:TP=-1.5:LRA=11[afinal]")
        audio_label = "[afinal]"

    cmd.extend(
        [
            "-filter_complex",
            ";".join(filter_parts),
            "-map",
            video_label,
            "-map",
            audio_label,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(target),
        ]
    )

    subprocess.run(cmd, capture_output=True, check=True)

    raw = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,width,height",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(target),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    meta = json.loads(raw.stdout)
    streams = meta.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = any(s.get("codec_type") == "audio" for s in streams)
    expected = (1080, 1920) if fmt == "short" else (1920, 1080)
    passed = (v.get("width"), v.get("height")) == expected and audio

    return {
        "file": str(target),
        "subtitle_file": str(srt_path) if srt_path else None,
        "duration_seconds": float(meta.get("format", {}).get("duration", 0)),
        "width": v.get("width"),
        "height": v.get("height"),
        "has_audio": audio,
        "qa_passed": passed,
        "scene_durations": durations,
        "renderer_mode": "still_image_slideshow",
    }
