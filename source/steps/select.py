# source/steps/select.py
"""
Clip selection step - MOMENT-BASED APPROACH

Works with per-frame data from enriched.csv.
Each moment_id = 1 real-world moment with 2 perspectives (both cameras).
Only one perspective per moment can be recommended.

Pipeline:

1. Load enriched.csv rows (already analyzed and scored).
2. Group rows by moment_id into canonical moments (Fly12/Fly6).
3. For each moment, compute:
   - score_fly12, score_fly6
   - best_score = max(score_fly12, score_fly6)
   - scene_boost_max = max(scene_boosts)
4. Build a candidate pool:
   - Compute target_clips from HIGHLIGHT_TARGET_DURATION_S / CLIP_OUT_LEN_S
   - pool_size = target_clips * CANDIDATE_FRACTION
   - Group moments by clip_num and select top K per clip,
     where K is proportional: ceil(pool_size / num_clips).
5. Apply gap filtering over the candidate pool using moment_epoch and scene_boost_max.
6. For each accepted moment, choose the best perspective (higher score_weighted).
7. Emit select.csv with two rows per candidate-pool moment, at most one recommended="true".
8. Extract JPGs for all candidate-pool rows for manual review.
"""

from __future__ import annotations
from typing import List, Dict, Tuple
from pathlib import Path
import csv
import math

from ..io_paths import enrich_path, select_path, frames_dir, _mk
from ..utils.log import setup_logger
from ..utils.common import safe_float as _sf, read_csv as _load_csv
from ..config import DEFAULT_CONFIG as CFG
from ..models import get_registry
from .enrich_helpers.segment_matcher import SegmentMatcher

log = setup_logger("steps.select")


def _write_csv(path: Path, rows: List[Dict]):
    if not rows:
        return
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def extract_frame_images(rows: List[Dict]) -> int:
    """
    Extract frame images from videos for manual review.

    Optimized: Uses VideoCache for efficient extraction with:
    - Automatic video file caching (minimizes open/close overhead)
    - Consistent error handling and logging
    - Cache hit/miss statistics

    Frames are sorted by video path and frame number for optimal sequential access.
    """
    from ..io_paths import frames_dir, _mk
    from ..utils.video_utils import VideoCache
    import cv2

    frames_dir_path = _mk(frames_dir())
    extracted_count = 0

    # Filter to only rows that need extraction
    pending_rows = []
    for row in rows:
        index = row["index"]
        primary_out = frames_dir_path / f"{index}_Primary.jpg"
        if not primary_out.exists():
            pending_rows.append(row)

    if not pending_rows:
        log.info("[select] All frame images already extracted")
        return 0

    log.info(f"[select] Extracting {len(pending_rows)} frame images for manual review...")

    # Sort by video path then frame number for optimal cache usage
    pending_rows.sort(key=lambda r: (r["video_path"], int(float(r["frame_number"]))))

    # Use VideoCache for efficient extraction
    cache = VideoCache()
    try:
        for row in pending_rows:
            video_path = Path(row["video_path"])
            index = row["index"]
            frame_number = int(float(row["frame_number"]))
            primary_out = frames_dir_path / f"{index}_Primary.jpg"

            # Extract frame using cache (reuses open video for consecutive frames)
            frame = cache.extract_frame(video_path, frame_number)

            if frame is not None:
                # Convert RGB back to BGR for cv2.imwrite
                frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                cv2.imwrite(str(primary_out), frame_bgr)
                extracted_count += 1
    finally:
        cache.close()  # Logs cache statistics

    log.info(f"[select] Extracted {extracted_count} new frame images to {frames_dir_path}")
    return extracted_count


# -----------------------------
# Moment-centric helpers
# -----------------------------

def _group_rows_by_moment(rows: List[Dict]) -> List[Dict]:
    """
    Group enriched rows into canonical moments by moment_id.

    Each moment dict contains:
        {
            "moment_id": int,
            "moment_epoch": float,
            "fly12": Dict or None,      # None if front camera unavailable
            "fly6": Dict or None,       # None if rear camera unavailable
            "clip_num": int,
            "score_fly12": float,
            "score_fly6": float,
            "best_score": float,        # Includes dual_camera bonus if both perspectives
            "scene_boost_max": float,
            "is_dual_camera": bool,     # True if both perspectives available
        }

    Single-camera moments are allowed but receive no dual_camera bonus.
    Dual-camera moments receive a scoring bonus from SCORE_WEIGHTS["dual_camera"].
    """
    by_moment: Dict[str, List[Dict]] = {}
    for r in rows:
        mid = r.get("moment_id")
        if mid is None or mid == "":
            continue
        by_moment.setdefault(str(mid), []).append(r)

    moments: List[Dict] = []
    dropped = 0
    single_camera_count = 0
    dual_camera_count = 0

    # Get dual_camera bonus weight
    dual_camera_weight = CFG.SCORE_WEIGHTS.get("dual_camera", 0.0)

    for mid, group in by_moment.items():
        # Expect at most one row per camera per moment
        registry = get_registry()
        fly12_row = None
        fly6_row = None
        for r in group:
            cam = r.get("camera", "")
            if registry.is_front_camera(cam):
                fly12_row = r
            elif registry.is_rear_camera(cam):
                fly6_row = r

        # Require at least one camera perspective
        if not fly12_row and not fly6_row:
            dropped += 1
            continue

        is_dual_camera = fly12_row is not None and fly6_row is not None
        if is_dual_camera:
            dual_camera_count += 1
        else:
            single_camera_count += 1

        # Use abs_time_epoch from available camera(s) as canonical moment_epoch
        if fly12_row and fly6_row:
            t12 = _sf(fly12_row.get("abs_time_epoch"))
            t6 = _sf(fly6_row.get("abs_time_epoch"))
            moment_epoch = min(t12, t6)
        elif fly12_row:
            moment_epoch = _sf(fly12_row.get("abs_time_epoch"))
        else:
            moment_epoch = _sf(fly6_row.get("abs_time_epoch"))

        # clip_num from available camera(s)
        clip_num = 0
        if fly12_row and fly6_row:
            try:
                clip_num_12 = int(fly12_row.get("clip_num", "0"))
            except Exception:
                clip_num_12 = 0
            try:
                clip_num_6 = int(fly6_row.get("clip_num", "0"))
            except Exception:
                clip_num_6 = 0
            clip_num = clip_num_12 if clip_num_12 == clip_num_6 else min(clip_num_12, clip_num_6)
        elif fly12_row:
            try:
                clip_num = int(fly12_row.get("clip_num", "0"))
            except Exception:
                clip_num = 0
        else:
            try:
                clip_num = int(fly6_row.get("clip_num", "0"))
            except Exception:
                clip_num = 0

        # Scores from available cameras
        score_fly12 = _sf(fly12_row.get("score_weighted")) if fly12_row else 0.0
        score_fly6 = _sf(fly6_row.get("score_weighted")) if fly6_row else 0.0
        base_score = max(score_fly12, score_fly6)

        # Apply dual_camera bonus: dual-perspective moments score higher
        # This ensures dual-camera moments are preferred but single-camera
        # high-value moments can still make it through
        if is_dual_camera:
            best_score = base_score + dual_camera_weight
        else:
            best_score = base_score

        # Scene boost from available cameras
        scene12 = _sf(fly12_row.get("scene_boost")) if fly12_row else 0.0
        scene6 = _sf(fly6_row.get("scene_boost")) if fly6_row else 0.0
        scene_boost_max = max(scene12, scene6)

        moments.append({
            "moment_id": int(mid),
            "moment_epoch": moment_epoch,
            "fly12": fly12_row,
            "fly6": fly6_row,
            "clip_num": clip_num,
            "score_fly12": score_fly12,
            "score_fly6": score_fly6,
            "best_score": best_score,
            "scene_boost_max": scene_boost_max,
            "is_dual_camera": is_dual_camera,
        })

    moments.sort(key=lambda m: m["moment_epoch"])

    log.info("")
    log.info("=" * 60)
    log.info("MOMENT BUILDING")
    log.info("=" * 60)
    log.info(f"Total enriched rows: {len(rows)}")
    log.info(f"Moments built: {len(moments)}")
    log.info(f"  - Dual-camera moments: {dual_camera_count}")
    log.info(f"  - Single-camera moments: {single_camera_count}")
    log.info(f"Moments dropped (no camera data): {dropped}")
    log.info(f"Dual-camera bonus weight: {dual_camera_weight:.2f}")
    return moments


def _build_candidate_pool(moments: List[Dict], target_clips: int) -> List[Dict]:
    """
    Build candidate pool per raw clip, proportional to number of clips.

    Steps:
        1. Compute pool_size = target_clips * CANDIDATE_FRACTION.
        2. Group moments by clip_num.
        3. For each clip, take top K moments by best_score, where:
               K = ceil(pool_size / number_of_clips)
        4. Combine all clip winners into a pool.
        5. If pool is larger than pool_size, trim globally by best_score.
    """
    if not moments or target_clips <= 0:
        return []

    pool_size = max(1, int(target_clips * CFG.CANDIDATE_FRACTION))

    # Group moments by clip_num
    by_clip: Dict[int, List[Dict]] = {}
    for m in moments:
        by_clip.setdefault(m["clip_num"], []).append(m)

    num_clips = len(by_clip)
    if num_clips == 0:
        return []

    k_per_clip = max(1, int(math.ceil(pool_size / float(num_clips))))

    log.info("")
    log.info("=" * 60)
    log.info("CANDIDATE POOL SELECTION (PER CLIP)")
    log.info("=" * 60)
    log.info(f"Target clips: {target_clips}")
    log.info(f"Candidate fraction: {CFG.CANDIDATE_FRACTION:.2f}x")
    log.info(f"Desired pool_size: {pool_size} moments")
    log.info(f"Number of clips: {num_clips}")
    log.info(f"Moments per clip (K_per_clip): {k_per_clip}")

    pool: List[Dict] = []
    for clip_num, clip_moments in sorted(by_clip.items()):
        clip_moments_sorted = sorted(clip_moments, key=lambda m: m["best_score"], reverse=True)
        selected_for_clip = clip_moments_sorted[:k_per_clip]
        pool.extend(selected_for_clip)

        scores = [m["best_score"] for m in selected_for_clip]
        if scores:
            log.info(
                f"  Clip {clip_num:04d}: selected {len(selected_for_clip)} "
                f"moments (best={max(scores):.3f}, worst={min(scores):.3f})"
            )
        else:
            log.info(f"  Clip {clip_num:04d}: no moments selected")

    # If pool is larger than requested pool_size, trim globally by best_score
    if len(pool) > pool_size:
        pool_sorted = sorted(pool, key=lambda m: m["best_score"], reverse=True)
        cutoff_score = pool_sorted[pool_size - 1]["best_score"]
        pool = [m for m in pool_sorted if m["best_score"] >= cutoff_score]
        log.info(
            f"Trimmed pool from {len(pool_sorted)} to {len(pool)} using cutoff score {cutoff_score:.3f}"
        )
    else:
        cutoff_score = min((m["best_score"] for m in pool), default=0.0)

    log.info(f"Final candidate pool size: {len(pool)} moments")
    log.info(f"Candidate pool cutoff score: {cutoff_score:.3f}")
    return sorted(pool, key=lambda m: m["best_score"], reverse=True)


def _apply_gap_filter(moments: List[Dict], target_clips: int) -> List[Dict]:
    """
    Apply gap filtering to moments based on moment_epoch and scene_boost_max.

    Uses similar logic to the previous pair-based implementation, but operates
    at the moment level instead of pair level.
    """
    log.info("")
    log.info("=" * 60)
    log.info("GAP FILTERING (MOMENT LEVEL)")
    log.info("=" * 60)
    log.info(f"Target: {target_clips} moments")
    log.info(f"Min gap: {CFG.MIN_GAP_BETWEEN_CLIPS}s")
    log.info(f"Scene threshold: {CFG.SCENE_HIGH_THRESHOLD} (gap multiplier: {CFG.SCENE_HIGH_GAP_MULTIPLIER})")
    log.info("")

    accepted: List[Dict] = []
    used_windows = set()
    last_time = None

    for i, m in enumerate(moments):
        t = int(m["moment_epoch"])
        # Handle single-camera moments (fly12 or fly6 may be None)
        fly12 = m["fly12"]
        fly6 = m["fly6"]
        if fly12 and fly6:
            time_iso = (fly12.get("abs_time_iso", "") or fly6.get("abs_time_iso", ""))[:19]
            idx1 = fly12["index"]
            idx2 = fly6["index"]
            idx_display = f"{idx1} ↔ {idx2}"
        elif fly12:
            time_iso = fly12.get("abs_time_iso", "")[:19]
            idx_display = f"{fly12['index']} (front only)"
        else:
            time_iso = fly6.get("abs_time_iso", "")[:19]
            idx_display = f"{fly6['index']} (rear only)"

        scene_boost = m["scene_boost_max"]

        effective_gap = CFG.MIN_GAP_BETWEEN_CLIPS
        gap_reason = "normal"
        if scene_boost >= CFG.SCENE_HIGH_THRESHOLD:
            effective_gap *= CFG.SCENE_HIGH_GAP_MULTIPLIER
            gap_reason = "high scene"

        effective_gap = max(1, int(effective_gap))
        window = t // effective_gap

        time_since_last = (t - last_time) if last_time is not None else float("inf")

        dual_tag = "" if m.get("is_dual_camera", True) else " [single-cam]"
        if window not in used_windows:
            accepted.append(m)
            for offset in range(-1, 2):
                used_windows.add(window + offset)

            log.info(f"✓ ACCEPT [{len(accepted)}/{target_clips}] @ {time_iso}{dual_tag}")
            log.info(f"    {idx_display}")
            log.info(
                f"    Best score: {m['best_score']:.3f} | Scene: {scene_boost:.3f} ({gap_reason})"
            )
            log.info(f"    Gap: {effective_gap}s | Since last: {time_since_last:.0f}s")
            log.info("")

            last_time = t
        else:
            log.info(f"✗ REJECT [{i+1}] @ {time_iso}{dual_tag}")
            log.info(f"    {idx_display}")
            log.info(
                f"    Best score: {m['best_score']:.3f} | Scene: {scene_boost:.3f}"
            )
            log.info(
                f"    Reason: Too close to accepted moment (window {window} in use); "
                f"time since last: {time_since_last:.0f}s < {effective_gap}s required"
            )
            log.info("")

        if len(accepted) >= target_clips:
            remaining = len(moments) - i - 1
            if remaining > 0:
                log.info(f"Target reached. Skipping {remaining} remaining moments.")
            break

    return accepted


def _find_pr_moments(moments: List[Dict], segment_matcher: SegmentMatcher) -> List[Dict]:
    """
    Find moments that fall within Strava PR segments.

    Returns moments with is_strava_pr=True flag set.
    """
    if not segment_matcher.segments:
        return []

    pr_moments = []
    for m in moments:
        epoch = m["moment_epoch"]
        boost = segment_matcher.get_segment_boost(epoch)

        # Only flag actual PRs (rank 1), not just top-3
        if boost >= 1.0:
            m_copy = dict(m)
            m_copy["is_strava_pr"] = True
            m_copy["segment_name"] = segment_matcher.get_segment_name(epoch)
            pr_moments.append(m_copy)

    if pr_moments:
        log.info(f"")
        log.info("=" * 60)
        log.info("STRAVA PR SEGMENTS DETECTED")
        log.info("=" * 60)
        for pm in pr_moments:
            seg_name = pm.get("segment_name", "Unknown")
            log.info(f"🏆 PR: {seg_name} @ moment {pm['moment_id']}")

    return pr_moments


def _find_zone_moments(
    candidate_moments: List[Dict],
    recommended_moments: List[Dict],
    first_epoch: float,
    last_epoch: float,
) -> Tuple[List[Dict], List[Dict]]:
    """
    Find additional moments from start and end zones.

    These are bonus clips to fill up to MAX zone clips if the main
    selection didn't already include enough from start/end zones.

    Args:
        candidate_moments: All candidate moments (sorted by score)
        recommended_moments: Already-recommended moments
        first_epoch: Timestamp of first frame
        last_epoch: Timestamp of last frame

    Returns:
        Tuple of (start_zone_moments, end_zone_moments)
    """
    start_zone_end = first_epoch + (CFG.START_ZONE_DURATION_M * 60)
    end_zone_start = last_epoch - (CFG.END_ZONE_DURATION_M * 60)

    max_start = CFG.MAX_START_ZONE_CLIPS
    max_end = CFG.MAX_END_ZONE_CLIPS

    if max_start == 0 and max_end == 0:
        return [], []

    # Count existing zone clips in recommended
    recommended_ids = {m["moment_id"] for m in recommended_moments}
    existing_start = sum(1 for m in recommended_moments if m["moment_epoch"] <= start_zone_end)
    existing_end = sum(1 for m in recommended_moments if m["moment_epoch"] >= end_zone_start)

    # Calculate remaining quota
    remaining_start = max(0, max_start - existing_start)
    remaining_end = max(0, max_end - existing_end)

    if remaining_start == 0 and remaining_end == 0:
        return [], []

    # Filter to moments not already recommended
    available = [m for m in candidate_moments if m["moment_id"] not in recommended_ids]

    # Find start zone moments (sorted by score, take remaining quota)
    start_zone = [m for m in available if m["moment_epoch"] <= start_zone_end]
    start_zone = sorted(start_zone, key=lambda m: m["best_score"], reverse=True)[:remaining_start]

    # Find end zone moments (sorted by score, take remaining quota)
    end_zone = [m for m in available if m["moment_epoch"] >= end_zone_start]
    end_zone = sorted(end_zone, key=lambda m: m["best_score"], reverse=True)[:remaining_end]

    if start_zone or end_zone:
        log.info("")
        log.info("=" * 60)
        log.info("ZONE BONUS CLIPS")
        log.info("=" * 60)
        log.info(f"Start zone: {existing_start} existing + {len(start_zone)} bonus = {existing_start + len(start_zone)} (max {max_start})")
        log.info(f"End zone: {existing_end} existing + {len(end_zone)} bonus = {existing_end + len(end_zone)} (max {max_end})")

        if start_zone:
            log.info(f"🚀 Start zone: adding {len(start_zone)} bonus clips")
            for m in start_zone:
                fly12, fly6 = m["fly12"], m["fly6"]
                t_iso = ((fly12 or fly6).get("abs_time_iso", ""))[:19]
                dual_tag = "" if m.get("is_dual_camera", True) else " [single-cam]"
                log.info(f"   {t_iso} - score {m['best_score']:.3f}{dual_tag}")

        if end_zone:
            log.info(f"🏁 End zone: adding {len(end_zone)} bonus clips")
            for m in end_zone:
                fly12, fly6 = m["fly12"], m["fly6"]
                t_iso = ((fly12 or fly6).get("abs_time_iso", ""))[:19]
                dual_tag = "" if m.get("is_dual_camera", True) else " [single-cam]"
                log.info(f"   {t_iso} - score {m['best_score']:.3f}{dual_tag}")

    return start_zone, end_zone


def _enforce_zone_limits(
    recommended: List[Dict],
    candidates: List[Dict],
    first_epoch: float,
    last_epoch: float,
) -> List[Dict]:
    """
    Enforce MAX_START_ZONE_CLIPS and MAX_END_ZONE_CLIPS on the main selection.

    If too many clips are from start/end zones, remove extras and replace
    with the next-best mid-ride clips from the candidate pool.

    Args:
        recommended: Currently recommended moments from gap filter
        candidates: Full candidate pool (sorted by score descending)
        first_epoch: Timestamp of first frame
        last_epoch: Timestamp of last frame

    Returns:
        Adjusted list of recommended moments
    """
    start_zone_end = first_epoch + (CFG.START_ZONE_DURATION_M * 60)
    end_zone_start = last_epoch - (CFG.END_ZONE_DURATION_M * 60)

    max_start = CFG.MAX_START_ZONE_CLIPS
    max_end = CFG.MAX_END_ZONE_CLIPS

    # Separate into zones
    start_zone = []
    end_zone = []
    mid_ride = []

    for m in recommended:
        t = m["moment_epoch"]
        if t <= start_zone_end:
            start_zone.append(m)
        elif t >= end_zone_start:
            end_zone.append(m)
        else:
            mid_ride.append(m)

    start_excess = max(0, len(start_zone) - max_start)
    end_excess = max(0, len(end_zone) - max_end)

    if start_excess == 0 and end_excess == 0:
        return recommended

    log.info("")
    log.info("=" * 60)
    log.info("ZONE LIMIT ENFORCEMENT")
    log.info("=" * 60)
    log.info(f"Start zone: {len(start_zone)} clips (max {max_start})")
    log.info(f"End zone: {len(end_zone)} clips (max {max_end})")
    log.info(f"Mid-ride: {len(mid_ride)} clips")

    # Keep only top N from each zone (sorted by score)
    start_zone_sorted = sorted(start_zone, key=lambda m: m["best_score"], reverse=True)
    end_zone_sorted = sorted(end_zone, key=lambda m: m["best_score"], reverse=True)

    kept_start = start_zone_sorted[:max_start]
    kept_end = end_zone_sorted[:max_end]
    removed_start = start_zone_sorted[max_start:]
    removed_end = end_zone_sorted[max_end:]

    if removed_start:
        log.info(f"⚠️  Removing {len(removed_start)} excess start zone clips:")
        for m in removed_start:
            t_iso = ((m["fly12"] or m["fly6"]).get("abs_time_iso", ""))[:19]
            log.info(f"   - {t_iso} score={m['best_score']:.3f}")

    if removed_end:
        log.info(f"⚠️  Removing {len(removed_end)} excess end zone clips:")
        for m in removed_end:
            t_iso = ((m["fly12"] or m["fly6"]).get("abs_time_iso", ""))[:19]
            log.info(f"   - {t_iso} score={m['best_score']:.3f}")

    # Build new recommended list
    new_recommended = kept_start + mid_ride + kept_end
    kept_ids = {m["moment_id"] for m in new_recommended}

    # Find replacement clips from mid-ride candidates
    slots_to_fill = start_excess + end_excess
    if slots_to_fill > 0:
        log.info(f"🔄 Finding {slots_to_fill} replacement clips from mid-ride...")

        # Find mid-ride candidates not already selected
        mid_ride_candidates = [
            m for m in candidates
            if m["moment_id"] not in kept_ids
            and start_zone_end < m["moment_epoch"] < end_zone_start
        ]

        # Apply simple gap filtering to replacements
        min_gap = CFG.MIN_GAP_BETWEEN_CLIPS
        used_times = {int(m["moment_epoch"]) for m in new_recommended}
        added = 0

        for m in mid_ride_candidates:
            if added >= slots_to_fill:
                break

            t = int(m["moment_epoch"])
            # Check gap from existing clips
            too_close = any(abs(t - ut) < min_gap for ut in used_times)
            if not too_close:
                new_recommended.append(m)
                used_times.add(t)
                added += 1
                t_iso = ((m["fly12"] or m["fly6"]).get("abs_time_iso", ""))[:19]
                log.info(f"   + {t_iso} score={m['best_score']:.3f}")

        if added < slots_to_fill:
            log.info(f"   (Only found {added} suitable replacements)")

    # Sort by time for consistent output
    new_recommended.sort(key=lambda m: m["moment_epoch"])

    log.info(f"Final selection: {len(new_recommended)} clips")
    log.info(f"  Start zone: {len([m for m in new_recommended if m['moment_epoch'] <= start_zone_end])}")
    log.info(f"  Mid-ride: {len([m for m in new_recommended if start_zone_end < m['moment_epoch'] < end_zone_start])}")
    log.info(f"  End zone: {len([m for m in new_recommended if m['moment_epoch'] >= end_zone_start])}")

    return new_recommended


# -----------------------------
# Main entrypoint
# -----------------------------

def run() -> Path:
    """Main selection step: moment-based selection."""
    log.info("=" * 60)
    log.info("SELECT STEP: Moment-based selection")
    log.info("=" * 60)

    enriched = _load_csv(enrich_path())
    if not enriched:
        log.warning("No enriched frames found.")
        return select_path()

    # Ensure chronological ordering for logs / duration reporting
    enriched.sort(key=lambda r: _sf(r.get("abs_time_epoch")))

    # DO NOT recompute moment_id here.
    # enrich.py already assigns correct moment_id using abs_time_epoch.
    log.info(f"Loaded {len(enriched)} enriched frames")
    log.info("Using moment_id from enrich.py (abs_time_epoch-based)")

    first_time = _sf(enriched[0].get("abs_time_epoch"))
    last_time = _sf(enriched[-1].get("abs_time_epoch"))
    duration_s = max(0.0, last_time - first_time)
    duration_h = duration_s / 3600.0
    log.info(f"Total footage duration: {duration_h:.1f} hours ({duration_s:.0f} seconds)")

    # Build canonical moments from enriched rows
    moments = _group_rows_by_moment(enriched)
    if not moments:
        log.error("No valid moments found in enriched data.")
        return select_path()

    # Selection targets
    target_clips = int((CFG.HIGHLIGHT_TARGET_DURATION_M * 60) // CFG.CLIP_OUT_LEN_S)
    if target_clips <= 0:
        log.warning("Non-positive target_clips; nothing to select.")
        return select_path()

    # Candidate pool per clip, proportional
    candidate_moments = _build_candidate_pool(moments, target_clips)
    if not candidate_moments:
        log.error("No candidate moments could be built.")
        return select_path()

    # Log top scoring candidate moments
    log.info("")
    log.info("=" * 60)
    log.info("TOP SCORING CANDIDATE MOMENTS")
    log.info("=" * 60)
    for i, m in enumerate(candidate_moments[:20], 1):
        fly12, fly6 = m["fly12"], m["fly6"]
        t_iso = ((fly12 or fly6).get("abs_time_iso", ""))[:19]
        dual_tag = "" if m.get("is_dual_camera", True) else " [single-cam]"
        log.info(f"{i:2d}. Score {m['best_score']:.3f} @ {t_iso}{dual_tag}")
        if fly12 and fly6:
            log.info(f"    {fly12['index']} ↔ {fly6['index']}")
        elif fly12:
            log.info(f"    {fly12['index']} (front only)")
        else:
            log.info(f"    {fly6['index']} (rear only)")
        log.info(
            f"    Fly12: {m['score_fly12']:.3f} | Fly6: {m['score_fly6']:.3f} | "
            f"Scene: {m['scene_boost_max']:.3f}"
        )

    # Gap filtering on candidate pool
    recommended_moments = _apply_gap_filter(candidate_moments, target_clips)

    # Enforce zone limits (cap start/end zone clips, replace with mid-ride)
    recommended_moments = _enforce_zone_limits(
        recommended_moments,
        candidate_moments,
        first_time,
        last_time,
    )

    # Check for Strava PR segments to auto-include
    segment_matcher = SegmentMatcher()
    pr_moments = _find_pr_moments(candidate_moments, segment_matcher)

    # Merge PR moments into recommended (avoid duplicates)
    recommended_moment_ids = {m["moment_id"] for m in recommended_moments}
    pr_added = 0
    for pm in pr_moments:
        if pm["moment_id"] not in recommended_moment_ids:
            recommended_moments.append(pm)
            recommended_moment_ids.add(pm["moment_id"])
            pr_added += 1

    if pr_added > 0:
        log.info(f"")
        log.info(f"🏆 Added {pr_added} Strava PR segment moments to recommended list")

    # Add zone bonus clips (start/end of ride).
    # Search the full moments list — not just the score-trimmed candidate pool —
    # so that low-scoring start/end zone content isn't eliminated before zone
    # selection runs. MAX_START/END_ZONE_CLIPS caps the count regardless.
    start_zone_moments, end_zone_moments = _find_zone_moments(
        moments,
        recommended_moments,
        first_time,
        last_time,
    )

    zone_added = 0
    for zm in start_zone_moments + end_zone_moments:
        if zm["moment_id"] not in recommended_moment_ids:
            recommended_moments.append(zm)
            recommended_moment_ids.add(zm["moment_id"])
            zone_added += 1

    if zone_added > 0:
        log.info(f"")
        log.info(f"📍 Added {zone_added} zone bonus clips to recommended list")

    log.info("")
    log.info("=" * 60)
    log.info("PERSPECTIVE SELECTION PER MOMENT")
    log.info("=" * 60)
    log.info("Choosing best perspective for each recommended moment...")

    recommended_indices = set()
    pr_indices = set()  # Track which indices are from PR segments

    for m in recommended_moments:
        fly12 = m["fly12"]
        fly6 = m["fly6"]
        score12 = m["score_fly12"]
        score6 = m["score_fly6"]
        is_dual = m.get("is_dual_camera", True)

        # Handle single-camera moments
        if not fly12:
            # Rear camera only
            chosen = fly6
            other = None
            chosen_score = score6
            other_score = None
        elif not fly6:
            # Front camera only
            chosen = fly12
            other = None
            chosen_score = score12
            other_score = None
        elif score12 >= score6:
            chosen = fly12
            other = fly6
            chosen_score = score12
            other_score = score6
        else:
            chosen = fly6
            other = fly12
            chosen_score = score6
            other_score = score12

        recommended_indices.add(chosen["index"])

        # Check if this is a PR moment
        is_pr = m.get("is_strava_pr", False)
        if is_pr:
            pr_indices.add(chosen["index"])
            if other:
                pr_indices.add(other["index"])

        time_iso = chosen.get("abs_time_iso", "")[:19]
        pr_badge = " 🏆 PR" if is_pr else ""
        dual_tag = "" if is_dual else " [single-cam]"
        log.info(f"✓ {time_iso}: {chosen['index']} (score {chosen_score:.3f}){pr_badge}{dual_tag}")
        if other:
            log.info(
                f"  Chosen: {chosen.get('camera')} - score={chosen_score:.3f} | "
                f"Other: {other.get('camera')} - score={other_score:.3f}"
            )
        else:
            log.info(f"  Camera: {chosen.get('camera')} - score={chosen_score:.3f} (single perspective)")

    # Build output rows: all rows from candidate pool, with recommended, strava_pr, and segment info
    output_rows: List[Dict] = []
    for m in candidate_moments:
        # Handle single-camera moments (fly12 or fly6 may be None)
        rows_to_process = [r for r in (m["fly12"], m["fly6"]) if r is not None]
        for row in rows_to_process:
            row = dict(row)  # avoid mutating original enriched row list
            row["recommended"] = "true" if row["index"] in recommended_indices else "false"
            row["strava_pr"] = "true" if row["index"] in pr_indices else "false"
            row["is_single_camera"] = "true" if not m.get("is_dual_camera", True) else "false"
            row["paired"] = "true" if m.get("is_dual_camera", True) else "false"
            # Add segment details for PR clips (for trophy badge overlay)
            if row["index"] in pr_indices:
                epoch = _sf(row.get("abs_time_epoch"))
                seg_info = segment_matcher.get_segment_info(epoch)
                if seg_info:
                    row["segment_name"] = seg_info.get("name", "")
                    row["segment_distance"] = str(seg_info.get("distance", 0))
                    row["segment_grade"] = str(seg_info.get("average_grade", 0))
                else:
                    row["segment_name"] = ""
                    row["segment_distance"] = ""
                    row["segment_grade"] = ""
            else:
                row["segment_name"] = ""
                row["segment_distance"] = ""
                row["segment_grade"] = ""
            output_rows.append(row)

    # Sort by aligned world time
    output_rows.sort(key=lambda r: _sf(r.get("abs_time_epoch")))

    # Write select.csv with all fields present in enriched rows
    _write_csv(select_path(), output_rows)

    log.info("")
    log.info("=" * 60)
    log.info("SELECT COMPLETE")
    log.info("=" * 60)
    log.info(f"Total frames analyzed: {len(enriched)}")
    log.info(f"Moments built: {len(moments)}")
    log.info(f"Candidate pool: {len(candidate_moments)} moments ({len(candidate_moments) * 2} rows)")
    log.info(f"Recommended: {len(recommended_moments)} moments ({len(recommended_indices)} clips)")
    log.info(f"Target: {target_clips}")
    if target_clips > 0:
        log.info(f"Pool ratio: {len(candidate_moments) / target_clips:.1f}x")
    log.info("=" * 60)

    extract_frame_images(output_rows)
    log.info("Ready for manual review")

    return select_path()

