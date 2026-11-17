"""The 启动中。。。 hint shown while the assistant is starting.

A hidden launch (``pythonw`` / packaged EXE) shows nothing until the dashboard is
built: worker imports, logging setup, the licence handshake and the Tk window all
happen first.  This module puts a small box in the top-left corner of the primary
screen for exactly that interval - background ``#f3f3f3``, text ``#00a4fa``, no
border - and removes it the moment the window is mapped.

Design notes, each one paid for in the field:

* **No Tk.**  A first version used a ``tkinter`` root in a background thread; that
  set ``tkinter._default_root``, so the application's own ``tk.Tk()`` never became
  the default root and the dashboard's first ``tk.StringVar()`` attached to the
  hint's interpreter: ``RuntimeError: main thread is not in main loop`` killed the
  UI at startup (15:30 log).  A Win32 window shares no state with Tk.
* **A plain painted window, not a layered one.**  ``UpdateLayeredWindow`` renders
  the box flat white as soon as the window is shown or re-positioned (the blue
  text is lost), and this hint MUST be re-positioned to stay on top.  A normal
  ``WS_POPUP`` window paints itself on ``WM_PAINT``, so a z-order change cannot
  destroy its content.
* **Topmost re-asserted.**  ``WS_EX_TOPMOST`` alone was not enough in the field
  ("it was covered by a desktop icon"), so the hint raises itself with
  ``SetWindowPos(HWND_TOPMOST)`` and repeats that every ``TOP_UP_INTERVAL_SECONDS``.
* **Harmless.**  Click-through (``WS_EX_TRANSPARENT``), never activated
  (``WS_EX_NOACTIVATE``), no taskbar entry, every failure swallowed, and it removes
  itself after ``MAX_SECONDS`` if nobody calls ``close()``.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import threading
import time

LOG = logging.getLogger(__name__)

TEXT = "启动中..."
BACKGROUND = (243, 243, 243)          # #f3f3f3
TEXT_COLOUR = (0, 164, 250)           # #00a4fa
FONT_FACE = "Microsoft YaHei UI"
FONT_HEIGHT_PX = 22
FONT_WEIGHT = 600                     # semibold
PADDING_X = 10
PADDING_Y = 6
MARGIN_X = 10
MARGIN_Y = 6
# Rounded corners: the window is clipped by a rounded region, so the desktop shows
# through the corners and the box reads as a rounded card instead of a hard block.
CORNER_DIAMETER = 20
POLL_SECONDS = 0.05
TOP_UP_INTERVAL_SECONDS = 0.5
MAX_SECONDS = 90.0

_CLASS_NAME = "MoonEclipseStartupHint"

# ---- Win32 ---------------------------------------------------------------
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
)

WS_POPUP = 0x80000000
WS_EX_TRANSPARENT = 0x00000020      # click-through: never eats a click
WS_EX_TOOLWINDOW = 0x00000080       # no taskbar entry
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000       # never steals focus from the game
SW_SHOWNOACTIVATE = 4
HWND_TOPMOST = -1
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
SWP_NOOWNERZORDER = 0x0200
SWP_SHOWWINDOW = 0x0040
WM_PAINT = 0x000F
WM_ERASEBKGND = 0x0014
PM_REMOVE = 0x0001
DT_CENTER = 0x0001
DT_VCENTER = 0x0004
DT_SINGLELINE = 0x0020
DT_NOPREFIX = 0x0800
TRANSPARENT = 1
DEFAULT_CHARSET = 1
CLEARTYPE_QUALITY = 5
ERROR_CLASS_ALREADY_EXISTS = 1410


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG), ("top", wintypes.LONG),
        ("right", wintypes.LONG), ("bottom", wintypes.LONG),
    ]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", wintypes.HDC),
        ("fErase", wintypes.BOOL),
        ("rcPaint", RECT),
        ("fRestore", wintypes.BOOL),
        ("fIncUpdate", wintypes.BOOL),
        ("rgbReserved", ctypes.c_ubyte * 32),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", POINT),
        ("lPrivate", wintypes.DWORD),
    ]


_user32 = getattr(ctypes, "WinDLL")("user32", use_last_error=True)
_gdi32 = getattr(ctypes, "WinDLL")("gdi32", use_last_error=True)
_kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)

_user32.CreateWindowExW.restype = wintypes.HWND
_user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
_user32.RegisterClassExW.restype = wintypes.ATOM
_user32.DefWindowProcW.restype = LRESULT
_user32.DefWindowProcW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
]
_user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
_user32.BeginPaint.restype = wintypes.HDC
_user32.BeginPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
_user32.EndPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
_user32.GetDC.restype = wintypes.HDC
_user32.GetDC.argtypes = [wintypes.HWND]
_user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.SetWindowPos.restype = wintypes.BOOL
_user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
_user32.DestroyWindow.argtypes = [wintypes.HWND]
_user32.PeekMessageW.argtypes = [
    ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT,
]
_user32.DrawTextW.restype = ctypes.c_int
_user32.DrawTextW.argtypes = [
    wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(RECT), wintypes.UINT,
]
_user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(RECT), wintypes.HBRUSH]
_user32.SetWindowRgn.restype = ctypes.c_int
_user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HRGN, wintypes.BOOL]
_gdi32.CreateRoundRectRgn.restype = wintypes.HRGN
_gdi32.CreateRoundRectRgn.argtypes = [
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
]
_gdi32.GetTextExtentPoint32W.restype = wintypes.BOOL
_gdi32.GetTextExtentPoint32W.argtypes = [
    wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(SIZE),
]
_gdi32.CreateFontW.restype = wintypes.HFONT
_gdi32.CreateFontW.argtypes = [
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR,
]
_gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
_gdi32.CreateSolidBrush.argtypes = [wintypes.DWORD]
_gdi32.SelectObject.restype = wintypes.HGDIOBJ
_gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
_gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
_gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.DWORD]
_gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]

_closed = threading.Event()
_started_at: float | None = None
_thread: threading.Thread | None = None
_lock = threading.Lock()
# Kept so the window procedure can paint without touching module state.
_font = 0
_brush = 0
_text = TEXT


def _colourref(rgb: tuple[int, int, int]) -> int:
    red, green, blue = rgb
    return red | (green << 8) | (blue << 16)


def _wndproc(hwnd, message, wparam, lparam):  # noqa: ANN001 - Win32 callback
    if message == WM_PAINT:
        paint = PAINTSTRUCT()
        dc = _user32.BeginPaint(hwnd, ctypes.byref(paint))
        try:
            area = RECT()
            _user32.GetClientRect(hwnd, ctypes.byref(area))
            # Fill the background HERE, not via WM_ERASEBKGND: returning 1 from
            # the erase message told Windows the area was cleared, nothing then
            # painted it, and the box came up as black with blue text on it.
            if _brush:
                _user32.FillRect(dc, ctypes.byref(area), _brush)
            _gdi32.SetBkMode(dc, TRANSPARENT)
            _gdi32.SetTextColor(dc, _colourref(TEXT_COLOUR))
            previous = _gdi32.SelectObject(dc, _font) if _font else None
            try:
                _user32.DrawTextW(
                    dc, _text, len(_text), ctypes.byref(area),
                    DT_CENTER | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX,
                )
            finally:
                if previous:
                    _gdi32.SelectObject(dc, previous)
        finally:
            _user32.EndPaint(hwnd, ctypes.byref(paint))
        return 0
    return _user32.DefWindowProcW(hwnd, message, wparam, lparam)


_WNDPROC = WNDPROC(_wndproc)


def since_launch() -> float | None:
    """Seconds since the hint appeared, or ``None`` when it never did."""

    if _started_at is None:
        return None
    return time.perf_counter() - _started_at


def show(text: str = TEXT) -> bool:
    """Display the hint; returns whether a hint was actually started."""

    global _thread, _started_at, _text
    with _lock:
        if _thread is not None or _closed.is_set():
            return False
        _text = text
        _started_at = time.perf_counter()
        _thread = threading.Thread(
            target=_run, name="startup-splash", daemon=True,
        )
        _thread.start()
        return True


def close(reason: str = "window ready") -> None:
    """Remove the hint (idempotent, thread-safe, never raises)."""

    _closed.set()
    elapsed = since_launch()
    if elapsed is not None:
        LOG.info("STARTUP: 启动提示 removed (%s) after %.1fs", reason, elapsed)


def _run() -> None:
    try:
        _draw()
    except Exception:  # noqa: BLE001 - a hint must never break a launch
        LOG.debug("startup hint could not be shown", exc_info=True)


def _measure(screen_dc: int, font: int, text: str) -> tuple[int, int]:
    """Text extent in pixels for the chosen font."""

    extent = SIZE()
    previous = _gdi32.SelectObject(screen_dc, font)
    try:
        _gdi32.GetTextExtentPoint32W(screen_dc, text, len(text), ctypes.byref(extent))
    finally:
        _gdi32.SelectObject(screen_dc, previous)
    return int(extent.cx), int(extent.cy)


def _draw() -> None:
    global _font, _brush
    if _closed.is_set():
        return
    instance = _kernel32.GetModuleHandleW(None)
    brush = _gdi32.CreateSolidBrush(_colourref(BACKGROUND))
    _brush = brush
    window_class = WNDCLASSEXW()
    window_class.cbSize = ctypes.sizeof(WNDCLASSEXW)
    window_class.lpfnWndProc = _WNDPROC
    window_class.hInstance = instance
    window_class.hbrBackground = brush
    window_class.lpszClassName = _CLASS_NAME
    if not _user32.RegisterClassExW(ctypes.byref(window_class)):
        if ctypes.get_last_error() != ERROR_CLASS_ALREADY_EXISTS:
            _gdi32.DeleteObject(brush)
            _brush = 0
            return

    _font = _gdi32.CreateFontW(
        -FONT_HEIGHT_PX, 0, 0, 0, FONT_WEIGHT,
        0, 0, 0, DEFAULT_CHARSET, 0, 0, CLEARTYPE_QUALITY, 0, FONT_FACE,
    )
    screen_dc = _user32.GetDC(None)
    try:
        text_width, text_height = _measure(screen_dc, _font, _text)
    finally:
        _user32.ReleaseDC(None, screen_dc)
    width = text_width + PADDING_X * 2
    height = text_height + PADDING_Y * 2

    hwnd = _user32.CreateWindowExW(
        WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT,
        _CLASS_NAME, "MoonEclipse", WS_POPUP,
        MARGIN_X, MARGIN_Y, width, height,
        None, None, instance, None,
    )
    if not hwnd:
        return
    try:
        # Rounded card: the system owns the region after a successful call.
        region = _gdi32.CreateRoundRectRgn(
            0, 0, width + 1, height + 1, CORNER_DIAMETER, CORNER_DIAMETER,
        )
        if region:
            _user32.SetWindowRgn(hwnd, region, True)
        # Shown and raised while it paints itself on demand: a plain window keeps
        # its content when the z-order changes, which the layered version did not.
        _user32.SetWindowPos(
            hwnd, HWND_TOPMOST, MARGIN_X, MARGIN_Y, width, height,
            SWP_NOACTIVATE | SWP_NOOWNERZORDER | SWP_SHOWWINDOW,
        )
        _user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        _pump(hwnd)
    finally:
        _user32.DestroyWindow(hwnd)
        _gdi32.DeleteObject(brush)
        _brush = 0
        if _font:
            _gdi32.DeleteObject(_font)
            _font = 0


def _raise_to_top(hwnd: int) -> None:
    """Re-assert the topmost band; z-order only, the window repaints itself."""

    try:
        _user32.SetWindowPos(
            hwnd, HWND_TOPMOST, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER,
        )
    except Exception:  # noqa: BLE001 - cosmetic only
        LOG.debug("startup hint could not re-assert topmost", exc_info=True)


def _pump(hwnd: int) -> None:
    """Keep the window alive and on top until ``close()`` (or the timeout)."""

    message = MSG()
    deadline = time.monotonic() + MAX_SECONDS
    next_top_up = time.monotonic() + TOP_UP_INTERVAL_SECONDS
    while not _closed.is_set() and time.monotonic() < deadline:
        while _user32.PeekMessageW(ctypes.byref(message), None, 0, 0, PM_REMOVE):
            _user32.TranslateMessage(ctypes.byref(message))
            _user32.DispatchMessageW(ctypes.byref(message))
        now = time.monotonic()
        if now >= next_top_up:
            _raise_to_top(hwnd)          # anything above it is pushed back below
            next_top_up = now + TOP_UP_INTERVAL_SECONDS
        time.sleep(POLL_SECONDS)
