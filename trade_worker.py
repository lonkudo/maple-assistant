"""Independent trade automation: virtual clicks, presence checks, and overlays."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

import numpy as np

from countdown_worker import play_mp3


LOG = logging.getLogger(__name__)

REFERENCE_CLIENT = (1366, 768)
CHARACTER_SIZE = 65
GRID_DIMENSION = 3
RANGE_SIZE = (CHARACTER_SIZE * GRID_DIMENSION, CHARACTER_SIZE * GRID_DIMENSION)
TRADE_MENU_OFFSET = (60, 60)
CONFIRM_BUTTON = (240, 110)
ACCEPT_INVITATION = (890, 668)
TRADE_MESSAGE_BOX = (460, 200)
# Small sample at the requested position inside the known trader-check area.
PRESENCE_BOX = (145, 105, 20, 20)
PRESENCE_COLOR = np.array((227, 225, 215), dtype=np.int16)  # #e3e1d7
TRADER_PRESENT_FRAMES = 2


class TradeOverlayWorker(threading.Thread):
    """Draw short-lived range/current-target rectangles without mouse input."""

    def __init__(self, stop_event: threading.Event) -> None:
        super().__init__(name="trade-overlay", daemon=True)
        self.stop_event = stop_event
        self._requests: "queue.Queue[list[tuple[int, int, int, int, str]]]" = (
            queue.Queue(maxsize=1)
        )

    def show(self, rectangles: list[tuple[int, int, int, int, str]]) -> None:
        try:
            while True:
                self._requests.get_nowait()
        except queue.Empty:
            pass
        try:
            self._requests.put_nowait(rectangles)
        except queue.Full:
            pass

    def clear(self) -> None:
        self.show([])

    def run(self) -> None:  # pragma: no cover - visual Windows path
        if sys.platform != "win32":
            self.stop_event.wait()
            return
        try:
            import tkinter as tk

            root = tk.Tk()
            root.withdraw()
            popups: list[Any] = []

            def render(rectangles: list[tuple[int, int, int, int, str]]) -> None:
                nonlocal popups
                for popup in popups:
                    try:
                        popup.destroy()
                    except Exception:
                        pass
                popups = []
                for left, top, width, height, color in rectangles:
                    popup = tk.Toplevel(root)
                    popup.overrideredirect(True)
                    popup.attributes("-topmost", True)
                    popup.attributes("-alpha", 0.35)
                    popup.configure(background=color)
                    popup.geometry(f"{width}x{height}+{left}+{top}")
                    popups.append(popup)

            def poll() -> None:
                if self.stop_event.is_set():
                    render([])
                    root.destroy()
                    return
                try:
                    render(self._requests.get_nowait())
                except queue.Empty:
                    pass
                root.after(35, poll)

            root.after(0, poll)
            root.mainloop()
        except Exception:
            LOG.exception("trade overlay stopped unexpectedly")


class VirtualMouse:
    """Windows SendInput mouse clicks; the physical cursor is not used."""

    @staticmethod
    def click(x: int, y: int, *, right: bool = False) -> None:
        if sys.platform != "win32":
            return
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
        SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
        left = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        top = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        width = max(1, user32.GetSystemMetrics(SM_CXVIRTUALSCREEN) - 1)
        height = max(1, user32.GetSystemMetrics(SM_CYVIRTUALSCREEN) - 1)
        dx = round((int(x) - left) * 65535 / width)
        dy = round((int(y) - top) * 65535 / height)

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", wintypes.WPARAM),
            ]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("mi", MOUSEINPUT)]

        move = 0x0001 | 0x8000 | 0x4000  # MOVE | ABSOLUTE | VIRTUALDESK
        down, up = (0x0008, 0x0010) if right else (0x0002, 0x0004)
        events = (INPUT(0, MOUSEINPUT(dx, dy, 0, move, 0, 0)),
                  INPUT(0, MOUSEINPUT(dx, dy, 0, move | down, 0, 0)),
                  INPUT(0, MOUSEINPUT(dx, dy, 0, move | up, 0, 0)))
        array_type = INPUT * len(events)
        payload = array_type(*events)
        sent = user32.SendInput(len(events), ctypes.byref(payload), ctypes.sizeof(INPUT))
        if sent != len(events):
            raise OSError(ctypes.get_last_error(), "trade virtual mouse click failed")


class TradeWorker(threading.Thread):
    """Processes Ctrl+Q invite flow and Ctrl+W acceptance flow independently."""

    def __init__(
        self,
        frames: "queue.Queue[Any]",
        stop_event: threading.Event,
        capture_active_event: threading.Event,
        key_sender: Any,
        window_title: str,
    ) -> None:
        super().__init__(name="trade-worker", daemon=True)
        self.frames = frames
        self.stop_event = stop_event
        self.capture_active_event = capture_active_event
        self.key_sender = key_sender
        self.window_title = window_title
        self._requests: "queue.Queue[tuple[str, str]]" = queue.Queue(maxsize=2)
        self._invite_lock = threading.Lock()
        self._invite_pending = False
        self._invite_cancel = threading.Event()
        self.overlay = TradeOverlayWorker(stop_event)

    def request(self, action: str, message: str = "") -> bool:
        if action == "trade:invite":
            return self.toggle_invite(message) == "started"
        try:
            self._requests.put_nowait((action, message))
            LOG.info("trade request queued: %s", action)
            return True
        except queue.Full:
            LOG.warning("trade request ignored: busy")
            return False

    def toggle_invite(self, message: str) -> str:
        """Start an invite sequence, or cancel the currently active one."""

        with self._invite_lock:
            if self._invite_pending:
                self._invite_cancel.set()
                LOG.info("trade invite cancellation requested")
                return "cancelled"
            try:
                self._requests.put_nowait(("trade:invite", message))
            except queue.Full:
                LOG.warning("trade invite ignored: worker is busy")
                return "busy"
            self._invite_cancel.clear()
            self._invite_pending = True
            LOG.info("trade request queued: trade:invite")
            return "started"

    def _invite_cancelled(self) -> bool:
        return self.stop_event.is_set() or self._invite_cancel.is_set()

    def _wait_or_cancel(self, seconds: float) -> bool:
        """Wait briefly, returning false as soon as Ctrl+Q cancels."""

        return not self._invite_cancel.wait(seconds) and not self.stop_event.is_set()

    @staticmethod
    def _scaled(value: int, client_width: int, client_height: int, *, y: bool = False) -> int:
        reference = REFERENCE_CLIENT[1 if y else 0]
        current = client_height if y else client_width
        return round(value * current / reference)

    def _client_geometry(self) -> Optional[tuple[int, int, int, int]]:
        try:
            import win32gui
            # Reuse the exact handle just selected by WindowKeySender.  Its
            # lookup already supports title variants; a second exact-title
            # FindWindow here would make trade fail on those clients.
            hwnd = getattr(self.key_sender, "hwnd", None)
            if not hwnd or not win32gui.IsWindow(hwnd):
                hwnd = win32gui.FindWindow(None, self.window_title)
            if not hwnd or not win32gui.IsWindowVisible(hwnd):
                return None
            left, top = win32gui.ClientToScreen(hwnd, (0, 0))
            client_rect = win32gui.GetClientRect(hwnd)
            right, bottom = win32gui.ClientToScreen(
                hwnd, (client_rect[2], client_rect[3])
            )
            return left, top, right - left, bottom - top
        except Exception:
            LOG.exception("trade game window lookup failed")
            return None

    def _point(self, geometry: tuple[int, int, int, int], point: tuple[int, int]) -> tuple[int, int]:
        left, top, width, height = geometry
        return (
            left + self._scaled(point[0], width, height),
            top + self._scaled(point[1], width, height, y=True),
        )

    def _grid_geometry(
        self, geometry: tuple[int, int, int, int]
    ) -> tuple[int, int, int, int, int, int]:
        """Return the centered 3×3 target grid in screen pixels."""

        left, top, width, height = geometry
        grid_width = self._scaled(RANGE_SIZE[0], width, height)
        grid_height = self._scaled(RANGE_SIZE[1], width, height, y=True)
        box_width = self._scaled(CHARACTER_SIZE, width, height)
        box_height = self._scaled(CHARACTER_SIZE, width, height, y=True)
        return (
            left + (width - grid_width) // 2,
            top + (height - grid_height) // 2,
            grid_width,
            grid_height,
            box_width,
            box_height,
        )

    def _show_range(self, geometry: tuple[int, int, int, int], current: Optional[int] = None) -> None:
        (range_left, range_top, range_width, range_height,
         box_width, box_height) = self._grid_geometry(geometry)
        rectangles = [(range_left, range_top, range_width, range_height, "#8b2be2")]
        if current is not None:
            row, column = divmod(current, GRID_DIMENSION)
            rectangles.append((
                range_left + column * box_width,
                range_top + row * box_height,
                box_width, box_height, "#00aaff",
            ))
        # Keep the presence sample visible even while the target grid is
        # shown, so the user can verify the exact area being evaluated.
        left, top, width, height = geometry
        x, y, presence_width, presence_height = PRESENCE_BOX
        rectangles.append((
            left + self._scaled(x, width, height),
            top + self._scaled(y, width, height, y=True),
            self._scaled(presence_width, width, height),
            self._scaled(presence_height, width, height, y=True),
            "#ffd400",
        ))
        self.overlay.show(rectangles)

    def _set_clipboard(self, message: str) -> bool:
        try:
            import win32clipboard
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(message, win32clipboard.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()
            return True
        except Exception:
            LOG.exception("trade could not set clipboard")
            return False

    def _show_presence_area(self, geometry: tuple[int, int, int, int]) -> None:
        """Show the exact trader-presence sample area while waiting."""

        left, top, width, height = geometry
        x, y, box_width, box_height = PRESENCE_BOX
        self.overlay.show([(
            left + self._scaled(x, width, height),
            top + self._scaled(y, width, height, y=True),
            self._scaled(box_width, width, height),
            self._scaled(box_height, width, height, y=True),
            "#ffd400",
        )])

    def _wait_for_trader(
        self, geometry: tuple[int, int, int, int],
        timeout: Optional[float] = None,
    ) -> bool:
        """Wait for a non-blank presence area, until stopped by default."""

        self.capture_active_event.set()
        deadline = None if timeout is None else time.monotonic() + timeout
        self._show_presence_area(geometry)
        LOG.info("trade waiting for trader; detection area is highlighted")
        present_frames = 0
        try:
            while (not self._invite_cancelled() and
                   (deadline is None or time.monotonic() < deadline)):
                try:
                    frame = self.frames.get(timeout=0.5)
                except queue.Empty:
                    continue
                image = getattr(frame, "image", None)
                if image is None:
                    continue
                width, height = image.size
                x, y, box_width, box_height = PRESENCE_BOX
                left = self._scaled(x, width, height)
                top = self._scaled(y, width, height, y=True)
                right = min(width, left + self._scaled(box_width, width, height))
                bottom = min(height, top + self._scaled(box_height, width, height, y=True))
                pixels = np.asarray(image.crop((left, top, right, bottom)).convert("RGB"), dtype=np.int16)
                blank_ratio = float(np.mean(np.all(np.abs(pixels - PRESENCE_COLOR) <= 5, axis=2)))
                if blank_ratio < 0.985:
                    present_frames += 1
                    if present_frames >= TRADER_PRESENT_FRAMES:
                        LOG.info(
                            "trade trader detected after %d frames "
                            "(blank ratio %.3f)",
                            present_frames, blank_ratio,
                        )
                        return True
                else:
                    present_frames = 0
            if self._invite_cancel.is_set():
                LOG.info("trade wait cancelled by Ctrl+Q")
            elif deadline is not None:
                LOG.info("trade wait timed out: trader not detected")
            return False
        finally:
            self.capture_active_event.clear()

    def _presence_area_is_blank(
        self, geometry: tuple[int, int, int, int]
    ) -> bool:
        """Return true when one fresh presence sample is the blank colour."""

        if self._invite_cancelled():
            return False
        self.capture_active_event.set()
        try:
            frame = self.frames.get(timeout=0.5)
        except queue.Empty:
            return False
        image = getattr(frame, "image", None)
        if image is None:
            return False
        width, height = image.size
        x, y, box_width, box_height = PRESENCE_BOX
        left = self._scaled(x, width, height)
        top = self._scaled(y, width, height, y=True)
        right = min(width, left + self._scaled(box_width, width, height))
        bottom = min(height, top + self._scaled(box_height, width, height, y=True))
        pixels = np.asarray(
            image.crop((left, top, right, bottom)).convert("RGB"),
            dtype=np.int16,
        )
        blank_ratio = float(np.mean(
            np.all(np.abs(pixels - PRESENCE_COLOR) <= 5, axis=2)
        ))
        blank = blank_ratio >= 0.985
        LOG.info("trade presence sample blank=%s ratio=%.3f", blank, blank_ratio)
        return blank

    def _confirm_trade(self, geometry: tuple[int, int, int, int]) -> bool:
        """Confirm the open trade dialog at the requested screen position."""

        # These are deliberately absolute full-screen coordinates from the
        # trade UI specification, not minimap/client-relative game points.
        VirtualMouse.click(*CONFIRM_BUTTON)
        time.sleep(0.15)
        return bool(self.key_sender.send_direct_keys("enter"))

    def _send_message(
        self, geometry: tuple[int, int, int, int], message: str
    ) -> bool:
        if not message or not self._set_clipboard(message):
            return False
        # Like the confirm button, the dialog field is an absolute
        # full-screen coordinate in the 1366×768 trade UI.
        VirtualMouse.click(*TRADE_MESSAGE_BOX)
        time.sleep(0.10)
        return bool(self.key_sender.send_direct_keys("ctrl+v", "enter"))

    @staticmethod
    def _play_success() -> None:
        """Play completion feedback without holding up the trade worker."""

        threading.Thread(
            target=play_mp3,
            args=(Path(__file__).resolve().parent / "sound" / "success.mp3",),
            name="trade-success-sound",
            daemon=True,
        ).start()

    def _invite(self, message: str) -> None:
        if self._invite_cancelled():
            return
        if self.key_sender.select_window() is False or not self.key_sender.is_game_foreground():
            LOG.warning("trade invite ignored: game window unavailable")
            return
        geometry = self._client_geometry()
        if geometry is None:
            return
        try:
            self.capture_active_event.set()
            self._show_range(geometry)
            if not self._wait_or_cancel(0.35):
                return
            for index in range(GRID_DIMENSION * GRID_DIMENSION):
                if self._invite_cancelled():
                    return
                self._show_range(geometry, index)
                if not self._wait_or_cancel(0.18):
                    return
                self.overlay.clear()
                if not self._wait_or_cancel(0.05):
                    return
                (range_left, range_top, _grid_width, _grid_height,
                 box_width, box_height) = self._grid_geometry(geometry)
                row, column = divmod(index, GRID_DIMENSION)
                center = (
                    range_left + column * box_width + box_width // 2,
                    range_top + row * box_height + box_height // 2,
                )
                _left, _top, width, height = geometry
                VirtualMouse.click(*center, right=True)
                if not self._wait_or_cancel(0.18):
                    return
                VirtualMouse.click(center[0] + self._scaled(TRADE_MENU_OFFSET[0], width, height),
                                   center[1] + self._scaled(TRADE_MENU_OFFSET[1], width, height, y=True))
                if not self._wait_or_cancel(0.18):
                    return
                # A blank presence area means this trade request is now
                # pending.  Do not continue inviting other targets: retain
                # the yellow area and wait until the trader arrives.
                if self._presence_area_is_blank(geometry):
                    LOG.info(
                        "trade invitation accepted as pending; "
                        "stopping remaining target invites"
                    )
                    break
                if self._invite_cancelled():
                    return
            # Finish the full 3×3 invitation pass before sampling for a
            # trader, unless a pending invitation stopped it sooner.  The
            # highlighted detection region remains visible until someone
            # arrives or the assistant is stopped.
            if self._wait_for_trader(geometry):
                self.overlay.clear()
                # The trader marker may appear one frame before the game's
                # confirmation dialog is clickable.  Let that dialog settle
                # then complete click + Enter as a separate stage before any
                # chat/message interaction can occur.
                if not self._wait_or_cancel(0.20):
                    return
                confirmed = self._confirm_trade(geometry)
                LOG.info("trade confirmation submitted=%s", confirmed)
                if not confirmed or not self._wait_or_cancel(0.35):
                    return
                if self._send_message(geometry, message):
                    self._play_success()
                    LOG.info("trade invite workflow completed")
        finally:
            self.capture_active_event.clear()
            self.overlay.clear()

    def _accept(self) -> None:
        if self.key_sender.select_window() is False or not self.key_sender.is_game_foreground():
            LOG.warning("trade accept ignored: game window unavailable")
            return
        geometry = self._client_geometry()
        if geometry is None:
            return
        VirtualMouse.click(*ACCEPT_INVITATION)
        time.sleep(0.20)
        self._confirm_trade(geometry)

    def run(self) -> None:
        self.overlay.start()
        LOG.info("trade worker started")
        try:
            while not self.stop_event.is_set():
                try:
                    action, message = self._requests.get(timeout=0.2)
                except queue.Empty:
                    continue
                try:
                    if action == "trade:invite":
                        self._invite(message)
                    elif action == "trade:accept":
                        self._accept()
                except Exception:
                    LOG.exception("trade action failed: %s", action)
                finally:
                    if action == "trade:invite":
                        with self._invite_lock:
                            self._invite_pending = False
                            self._invite_cancel.clear()
                    self._requests.task_done()
        finally:
            self.capture_active_event.clear()
            self.overlay.clear()
            LOG.info("trade worker stopped")


__all__ = ["TradeWorker"]
