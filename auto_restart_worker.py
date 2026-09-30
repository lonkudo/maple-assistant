"""Independent game-restart workflow for the game's memory-leak recovery.

The worker owns only the restart lifecycle.  Login/world/channel selection stays
inside :mod:`reconnect_worker`; this boundary prevents two reconnect sequences
from competing for the keyboard.
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
import threading
import time
from typing import Any, Callable, Optional

from game_chat import send_game_chat_message


LOG = logging.getLogger(__name__)


class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def system_memory_percent() -> Optional[float]:
    """Return Windows system-RAM use without an extra dependency."""

    if not hasattr(ctypes, "WinDLL"):
        return None
    status = _MemoryStatus()
    status.dwLength = ctypes.sizeof(status)
    try:
        ok = ctypes.WinDLL("kernel32", use_last_error=True).GlobalMemoryStatusEx(
            ctypes.byref(status)
        )
    except Exception:
        LOG.debug("auto restart: system memory probe failed", exc_info=True)
        return None
    return float(status.dwMemoryLoad) if ok else None


class AutoRestartWorker(threading.Thread):
    """Restart the game after sustained high system memory usage.

    ``restart_active_event`` covers the whole launcher-to-login handoff.  The
    ordinary reconnect selection is never changed: a dedicated reconnect
    request takes over only after the game client has returned.
    """

    CHECK_SECONDS = 30.0
    MEMORY_THRESHOLD_PERCENT = 98.0
    # The game and launcher consume input asynchronously.  These are short
    # deliberate settles between lifecycle steps; each wait is cancellable.
    GAME_FOCUS_SETTLE_SECONDS = 0.35
    MESSAGE_SETTLE_SECONDS = 0.35
    OFFLINE_KEY_HOLD_SECONDS = 0.08
    OFFLINE_KEY_GAP_SECONDS = 0.35
    OFFLINE_WAIT_SECONDS = 3.0
    AFTER_GAME_EXIT_SETTLE_SECONDS = 0.75
    GAME_EXIT_RECHECK_SECONDS = 10.0
    LAUNCHER_FOCUS_SETTLE_SECONDS = 0.50
    LAUNCHER_CLICK_SETTLE_SECONDS = 0.75
    GAME_WINDOW_SETTLE_SECONDS = 1.0
    # The game window is visible before the client has painted its login page,
    # and its protection component can temporarily take foreground focus.  Do
    # not hand it to reconnect until the existing login-colour detector has
    # positively identified the page.
    GAME_LOGIN_PAGE_TIMEOUT_SECONDS = 60.0
    GAME_REFOCUS_INTERVAL_SECONDS = 1.0
    # MXD's MxdLauncher.exe is only a bootstrapper.  It starts Launcher.dat,
    # whose visible dialog is consistently titled launcher3.0, then exits.
    # This exact title is a safe fallback when there is no surviving process
    # tree to follow.
    LAUNCHER_WINDOW_TITLE = "launcher3.0"
    GAME_APPEAR_TIMEOUT_SECONDS = 45.0
    RECONNECT_START_TIMEOUT_SECONDS = 8.0
    RECONNECT_FINISH_TIMEOUT_SECONDS = 90.0
    RETRY_COUNT_AFTER_INITIAL = 3

    def __init__(
        self,
        stop_event: threading.Event,
        key_sender: Any,
        reconnect_worker: Any,
        *,
        restart_active_event: threading.Event,
        stop_patrol: Optional[Callable[[], None]] = None,
        result_queue: Any = None,
    ) -> None:
        super().__init__(name="auto-restart-worker", daemon=True)
        self.stop_event = stop_event
        self.key_sender = key_sender
        self.reconnect_worker = reconnect_worker
        self.restart_active_event = restart_active_event
        self.stop_patrol = stop_patrol
        self.result_queue = result_queue
        self._lock = threading.Lock()
        self._enabled = False
        self._offline_message = ""
        self._wake = threading.Event()
        self._cancel_requested = threading.Event()
        # GetAsyncKeyState cannot distinguish an injected Esc from a physical
        # one.  This event scopes the one internal Esc tap so WorkflowCancel
        # does not cancel the restart before its Up/Enter follow-up.
        self._sending_offline_esc = threading.Event()
        self._running = False
        self._test_requested = False

    def configure(self, *, enabled: bool, offline_message: object = "") -> None:
        with self._lock:
            self._enabled = bool(enabled)
            self._offline_message = str(offline_message or "").strip()[:500]
        self._wake.set()
        LOG.info("auto restart %s", "enabled" if enabled else "disabled")

    def settings(self) -> tuple[bool, str]:
        with self._lock:
            return self._enabled, self._offline_message

    def is_active(self) -> bool:
        with self._lock:
            return self._running

    def request_cancel(self) -> bool:
        if self._sending_offline_esc.is_set():
            LOG.debug("auto restart: ignored its own offline Esc in cancellation watcher")
            return False
        with self._lock:
            if not self._running:
                return False
        self._cancel_requested.set()
        self._wake.set()
        LOG.warning("auto restart: cancellation requested by Esc")
        return True

    def trigger_test(self) -> bool:
        """Run the same lifecycle now, without waiting for high memory."""

        with self._lock:
            if self._running or self._test_requested:
                LOG.info("auto restart: manual test ignored; a restart is already active")
                return False
            self._test_requested = True
        self._wake.set()
        LOG.info("auto restart: manual test requested")
        return True

    def _report(self, state: str, detail: str) -> None:
        LOG.info("auto restart %s: %s", state, detail)
        if self.result_queue is None:
            return
        try:
            self.result_queue.put_nowait(("restart", f"{state}: {detail}"))
        except Exception:
            LOG.debug("auto restart result queue is unavailable", exc_info=True)

    def _report_memory(self, percent: Optional[float]) -> None:
        """Publish the low-frequency system-memory reading for the UI header."""

        if self.result_queue is None:
            return
        detail = "无法读取" if percent is None else f"{percent:.1f}%"
        try:
            self.result_queue.put_nowait(("memory", detail))
        except Exception:
            LOG.debug("auto restart memory result queue is unavailable", exc_info=True)

    def _cancelled(self) -> bool:
        return self.stop_event.is_set() or self._cancel_requested.is_set()

    def _wait(self, seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, seconds)
        while not self._cancelled():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            self.stop_event.wait(min(0.10, remaining))
        return False

    def _game_available(self) -> bool:
        # WindowKeySender calls this game_window_present().  The former name
        # was a mistaken guessed API, so the test silently decided the game
        # was absent and jumped straight to the launcher phase.
        probe = getattr(self.key_sender, "game_window_present", None)
        try:
            return bool(probe and probe())
        except Exception:
            LOG.debug("auto restart: game-window probe failed", exc_info=True)
            return False

    def _send_offline_sequence(self, message: str) -> bool:
        """Send the optional notice then Esc, Up, Enter, Enter to leave the game."""

        if not self._game_available():
            return True
        exclusive = getattr(self.key_sender, "begin_exclusive", None)
        release_exclusive = getattr(self.key_sender, "end_exclusive", None)
        try:
            if not self.key_sender.select_window() or not self.key_sender.is_game_foreground():
                self._report("warning", "无法聚焦游戏，跳过下线按键")
                return False
            if not self._wait(self.GAME_FOCUS_SETTLE_SECONDS):
                return False
            self.key_sender.enable_input()
            if callable(exclusive):
                exclusive("auto-restart")
            if message:
                send_game_chat_message(self.key_sender, message)
                if not self._wait(self.MESSAGE_SETTLE_SECONDS):
                    return False
            # The game confirms the selected logout entry on a second Enter.
            for key in ("esc", "up", "enter", "enter"):
                internal_esc = key == "esc"
                if internal_esc:
                    self._sending_offline_esc.set()
                try:
                    if self._cancelled() or not self.key_sender.press(
                        key, duration=self.OFFLINE_KEY_HOLD_SECONDS, owner="auto-restart"
                    ):
                        return False
                finally:
                    if internal_esc:
                        # Keep the marker for one poll after key-up: Windows
                        # can report an injected Esc as down briefly after the
                        # SendInput key-up has returned.
                        self._wait(0.05)
                        self._sending_offline_esc.clear()
                if not self._wait(self.OFFLINE_KEY_GAP_SECONDS):
                    return False
            return True
        except Exception:
            LOG.warning("auto restart: offline sequence failed", exc_info=True)
            return False
        finally:
            self._sending_offline_esc.clear()
            if callable(release_exclusive):
                try:
                    release_exclusive("auto-restart")
                except Exception:
                    LOG.debug("auto restart: keyboard ownership release failed", exc_info=True)
            try:
                self.key_sender.disable_input(refocus_before_release=False)
            except Exception:
                LOG.debug("auto restart: could not disarm after offline sequence", exc_info=True)

    def _terminate_game(self) -> bool:
        """Terminate the game tree and wait until it is genuinely gone."""

        if not self._game_available():
            return True
        try:
            import psutil
            import win32process

            hwnd = int(getattr(self.key_sender, "hwnd", 0) or 0)
            if not hwnd:
                selector = getattr(self.key_sender, "select_window", None)
                if callable(selector):
                    selector()
                hwnd = int(getattr(self.key_sender, "hwnd", 0) or 0)
            if not hwnd:
                raise RuntimeError("未找到游戏窗口")
            _thread, pid = win32process.GetWindowThreadProcessId(hwnd)
            root = psutil.Process(int(pid))
            targets = root.children(recursive=True) + [root]
            for process in reversed(targets):
                try:
                    process.terminate()
                except psutil.Error:
                    pass
            _gone, alive = psutil.wait_procs(targets, timeout=4.0)
            for process in alive:
                try:
                    process.kill()
                except psutil.Error:
                    pass
            # task termination is asynchronous on some game/protection builds.
            # Starting the launcher while either the old process or its window
            # survives can make it reject the new client or re-open the old
            # process.  Require both to disappear before proceeding.
            next_notice = time.monotonic() + self.GAME_EXIT_RECHECK_SECONDS
            while not self._cancelled():
                processes_alive = False
                for process in targets:
                    try:
                        if process.is_running():
                            processes_alive = True
                            break
                    except psutil.Error:
                        pass
                if not processes_alive and not self._game_available():
                    self._report("game", "游戏进程已完全关闭")
                    self._wait(self.AFTER_GAME_EXIT_SETTLE_SECONDS)
                    return True
                if time.monotonic() >= next_notice:
                    self._report("waiting-exit", "游戏进程仍在退出，继续等待完全关闭")
                    next_notice = time.monotonic() + self.GAME_EXIT_RECHECK_SECONDS
                self._wait(0.25)
            self._report("warning", "自动重开已取消，游戏进程尚未完全关闭")
            return False
        except ImportError:
            # pywin32 is already a product dependency; taskkill is only the
            # no-psutil fallback for a minimally installed package.
            try:
                import win32process
                hwnd = int(getattr(self.key_sender, "hwnd", 0) or 0)
                _thread, pid = win32process.GetWindowThreadProcessId(hwnd)
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False,
                               capture_output=True, creationflags=0x08000000)
                next_notice = time.monotonic() + self.GAME_EXIT_RECHECK_SECONDS
                while not self._cancelled():
                    if not self._game_available():
                        self._report("game", "游戏进程已完全关闭")
                        self._wait(self.AFTER_GAME_EXIT_SETTLE_SECONDS)
                        return True
                    if time.monotonic() >= next_notice:
                        self._report("waiting-exit", "游戏进程仍在退出，继续等待完全关闭")
                        next_notice = time.monotonic() + self.GAME_EXIT_RECHECK_SECONDS
                    self._wait(0.25)
                self._report("warning", "自动重开已取消，游戏进程尚未完全关闭")
                return False
            except Exception:
                LOG.warning("auto restart: game process could not be terminated", exc_info=True)
                return False
        except Exception:
            LOG.warning("auto restart: game process could not be terminated", exc_info=True)
            return False

    def _launcher_hwnd(self) -> int:
        """Find the known persistent launcher dialog by its exact title."""

        try:
            import win32gui
            import win32process
        except Exception:
            return 0
        known_launcher_title: list[int] = []

        def collect(hwnd: int, _extra: object) -> None:
            if not win32gui.IsWindowVisible(hwnd):
                return
            try:
                title = str(win32gui.GetWindowText(hwnd) or "")
            except Exception:
                title = ""
            if title.casefold() == self.LAUNCHER_WINDOW_TITLE:
                known_launcher_title.append(int(hwnd))

        try:
            win32gui.EnumWindows(collect, None)
        except Exception:
            return 0
        return (known_launcher_title or [0])[0]

    def _launch_game(self) -> bool:
        try:
            hwnd = self._launcher_hwnd()
            if not hwnd:
                self._report("failed", "未找到 launcher3.0 启动器窗口")
                return False
            import win32gui
            import win32con
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
            if not self._wait(self.LAUNCHER_FOCUS_SETTLE_SECONDS):
                return False
            # A minimized launcher reports a zero-sized client rectangle.
            # Measure only *after* it has been restored and allowed to lay out;
            # otherwise the calculated start position becomes the screen's
            # top-left corner and the real button is never clicked.
            left, top, right, bottom = win32gui.GetClientRect(hwnd)
            width, height = right - left, bottom - top
            if width < 100 or height < 100:
                self._report("failed", "启动器窗口尚未完成显示")
                return False
            origin = win32gui.ClientToScreen(hwnd, (0, 0))
            # Coordinate is measured inward from the bottom-right: 18.23%
            # from the right and 25.93% from the bottom.
            x = origin[0] + round(width * 0.8177)
            y = origin[1] + round(height * 0.7407)
            import win32api
            win32api.SetCursorPos((x, y))
            win32api.mouse_event(0x0002, 0, 0, 0, 0)
            win32api.mouse_event(0x0004, 0, 0, 0, 0)
            if not self._wait(self.LAUNCHER_CLICK_SETTLE_SECONDS):
                return False
            self._report("launcher", f"已点击启动器开始游戏（窗口 {hwnd}，坐标 {x},{y}）")
            return True
        except Exception:
            LOG.warning("auto restart: launcher interaction failed", exc_info=True)
            self._report("failed", "启动器启动游戏失败")
            return False

    def _wait_for_game(self) -> bool:
        deadline = time.monotonic() + self.GAME_APPEAR_TIMEOUT_SECONDS
        while not self._cancelled() and time.monotonic() < deadline:
            if self._game_available():
                if not self._wait(self.GAME_WINDOW_SETTLE_SECONDS):
                    return False
                self._report("game", "游戏窗口已出现，交给自动重连")
                return True
            self._wait(0.50)
        return False

    def _wait_for_login_page_ready(self) -> bool:
        """Keep the freshly launched client focused until its login page exists."""

        measure = getattr(self.reconnect_worker, "measure_login_page_colour", None)
        if not callable(measure):
            self._report("failed", "自动重连缺少登录页检测器")
            return False
        deadline = time.monotonic() + self.GAME_LOGIN_PAGE_TIMEOUT_SECONDS
        reported_wait = False
        while not self._cancelled() and time.monotonic() < deadline:
            if not self._game_available():
                self._report("game", "游戏窗口在启动中消失")
                return False
            # This shares the normal re-anchor path with every other workflow.
            # It is intentionally repeated: the game's protection window may
            # steal focus while the client is painting the login screen.
            try:
                self.key_sender.select_window()
            except Exception:
                LOG.debug("auto restart: game re-focus during launch failed", exc_info=True)
            try:
                evidence = measure()
                if evidence is not None and evidence.is_login_page():
                    self._report("login-ready", "登录页已确认，交给自动重连")
                    return True
            except Exception:
                LOG.debug("auto restart: login-page readiness probe failed", exc_info=True)
            if not reported_wait:
                self._report("waiting-login", "游戏启动中：等待登录页并持续聚焦游戏窗口")
                reported_wait = True
            if not self._wait(self.GAME_REFOCUS_INTERVAL_SECONDS):
                return False
        self._report("warning", "游戏窗口仍在启动，尚未检测到登录页；保留游戏窗口")
        return False

    def _handoff_to_reconnect(self) -> bool:
        request = getattr(self.reconnect_worker, "trigger_restart_reconnect", None)
        if not callable(request) or not request():
            self._report("failed", "自动重连未接受重开接管请求")
            return False
        deadline = time.monotonic() + self.RECONNECT_START_TIMEOUT_SECONDS
        started = False
        while not self._cancelled() and time.monotonic() < deadline:
            if bool(getattr(self.reconnect_worker, "is_active", lambda: False)()):
                started = True
                break
            self._wait(0.10)
        if not started:
            self._report("failed", "自动重连没有开始")
            return False
        deadline = time.monotonic() + self.RECONNECT_FINISH_TIMEOUT_SECONDS
        while not self._cancelled() and time.monotonic() < deadline:
            if not self._game_available():
                self._report("game", "游戏窗口再次消失")
                return False
            if not bool(getattr(self.reconnect_worker, "is_active", lambda: False)()):
                succeeded = getattr(self.reconnect_worker, "last_run_succeeded", lambda: False)()
                if succeeded:
                    self._report("done", "自动重连已完成")
                    return True
                self._report("failed", "自动重连没有成功")
                return False
            self._wait(0.25)
        return False

    def _run_restart(self, offline_message: str) -> None:
        self.restart_active_event.set()
        try:
            if callable(self.stop_patrol):
                self.stop_patrol()
            if not self._send_offline_sequence(offline_message):
                self._report("warning", "下线按键未完整送达，仍继续重开")
            if not self._wait(self.OFFLINE_WAIT_SECONDS):
                return
            if not self._terminate_game():
                return
            for attempt in range(self.RETRY_COUNT_AFTER_INITIAL + 1):
                if self._cancelled():
                    return
                self._report("attempt", f"重开第 {attempt + 1}/{self.RETRY_COUNT_AFTER_INITIAL + 1} 次")
                launched = self._launch_game()
                game_visible = launched and self._wait_for_game()
                if game_visible:
                    login_ready = self._wait_for_login_page_ready()
                    if login_ready and self._handoff_to_reconnect():
                        return
                    # A game window that is still present is in its own
                    # startup/login transition, not proof that it crashed.
                    # Do not kill it and create an endless launcher loop.
                    if self._game_available():
                        self._report("warning", "游戏已启动但自动重连未完成；保留游戏窗口，不重复结束进程")
                        return
                if attempt < self.RETRY_COUNT_AFTER_INITIAL:
                    self._report("retry", "游戏未稳定进入，准备再次启动")
                    if not self._wait(2.0):
                        return
            self._report("failed", "重开重试次数已用尽")
        finally:
            self.restart_active_event.clear()

    def run(self) -> None:
        LOG.info("auto restart worker started (checks system memory every %.0f minutes)", self.CHECK_SECONDS / 60.0)
        # Give the header one prompt reading, then use the shared 30-second
        # cadence.  This still runs when 自动重开 itself is unchecked.
        next_check = time.monotonic()
        while not self.stop_event.is_set():
            wait_for = max(0.0, next_check - time.monotonic())
            self._wake.wait(min(wait_for, 1.0))
            self._wake.clear()
            enabled, offline_message = self.settings()
            with self._lock:
                manual_test = self._test_requested
                self._test_requested = False
            if manual_test:
                with self._lock:
                    self._running = True
                    self._cancel_requested.clear()
                try:
                    self._report("test", "手动测试自动重开")
                    self._run_restart(offline_message)
                finally:
                    with self._lock:
                        self._running = False
                next_check = time.monotonic() + self.CHECK_SECONDS
                continue
            if time.monotonic() < next_check:
                continue
            next_check = time.monotonic() + self.CHECK_SECONDS
            percent = system_memory_percent()
            self._report_memory(percent)
            if percent is None:
                LOG.warning("auto restart: unable to read system memory")
                continue
            LOG.info("auto restart: system memory %.1f%% (threshold %.1f%%)", percent, self.MEMORY_THRESHOLD_PERCENT)
            if not enabled:
                continue
            if percent < self.MEMORY_THRESHOLD_PERCENT:
                continue
            if bool(getattr(self.reconnect_worker, "is_active", lambda: False)()):
                self._report("deferred", "自动重连正在运行，稍后再检查内存")
                continue
            with self._lock:
                self._running = True
                self._cancel_requested.clear()
            try:
                self._report("trigger", f"系统内存 {percent:.1f}%")
                self._run_restart(offline_message)
            finally:
                with self._lock:
                    self._running = False
        LOG.info("auto restart worker stopped")


__all__ = ["AutoRestartWorker", "system_memory_percent"]
