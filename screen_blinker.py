"""Optional full-screen blue visual alert shared by all beep triggers."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import threading


LOG = logging.getLogger(__name__)


class ScreenBlinker(threading.Thread):
    """Show a short blue full-screen overlay twice for each queued alert.

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
        flash_seconds: float = 0.16,
        gap_seconds: float = 0.12,
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

    def _wait(self, seconds: float) -> bool:
        return self.stop_event.wait(seconds)

    def _blink_twice(self) -> None:
        """Show a native, no-activation blue overlay above the game window."""

        if not hasattr(ctypes, "windll"):
            LOG.warning("screen blink alert requires Windows")
            return
        try:
            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32
            kernel32 = ctypes.windll.kernel32
            window_proc_type = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                wintypes.WPARAM, wintypes.LPARAM,
            )

            class WindowClass(ctypes.Structure):
                _fields_ = [
                    ("style", wintypes.UINT),
                    ("lpfnWndProc", window_proc_type),
                    ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE),
                    ("hIcon", wintypes.HANDLE),
                    ("hCursor", wintypes.HANDLE),
                    ("hbrBackground", wintypes.HANDLE),
                    ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR),
                ]

            # Explicit pointer-sized signatures are essential on 64-bit
            # Windows. ctypes otherwise treats HWNDs as 32-bit integers and
            # the overlay can be created with a truncated, unusable handle.
            user32.DefWindowProcW.argtypes = (
                wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
            )
            user32.DefWindowProcW.restype = ctypes.c_ssize_t
            user32.RegisterClassW.argtypes = (ctypes.POINTER(WindowClass),)
            user32.RegisterClassW.restype = wintypes.ATOM
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
            gdi32.CreateSolidBrush.argtypes = (wintypes.COLORREF,)
            gdi32.CreateSolidBrush.restype = wintypes.HBRUSH

            def window_proc(hwnd, message, wparam, lparam):
                return user32.DefWindowProcW(hwnd, message, wparam, lparam)

            callback = window_proc_type(window_proc)
            brush = gdi32.CreateSolidBrush(0x00D77800)  # Windows COLORREF BGR
            if not brush:
                raise OSError("could not create blue overlay brush")
            instance = kernel32.GetModuleHandleW(None)
            class_name = f"MapleAssistantBlueAlert{threading.get_ident()}"
            window_class = WindowClass(
                0, callback, 0, 0, instance, None, None, brush,
                None, class_name,
            )
            if not user32.RegisterClassW(ctypes.byref(window_class)):
                error = ctypes.get_last_error()
                if error not in (0, 1410):  # class already registered
                    raise OSError(error, "could not register blue overlay")

            # Full virtual desktop covers the game even when it is on another
            # monitor. The no-activate and tool-window flags avoid stealing
            # keyboard focus or appearing on the taskbar.
            left = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
            top = user32.GetSystemMetrics(77)    # SM_YVIRTUALSCREEN
            width = max(1, user32.GetSystemMetrics(78))
            height = max(1, user32.GetSystemMetrics(79))
            hwnd = user32.CreateWindowExW(
                0x00000008 | 0x00000080 | 0x08000000,  # topmost/tool/no activate
                class_name, None, 0x80000000,  # WS_POPUP
                left, top, width, height, None, None, instance, None,
            )
            if not hwnd:
                raise OSError(ctypes.get_last_error(), "could not create blue overlay")
        except Exception:
            LOG.warning("screen blink alert is unavailable", exc_info=True)
            return
        try:
            for _ in range(self.flashes_per_alert):
                if self.stop_event.is_set() or not self.enabled:
                    return
                user32.SetWindowPos(
                    hwnd, ctypes.c_void_p(-1), left, top, width, height,
                    0x0010 | 0x0040,
                )
                user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
                user32.UpdateWindow(hwnd)
                if self._wait(self.flash_seconds):
                    return
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
