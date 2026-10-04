# -*- coding: utf-8 -*-
"""mouse_aim_controller.py

让真实 Windows 鼠标跟随追踪目标（画面里的准星）的独立模块。

设计原则
--------
- 与追踪器/播放器完全解耦：本模块不 import 任何 tracker/Tk/video 代码。
- 调用方只需要做两件事：
    1. push_target(x, y, confidence, state)
       推送"视频像素坐标系"的目标点（建议用平滑后的 x/y）。
    2. set_region(left, top, right, bottom)
       告诉控制器：视频画面显示在屏幕上的哪个矩形区域。
- 模块内部有一个常驻工作线程，按 update_hz 频率把鼠标移动到最新目标点，
  自带：速度上限、边界钳制、低置信度保持、F8 全局开关。

坐标映射
--------
    屏幕坐标 = region 左上角 + (视频坐标 / 视频尺寸) * region 尺寸
也就是说目标在视频里的相对位置，会原样映射到它在屏幕上出现的位置，
与播放窗口大小、缩放比例无关（由调用方每次窗口变化时刷新 region）。

安全策略
--------
- 只移动鼠标，绝不发送点击或鼠标按键。
- 未收到 set_region() 前不移动鼠标，避免开局乱跳。
- 目标置信度低于 confidence_threshold 时保持原位（不追噪声）。
- 单次移动距离受 max_speed_px_s 限制，防止瞬移。
- F8 随时开关；关闭后鼠标立即交还用户。

典型接入（见两个播放器文件里的用法）：
    aim = MouseAimController(video_width, video_height)
    aim.push_target(result.x, result.y, result.confidence, result.state)
    region = widget_image_region(image_label, photo.width(), photo.height())
    aim.set_region(*region)          # 窗口移动/缩放后要重新调用
    aim.close()                      # 退出时
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from threading import Lock, Thread
from time import monotonic, sleep
# ---------------------------------------------------------------------------
# Win32 接口（仅移动鼠标，不注入点击）
# ---------------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)

user32.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
user32.SetCursorPos.restype = wintypes.BOOL

user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
user32.GetSystemMetrics.restype = ctypes.c_int

user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
user32.GetAsyncKeyState.restype = ctypes.c_short

user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
user32.GetCursorPos.restype = wintypes.BOOL

SM_CXSCREEN = 0
SM_CYSCREEN = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000


class MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    )


class _INPUT_UNION(ctypes.Union):
    _fields_ = (("mi", MOUSEINPUT),)


class INPUT(ctypes.Structure):
    _anonymous_ = ("input",)
    _fields_ = (("type", wintypes.DWORD), ("input", _INPUT_UNION))


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT

# F8：鼠标跟随 开/关 热键（避免与播放器里的 Esc/空格 冲突）
VK_F8 = 0x77


@dataclass(frozen=True)
class _TargetSample:
    """调用方推送的最新目标点（视频或桌面像素坐标系）。"""

    x: float
    y: float
    confidence: float
    state: str
    pushed_at: float
    screen_space: bool = False
    immediate: bool = False


def widget_image_region(label, image_width: int, image_height: int):
    """返回图片显示在屏幕上的矩形 (left, top, right, bottom)。

    适用于"图片被居中放在一个 Tk Label 里"的常见布局（两个播放器都是）。
    label 只要求有 winfo_* 接口，本模块不依赖 tkinter。
    测量不到（窗口未显示/已销毁）时返回 None。

    图片比 Label 大时 Tk **不是**把它从左上角开始画，而是以 Label 中心对齐、
    多出来的部分被裁掉。所以居中的偏移量必须允许为负：

        实测（1366x768 视频放进 1084x744 的 Label，150% 缩放屏）
        Tk 真正绘制图片的原点 = Label 中心 - 图片尺寸/2
        = Label 原点 + (1084-1366)/2 = Label 原点 - 141

    旧代码把这个负偏移用 max(0, ...) 夹成 0，于是计算出的区域比图片实际
    位置偏右 141px、偏下 12px（物理像素 212px）——鼠标就会停在准星右侧，
    而 _verify_aim_delivery 发现不了：它拿"同一个错误区域"去比对光标。
    """
    if image_width <= 0 or image_height <= 0:
        return None
    try:
        if not label.winfo_ismapped():
            return None
        label_width = label.winfo_width()
        label_height = label.winfo_height()
        left = label.winfo_rootx()
        top = label.winfo_rooty()
    except Exception:
        return None
    if label_width <= 1 or label_height <= 1:
        return None
    # 允许负值：图片大于 Label 时，可见内容从 Label 左上角之外开始。
    left += (label_width - image_width) // 2
    top += (label_height - image_height) // 2
    return (left, top, left + image_width, top + image_height)


# ---------------------------------------------------------------------------
# 活动实例注册表
# ---------------------------------------------------------------------------
# 每个 MouseAimController 启动时登记、close() 时注销。助手在某个自动序列
# 开始驱动鼠标时，用它挂起其它实例（例如演示窗口的鼠标跟随），结束后恢复，
# 避免两个控制器抢同一只鼠标。
#
# _EXCLUSIVE_CONTROLLER 记录当前“唯一鼠标持有者”。在持有期间新登记的控制
# 器（例如序列进行中才打开的演示窗口）会以禁用状态启动，避免漏网实例与
# 序列争抢鼠标；持有者 close() 时这些被压制实例自动恢复。
# How far inside the region the cursor is kept, so it can never come to rest on
# the drawn edge of the tracked area (see _clamp_to_screen).
_REGION_MARGIN_PX = 2.0
_REGISTRY_LOCK = Lock()
_LIVE_CONTROLLERS: "set[MouseAimController]" = set()
_EXCLUSIVE_CONTROLLER: "MouseAimController | None" = None


def suspend_others_except(keep):
    """Make ``keep`` the sole cursor driver and disable every other live
    controller.

    Returns the list of controllers that were running and are now disabled,
    so the caller can re-enable them afterwards.
    """

    global _EXCLUSIVE_CONTROLLER
    restore = []
    with _REGISTRY_LOCK:
        _EXCLUSIVE_CONTROLLER = keep
        for controller in tuple(_LIVE_CONTROLLERS):
            if controller is keep:
                continue
            if controller.enabled:
                restore.append(controller)
                controller.set_enabled(False, silent=True)
    return restore


def claim_cursor(controller) -> list:
    """Make ``controller`` the sole ENABLED cursor driver; return suppressed ones.

    A controller created while another one already owns the cursor exclusively
    starts disabled by design (see ``_register``), which is what a caller wants
    for an accidentally opened window - but not for a window the operator
    opened on purpose (the tracking test).  This claims the cursor and, crucially,
    ENABLES the controller: without the second step such a window tracked
    correctly and never moved the mouse at all.

    It refuses to STEAL the cursor from a controller that already owns it: that
    owner is an automatic sequence, which is bounded to the area it feeds (strictly
    inside the drawn rectangle) while a viewer window would drive the mouse over its
    whole video - i.e. outside that area and onto its border, which is exactly what
    the operator reported.  The owner hands the cursor back when it closes.
    """

    with _REGISTRY_LOCK:
        owner = _EXCLUSIVE_CONTROLLER
    if owner is not None and owner is not controller:
        print(
            "mouse aim: an automatic sequence owns the cursor right now; "
            "this window will not move the mouse until it finishes"
        )
        return []
    suspended = suspend_others_except(controller)
    try:
        controller.set_enabled(True, silent=True)
    except Exception:
        pass
    return suspended


def release_cursor(suspended) -> None:
    """Hand the cursor back to the controllers ``claim_cursor`` suppressed."""

    for controller in tuple(suspended or ()):
        try:
            controller.set_enabled(True, silent=True)
        except Exception:
            pass


def read_cursor() -> "tuple[float, float] | None":
    """Current cursor position in screen pixels (None when unavailable).

    Public because callers must be able to verify that a pushed aim point
    actually reached the cursor: a mapping error (DPI scaling, a widget that
    moved) is otherwise invisible - the tracker reports a correct point while
    the mouse sits somewhere else.
    """

    return MouseAimController._read_cursor()


def _register(controller) -> bool:
    """Add a controller to the registry.

    Returns True when the controller must start DISABLED because another
    controller currently owns the cursor exclusively (a window opened in the
    middle of an automatic sequence must not fight for the mouse).
    """

    with _REGISTRY_LOCK:
        _LIVE_CONTROLLERS.add(controller)
        if (
            _EXCLUSIVE_CONTROLLER is not None
            and controller is not _EXCLUSIVE_CONTROLLER
        ):
            return True
    return False


def _unregister(controller) -> None:
    """Remove a controller; when it was the exclusive owner, release the lock
    and re-enable controllers that were suppressed while it held it."""

    global _EXCLUSIVE_CONTROLLER
    with _REGISTRY_LOCK:
        _LIVE_CONTROLLERS.discard(controller)
        if _EXCLUSIVE_CONTROLLER is not controller:
            return
        _EXCLUSIVE_CONTROLLER = None
        for other in tuple(_LIVE_CONTROLLERS):
            if getattr(other, "_suppressed_by_exclusive", False):
                other._suppressed_by_exclusive = False
                other.set_enabled(True, silent=True)


class MouseAimController:
    """把最新目标点转换成屏幕坐标并驱动真实鼠标的工作器。

    Parameters
    ----------
    video_width, video_height:
        视频（首帧）像素尺寸，用于把目标点按比例映射到 region 上。
    enabled:
        是否一开始就跟随。False 时鼠标完全不动，F8 可随时打开。
    confidence_threshold:
        目标置信度低于该值时保持原位不动。
    max_speed_px_s:
        鼠标最大移动速度（像素/秒），防止跳变和瞬移。
    update_hz:
        工作线程刷新频率。数值越大移动越平滑，开销可忽略。
    toggle_key:
        全局开关热键的虚拟键码，默认 F8。
    use_sendinput:
        True 时用 SendInput 绝对移动（部分独占全屏游戏需要）；
        False 时用 SetCursorPos（更简单、支持多显示器）。默认 False。
    dead_band_px:
        悬停死区（屏幕像素）。光标距目标点在此范围内时**完全不移动鼠标**：
        光标已经"停"在目标上，反复 SetCursorPos 只会抖动。默认 0.0
        （=旧的持续跟随行为），自动过测谎传 8px。
    target_stale_seconds:
        目标点多久没被 push_target() 更新就当作没有目标（不再驱动鼠标）。生产者
        每跟随一帧都会推送，所以只有在它停止跟随时才会出现这么长的间隔；没有这个
        超时，控制器会永远朝最后一个点移动，任何别的东西挪动光标后它还会把光标
        拽回那个点（看起来就像"卡在边缘"）。
    """

    def __init__(
        self,
        video_width: int,
        video_height: int,
        *,
        enabled: bool = True,
        confidence_threshold: float = 0.5,
        max_speed_px_s: float = 2000.0,
        update_hz: float = 120.0,
        toggle_key: int = VK_F8,
        use_sendinput: bool = False,
        dead_band_px: float = 0.0,
        target_stale_seconds: float = 0.75,
    ) -> None:
        if video_width <= 0 or video_height <= 0:
            raise ValueError("video size must be positive")
        if max_speed_px_s <= 0 or update_hz <= 0:
            raise ValueError("speed and rate must be positive")

        self._video_width = float(video_width)
        self._video_height = float(video_height)
        self._confidence_threshold = float(confidence_threshold)
        self._max_step_px = max_speed_px_s / update_hz
        self._interval = 1.0 / update_hz
        self._toggle_key = toggle_key
        self._use_sendinput = use_sendinput
        self._dead_band_px = max(0.0, float(dead_band_px))
        self._target_stale_seconds = max(0.05, float(target_stale_seconds))

        self._lock = Lock()
        self._target: _TargetSample | None = None
        self._region: tuple[int, int, int, int] | None = None
        self._enabled = bool(enabled)
        self._closed = False
        self._last_cursor: tuple[float, float] | None = None
        self._toggle_was_down = False
        self._suppressed_by_exclusive = False

        self._thread = Thread(
            target=self._run,
            name="mouse-aim-controller",
            daemon=True,
        )
        self._thread.start()
        if _register(self):
            # Another controller owns the cursor right now (an automatic
            # sequence is running): start disabled instead of fighting it.
            self._suppressed_by_exclusive = True
            with self._lock:
                self._enabled = False

    # ------------------------------------------------------------------ 公开 API

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    @property
    def status_text(self) -> str:
        """给播放器状态栏用的短文本（线程安全）。"""
        return "鼠标跟随：开（按 F8 关）" if self.enabled else "鼠标跟随：关（按 F8 开）"

    def push_target(
        self,
        x: float,
        y: float,
        confidence: float = 1.0,
        state: str = "",
    ) -> None:
        """推送一个目标点（视频像素坐标）。任何线程可调用，非阻塞。"""
        with self._lock:
            self._target = _TargetSample(
                x=float(x),
                y=float(y),
                confidence=float(confidence),
                state=str(state),
                pushed_at=monotonic(),
            )

    def push_screen_target(
        self,
        screen_x: float,
        screen_y: float,
        confidence: float = 1.0,
        state: str = "",
        *,
        immediate: bool = False,
    ) -> None:
        """Push a target already expressed in physical desktop pixels.

        This avoids re-mapping a target through the video's client rectangle.
        It is intended for a producer that has already completed the same
        capture-pixel-to-screen conversion used by its display overlay.  An
        ``immediate`` target intentionally bypasses the viewer's dead band and
        speed limit, which is appropriate for a short automated cursor pass
        but not for the interactive video viewer.
        """

        with self._lock:
            self._target = _TargetSample(
                x=float(screen_x),
                y=float(screen_y),
                confidence=float(confidence),
                state=str(state),
                pushed_at=monotonic(),
                screen_space=True,
                immediate=bool(immediate),
            )

    def set_region(self, left: int, top: int, right: int, bottom: int) -> None:
        """设置视频画面在屏幕上的矩形区域（屏幕像素坐标）。"""
        if right <= left or bottom <= top:
            return
        with self._lock:
            self._region = (int(left), int(top), int(right), int(bottom))

    def clear_region(self) -> None:
        """清除 region：之后不再移动鼠标，直到重新 set_region。"""
        with self._lock:
            self._region = None

    def set_enabled(self, enabled: bool, *, silent: bool = False) -> None:
        if enabled and self._owned_by_someone_else():
            # An automatic sequence owns the cursor: it is bounded to the area it
            # feeds, and letting another controller drive the mouse now is how the
            # cursor ended up outside that area.  F8 cannot override it either.
            if not silent:
                print(
                    "mouse aim: an automatic sequence owns the cursor; "
                    "this controller stays off until it finishes"
                )
            return
        with self._lock:
            if self._enabled == bool(enabled):
                return
            self._enabled = bool(enabled)
        if not silent:
            print(self.status_text)

    def _owned_by_someone_else(self) -> bool:
        with _REGISTRY_LOCK:
            owner = _EXCLUSIVE_CONTROLLER
        return owner is not None and owner is not self

    def toggle(self) -> None:
        self.set_enabled(not self.enabled)

    def map_to_screen(self, x: float, y: float) -> tuple[float, float] | None:
        """把视频坐标映射成屏幕坐标；未设置 region 时返回 None。"""
        with self._lock:
            region = self._region
            if region is None:
                return None
        left, top, right, bottom = region
        screen_x = left + (float(x) / self._video_width) * (right - left)
        screen_y = top + (float(y) / self._video_height) * (bottom - top)
        return (screen_x, screen_y)

    def close(self) -> None:
        _unregister(self)
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._enabled = False
        self._thread.join(timeout=1.0)

    def __enter__(self) -> "MouseAimController":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    # ------------------------------------------------------------------ 内部实现

    def _run(self) -> None:
        while True:
            with self._lock:
                closed = self._closed
            if closed:
                return
            try:
                self._tick()
            except Exception:
                pass  # 单次失败不影响后续；绝不因鼠标异常拖垮播放器
            sleep(self._interval)

    def _tick(self) -> None:
        self._handle_toggle_key()
        with self._lock:
            if not self._enabled:
                return
            sample = self._target
            if sample is None:
                return
        if sample.confidence < self._confidence_threshold:
            return  # 低置信度：保持原位，不追噪声
        # A target that stopped being pushed is not a target any more.  Without
        # this the controller keeps steering to the last point for ever, and drags
        # the cursor BACK to it whenever anything else moves the mouse: the cursor
        # looks glued to whatever edge the last point sat on ("the aim is trapped
        # at the edge").  The producer (the live pass) pushes on every followed
        # frame, so a gap this long only happens when it stopped following.
        if monotonic() - sample.pushed_at > self._target_stale_seconds:
            return

        if sample.screen_space:
            # The caller has already made the physical screen conversion.
            # Applying the client-region mapping here would re-scale it when
            # GDI's captured bitmap and Windows' logical client rectangle
            # differ under DPI virtualization.
            screen_x, screen_y = sample.x, sample.y
        else:
            mapped = self.map_to_screen(sample.x, sample.y)
            if mapped is None:
                return  # region 尚未就绪：不动
            screen_x, screen_y = self._clamp_to_screen(*mapped)
        if not sample.immediate and self._dead_band_px > 0.0:
            # 悬停：光标已经在目标上就不再下发移动指令。用真实光标位置判断
            # （而不是上一次下发的坐标），这样用户手动挪开、或游戏自己移动
            # 光标之后会自动重新对齐。
            current = self._read_cursor() or self._last_cursor
            if current is not None:
                offset_x = screen_x - current[0]
                offset_y = screen_y - current[1]
                if (offset_x * offset_x + offset_y * offset_y) ** 0.5 \
                        <= self._dead_band_px:
                    return
        with self._lock:
            previous = self._last_cursor
        if previous is not None and not sample.immediate:
            delta_x = screen_x - previous[0]
            delta_y = screen_y - previous[1]
            distance = (delta_x * delta_x + delta_y * delta_y) ** 0.5
            if distance > self._max_step_px:
                ratio = self._max_step_px / distance
                screen_x = previous[0] + delta_x * ratio
                screen_y = previous[1] + delta_y * ratio

        if self._move_cursor(screen_x, screen_y):
            with self._lock:
                self._last_cursor = (screen_x, screen_y)

    def _handle_toggle_key(self) -> None:
        down = bool(user32.GetAsyncKeyState(self._toggle_key) & 0x8000)
        if down and not self._toggle_was_down:
            self.toggle()
        self._toggle_was_down = down

    @staticmethod
    def _read_cursor() -> tuple[float, float] | None:
        """真实光标位置（屏幕像素）；读取失败返回 None。"""

        point = wintypes.POINT()
        try:
            if not user32.GetCursorPos(ctypes.byref(point)):
                return None
        except Exception:
            return None
        return (float(point.x), float(point.y))

    def _clamp_to_screen(self, screen_x: float, screen_y: float) -> tuple[float, float]:
        if self._use_sendinput:
            # SendInput 绝对坐标只能落在主屏内
            width = user32.GetSystemMetrics(SM_CXSCREEN)
            height = user32.GetSystemMetrics(SM_CYSCREEN)
            return (
                min(max(screen_x, 0.0), float(width - 1)),
                min(max(screen_y, 0.0), float(height - 1)),
            )
        with self._lock:
            region = self._region
        if region is not None:
            left, top, right, bottom = region
            # Keep the cursor just INSIDE the region instead of exactly on its
            # border.  A point that saturates on the tracked area's own border maps
            # onto the region's border pixel, and a cursor resting on the drawn
            # border is what the operator reports as "the aim is trapped at the
            # edge" - only 1-2px of it, but it is the visible edge of the rectangle
            # the pass draws around what the tracker is given.
            low_x, high_x = float(left) + _REGION_MARGIN_PX, float(right - 1) - _REGION_MARGIN_PX
            low_y, high_y = float(top) + _REGION_MARGIN_PX, float(bottom - 1) - _REGION_MARGIN_PX
            if high_x < low_x:                     # a region smaller than the margin
                low_x, high_x = float(left), float(right - 1)
            if high_y < low_y:
                low_y, high_y = float(top), float(bottom - 1)
            screen_x = min(max(screen_x, low_x), high_x)
            screen_y = min(max(screen_y, low_y), high_y)
        return (screen_x, screen_y)

    def _move_cursor(self, screen_x: float, screen_y: float) -> bool:
        pixel_x = int(round(screen_x))
        pixel_y = int(round(screen_y))
        if self._use_sendinput:
            sent = self._sendinput_move(pixel_x, pixel_y)
            if sent:
                return True
            # SendInput 失败时退化为 SetCursorPos
        return bool(user32.SetCursorPos(pixel_x, pixel_y))

    @staticmethod
    def _sendinput_move(pixel_x: int, pixel_y: int) -> bool:
        width = user32.GetSystemMetrics(SM_CXSCREEN)
        height = user32.GetSystemMetrics(SM_CYSCREEN)
        if width <= 1 or height <= 1:
            return False
        mouse_input = MOUSEINPUT(
            dx=int(round(pixel_x / (width - 1) * 65535)),
            dy=int(round(pixel_y / (height - 1) * 65535)),
            mouseData=0,
            dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
            time=0,
            dwExtraInfo=0,
        )
        event = INPUT(type=0, mi=mouse_input)  # type=0 -> INPUT_MOUSE
        return user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT)) == 1


def _selftest() -> None:
    """纯数学自检：不移动真实鼠标。"""
    controller = MouseAimController(320, 240, enabled=False)
    try:
        assert controller.map_to_screen(0, 0) is None, "region 未设置时应返回 None"

        controller.set_region(100, 50, 420, 290)  # region 与视频等比例
        assert controller.map_to_screen(0, 0) == (100.0, 50.0)
        assert controller.map_to_screen(320, 240) == (420.0, 290.0)
        mid = controller.map_to_screen(160, 120)
        assert mid is not None and abs(mid[0] - 260.0) < 1e-9 and abs(mid[1] - 170.0) < 1e-9

        controller.set_region(0, 0, 640, 480)  # 2 倍放大
        scaled = controller.map_to_screen(100, 50)
        assert scaled is not None and scaled == (200.0, 100.0)
        print("mouse_aim_controller selftest: OK")
    finally:
        controller.close()

    class _Label:
        def winfo_ismapped(self):
            return True

        def winfo_rootx(self):
            return 136

        def winfo_rooty(self):
            return 159

        def winfo_width(self):
            return 1084

        def winfo_height(self):
            return 744

    # 图片(1366x768)比 Label(1084x744)大：Tk 以 Label 中心对齐并裁掉多余部分，
    # 所以区域左上角在 Label 之外（旧代码夹成 0，导致准星与鼠标差 141px）。
    region = widget_image_region(_Label(), 1366, 768)
    assert region == (136 - 141, 159 - 12, 136 - 141 + 1366, 159 - 12 + 768), region
    print("mouse_aim_controller clipping selftest: OK")


if __name__ == "__main__":
    _selftest()
