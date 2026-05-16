"""PC-side video sink utilities for frame display and monitoring."""

from __future__ import annotations

from typing import Optional


class FrameDisplay:
    """Display sink supporting OpenCV (live) and headless (no-op) modes."""
    
    def __init__(self, display_mode: str = "opencv", width: int = 640, height: int = 480) -> None:
        """Initialize frame display sink.
        
        Args:
            display_mode: "opencv" for live display or "headless" for no-op logging
            width: Frame width in pixels (for buffer reshaping)
            height: Frame height in pixels (for buffer reshaping)
        """
        self._mode = display_mode
        self._width = width
        self._height = height
        self._cv2 = None
        self._window = "OS-VideoSDR"
        self._frame_count = 0
        
        if display_mode == "opencv":
            try:
                import cv2  # type: ignore
                self._cv2 = cv2
                self._cv2.namedWindow(self._window, self._cv2.WINDOW_NORMAL)
            except Exception as exc:
                print(f"OpenCV not available ({exc}); falling back to headless mode")
                self._mode = "headless"
        elif display_mode != "headless":
            raise ValueError(f"Unsupported display_mode: {display_mode}")
    
    def show(self, frame: bytes, frame_id: Optional[int] = None, format_hint: str = "gray8") -> None:
        """Display or log frame.
        
        Args:
            frame: Raw frame bytes
            frame_id: Optional frame identifier for logging
            format_hint: "gray8" (default), "rgb24" (future)
        """
        self._frame_count += 1
        
        if self._mode == "headless":
            # No-op mode: just count frames and log periodically
            if self._frame_count % 30 == 0:
                fid_str = f"frame={frame_id}" if frame_id else f"count={self._frame_count}"
                print(f"[display] {fid_str} bytes={len(frame)}")
            return
        
        # OpenCV mode
        if self._cv2 is None:
            return
        
        try:
            import numpy as np  # type: ignore
            
            if format_hint == "gray8":
                # Grayscale: reshape as single-channel 8-bit image
                pixels = self._width * self._height
                src = frame[:pixels]
                if len(src) < pixels:
                    src = src + b"\x00" * (pixels - len(src))
                img = np.frombuffer(src, dtype=np.uint8).reshape((self._height, self._width))
                self._cv2.imshow(self._window, img)
            elif format_hint == "rgb24":
                # RGB24: reshape as 3-channel 8-bit image
                pixels = self._width * self._height * 3
                src = frame[:pixels]
                if len(src) < pixels:
                    src = src + b"\x00" * (pixels - len(src))
                img = np.frombuffer(src, dtype=np.uint8).reshape((self._height, self._width, 3))
                self._cv2.imshow(self._window, img)
            else:
                print(f"Unsupported format hint: {format_hint}")
                return
            
            # Non-blocking wait (1ms per frame)
            self._cv2.waitKey(1)
        
        except Exception as exc:
            print(f"Display frame failed ({format_hint}): {exc}")
    
    def show_gray(self, frame: bytes) -> None:
        """Legacy grayscale-only display (for compatibility)."""
        self.show(frame, format_hint="gray8")
    
    def close(self) -> None:
        """Clean up display resources."""
        if self._cv2 is not None:
            self._cv2.destroyAllWindows()
        if self._frame_count > 0:
            print(f"[display] Total frames displayed: {self._frame_count}")
