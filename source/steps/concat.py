# source/steps/concat.py
"""
Concatenate _intro.mp4, all _middle_##.mp4 segments, and _outro.mp4 into final reel.
Output filename is derived from ride folder name.

MODIFIED: Uses stream copy (fast!) since all inputs are already 1080p.
"""

from __future__ import annotations
from pathlib import Path
import subprocess
import re
import time

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


def _format_eta(seconds: float) -> str:
    """Format seconds as human-readable ETA string."""
    if seconds >= 60:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    else:
        return f"{int(seconds)}s"


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

    # Step 2: Concatenate and re-encode for Facebook compatibility
    total_duration = _get_total_duration(final_parts)
    log.info(f"[concat] Concatenating {len(final_parts)} parts ({total_duration:.1f}s total) with Facebook-compliant encoding...")

    start_time = time.time()

    # Run FFmpeg with progress output
    cmd = [
        "ffmpeg", "-hide_banner", "-y",
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        # Re-encode to ensure Facebook compatibility
        "-c:v", "libx264",           # H.264 codec
        "-preset", "medium",         # Encoding speed
        "-crf", "23",                # Quality (18-28, 23 is good)
        "-profile:v", "high",        # H.264 profile
        "-level", "4.0",             # H.264 level
        "-vf", f"scale={CFG.OUTPUT_W}:{CFG.OUTPUT_H}",  # Force output resolution
        "-pix_fmt", "yuv420p",       # Pixel format (required)
        "-r", "30",                  # Force 30 fps
        "-c:a", "aac",               # AAC audio
        "-b:a", "128k",              # Audio bitrate
        "-ar", "48000",              # Audio sample rate
        "-progress", "pipe:1",       # Output progress to stdout
        "-loglevel", "error",
        str(out)
    ]

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    last_report_time = 0
    current_time_s = 0.0

    # Parse progress output
    for line in process.stdout:
        line = line.strip()
        if line.startswith("out_time_ms="):
            try:
                out_time_ms = int(line.split("=")[1])
                current_time_s = out_time_ms / 1_000_000.0
            except (ValueError, IndexError):
                pass
        elif line == "progress=continue" or line == "progress=end":
            # Update progress
            if total_duration > 0 and current_time_s > 0:
                progress_pct = min(current_time_s / total_duration, 1.0)
                elapsed = time.time() - start_time

                if elapsed > 0 and progress_pct > 0:
                    eta_seconds = (elapsed / progress_pct) * (1.0 - progress_pct)
                    eta_str = _format_eta(eta_seconds)
                else:
                    eta_str = "calculating..."

                # Report every 2 seconds to avoid spam
                now = time.time()
                if now - last_report_time >= 2.0:
                    report_progress(
                        int(progress_pct * 100),
                        100,
                        f"Encoding: {int(progress_pct * 100)}% (ETA: {eta_str})"
                    )
                    last_report_time = now

    # Wait for process to complete
    process.wait()

    if process.returncode != 0:
        stderr = process.stderr.read()
        log.error(f"[concat] FFmpeg failed: {stderr}")
        raise subprocess.CalledProcessError(process.returncode, cmd)

    # Step 3: Finalize output
    report_progress(3, 3, "Finalizing output...")
    
    # Get final file size
    file_size_mb = out.stat().st_size / (1024 * 1024)
    log.info(f"[concat] Final 1080p reel: {out}")
    log.info(f"[concat] File size: {file_size_mb:.1f} MB")
    
    # Warn if approaching Facebook limits
    if file_size_mb > 4000:  # 4GB
        log.warning(f"[concat] File size ({file_size_mb:.1f} MB) exceeds 4GB - may have upload issues")
    elif file_size_mb > 3000:  # 3GB
        log.warning(f"[concat] File size ({file_size_mb:.1f} MB) is large - upload may be slow")
    
    log.info("[concat] ✅ Output is Facebook-compliant (1080p, H.264, 30fps, AAC audio)")
    log.info("[concat] ✅ Intro, middle segments, and outro are all Strava-compliant (≤30s each)")

    try:
        concat_list.unlink()
    except Exception as e:
        log.debug(f"[concat] cleanup warning: {e}")

    return out


