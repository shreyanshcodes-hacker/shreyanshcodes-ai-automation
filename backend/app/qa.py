"""Comprehensive video, audio, subtitle, duplicate-hash, and provenance QA checks (Milestone 8)."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import db
from .schemas import QACheckResult, ScenePlan


def _bin(name: str) -> str:
    val = shutil.which(name)
    if not val:
        raise RuntimeError(f"{name} is not installed or not on PATH")
    return val


def check_subtitle_safe_margins(srt_path: Path, fmt: str = "short") -> tuple[bool, dict[str, Any]]:
    """Verify subtitle file is readable, non-empty, and respects line-length/safe-margin bounds."""
    if not srt_path.is_file():
        return False, {"error": f"Subtitle file not found: {srt_path}"}
    text = srt_path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return False, {"error": "Subtitle file is empty"}

    max_line_chars = 42 if fmt == "short" else 72
    too_long: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        lines = block.splitlines()
        if len(lines) >= 3:
            caption_lines = lines[2:]
            for line in caption_lines:
                if len(line.strip()) > max_line_chars:
                    too_long.append(line.strip()[:50])

    return len(too_long) == 0, {
        "max_line_chars": max_line_chars,
        "violating_lines": too_long,
    }


def _detect_black_frames(ffmpeg: str, video_path: Path) -> tuple[bool, dict[str, Any]]:
    proc = subprocess.run(
        [
            ffmpeg,
            "-i",
            str(video_path),
            "-vf",
            "blackdetect=d=0.5:pix_th=0.10",
            "-an",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
    )
    matches = re.findall(r"black_duration:([0-9.]+)", proc.stderr or "")
    durations = [float(m) for m in matches]
    return len(durations) == 0, {"black_segments": durations}


def _detect_audio_loudness_and_clipping(
    ffmpeg: str, video_path: Path
) -> tuple[bool, dict[str, Any]]:
    proc = subprocess.run(
        [
            ffmpeg,
            "-i",
            str(video_path),
            "-af",
            "volumedetect",
            "-vn",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
    )
    stderr = proc.stderr or ""
    max_match = re.search(r"max_volume:\s*(-?[0-9.]+)\s*dB", stderr)
    mean_match = re.search(r"mean_volume:\s*(-?[0-9.]+)\s*dB", stderr)
    max_vol = float(max_match.group(1)) if max_match else None
    mean_vol = float(mean_match.group(1)) if mean_match else None

    # Clipping if max_volume >= 0.0 dB; too quiet if mean_volume < -35 dB
    ok = True
    if max_vol is not None and max_vol >= 0.0:
        ok = False
    if mean_vol is not None and mean_vol < -35.0:
        ok = False
    return ok, {"max_volume_db": max_vol, "mean_volume_db": mean_vol}


def run_qa_checks(
    *,
    video_path: str | Path,
    fmt: str = "short",
    scene_plan: ScenePlan | None = None,
    subtitle_path: str | Path | None = None,
    asset_ids: list[str] | None = None,
    check_duplicate_hash: bool = True,
) -> QACheckResult:
    """Run all technical, safety, factual-source, and provenance QA checks."""
    vpath = Path(video_path).resolve()
    checks: dict[str, bool] = {}
    details: dict[str, Any] = {}
    blocking: list[str] = []

    if not vpath.is_file() or vpath.stat().st_size == 0:
        return QACheckResult(
            passed=False,
            checks={"file_exists": False},
            details={"video_path": str(vpath)},
            blocking_reasons=["Rendered video file is missing or empty."],
        )
    checks["file_exists"] = True

    # FFprobe metadata
    ffprobe = _bin("ffprobe")
    ffmpeg = _bin("ffmpeg")
    raw = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,width,height,sample_rate",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(vpath),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    meta = json.loads(raw.stdout)
    streams = meta.get("streams", [])
    v_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
    a_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    expected_dims = (1080, 1920) if fmt == "short" else (1920, 1080)
    actual_dims = (v_stream.get("width"), v_stream.get("height"))
    checks["dimensions"] = actual_dims == expected_dims
    details["dimensions"] = {"expected": expected_dims, "actual": actual_dims}
    if not checks["dimensions"]:
        blocking.append(f"Dimension mismatch: expected {expected_dims}, got {actual_dims}")

    duration = float(meta.get("format", {}).get("duration", 0.0))
    min_dur, max_dur = (15.0, 62.0) if fmt == "short" else (180.0, 1800.0)
    checks["duration_range"] = min_dur <= duration <= max_dur
    details["duration_seconds"] = duration
    details["duration_range"] = [min_dur, max_dur]
    if not checks["duration_range"]:
        blocking.append(f"Duration {duration:.2f}s outside expected range [{min_dur}, {max_dur}]")

    checks["audio_stream"] = a_stream is not None
    details["audio_stream"] = a_stream or {}
    if not checks["audio_stream"]:
        blocking.append("Missing audio stream in rendered video.")

    # Black frames & loudness/clipping
    black_ok, black_info = _detect_black_frames(ffmpeg, vpath)
    checks["no_black_frames"] = black_ok
    details["black_frames"] = black_info
    if not black_ok:
        blocking.append(f"Black frames detected: {black_info}")

    loud_ok, loud_info = _detect_audio_loudness_and_clipping(ffmpeg, vpath)
    checks["audio_loudness_no_clipping"] = loud_ok
    details["audio_loudness"] = loud_info
    if not loud_ok:
        blocking.append(f"Audio loudness/clipping check failed: {loud_info}")

    # Subtitle safe margins
    if subtitle_path:
        sub_ok, sub_info = check_subtitle_safe_margins(Path(subtitle_path), fmt=fmt)
        checks["subtitle_safe_margins"] = sub_ok
        details["subtitles"] = sub_info
        if not sub_ok:
            blocking.append(f"Subtitle safe-margin check failed: {sub_info}")

    # Duplicate file hash check
    video_hash = db.sha256_file(vpath)
    details["video_sha256"] = video_hash
    if check_duplicate_hash:
        existing = db.get_asset_by_hash(video_hash)
        is_dup = existing is not None and Path(existing["file_path"]).resolve() != vpath
        checks["not_duplicate_hash"] = not is_dup
        if is_dup:
            blocking.append(f"Duplicate rendered video hash detected: {video_hash}")
    else:
        checks["not_duplicate_hash"] = True

    # Factual-source & provenance checks
    if scene_plan is not None:
        missing_sources = [
            s.scene_index for s in scene_plan.scenes if not (s.source_notes or "").strip()
        ]
        unverified_facts = list(scene_plan.facts_to_verify)
        checks["factual_sources_present"] = len(missing_sources) == 0
        checks["no_pending_fact_verification"] = len(unverified_facts) == 0
        checks["synthetic_disclosure_set"] = bool(scene_plan.synthetic_disclosure)
        details["provenance"] = {
            "missing_source_scenes": missing_sources,
            "facts_to_verify": unverified_facts,
            "synthetic_disclosure": scene_plan.synthetic_disclosure,
        }
        if missing_sources:
            blocking.append(f"Missing source_notes on scenes: {missing_sources}")
        if unverified_facts:
            blocking.append(f"Unverified facts must be reviewed before public publish: {unverified_facts}")
        if not scene_plan.synthetic_disclosure:
            blocking.append("Synthetic media disclosure flag must be true for AI voice/visuals.")
    else:
        checks["factual_sources_present"] = False
        blocking.append("Missing ScenePlan provenance/source evidence.")

    if asset_ids is not None:
        checks["asset_provenance_recorded"] = len(asset_ids) > 0
        if not asset_ids:
            blocking.append("No registered asset provenance records provided.")

    passed = all(checks.values())
    return QACheckResult(
        passed=passed,
        checks=checks,
        details=details,
        blocking_reasons=blocking,
    )
