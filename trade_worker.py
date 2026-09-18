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

import json

import numpy as np

from countdown_worker import play_mp3
from minimap_detector import hud_scale_for


LOG = logging.getLogger(__name__)

TRADE_MENU_OFFSET = (60, 60)
CONFIRM_BUTTON = (240, 110)
# 交易接受按钮：以游戏客户区**右下角**为基准的逻辑像素偏移（1366×768 预设实测值）。
# 右下角 = 客户区原点 + 客户区宽高（GetClientRect），即 right = left + width, bottom = top + height。
# 旧的绝对值 (885, 672) 只在 1366×768 下等于 (right-481, bottom-96)，其他尺寸必然点偏。
ACCEPT_INVITATION_OFFSET = (206, 91)
# 更窄的预设（例如 1080×768）里偏移量是否跟着界面缩小：
#   "width" = 按宽度比例缩小（hud_scale_for，1080 时 0.7906）——1366×768 实测值的等比假设
#   "none"  = 不缩小（界面固定像素、贴右下角）
# 两种都可能，取决于这台机器上游戏如何渲染交易窗，所以留成可校准项：见下面的
# ``trade_offsets.json`` 与 ``trade_offset_probe.py``。
# 已用实测值验证：1080×768 客户区上实测偏移是 (165, 71)，而 0.7906×(206, 91) = (163, 72)，
# 相差 2px（鼠标悬停精度）——即偏移量确实随宽度比例缩小。1366×768 下比例为 1.0，两种模式
# 等价，所以这个默认值不会影响已经正常的那台机器。
ACCEPT_INVITATION_SCALE = "width"
# 每机校准文件（随包分发时被排除，用户自己生成）。内容示例：
#   {"accept_offset": [206, 91], "accept_scale": "none"}
ACCEPT_OFFSET_FILE = "trade_offsets.json"
ACCEPT_SCALE_MODES = ("width", "none")
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

    @staticmethod
    def accept_invitation_offset(
        width: int,
        *,
        offset: Optional[tuple[int, int]] = None,
        scale: Optional[str] = None,
    ) -> tuple[int, int]:
        """The (right, bottom) pixel offsets to subtract from the client's bottom-right corner."""

        offset_x, offset_y = ACCEPT_INVITATION_OFFSET if offset is None else offset
        mode = ACCEPT_INVITATION_SCALE if scale is None else scale
        if mode == "none":
            return int(offset_x), int(offset_y)
        factor = hud_scale_for(width)
        return round(offset_x * factor), round(offset_y * factor)

    @classmethod
    def accept_invitation_point(
        cls,
        geometry: tuple[int, int, int, int],
        *,
        offset: Optional[tuple[int, int]] = None,
        scale: Optional[str] = None,
    ) -> tuple[int, int]:
        """Screen point of the trade-accept button, measured from the client's bottom-right.

        ``geometry`` is ``(left, top, width, height)`` of the game CLIENT in screen coordinates, as
        returned by ``_client_geometry`` (``left/top`` is the client origin on the desktop, so
        ``left + width`` and ``top + height`` are the client's right/bottom edges).
        """

        left, top, width, height = geometry
        offset_x, offset_y = cls.accept_invitation_offset(
            width, offset=offset, scale=scale
        )
        return left + width - offset_x, top + height - offset_y

    @staticmethod
    def load_accept_calibration(
        path: Optional[Path] = None,
    ) -> tuple[tuple[int, int], str]:
        """Read the per-machine calibration; returns ``(offset, scale_mode)``.

        ``trade_offsets.json`` is written by ``trade_offset_probe.py`` (never shipped): the operator
        hovers the cursor over the accept button on the machine in question and the probe records
        the real offset from the client's bottom-right corner, which is what tells us whether that
        machine shrinks the offsets or not.
        """

        default = ((int(ACCEPT_INVITATION_OFFSET[0]), int(ACCEPT_INVITATION_OFFSET[1])),
                   ACCEPT_INVITATION_SCALE)
        config_path = Path(path) if path is not None else Path(__file__).with_name(
            ACCEPT_OFFSET_FILE
        )
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default
        if not isinstance(data, dict):
            return default
        raw = data.get("accept_offset")
        offset = default[0]
        if (isinstance(raw, (list, tuple)) and len(raw) == 2
                and all(isinstance(value, (int, float)) for value in raw)):
            offset = (int(round(float(raw[0]))), int(round(float(raw[1]))))
        mode = str(data.get("accept_scale", default[1])).strip().lower()
        if mode not in ACCEPT_SCALE_MODES:
            mode = default[1]
        return offset, mode

    @staticmethod
    def _describe_window(hwnd: int) -> str:
        """Identity of a window for the log: class, pid, executable, client and window rects."""

        try:
            import win32gui
            import win32process

            _thread, pid = win32process.GetWindowThreadProcessId(hwnd)
            class_name = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            try:
                import win32api
                import win32con

                handle = win32api.OpenProcess(
                    win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ,
                    False, pid,
                )
                exe = win32process.GetModuleFileNameEx(handle, 0)
                win32api.CloseHandle(handle)
            except Exception:
                exe = "?"
            client_rect = win32gui.GetClientRect(hwnd)
            window_rect = win32gui.GetWindowRect(hwnd)
            return (
                f"hwnd={hwnd} class={class_name!r} pid={pid} exe={exe!r} "
                f"title={title.encode('ascii', 'backslashreplace').decode()!r} "
                f"client={client_rect[2]}x{client_rect[3]} window_rect={window_rect}"
            )
        except Exception as exc:
            return f"hwnd={hwnd} (could not describe: {exc!r})"

    def _game_window_candidates(self, win32gui: Any) -> list[int]:
        """Visible top-level windows of the game process, largest client area first.

        ``key_sender.hwnd`` is normally the game view, but on some machines it can be another
        window of the same process (a launcher/login dialog): its client rect would then place the
        bottom-right corner somewhere else and a corner-anchored click misses.  This list lets the
        caller fall back to the biggest client of that same process - the game view.
        """

        base = getattr(self.key_sender, "hwnd", None)
        if not base:
            return []
        try:
            import win32process

            _thread, pid = win32process.GetWindowThreadProcessId(base)
        except Exception:
            return []
        found: list[int] = []

        def visit(hwnd: int, _extra: object) -> bool:
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                _thread, other_pid = win32process.GetWindowThreadProcessId(hwnd)
                if other_pid != pid:
                    return True
                rect = win32gui.GetClientRect(hwnd)
                if rect[2] >= 640 and rect[3] >= 480:
                    found.append(hwnd)
            except Exception:
                return True
            return True

        try:
            win32gui.EnumWindows(visit, None)
        except Exception:
            return []

        def area(hwnd: int) -> int:
            rect = win32gui.GetClientRect(hwnd)
            return int(rect[2]) * int(rect[3])

        return sorted(found, key=area, reverse=True)

    def _client_geometry(self) -> Optional[tuple[int, int, int, int]]:
        try:
            import win32gui

            # Reuse the exact handle just selected by WindowKeySender.  Its
            # lookup already supports title variants; a second exact-title
            # FindWindow here would make trade fail on those clients.
            hwnd = getattr(self.key_sender, "hwnd", None)
            candidates: list[int] = []
            if hwnd and win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd):
                candidates.append(int(hwnd))
            # A window of the game process whose client is too small is not the game view (a
            # launcher or dialog); prefer the process's largest visible client then.
            usable = [
                candidate for candidate in candidates
                if min(win32gui.GetClientRect(candidate)[2],
                       win32gui.GetClientRect(candidate)[3]) > 0
            ]
            for candidate in self._game_window_candidates(win32gui):
                if candidate not in candidates:
                    usable.append(candidate)
            if not usable:
                found = win32gui.FindWindow(None, self.window_title)
                if found and win32gui.IsWindowVisible(found):
                    usable.append(int(found))
            if not usable:
                LOG.warning("trade: no usable game window found for the click geometry")
                return None
            chosen = usable[0]
            left, top = win32gui.ClientToScreen(chosen, (0, 0))
            client_rect = win32gui.GetClientRect(chosen)
            right, bottom = win32gui.ClientToScreen(
                chosen, (client_rect[2], client_rect[3])
            )
            LOG.info(
                "trade client geometry: %s -> origin=(%d, %d) size=%dx%d "
                "bottom_right=(%d, %d)",
                self._describe_window(chosen), left, top, right - left, bottom - top,
                right, bottom,
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
        point = self.accept_invitation_point(geometry)
        left, top, width, height = geometry
        # Both hypotheses are logged: whichever point the operator sees the cursor land on tells us
        # immediately whether this machine shrinks the offsets or keeps them fixed.
        scaled = self.accept_invitation_point(geometry, scale="width")
        fixed = self.accept_invitation_point(geometry, scale="none")
        LOG.info(
            "trade accept click: client=%dx%d origin=(%d, %d) bottom_right=(%d, %d) "
            "offset=%s scale_mode=%s scale=%.4f applied=%s screen=%s "
            "(scaled=%s fixed=%s)",
            width, height, left, top, left + width, top + height,
            ACCEPT_INVITATION_OFFSET, ACCEPT_INVITATION_SCALE,
            hud_scale_for(width), self.accept_invitation_offset(width), point,
            scaled, fixed,
        )
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
