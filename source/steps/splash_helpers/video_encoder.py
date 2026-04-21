# source/steps/splash_helpers/video_encoder.py
"""
FFmpeg video encoding utilities for splash sequences.
Handles clip creation, music overlay, and concatenation.
Uses hardware-accelerated encoding (VideoToolbox on Apple Silicon) via get_optimal_video_codec().
"""

from __future__ import annotations
import subprocess
import json
from pathlib import Path
from typing import List

from ...config import DEFAULT_CONFIG as CFG
from ...utils.log import setup_logger
from ...utils.hardware import get_optimal_video_codec

log = setup_logger("steps.splash_helpers.video_encoder")

AUDIO_SAMPLE_RATE = "48000"


class VideoEncoder:
    """Handles FFmpeg operations for splash video creation."""
    
    def __init__(self, temp_files_tracker: List[Path]):
        """
        Args:
            temp_files_tracker: List to track temporary files for cleanup
        """
        self.temp_files = temp_files_tracker
    
    def create_clip_from_image(
        self,
        image_path: Path,
        duration: float,
        output_path: Path,
        filter_vf: str = ""
    ) -> Path:
        """
        Create video clip from static image.
        
        Args:
            image_path: Path to source image
            duration: Clip duration in seconds
            output_path: Where to save output video
            filter_vf: Optional FFmpeg video filter
            
        Returns:
            Path to created video file
        """
        self.temp_files.append(output_path)
        
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-loop", "1", "-t", f"{duration:.2f}", "-framerate", "30",
            "-i", str(image_path),
            "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate={AUDIO_SAMPLE_RATE}",
            "-shortest"
        ]
        
        if filter_vf:
            cmd.extend(["-vf", filter_vf])
        
        cmd.extend([
            "-map", "0:v", "-map", "1:a",
            "-c:v", get_optimal_video_codec(), "-b:v", CFG.BITRATE, "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", AUDIO_SAMPLE_RATE, "-ac", "2",
            str(output_path)
        ])

        log.debug(f"[encoder] Creating clip: {output_path.name} ({duration:.2f}s)")
        subprocess.run(cmd, check=True)
        return output_path
    
    def create_color_clip(
        self,
        color: str,
        size: tuple[int, int],
        duration: float,
        output_path: Path
    ) -> Path:
        """
        Create solid color video clip.
        
        Args:
            color: Color name (e.g., "black")
            size: (width, height) in pixels
            duration: Clip duration in seconds
            output_path: Where to save output
            
        Returns:
            Path to created video file
        """
        self.temp_files.append(output_path)
        
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"color=c={color}:s={size[0]}x{size[1]}:d={duration}",
            "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate={AUDIO_SAMPLE_RATE}",
            "-shortest",
            "-c:v", get_optimal_video_codec(), "-b:v", CFG.BITRATE, "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", AUDIO_SAMPLE_RATE, "-ac", "2",
            str(output_path)
        ]

        log.debug(f"[encoder] Creating color clip: {output_path.name}")
        subprocess.run(cmd, check=True)
        return output_path
    
    def concatenate_clips(
        self,
        clip_paths: List[Path],
        output_path: Path,
        concat_list_path: Path
    ) -> Path:
        """
        Concatenate multiple video clips.
        
        Args:
            clip_paths: List of video file paths to concatenate
            output_path: Where to save concatenated video
            concat_list_path: Temporary file for FFmpeg concat list
            
        Returns:
            Path to concatenated video
        """
        self.temp_files.append(concat_list_path)
        self.temp_files.append(output_path)
        
        # Write concat list
        with concat_list_path.open("w") as f:
            for clip in clip_paths:
                f.write(f"file '{clip.resolve()}'\n")
        
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_list_path),
            "-c", "copy",
            str(output_path)
        ]
        
        log.info(f"[encoder] Concatenating {len(clip_paths)} clips → {output_path.name}")
        subprocess.run(cmd, check=True)
        return output_path
    
    def add_music_overlay(
        self,
        video_path: Path,
        music_path: Path,
        output_path: Path
    ) -> Path:
        """
        Add music track to video with duration matching.
        
        Args:
            video_path: Source video file
            music_path: Music file to overlay
            output_path: Where to save output with music
            
        Returns:
            Path to video with music
        """
        if not music_path.exists():
            log.warning(f"[encoder] Music not found: {music_path}, copying video without music")
            subprocess.run(["cp", str(video_path), str(output_path)], check=True)
            return output_path
        
        # Get video duration
        duration = self._get_video_duration(video_path)
        if duration == 0:
            log.warning(f"[encoder] Could not determine duration for {video_path}")
            subprocess.run(["cp", str(video_path), str(output_path)], check=True)
            return output_path
        
        log.info(f"[encoder] Adding music: {music_path.name} ({duration:.2f}s)")
        
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(video_path),
            "-i", str(music_path),
            "-filter_complex", "[1:a]volume=1.0[music]",
            "-map", "0:v", "-map", "[music]",
            "-t", f"{duration:.3f}",
            "-c:v", "copy",
            "-c:a", "aac", "-ar", AUDIO_SAMPLE_RATE, "-ac", "2",
            str(output_path)
        ]
        
        subprocess.run(cmd, check=True)
        return output_path
    
    def _get_video_duration(self, video_path: Path) -> float:
        """Get video duration in seconds using ffprobe."""
        try:
            result = subprocess.run([
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_entries", "format=duration", str(video_path)
            ], capture_output=True, text=True, check=True)
            
            data = json.loads(result.stdout)
            return float(data.get("format", {}).get("duration", 0))
        except Exception as e:
            log.warning(f"[encoder] ffprobe failed: {e}")
            return 0.0