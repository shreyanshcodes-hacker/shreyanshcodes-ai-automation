"""Comprehensive video, audio, subtitle, duplicate-hash, and structured provenance QA checks."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import db
from .schemas import ClaimProvenance, QACheckResult, ScenePlan


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
            for line in lines[2:]:
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

    ok = True
    if max_vol is not None and max_vol >= 0.0:
        ok = False
    if mean_vol is not None and mean_vol < -35.0:
        ok = False
    return ok, {"max_volume_db": max_vol, "mean_volume_db": mean_vol}


def evaluate_provenance_records(
    records: list[ClaimProvenance],
    scene_plan: ScenePlan | None = None,
) -> tuple[bool, bool, dict[str, Any], list[str], list[str]]:
    """
    Evaluate structured factual provenance records.
    Returns (records_valid, all_verified, summary_dict, blocking_reasons, warnings).
    """
    blocking: list[str] = []
    warnings: list[str] = []
    verified: list[str] = []
    pending: list[str] = []
    unsupported: list[str] = []
    contradictory: list[str] = []
    malformed: list[str] = []

    for rec in records:
        parsed = urlparse(rec.source_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            malformed.append(rec.claim_text[:60])
        if not rec.supporting_excerpt.strip() or not rec.source_title.strip():
            malformed.append(rec.claim_text[:60])

        if rec.verification_status == "verified":
            verified.append(rec.claim_text[:80])
        elif rec.verification_status == "pending_review":
            pending.append(rec.claim_text[:80])
        elif rec.verification_status == "unsupported":
            unsupported.append(rec.claim_text[:80])
        elif rec.verification_status == "contradictory":
            contradictory.append(rec.claim_text[:80])

    if malformed:
        blocking.append(f"Malformed provenance records: {malformed}")
    if unsupported:
        blocking.append(f"Unsupported factual claims: {unsupported}")
    if contradictory:
        blocking.append(f"Contradictory factual claims: {contradictory}")
    if pending:
        warnings.append(f"Claims awaiting human verification (public scheduling blocked): {pending}")

    if scene_plan and scene_plan.facts_to_verify:
        warnings.append(
            f"ScenePlan lists facts_to_verify awaiting human review: {scene_plan.facts_to_verify}"
        )

    needs_verif_scenes = (
        [s.scene_index for s in scene_plan.scenes if s.needs_verification]
        if scene_plan
        else []
    )
    if needs_verif_scenes and not records:
        warnings.append(
            f"Scenes {needs_verif_scenes} marked needs_verification=true without structured provenance records."
        )

    records_valid = len(malformed) == 0 and len(unsupported) == 0 and len(contradictory) == 0
    all_verified = (
        records_valid
        and len(pending) == 0
        and not (scene_plan and scene_plan.facts_to_verify)
        and not (needs_verif_scenes and not records)
    )
    summary = {
        "verified_count": len(verified),
        "pending_review_count": len(pending),
        "unsupported_count": len(unsupported),
        "contradictory_count": len(contradictory),
        "malformed_count": len(malformed),
        "records": [r.model_dump() for r in records],
    }
    return records_valid, all_verified, summary, blocking, warnings


def run_qa_checks(
    *,
    video_path: str | Path,
    fmt: str = "short",
    scene_plan: ScenePlan | None = None,
    subtitle_path: str | Path | None = None,
    asset_ids: list[str] | None = None,
    provenance_records: list[ClaimProvenance] | None = None,
    check_duplicate_hash: bool = True,
) -> QACheckResult:
    """Run technical, safety, factual provenance, and asset provenance QA checks."""
    vpath = Path(video_path).resolve()
    checks: dict[str, bool] = {}
    details: dict[str, Any] = {}
    blocking: list[str] = []
    warnings: list[str] = []

    if not vpath.is_file() or vpath.stat().st_size == 0:
        return QACheckResult(
            passed=False,
            public_schedule_allowed=False,
            private_preview_allowed=False,
            checks={"file_exists": False},
            details={"video_path": str(vpath)},
            blocking_reasons=["Rendered video file is missing or empty."],
        )
    checks["file_exists"] = True

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

    if subtitle_path:
        sub_ok, sub_info = check_subtitle_safe_margins(Path(subtitle_path), fmt=fmt)
        checks["subtitle_safe_margins"] = sub_ok
        details["subtitles"] = sub_info
        if not sub_ok:
            blocking.append(f"Subtitle safe-margin check failed: {sub_info}")

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

    records = provenance_records if provenance_records is not None else (
        scene_plan.provenance_records if scene_plan else []
    )
    records_valid, all_verified, prov_summary, prov_blocking, prov_warnings = evaluate_provenance_records(
        records, scene_plan=scene_plan
    )
    blocking.extend(prov_blocking)
    warnings.extend(prov_warnings)

    if scene_plan is not None:
        missing_sources = [
            s.scene_index for s in scene_plan.scenes if not (s.source_notes or "").strip()
        ]
        checks["factual_sources_present"] = len(missing_sources) == 0 and records_valid
        checks["factual_claims_verified"] = all_verified
        checks["synthetic_disclosure_set"] = bool(scene_plan.synthetic_disclosure)
        details["provenance"] = {
            "missing_source_scenes": missing_sources,
            "facts_to_verify": list(scene_plan.facts_to_verify),
            "synthetic_disclosure": scene_plan.synthetic_disclosure,
            "structured_claims": prov_summary,
        }
        if missing_sources:
            blocking.append(f"Missing source_notes on scenes: {missing_sources}")
        if not scene_plan.synthetic_disclosure:
            blocking.append("Synthetic media disclosure flag must be true for AI voice/visuals.")
    else:
        checks["factual_sources_present"] = False
        checks["factual_claims_verified"] = False
        blocking.append("Missing ScenePlan provenance/source evidence.")

    if asset_ids is not None:
        checks["asset_provenance_recorded"] = len(asset_ids) > 0
        if not asset_ids:
            blocking.append("No registered asset provenance records provided.")

    technical_checks = {
        k: v for k, v in checks.items() if k != "factual_claims_verified"
    }
    private_preview_allowed = all(technical_checks.values())
    public_schedule_allowed = all(checks.values())

    return QACheckResult(
        passed=public_schedule_allowed,
        public_schedule_allowed=public_schedule_allowed,
        private_preview_allowed=private_preview_allowed,
        checks=checks,
        details=details,
        blocking_reasons=blocking,
        warnings=warnings,
    )
