"""Optional full-screen red visual reminder shared by all alert events."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import threading
import time
from typing import Iterable, Optional, Sequence


LOG = logging.getLogger(__name__)

_LAYER_COLORS_RGB = (
    (255, 70, 70),
    (255, 180, 45),
    (70, 220, 110),
    (55, 190, 255),
    (185, 90, 255),
    (255, 85, 190),
)


def _colorref(rgb: tuple[int, int, int]) -> int:
    """Convert RGB to Win32 COLORREF (0x00bbggrr)."""

    red, green, blue = rgb
    return int(red) | (int(green) << 8) | (int(blue) << 16)


def _shade(rgb: tuple[int, int, int], amount: int) -> tuple[int, int, int]:
    return tuple(max(0, min(255, channel + amount)) for channel in rgb)


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
        # The live aim marker of 自动过测谎 (see show_aim_marker): one crosshair overlay that follows
        # the point the api pass is driving the cursor to.
        self._aim_lock = threading.Lock()
        self._aim_point: Optional[tuple[int, int, float]] = None
        self._aim_size = 46
        self._aim_thread: Optional[threading.Thread] = None

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

    def show_layer_bands(
        self,
        window_rect: tuple[int, int, int, int],
        image_size: tuple[int, int],
        analysis_box: tuple[int, int, int, int],
        bands: Iterable[tuple[str, tuple[float, float]]],
        *,
        wait_until_hidden: bool = False,
    ) -> None:
        """Overlay every normalized marker-Y band on the live minimap.

        Each layer receives a distinct translucent vertical colour gradient.
        Overlap becomes visibly darker/mixed while the no-activate native
        windows leave keyboard focus on the game.
        """

        image_width, image_height = image_size
        client_left, client_top, client_right, client_bottom = window_rect
        client_width = client_right - client_left
        client_height = client_bottom - client_top
        analysis_left, analysis_top, analysis_right, analysis_bottom = analysis_box
        analysis_width = analysis_right - analysis_left
        analysis_height = analysis_bottom - analysis_top
        if (image_width <= 0 or image_height <= 0
                or client_width <= 0 or client_height <= 0
                or analysis_width <= 0 or analysis_height <= 0):
            return
        screen_bands = []
        for index, (name, (upper_y, lower_y)) in enumerate(bands):
            upper = max(0.0, min(1.0, float(upper_y)))
            lower = max(upper, min(1.0, float(lower_y)))
            pixel_box = (
                analysis_left,
                analysis_top + round(upper * analysis_height),
                analysis_right,
                analysis_top + round(lower * analysis_height),
            )
            x1 = client_left + round(pixel_box[0] * client_width / image_width)
            y1 = client_top + round(pixel_box[1] * client_height / image_height)
            x2 = client_left + round(pixel_box[2] * client_width / image_width)
            y2 = client_top + round(pixel_box[3] * client_height / image_height)
            if x2 <= x1 or y2 <= y1:
                continue
            base = _LAYER_COLORS_RGB[index % len(_LAYER_COLORS_RGB)]
            screen_bands.append((
                name,
                (x1, y1, x2, y2),
                _colorref(_shade(base, 35)),
                _colorref(_shade(base, -35)),
            ))
        if not screen_bands:
            return
        if wait_until_hidden:
            self._show_layer_band_regions(tuple(screen_bands))
        else:
            threading.Thread(
                target=self._show_layer_band_regions,
                args=(tuple(screen_bands),),
                name="layer-band-overlay",
                daemon=True,
            ).start()

    def show_patrol_points(
        self,
        window_rect: tuple[int, int, int, int],
        image_size: tuple[int, int],
        analysis_box: tuple[int, int, int, int],
        points: Iterable[tuple[str, float, float]],
    ) -> None:
        """Show concise, click-through markers for recorded patrol points.

        ``jump_left`` and ``jump_right`` are 45-degree arrows in their travel
        direction, ``rope`` is an up arrow, and endpoints are vertical bars.
        This is start-of-patrol feedback only: it has no capture or input role.
        """

        image_width, image_height = image_size
        client_left, client_top, client_right, client_bottom = window_rect
        client_width = client_right - client_left
        client_height = client_bottom - client_top
        analysis_left, analysis_top, analysis_right, analysis_bottom = analysis_box
        analysis_width = analysis_right - analysis_left
        analysis_height = analysis_bottom - analysis_top
        if (image_width <= 0 or image_height <= 0 or client_width <= 0
                or client_height <= 0 or analysis_width <= 0 or analysis_height <= 0):
            return
        screen_points: list[tuple[str, int, int]] = []
        for kind, x, y in points:
            try:
                point_x = max(0.0, min(1.0, float(x)))
                point_y = max(0.0, min(1.0, float(y)))
            except (TypeError, ValueError):
                continue
            pixel_x = analysis_left + round(point_x * analysis_width)
            pixel_y = analysis_top + round(point_y * analysis_height)
            screen_points.append((
                str(kind),
                client_left + round(pixel_x * client_width / image_width),
                client_top + round(pixel_y * client_height / image_height),
            ))
        if not screen_points:
            return
        kinds = ", ".join(kind for kind, _x, _y in screen_points)
        LOG.info("patrol-point overlay: drawing %d marker(s): %s", len(screen_points), kinds)
        threading.Thread(
            target=self._show_patrol_point_markers,
            args=(tuple(screen_points),),
            name="patrol-point-overlay",
            daemon=True,
        ).start()

    def _show_patrol_point_markers(self, points: Sequence[tuple[str, int, int]]) -> None:
        """Render compact patrol markers without taking game focus."""

        if not hasattr(ctypes, "windll"):
            return
        windows: list[tuple[int, int, int]] = []
        user32 = gdi32 = None
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
            user32.SetLayeredWindowAttributes.argtypes = (
                wintypes.HWND, wintypes.COLORREF, ctypes.c_ubyte, wintypes.DWORD,
            )
            user32.GetDC.argtypes = (wintypes.HWND,)
            user32.GetDC.restype = wintypes.HDC
            user32.ReleaseDC.argtypes = (wintypes.HWND, wintypes.HDC)
            user32.DestroyWindow.argtypes = (wintypes.HWND,)
            gdi32.CreateSolidBrush.argtypes = (wintypes.COLORREF,)
            gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
            gdi32.DeleteObject.argtypes = (wintypes.HGDIOBJ,)
            gdi32.Polygon.argtypes = (wintypes.HDC, ctypes.POINTER(wintypes.POINT), ctypes.c_int)
            gdi32.Polygon.restype = wintypes.BOOL
            instance = kernel32.GetModuleHandleW(None)
            black = _colorref((0, 0, 0))
            colours = {
                "jump_left": _colorref((50, 235, 120)),
                "jump_right": _colorref((50, 235, 120)),
                "rope": _colorref((245, 205, 35)),
                "left_endpoint": _colorref((45, 140, 245)),
                "right_endpoint": _colorref((45, 140, 245)),
            }
            for kind, x, y in points:
                hwnd = user32.CreateWindowExW(
                    0x00000008 | 0x00000080 | 0x08000000 | 0x00080000 | 0x00000020,
                    "STATIC", None, 0x80000000,
                    x - 7, y - 7, 15, 15, None, None, instance, None,
                )
                if not hwnd:
                    continue
                background = gdi32.CreateSolidBrush(black)
                marker = gdi32.CreateSolidBrush(colours.get(kind, colours["jump_right"]))
                windows.append((hwnd, background, marker))
                # Black is transparent. Green diagonal arrows show the jump
                # direction, yellow means climb Up, and blue bars are ends.
                user32.SetLayeredWindowAttributes(hwnd, black, 255, 0x00000001)
                user32.ShowWindow(hwnd, 4)
                hdc = user32.GetDC(hwnd)
                if hdc:
                    try:
                        rect = wintypes.RECT(0, 0, 15, 15)
                        user32.FillRect(hdc, ctypes.byref(rect), background)
                        if kind in ("left_endpoint", "right_endpoint"):
                            bar = wintypes.RECT(6, 1, 9, 14)
                            user32.FillRect(hdc, ctypes.byref(bar), marker)
                        else:
                            old = gdi32.SelectObject(hdc, marker)
                            def draw_polygon(coords: tuple[tuple[int, int], ...]) -> None:
                                polygon = (wintypes.POINT * len(coords))()
                                for index, (px, py) in enumerate(coords):
                                    polygon[index] = wintypes.POINT(px, py)
                                gdi32.Polygon(hdc, polygon, len(coords))

                            if kind == "jump_right":
                                # Separate arrow head and diagonal stem are
                                # intentionally non-overlapping polygons;
                                # GDI can discard self-crossing polygons.
                                draw_polygon(((14, 0), (14, 7), (8, 1)))
                                draw_polygon(((8, 3), (11, 6), (5, 12), (2, 9)))
                            elif kind == "jump_left":
                                draw_polygon(((0, 0), (0, 7), (6, 1)))
                                draw_polygon(((6, 3), (3, 6), (9, 12), (12, 9)))
                            else:  # rope: a conventional upward arrow
                                draw_polygon(((7, 0), (14, 7), (10, 7), (10, 14),
                                              (4, 14), (4, 7), (0, 7)))
                            gdi32.SelectObject(hdc, old)
                    finally:
                        user32.ReleaseDC(hwnd, hdc)
            self._wait(5.0)
        except Exception:
            LOG.warning("patrol-point overlay failed", exc_info=True)
        finally:
            for hwnd, background, arrow in windows:
                try:
                    if user32 is not None:
                        user32.DestroyWindow(hwnd)
                except Exception:
                    pass
                for brush in (background, arrow):
                    try:
                        if gdi32 is not None:
                            gdi32.DeleteObject(brush)
                    except Exception:
                        pass

    def _show_layer_band_regions(
        self,
        bands: Sequence[
            tuple[str, tuple[int, int, int, int], int, int]
        ],
    ) -> None:
        """Render one solid translucent strip per band, plus a brighter 1 px edge line each side.

        It used to draw every band as EIGHT gradient stripes.  The operator's bands are only ~1.6 px
        tall (floors 5.3 px apart on the minimap), so most stripes rounded onto the same rows and their
        translucent colours blended into a single smear - his report: "the drawing on minimap tells me
        that it mixed".  A band that short is now a single solid line of its own colour, and a genuine
        overlap between two floors shows as two distinct colours instead of one blend.  Taller bands
        keep the translucent body with brighter top and bottom edges.
        """

        if not hasattr(ctypes, "windll"):
            LOG.warning("layer-band overlay requires Windows")
            return
        windows: list[tuple[int, int, int]] = []
        user32 = None
        gdi32 = None
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
            user32.SetLayeredWindowAttributes.argtypes = (
                wintypes.HWND, wintypes.COLORREF, ctypes.c_ubyte, wintypes.DWORD,
            )
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
            for name, (left, top, right, bottom), top_color, bottom_color in bands:
                height = max(1, bottom - top)
                LOG.info(
                    "LAYER BAND OVERLAY: %s screen=(%d,%d,%d,%d) height=%dpx",
                    name, left, top, right, bottom, height,
                )
                # Body (translucent tint) + one bright edge line per side.  The old 8-stripe gradient
                # collapsed onto single rows for bands this short and blended the floors together.
                body_color = 0
                for shift in (0, 8, 16):
                    start = (top_color >> shift) & 0xFF
                    end = (bottom_color >> shift) & 0xFF
                    body_color |= round((start + end) / 2.0) << shift
                parts = [
                    (left, top, max(1, right - left), max(1, height), body_color, 120),
                ]
                # The operator's bands are only ~2 px tall, so an edge line on each side would land on
                # the same rows as the body and hide it completely.  Only bands tall enough to have
                # spare rows get the bright edges; a thin band is simply its own solid line.
                if height >= 4:
                    parts.append((left, top, max(1, right - left), 1, top_color, 200))
                    parts.append(
                        (left, max(top, bottom - 1), max(1, right - left), 1, bottom_color, 200)
                    )
                parts = tuple(parts)
                for x, y, width, part_height, color, alpha in parts:
                    brush = gdi32.CreateSolidBrush(color)
                    if not brush:
                        continue
                    hwnd = user32.CreateWindowExW(
                        # topmost, tool, no-activate, layered, CLICK-THROUGH (0x20):
                        # without it the overlay swallows every click that lands on it, so the
                        # auto-reconnect's clicks never reached the game while an alarm flashed.
                        0x00000008 | 0x00000080 | 0x08000000 | 0x00080000 | 0x00000020,
                        "STATIC", None, 0x80000000,
                        x, y, max(1, width), max(1, part_height),
                        None, None, instance, None,
                    )
                    if hwnd:
                        windows.append((hwnd, brush, alpha))
                    else:
                        gdi32.DeleteObject(brush)
            for hwnd, brush, alpha in windows:
                user32.SetLayeredWindowAttributes(hwnd, 0, alpha, 0x00000002)
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
            self._wait(2.2)
        except Exception:
            LOG.warning("layer-band overlay failed", exc_info=True)
        finally:
            for hwnd, brush, _alpha in windows:
                try:
                    if user32 is not None:
                        user32.DestroyWindow(hwnd)
                except Exception:
                    pass
                try:
                    if gdi32 is not None:
                        gdi32.DeleteObject(brush)
                except Exception:
                    pass

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
                        # topmost/tool/no-activate + CLICK-THROUGH: the outline is decoration and
                        # must never take a click that belongs to the game.
                        0x00000008 | 0x00000080 | 0x08000000 | 0x00000020,
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

    # ------------------------------------------------------------------ the live aim marker
    def show_aim_marker(self, screen_x: int, screen_y: int, *, ttl_seconds: float = 2.0,
                        size: int = 46) -> None:
        """Draw a crosshair on the game at a SCREEN point - the aim of the api pass.

        The 测试api video drill draws the answer it aims at inside its own window ("draw the aim"); the
        automatic pass runs against the live game, so the equivalent is a small click-through overlay
        at the answered point.  It shows WHAT the pass is aiming at (and that it really takes control),
        it never takes focus, and it disappears by itself when the pass stops updating it.
        """

        if not hasattr(ctypes, "windll"):
            return
        with self._aim_lock:
            self._aim_point = (int(screen_x), int(screen_y),
                               time.monotonic() + max(0.1, float(ttl_seconds)))
            # API aiming remains large by default; stationary-position
            # confirmation intentionally requests a compact crosshair.
            self._aim_size = max(8, int(size))
        thread = self._aim_thread
        if thread is None or not thread.is_alive():
            self._aim_thread = threading.Thread(target=self._aim_marker_loop,
                                                name="aim-marker-overlay", daemon=True)
            self._aim_thread.start()

    def _aim_marker_loop(self) -> None:
        """Own the crosshair overlay window until the aim stops being updated."""

        if not hasattr(ctypes, "windll"):
            return
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        kernel32 = ctypes.windll.kernel32
        user32.CreateWindowExW.argtypes = (
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
        )
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.SetLayeredWindowAttributes.argtypes = (
            wintypes.HWND, wintypes.COLORREF, ctypes.c_ubyte, wintypes.DWORD,
        )
        user32.SetWindowPos.argtypes = (
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        )
        user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        user32.DestroyWindow.argtypes = (wintypes.HWND,)
        user32.GetDC.argtypes = (wintypes.HWND,)
        user32.GetDC.restype = wintypes.HDC
        user32.ReleaseDC.argtypes = (wintypes.HWND, wintypes.HDC)
        user32.FillRect.argtypes = (wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH)
        gdi32.CreateSolidBrush.argtypes = (wintypes.COLORREF,)
        gdi32.CreateSolidBrush.restype = wintypes.HBRUSH

        key_colour = 0x00FF00FF                       # COLORREF magenta = the transparent colour key
        key_brush = gdi32.CreateSolidBrush(key_colour)
        bar_brush = gdi32.CreateSolidBrush(0x000000FF)     # red bars
        dot_brush = gdi32.CreateSolidBrush(0x00FFFFFF)     # white centre
        hwnd = 0
        shown = False
        last_seen = 0.0
        try:
            while not self.stop_event.is_set():
                with self._aim_lock:
                    aim = self._aim_point
                    size = self._aim_size
                now = time.monotonic()
                if aim is None:
                    break
                x, y, expires = aim
                if expires < now:
                    if shown:
                        user32.ShowWindow(hwnd, 0)
                        shown = False
                    if now - last_seen > 5.0:
                        break                          # nothing new for a long time: give up the window
                    time.sleep(0.05)
                    continue
                last_seen = now
                if not hwnd:
                    hwnd = user32.CreateWindowExW(
                        # topmost, tool window, NO-ACTIVATE, layered, CLICK-THROUGH:
                        # the marker must never take the keyboard or swallow a click.
                        0x00000008 | 0x00000080 | 0x08000000 | 0x00080000 | 0x00000020,
                        "STATIC", None, 0x80000000,
                        x - size // 2, y - size // 2, size, size, None, None,
                        kernel32.GetModuleHandleW(None), None,
                    )
                    if not hwnd:
                        LOG.warning("aim marker overlay could not be created")
                        break
                    user32.SetLayeredWindowAttributes(hwnd, key_colour, 235, 0x00000001)
                user32.SetWindowPos(hwnd, ctypes.c_void_p(-1), x - size // 2, y - size // 2,
                                    size, size, 0x0010 | 0x0040)
                if not shown:
                    user32.ShowWindow(hwnd, 4)          # SW_SHOWNOACTIVATE
                    shown = True
                hdc = user32.GetDC(hwnd)
                if hdc:
                    try:
                        whole = wintypes.RECT(0, 0, size, size)
                        user32.FillRect(hdc, ctypes.byref(whole), key_brush)
                        middle = size // 2
                        horizontal = wintypes.RECT(0, middle - 1, size, middle + 1)
                        vertical = wintypes.RECT(middle - 1, 0, middle + 1, size)
                        user32.FillRect(hdc, ctypes.byref(horizontal), bar_brush)
                        user32.FillRect(hdc, ctypes.byref(vertical), bar_brush)
                        centre = wintypes.RECT(middle - 2, middle - 2, middle + 2, middle + 2)
                        user32.FillRect(hdc, ctypes.byref(centre), dot_brush)
                    finally:
                        user32.ReleaseDC(hwnd, hdc)
                time.sleep(0.05)
        except Exception:
            LOG.warning("aim marker overlay failed", exc_info=True)
        finally:
            try:
                if hwnd:
                    user32.DestroyWindow(hwnd)
            except Exception:
                pass
            for brush in (key_brush, bar_brush, dot_brush):
                try:
                    gdi32.DeleteObject(brush)
                except Exception:
                    pass
            with self._aim_lock:
                self._aim_point = None

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
                # topmost/tool/no-activate + CLICK-THROUGH (0x20).  This one covers the WHOLE
                # virtual desktop, so without 0x20 it swallowed every click during the flash -
                # exactly the window in which the auto-reconnect starts clicking.
                0x00000008 | 0x00000080 | 0x08000000 | 0x00000020,
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
