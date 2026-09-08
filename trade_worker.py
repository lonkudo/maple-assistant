"""Independent trade automation: virtual clicks and invisible presence checks."""

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
from minimap_detector import hud_scale_for


LOG = logging.getLogger(__name__)

TRADE_MENU_OFFSET = (60, 60)
CONFIRM_BUTTON = (240, 110)
ACCEPT_INVITATION = (885, 672)
TRADE_MESSAGE_BOX = (460, 200)
# Small sample at the requested position inside the known trader-check area.
PRESENCE_BOX = (145, 105, 20, 20)
PRESENCE_COLOR = np.array((227, 225, 215), dtype=np.int16)  # #e3e1d7
TRADE_DIALOG_GREY_FRAMES = 2
TRADER_PRESENT_FRAMES = 2
# A trade window that remains empty is a failed invitation, not a workflow
# that should keep sampling the capture stream forever.
TRADER_WAIT_TIMEOUT_SECONDS = 10.0
VK_ESCAPE = 0x1B


class VirtualMouse:
    """Windows SendInput mouse clicks, starting at the user's cursor."""

    @staticmethod
    def position() -> Optional[tuple[int, int]]:
        """Return the current desktop cursor position without moving it."""

        if sys.platform != "win32":
            return None
        point = wintypes.POINT()
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        if not user32.GetCursorPos(ctypes.byref(point)):
            raise OSError(ctypes.get_last_error(), "could not read mouse position")
        return int(point.x), int(point.y)

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
        if self.stop_event.is_set() or self._invite_cancel.is_set():
            return True
        # Esc is deliberately polled only by an active trade request.  It is
        # not registered as a permanent global hotkey, so normal game Esc
        # behavior remains unchanged when no trade workflow is running.
        if sys.platform == "win32":
            try:
                pressed = bool(
                    ctypes.WinDLL("user32", use_last_error=True)
                    .GetAsyncKeyState(VK_ESCAPE) & 0x8000
                )
            except Exception:
                pressed = False
            if pressed:
                self._invite_cancel.set()
                LOG.info("trade invite cancelled by Esc")
                return True
        return False

    def _wait_or_cancel(self, seconds: float) -> bool:
        """Interrupt a delay within 25ms when Ctrl+Q or Esc cancels."""

        deadline = time.monotonic() + max(0.0, float(seconds))
        while time.monotonic() < deadline:
            if self._invite_cancelled():
                return False
            self._invite_cancel.wait(min(0.025, deadline - time.monotonic()))
        return not self._invite_cancelled()

    @staticmethod
    def _scaled(
        value: int, client_width: int, client_height: int, *, y: bool = False,
    ) -> int:
        """Scale fixed game-UI coordinates with the game's uniform HUD scale.

        1366×768 and larger clients keep this HUD at its original pixel size.
        On smaller clients such as 1075×768, the game shrinks the *entire*
        interface from its width, so Y must use the same factor as X rather
        than the unchanged 768px client height.
        """

        del client_height, y
        return round(value * hud_scale_for(client_width))

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

    def _sample_presence_is_grey(
        self, geometry: tuple[int, int, int, int]
    ) -> Optional[bool]:
        """Return whether one fresh presence sample is the dialog grey.

        ``None`` means no usable frame arrived and is deliberately distinct
        from non-grey: a slow capture must not be treated as a failed dialog.
        """

        try:
            # Keep Esc cancellation responsive even when capture has not
            # produced a new frame yet.
            frame = self.frames.get(timeout=0.05)
        except queue.Empty:
            return None
        image = getattr(frame, "image", None)
        if image is None:
            return None
        width, height = image.size
        x, y, box_width, box_height = PRESENCE_BOX
        left = self._scaled(x, width, height)
        top = self._scaled(y, width, height, y=True)
        right = min(width, left + self._scaled(box_width, width, height))
        bottom = min(height, top + self._scaled(box_height, width, height, y=True))
        if right <= left or bottom <= top:
            return None
        pixels = np.asarray(
            image.crop((left, top, right, bottom)).convert("RGB"),
            dtype=np.int16,
        )
        grey_ratio = float(np.mean(
            np.all(np.abs(pixels - PRESENCE_COLOR) <= 5, axis=2)
        ))
        LOG.info("trade presence sample grey=%s ratio=%.3f", grey_ratio >= 0.985, grey_ratio)
        return grey_ratio >= 0.985

    def _wait_for_trade_dialog(
        self, geometry: tuple[int, int, int, int], timeout: float = 1.0,
    ) -> bool:
        """Require the grey trade-dialog state before waiting for a trader."""

        self.capture_active_event.set()
        deadline = time.monotonic() + max(0.5, float(timeout))
        grey_frames = 0
        non_grey_frames = 0
        try:
            while (not self._invite_cancelled()
                   and time.monotonic() < deadline):
                grey = self._sample_presence_is_grey(geometry)
                if grey is None:
                    continue
                if grey:
                    grey_frames += 1
                    non_grey_frames = 0
                    if grey_frames >= TRADE_DIALOG_GREY_FRAMES:
                        LOG.info("trade dialog observed; waiting for trader")
                        return True
                else:
                    non_grey_frames += 1
                    grey_frames = 0
                    # If the expected grey dialog area never appears across
                    # usable frames, Start Trade did not open its dialog.
                    # Stop here: non-grey is *not* a trader arrival until a
                    # grey dialog has first been confirmed.
                    if non_grey_frames >= TRADE_DIALOG_GREY_FRAMES:
                        LOG.warning(
                            "trade dialog did not appear; aborting before "
                            "message or confirmation"
                        )
                        return False
            if self._invite_cancel.is_set():
                LOG.info("trade dialog wait cancelled by Ctrl+Q")
            else:
                LOG.warning(
                    "trade dialog wait timed out; aborting before message "
                    "or confirmation"
                )
            return False
        finally:
            self.capture_active_event.clear()

    def _wait_for_trader(
        self, geometry: tuple[int, int, int, int],
        timeout: float = TRADER_WAIT_TIMEOUT_SECONDS,
    ) -> bool:
        """Wait up to ``timeout`` seconds for a non-blank presence area."""

        self.capture_active_event.set()
        deadline = time.monotonic() + max(0.0, float(timeout))
        LOG.info(
            "trade waiting for trader (presence check is hidden; timeout=%.1fs)",
            timeout,
        )
        present_frames = 0
        try:
            while (not self._invite_cancelled() and
                   time.monotonic() < deadline):
                grey = self._sample_presence_is_grey(geometry)
                if grey is None:
                    continue
                if not grey:
                    present_frames += 1
                    if present_frames >= TRADER_PRESENT_FRAMES:
                        LOG.info(
                            "trade trader detected after %d frames "
                            "after confirmed dialog", present_frames,
                        )
                        return True
                else:
                    present_frames = 0
            if self._invite_cancel.is_set():
                LOG.info("trade wait cancelled by Ctrl+Q")
            else:
                LOG.warning(
                    "trade wait expired after %.1fs: trader not detected; "
                    "stopping invite workflow",
                    timeout,
                )
            return False
        finally:
            self.capture_active_event.clear()

    def _confirm_trade(self, geometry: tuple[int, int, int, int]) -> bool:
        """Confirm the open trade dialog at the requested screen position."""

        # (240, 110) is absolute inside the 1366×768 *game client*.  Convert
        # it through the selected client origin so a windowed/offset game does
        # not receive a desktop click at the wrong location.
        point = self._point(geometry, CONFIRM_BUTTON)
        LOG.info("trade confirm click client=%s screen=%s", CONFIRM_BUTTON, point)
        VirtualMouse.click(*point)
        if not self._wait_or_cancel(0.15):
            return False
        return bool(self.key_sender.send_direct_keys("enter"))

    def _send_message(
        self, geometry: tuple[int, int, int, int], message: str
    ) -> bool:
        if not message or not self._set_clipboard(message):
            return False
        point = self._point(geometry, TRADE_MESSAGE_BOX)
        LOG.info("trade message click client=%s screen=%s", TRADE_MESSAGE_BOX, point)
        VirtualMouse.click(*point)
        if not self._wait_or_cancel(0.10):
            return False
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
            cursor = VirtualMouse.position()
            if cursor is None:
                LOG.warning("trade invite ignored: mouse position unavailable")
                return
            left, top, width, height = geometry
            if not (left <= cursor[0] < left + width and top <= cursor[1] < top + height):
                LOG.warning("trade invite ignored: place the cursor on a trader in the game window first")
                return
            LOG.info("trade invite: using user cursor at screen=%s", cursor)
            VirtualMouse.click(*cursor, right=True)
            if not self._wait_or_cancel(0.18):
                return
            start_trade = (
                cursor[0] + self._scaled(TRADE_MENU_OFFSET[0], width, height),
                cursor[1] + self._scaled(TRADE_MENU_OFFSET[1], width, height, y=True),
            )
            LOG.info("trade start click screen=%s", start_trade)
            VirtualMouse.click(*start_trade)
            if not self._wait_or_cancel(0.18):
                return
            # Phase 1: the grey dialog must appear. If it does not, a
            # non-grey sample means Start Trade failed, not that the trader
            # has arrived; do not send a message or click confirmation.
            if not self._wait_for_trade_dialog(geometry):
                return
            # Phase 2: after grey was observed, non-grey means the trader
            # entered the dialog. The check remains completely invisible.
            if self._wait_for_trader(geometry):
                if not self._wait_or_cancel(0.20):
                    return
                # The detector overlay or another desktop window may now be
                # foreground.  Focus the game again before its trade dialog
                # receives the message and confirmation.
                if self.key_sender.select_window() is False:
                    LOG.warning(
                        "trade completion aborted: game could not be "
                        "refocused after trader detection"
                    )
                    return
                if not self._send_message(geometry, message):
                    LOG.warning("trade message failed; confirmation skipped")
                    return
                if not self._wait_or_cancel(0.35):
                    return
                confirmed = self._confirm_trade(geometry)
                LOG.info("trade confirmation submitted=%s", confirmed)
                if confirmed:
                    self._play_success()
                    LOG.info("trade invite workflow completed")
        finally:
            self.capture_active_event.clear()

    def _accept(self, message: str) -> None:
        if self.key_sender.select_window() is False or not self.key_sender.is_game_foreground():
            LOG.warning("trade accept ignored: game window unavailable")
            return
        geometry = self._client_geometry()
        if geometry is None:
            return
        point = self._point(geometry, ACCEPT_INVITATION)
        LOG.info("trade accept click client=%s screen=%s", ACCEPT_INVITATION, point)
        VirtualMouse.click(*point)
        time.sleep(0.20)
        confirmed = self._confirm_trade(geometry)
        LOG.info("trade acceptance confirmation submitted=%s", confirmed)
        if not confirmed:
            return
        # Let the accepted trade dialog settle before entering chat text.
        time.sleep(0.35)
        if self._send_message(geometry, message):
            self._play_success()
            LOG.info("trade acceptance workflow completed")

    def run(self) -> None:
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
                        self._accept(message)
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
            LOG.info("trade worker stopped")


__all__ = ["TradeWorker"]
