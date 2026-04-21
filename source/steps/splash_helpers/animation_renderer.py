# source/steps/splash_helpers/animation_renderer.py
"""
Flip animation renderer for splash intro sequence.
Creates smooth tile-flip transitions from map to collage.
Uses hardware-accelerated encoding (VideoToolbox on Apple Silicon) via get_optimal_video_codec().
"""

from __future__ import annotations
import math
from concurrent.futures import ThreadPoolExecutor
from os import cpu_count
from pathlib import Path
from typing import List, Tuple
from PIL import Image

from ...utils.log import setup_logger
from ...config import DEFAULT_CONFIG as CFG
from ...utils.hardware import get_optimal_video_codec

log = setup_logger("steps.splash_helpers.animation_renderer")


class AnimationRenderer:
    """Renders flip animation frames for map-to-collage transition."""
    
    def __init__(
        self,
        base_image: Image.Image,
        banner_height: int,
        fps: int = 30
    ):
        """
        Args:
            base_image: Base canvas image with banner
            banner_height: Height of banner area (tiles start below this)
            fps: Frames per second for animation
        """
        self.base_image = base_image
        self.banner_height = banner_height
        self.fps = fps
    
    def extract_map_tiles(
        self,
        grid_info: Tuple[int, int, int, int]
    ) -> List[Image.Image]:
        """
        Extract map tiles from base image.
        
        Args:
            grid_info: (cols, rows, tile_w, tile_h) from CollageBuilder
            
        Returns:
            List of tile images cut from base
        """
        cols, rows, tile_w, tile_h = grid_info
        tiles = []
        
        for row in range(rows):
            for col in range(cols):
                x0 = col * tile_w
                y0 = self.banner_height + row * tile_h
                x1 = x0 + tile_w
                y1 = y0 + tile_h
                
                tile = self.base_image.crop((x0, y0, x1, y1))
                tiles.append(tile)
        
        log.debug(f"[animation] Extracted {len(tiles)} map tiles")
        return tiles
    
    def prepare_frame_tiles(
        self,
        image_paths: List[Path],
        grid_info: Tuple[int, int, int, int]
    ) -> List[Image.Image]:
        """
        Load and resize frame images to tile size.
        
        Args:
            image_paths: List of frame image paths
            grid_info: (cols, rows, tile_w, tile_h)
            
        Returns:
            List of resized tile images (None for missing slots)
        """
        cols, rows, tile_w, tile_h = grid_info
        total_slots = cols * rows
        tiles = []
        
        for i in range(total_slots):
            if i < len(image_paths):
                try:
                    src = Image.open(image_paths[i]).convert("RGB")
                    tile = src.resize((tile_w, tile_h), Image.Resampling.LANCZOS)
                    tiles.append(tile)
                except Exception as e:
                    log.warning(f"[animation] Failed to load frame {i}: {e}")
                    tiles.append(None)
            else:
                tiles.append(None)
        
        log.debug(f"[animation] Prepared {len(tiles)} frame tiles")
        return tiles
    
    def render_flip_sequence(
        self,
        map_tiles: List[Image.Image],
        frame_tiles: List[Image.Image],
        grid_info: Tuple[int, int, int, int],
        duration: float = 1.2
    ) -> List[Image.Image]:
        """
        Render flip animation from map tiles to frame tiles.
        
        Args:
            map_tiles: Tiles from map image
            frame_tiles: Tiles from frame collage
            grid_info: (cols, rows, tile_w, tile_h)
            duration: Animation duration in seconds
            
        Returns:
            List of rendered animation frames
        """
        cols, rows, tile_w, tile_h = grid_info
        num_frames = max(1, int(round(duration * self.fps)))
        
        # Build slot position lookup
        slots = []
        for row in range(rows):
            for col in range(cols):
                x = col * tile_w
                y = self.banner_height + row * tile_h
                slots.append((col, row, x, y))
        
        log.info(f"[animation] Rendering {num_frames} flip frames ({duration:.2f}s @ {self.fps}fps)")
        
        # Render each frame
        frames = []
        for frame_idx in range(num_frames):
            # Calculate transition progress (0.0 to 1.0)
            t = frame_idx / (num_frames - 1) if num_frames > 1 else 1.0
            
            # First half: shrink map tiles, second half: grow frame tiles
            midpoint = 0.5
            if t <= midpoint:
                # Shrinking map tiles (scale from 1.0 to 0.0)
                scale_x = 1.0 - (t / midpoint)
                use_map = True
            else:
                # Growing frame tiles (scale from 0.0 to 1.0)
                scale_x = (t - midpoint) / midpoint
                use_map = False
            
            # Create frame by compositing tiles
            frame = self.base_image.copy()
            
            for slot_idx, (col, row, x0, y0) in enumerate(slots):
                if slot_idx >= len(map_tiles):
                    break
                
                # Select source tile
                if use_map or frame_tiles[slot_idx] is None:
                    tile_src = map_tiles[slot_idx]
                else:
                    tile_src = frame_tiles[slot_idx]
                
                if tile_src is None:
                    continue
                
                # Apply horizontal scale
                w_scaled = max(1, int(round(tile_w * scale_x)))
                
                if w_scaled == tile_w:
                    # Full width - paste directly
                    frame.paste(tile_src, (x0, y0))
                else:
                    # Scaled - resize and center
                    tile_scaled = tile_src.resize((w_scaled, tile_h), Image.Resampling.LANCZOS)
                    x_center = x0 + (tile_w - w_scaled) // 2
                    frame.paste(tile_scaled, (x_center, y0))
            
            frames.append(frame)
        
        log.info(f"[animation] Rendered {len(frames)} frames")
        return frames
    
    def encode_frames_to_video(
        self,
        frames: List[Image.Image],
        output_path: Path,
        temp_dir: Path
    ) -> Path:
        """
        Encode frame sequence to video file.
        
        Args:
            frames: List of PIL Images
            output_path: Where to save video
            temp_dir: Directory for temporary frame PNGs
            
        Returns:
            Path to encoded video
        """
        import subprocess
        
        # Save frames as PNGs (parallelized for performance)
        temp_dir.mkdir(parents=True, exist_ok=True)

        def save_frame(args: Tuple[int, Image.Image]) -> None:
            idx, img = args
            frame_path = temp_dir / f"flip_{idx:04d}.png"
            img.save(frame_path, quality=95)

        num_workers = min(cpu_count() or 4, 8)
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            executor.map(save_frame, enumerate(frames))
        
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-framerate", str(self.fps),
            "-i", str(temp_dir / "flip_%04d.png"),
            "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=48000",
            "-shortest",
            "-map", "0:v", "-map", "1:a",
            "-c:v", get_optimal_video_codec(), "-b:v", CFG.BITRATE, "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "48000", "-ac", "2",
            str(output_path)
        ]
        
        log.info(f"[animation] Encoding {len(frames)} frames → {output_path.name}")
        subprocess.run(cmd, check=True)
        return output_path