# source/steps/splash_helpers/intro_builder.py
"""
Intro sequence builder: logo → map → flip → collage.
"""

from __future__ import annotations
from pathlib import Path
from typing import List, Tuple
from PIL import Image, ImageDraw, ImageFont

from ...config import DEFAULT_CONFIG as CFG
from ...utils.log import setup_logger
from ...utils.gpx import load_gpx, compute_stats
from ...utils.map_overlay import render_splash_map_with_xy
from .collage_builder import CollageBuilder
from .video_encoder import VideoEncoder
from .animation_renderer import AnimationRenderer

log = setup_logger("steps.splash_helpers.intro_builder")

# Canvas constants — match pipeline output resolution
OUT_W = CFG.OUTPUT_W
OUT_H = CFG.OUTPUT_H
BANNER_HEIGHT = 220 * OUT_H // 1440
TITLE_FONT_SIZE = 80 * OUT_W // 2560
STATS_FONT_SIZE = 55 * OUT_W // 2560
FONT_FILE = "/Library/Fonts/Arial.ttf"
LOGO_PATH = CFG.PROJECT_ROOT / "assets" / "velo_films.png"


class IntroBuilder:
    """Builds intro sequence: logo → map+banner → flip → collage."""
    
    def __init__(self, assets_dir: Path, temp_files_tracker: List[Path]):
        """
        Args:
            assets_dir: Directory for persistent assets
            temp_files_tracker: List for tracking temp files
        """
        self.assets_dir = assets_dir
        self.temp_files = temp_files_tracker
        self.encoder = VideoEncoder(temp_files_tracker)
    
    def build_intro(
        self,
        frame_images: List[Path],
        grid_info: Tuple[int, int, int, int],
        output_path: Path
    ) -> Path:
        """
        Build complete intro sequence.
        
        Args:
            frame_images: List of frame image paths for collage
            grid_info: (cols, rows, tile_w, tile_h) from CollageBuilder
            output_path: Where to save final intro video
            
        Returns:
            Path to completed intro video
        """
        log.info("[intro] Building intro sequence...")
        
        # 1. Logo clip
        logo_clip = self._build_logo_clip()
        
        # 2. Map with banner
        map_canvas = self._build_map_canvas()
        map_still = self.assets_dir / "splash_open_map.png"
        self.temp_files.append(map_still)
        map_canvas.save(map_still, quality=95)
        map_clip = self.encoder.create_clip_from_image(map_still, 2.0, self.assets_dir / "splash_open_map.mp4")
        
        # 3. Flip animation
        flip_clip = self._build_flip_animation(map_canvas, frame_images, grid_info)
        
        # 4. Collage still
        collage_clip = self._build_collage_clip(frame_images, grid_info)
        
        # 5. Concatenate all parts
        concat_list = self.assets_dir / "splash_open_concat.txt"
        self.temp_files.append(concat_list)
        
        temp_intro = self.assets_dir / "_intro_temp.mp4"
        self.temp_files.append(temp_intro)
        
        self.encoder.concatenate_clips(
            [logo_clip, map_clip, flip_clip, collage_clip],
            temp_intro,
            concat_list
        )
        
        # 6. Add music
        final_intro = self.encoder.add_music_overlay(temp_intro, CFG.INTRO_MUSIC, output_path)
        
        log.info(f"[intro] Complete: {final_intro}")
        return final_intro
    
    def _build_logo_clip(self) -> Path:
        """Create logo intro clip."""
        output = self.assets_dir / "splash_open_logo.mp4"
        return self.encoder.create_clip_from_image(
            LOGO_PATH,
            2.0,
            output,
            filter_vf=f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease,pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2:black"
        )
    
    def _build_map_canvas(self) -> Image.Image:
        """Create map canvas with ride banner."""
        canvas = Image.new("RGB", (OUT_W, OUT_H), (0, 0, 0))
        draw = ImageDraw.Draw(canvas, "RGBA")
        
        # Banner background
        draw.rectangle([0, 0, OUT_W, BANNER_HEIGHT], fill=(0, 0, 0, 200))
        
        # Ride title
        ride_name = CFG.RIDE_FOLDER
        title_font = self._safe_font(TITLE_FONT_SIZE)
        tw = int(draw.textlength(ride_name, font=title_font))
        draw.text(((OUT_W - tw) // 2, 30), ride_name, font=title_font, fill=(255, 255, 255))
        
        # Stats line
        try:
            gpx_pts = load_gpx(str(CFG.GPX_FILE if CFG.GPX_FILE.exists() else CFG.INPUT_GPX_FILE))
            stats = compute_stats(gpx_pts)
            
            d_s = int(stats.get("duration_s", 0))
            h = d_s // 3600
            m = (d_s % 3600) // 60
            banner_text = (
                f"Distance: {stats.get('distance_km', 0):.1f} km   "
                f"Duration: {h}h {m}m   "
                f"Avg: {stats.get('avg_speed', 0):.1f} km/h   "
                f"Ascent: {stats.get('total_climb_m', 0):.0f} m"
            )
            
            stats_font = self._safe_font(STATS_FONT_SIZE)
            tw2 = int(draw.textlength(banner_text, font=stats_font))
            draw.text(((OUT_W - tw2) // 2, 120), banner_text, font=stats_font, fill=(255, 255, 255))
        except Exception as e:
            log.warning(f"[intro] Could not load GPX stats: {e}")
        
        # Map overlay - centered, preserving aspect ratio
        try:
            gpx_pts = load_gpx(str(CFG.GPX_FILE if CFG.GPX_FILE.exists() else CFG.INPUT_GPX_FILE))
            if gpx_pts:
                map_area_h = OUT_H - BANNER_HEIGHT
                base, _ = render_splash_map_with_xy(gpx_pts, size=(OUT_W, map_area_h))
                # Center the map (may be smaller than requested due to aspect ratio)
                map_w, map_h = base.size
                x_offset = (OUT_W - map_w) // 2
                y_offset = BANNER_HEIGHT + (map_area_h - map_h) // 2
                canvas.paste(base, (x_offset, y_offset))
        except Exception as e:
            log.warning(f"[intro] Could not render map: {e}")
        
        return canvas
    
    def _build_flip_animation(
        self,
        map_canvas: Image.Image,
        frame_images: List[Path],
        grid_info: Tuple[int, int, int, int]
    ) -> Path:
        """Build flip animation clip."""
        renderer = AnimationRenderer(map_canvas, BANNER_HEIGHT)
        
        # Extract tiles
        map_tiles = renderer.extract_map_tiles(grid_info)
        frame_tiles = renderer.prepare_frame_tiles(frame_images, grid_info)
        
        # Render animation
        flip_frames = renderer.render_flip_sequence(map_tiles, frame_tiles, grid_info, duration=1.2)
        
        # Encode
        temp_dir = self.assets_dir / "flip_frames"
        self.temp_files.append(temp_dir)
        
        output = self.assets_dir / "splash_open_flip.mp4"
        return renderer.encode_frames_to_video(flip_frames, output, temp_dir)
    
    def _build_collage_clip(
        self,
        frame_images: List[Path],
        grid_info: Tuple[int, int, int, int]
    ) -> Path:
        """Build collage still clip."""
        # Build collage
        builder = CollageBuilder(OUT_W, OUT_H - BANNER_HEIGHT)
        collage = builder.build_collage(frame_images)
        
        # Add black banner
        canvas_full = Image.new("RGB", (OUT_W, OUT_H), (0, 0, 0))
        canvas_full.paste(collage, (0, BANNER_HEIGHT))
        
        # Save persistent collage
        collage_path = self.assets_dir / "splash_open_collage.png"
        canvas_full.save(collage_path, quality=95)
        log.info(f"[intro] Saved collage: {collage_path}")
        
        # Create video clip
        output = self.assets_dir / "splash_open_collage.mp4"
        return self.encoder.create_clip_from_image(collage_path, 2.0, output)
    
    def _safe_font(self, size: int):
        """Load font with fallback."""
        try:
            return ImageFont.truetype(FONT_FILE, size)
        except Exception:
            return ImageFont.load_default()