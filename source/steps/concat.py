# source/steps/concat.py
"""
Concatenate _intro.mp4, all _middle_##.mp4 segments, and _outro.mp4 into final reel.
Output filename is derived from ride folder name.

Uses stream copy — all inputs are 1920×1080 H.264 yuv420p limited-range (color_range=1),
so no re-encode is needed. The final concat is near-instant.
"""

from __future__ import annotations
from pathlib import Path
import subprocess
import re

from ..config import DEFAULT_CONFIG as CFG
from ..io_paths import clips_dir
from ..utils.log import setup_logger
from ..utils.progress_reporter import report_progress
from ..utils.ffmpeg import get_video_duration

log = setup_logger("steps.concat")


def _get_total_duration(parts: list[Path]) -> float:
    """Get total duration of all video parts in seconds."""
    total = 0.0
    for part in parts:
        try:
            duration = get_video_duration(part)
            if duration:
                total += duration
        except Exception:
            pass
    return total


def run() -> Path:
    """Concatenate intro, multiple middle segments, and outro into final 1080p reel."""
    clips_path = CFG.FINAL_REEL_PATH.parent
    out = CFG.FINAL_REEL_PATH

    intro = clips_path / "_intro.mp4"
    outro = clips_path / "_outro.mp4"

    # Step 1: Collect segments
    report_progress(1, 3, "Collecting segments...")
    middle_segments = []
    for f in clips_path.glob("_middle_*.mp4"):
        m = re.match(r"_middle_(\d+)\.mp4", f.name)
        if m:
            idx = int(m.group(1))
            middle_segments.append((idx, f))
    middle_segments.sort(key=lambda x: x[0])
    middle_files = [f for _, f in middle_segments]

    if not middle_files:
        log.error("[concat] No _middle_##.mp4 segments found – run 'build' step first")
        return out

    # Build final parts list
    final_parts = []
    if intro.exists():
        final_parts.append(intro)
    else:
        log.warning("[concat] _intro.mp4 not found – skipping")

    final_parts.extend(middle_files)

    if outro.exists():
        final_parts.append(outro)
    else:
        log.warning("[concat] _outro.mp4 not found – skipping")

    if not final_parts:
        log.error("[concat] No clips to concatenate")
        return out

    concat_list = CFG.WORKING_DIR / "final_concat_list.txt"
    with concat_list.open("w") as f:
        for part in final_parts:
            f.write(f"file '{part.resolve()}'\n")

    # Step 2: Stream-copy concat — all inputs are 1920×1080 H.264 yuv420p limited-range,
    # so no re-encode is needed. This completes in seconds regardless of reel length.
    total_duration = _get_total_duration(final_parts)
    log.info(f"[concat] Stream-copying {len(final_parts)} parts ({total_duration:.1f}s total)...")

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c", "copy",
        "-movflags", "+faststart",
        str(out)
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error(f"[concat] FFmpeg failed: {result.stderr}")
        raise subprocess.CalledProcessError(result.returncode, cmd)

    # Step 3: Finalize output
    report_progress(3, 3, "Finalizing output...")

    file_size_mb = out.stat().st_size / (1024 * 1024)
    log.info(f"[concat] Final reel: {out} ({file_size_mb:.1f} MB)")

    if file_size_mb > 4000:
        log.warning(f"[concat] File size ({file_size_mb:.1f} MB) exceeds 4GB — may have upload issues")

    try:
        concat_list.unlink()
    except Exception as e:
        log.debug(f"[concat] cleanup warning: {e}")

    return out


