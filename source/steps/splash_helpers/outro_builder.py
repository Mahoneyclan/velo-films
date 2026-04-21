# source/steps/splash_helpers/outro_builder.py
"""
Outro sequence builder: collage → text → logo → black.
Uses hardware-accelerated encoding (VideoToolbox on Apple Silicon) via get_optimal_video_codec().
"""

from __future__ import annotations
from pathlib import Path
from typing import List
from PIL import Image

from ...config import DEFAULT_CONFIG as CFG
from ...utils.log import setup_logger
from ...utils.hardware import get_optimal_video_codec
from .collage_builder import CollageBuilder
from .video_encoder import VideoEncoder

log = setup_logger("steps.splash_helpers.outro_builder")

# Canvas constants — match pipeline output resolution
OUT_W = CFG.OUTPUT_W
OUT_H = CFG.OUTPUT_H
BANNER_HEIGHT = 220 * OUT_H // 1440
LOGO_PATH = CFG.PROJECT_ROOT / "assets" / "velo_films.png"
FONT_FILE = "/Library/Fonts/Arial.ttf"

# Outro timing
OUTRO_DURATION_S = 3.7
OUTRO_TITLE_TEXT = "Velo Films"
OUTRO_TITLE_APPEAR_T = 1.0
OUTRO_TITLE_FADEIN_D = 0.5
OUTRO_FADEOUT_START_T = 3.0
OUTRO_FADEOUT_D = 0.7


class OutroBuilder:
    """Builds outro sequence: collage+text → logo → black."""
    
    def __init__(self, assets_dir: Path, temp_files_tracker: List[Path]):
        """
        Args:
            assets_dir: Directory for persistent assets
            temp_files_tracker: List for tracking temp files
        """
        self.assets_dir = assets_dir
        self.temp_files = temp_files_tracker
        self.encoder = VideoEncoder(temp_files_tracker)
    
    def build_outro(
        self,
        frame_images: List[Path],
        output_path: Path
    ) -> Path:
        """
        Build complete outro sequence.
        
        Args:
            frame_images: List of frame image paths for collage
            output_path: Where to save final outro video
            
        Returns:
            Path to completed outro video
        """
        log.info("[outro] Building outro sequence...")
        
        # 1. Build collage
        collage_path = self._build_collage(frame_images)
        
        # 2. Collage with animated text
        collage_clip = self._build_collage_with_text(collage_path)
        
        # 3. Logo clip
        logo_clip = self.encoder.create_clip_from_image(
            LOGO_PATH,
            2.0,
            self.assets_dir / "splash_close_logo.mp4",
            filter_vf=f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease,pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2:black"
        )
        
        # 4. Black screen
        black_clip = self.encoder.create_color_clip(
            "black",
            (OUT_W, OUT_H),
            2.0,
            self.assets_dir / "splash_close_black.mp4"
        )
        
        # 5. Concatenate
        concat_list = self.assets_dir / "splash_close_concat.txt"
        self.temp_files.append(concat_list)
        
        temp_outro = self.assets_dir / "_outro_temp.mp4"
        self.temp_files.append(temp_outro)
        
        self.encoder.concatenate_clips(
            [collage_clip, logo_clip, black_clip],
            temp_outro,
            concat_list
        )
        
        # 6. Add music
        final_outro = self.encoder.add_music_overlay(temp_outro, CFG.OUTRO_MUSIC, output_path)
        
        log.info(f"[outro] Complete: {final_outro}")
        return final_outro
    
    def _build_collage(self, frame_images: List[Path]) -> Path:
        """Build and save collage image."""
        builder = CollageBuilder(OUT_W, OUT_H - BANNER_HEIGHT)
        collage = builder.build_collage(frame_images)
        
        collage_path = self.assets_dir / "close_splash_collage.png"
        collage.save(collage_path, quality=95)
        log.info(f"[outro] Saved collage: {collage_path}")
        return collage_path
    
    def _build_collage_with_text(self, collage_path: Path) -> Path:
        """Create collage clip with animated text overlay."""
        output = self.assets_dir / "splash_close_collage.mp4"
        self.temp_files.append(output)
        
        # Build FFmpeg filter for text animation
        alpha_expr = (
            f"if(lt(t,{OUTRO_TITLE_APPEAR_T}),0,"
            f" if(lt(t,{OUTRO_TITLE_APPEAR_T + OUTRO_TITLE_FADEIN_D}),"
            f"(t-{OUTRO_TITLE_APPEAR_T})/{OUTRO_TITLE_FADEIN_D},1))"
        )
        
        drawtext = (
            "drawtext="
            f"fontfile='{FONT_FILE}':text='{OUTRO_TITLE_TEXT}':"
            f"x=(w-text_w)/2:y=(h-text_h)/2:fontsize={160 * OUT_W // 2560}:fontcolor=white:"
            "bordercolor=black@0.45:borderw=6:shadowcolor=black@0.7:shadowx=4:shadowy=4:"
            f"alpha='{alpha_expr}'"
        )
        
        fade = f"fade=t=out:st={OUTRO_FADEOUT_START_T}:d={OUTRO_FADEOUT_D}:alpha=0"
        
        vf = (
            f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease,"
            f"pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2,{drawtext},{fade}"
        )
        
        # Create clip with text overlay
        import subprocess
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-loop", "1", "-t", f"{OUTRO_DURATION_S:.2f}", "-framerate", "30",
            "-i", str(collage_path),
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-shortest",
            "-vf", vf,
            "-map", "0:v", "-map", "1:a",
            "-c:v", get_optimal_video_codec(), "-b:v", CFG.BITRATE, "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "48000", "-ac", "2",
            str(output)
        ]
        
        log.info(f"[outro] Creating collage with text animation ({OUTRO_DURATION_S:.2f}s)")
        subprocess.run(cmd, check=True)
        return output