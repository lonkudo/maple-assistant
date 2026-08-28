"""Optional full-screen red visual alert shared by all beep triggers."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import threading
from typing import Iterable, Sequence


LOG = logging.getLogger(__name__)


class ScreenBlinker(threading.Thread):
    """Show a short red full-screen overlay twice for each queued alert.

    The overlay is a native topmost Win32 window. This keeps it above the game
    client; a Tk window created on a background worker may be hidden behind a
    game window or not painted at all.
    """

    def __init__(
        self,
        stop_event: threading.Event,
        *,
        enabled: bool = False,
        flashes_per_alert: int = 2,
        flash_seconds: float = 0.5,
        gap_seconds: float = 0.3,
    ) -> None:
        super().__init__(name="screen-blinker", daemon=True)
        self.stop_event = stop_event
        self.flashes_per_alert = max(1, int(flashes_per_alert))
        self.flash_seconds = max(0.02, float(flash_seconds))
        self.gap_seconds = max(0.02, float(gap_seconds))
        self._lock = threading.Lock()
        self._enabled = bool(enabled)
        self._pending = 0
        self._wake_event = threading.Event()

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = bool(enabled)
            if not self._enabled:
                self._pending = 0
        self._wake_event.set()
        LOG.info("screen blink alert %s", "enabled" if enabled else "disabled")

    def request_blink(self) -> None:
        """Queue the visual half of one alarm without delaying its caller."""

        with self._lock:
            if not self._enabled:
                return
            self._pending += 1
        self._wake_event.set()

    def show_detection_regions(
        self,
        window_rect: tuple[int, int, int, int],
        image_size: tuple[int, int],
        regions: Iterable[tuple[str, tuple[int, int, int, int], int]],
    ) -> None:
        """Briefly outline capture regions over the selected game client.

        This is an always-on start-of-patrol visual check, separate from the
        optional red alert setting.  Boxes use pixels in the full client
        capture and are converted to screen pixels with the captured client
        rectangle, so the outlines remain correct at every game resolution.
        """

        image_width, image_height = image_size
        left, top, right, bottom = window_rect
        client_width = right - left
        client_height = bottom - top
        if image_width <= 0 or image_height <= 0 or client_width <= 0 or client_height <= 0:
            return
        screen_regions: list[tuple[str, tuple[int, int, int, int], int]] = []
        for name, box, color in regions:
            box_left, box_top, box_right, box_bottom = box
            x1 = left + round(box_left * client_width / image_width)
            y1 = top + round(box_top * client_height / image_height)
            x2 = left + round(box_right * client_width / image_width)
            y2 = top + round(box_bottom * client_height / image_height)
            if x2 > x1 and y2 > y1:
                screen_regions.append((name, (x1, y1, x2, y2), int(color)))
        if not screen_regions:
            return
        threading.Thread(
            target=self._flash_detection_regions,
            args=(tuple(screen_regions),),
            name="detection-region-overlay",
            daemon=True,
        ).start()

    def _flash_detection_regions(
        self,
        regions: Sequence[tuple[str, tuple[int, int, int, int], int]],
    ) -> None:
        """Draw two no-activation border flashes without covering the game."""

        if not hasattr(ctypes, "windll"):
            LOG.warning("detection-region overlay requires Windows")
            return
        windows: list[tuple[int, int]] = []
        try:
            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32
            kernel32 = ctypes.windll.kernel32
            user32.CreateWindowExW.argtypes = (
                wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                wintypes.HINSTANCE, wintypes.LPVOID,
            )
            user32.CreateWindowExW.restype = wintypes.HWND
            user32.SetWindowPos.argtypes = (
                wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, wintypes.UINT,
            )
            user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
            user32.DestroyWindow.argtypes = (wintypes.HWND,)
            user32.GetDC.argtypes = (wintypes.HWND,)
            user32.GetDC.restype = wintypes.HDC
            user32.ReleaseDC.argtypes = (wintypes.HWND, wintypes.HDC)
            user32.GetClientRect.argtypes = (
                wintypes.HWND, ctypes.POINTER(wintypes.RECT),
            )
            user32.FillRect.argtypes = (
                wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH,
            )
            gdi32.CreateSolidBrush.argtypes = (wintypes.COLORREF,)
            gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
            instance = kernel32.GetModuleHandleW(None)
            border = 3
            for _name, (left, top, right, bottom), color in regions:
                brush = gdi32.CreateSolidBrush(color)
                if not brush:
                    continue
                # Four small built-in STATIC windows form a transparent-style
                # outline without a custom Win32 message callback.
                for x, y, width, height in (
                    (left, top, right - left, border),
                    (left, bottom - border, right - left, border),
                    (left, top, border, bottom - top),
                    (right - border, top, border, bottom - top),
                ):
                    hwnd = user32.CreateWindowExW(
                        0x00000008 | 0x00000080 | 0x08000000,
                        "STATIC", None, 0x80000000,
                        x, y, max(1, width), max(1, height),
                        None, None, instance, None,
                    )
                    if hwnd:
                        windows.append((hwnd, brush))
            if not windows:
                return
            for flash_index in range(2):
                for hwnd, brush in windows:
                    user32.SetWindowPos(
                        hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0,
                        0x0001 | 0x0002 | 0x0010 | 0x0040,
                    )
                    user32.ShowWindow(hwnd, 4)
                    rect = wintypes.RECT()
                    hdc = user32.GetDC(hwnd)
                    if hdc:
                        try:
                            user32.GetClientRect(hwnd, ctypes.byref(rect))
                            user32.FillRect(hdc, ctypes.byref(rect), brush)
                        finally:
                            user32.ReleaseDC(hwnd, hdc)
                if self._wait(0.45):
                    return
                if flash_index == 0:
                    for hwnd, _brush in windows:
                        user32.ShowWindow(hwnd, 0)
                    if self._wait(0.20):
                        return
        except Exception:
            LOG.warning("detection-region overlay failed", exc_info=True)
        finally:
            brushes: set[int] = set()
            for hwnd, brush in windows:
                try:
                    user32.DestroyWindow(hwnd)
                except Exception:
                    pass
                brushes.add(int(brush))
            for brush in brushes:
                try:
                    gdi32.DeleteObject(brush)
                except Exception:
                    pass

    def _wait(self, seconds: float) -> bool:
        return self.stop_event.wait(seconds)

    def _blink_twice(self) -> None:
        """Show a native, no-activation red overlay above the game window."""

        if not hasattr(ctypes, "windll"):
            LOG.warning("screen blink alert requires Windows")
            return
        try:
            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32
            kernel32 = ctypes.windll.kernel32
            # Explicit pointer-sized signatures are essential on 64-bit
            # Windows. ctypes otherwise treats HWNDs as 32-bit integers and
            # the overlay can be created with a truncated, unusable handle.
            user32.CreateWindowExW.argtypes = (
                wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                wintypes.HINSTANCE, wintypes.LPVOID,
            )
            user32.CreateWindowExW.restype = wintypes.HWND
            user32.SetWindowPos.argtypes = (
                wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, wintypes.UINT,
            )
            user32.SetWindowPos.restype = wintypes.BOOL
            user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
            user32.UpdateWindow.argtypes = (wintypes.HWND,)
            user32.DestroyWindow.argtypes = (wintypes.HWND,)
            user32.GetDC.argtypes = (wintypes.HWND,)
            user32.GetDC.restype = wintypes.HDC
            user32.ReleaseDC.argtypes = (wintypes.HWND, wintypes.HDC)
            user32.GetClientRect.argtypes = (
                wintypes.HWND, ctypes.POINTER(wintypes.RECT),
            )
            user32.FillRect.argtypes = (
                wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH,
            )
            gdi32.CreateSolidBrush.argtypes = (wintypes.COLORREF,)
            gdi32.CreateSolidBrush.restype = wintypes.HBRUSH

            brush = gdi32.CreateSolidBrush(0x000000FF)  # Windows COLORREF BGR: red
            if not brush:
                raise OSError("could not create red overlay brush")
            instance = kernel32.GetModuleHandleW(None)
            # Full virtual desktop covers the game even when it is on another
            # monitor. The no-activate and tool-window flags avoid stealing
            # keyboard focus or appearing on the taskbar.
            left = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
            top = user32.GetSystemMetrics(77)    # SM_YVIRTUALSCREEN
            width = max(1, user32.GetSystemMetrics(78))
            height = max(1, user32.GetSystemMetrics(79))
            hwnd = user32.CreateWindowExW(
                0x00000008 | 0x00000080 | 0x08000000,  # topmost/tool/no activate
                "STATIC", None, 0x80000000,  # built-in class + WS_POPUP
                left, top, width, height, None, None, instance, None,
            )
            if not hwnd:
                raise OSError(ctypes.get_last_error(), "could not create red overlay")
        except Exception:
            LOG.warning("screen blink alert is unavailable", exc_info=True)
            return
        try:
            for flash_index in range(self.flashes_per_alert):
                if self.stop_event.is_set() or not self.enabled:
                    return
                user32.SetWindowPos(
                    hwnd, ctypes.c_void_p(-1), left, top, width, height,
                    0x0010 | 0x0040,
                )
                user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
                user32.UpdateWindow(hwnd)
                # Explicitly fill for EVERY flash. Relying on the class
                # background after a hide/show can leave a white repaint on
                # the second flash on some Windows desktop themes. The built-
                # in STATIC class avoids retaining a Python window callback
                # after a previous alert has ended.
                rect = wintypes.RECT()
                hdc = user32.GetDC(hwnd)
                if hdc:
                    try:
                        user32.GetClientRect(hwnd, ctypes.byref(rect))
                        user32.FillRect(hdc, ctypes.byref(rect), brush)
                    finally:
                        user32.ReleaseDC(hwnd, hdc)
                if self._wait(self.flash_seconds):
                    return
                if flash_index + 1 >= self.flashes_per_alert:
                    break
                user32.ShowWindow(hwnd, 0)  # SW_HIDE
                if self._wait(self.gap_seconds):
                    return
        except Exception:
            LOG.warning("screen blink alert failed", exc_info=True)
        finally:
            try:
                user32.DestroyWindow(hwnd)
            except Exception:
                pass
            try:
                gdi32.DeleteObject(brush)
            except Exception:
                pass

    def run(self) -> None:
        LOG.info("screen blinker started")
        while not self.stop_event.is_set():
            self._wake_event.wait(0.25)
            self._wake_event.clear()
            while not self.stop_event.is_set():
                with self._lock:
                    if not self._enabled or self._pending <= 0:
                        break
                    self._pending -= 1
                self._blink_twice()
        LOG.info("screen blinker stopped")


__all__ = ["ScreenBlinker"]
