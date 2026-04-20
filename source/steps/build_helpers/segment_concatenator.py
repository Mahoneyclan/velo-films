# source/steps/build_helpers/segment_concatenator.py
"""
Multi-segment video assembly with CONTINUOUS music overlay.
Concatenates clips into ~30s segments with seamless music progression.

KEY FEATURES:
- Music continues across segments without restarting
- Crossfade transitions between clips (0.2s default)
- Fade in on first clip, fade out on last clip
Music stored in: assets/music/
Intro/outro music: separate files in assets/
"""

from __future__ import annotations
import subprocess
import random
import json
from pathlib import Path
from typing import List, Optional, Tuple

from ...config import DEFAULT_CONFIG as CFG
from ...utils.log import setup_logger
from ...utils.progress_reporter import report_progress
from ...utils.hardware import get_optimal_video_codec
from ...io_paths import _mk

log = setup_logger("steps.build_helpers.segment_concatenator")

AUDIO_SAMPLE_RATE = "48000"
CROSSFADE_DURATION = 0.2  # seconds
FADE_DURATION = 0.3  # fade in/out duration for first/last clips

# Supported music formats
MUSIC_EXTENSIONS = [".mp3", ".wav", ".m4a", ".aac", ".flac"]


def get_music_dir() -> Path:
    """
    Get the music directory path.
    Music is stored in: PROJECT_ROOT/assets/music/
    
    Returns:
        Path to music directory
    """
    return CFG.PROJECT_ROOT / "assets" / "music"


class SegmentConcatenator:
    """Concatenates clips into segments with continuous music overlay."""
    
    def __init__(self, project_dir: Path, working_dir: Path):
        """
        Args:
            project_dir: Project directory for output segments
            working_dir: Working directory for temp files
        """
        self.project_dir = project_dir
        self.working_dir = _mk(working_dir)
        self.temp_files: List[Path] = []
        
        # Music tracking for continuous playback
        self.selected_music_track: Optional[Path] = None
        self.music_offset: float = 0.0  # Current position in music track
    
    def concatenate_into_segments(
        self,
        clips: List[Path],
        music_volume: float = 0.5,
        raw_audio_volume: float = 0.6
    ) -> List[Path]:
        """
        Concatenate clips into ~30s segments with CONTINUOUS music.
        
        Music plays continuously across all segments:
        - Segment 1: music 0:00-0:30
        - Segment 2: music 0:30-1:00
        - Segment 3: music 1:00-1:30
        - etc.
        
        Music is loaded from: PROJECT_ROOT/assets/music/
        
        Args:
            clips: List of clip paths to concatenate
            music_volume: Music track volume (0.0-1.0)
            raw_audio_volume: Camera audio volume (0.0-1.0)
            
        Returns:
            List of paths to created segment files
        """
        if not clips:
            log.warning("[segment] No clips to concatenate")
            return []
        
        # Calculate segments
        highlights_per_segment = int(30.0 // CFG.CLIP_OUT_LEN_S)
        num_segments = (len(clips) + highlights_per_segment - 1) // highlights_per_segment
        
        log.info(
            f"[segment] Concatenating {len(clips)} clips into "
            f"{num_segments} × ~30s segments ({highlights_per_segment} clips/segment)"
        )
        
        # Select SINGLE music track for all segments
        music_dir = get_music_dir()
        self._select_music_track(music_dir)
        
        if self.selected_music_track:
            log.info(f"[segment] Using continuous music: {self.selected_music_track.name}")
        else:
            log.warning("[segment] No music track found, creating segments without music")
        
        # Reset music offset for new concatenation
        self.music_offset = 0.0
        
        segment_paths: List[Path] = []

        for seg_idx in range(num_segments):
            start = seg_idx * highlights_per_segment
            end = min(start + highlights_per_segment, len(clips))
            segment_clips = clips[start:end]

            if not segment_clips:
                continue

            # Report progress
            report_progress(
                seg_idx + 1, num_segments,
                f"Creating segment {seg_idx + 1}/{num_segments}"
            )

            # Create segment with continuous music and transitions
            is_first = (seg_idx == 0)
            is_last = (seg_idx == num_segments - 1)

            segment_path = self._create_segment(
                segment_clips=segment_clips,
                segment_num=seg_idx + 1,
                music_volume=music_volume,
                raw_audio_volume=raw_audio_volume,
                is_first_segment=is_first,
                is_last_segment=is_last
            )

            if segment_path:
                segment_paths.append(segment_path)
                log.info(f"[segment] Segment {seg_idx + 1}/{num_segments} complete")
        
        # Cleanup temp files
        self._cleanup_temp_files()
        
        log.info(f"[segment] Created {len(segment_paths)} segments with continuous music")
        return segment_paths
    
    def _select_music_track(self, music_path: Path) -> None:
        """
        Select a music track - user-selected or random from directory.

        Checks CFG.SELECTED_MUSIC_TRACK first. If empty/not set, picks randomly.

        Args:
            music_path: Path to music directory (assets/music)
        """
        from ...config import DEFAULT_CONFIG as CFG

        # Check for user-selected track
        user_selected = getattr(CFG, 'SELECTED_MUSIC_TRACK', "")
        if user_selected:
            track_path = Path(user_selected)
            if track_path.exists():
                self.selected_music_track = track_path
                log.info(f"[segment] Using user-selected track: {track_path.name}")
                return
            else:
                log.warning(f"[segment] Selected track not found: {track_path}, falling back to random")

        # Fall back to random selection
        music_files = self._find_music_files(music_path)

        if not music_files:
            self.selected_music_track = None
            return

        self.selected_music_track = random.choice(music_files)
        log.info(f"[segment] Randomly selected track: {self.selected_music_track.name}")
    
    def _find_music_files(self, music_path: Path) -> List[Path]:
        """
        Find all supported music files in directory.
        
        Args:
            music_path: Path to music directory
            
        Returns:
            List of music file paths
        """
        if not music_path.exists():
            log.warning(f"[segment] Music directory not found: {music_path}")
            log.info(f"[segment] Create directory and add music: mkdir -p {music_path}")
            return []
        
        music_files = []
        for ext in MUSIC_EXTENSIONS:
            music_files.extend(music_path.glob(f"*{ext}"))
            music_files.extend(music_path.glob(f"*{ext.upper()}"))
        
        # Remove duplicates and sort
        music_files = sorted(set(music_files))
        
        if music_files:
            log.info(f"[segment] Found {len(music_files)} music file(s) in {music_path}")
        
        return music_files
    
    def _get_video_duration(self, video_path: Path) -> float:
        """
        Get video duration in seconds using ffprobe.
        
        Args:
            video_path: Path to video file
            
        Returns:
            Duration in seconds, or 0.0 if unable to determine
        """
        try:
            result = subprocess.run([
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_entries", "format=duration", str(video_path)
            ], capture_output=True, text=True, check=True)
            
            data = json.loads(result.stdout)
            return float(data.get("format", {}).get("duration", 0))
        except Exception as e:
            log.debug(f"[segment] Could not get duration for {video_path.name}: {e}")
            return 0.0
    
    def _create_segment(
        self,
        segment_clips: List[Path],
        segment_num: int,
        music_volume: float,
        raw_audio_volume: float,
        is_first_segment: bool = False,
        is_last_segment: bool = False
    ) -> Path:
        """Create single segment from clips with transitions and music overlay."""
        # Step 1: Concatenate clips with crossfade transitions
        raw_segment = self._concatenate_clips_with_transitions(
            segment_clips, segment_num, is_first_segment, is_last_segment
        )
        if not raw_segment:
            return None
        
        # Step 2: Get segment duration
        segment_duration = self._get_video_duration(raw_segment)
        if segment_duration == 0:
            log.warning(f"[segment] Could not determine duration for segment {segment_num}")
            segment_duration = len(segment_clips) * CFG.CLIP_OUT_LEN_S  # Fallback estimate
        
        # Step 3: Add continuous music overlay
        final_segment = self._add_continuous_music(
            video_path=raw_segment,
            segment_num=segment_num,
            segment_duration=segment_duration,
            music_volume=music_volume,
            raw_audio_volume=raw_audio_volume
        )
        
        # Step 4: Update music offset for next segment
        self.music_offset += segment_duration
        
        # Cleanup raw segment (temp file)
        try:
            raw_segment.unlink()
        except Exception as e:
            log.debug(f"[segment] Could not delete temp file {raw_segment.name}: {e}")
        
        return final_segment
    
    def _concatenate_clips_with_transitions(
        self,
        clips: List[Path],
        segment_num: int,
        is_first_segment: bool,
        is_last_segment: bool
    ) -> Path:
        """
        Concatenate clips with crossfade transitions using FFmpeg xfade filter.

        Args:
            clips: List of clip paths to concatenate
            segment_num: Segment number for output naming
            is_first_segment: If True, add fade-in on first clip
            is_last_segment: If True, add fade-out on last clip

        Returns:
            Path to concatenated segment, or None on failure
        """
        if not clips:
            return None

        # Single clip - just copy with optional fade in/out
        if len(clips) == 1:
            return self._process_single_clip(
                clips[0], segment_num, is_first_segment, is_last_segment
            )

        # Get durations for all clips
        durations = [self._get_video_duration(clip) or CFG.CLIP_OUT_LEN_S for clip in clips]

        # Build xfade filter chain for video and audio
        video_filter, audio_filter, total_duration = self._build_xfade_filter(
            len(clips), durations, is_first_segment, is_last_segment
        )

        # Build FFmpeg command with all inputs
        output_path = self.project_dir / f"_middle_raw_{segment_num:02d}.mp4"
        self.temp_files.append(output_path)

        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]

        # Add all clip inputs
        for clip in clips:
            cmd.extend(["-i", str(clip)])

        # Apply filter complex
        filter_complex = f"{video_filter};{audio_filter}"
        cmd.extend([
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-map", "[aout]",
            "-c:v", get_optimal_video_codec(), "-b:v", CFG.BITRATE, "-maxrate", CFG.MAXRATE, "-bufsize", CFG.BUFSIZE, "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", AUDIO_SAMPLE_RATE,
            str(output_path)
        ])

        try:
            subprocess.run(cmd, check=True)
            log.info(
                f"[segment] Concatenated segment {segment_num} "
                f"({len(clips)} clips with crossfade transitions)"
            )
            return output_path
        except subprocess.CalledProcessError as e:
            log.error(f"[segment] Transition concatenation failed for segment {segment_num}: {e}")
            # Fallback to simple concat without transitions
            return self._concatenate_clips_simple(clips, segment_num)

    def _build_xfade_filter(
        self,
        num_clips: int,
        durations: List[float],
        fade_in: bool,
        fade_out: bool
    ) -> Tuple[str, str, float]:
        """
        Build FFmpeg xfade filter chain for video and acrossfade for audio.

        Returns:
            Tuple of (video_filter, audio_filter, total_duration)
        """
        xfade_dur = CROSSFADE_DURATION

        # Calculate offsets for each transition
        # offset[i] = sum of durations[0:i+1] - (i+1) * xfade_dur
        video_parts = []
        audio_parts = []

        # First clip
        current_video = "[0:v]"
        current_audio = "[0:a]"

        cumulative_duration = durations[0]

        for i in range(1, num_clips):
            # Offset is when the transition starts
            offset = cumulative_duration - xfade_dur

            # Video xfade
            out_video = f"[v{i}]" if i < num_clips - 1 else "[vxfade]"
            video_parts.append(
                f"{current_video}[{i}:v]xfade=transition=fade:duration={xfade_dur}:offset={offset:.3f}{out_video}"
            )
            current_video = out_video

            # Audio crossfade
            out_audio = f"[a{i}]" if i < num_clips - 1 else "[axfade]"
            audio_parts.append(
                f"{current_audio}[{i}:a]acrossfade=d={xfade_dur}:c1=tri:c2=tri{out_audio}"
            )
            current_audio = out_audio

            # Update cumulative duration (subtract overlap)
            cumulative_duration += durations[i] - xfade_dur

        # Build final video filter with optional fade in/out
        video_filter = ";".join(video_parts)

        if fade_in and fade_out:
            video_filter += f";[vxfade]fade=t=in:st=0:d={FADE_DURATION},fade=t=out:st={cumulative_duration - FADE_DURATION}:d={FADE_DURATION}[vout]"
        elif fade_in:
            video_filter += f";[vxfade]fade=t=in:st=0:d={FADE_DURATION}[vout]"
        elif fade_out:
            video_filter += f";[vxfade]fade=t=out:st={cumulative_duration - FADE_DURATION}:d={FADE_DURATION}[vout]"
        else:
            # Just rename the output
            video_filter = video_filter.replace("[vxfade]", "[vout]")

        # Build final audio filter with optional fade in/out
        audio_filter = ";".join(audio_parts)

        if fade_in and fade_out:
            audio_filter += f";[axfade]afade=t=in:st=0:d={FADE_DURATION},afade=t=out:st={cumulative_duration - FADE_DURATION}:d={FADE_DURATION}[aout]"
        elif fade_in:
            audio_filter += f";[axfade]afade=t=in:st=0:d={FADE_DURATION}[aout]"
        elif fade_out:
            audio_filter += f";[axfade]afade=t=out:st={cumulative_duration - FADE_DURATION}:d={FADE_DURATION}[aout]"
        else:
            audio_filter = audio_filter.replace("[axfade]", "[aout]")

        return video_filter, audio_filter, cumulative_duration

    def _process_single_clip(
        self,
        clip: Path,
        segment_num: int,
        fade_in: bool,
        fade_out: bool
    ) -> Path:
        """Process a single clip with optional fade in/out."""
        output_path = self.project_dir / f"_middle_raw_{segment_num:02d}.mp4"
        self.temp_files.append(output_path)

        duration = self._get_video_duration(clip) or CFG.CLIP_OUT_LEN_S

        # Build filter for fade in/out
        video_filter = ""
        audio_filter = ""

        if fade_in and fade_out:
            video_filter = f"fade=t=in:st=0:d={FADE_DURATION},fade=t=out:st={duration - FADE_DURATION}:d={FADE_DURATION}"
            audio_filter = f"afade=t=in:st=0:d={FADE_DURATION},afade=t=out:st={duration - FADE_DURATION}:d={FADE_DURATION}"
        elif fade_in:
            video_filter = f"fade=t=in:st=0:d={FADE_DURATION}"
            audio_filter = f"afade=t=in:st=0:d={FADE_DURATION}"
        elif fade_out:
            video_filter = f"fade=t=out:st={duration - FADE_DURATION}:d={FADE_DURATION}"
            audio_filter = f"afade=t=out:st={duration - FADE_DURATION}:d={FADE_DURATION}"

        if video_filter:
            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(clip),
                "-vf", video_filter,
                "-af", audio_filter,
                "-c:v", get_optimal_video_codec(), "-b:v", CFG.BITRATE, "-maxrate", CFG.MAXRATE, "-bufsize", CFG.BUFSIZE, "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-ar", AUDIO_SAMPLE_RATE,
                str(output_path)
            ]
        else:
            # No fades needed, just copy
            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(clip),
                "-c", "copy",
                str(output_path)
            ]

        try:
            subprocess.run(cmd, check=True)
            log.info(f"[segment] Processed single-clip segment {segment_num}")
            return output_path
        except subprocess.CalledProcessError as e:
            log.error(f"[segment] Single clip processing failed: {e}")
            return None

    def _concatenate_clips_simple(self, clips: List[Path], segment_num: int) -> Path:
        """Fallback: Concatenate clips using simple concat demuxer (no transitions)."""
        concat_list = self.working_dir / f"middle_list_{segment_num:02d}.txt"
        with concat_list.open("w") as f:
            for clip in clips:
                f.write(f"file '{clip.resolve()}'\n")

        self.temp_files.append(concat_list)

        output_path = self.project_dir / f"_middle_raw_{segment_num:02d}.mp4"
        self.temp_files.append(output_path)

        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-c", "copy",
            str(output_path)
        ]

        try:
            subprocess.run(cmd, check=True)
            log.warning(f"[segment] Used fallback concat for segment {segment_num} (no transitions)")
            return output_path
        except subprocess.CalledProcessError as e:
            log.error(f"[segment] Fallback concat failed for segment {segment_num}: {e}")
            return None
    
    def _add_continuous_music(
        self,
        video_path: Path,
        segment_num: int,
        segment_duration: float,
        music_volume: float,
        raw_audio_volume: float
    ) -> Path:
        """
        Add music overlay using continuous playback (no restart between segments).
        
        Uses -ss (seek start) to extract the correct portion of music for this segment.
        
        Args:
            video_path: Path to video segment
            segment_num: Segment number
            segment_duration: Duration of this segment in seconds
            music_volume: Music track volume
            raw_audio_volume: Camera audio volume
            
        Returns:
            Path to segment with music
        """
        output_path = self.project_dir / f"_middle_{segment_num:02d}.mp4"
        
        # If no music track selected, copy without music
        if not self.selected_music_track or not self.selected_music_track.exists():
            log.warning(f"[segment] No music for segment {segment_num}, creating video-only")
            return self._copy_without_music(video_path, output_path)
        
        # Calculate music seek position and duration
        music_start = self.music_offset
        music_duration = segment_duration
        
        log.info(
            f"[segment] Adding music to segment {segment_num}: "
            f"{self.selected_music_track.name} [{music_start:.1f}s-{music_start + music_duration:.1f}s]"
        )
        
        # Build FFmpeg command with music seek
        # Audio normalization: loudnorm to -16 LUFS (broadcast standard)
        # Then apply volume adjustments for mixing balance
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            # Video input
            "-i", str(video_path),
            # Music input with seek and loop
            "-ss", f"{music_start:.3f}",  # Start at current offset
            "-stream_loop", "-1",          # Loop if music is shorter than needed
            "-i", str(self.selected_music_track),
            # Audio mixing filter with normalization for consistent levels
            "-filter_complex",
            f"[0:a]loudnorm=I=-16:TP=-1.5:LRA=11,volume={raw_audio_volume}[raw];"
            f"[1:a]loudnorm=I=-16:TP=-1.5:LRA=11,volume={music_volume}[music];"
            f"[raw][music]amix=inputs=2:duration=first:dropout_transition=0[out]",
            # Output mapping
            "-map", "0:v", "-map", "[out]",
            # Output settings
            "-c:v", "copy",
            "-c:a", "aac", "-ar", AUDIO_SAMPLE_RATE,
            "-t", f"{segment_duration:.3f}",  # Limit to segment duration
            str(output_path)
        ]
        
        try:
            subprocess.run(cmd, check=True)
            return output_path
        except subprocess.CalledProcessError as e:
            log.error(f"[segment] Music overlay failed for segment {segment_num}: {e}")
            # Fallback: copy without music
            return self._copy_without_music(video_path, output_path)
    
    def _copy_without_music(self, source: Path, dest: Path) -> Path:
        """Copy video without music overlay (fallback)."""
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source),
            "-c", "copy",
            str(dest)
        ]
        
        try:
            subprocess.run(cmd, check=True)
            log.info(f"[segment] Created segment without music: {dest.name}")
            return dest
        except subprocess.CalledProcessError as e:
            log.error(f"[segment] Failed to copy segment: {e}")
            return None
    
    def _cleanup_temp_files(self):
        """Remove temporary files created during segment creation."""
        if not self.temp_files:
            return
        
        removed = 0
        for temp_file in self.temp_files:
            try:
                if temp_file.exists():
                    temp_file.unlink()
                    removed += 1
            except Exception as e:
                log.debug(f"[segment] Could not remove {temp_file.name}: {e}")
        
        if removed > 0:
            log.debug(f"[segment] Cleaned up {removed} temporary files")
        
        self.temp_files.clear()