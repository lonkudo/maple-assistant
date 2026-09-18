# -*- coding: utf-8 -*-
"""自动重连: the login-page colour gate and the keyboard reconnect sequence.

The flow is fixed by the operator: 掉线 event -> the game window must show the login page's
base colour (``screenshots/login_page_target.jpg`` is the colour reference) -> Enter -> 3s ->
walk down to the chosen world and confirm -> walk right/down to the chosen channel and confirm
-> 2s -> Enter.  Ticking the 自动重连 box only ARMS the worker; the drill starts on a 掉线 event
or on the temporary 测试自动重连（临时）button.

The sequence is **keyboard only** on purpose: the game opens each list on its first entry, so no
screen geometry has to be measured and the panel needs no window-capture or calibration button.
These tests cover the parts that can be checked without a game window: the colour gate, the
1-60 input rule, the move counts, and the order of the actions with a fake screen and keyboard.
"""

import queue
import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np

from reconnect_worker import (
    CHANNEL_MAX,
    WORLD_LIST_CLIENT_BOX,
    PAGE_CHANNEL,
    PAGE_LOGIN,
    PAGE_WORLD,
    CHANNEL_MIN,
    CHANNELS_PER_ROW,
    CHANNEL_FIRST_CLIENT,
    CHANNEL_LIST_CLIENT_BOX,
    CHANNEL_ROW_STEP_CLIENT,
    CHANNEL_STEP_CLIENT,
    CHANNEL_VISIBLE_ROWS,
    CHANNEL_ROW_STEP_CLIENT,
    CHANNEL_STEP_CLIENT,
    ReconnectWorker,
    WORLD_NAMES,
    find_login_page,
    load_login_reference,
    point_in_box,
    valid_channel,
)

class ChannelInputTests(unittest.TestCase):
    def test_only_integers_1_to_60_are_accepted(self):
        self.assertEqual(CHANNEL_MIN, 1)
        self.assertEqual(CHANNEL_MAX, 60)
        for good in (1, 60, "1", "60", " 7 ", 7):
            self.assertEqual(valid_channel(good), int(str(good).strip()), good)
        for bad in (0, 61, -1, "0", "61", "abc", "", None, "3.5", 3.5, "1e2"):
            self.assertIsNone(valid_channel(bad), bad)


class LoginPageColourTests(unittest.TestCase):
    """Sign two: the window must show the login page's base colour, over one big region.

    The reference crop is a patch of the page's cream background, so it defines a COLOUR to
    look for (base + narrow range), not a shape to correlate.  The negatives are the
    operator's own frames: two select windows (cream themed, 11.7 % one region) and three
    in-game frames (0.0-0.1 %).
    """

    @staticmethod
    def _reference():
        from reconnect_worker import login_colour_reference

        patch = np.zeros((200, 200, 3), dtype=np.uint8)
        patch[:, :] = (216, 232, 248)                      # cream, BGR
        patch[:100, :100] = (208, 224, 240)                # its own texture
        return login_colour_reference(patch)

    def test_the_base_colour_and_range_come_from_the_reference(self):
        reference = self._reference()
        self.assertIsNotNone(reference)
        for channel, expected in enumerate((216, 232, 248)):
            self.assertAlmostEqual(reference.base_bgr[channel], expected, delta=2)
            self.assertLessEqual(reference.lower_bgr[channel], expected)
            self.assertGreaterEqual(reference.upper_bgr[channel], expected)
        self.assertEqual(reference.pixels, 200 * 200)

    def test_a_login_page_frame_is_accepted(self):
        from reconnect_worker import find_login_page

        frame = np.zeros((768, 1366, 3), dtype=np.uint8)
        frame[100:700, 300:1100] = (216, 232, 248)         # 46 % of the window, one region
        evidence = find_login_page(frame, self._reference())
        self.assertIsNotNone(evidence)
        self.assertGreater(evidence.fraction, 0.4)
        self.assertEqual(evidence.area, 600 * 800)
        self.assertEqual(evidence.bbox, (300, 100, 800, 600))

    def test_a_frame_that_is_not_the_login_page_is_rejected(self):
        from reconnect_worker import find_login_page

        in_game = np.zeros((768, 1366, 3), dtype=np.uint8)
        in_game[:, :] = (40, 120, 60)                      # grass
        self.assertIsNone(find_login_page(in_game, self._reference()))
        # a small patch of the page colour is not enough either (window chrome, a button)
        small = np.zeros((768, 1366, 3), dtype=np.uint8)
        small[200:260, 400:520] = (216, 232, 248)
        self.assertIsNone(find_login_page(small, self._reference()))

    def test_the_operators_own_frames_separate_cleanly(self):
        import cv2

        from reconnect_worker import (LOGIN_PAGE_MIN_FRACTION, analyse_login_page,
                                      find_login_page, load_login_reference)

        folder = Path(__file__).resolve().parent / "screenshots"
        reference = load_login_reference(folder / "login_page_target.jpg")
        self.assertIsNotNone(reference, "the operator's own reference must load")
        # the login page and the two select windows are cream themed: one region covers 8-13 % of the
        # window - at 1366 and at 1080 (his second device, where the shipped 1366 crop left only a
        # 0.06 margin, which is why the reference is now his own 1080 background patch)
        for name in ("login_page.jpg", "channel_select_first.jpg", "channel_select_second.jpg",
                     "1080_login_page.jpg", "1080_wolrd_select.jpg", "1080_channel_select.jpg"):
            frame = cv2.imread(str(folder / name), cv2.IMREAD_COLOR)
            if frame is None:
                self.skipTest(f"{name} is missing")
            evidence = analyse_login_page(frame, reference)
            self.assertGreaterEqual(evidence.fraction, LOGIN_PAGE_MIN_FRACTION, name)
            self.assertIsNotNone(find_login_page(frame, reference), name)
        # ... and the in-game frames are nowhere near the threshold
        for name in ("second_window.jpg", "frame_000010.jpg"):
            frame = cv2.imread(str(folder / name), cv2.IMREAD_COLOR)
            if frame is None:
                self.skipTest(f"{name} is missing")
            evidence = analyse_login_page(frame, reference)
            self.assertLess(evidence.fraction, LOGIN_PAGE_MIN_FRACTION, name)
            self.assertIsNone(find_login_page(frame, reference), name)

    def test_a_missing_reference_file_is_reported_not_raised(self):
        from reconnect_worker import load_login_reference

        self.assertIsNone(load_login_reference(Path("does/not/exist.jpg")))
        self.assertIsNotNone(load_login_reference(
            Path(__file__).resolve().parent / "screenshots" / "login_page_target.jpg"))

    def test_the_reference_is_found_without_a_path(self):
        """The packaged copy lives in recording-assets, the personal one in screenshots."""

        from reconnect_worker import LOGIN_REFERENCE_PATHS, load_login_reference

        names = [Path(entry).name for entry in LOGIN_REFERENCE_PATHS]
        self.assertEqual(names.count("login_page_target.jpg"), len(names))
        folders = {Path(entry).parent.name for entry in LOGIN_REFERENCE_PATHS}
        self.assertEqual(folders, {"screenshots", "recording-assets"})
        # at least one of them exists in this checkout, and the loader finds it
        reference = load_login_reference()
        self.assertIsNotNone(reference)


class _PageMachine:
    """The page the fake game is showing, driven by the actions the worker sends.

    v0405: every step is verified by the PAGE classifier (login board / world list / channel grid)
    and retried when the page did not change.  A flat fake frame cannot answer that question, so the
    tests drive this little state machine instead:

    * a click on the 连接 point leaves the login page,
    * Enter confirms a selection: world -> channel -> in game,
    * a click on the world row or a channel cell only selects (the page does not change).
    """

    def __init__(self, page=PAGE_LOGIN, score=1.0, advances_on_enter=True, connect_ignored=0):
        self.page = page
        self.score = float(score)
        self.advances_on_enter = bool(advances_on_enter)
        # How many 连接 clicks the fake game ignores (0 = the click works).
        self.connect_ignored = int(connect_ignored)
        self.ignored_connects = 0
        self.history = []

    def __call__(self):
        return (self.page, self.score)

    def set(self, page):
        self.page = page
        self.history.append(page)

    def key(self, name):
        if name != "enter" or not self.advances_on_enter:
            return
        if self.page == PAGE_LOGIN:
            self.set(PAGE_WORLD)
        elif self.page == PAGE_WORLD:
            self.set(PAGE_CHANNEL)
        elif self.page == PAGE_CHANNEL:
            self.set(None)

    def clicked(self, x, y, rect=(100, 50, 1466, 818)):
        from reconnect_worker import LOGIN_CONNECT_CLICK_CLIENT, login_client_point

        client_x, client_y = int(x) - int(rect[0]), int(y) - int(rect[1])
        # The 连接 point is a preset-space constant, so the fake game has to map it the same way the
        # worker does - otherwise a smaller client would look like "the click missed the button".
        client_size = (int(rect[2]) - int(rect[0]), int(rect[3]) - int(rect[1]))
        expected = login_client_point(LOGIN_CONNECT_CLICK_CLIENT, client_size) \
            if LOGIN_CONNECT_CLICK_CLIENT is not None else None
        if expected is not None and abs(client_x - expected[0]) <= 6 and abs(
                client_y - expected[1]) <= 6:
            if self.page != PAGE_LOGIN:
                return
            if self.connect_ignored > 0:
                self.connect_ignored -= 1
                self.ignored_connects += 1
                return
            self.set(PAGE_WORLD)


def _page_aware_click(pages, click, rect=(100, 50, 1466, 818)):
    """Wrap a click_fn so the page machine learns about the click as well."""

    def wrapper(x, y):
        pages.clicked(x, y, rect)
        return click(x, y)

    return wrapper


def _page_fn(page=PAGE_LOGIN, *, advances_on_enter=True):
    """A page machine for a test that builds its own worker."""

    return _PageMachine(page, advances_on_enter=advances_on_enter)


def _channel_clicking_click_fn(screen, clicks=None, rect=(100, 50, 1466, 818), on_click=None):
    """A ``click_fn`` for a fake screen that shows a clicked channel cell as highlighted.

    v0402: the worker only sends Enter after it has SEEN the channel selection take, so a fake
    screen has to react to the click the way the game does - a click on a cell highlights it.
    Clicks outside the channel list (the login board, the world row) leave the picture alone.
    """

    def click(x, y):
        if clicks is not None:
            clicks.append((x, y))
        if on_click is not None:
            on_click(x, y)
        from reconnect_worker import window_client_box

        client_x, client_y = int(x) - int(rect[0]), int(y) - int(rect[1])
        size = tuple(screen.frame.shape[1::-1]) if screen.frame is not None else (1366, 768)
        if point_in_box(client_x, client_y, window_client_box(CHANNEL_LIST_CLIENT_BOX, size)):
            screen.react_to_channel_click(client_x, client_y)
        elif point_in_box(client_x, client_y, window_client_box(WORLD_LIST_CLIENT_BOX, size)):
            screen.react_to_world_click(client_x, client_y)
        return True

    return click


def _world_click_frame(frame, client_x, client_y):
    """The picture after a world row is clicked: that row is highlighted.

    v0420: the world step requires the click to MOVE the selection before Enter may confirm it (a click
    the game ignored, followed by Enter, confirmed the default world - "蘑菇仔 chosen, 蓝蜗牛 logged in").
    The fake therefore has to show the same thing the game does.
    """

    if frame is None:
        return frame
    from reconnect_worker import WORLD_LIST_CLIENT_BOX, window_client_box, window_client_length

    size = tuple(frame.shape[1::-1])
    left, top, width, height = window_client_box(WORLD_LIST_CLIENT_BOX, size)
    row_width = int(round(104 * window_client_length(1.0, size)))
    x0 = max(left, min(int(client_x) - row_width // 2, left + width - row_width))
    y0 = max(top, min(int(client_y) - 15, top + height - 30))
    out = frame.copy()
    out[y0:y0 + 30, x0:x0 + row_width] = (60, 140, 230)
    return out


def _channel_click_frame(frame, client_x, client_y):
    """The picture after a channel cell is clicked: the cell is highlighted.

    A single click only highlights a cell in the game, which is exactly why the sequence has to
    double-click - and the worker verifies the picture changed before it sends Enter.
    """

    if frame is None:
        return frame
    from reconnect_worker import (
        CHANNEL_LIST_CLIENT_BOX,
        window_client_box,
        window_client_length,
    )

    size = tuple(frame.shape[1::-1])
    left, top, width, height = window_client_box(CHANNEL_LIST_CLIENT_BOX, size)
    cell_width = int(round(94 * window_client_length(1.0, size)))
    cell_height = max(6, int(round(30 * window_client_length(1.0, size))))
    x0 = max(left, min(int(client_x) - cell_width // 2, left + width - cell_width))
    y0 = max(top, min(int(client_y) - cell_height // 2, top + height - cell_height))
    out = frame.copy()
    # BGR grey ~114: the highlight has to stand out from a DARK and from a LIGHT fake background,
    # because the measurement is a grey-level difference (the real highlight is this blue).
    out[y0:y0 + cell_height, x0:x0 + cell_width] = (230, 120, 60)
    return out


class _Screen:
    """A fake game window whose picture the test can change between captures."""

    def __init__(self, frame, rect=(100, 50, 1466, 818)):
        self.frame = frame
        self.rect = rect
        self.frames_returned = 0

    def __call__(self):
        self.frames_returned += 1
        return self.frame, self.rect

    def react_to_channel_click(self, client_x, client_y):
        """The game highlighted the clicked channel cell."""

        self.frame = _channel_click_frame(self.frame, client_x, client_y)

    def react_to_world_click(self, client_x, client_y):
        """The game highlighted the clicked world row."""

        self.frame = _world_click_frame(self.frame, client_x, client_y)

    def react_to_enter(self):
        """The game reacted to Enter: the picture becomes a different one (a real switch)."""

        if self.frame is None:
            return
        self.frame = np.full_like(self.frame, 30 if float(self.frame.mean()) > 100 else 200)


class _FakeSender:
    def __init__(self, *, on_press=None, pages=None):
        self.keys = []
        self.selected = 0
        self.on_press = on_press
        # The page machine (v0405): a key press has to move the fake game's page as well.
        self.pages = pages

    def select_window(self):
        self.selected += 1
        return True

    def is_game_foreground(self):
        return True

    def press(self, key, duration=0.025):
        self.keys.append(key)
        if self.pages is not None:
            self.pages.key(key)
        if self.on_press is not None:
            self.on_press(key)
        return True


class ReconnectSequenceTests(unittest.TestCase):
    """The order of actions, with a fake screen, keyboard and clock.

    The template is generated here (with real structure) because the operator's bundled
    `login_page_target.jpg` is a flat patch that the worker rightly refuses to trust.
    """

    def setUp(self):
        import cv2
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        folder = Path(self._tmp.name)
        # the colour reference: a crop of the login page's cream background
        reference = np.zeros((200, 200, 3), dtype=np.uint8)
        reference[:, :] = (216, 232, 248)
        reference[:80, :80] = (206, 222, 238)
        self.template_path = folder / "login_target.jpg"
        cv2.imwrite(str(self.template_path), reference)
        # a game window showing the login page: one large region of that colour
        login_frame = np.zeros((768, 1366, 3), dtype=np.uint8)
        login_frame[:, :] = (30, 40, 60)
        login_frame[80:700, 260:1120] = (216, 232, 248)
        self.login_frame = login_frame

    def tearDown(self):
        self._tmp.cleanup()

    def _worker(self, *, frame, rect=(100, 50, 1466, 818), enabled=True, sender=None,
                click_reacts_channels=False):
        self.sleeps = []
        # the activation click is recorded, never sent to the real screen
        self.clicks = []
        # the login page reacts as soon as Enter is accepted: the worker verifies that the screen
        # changed (see LoginEnterTests), so a static login frame would look like a stuck page
        screen = _Screen(frame, rect)
        if sender is not None:
            original_press = sender.press

            def press(key, duration=0.025, *, owner="", _original=original_press):
                try:
                    # v0411: the reconnect passes its owner so it keeps the keyboard it took
                    result = _original(key, duration=duration, owner=owner)
                except TypeError:
                    result = _original(key, duration=duration)
                # the game reacts to a key (the walk relies on seeing progress) and the page
                # state machine follows what the game would do
                screen.react_to_enter()
                self.pages.key(key)
                return result

            try:
                sender.press = press
            except Exception:
                pass

        self.wheels = []
        # The page the fake game shows (v0405: every step is verified by the page classifier).
        self.pages = _PageMachine()

        # A click on a channel cell highlights it in the game, which is what the worker checks
        # before it sends Enter.  Clicks elsewhere (the login board, the world row) leave the
        # picture alone, so the world row keeps exercising its keyboard fallback here.
        base_click = _channel_clicking_click_fn(screen, self.clicks, rect) if click_reacts_channels \
            else (lambda x, y: self.clicks.append((x, y)) or True)

        def click(x, y):
            self.pages.clicked(x, y, rect)
            return base_click(x, y)

        worker = ReconnectWorker(
            sender if sender is not None else _FakeSender(),
            threading.Event(),
            queue.Queue(maxsize=32),
            capture_fn=screen,
            template_path=self.template_path,
            sleep=lambda seconds: self.sleeps.append(seconds),
            click_fn=click,
            wheel_fn=lambda x, y, n: self.wheels.append((x, y, n)) or True,
            page_fn=self.pages,
        )
        worker.set_enabled(enabled)
        # The 掉线提示窗口 Enter (OFFLINE_PROMPT_ENTER) is covered by OfflinePromptTests; these flows
        # model a login page that is already usable, so the extra leading Enter is switched off here.
        worker.press_enter_for_offline_prompt = False
        return worker

    @staticmethod
    def _reports(worker) -> list:
        out = []
        while not worker.result_queue.empty():
            out.append(worker.result_queue.get_nowait())
        return out

    def test_the_sequence_is_enter_world_channel_moves_enter_enter(self):
        sender = _FakeSender()
        worker = self._worker(frame=self.login_frame, sender=sender, click_reacts_channels=True)
        worker.set_world("绿水灵")
        worker.set_channel(8)
        worker._handle_disconnect()

        # window brought forward, Enter sent; then the world walk (绿水灵 is row 3 -> two
        # rights) confirmed with Enter; then channel 8 is SCROLLED to and DOUBLE-CLICKED - the
        # keyboard walk through the channels is gone (v0402) - and the two Enters that confirm
        # the channel and start the login
        self.assertEqual(sender.selected, 1)
        # the 连接 click leaves the login page (verified), the world row click + Enter opens the
        # channels (verified), the channel double-click + Enter + Enter start the login
        self.assertEqual(sender.keys, ["enter", "enter", "enter"], sender.keys)
        # no keyboard walk at all any more: the page verifier showed the steps worked
        self.assertNotIn("down", sender.keys, sender.keys)
        self.assertNotIn("right", sender.keys, sender.keys)
        # the operator's pauses: 3s after Enter, the select-window pause, then 2s between the
        # two Enters at the end
        self.assertIn(5.0, self.sleeps, "5 s after the login Enter (operator's rule)")
        self.assertIn(2.0, self.sleeps, "2 s before each next step (operator's rule)")
        self.assertLess(self.sleeps.index(5.0), len(self.sleeps) - 1,
                        "the login wait happens before the later steps")

    def test_a_manual_test_runs_even_when_the_checkbox_is_off(self):
        sender = _FakeSender()
        worker = self._worker(frame=self.login_frame, sender=sender, enabled=False,
                              click_reacts_channels=True)
        worker.set_world("蘑菇仔")
        worker.set_channel(3)
        # the 掉线 trigger stays disabled ...
        worker.notify_disconnect()
        worker._handle_disconnect()
        self.assertEqual(sender.keys, [])
        # ... but the temporary test button runs the whole sequence once
        self.assertTrue(worker.trigger_test())
        worker._handle_disconnect()
        self.assertEqual(sender.keys, ["enter", "enter", "enter"])
        self.assertIn(("done", "蘑菇仔 3频道"), self._reports(worker))

    def test_a_manual_test_does_not_need_the_login_page_colour(self):
        """The button is pressed while the operator looks at the login page themselves."""

        sender = _FakeSender()
        worker = self._worker(frame=np.zeros((768, 1366, 3), dtype=np.uint8),
                              sender=sender, enabled=False, click_reacts_channels=True)
        worker.set_channel(1)
        self.assertTrue(worker.trigger_test())
        worker._handle_disconnect()
        # no login page colour in the frame, and the sequence still ran end to end:
        # world Enter, channel confirm, login Enter (the 连接 click left the login page)
        self.assertEqual(sender.keys, ["enter", "enter", "enter"])

    def test_a_manual_test_reports_the_login_page_colour_measurement(self):
        worker = self._worker(frame=self.login_frame)
        evidence = worker.measure_login_page_colour()
        self.assertIsNotNone(evidence)
        self.assertAlmostEqual(evidence.fraction, 620 * 860 / (768 * 1366), delta=0.01)
        self.assertTrue(worker.trigger_test())
        worker._handle_disconnect()
        colours = [detail for state, detail in self._reports(worker) if state == "colour"]
        self.assertTrue(colours)
        self.assertIn("判定为登录页", colours[-1])
        self.assertIn("阈值", colours[-1])

    def test_the_test_button_is_refused_while_a_run_is_going_on(self):
        worker = self._worker(frame=self.login_frame)
        self.assertTrue(worker.trigger_test())
        self.assertFalse(worker.trigger_test())
        self.assertFalse(worker.trigger_test())
        worker._handle_disconnect()               # the queued run is consumed
        self.assertTrue(worker.trigger_test(), "a finished run must re-arm the button")

    def test_without_the_login_page_colour_nothing_is_pressed(self):
        sender = _FakeSender()
        worker = self._worker(frame=np.zeros((768, 1366, 3), dtype=np.uint8), sender=sender)
        worker._handle_disconnect()
        self.assertEqual(sender.keys, [])
        failed = [detail for state, detail in self._reports(worker) if state == "failed"]
        self.assertTrue(failed)
        self.assertIn("登录页", " ".join(failed))

    @staticmethod
    def _loop_once(worker):
        """One iteration of ReconnectWorker.run()'s loop (the real trigger path)."""

        if worker._wake.wait(0.05):
            worker._wake.clear()
            worker._handle_disconnect()

    def test_enabling_the_checkbox_only_arms_the_worker(self):
        """The operator's rule: ticking 自动重连 must not run the drill."""

        sender = _FakeSender()
        worker = self._worker(frame=self.login_frame, sender=sender, enabled=False)
        worker.set_enabled(True)
        # arming must not wake the loop, so the loop finds nothing to do
        self.assertFalse(worker._wake.wait(0.05),
                         "enabling must not wake the worker for a run")
        self._loop_once(worker)
        self.assertEqual(sender.keys, [])
        self.assertEqual(sender.selected, 0)
        # only the 掉线 event (or the test button) starts the sequence
        worker.notify_disconnect()
        self._loop_once(worker)
        self.assertEqual(sender.keys[0], "enter")

    def test_disabled_means_no_action_at_all(self):
        sender = _FakeSender()
        worker = self._worker(frame=self.login_frame, sender=sender, enabled=False)
        worker._handle_disconnect()
        self.assertEqual(sender.keys, [])
        self.assertEqual(sender.selected, 0)

    def test_the_channel_of_channel_one_needs_no_channel_move(self):
        sender = _FakeSender()
        worker = self._worker(frame=self.login_frame, sender=sender, click_reacts_channels=True)
        worker.set_channel(1)
        worker._handle_disconnect()
        # world Enter (row 1: no move needed), channel confirm, login Enter
        self.assertEqual(sender.keys, ["enter", "enter", "enter"])

    def test_the_settings_are_validated(self):
        worker = self._worker(frame=self.login_frame)
        self.assertTrue(worker.set_world("蘑菇仔"))
        self.assertFalse(worker.set_world("没有这个区"))
        self.assertTrue(worker.set_channel(7))
        self.assertFalse(worker.set_channel(0))
        self.assertFalse(worker.set_channel(61))
        self.assertFalse(worker.set_channel("abc"))
        self.assertFalse(worker.set_channel(3.5))
        _, world, channel = worker.settings()
        self.assertEqual((world, channel), ("蘑菇仔", 7))


class PanelInputTests(unittest.TestCase):
    """The 自动重连 box in the Additional Functions panel only takes 1-60."""

    def test_the_spinbox_validator_rejects_everything_but_1_to_60(self):
        from ui_worker import UiWorker

        validator = UiWorker._validate_reconnect_channel
        for good in ("1", "9", "60", ""):        # "" = mid-edit, checked on change
            self.assertTrue(validator(None, good), good)
        for bad in ("0", "61", "61 ", "12a", "-1", "1.5", "abc"):
            self.assertFalse(validator(None, bad), bad)

    def test_the_change_handler_writes_all_three_settings(self):
        from ui_worker import UiWorker

        class _Var:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        class _Worker:
            def __init__(self):
                self.calls = []

            def set_world(self, world):
                self.calls.append(("world", world))
                return True

            def set_channel(self, channel):
                self.calls.append(("channel", channel))
                return True

            def set_enabled(self, enabled):
                self.calls.append(("enabled", enabled))

        class _Status:
            def __init__(self):
                self.text = ""

            def configure(self, **kwargs):
                self.text = kwargs.get("text", "")

        class _Ui:
            _validate_reconnect_channel = UiWorker._validate_reconnect_channel
            _reconnect_on_change = UiWorker._reconnect_on_change
            _reconnect_test_idle = UiWorker._reconnect_test_idle
            _save_reconnect_settings = UiWorker._save_reconnect_settings

            def __init__(self):
                self._reconnect_var = _Var(True)
                self._reconnect_world_var = _Var("漂漂猪")
                self._reconnect_channel_var = _Var("12")
                self._reconnect_status = _Status()
                self.reconnect_worker = _Worker()
                self.saved = None

            def _shutdown_save_settings(self, data):
                self.saved = data

            def _shutdown_collect_data(self):
                return {
                    "auto_reconnect_enabled": bool(self._reconnect_var.get()),
                    "auto_reconnect_world": self._reconnect_world_var.get(),
                    "auto_reconnect_channel": int(self._reconnect_channel_var.get()),
                }

        ui = _Ui()
        ui._reconnect_on_change()
        self.assertEqual(ui.reconnect_worker.calls,
                         [("world", "漂漂猪"), ("channel", 12), ("enabled", True)])
        self.assertEqual(ui.saved["auto_reconnect_world"], "漂漂猪")
        self.assertEqual(ui.saved["auto_reconnect_channel"], 12)
        self.assertIn("漂漂猪", ui._reconnect_status.text)

    def test_an_out_of_range_channel_falls_back_to_the_default(self):
        from ui_worker import UiWorker

        class _Var:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        class _Worker:
            def __init__(self):
                self.calls = []

            def set_world(self, world):
                return True

            def set_channel(self, channel):
                self.calls.append(channel)
                return True

            def set_enabled(self, enabled):
                pass

        class _Ui:
            _validate_reconnect_channel = UiWorker._validate_reconnect_channel
            _reconnect_on_change = UiWorker._reconnect_on_change
            _reconnect_test_idle = UiWorker._reconnect_test_idle

            def __init__(self):
                self._reconnect_var = _Var(False)
                self._reconnect_world_var = _Var("蓝蜗牛")
                self._reconnect_channel_var = _Var("99")   # typed past the limit
                self._reconnect_status = type("S", (), {
                    "configure": lambda self, **kw: None})()
                self.reconnect_worker = _Worker()

            def _save_reconnect_settings(self, *args):
                pass

        ui = _Ui()
        ui._reconnect_on_change()
        self.assertEqual(ui._reconnect_channel_var.get(), "1",
                         "an out-of-range entry must fall back to a valid channel")
        self.assertEqual(ui.reconnect_worker.calls, [1])


class TemporaryTestButtonTests(unittest.TestCase):
    """The temporary 测试重连 button: trigger the sequence now, re-arm when it is over."""

    @staticmethod
    def _ui(worker):
        from ui_worker import UiWorker

        class _Var:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        class _Status:
            def __init__(self):
                self.text = ""

            def configure(self, **kwargs):
                self.text = kwargs.get("text", "")

        class _Button:
            def __init__(self):
                self.state = "normal"

            def configure(self, **kwargs):
                self.state = kwargs.get("state", self.state)

        class _Uri(UiWorker):
            def __init__(self):
                self._reconnect_var = _Var(False)      # checkbox deliberately off
                self._reconnect_world_var = _Var("小白兔")
                self._reconnect_channel_var = _Var("4")
                self._reconnect_status = _Status()
                self._reconnect_test_button = _Button()
                self._reconnect_capture_button = _Button()
                self.reconnect_worker = worker
                self.error_log_blocks = []

            def _save_reconnect_settings(self, *args):
                pass

            def _play_action_sound(self, success):      # no mp3 during tests
                pass

            def _append_error_log(self, text, tag):     # never touch the real error.log
                self.error_log_blocks.append((tag, text))

        return _Uri()

    def test_the_button_applies_the_settings_and_starts_one_run(self):
        started = []

        class _Worker:
            def set_world(self, world):
                return True

            def set_channel(self, channel):
                return True

            def set_enabled(self, enabled):
                pass

            def trigger_test(self):
                started.append(True)
                return True

        ui = self._ui(_Worker())
        ui._reconnect_test_clicked()
        self.assertEqual(
            (ui._reconnect_world_var.get(), ui._reconnect_channel_var.get()),
            ("小白兔", "4"),
        )
        self.assertEqual(len(started), 1)
        self.assertEqual(ui._reconnect_test_button.state, "disabled")
        self.assertIn("手动测试", ui._reconnect_status.text)

    def test_a_run_in_progress_is_reported_and_a_finished_run_re_arms(self):
        import queue as _queue

        class _Worker:
            def set_world(self, world):
                return True

            def set_channel(self, channel):
                return True

            def set_enabled(self, enabled):
                pass

            def trigger_test(self):
                return False                            # already running

        ui = self._ui(_Worker())
        ui._reconnect_test_clicked()
        self.assertIn("已有一次重连", ui._reconnect_status.text)

        # the worker's own report re-arms the button
        results = _queue.Queue()
        results.put(("done", "小白兔 4频道"))
        ui.reconnect_results = results
        ui._drain_reconnect_results()
        self.assertEqual(ui._reconnect_test_button.state, "normal")

    def test_a_failure_is_written_to_error_log_with_the_settings(self):
        import queue as _queue

        class _Worker:
            def set_world(self, world):
                return True

            def set_channel(self, channel):
                return True

            def set_enabled(self, enabled):
                pass

            def trigger_test(self):
                return True

        ui = self._ui(_Worker())
        results = _queue.Queue()
        results.put(("failed", "实时输入未开启（请点击 开始巡逻 启用输入后再试）"))
        ui.reconnect_results = results
        ui._drain_reconnect_results()
        self.assertIn("自动重连失败", ui._reconnect_status.text)
        self.assertEqual(len(ui.error_log_blocks), 1)
        tag, text = ui.error_log_blocks[0]
        self.assertEqual(tag, "auto reconnect")
        self.assertIn("实时输入未开启", text)
        self.assertIn("小白兔", text)                   # the run's own settings
        self.assertIn("频道=4", text)

    def test_the_login_page_colour_measurement_is_shown(self):
        import queue as _queue

        class _Worker:
            def set_world(self, world):
                return True

            def set_channel(self, channel):
                return True

            def set_enabled(self, enabled):
                pass

            def trigger_test(self):
                return True

        ui = self._ui(_Worker())
        results = _queue.Queue()
        results.put(("colour", "同色区域 50.8%（阈值 6.0%），判定为登录页"))
        ui.reconnect_results = results
        ui._drain_reconnect_results()
        self.assertIn("登录页颜色检测", ui._reconnect_status.text)
        self.assertIn("50.8", ui._reconnect_status.text)


class _ArmableFakeSender(_FakeSender):
    """A sender whose live input must be armed before it delivers any key.

    This is what WindowKeySender does in the field (`input_is_enabled` is False until
    开始巡逻/Start Patrol), and the reason the first field test ended with
    "key enter was not claimed".
    """

    def __init__(self, *, enabled: bool = False):
        super().__init__()
        self.input_enabled = bool(enabled)
        self.armed = 0
        self.disarmed = 0

    def input_is_enabled(self):
        return self.input_enabled

    def enable_input(self):
        self.armed += 1
        self.input_enabled = True

    def disable_input(self, **kwargs):
        self.disarmed += 1
        self.input_enabled = False

    def press(self, key, duration=0.025):
        if not self.input_enabled:
            return False                       # exactly the field behaviour
        self.keys.append(key)
        return True


class LiveInputTests(unittest.TestCase):
    """The reconnect must be able to send keys, and every failure must reach error.log."""

    def setUp(self):
        self._case = ReconnectSequenceTests("test_the_settings_are_validated")
        self._case.setUp()

    def tearDown(self):
        self._case.tearDown()

    def _worker(self, **kwargs):
        return self._case._worker(**kwargs)

    def test_the_sequence_arms_live_input_and_puts_the_state_back(self):
        sender = _ArmableFakeSender(enabled=False)
        worker = self._worker(frame=self._case.login_frame, sender=sender,
                              click_reacts_channels=True)
        worker.set_channel(1)
        worker._handle_disconnect()
        self.assertEqual(sender.keys, ["enter", "enter", "enter"])
        self.assertEqual(sender.armed, 1, "a disarmed sender must be armed for the drill")
        self.assertEqual(sender.disarmed, 1, "and disarmed again afterwards")
        self.assertFalse(sender.input_enabled)
        reports = []
        while not worker.result_queue.empty():
            reports.append(worker.result_queue.get_nowait())
        self.assertTrue(any(state == "input" for state, _detail in reports), reports)
        self.assertIn(("done", "蓝蜗牛 1频道"), reports)

    def test_an_already_armed_input_is_left_alone(self):
        sender = _ArmableFakeSender(enabled=True)
        worker = self._worker(frame=self._case.login_frame, sender=sender,
                              click_reacts_channels=True)
        worker.set_channel(1)
        worker._handle_disconnect()
        self.assertEqual(sender.keys, ["enter", "enter", "enter"])
        self.assertEqual((sender.armed, sender.disarmed), (0, 0))
        self.assertTrue(sender.input_enabled)

    def test_a_key_that_cannot_be_sent_says_why_and_logs_an_error(self):
        class _BlockedSender(_FakeSender):
            """Disarmed input and no way to arm it (no enable_input)."""

            def input_is_enabled(self):
                return False

        sender = _BlockedSender()
        worker = self._worker(frame=self._case.login_frame, sender=sender)
        with self.assertLogs("reconnect_worker", level="ERROR") as captured:
            worker._handle_disconnect()
        self.assertEqual(sender.keys, [])
        reports = []
        while not worker.result_queue.empty():
            reports.append(worker.result_queue.get_nowait())
        failed = [detail for state, detail in reports if state == "failed"]
        self.assertTrue(any("实时输入未开启" in detail for detail in failed), reports)
        self.assertTrue(any("live input is disarmed" in line for line in captured.output),
                        captured.output)

    def test_a_window_that_cannot_be_captured_is_reported_as_such(self):
        """No frame at all is not "not the login page" - it must say so."""

        worker = self._worker(frame=None)
        with self.assertLogs("reconnect_worker", level="ERROR") as captured:
            worker._handle_disconnect()
        reports = []
        while not worker.result_queue.empty():
            reports.append(worker.result_queue.get_nowait())
        failed = [detail for state, detail in reports if state == "failed"]
        self.assertTrue(any("无法截取游戏窗口" in detail for detail in failed), reports)
        self.assertTrue(any("could not be captured" in line for line in captured.output),
                        captured.output)

    def test_the_real_key_sender_is_armed_so_enter_reaches_the_game(self):
        """The field failure end to end, with the real WindowKeySender.

        Measured in the field (13:01:57): live input was disarmed, so `press` refused every
        key - `key enter was not claimed` - and Enter never reached the game.  With the real
        sender class here, nothing at all may be emitted unless the worker arms it first.
        """

        from status_worker import WindowKeySender

        sender = WindowKeySender("game", dry_run=False, input_enabled=False)
        sender.select_window = lambda: True
        sender.is_game_foreground = lambda: True
        sender._foreground_matches = lambda: True
        emitted = []
        sender._send_scan_code = lambda code, key_up, extended: emitted.append(
            (code, key_up, extended))
        self.assertFalse(sender.press("enter"), "a disarmed sender refuses keys (field case)")

        worker = self._worker(frame=self._case.login_frame, sender=sender,
                              click_reacts_channels=True)
        worker.set_channel(1)
        worker._handle_disconnect()

        enter_code = WindowKeySender._SCAN["enter"][0]
        downs = [event for event in emitted if event[0] == enter_code and event[1] is False]
        self.assertEqual(len(downs), 3, f"three Enters must reach the game: {emitted}")
        self.assertFalse(sender.input_is_enabled(),
                         "the previous (disarmed) state must be restored")
        reports = []
        while not worker.result_queue.empty():
            reports.append(worker.result_queue.get_nowait())
        self.assertIn(("done", "蓝蜗牛 1频道"), reports)

    def test_every_failure_is_logged_at_error_level(self):
        """error.log only receives ERROR+, so a stopped run must be an ERROR record."""

        worker = self._worker(frame=np.zeros((768, 1366, 3), dtype=np.uint8))
        with self.assertLogs("reconnect_worker", level="ERROR") as captured:
            worker._handle_disconnect()
        self.assertTrue(
            any("auto reconnect failed" in line for line in captured.output),
            captured.output,
        )


class TkThreadingTests(unittest.TestCase):
    """The UI thread must talk to Tk through widgets, never through the UiWorker itself.

    Measured failure: `self.register(...)` for the 频道 Spinbox raised
    ``AttributeError: 'UiWorker' object has no attribute 'register'`` while the panel was
    being built, which stopped the assistant from starting at all.  `self.after(...)` was
    the same mistake waiting to happen (it only triggers when 自动重连 was saved enabled).
    """

    TK_ONLY_ON_SELF = {
        "register", "after", "after_cancel", "after_idle", "bind", "title", "geometry",
        "protocol", "mainloop", "update_idletasks", "update", "destroy", "quit",
        "winfo_children", "winfo_width", "winfo_height", "winfo_exists", "wm_attributes",
        "grab_current", "clipboard_append", "wait_variable", "wait_window",
    }

    def test_no_tk_method_is_called_on_the_worker(self):
        import ast

        source = (Path(__file__).resolve().parent / "ui_worker.py").read_text(
            encoding="utf-8")
        offenders = []
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            name = node.func.attr
            if (name in self.TK_ONLY_ON_SELF
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "self"):
                offenders.append((node.lineno, name))
        self.assertEqual(
            offenders, [],
            "Tk methods must be called on a widget or on self._root, not on the UiWorker: "
            f"{offenders}",
        )

    def test_the_channel_validator_is_registered_on_a_widget(self):
        import tkinter as tk

        from ui_worker import UiWorker

        try:
            root = tk.Tk()
        except Exception as exc:                    # no display: nothing to check here
            self.skipTest(f"Tk is not available: {exc}")
        try:
            root.withdraw()
            frame = tk.Frame(root)
            ui = UiWorker.__new__(UiWorker)          # only the helper is needed
            check = ui._register_validator(
                frame, lambda proposed: UiWorker._validate_reconnect_channel(
                    None, proposed)
            )
            box = tk.Spinbox(frame, from_=1, to=60, validate="key",
                             validatecommand=check)
            box.delete(0, "end")                     # the box starts at from_ (= 1)
            box.insert(0, "6")
            self.assertEqual(box.get(), "6")
            box.insert("end", "1")                   # 61 is not a channel
            self.assertEqual(box.get(), "6", "Tk must refuse the 61 keypress")
            box.delete(0, "end")
            box.insert(0, "12")
            self.assertEqual(box.get(), "12")
        finally:
            root.destroy()


class LoginReferenceAssetTests(unittest.TestCase):
    """The worker's own reference file: it must be read as a COLOUR, not as a shape."""

    def test_the_operators_reference_gives_the_cream_base_colour(self):
        from reconnect_worker import load_login_reference

        reference = load_login_reference(
            Path(__file__).resolve().parent / "screenshots" / "login_page_target.jpg")
        self.assertIsNotNone(reference)
        # measured on the operator's own crop: warm cream, B < G < R
        blue, green, red = reference.base_bgr
        self.assertLess(blue, green)
        self.assertLess(green, red)
        self.assertGreaterEqual(blue, 180)
        for channel in range(3):
            self.assertLessEqual(reference.lower_bgr[channel],
                                 reference.base_bgr[channel])
            self.assertLessEqual(reference.base_bgr[channel],
                                 reference.upper_bgr[channel])

    def test_a_frame_without_the_colour_stops_the_worker_before_any_key(self):
        sender = _FakeSender()
        worker = ReconnectWorker(
            sender,
            threading.Event(),
            queue.Queue(maxsize=32),
            capture_fn=lambda: (np.zeros((768, 1366, 3), dtype=np.uint8),
                                (0, 0, 1366, 768)),
            template_path=Path(__file__).resolve().parent / "screenshots"
            / "login_page_target.jpg",
            sleep=lambda seconds: None,
        )
        worker.set_enabled(True)
        worker.notify_disconnect()
        worker._handle_disconnect()
        self.assertEqual(sender.keys, [])
        reports = []
        while not worker.result_queue.empty():
            reports.append(worker.result_queue.get_nowait())
        self.assertTrue(any(state == "failed" for state, _detail in reports), reports)

    def test_a_missing_reference_stops_the_worker_with_a_clear_report(self):
        sender = _FakeSender()
        worker = ReconnectWorker(
            sender,
            threading.Event(),
            queue.Queue(maxsize=32),
            capture_fn=lambda: (np.zeros((768, 1366, 3), dtype=np.uint8),
                                (0, 0, 1366, 768)),
            template_path=Path("does/not/exist.jpg"),
            sleep=lambda seconds: None,
        )
        worker.set_enabled(True)
        worker.notify_disconnect()
        worker._handle_disconnect()
        self.assertEqual(sender.keys, [])
        reports = []
        while not worker.result_queue.empty():
            reports.append(worker.result_queue.get_nowait())
        failed = [detail for state, detail in reports if state == "failed"]
        self.assertTrue(any("颜色参考图" in detail for detail in failed), reports)


class LoginEnterTests(unittest.TestCase):
    """Enter on the login page is verified, not assumed (measured failure at 22:35)."""

    def setUp(self):
        self._case = ReconnectSequenceTests("test_the_settings_are_validated")
        self._case.setUp()

    def tearDown(self):
        self._case.tearDown()

    def _worker(self, screen, **kwargs):
        kwargs.setdefault("click_fn", _channel_clicking_click_fn(screen))
        kwargs.setdefault("page_fn", _page_fn())
        worker = ReconnectWorker(
            kwargs.pop("sender", _FakeSender()),
            threading.Event(),
            queue.Queue(maxsize=64),
            capture_fn=screen,
            template_path=self._case.template_path,
            sleep=lambda seconds: None,
            wheel_fn=lambda x, y, n: True,
            **kwargs,
        )
        worker.set_enabled(True)
        worker.press_enter_for_offline_prompt = False     # see the shared factory's note
        return worker

    def test_the_screen_change_is_what_verifies_the_enter(self):
        screen = _Screen(self._case.login_frame)
        sender = _FakeSender(on_press=lambda key: screen.react_to_enter())
        worker = self._worker(screen, sender=sender)
        worker.set_world("蓝蜗牛")
        worker.set_channel(1)
        worker._handle_disconnect()
        # the 连接 click left the login page, the world Enter opened the channels, then the
        # channel confirm and the login Enter
        self.assertEqual(sender.keys, ["enter", "enter", "enter"])
        self.assertGreater(screen.frames_returned, 2,
                           "the frame must be re-captured to compare it")

    def test_enter_is_pressed_again_while_the_screen_never_changes(self):
        """The field case: the key was delivered and nothing happened."""

        screen = _Screen(self._case.login_frame)          # static picture
        sender = _FakeSender()
        # the page never changes either: the step is retried, and each retry presses Enter once
        worker = self._worker(screen, sender=sender, page_fn=_page_fn(advances_on_enter=False))
        worker.set_channel(1)
        worker._handle_disconnect()
        from reconnect_worker import PAGE_STEP_ATTEMPTS

        self.assertEqual(sender.keys, ["enter"] * PAGE_STEP_ATTEMPTS,
                         "no world/channel key may be sent while the page is static")
        reports = []
        while not worker.result_queue.empty():
            reports.append(worker.result_queue.get_nowait())
        failed = [detail for state, detail in reports if state == "failed"]
        # the step was retried and each retry pressed Enter once, so the run ends with the step
        # failure - the whole step is what the operator asked to be verified and retried
        self.assertTrue(any("登录步骤失败" in detail for detail in failed), reports)

    def test_a_login_enter_uses_a_longer_hold(self):
        from reconnect_worker import KEY_HOLD_SECONDS, LOGIN_ENTER_HOLD_SECONDS

        self.assertGreater(LOGIN_ENTER_HOLD_SECONDS, KEY_HOLD_SECONDS)

        holds = []

        class _RecordingSender(_FakeSender):
            def press(self, key, duration=0.025):
                holds.append((key, duration))
                return super().press(key)

        screen = _Screen(self._case.login_frame)
        sender = _RecordingSender(on_press=lambda key: screen.react_to_enter())
        worker = self._worker(screen, sender=sender)
        worker.set_channel(1)
        worker._handle_disconnect()
        first = next(duration for key, duration in holds if key == "enter")
        self.assertEqual(first, LOGIN_ENTER_HOLD_SECONDS)
        self.assertTrue(all(duration < LOGIN_ENTER_HOLD_SECONDS
                            for key, duration in holds if key != "enter"), holds)

    def test_a_stolen_foreground_is_taken_back_instead_of_failing(self):
        """Measured at 22:35:23: the assistant's own window took focus mid-sequence."""

        state = {"foreground": True}

        class _FocusSender(_FakeSender):
            def is_game_foreground(self):
                return state["foreground"]

        sender = _FocusSender(on_press=lambda key: state.__setitem__("foreground", False))
        worker = ReconnectWorker(
            sender,
            threading.Event(),
            queue.Queue(maxsize=64),
            capture_fn=lambda: (np.zeros((768, 1366, 3), dtype=np.uint8),
                                (0, 0, 1366, 768)),
            template_path=self._case.template_path,
            sleep=lambda seconds: None,
            wheel_fn=lambda x, y, n: True,
        )
        worker.set_enabled(True)
        # the window is brought forward again, so the key goes through instead of failing
        worker._prepare_window = lambda: state.__setitem__("foreground", True)
        reason = worker._press("enter")
        self.assertIsNone(reason, reason)
        self.assertEqual(sender.keys, ["enter"])


class ActivationClickTests(unittest.TestCase):
    """The game ignores keys until its window has been clicked (field measurement, v0391/v0392).

    The operator's order is fixed: focus the window -> click a harmless spot inside the client ->
    press Enter.  The click point is client (50, 50), mapped through the captured window rect.
    """

    def setUp(self):
        self._case = ReconnectSequenceTests("test_the_settings_are_validated")
        self._case.setUp()

    def tearDown(self):
        self._case.tearDown()

    def test_focus_then_click_then_enter(self):
        from reconnect_worker import ACTIVATE_CLICK_CLIENT

        order = []

        class _Sender(_FakeSender):
            def select_window(self):
                order.append("focus")
                return super().select_window()

            def press(self, key, duration=0.025):
                order.append(f"press {key}")
                return super().press(key, duration=duration)

        sender = _Sender(on_press=lambda key: None)
        screen = _Screen(self._case.login_frame)
        sender.on_press = lambda key: screen.react_to_enter()
        clicks = []
        pages = _page_fn()
        sender.pages = pages

        def record_click(x, y):
            order.append("click")
            clicks.append((x, y))
            return True

        # The activation order (focus -> click -> key) is what this test asserts; the extra
        # 掉线提示窗口 Enter has its own tests (OfflinePromptTests).

        worker = ReconnectWorker(
            sender,
            threading.Event(),
            queue.Queue(maxsize=64),
            capture_fn=screen,
            template_path=self._case.template_path,
            sleep=lambda seconds: None,
            click_fn=_page_aware_click(pages, record_click),
            page_fn=pages,
            wheel_fn=lambda x, y, n: True,
        )
        worker.set_enabled(True)
        worker.press_enter_for_offline_prompt = False     # see the shared factory's note
        worker.set_world("蓝蜗牛")
        worker.set_channel(1)
        worker._handle_disconnect()

        from reconnect_worker import (
            CHANNEL_FIRST_CLIENT, LOGIN_CONNECT_CLICK_CLIENT, WORLD_ROW_CLIENT,
        )

        # focus -> activation click -> 连接 click -> world row click -> channel cell
        # DOUBLE-clicked (two clicks on the same cell), because a single click only highlights it
        self.assertEqual(order[0], "focus", order)
        self.assertEqual(order[1], "click", order)
        self.assertEqual(order[2], "click", order)
        # a fallback walk may reset the selection by clicking channel 1 again, so only the first
        # five clicks are asserted here (the measured double-click pair is clicks 4 and 5)
        self.assertEqual(len(clicks) >= 5, True, clicks)
        from reconnect_worker import LOGIN_BOARD_CLICK_CLIENT

        self.assertEqual(clicks[0], (100 + ACTIVATE_CLICK_CLIENT[0],
                                     50 + ACTIVATE_CLICK_CLIENT[1]))
        self.assertEqual(clicks[1], (100 + LOGIN_BOARD_CLICK_CLIENT[0],
                                     50 + LOGIN_BOARD_CLICK_CLIENT[1]))
        self.assertEqual(clicks[2], (100 + LOGIN_CONNECT_CLICK_CLIENT[0],
                                     50 + LOGIN_CONNECT_CLICK_CLIENT[1]))
        self.assertEqual(clicks[3], (100 + WORLD_ROW_CLIENT[0], 50 + WORLD_ROW_CLIENT[1]))
        self.assertEqual(clicks[4], clicks[5],
                         "the channel cell is double-clicked at one point")
        self.assertEqual(clicks[4], (100 + CHANNEL_FIRST_CLIENT[0],
                                     50 + CHANNEL_FIRST_CLIENT[1]))
        # client (50, 50) of a window whose origin is (100, 50)
        self.assertEqual(clicks[0], (100 + ACTIVATE_CLICK_CLIENT[0],
                                     50 + ACTIVATE_CLICK_CLIENT[1]))

    def test_the_click_point_is_the_operators_harmless_spot(self):
        from reconnect_worker import ACTIVATE_CLICK_CLIENT

        self.assertEqual(ACTIVATE_CLICK_CLIENT, (50, 50))

    def test_no_click_is_sent_to_the_real_screen_in_the_tests(self):
        """Every test injects its own click function; the default one really clicks."""

        import reconnect_worker as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        self.assertIn("def click_screen(", source)
        self.assertIn("user32.mouse_event", source)
        # ... and it goes through SendInput: this game ignores the legacy mouse_event path, which is
        # what made the channel cell impossible to enter ("i can see that the mouse is double clicking
        # but it just cannot enter that channel, however my manual operation is good")
        self.assertIn("user32.SendInput", source)
        self.assertIn("def send_input_click(", source)


class ClientSpaceTests(unittest.TestCase):
    """The operator's second device: a **1080x768** client, on which the reconnect failed.

    Every client constant is a 1366x768 measurement, and nothing was mapped, so on a 1080-wide client
    the 连接 click went to x 854 instead of 721 - 143 px = (1366-1080)/2 to the RIGHT of the button,
    and the page patches read the wrong pixels (`detect_page` scored his login page -0.171, world
    0.129, channel 0.046, all below the 0.60 gate).  The two mapping families below are measured on
    his own 1080 screenshots (work/anchors_1080.json, work/probe_1080_scale.py).
    """

    @staticmethod
    def _client(size):
        return size

    def test_the_preset_client_maps_to_itself(self):
        from reconnect_worker import (
            CHANNEL_FIRST_CLIENT, LOGIN_BOARD_CLIENT_BOX, LOGIN_CONNECT_CLICK_CLIENT,
            SELECT_WINDOW_CLIENT_BOX, WORLD_ROW_CLIENT, login_client_box, login_client_point,
            window_client_box, window_client_point,
        )

        size = (1366, 768)
        self.assertEqual(login_client_point(LOGIN_CONNECT_CLICK_CLIENT, size),
                         LOGIN_CONNECT_CLICK_CLIENT)
        self.assertEqual(login_client_box(LOGIN_BOARD_CLIENT_BOX, size), LOGIN_BOARD_CLIENT_BOX)
        self.assertEqual(window_client_point(WORLD_ROW_CLIENT, size), WORLD_ROW_CLIENT)
        self.assertEqual(window_client_point(CHANNEL_FIRST_CLIENT, size), CHANNEL_FIRST_CLIENT)
        self.assertEqual(window_client_box(SELECT_WINDOW_CLIENT_BOX, size),
                         SELECT_WINDOW_CLIENT_BOX)

    def test_the_login_board_is_centred_and_keeps_its_size(self):
        """His own 1080 picks: the 连接 point (721, 401) and the board (536, 190, 370x392)."""

        from reconnect_worker import (
            LOGIN_BOARD_CLICK_CLIENT, LOGIN_BOARD_CLIENT_BOX, LOGIN_CONNECT_CLICK_CLIENT,
            login_client_box, login_client_point,
        )

        size = (1080, 768)
        self.assertEqual(login_client_point(LOGIN_CONNECT_CLICK_CLIENT, size), (721, 401))
        self.assertEqual(login_client_point(LOGIN_BOARD_CLICK_CLIENT, size), (647, 326))
        self.assertEqual(login_client_box(LOGIN_BOARD_CLIENT_BOX, size), (536, 190, 370, 392))

    def test_the_select_windows_scale_with_the_width(self):
        """His own 1080 picks: 蓝蜗牛 (392, 209), channel 1 (441, 387), world step 82 px right."""

        from reconnect_worker import (
            CHANNEL_FIRST_CLIENT, CHANNEL_ROW_STEP_CLIENT, CHANNEL_STEP_CLIENT, WORLD_ROW_CLIENT,
            WORLD_ROW_STEP_CLIENT, window_client_delta, window_client_length, window_client_point,
        )

        size = (1080, 768)
        world_x, world_y = window_client_point(WORLD_ROW_CLIENT, size)
        self.assertLessEqual(abs(world_x - 392), 3, "his 1080 蓝蜗牛 click")
        self.assertLessEqual(abs(world_y - 209), 3, "his 1080 蓝蜗牛 click")
        channel_x, channel_y = window_client_point(CHANNEL_FIRST_CLIENT, size)
        self.assertLessEqual(abs(channel_x - 441), 3, "his 1080 channel 1 click")
        self.assertLessEqual(abs(channel_y - 387), 3, "his 1080 channel 1 click")
        # 蘑菇仔 is 82 px to the RIGHT at 1080 (his answer), the column pitch is 74.3 (his 75) and the
        # row pitch 24.1 (the row bands in his screenshot measured 24.5)
        self.assertEqual(window_client_delta(WORLD_ROW_STEP_CLIENT, size), (82, 0))
        self.assertAlmostEqual(window_client_length(CHANNEL_STEP_CLIENT, size), 74.3, places=1)
        self.assertAlmostEqual(window_client_length(CHANNEL_ROW_STEP_CLIENT, size), 24.1, places=1)

    def test_a_page_patch_box_is_mapped_for_the_client(self):
        """A patch planted where the model says it belongs is recognised on a 2000x768 frame."""

        from reconnect_worker import (
            ANCHOR_SPACE_LOGIN, PAGE_LOGIN_CONNECT_BOX, detect_page, login_client_box,
        )

        size = (2000, 768)
        patch = np.zeros((40, 160, 3), dtype=np.uint8)
        patch[:, :80] = (200, 40, 40)
        patch[10:30, 90:150] = (40, 200, 240)
        frame = np.full((768, 2000, 3), 30, dtype=np.uint8)
        left, top, width, height = login_client_box(PAGE_LOGIN_CONNECT_BOX, size)
        frame[top:top + height, left:left + width] = patch
        page, score = detect_page(frame, {"login": [(patch, PAGE_LOGIN_CONNECT_BOX,
                                                     ANCHOR_SPACE_LOGIN)]})
        self.assertEqual(page, "login", f"{page} ({score:.3f})")
        self.assertGreater(score, 0.99, score)
        # the preset box would be 143 px to the right and find nothing
        page_off, score_off = detect_page(frame, {"login": [(patch, PAGE_LOGIN_CONNECT_BOX,
                                                             "window")]})
        self.assertNotEqual(page_off, "login", f"{page_off} ({score_off:.3f})")

    def test_the_exit_button_guard_follows_the_client(self):
        """结束游戏 must be recognised at both resolutions - a click on it ends the game."""

        from reconnect_worker import (
            LOGIN_CONNECT_CLICK_CLIENT, LOGIN_EXIT_BUTTON_CLIENT_BOXES, login_client_box,
            login_client_point,
        )

        worker = object.__new__(ReconnectWorker)
        for size, centre in (((1366, 768), (1192, 609)),        # his 1366 结束游戏 measurement
                             ((1080, 768), (944, 481))):         # his 1080 pick (897, 467), mapped
            worker._client_size_fn = None
            worker._last_frame_size = size
            self.assertTrue(worker._in_exit_button(*centre), f"{size} {centre}")
            connect = login_client_point(LOGIN_CONNECT_CLICK_CLIENT, size)
            self.assertFalse(worker._in_exit_button(*connect),
                             f"the 连接 point is not 结束游戏 ({size})")
            for box in LOGIN_EXIT_BUTTON_CLIENT_BOXES:
                left, top, width, height = login_client_box(box, size)
                self.assertTrue(worker._in_exit_button(left + width // 2, top + height // 2))

    def test_measuring_the_list_does_not_capture_a_frame(self):
        """A measurement loop must not consume a capture: it would skip the frame it waits for."""

        frames = [np.zeros((768, 1080, 3), dtype=np.uint8) for _ in range(4)]
        served = []

        def capture():
            served.append(1)
            return frames[min(len(served) - 1, len(frames) - 1)], (0, 0, 1080, 768)

        worker = ReconnectWorker(
            _FakeSender(), threading.Event(), queue.Queue(maxsize=8),
            capture_fn=capture, sleep=lambda seconds: None,
        )
        worker._capture()
        self.assertEqual(len(served), 1)
        size = worker._click_size()
        self.assertEqual(size, (1080, 768))
        self.assertEqual(len(served), 1, "the size must come from the last frame, not a new capture")


class ReconnectOn1080Tests(unittest.TestCase):
    """The whole login step on a 1080x768 client: every click must use the mapped point."""

    def setUp(self):
        self._case = ReconnectSequenceTests("test_the_settings_are_validated")
        self._case.setUp()
        base = self._case.login_frame
        # the same blue frame with a big cream login board, at 1080x768
        frame = np.zeros((768, 1080, 3), dtype=np.uint8)
        frame[:, :] = base[0, 0]
        frame[80:700, 206:886] = base[400, 600]
        self.frame = frame
        self.rect = (100, 50, 1180, 818)          # client origin (100, 50), 1080x768 client

    def tearDown(self):
        self._case.tearDown()

    def test_the_login_clicks_use_the_1080_points(self):
        from reconnect_worker import (
            ACTIVATE_CLICK_CLIENT, LOGIN_BOARD_CLICK_CLIENT, LOGIN_CONNECT_CLICK_CLIENT,
            login_client_point,
        )

        clicks = []
        screen = _Screen(self.frame, self.rect)
        sender = _FakeSender(on_press=lambda key: screen.react_to_enter())
        pages = _page_fn()
        sender.pages = pages
        worker = ReconnectWorker(
            sender, threading.Event(), queue.Queue(maxsize=64),
            capture_fn=screen,
            template_path=self._case.template_path,
            sleep=lambda seconds: None,
            click_fn=_page_aware_click(pages, lambda x, y: clicks.append((x, y)) or True,
                                       self.rect),
            page_fn=pages,
            wheel_fn=lambda x, y, n: True,
        )
        worker.set_enabled(True)
        worker.press_enter_for_offline_prompt = False
        worker.set_world("蓝蜗牛")
        worker.set_channel(1)
        worker._handle_disconnect()

        size = (1080, 768)
        origin_x, origin_y = self.rect[0], self.rect[1]
        board = login_client_point(LOGIN_BOARD_CLICK_CLIENT, size)
        connect = login_client_point(LOGIN_CONNECT_CLICK_CLIENT, size)
        self.assertEqual(clicks[0], (origin_x + ACTIVATE_CLICK_CLIENT[0],
                                     origin_y + ACTIVATE_CLICK_CLIENT[1]))
        self.assertIn((origin_x + board[0], origin_y + board[1]), clicks, clicks)
        self.assertIn((origin_x + connect[0], origin_y + connect[1]), clicks, clicks)
        # THE BUG: the preset point must never be used on a 1080 client
        self.assertNotIn((origin_x + LOGIN_CONNECT_CLICK_CLIENT[0],
                          origin_y + LOGIN_CONNECT_CLICK_CLIENT[1]), clicks,
                         "the 1366 连接 point must not be clicked on a 1080 client")
        self.assertIn(PAGE_WORLD, pages.history,
                      "the login step must have left the login page")


class ConnectButtonTests(unittest.TestCase):
    """The 连接 button: found by OpenCV on the login board, clicked instead of pressing Enter.

    The board is flat cream, so the button is the largest saturated button-like shape on it.  A
    shipped crop (``recording-assets/login_connect_target.jpg``) is matched as a template first
    when one exists.
    """

    @staticmethod
    def _login_page(*, button: bool = True):
        import numpy as np

        frame = np.zeros((768, 1366, 3), dtype=np.uint8)
        frame[:, :] = (40, 45, 60)                     # the game around the board
        frame[85:683, 299:1066] = (241, 235, 213)      # the cream board (BGR)
        if button:
            frame[520:556, 620:760] = (200, 90, 40)    # the 连接 button, saturated
        return frame

    def test_the_button_is_found_in_the_board(self):
        from reconnect_worker import find_connect_button

        box = (299, 85, 767, 598)
        found = find_connect_button(self._login_page(), box)
        self.assertIsNotNone(found, "the button is a large coloured rectangle on the board")
        left, top, width, height = found
        # the synthetic button's centre, whatever box shape the detector returns
        self.assertLessEqual(abs((left + width / 2) - 690), 4)
        self.assertLessEqual(abs((top + height / 2) - 538), 4)

    def test_nothing_is_found_on_a_board_without_a_button(self):
        from reconnect_worker import find_connect_button

        self.assertIsNone(find_connect_button(self._login_page(button=False),
                                              (299, 85, 767, 598)))

    def test_an_unusable_frame_is_not_an_error(self):
        from reconnect_worker import find_connect_button

        for frame in (None, np.zeros((0, 0, 3), dtype=np.uint8)):
            self.assertIsNone(find_connect_button(frame, (0, 0, 10, 10)))

    def test_a_template_is_used_when_one_is_shipped(self):
        from reconnect_worker import find_connect_button

        frame = self._login_page()
        template = frame[520:556, 620:760].copy()
        found = find_connect_button(frame, (299, 85, 767, 598), template=template)
        self.assertIsNotNone(found)
        left, top, width, height = found
        self.assertEqual((left, top), (620, 520))
        self.assertEqual((width, height), (140, 36))

    def test_the_button_is_clicked_instead_of_pressing_enter(self):
        frame = self._login_page()
        screen = _Screen(frame)
        clicks = []
        sender = _FakeSender()
        keys_seen = []

        def on_press(key):
            keys_seen.append(key)
            # every key changes the picture, so the verified steps see progress
            screen.frame = np.full_like(screen.frame, 30 + (len(keys_seen) % 7) * 8)

        sender.on_press = on_press

        # the screen reacts to the 连接 click: after it the board is gone
        def on_click(x, y):
            if len(clicks) == 3:            # activate, board, then the 连接 click
                screen.frame = np.full_like(screen.frame, 30)

        page_machine = _page_fn()
        sender.pages = page_machine
        base_click = _channel_clicking_click_fn(screen, clicks, on_click=on_click)
        # the 掉线提示窗口 Enter is a separate step (OfflinePromptTests): this test is about the
        # 连接 click, and its fake clicks are counted from the activation click

        def click(x, y):
            page_machine.clicked(x, y)
            return base_click(x, y)

        worker = ReconnectWorker(
            sender,
            threading.Event(),
            queue.Queue(maxsize=64),
            capture_fn=screen,
            template_path=None,
            sleep=lambda seconds: None,
            click_fn=click,
            wheel_fn=lambda x, y, n: True,
            page_fn=page_machine,
        )
        worker.set_enabled(True)
        worker.press_enter_for_offline_prompt = False     # see the shared factory's note
        # the colour gate needs a reference to measure the board; give it one
        worker._template_path = Path("does-not-exist.jpg")
        worker._login_reference = lambda: _cream_reference()
        worker.set_world("蓝蜗牛")
        worker.set_channel(1)
        worker._handle_disconnect()

        # the activation click, then the 连接 click
        self.assertGreaterEqual(len(clicks), 2, clicks)
        reports = [state for state, _detail in
                   [worker.result_queue.get_nowait() for _ in range(worker.result_queue.qsize())]]
        self.assertIn("connect", reports)
        self.assertIn("login-done", reports, reports)
        # the Enter fallback never ran: the 连接 click did the job
        self.assertNotIn("login-page", reports, reports)
        # the three Enters that remain are the world confirm, the channel confirm and the login
        self.assertEqual(sender.keys, ["enter", "enter", "enter"], sender.keys)

    def test_a_missing_button_falls_back_to_enter(self):
        frame = self._login_page(button=False)
        screen = _Screen(frame)
        clicks = []

        def click(x, y):
            clicks.append((x, y))
            return True

        page_machine = _page_fn()
        sender = _FakeSender(pages=page_machine,
                             on_press=lambda key: screen.react_to_enter())
        worker = ReconnectWorker(
            sender,
            threading.Event(),
            queue.Queue(maxsize=64),
            capture_fn=screen,
            sleep=lambda seconds: None,
            click_fn=click,
            wheel_fn=lambda x, y, n: True,
            page_fn=page_machine,
        )
        worker.set_enabled(True)
        worker._login_reference = lambda: _cream_reference()
        worker.set_channel(1)
        worker._handle_disconnect()
        self.assertIn("enter", sender.keys, "Enter is the fallback when 连接 is not found")


def _cream_reference():
    """A login colour reference built from the synthetic board's cream."""

    from reconnect_worker import login_colour_reference

    patch = np.zeros((200, 200, 3), dtype=np.uint8)
    patch[:, :] = (241, 235, 213)
    patch[:80, :80] = (233, 227, 205)
    return login_colour_reference(patch)


class SafeClickTests(unittest.TestCase):
    """Only ever click the activation spot and 连接 itself - never a guessed board point."""

    def setUp(self):
        self._case = ReconnectSequenceTests("test_the_settings_are_validated")
        self._case.setUp()

    def tearDown(self):
        self._case.tearDown()

    @staticmethod
    def _board_without_a_button():
        import numpy as np

        frame = np.zeros((768, 1366, 3), dtype=np.uint8)
        frame[:, :] = (40, 45, 60)
        frame[85:683, 299:1066] = (241, 235, 213)     # a flat board: no button to find
        return frame

    def _worker(self, screen, clicks, sender=None, pages=None):
        pages = _page_fn() if pages is None else pages
        if sender is None:
            sender = _FakeSender(pages=pages)
            # the login page reacts to Enter, otherwise the verified Enter would retry and the
            # run would stop before the world/channel steps
            original_press = sender.press

            def press(key, duration=0.025, _original=original_press):
                result = _original(key, duration=duration)
                if key == "enter":
                    screen.react_to_enter()
                return result

            sender.press = press
        worker = ReconnectWorker(
            sender,
            threading.Event(),
            queue.Queue(maxsize=64),
            capture_fn=screen,
            sleep=lambda seconds: None,
            click_fn=_page_aware_click(pages, lambda x, y: clicks.append((x, y)) or True),
            wheel_fn=lambda x, y, n: True,
            page_fn=pages,
        )
        worker.set_enabled(True)
        worker.press_enter_for_offline_prompt = False     # see the shared factory's note
        worker._login_reference = lambda: _cream_reference()
        return worker

    def test_an_unlocatable_button_means_no_board_click(self):
        import reconnect_worker as module
        from reconnect_worker import ACTIVATE_CLICK_CLIENT

        screen = _Screen(self._board_without_a_button())
        clicks = []
        pages = _page_fn()
        sender = _FakeSender(on_press=lambda key: screen.react_to_enter(), pages=pages)
        worker = self._worker(screen, clicks, sender=sender, pages=pages)
        original = module.LOGIN_CONNECT_CLICK_CLIENT
        module.LOGIN_CONNECT_CLICK_CLIENT = None       # no measured point, no OpenCV candidate
        try:
            worker.set_channel(1)
            worker._handle_disconnect()
        finally:
            module.LOGIN_CONNECT_CLICK_CLIENT = original

        # the first click is the activation click at client (50, 50); no click may land on the
        # 结束游戏 point, and after the login fallback the world row and channel 1 are clicked
        self.assertEqual(clicks[0], (100 + ACTIVATE_CLICK_CLIENT[0],
                                     50 + ACTIVATE_CLICK_CLIENT[1]))
        self.assertNotIn((100 + 1192, 50 + 609), clicks, "the 结束游戏 point must never be clicked")
        self.assertIn((100 + 498, 50 + 163), clicks, clicks)
        self.assertIn("enter", sender.keys, "the login falls back to Enter")

    def test_a_measured_point_is_used_verbatim(self):
        import reconnect_worker as module

        screen = _Screen(self._board_without_a_button())
        clicks = []
        worker = self._worker(screen, clicks)
        original = module.LOGIN_CONNECT_CLICK_CLIENT
        module.LOGIN_CONNECT_CLICK_CLIENT = (900, 500)      # inside the board box
        try:
            worker.set_channel(1)
            worker._handle_disconnect()
        finally:
            module.LOGIN_CONNECT_CLICK_CLIENT = original
        # activation click, the board click, then the configured 连接 point
        from reconnect_worker import LOGIN_BOARD_CLICK_CLIENT

        self.assertEqual(clicks[0], (150, 100), clicks)
        self.assertEqual(clicks[1], (100 + LOGIN_BOARD_CLICK_CLIENT[0],
                                     50 + LOGIN_BOARD_CLICK_CLIENT[1]), clicks)
        self.assertEqual(clicks[2], (1000, 550), clicks)

    def test_a_point_outside_the_board_is_refused(self):
        """结束游戏 sits OUTSIDE the board, so an outside point must never be clicked."""

        import reconnect_worker as module

        screen = _Screen(self._board_without_a_button())
        clicks = []
        worker = self._worker(screen, clicks)
        original = module.LOGIN_CONNECT_CLICK_CLIENT
        module.LOGIN_CONNECT_CLICK_CLIENT = (1192, 609)     # 结束游戏!
        try:
            worker.set_channel(1)
            worker._handle_disconnect()
        finally:
            module.LOGIN_CONNECT_CLICK_CLIENT = original
        # no click may land on the 结束游戏 point; the business clicks (world row, channel 1)
        # still happen after the login fallback
        self.assertEqual(clicks[0], (150, 100))
        self.assertNotIn((100 + 1192, 50 + 609), clicks, "the 结束游戏 point must never be clicked")
        self.assertIn((100 + 498, 50 + 163), clicks, "the world row is still clicked")

    def test_the_shipped_point_is_inside_the_shipped_board(self):
        from reconnect_worker import (
            LOGIN_BOARD_CLIENT_BOX, LOGIN_BOARD_CLICK_CLIENT, LOGIN_CONNECT_CLICK_CLIENT,
            LOGIN_EXIT_BUTTON_CLIENT_BOXES, point_in_box,
        )

        # re-measured by the operator on his 1080 client (2026-09-17), mapped back to this space
        self.assertEqual(LOGIN_CONNECT_CLICK_CLIENT, (864, 401))
        self.assertEqual(LOGIN_BOARD_CLICK_CLIENT, (790, 326))
        for point in (LOGIN_CONNECT_CLICK_CLIENT, LOGIN_BOARD_CLICK_CLIENT):
            self.assertTrue(point_in_box(*point, LOGIN_BOARD_CLIENT_BOX), point)
        # 结束游戏 is outside the board, and every exit-button box is a "never click" guard
        self.assertFalse(point_in_box(1192, 609, LOGIN_BOARD_CLIENT_BOX),
                         "结束游戏 is outside the board")
        self.assertTrue(any(point_in_box(1192 + 15, 609 - 15, box)
                            for box in LOGIN_EXIT_BUTTON_CLIENT_BOXES),
                        LOGIN_EXIT_BUTTON_CLIENT_BOXES)


class _ScrollableScreen:
    """A fake channel window whose list really scrolls one row per wheel notch.

    The list is drawn with per-channel marks (the way the game draws channel numbers), so the
    closed-loop scroll can MEASURE how far each notch moved it: a flat fake frame would make that
    measurement meaningless.  ``Enter`` toggles a strip outside the list, which is the page switch
    the worker verifies; a click inside the list highlights that cell.
    """

    def __init__(self, rect=(100, 50, 1466, 818)):
        self.rect = rect
        self.scroll = 0
        self.highlight = None
        self.strip = 0
        self.phase = 0                            # 0 = the login page, 1 = the channel window
        self.frames_returned = 0
        self.frame = self._draw()

    def _draw(self):
        frame = np.zeros((768, 1366, 3), dtype=np.uint8)
        if self.phase == 0:
            # the colour gate needs the login page's cream over >= 6 % of the window
            frame[:, :] = (216, 232, 248)
        else:
            frame[:, :] = (40, 40, 40)
        frame[:70, :] = (30, 190, 90) if self.strip else (30, 60, 120)
        left, top, width, height = CHANNEL_LIST_CLIENT_BOX
        frame[top:top + height, left:left + width] = (64, 64, 64)
        for visible in range(CHANNEL_VISIBLE_ROWS + 8):
            row = visible + self.scroll
            y = int(round(top + 8 + visible * CHANNEL_ROW_STEP_CLIENT))
            for column in range(CHANNELS_PER_ROW):
                number = row * CHANNELS_PER_ROW + column + 1
                if number > CHANNEL_MAX:
                    continue
                x = left + 24 + column * CHANNEL_STEP_CLIENT
                # Channel numbers the way the game draws them: a bright block whose size follows
                # the number, so one row of scroll changes a lot of pixels and the marks are not
                # all identical (which the shift measurement needs).
                frame[y:y + 10 + (number // 7) % 5, x:x + 12 + number % 7] = 200
                frame[y + 12:y + 16, x + 20:x + 20 + (number % 5) * 6] = 170
        if self.highlight is not None:
            frame = _channel_click_frame(frame, *self.highlight)
        return frame

    def wheel(self, notches):
        self.scroll += int(notches)
        self.frame = self._draw()

    def click(self, client_x, client_y):
        if point_in_box(int(client_x), int(client_y), CHANNEL_LIST_CLIENT_BOX):
            self.highlight = (client_x, client_y)
        self.frame = self._draw()

    def react_to_key(self, key):
        if key == "enter":
            if self.phase == 0:
                self.phase = 1                    # the login Enter left the login page
            self.strip = 1 - self.strip           # the page changed
            self.frame = self._draw()
        return None

    def __call__(self):
        self.frames_returned += 1
        return self.frame, self.rect


class ChannelScrollTests(unittest.TestCase):
    """The operator's own case: channel 51 = row 13, five rows visible -> scroll 8 rows.

    "the target channel is 51, why is that the algorithm say 滚动3行? it should be 8 rows and then
    the 51 will show and then double clicked 51, and there's no need to click [channels] before
    scroll, just click 1, then scroll" - v0402 does exactly that: one click on channel 1, then one
    verified notch per row, then the double-click on the target cell.
    """

    def setUp(self):
        self._case = ReconnectSequenceTests("test_the_settings_are_validated")
        self._case.setUp()

    def tearDown(self):
        self._case.tearDown()

    def _run(self, channel, *, wheel_breaks=False):
        screen = _ScrollableScreen()
        wheels = []
        clicks = []

        def wheel(x, y, notches):
            wheels.append(notches)
            if wheel_breaks:
                return True                        # the event is swallowed: nothing moves
            screen.wheel(notches)
            return True

        def click(x, y):
            clicks.append((x, y))
            screen.click(x - screen.rect[0], y - screen.rect[1])
            return True

        page_machine = _page_fn()

        def on_press(key):
            screen.react_to_key(key)
            page_machine.key(key)

        sender = _FakeSender(on_press=on_press)

        def click_with_page(x, y):
            page_machine.clicked(x, y, screen.rect)
            return click(x, y)

        worker = ReconnectWorker(
            sender, threading.Event(), queue.Queue(maxsize=64),
            capture_fn=screen,
            template_path=self._case.template_path,
            sleep=lambda seconds: None,
            click_fn=click_with_page,
            wheel_fn=wheel,
            page_fn=page_machine,
        )
        worker.set_enabled(True)
        worker.set_channel(channel)
        worker._handle_disconnect()
        reports = []
        while not worker.result_queue.empty():
            reports.append(worker.result_queue.get_nowait())
        return wheel, screen, wheels, clicks, sender, reports

    def test_channel_51_scrolls_eight_rows_and_double_clicks_the_target_cell(self):
        wheel, screen, wheels, clicks, sender, reports = self._run(51)

        from reconnect_worker import (  # noqa: F401 - the numbers the test asserts on
            CHANNELS_PER_ROW as PER_ROW, CHANNEL_VISIBLE_ROWS as VISIBLE,
        )

        expected_rows = (51 - 1) // PER_ROW - (VISIBLE - 1)
        self.assertEqual(expected_rows, 8, "channel 51 = row 13, 5 rows visible -> 8 rows")
        self.assertEqual(sum(wheels), expected_rows, wheels)
        self.assertEqual(set(wheels), {1}, f"one notch at a time: {wheels}")

        # channel 1 first, then the target cell (row 13 = 5th visible row, column 3)
        self.assertIn((100 + CHANNEL_FIRST_CLIENT[0], 50 + CHANNEL_FIRST_CLIENT[1]), clicks)
        target = (100 + CHANNEL_FIRST_CLIENT[0] + 2 * CHANNEL_STEP_CLIENT,
                  50 + int(round(CHANNEL_FIRST_CLIENT[1] + 4 * CHANNEL_ROW_STEP_CLIENT)))
        # the operator's rule: ONE click selects the cell, then the DOUBLE CLICK enters the channel
        # ("press enter will go into channel1", so Enter is not a substitute).  The pair is repeated
        # once with a slower gap when the game shows no reaction.
        from reconnect_worker import DOUBLE_CLICK_GAPS

        self.assertEqual(len(DOUBLE_CLICK_GAPS), 2)
        self.assertEqual(clicks.count(target), 1 + 2 * len(DOUBLE_CLICK_GAPS),
                         f"one click + {len(DOUBLE_CLICK_GAPS)} double clicks: {clicks}")
        # and nothing walks through the channels
        self.assertEqual([key for key in sender.keys if key in ("down", "right")], [],
                         sender.keys)
        self.assertIn("done", [state for state, _detail in reports], reports)

    def test_a_swallowed_wheel_is_evidence_not_a_failure(self):
        """v0419: a wheel the game ignores no longer fails the run - the arithmetic decides.

        Measured in the field: the list had scrolled while the reconnect's captured frames were stale,
        so "unchanged" was reported twelve times and the run gave up on a step that had worked.
        """

        wheel, screen, wheels, clicks, sender, reports = self._run(51, wheel_breaks=True)

        states = [state for state, _detail in reports]
        self.assertIn("done", states, reports)
        self.assertEqual(sum(wheels), 8, wheels)
        self.assertTrue(any("按计算" in detail for _state, detail in reports), reports)

    def test_an_off_screen_target_is_reported_instead_of_logging_in_to_channel_one(self):
        """The field failure: Enter confirmed channel 1 because the target was never selected."""

        screen = _Screen(self._case.login_frame)
        sender = _FakeSender(on_press=lambda key: screen.react_to_enter() if key == "enter"
                             else None)
        worker = ReconnectWorker(
            sender, threading.Event(), queue.Queue(maxsize=64),
            capture_fn=screen,
            sleep=lambda seconds: None,
            click_fn=lambda x, y: True,
            wheel_fn=lambda x, y, n: False,       # scrolling fails -> the row cannot be reached
            page_fn=_page_fn(PAGE_CHANNEL),
        )
        worker.set_enabled(True)
        worker.set_channel(51)
        worker._handle_disconnect()
        reports = [state for state, _detail in
                   [worker.result_queue.get_nowait() for _ in range(worker.result_queue.qsize())]]
        self.assertIn("failed", reports, reports)

    def test_a_double_click_that_nothing_confirms_still_follows_the_recipe(self):
        """v0419: the clicks are CALCULATED - the picture is evidence, the Enter decides.

        The operator: "openCV text check may not be robust at this stage, you can maybe click the pos by
        calc".  The clicks and the notches are arithmetic; a stale capture therefore no longer aborts a
        step that is working (measured: "51 is on screen but failed to click that channel").
        """

        screen = _Screen(self._case.login_frame)          # clicks leave the picture alone
        pages = _page_fn(PAGE_CHANNEL)
        sender = _FakeSender(pages=pages)                 # the Enter must move the fake game's page
        worker = ReconnectWorker(
            sender, threading.Event(), queue.Queue(maxsize=64),
            capture_fn=screen,
            sleep=lambda seconds: None,
            click_fn=lambda x, y: True,
            wheel_fn=lambda x, y, n: True,
            page_fn=pages,
        )
        worker.set_enabled(True)
        # channel 6 = row 2 column 2: a DIFFERENT cell than channel 1, so "nothing changed" here
        # really does mean the double-click was ignored
        worker.set_channel(6)
        worker._select_channel(6)

        reports = [state for state, _detail in
                   [worker.result_queue.get_nowait() for _ in range(worker.result_queue.qsize())]]
        # the recipe ran: the double click on the cell plus the channel Enter and the login Enter
        self.assertEqual(len([key for key in sender.keys if key == "enter"]), 2, sender.keys)
        self.assertNotIn("failed", reports, reports)


class KeyboardOnlyTests(unittest.TestCase):
    """The panel has no calibration: the points ship as constants (the keyboard is the fallback).

    The operator's rule: the game window is fixed, so the panel needs no 「截取游戏窗口」and no
    「标定选择窗口」, and the worker carries no layout/calibration API any more.  These checks are
    what keeps the controls (and the machinery behind them) from creeping back.
    """

    def test_the_panel_has_no_capture_or_calibration_buttons(self):
        source = (Path(__file__).resolve().parent / "ui_worker.py").read_text(encoding="utf-8")
        for gone in ("截取游戏窗口", "标定选择窗口", "_reconnect_capture_button",
                     "_reconnect_calibrate_button", "_reconnect_capture_clicked",
                     "_reconnect_calibrate_clicked", "RECONNECT_CALIBRATION_DELAY_SECONDS"):
            self.assertNotIn(gone, source, gone)
        # the temporary drill button stays: it is how the sequence is tried by hand
        self.assertIn("测试自动重连", source)

    def test_the_worker_has_no_layout_or_calibration_api(self):
        import reconnect_worker as module

        for gone in ("ReconnectLayout", "RECONNECT_LAYOUT", "ScreenClicker",
                     "CALIBRATION_STEPS", "layout_path", "load_layout", "write_layout",
                     "layout_from_points"):
            self.assertFalse(hasattr(module, gone), gone)

    def test_the_world_row_is_clicked_at_the_measured_position(self):
        """The five worlds are in ONE horizontal line: click the row, never walk with 'down'."""

        from reconnect_worker import WORLD_ROW_CLIENT, WORLD_ROW_STEP_CLIENT

        self.assertEqual(len(WORLD_NAMES), 5)
        case = ReconnectSequenceTests("test_the_settings_are_validated")
        case.setUp()
        try:
            for index, world in enumerate(WORLD_NAMES):
                clicks = []
                screen = _Screen(case.login_frame)
                pages = _page_fn()
                sender = _FakeSender(on_press=lambda key: screen.react_to_enter(), pages=pages)
                worker = ReconnectWorker(
                    sender, threading.Event(), queue.Queue(maxsize=64),
                    capture_fn=screen,
                    sleep=lambda seconds: None,
                    click_fn=_page_aware_click(
                        pages, lambda x, y: clicks.append((x, y)) or True),
                    page_fn=pages,
                )
                worker.set_enabled(True)
                worker.set_world(world)
                worker.set_channel(1)
                worker._handle_disconnect()
                expected_x = 100 + WORLD_ROW_CLIENT[0] + index * WORLD_ROW_STEP_CLIENT[0]
                expected_y = 50 + WORLD_ROW_CLIENT[1] + index * WORLD_ROW_STEP_CLIENT[1]
                self.assertIn((expected_x, expected_y), clicks,
                              f"{world} (row {index + 1}) must be clicked at "
                              f"({expected_x}, {expected_y}); clicks were {clicks}")
        finally:
            case.tearDown()

    def test_the_channel_step_clicks_channel_one_then_the_target_cell(self):
        """Channel 1 is clicked, then the TARGET cell is double-clicked - no keyboard walk.

        The operator's rule (v0402): "there's no need to click [channels] before scroll, just click
        1, then scroll".  For a channel that is already on screen there is not even a scroll.
        """

        from reconnect_worker import (
            CHANNEL_FIRST_CLIENT, CHANNEL_STEP_CLIENT, CHANNELS_PER_ROW,
        )

        self.assertEqual(CHANNELS_PER_ROW, 4)

        case = ReconnectSequenceTests("test_the_settings_are_validated")
        case.setUp()
        try:
            clicks = []
            screen = _Screen(case.login_frame)
            pages = _page_fn()
            sender = _FakeSender(on_press=lambda key: screen.react_to_enter(), pages=pages)

            def click(x, y):
                clicks.append((x, y))
                client_x, client_y = int(x) - 100, int(y) - 50
                screen.react_to_channel_click(client_x, client_y)
                return True

            worker = ReconnectWorker(
                sender, threading.Event(), queue.Queue(maxsize=64),
                capture_fn=screen,
                sleep=lambda seconds: None,
                click_fn=_page_aware_click(pages, click),
                wheel_fn=lambda x, y, n: True,
                page_fn=pages,
            )
            worker.set_enabled(True)
            worker.set_channel(8)          # row 2, column 4 -> on screen, no scroll
            worker._handle_disconnect()

            # channel 1 is clicked ...
            self.assertIn((100 + CHANNEL_FIRST_CLIENT[0], 50 + CHANNEL_FIRST_CLIENT[1]),
                          clicks, clicks)
            # ... and the TARGET cell of channel 8 (row 2 column 4) is double-clicked
            target = (100 + CHANNEL_FIRST_CLIENT[0] + 3 * CHANNEL_STEP_CLIENT,
                      50 + CHANNEL_FIRST_CLIENT[1] + int(CHANNEL_ROW_STEP_CLIENT))
            self.assertIn(target, clicks, clicks)
            # ONE click selects the cell, then the DOUBLE CLICK enters the channel (the operator:
            # "press enter will go into channel1", so Enter is not a substitute).  The pair is
            # repeated once with a slower gap when the game shows no reaction.
            from reconnect_worker import DOUBLE_CLICK_GAPS

            self.assertEqual(clicks.count(target), 1 + 2 * len(DOUBLE_CLICK_GAPS),
                             "one click + the double-click pairs")
            # the channel is never reached with the keyboard
            self.assertNotIn("down", sender.keys, sender.keys)
        finally:
            case.tearDown()

    def test_the_double_click_is_quick_and_sent_twice(self):
        """The operator: "i think maybe the double click is to slow make it quicker, and double click
        twice" (2026-09-17, after the channel-40 failures)."""

        from reconnect_worker import (
            CHANNEL_FIRST_CLIENT, CHANNEL_ROW_STEP_CLIENT, CHANNELS_PER_ROW, DOUBLE_CLICK_GAPS,
            DOUBLE_CLICK_HOLD_SECONDS,
        )

        self.assertEqual(len(DOUBLE_CLICK_GAPS), 2, "'double click twice'")
        self.assertLessEqual(DOUBLE_CLICK_GAPS[0], 0.10, "'make it quicker'")
        self.assertLessEqual(DOUBLE_CLICK_HOLD_SECONDS, 0.05, "a quick press")
        self.assertTrue(all(gap <= 0.10 for gap in DOUBLE_CLICK_GAPS), DOUBLE_CLICK_GAPS)

        case = ReconnectSequenceTests("test_the_settings_are_validated")
        case.setUp()
        try:
            clicks = []
            sleeps = []
            screen = _Screen(case.login_frame)
            pages = _page_fn(PAGE_CHANNEL)          # the channel window never goes away
            sender = _FakeSender(on_press=lambda key: screen.react_to_enter(), pages=pages)

            def click(x, y):
                clicks.append((x, y))
                screen.react_to_channel_click(int(x) - 100, int(y) - 50)
                return True

            worker = ReconnectWorker(
                sender, threading.Event(), queue.Queue(maxsize=64),
                capture_fn=screen,
                sleep=lambda seconds: sleeps.append(seconds),
                click_fn=_page_aware_click(pages, click),
                wheel_fn=lambda x, y, n: True,
                page_fn=pages,
            )
            worker.set_enabled(True)
            worker.set_channel(int(CHANNELS_PER_ROW) + 1)      # row 2, column 1
            worker._select_channel(int(CHANNELS_PER_ROW) + 1)

            target = (100 + CHANNEL_FIRST_CLIENT[0],
                      50 + int(round(CHANNEL_FIRST_CLIENT[1] + CHANNEL_ROW_STEP_CLIENT)))
            self.assertEqual(clicks.count(target), 1 + 2 * len(DOUBLE_CLICK_GAPS), clicks)
            for gap in DOUBLE_CLICK_GAPS:
                self.assertIn(gap, sleeps, f"the {gap:.2f} s gap must be used: {sleeps}")
        finally:
            case.tearDown()


class ChannelListMeasurementTests(unittest.TestCase):
    """The v0400 field report: a scroll that DID happen was measured as "not scrolled".

    Root cause: ``screen_change`` compares a 64x36 grey thumbnail of the whole 1366x768 frame.  One
    channel row is 30 px, i.e. 1.4 thumbnail pixels, and the only thing that moves is the *text*
    inside the row - at that scale it is averaged away, so a real one-row scroll measured far below
    SCREEN_CHANGE_MIN.  Every channel-list decision now measures the list region itself
    (``CHANNEL_LIST_CLIENT_BOX``) and waits several frames for the game to repaint.
    """

    def _frame(self):
        frame = np.zeros((768, 1366, 3), dtype=np.uint8)
        frame[:, :] = (40, 40, 40)
        return frame

    def _list_frame(self, offset=0.0):
        """A channel list of seven rows of glyph-like marks, scrolled up by ``offset`` px.

        The marks are small and row-dependent, the way the game's channel numbers are: a real
        scroll moves a little text and leaves the background alone.
        """

        left, top, width, height = CHANNEL_LIST_CLIENT_BOX
        frame = self._frame()
        frame[top:top + height, left:left + width] = (64, 64, 64)
        for row in range(7):
            y = int(round(top + 5 + row * CHANNEL_ROW_STEP_CLIENT - offset))
            for column in range(CHANNELS_PER_ROW):
                # the glyphs differ from row to row, so a scroll is not a perfect overlap
                mark_width = 8 + ((row * 7 + column * 3) % 17)
                x = left + 24 + column * CHANNEL_STEP_CLIENT
                frame[y:y + 6, x:x + mark_width] = 205
                frame[y + 6:y + 9, x + 4:x + 4 + mark_width // 2] = 150
        return frame

    def test_a_one_row_scroll_is_visible_only_to_the_list_measurement(self):
        """The regression itself: the whole frame misses it, the list region sees it."""

        from reconnect_worker import (
            CHANNEL_LIST_CHANGE_MIN,
            CHANNEL_LIST_CLIENT_BOX,
            CHANNEL_ROW_STEP_CLIENT,
            SCREEN_CHANGE_MIN,
            region_change,
            screen_change,
        )

        before = self._list_frame(0.0)
        after = self._list_frame(CHANNEL_ROW_STEP_CLIENT)

        # the old measurement (64x36 thumbnail of the whole window) reported "not scrolled"
        self.assertLess(screen_change(before, after), SCREEN_CHANGE_MIN)
        # the list measurement sees the same scroll clearly
        self.assertGreaterEqual(region_change(before, after, CHANNEL_LIST_CLIENT_BOX),
                                CHANNEL_LIST_CHANGE_MIN)

    def test_a_moved_selection_highlight_is_seen_by_the_list_measurement(self):
        """A single ``down`` press moves one 94x30 highlight - also invisible to the old test."""

        from reconnect_worker import (
            CHANNEL_LIST_CHANGE_MIN,
            CHANNEL_LIST_CLIENT_BOX,
            CHANNEL_STEP_CLIENT,
            SCREEN_CHANGE_MIN,
            region_change,
            screen_change,
        )

        before = self._list_frame(0.0)
        after = before.copy()
        left, top, _width, _height = CHANNEL_LIST_CLIENT_BOX
        # the second row's cell is highlighted instead of the first one
        after[top + 34:top + 64, left + 24:left + 24 + CHANNEL_STEP_CLIENT] = (120, 170, 240)

        self.assertLess(screen_change(before, after), SCREEN_CHANGE_MIN)
        self.assertGreaterEqual(region_change(before, after, CHANNEL_LIST_CLIENT_BOX),
                                CHANNEL_LIST_CHANGE_MIN)

    def _worker(self, frames):
        frames = list(frames)
        self.sleeps = []

        def capture():
            frame = frames.pop(0) if len(frames) > 1 else frames[0]
            return frame, (100, 50, 1466, 818)

        worker = ReconnectWorker(
            _FakeSender(), threading.Event(), queue.Queue(maxsize=32),
            capture_fn=capture,
            sleep=lambda seconds: self.sleeps.append(seconds),
        )
        return worker

    def test_the_wait_lets_the_game_repaint_before_calling_it_unscrolled(self):
        """The game finishes a scroll a few frames later - the wait must not measure at once."""

        from reconnect_worker import CHANNEL_ROW_STEP_CLIENT

        before = self._list_frame(0.0)
        scrolled = self._list_frame(CHANNEL_ROW_STEP_CLIENT)
        # the first two captures still show the old row, the third shows the scrolled list
        worker = self._worker([before, before, scrolled])

        moved, _frame, change = worker._wait_for_list_change(before, "test scroll")

        self.assertTrue(moved, f"change {change}")
        self.assertGreaterEqual(len(self.sleeps), 3, self.sleeps)

    def test_a_change_outside_the_list_is_not_a_scroll(self):
        """A blinking element elsewhere in the window must not count as a scroll."""

        before = self._list_frame(0.0)
        other = before.copy()
        other[:60, :] = (250, 250, 250)          # a change well above the list region
        worker = self._worker([other])

        moved, _frame, change = worker._wait_for_list_change(before, "test scroll")

        self.assertFalse(moved)
        self.assertEqual(change, 0.0)
        # it only gives up after the full settle window
        from reconnect_worker import CHANNEL_LIST_SETTLE_ATTEMPTS

        self.assertEqual(len(self.sleeps), CHANNEL_LIST_SETTLE_ATTEMPTS, self.sleeps)

    def test_the_list_region_lies_inside_the_window_clamp(self):
        """The measured region must be click-safe: it is inside the select-window clamp."""

        from reconnect_worker import CHANNEL_LIST_CLIENT_BOX, SELECT_WINDOW_CLIENT_BOX

        left, top, width, height = CHANNEL_LIST_CLIENT_BOX
        self.assertTrue(point_in_box(left, top, SELECT_WINDOW_CLIENT_BOX), CHANNEL_LIST_CLIENT_BOX)
        self.assertTrue(point_in_box(left + width, top + height, SELECT_WINDOW_CLIENT_BOX),
                        CHANNEL_LIST_CLIENT_BOX)
        # and it covers the whole measured grid
        from reconnect_worker import (
            CHANNEL_FIRST_CLIENT,
            CHANNELS_PER_ROW,
            CHANNEL_ROW_STEP_CLIENT,
            CHANNEL_STEP_CLIENT,
            CHANNEL_VISIBLE_ROWS,
        )

        last_x = CHANNEL_FIRST_CLIENT[0] + (CHANNELS_PER_ROW - 1) * CHANNEL_STEP_CLIENT
        last_y = CHANNEL_FIRST_CLIENT[1] + (CHANNEL_VISIBLE_ROWS - 1) * CHANNEL_ROW_STEP_CLIENT
        self.assertLessEqual(left, CHANNEL_FIRST_CLIENT[0])
        self.assertLessEqual(top, CHANNEL_FIRST_CLIENT[1])
        self.assertGreaterEqual(left + width, last_x)
        self.assertGreaterEqual(top + height, last_y)



class FrameSizeGuardTests(unittest.TestCase):
    """Every click constant assumes the 1366x768 client space (150 % scaling, DPI-unaware)."""

    def _worker(self, frame):
        def capture():
            return frame, (100, 50, 1466, 818)

        return ReconnectWorker(
            _FakeSender(), threading.Event(), queue.Queue(maxsize=32),
            capture_fn=capture,
            sleep=lambda seconds: None,
        )

    def test_the_shipped_size_is_logged_as_the_click_space(self):
        from reconnect_worker import REFERENCE_CLIENT

        frame = np.zeros((REFERENCE_CLIENT[1], REFERENCE_CLIENT[0], 3), dtype=np.uint8)
        worker = self._worker(frame)
        with self.assertLogs("reconnect_worker", level="INFO") as logs:
            worker._check_frame_size()
        self.assertTrue(any("the space every click is measured in" in line for line in logs.output),
                        logs.output)

    def test_another_frame_size_is_warned_about_instead_of_clicking_on_it(self):
        frame = np.zeros((600, 1000, 3), dtype=np.uint8)
        worker = self._worker(frame)
        with self.assertLogs("reconnect_worker", level="WARNING") as logs:
            worker._check_frame_size()
        self.assertTrue(any("1000x600" in line and "1366x768" in line for line in logs.output),
                        logs.output)

    def test_no_frame_is_not_an_error(self):
        worker = self._worker(None)
        with self.assertLogs("reconnect_worker", level="WARNING") as logs:
            worker._check_frame_size()
        self.assertTrue(any("no frame to measure" in line for line in logs.output), logs.output)



class PointOwnershipTests(unittest.TestCase):
    """A click point that belongs to ANOTHER window must be refused, not clicked.

    The operator's field report (v0402): "the window keeps losing focus to assistant, so that the
    click is not successfully emitted to the game".  A click on a point another window owns hands
    the foreground to that window - so the game never sees it and the assistant takes focus back.
    The worker now asks which window really owns the point, brings the game forward once, and
    refuses (naming the window) when the point is still somebody else's.
    """

    def setUp(self):
        self._case = ReconnectSequenceTests("test_the_settings_are_validated")
        self._case.setUp()

    def tearDown(self):
        self._case.tearDown()

    def _worker(self, owners, *, clicks=None):
        """``owners`` is a list of answers, one per ownership check (None = the game owns it)."""
        answers = list(owners)
        screen = _Screen(self._case.login_frame)
        clicks = [] if clicks is None else clicks

        def point_owner(x, y):
            return answers.pop(0) if answers else None

        worker = ReconnectWorker(
            _FakeSender(), threading.Event(), queue.Queue(maxsize=64),
            capture_fn=screen,
            template_path=self._case.template_path,
            sleep=lambda seconds: None,
            click_fn=lambda x, y: clicks.append((x, y)) or True,
            wheel_fn=lambda x, y, n: True,
            point_owner_fn=point_owner,
        )
        worker.set_enabled(True)
        return worker, screen, clicks

    def _reports(self, worker):
        out = []
        while not worker.result_queue.empty():
            out.append(worker.result_queue.get_nowait())
        return out

    def test_a_point_owned_by_another_window_is_not_clicked(self):
        """Both checks say "the assistant owns it" -> no click, and the panel names that window."""

        worker, _screen, clicks = self._worker([(4242, "todo_helper (0403)"),
                                                (4242, "todo_helper (0403)")])
        with self.assertLogs("reconnect_worker", level="ERROR") as logs:
            self.assertFalse(worker._activate_window())
        self.assertEqual(clicks, [], "no click may be sent to another window")
        self.assertTrue(any("todo_helper (0403)" in line for line in logs.output), logs.output)
        failed = [detail for state, detail in self._reports(worker) if state == "failed"]
        self.assertTrue(any("todo_helper (0403)" in detail for detail in failed), failed)

    def test_the_game_is_brought_forward_once_before_the_point_is_refused(self):
        """First answer "another window", second "the game" -> the click IS sent."""

        worker, _screen, clicks = self._worker([(4242, "todo_helper (0403)"), None])
        self.assertTrue(worker._activate_window())
        self.assertEqual(len(clicks), 1, clicks)

    def test_the_guard_also_covers_the_channel_and_world_clicks(self):
        """The world row falls back to the keyboard (whose sender checks the foreground), the
        channel step fails - but neither ever clicks the other window."""

        def build():
            sender = _FakeSender()
            clicks = []
            screen = _Screen(self._case.login_frame)

            worker = ReconnectWorker(
                sender, threading.Event(), queue.Queue(maxsize=64),
                capture_fn=screen, template_path=self._case.template_path,
                sleep=lambda seconds: None,
                click_fn=lambda x, y: clicks.append((x, y)) or True,
                wheel_fn=lambda x, y, n: True,
                # EVERY point belongs to the assistant window
                point_owner_fn=lambda x, y: (4242, "todo_helper (0403)"),
                page_fn=_page_fn(),
            )
            worker.set_enabled(True)
            return worker, sender, clicks

        worker, sender, clicks = build()
        with self.assertLogs("reconnect_worker", level="ERROR"):
            self.assertFalse(worker._select_world("蘑菇仔"),
                             "a world that cannot be selected must not be confirmed with Enter")
        self.assertEqual(clicks, [], "the world click must not be sent to another window")
        self.assertNotIn("enter", sender.keys,
                         "no Enter may confirm the default world when the click never landed")

        worker, sender, clicks = build()
        with self.assertLogs("reconnect_worker", level="ERROR"):
            self.assertFalse(worker._select_channel(1))
        self.assertEqual(clicks, [], "the channel click must not be sent to another window")

    def test_no_answer_means_the_point_is_used(self):
        """The real reader may be unavailable (no pywin32) - that must never block the click."""

        worker, _screen, clicks = self._worker([])
        self.assertTrue(worker._activate_window())
        self.assertEqual(len(clicks), 1, clicks)

    def test_dry_run_never_asks_for_the_owner(self):
        asked = []

        def point_owner(x, y):
            asked.append((x, y))
            return (4242, "todo_helper (0403)")

        screen = _Screen(self._case.login_frame)
        worker = ReconnectWorker(
            _FakeSender(), threading.Event(), queue.Queue(maxsize=64),
            capture_fn=screen, sleep=lambda seconds: None, dry_run=True,
            click_fn=lambda x, y: True, wheel_fn=lambda x, y, n: True,
            point_owner_fn=point_owner,
        )
        self.assertTrue(worker._activate_window())
        self.assertEqual(asked, [], "a dry run must not query the desktop")



class PointOwnerClassificationTests(unittest.TestCase):
    """Which window at a click point is refused, and which is accepted (the field log)."""

    GAME_HWND = 263702
    DIALOG_HWND = 327856

    def _worker(self):
        worker = ReconnectWorker(
            _FakeSender(), threading.Event(), queue.Queue(maxsize=64),
            capture_fn=lambda: (None, (0, 0, 0, 0)),
            window_title="冒险岛怀旧服",
            sleep=lambda seconds: None,
            click_fn=lambda x, y: True,
            wheel_fn=lambda x, y, n: True,
        )
        worker.key_sender.hwnd = self.GAME_HWND
        return worker

    def _point_owner(self, worker, *, owner_pid, owner_title="igwUserLoginDialog", game_pid=None,
                     owner_class="igwUserLoginDialog"):
        import reconnect_worker as module
        from unittest import mock

        game_pid = owner_pid if game_pid is None else game_pid

        def process_id(hwnd):
            return game_pid if int(hwnd) == self.GAME_HWND else owner_pid

        with mock.patch.object(module, "window_at_point",
                               lambda x, y: (self.DIALOG_HWND, owner_title, owner_class)), \
                mock.patch.object(module, "window_process_id", process_id):
            return worker._point_owner(1322, 593)

    def test_a_window_of_the_games_process_is_accepted(self):
        """THE field case: igwUserLoginDialog is the game's own login dialog (same process)."""

        worker = self._worker()
        self.assertIsNone(self._point_owner(worker, owner_pid=4242),
                          "the game's own dialog must not block the click")
        # the game's own process is 4242 here, and it is not ours
        self.assertNotEqual(4242, __import__("os").getpid())

    def test_the_assistant_itself_is_refused(self):
        """The window that kept taking the foreground back: the assistant's own window."""

        import os

        worker = self._worker()
        owner = self._point_owner(worker, owner_pid=os.getpid(), owner_title="todo_helper (0404)")
        self.assertIsNotNone(owner, "a click on the assistant must be refused")
        self.assertEqual(owner[1], "todo_helper (0404)")

    def test_a_foreign_window_is_refused(self):
        """v0418: a window that is not the game's family is refused.

        Measured on this machine: a maximised browser window covered every click point, so the clicks
        went to the browser instead of the game (the operator: "so every click went to Chrome" - that
        was HIS browser being in front, but the guard must still refuse it: clicking a window that is
        not the game hands it the foreground and the game never sees the click).
        """

        worker = self._worker()
        with self.assertLogs("reconnect_worker", level="ERROR") as logs:
            owner = self._point_owner(worker, owner_pid=99999999, owner_title="some window",
                                      game_pid=4242, owner_class="SomeForeignClass")
        self.assertIsNotNone(owner, "a foreign window must be refused")
        self.assertEqual(owner[1], "some window")
        self.assertTrue(any("NOT the game" in line for line in logs.output), logs.output)

    def test_the_games_login_ui_is_accepted(self):
        """`igwUserLoginDialog` draws the login board and 连接, so it must NOT be refused."""

        worker = self._worker()
        owner = self._point_owner(worker, owner_pid=99999999, owner_title="igwUserLoginDialog",
                                  game_pid=4242, owner_class="igwUserLoginDialog")
        self.assertIsNone(owner, "the game's login UI must be clickable")

    def test_our_own_window_is_still_refused(self):
        """The one window that must never be clicked: the assistant's own."""

        import os

        worker = self._worker()
        owner = self._point_owner(worker, owner_pid=os.getpid(), owner_title="todo_helper")
        self.assertIsNotNone(owner)
        self.assertEqual(owner[1], "todo_helper")

    def test_without_the_game_handle_nothing_is_refused(self):
        """No game handle -> no comparison possible -> the point is used (never refuse blindly)."""

        import reconnect_worker as module
        from unittest import mock

        worker = self._worker()
        worker.key_sender.hwnd = 0
        worker.key_sender._hwnd = 0
        worker.window_title = ""
        with mock.patch.object(module, "window_at_point",
                               lambda x, y: (4242, "some window", "SomeClass")):
            self.assertIsNone(worker._point_owner(1322, 593),
                              "without the game hwnd the guard must not judge")

    def test_the_game_window_itself_is_accepted(self):
        import reconnect_worker as module
        from unittest import mock

        worker = self._worker()
        with mock.patch.object(module, "window_at_point",
                               lambda x, y: (self.GAME_HWND, "冒险岛怀旧服", "MapleStory")):
            self.assertIsNone(worker._point_owner(1322, 593))



class _StubbornPageMachine(_PageMachine):
    """A page machine that IGNORES the first ``stubborn`` Enter presses (the game ignored them)."""

    def __init__(self, stubborn=1, **kwargs):
        super().__init__(**kwargs)
        self.stubborn = int(stubborn)
        self.ignored = 0

    def key(self, name):
        if name == "enter" and self.stubborn > 0:
            self.stubborn -= 1
            self.ignored += 1
            return
        super().key(name)

class PageVerifierTests(unittest.TestCase):
    """The operator's own frame for each page, and the retry the verifier drives.

    "you didn't make a verifier for each step, this time the first step is failed but you didn't
    notice and retry.  the first page contains element 连接 a deep brown login board and 结束游戏,
    the second one contains 蓝蜗牛 or other options, the third one should show channel"
    """

    def setUp(self):
        self._case = ReconnectSequenceTests("test_the_settings_are_validated")
        self._case.setUp()

    def tearDown(self):
        self._case.tearDown()

    def _worker(self, pages, *, clicks=None, screen=None):
        screen = _Screen(self._case.login_frame) if screen is None else screen
        clicks = [] if clicks is None else clicks
        worker = ReconnectWorker(
            _FakeSender(pages=pages), threading.Event(), queue.Queue(maxsize=64),
            capture_fn=screen, template_path=self._case.template_path,
            sleep=lambda seconds: None,
            click_fn=_page_aware_click(pages, lambda x, y: clicks.append((x, y)) or True),
            wheel_fn=lambda x, y, n: True,
            page_fn=pages,
        )
        worker.set_enabled(True)
        worker.press_enter_for_offline_prompt = False     # see the shared factory's note
        return worker, clicks

    # ---------------------------------------------------------------- the classifier itself
    # The frames the operator supplied, with the page each one shows and the score that frame's own
    # signature must reach.  0.8 is the bar for a patch that was cropped FROM that frame's own
    # resolution; the channel signature is cropped from his 1080 channel window, and the older 1366
    # screenshot shows a DIFFERENT channel state (its rows/numbers moved on), which costs it ~0.10 -
    # it still beats the next-best page by a factor of nine, which is what page recognition needs.
    OPERATOR_FRAMES = {
        "login_page.jpg": (PAGE_LOGIN, 0.8),
        "channel_select_first.jpg": (PAGE_WORLD, 0.8),
        "channel_select_second.jpg": (PAGE_CHANNEL, 0.65),
        "1080_login_page.jpg": (PAGE_LOGIN, 0.8),
        "1080_wolrd_select.jpg": (PAGE_WORLD, 0.8),
        "1080_channel_select.jpg": (PAGE_CHANNEL, 0.8),
    }

    def test_the_operators_own_frames_are_classified(self):
        """login_page.jpg -> login, channel_select_first -> world, ..._second -> channel, and the
        three 1080 frames of his second device must be classified just as reliably."""

        import cv2
        from reconnect_worker import detect_page, load_page_references

        references = load_page_references()
        self.assertEqual(set(references), {PAGE_LOGIN, PAGE_WORLD, PAGE_CHANNEL},
                         "all three page signatures must be shipped/available")
        for name, (page, minimum) in self.OPERATOR_FRAMES.items():
            path = Path("screenshots") / name
            if not path.is_file():
                self.skipTest(f"{path} is not in this checkout")
            frame = cv2.imread(str(path), cv2.IMREAD_COLOR)
            detected, score = detect_page(frame, references)
            self.assertEqual(detected, page, f"{name} -> {detected} ({score:.3f})")
            self.assertGreater(score, minimum, f"{name} matched only {score:.3f}")

    def test_a_frame_that_is_none_of_the_pages_scores_low(self):
        """In game (or anything else) the answer must be None, not a guess."""

        import cv2
        from reconnect_worker import detect_page, load_page_references

        path = Path("screenshots/second_window.jpg")
        if not path.is_file():
            self.skipTest(f"{path} is not in this checkout")
        frame = cv2.imread(str(path), cv2.IMREAD_COLOR)
        detected, score = detect_page(frame, load_page_references())
        self.assertIsNone(detected, f"an in-game frame must not be a page ({detected} {score:.3f})")

    def test_the_disconnect_prompt_page_is_not_the_login_page(self):
        """掉线提示 covers the login board - it must NOT be classified as the login page.

        Measured on the operator's own screenshot of that page (his 1080x768 client): the 连接 patch
        scores 0.031 there against 0.994 on the real login page, and both login click points land on
        the prompt.  This is what lets the worker wait for the prompt to close instead of clicking the
        board through a dialog ("the real disconnect event will first go to this page, so after 30s the
        first thing you should do is press enter to close this prompt").
        """

        import cv2
        from reconnect_worker import PAGE_MIN_SCORE, detect_page, load_page_references, page_scores

        path = Path("screenshots/faulty_disconnect.jpg")
        if not path.is_file():
            self.skipTest(f"{path} is not in this checkout")
        frame = cv2.imread(str(path), cv2.IMREAD_COLOR)
        scores = page_scores(frame, load_page_references())
        detected, score = detect_page(frame, load_page_references())
        self.assertNotEqual(detected, PAGE_LOGIN, f"the prompt page looked like the login page ({scores})")
        self.assertLess(scores[PAGE_LOGIN], PAGE_MIN_SCORE, scores)
        self.assertIsNone(detected, f"nothing on that page is recognised ({detected} {score:.3f})")

    # ---------------------------------------------------------------- the retries
    def test_the_login_step_is_retried_until_the_page_changes(self):
        """The 连接 click is ignored twice: the step repeats itself and still succeeds."""

        pages = _PageMachine(connect_ignored=1)
        worker, clicks = self._worker(pages)
        worker.set_channel(1)
        self.assertTrue(worker._enter_game(), "the step must retry until the world page appears")
        self.assertEqual(pages.page, PAGE_WORLD)
        self.assertEqual(pages.ignored_connects, 1, "the 连接 click was ignored once")
        # the click on 连接 was repeated (the step retried), it was not assumed to have worked
        from reconnect_worker import LOGIN_CONNECT_CLICK_CLIENT

        connect = (100 + LOGIN_CONNECT_CLICK_CLIENT[0], 50 + LOGIN_CONNECT_CLICK_CLIENT[1])
        self.assertGreaterEqual(clicks.count(connect), 1, clicks)
        reports = [state for state, _detail in self._case._reports(worker)]
        self.assertIn("login-done", reports, reports)

    def test_a_step_that_never_changes_the_page_fails_instead_of_clicking_on(self):
        """The field complaint: a failed first step must be noticed, not walked past."""

        # the login page never goes away: no Enter and no 连接 click has any effect
        pages = _PageMachine(advances_on_enter=False, connect_ignored=99)
        worker, clicks = self._worker(pages)
        worker.set_channel(1)
        with self.assertLogs("reconnect_worker", level="ERROR"):
            worker._handle_disconnect()

        from reconnect_worker import CHANNEL_FIRST_CLIENT, WORLD_ROW_CLIENT

        reports = self._case._reports(worker)
        failed = [detail for state, detail in reports if state == "failed"]
        self.assertTrue(any("登录步骤失败" in detail for detail in failed), reports)
        # and NO world row / channel cell was ever clicked after the failed login step
        self.assertNotIn((100 + WORLD_ROW_CLIENT[0], 50 + WORLD_ROW_CLIENT[1]), clicks, clicks)
        self.assertNotIn((100 + CHANNEL_FIRST_CLIENT[0], 50 + CHANNEL_FIRST_CLIENT[1]), clicks,
                         clicks)

    def test_the_world_step_is_retried_until_the_channel_page_appears(self):
        pages = _StubbornPageMachine(stubborn=2, page=PAGE_WORLD)
        worker, _clicks = self._worker(pages)
        self.assertTrue(worker._select_world("蓝蜗牛"))
        self.assertEqual(pages.page, PAGE_CHANNEL)

    def test_the_world_step_fails_when_the_channel_page_never_appears(self):
        pages = _PageMachine(page=PAGE_WORLD, advances_on_enter=False)
        worker, _clicks = self._worker(pages)
        with self.assertLogs("reconnect_worker", level="ERROR"):
            self.assertFalse(worker._select_world("蓝蜗牛"))

    def test_a_step_skips_itself_when_the_page_is_already_past_it(self):
        """Robustness: if the game is already on the channel page, the login/world steps are not
        repeated."""

        pages = _PageMachine(page=PAGE_CHANNEL)
        worker, clicks = self._worker(pages)
        self.assertTrue(worker._enter_game(), "already past the login page")
        self.assertTrue(worker._select_world("蓝蜗牛"), "already on the channel page")
        self.assertEqual(clicks, [], f"nothing may be clicked when the page is already right: {clicks}")

    def test_the_channel_step_is_skipped_when_the_game_is_already_running(self):
        pages = _PageMachine(page=None)
        worker, clicks = self._worker(pages)
        self.assertTrue(worker._select_channel(51))
        self.assertEqual(clicks, [], clicks)

    def test_no_page_signature_matches_another_page(self):
        """The v0405 flaw, pinned: the world signature must NOT match the login page.

        Measured field report: "the verifier broke (login not success but going into the second
        stage)".  The single 340x512 board crop was dominated by background and scored 1.000 on the
        world page as well, so the login step could be declared done while the login page was still
        on screen.  Every page now needs SEVERAL small patches and scores the MINIMUM of them.
        """

        import cv2
        from reconnect_worker import PAGE_MIN_SCORE, load_page_references, page_scores

        references = load_page_references()
        for name, (own_page, minimum) in self.OPERATOR_FRAMES.items():
            path = Path("screenshots") / name
            if not path.is_file():
                self.skipTest(f"{path} is not in this checkout")
            frame = cv2.imread(str(path), cv2.IMREAD_COLOR)
            scores = page_scores(frame, references)
            self.assertGreater(scores[own_page], minimum,
                               f"{name} must match {own_page}: {scores}")
            for page, score in scores.items():
                if page == own_page:
                    continue
                self.assertLess(score, PAGE_MIN_SCORE,
                                f"{name} must NOT match {page} ({score:.3f}); all: {scores}")

    def test_the_board_click_alone_never_completes_the_login_step(self):
        """A pixel change from the board click is not evidence: only the page may confirm it."""

        clicks = []
        screen = _Screen(self._case.login_frame)

        def click(x, y):
            clicks.append((x, y))
            # every click changes the picture (a hover/highlight would do the same)
            screen.frame = np.full_like(screen.frame, 30 + (len(clicks) % 7) * 8)
            return True

        pages = _PageMachine(advances_on_enter=False, connect_ignored=99)   # the login page sticks
        sender = _FakeSender(pages=pages)
        worker = ReconnectWorker(
            sender, threading.Event(), queue.Queue(maxsize=64),
            capture_fn=screen, template_path=self._case.template_path,
            sleep=lambda seconds: None,
            click_fn=click,
            wheel_fn=lambda x, y, n: True,
            page_fn=pages,
        )
        worker.set_enabled(True)
        worker.set_channel(1)
        with self.assertLogs("reconnect_worker", level="ERROR"):
            worker._handle_disconnect()

        from reconnect_worker import WORLD_ROW_CLIENT

        reports = self._case._reports(worker)
        failed = [detail for state, detail in reports if state == "failed"]
        self.assertTrue(any("登录步骤失败" in detail for detail in failed), reports)
        self.assertNotIn((100 + WORLD_ROW_CLIENT[0], 50 + WORLD_ROW_CLIENT[1]), clicks,
                         f"the world step must not run after a failed login: {clicks}")

    def test_the_page_is_polled_while_the_game_catches_up(self):
        """The game is not instant: the page may appear a few polls after the action."""

        class _SlowPageMachine(_PageMachine):
            def __init__(self, delay=3, **kwargs):
                super().__init__(**kwargs)
                self.delay = int(delay)

            def key(self, name):
                if name != "enter":
                    return
                self.delay -= 1
                if self.delay <= 0:
                    super().key(name)

        pages = _SlowPageMachine(delay=3, connect_ignored=99)
        worker, _clicks = self._worker(pages)
        worker.set_channel(1)
        self.assertTrue(worker._enter_game(),
                        "the step must wait for the page instead of sampling once")
        self.assertEqual(pages.page, PAGE_WORLD)

    def test_our_own_window_is_refused_even_without_a_game_handle(self):
        """The operator's bug: a click reached the panel's 自动重连 button.

        The "our own process" test used to sit AFTER the "the game window could not be resolved ->
        accept" early return, so with no game handle a point over our own panel was accepted and
        clicked.
        """

        import os
        from unittest import mock

        import reconnect_worker as module

        worker, _clicks = self._worker(_page_fn())
        worker.key_sender.hwnd = 0
        worker.key_sender._hwnd = 0
        worker.window_title = ""
        worker._frame_hwnd = 0
        with mock.patch.object(module, "window_at_point",
                               lambda x, y: (4242, "todo_helper (v0416)", "TkTopLevel")), \
                mock.patch.object(module, "window_process_id", lambda hwnd: os.getpid()), \
                self.assertLogs("reconnect_worker", level="ERROR") as logs:
            owner = worker._point_owner(1322, 593)
        self.assertIsNotNone(owner, "our own window must be refused even without a game handle")
        self.assertTrue(any("OUR OWN window" in line for line in logs.output), logs.output)

    def test_a_click_on_our_own_panel_is_never_sent(self):
        """End to end: the panel covers the activation point -> no click, and the panel is lowered."""

        import os
        from unittest import mock

        import reconnect_worker as module

        clicks = []
        lowered = []

        class _Worker(ReconnectWorker):
            def _lower_own_windows(self):
                lowered.append(True)
                return 1

        screen = _Screen(self._case.login_frame)
        worker = _Worker(
            _FakeSender(pages=_page_fn()), threading.Event(), queue.Queue(maxsize=64),
            capture_fn=screen, template_path=self._case.template_path,
            sleep=lambda seconds: None,
            click_fn=lambda x, y: clicks.append((x, y)) or True,
            wheel_fn=lambda x, y, n: True,
            page_fn=_page_fn(),
        )
        worker.set_enabled(True)
        with mock.patch.object(module, "window_at_point",
                               lambda x, y: (4242, "todo_helper (v0416)", "TkTopLevel")), \
                mock.patch.object(module, "window_process_id", lambda hwnd: os.getpid()), \
                self.assertLogs("reconnect_worker", level="ERROR"):
            self.assertFalse(worker._activate_window())
        self.assertEqual(clicks, [], "the panel must never be clicked")
        self.assertEqual(lowered, [True], "the panel is lowered before giving up")

    def test_the_assistant_is_lowered_before_a_refused_point_is_given_up(self):
        """A point our own window covers: lower the panel, retry, and only then refuse."""

        lowered = []

        class _Worker(ReconnectWorker):
            def _lower_own_windows(self):
                lowered.append(True)
                return 1

        screen = _Screen(self._case.login_frame)
        worker = _Worker(
            _FakeSender(pages=_page_fn()), threading.Event(), queue.Queue(maxsize=64),
            capture_fn=screen, template_path=self._case.template_path,
            sleep=lambda seconds: None,
            click_fn=lambda x, y: True,
            wheel_fn=lambda x, y, n: True,
            # EVERY point belongs to the assistant window
            point_owner_fn=lambda x, y: (4242, "todo_helper (0407)"),
            page_fn=_page_fn(),
        )
        worker.set_enabled(True)
        with self.assertLogs("reconnect_worker", level="ERROR"):
            self.assertFalse(worker._activate_window())
        self.assertEqual(lowered, [True], "the assistant must be lowered before giving up")

    def test_a_board_click_that_cannot_be_sent_does_not_end_the_run(self):
        """The operator's report: the board step always failed and nothing else happened."""

        from reconnect_worker import LOGIN_BOARD_CLICK_CLIENT

        pages = _page_fn()
        screen = _Screen(self._case.login_frame)
        clicks = []

        def owner(x, y):
            # only the board point belongs to the assistant; 连接 and the rest are fine
            client_x, client_y = int(x) - 100, int(y) - 50
            if (abs(client_x - LOGIN_BOARD_CLICK_CLIENT[0]) <= 6
                    and abs(client_y - LOGIN_BOARD_CLICK_CLIENT[1]) <= 6):
                return (4242, "todo_helper (0407)")
            return None

        sender = _FakeSender(pages=pages)

        def click(x, y):
            clicks.append((x, y))
            pages.clicked(x, y)
            return True

        worker = ReconnectWorker(
            sender, threading.Event(), queue.Queue(maxsize=64),
            capture_fn=screen, template_path=self._case.template_path,
            sleep=lambda seconds: None,
            click_fn=click,
            wheel_fn=lambda x, y, n: True,
            point_owner_fn=owner,
            page_fn=pages,
        )
        worker.set_enabled(True)
        worker.press_enter_for_offline_prompt = False     # see the shared factory's note
        worker.set_channel(1)
        with self.assertLogs("reconnect_worker", level="WARNING"):
            worker._handle_disconnect()

        reports = self._case._reports(worker)
        states = [state for state, _detail in reports]
        self.assertIn("done", states, reports)
        self.assertNotIn("failed", states, reports)
        # the board point was never clicked, the run went on to 连接
        from reconnect_worker import LOGIN_CONNECT_CLICK_CLIENT

        board = (100 + LOGIN_BOARD_CLICK_CLIENT[0], 50 + LOGIN_BOARD_CLICK_CLIENT[1])
        connect = (100 + LOGIN_CONNECT_CLICK_CLIENT[0], 50 + LOGIN_CONNECT_CLICK_CLIENT[1])
        self.assertNotIn(board, clicks, clicks)
        self.assertIn(connect, clicks, clicks)

    def test_a_point_on_the_window_title_bar_is_refused(self):
        """The title bar belongs to the game window, so only the CLIENT rect can exclude it."""

        class _Worker(ReconnectWorker):
            def _client_rect_on_screen(self):
                # the client starts 51 px below the capture origin: y < 231 is the title-bar band
                return (600, 231, 1966, 999)

        screen = _Screen(self._case.login_frame)
        clicks = []
        pages = _page_fn()
        worker = _Worker(
            _FakeSender(pages=pages), threading.Event(), queue.Queue(maxsize=64),
            capture_fn=screen, template_path=self._case.template_path,
            sleep=lambda seconds: None,
            click_fn=lambda x, y: clicks.append((x, y)) or True,
            wheel_fn=lambda x, y, n: True,
            page_fn=pages,
        )
        worker.set_enabled(True)
        # the activation point would be at the client origin (0, 0) in this fake: put it on the title
        worker._capture_fn = lambda: (self._case.login_frame, (600, 180, 1966, 948))
        with self.assertLogs("reconnect_worker", level="ERROR") as logs:
            self.assertFalse(worker._activate_window())
        self.assertEqual(clicks, [], "a title-bar point must never be clicked")
        self.assertTrue(any("outside its CLIENT area" in line for line in logs.output), logs.output)

    def test_a_point_inside_the_client_is_clicked(self):
        class _Worker(ReconnectWorker):
            def _client_rect_on_screen(self):
                return (100, 50, 1466, 818)

        screen = _Screen(self._case.login_frame)
        clicks = []
        pages = _page_fn()
        worker = _Worker(
            _FakeSender(pages=pages), threading.Event(), queue.Queue(maxsize=64),
            capture_fn=screen, template_path=self._case.template_path,
            sleep=lambda seconds: None,
            click_fn=lambda x, y: clicks.append((x, y)) or True,
            wheel_fn=lambda x, y, n: True,
            page_fn=pages,
        )
        worker.set_enabled(True)
        self.assertTrue(worker._activate_window())
        self.assertEqual(len(clicks), 1, clicks)

    def test_without_page_signatures_the_pixel_check_is_used(self):
        """No signature available (older/other install): the old pixel verification still logs in."""

        from unittest import mock

        import reconnect_worker as module

        screen = _Screen(self._case.login_frame)
        clicks = []
        sender = _FakeSender(on_press=lambda key: screen.react_to_enter())
        worker = ReconnectWorker(
            sender, threading.Event(), queue.Queue(maxsize=64),
            capture_fn=screen, template_path=self._case.template_path,
            sleep=lambda seconds: None,
            click_fn=lambda x, y: clicks.append((x, y)) or True,
            wheel_fn=lambda x, y, n: True,
        )
        worker.set_enabled(True)
        worker.set_channel(1)
        with mock.patch.object(module, "load_page_references", lambda: {}):
            self.assertFalse(worker.page_verification_available(),
                             "no signature must mean: fall back, not fail")
            worker._handle_disconnect()

        reports = self._case._reports(worker)
        states = [state for state, _detail in reports]
        self.assertIn("done", states, reports)
        self.assertNotIn("failed", states, reports)


class DisarmedInputAnnouncementTests(unittest.TestCase):
    """After a run that leaves live input off, the operator is told (log + panel)."""

    def setUp(self):
        self._case = ReconnectSequenceTests("test_the_settings_are_validated")
        self._case.setUp()

    def tearDown(self):
        self._case.tearDown()

    def _run(self, sender):
        worker = self._case._worker(
            frame=self._case.login_frame, sender=sender, click_reacts_channels=True
        )
        worker.set_channel(1)
        with self.assertLogs("reconnect_worker", level="ERROR") as captured:
            worker._handle_disconnect()
        reports = []
        while not worker.result_queue.empty():
            reports.append(worker.result_queue.get_nowait())
        return reports, captured.output

    def test_a_successful_run_that_leaves_input_off_says_so(self):
        # The successful drill restores the input state it found: disarmed stays disarmed, which is
        # exactly the state in which every hotkey that types is refused.
        reports, log = self._run(_ArmableFakeSender(enabled=False))
        details = [detail for state, detail in reports if state == "input"]
        self.assertTrue(any("实时输入已关闭" in text for text in details), reports)
        self.assertTrue(
            any("live input is OFF after this run" in line for line in log), log
        )

    def test_an_armed_run_does_not_announce_a_disarmed_input(self):
        reports, _log = self._run(_ArmableFakeSender(enabled=True))
        details = [detail for state, detail in reports if state == "input"]
        self.assertFalse(any("实时输入已关闭" in text for text in details), reports)


class OfflinePromptTests(unittest.TestCase):
    """掉线提示窗口: ONE Enter right after the login page is recognized (operator, 2026-09-17).

    The game puts a prompt (default button 确定) over the client the moment the connection drops;
    the login board is not usable until it is closed.  The Enter must come AFTER the activation
    click (the game ignores keys until its window has been clicked) and BEFORE anything is aimed at
    the board.
    """

    def setUp(self):
        self._case = ReconnectSequenceTests("test_the_settings_are_validated")
        self._case.setUp()

    def tearDown(self):
        self._case.tearDown()

    def _run(self, *, sender=None, screen=None):
        screen = _Screen(self._case.login_frame) if screen is None else screen
        clicks = []
        pages = _page_fn()
        worker = ReconnectWorker(
            sender if sender is not None else _FakeSender(pages=pages),
            threading.Event(), queue.Queue(maxsize=64),
            capture_fn=screen, template_path=self._case.template_path,
            sleep=lambda seconds: None,
            click_fn=_page_aware_click(pages, lambda x, y: clicks.append((x, y)) or True),
            wheel_fn=lambda x, y, n: True,
            page_fn=pages,
        )
        worker.set_enabled(True)
        worker.set_world("蓝蜗牛")
        worker.set_channel(1)
        worker._handle_disconnect()
        reports = []
        while not worker.result_queue.empty():
            reports.append(worker.result_queue.get_nowait())
        return worker, clicks, reports

    def test_one_enter_is_sent_and_it_comes_before_the_board_click(self):
        from reconnect_worker import ACTIVATE_CLICK_CLIENT, LOGIN_BOARD_CLICK_CLIENT

        sender = _FakeSender()
        order = []
        sender.press = lambda key, duration=0.025, **kwargs: order.append(("press", key)) or True
        screen = _Screen(self._case.login_frame)
        worker, clicks, reports = self._run(sender=sender, screen=screen)
        # the prompt Enter is the FIRST key of the run
        self.assertEqual(order[0], ("press", "enter"), order[:3])
        # ...and the activation click comes before it, the board click after it
        self.assertEqual(clicks[0], (100 + ACTIVATE_CLICK_CLIENT[0],
                                    50 + ACTIVATE_CLICK_CLIENT[1]), clicks)
        self.assertIn((100 + LOGIN_BOARD_CLICK_CLIENT[0],
                       50 + LOGIN_BOARD_CLICK_CLIENT[1]), clicks[1:], clicks)
        self.assertTrue(any(state == "prompt" for state, _detail in reports), reports)

    def _worker_with_page(self, page_fn, sender=None, screen=None):
        screen = _Screen(self._case.login_frame) if screen is None else screen
        clicks = []
        worker = ReconnectWorker(
            sender if sender is not None else _FakeSender(),
            threading.Event(), queue.Queue(maxsize=64),
            capture_fn=screen, template_path=self._case.template_path,
            sleep=lambda seconds: None,
            click_fn=lambda x, y: clicks.append((x, y)) or True,
            wheel_fn=lambda x, y, n: True,
            page_fn=page_fn,
        )
        worker.set_enabled(True)
        worker.set_world("蓝蜗牛")
        worker.set_channel(1)
        return worker, clicks

    def test_the_enter_is_repeated_until_the_prompt_is_gone(self):
        """The prompt page is not one of the three pages: keep pressing Enter until it closes."""

        from reconnect_worker import LOGIN_BOARD_CLICK_CLIENT, OFFLINE_PROMPT_ATTEMPTS

        self.assertGreaterEqual(OFFLINE_PROMPT_ATTEMPTS, 2)
        state = {"enters": 0}

        def page_fn():
            # the prompt page: the classifier recognises nothing until the prompt is closed
            return (PAGE_LOGIN, 1.0) if state["enters"] >= 2 else (None, 0.31)

        sender = _FakeSender()
        sender.press = lambda key, duration=0.025, **kwargs: state.__setitem__(
            "enters", state["enters"] + 1) or True
        worker, clicks = self._worker_with_page(page_fn, sender)

        worker._handle_disconnect()

        self.assertGreaterEqual(state["enters"], 2, state)
        self.assertIn((100 + LOGIN_BOARD_CLICK_CLIENT[0], 50 + LOGIN_BOARD_CLICK_CLIENT[1]), clicks,
                      f"the workflow starts once the prompt is closed: {clicks}")

    def test_a_prompt_that_never_closes_stops_before_the_board_click(self):
        """A prompt that stays up must NOT be clicked through: only its close button may be clicked.

        The close button is the middle-centred anchor (middle, 424) - and on this 1366 client that
        point happens to lie inside the board box, so the check is "the board point and the 连接 point
        were never clicked", not "no click inside the box".
        """

        from reconnect_worker import (
            LOGIN_BOARD_CLICK_CLIENT, LOGIN_CONNECT_CLICK_CLIENT, LOGIN_PROMPT_CLOSE_CLIENT,
            OFFLINE_PROMPT_ATTEMPTS,
        )

        state = {"enters": 0}
        sender = _FakeSender()
        sender.press = lambda key, duration=0.025, **kwargs: state.__setitem__(
            "enters", state["enters"] + 1) or True
        worker, clicks = self._worker_with_page(lambda: (None, 0.31), sender)

        worker._handle_disconnect()

        self.assertGreaterEqual(state["enters"], OFFLINE_PROMPT_ATTEMPTS)
        for point, label in ((LOGIN_BOARD_CLICK_CLIENT, "the board point"),
                             (LOGIN_CONNECT_CLICK_CLIENT, "the 连接 point")):
            self.assertNotIn((100 + point[0], 50 + point[1]), clicks,
                             f"{label} must not be clicked through the prompt: {clicks}")
        close = (100 + LOGIN_PROMPT_CLOSE_CLIENT[0], 50 + LOGIN_PROMPT_CLOSE_CLIENT[1])
        self.assertIn(close, clicks, f"the prompt close button is clicked: {clicks}")
        reports = self._reports(worker)
        self.assertTrue(any(state == "failed" and "掉线提示" in detail
                            for state, detail in reports), reports)

    def test_the_prompt_enter_can_be_switched_off(self):
        sender = _FakeSender()
        keys = []
        sender.press = lambda key, duration=0.025, **kwargs: keys.append(key) or True
        screen = _Screen(self._case.login_frame)
        pages = _page_fn()
        worker = ReconnectWorker(
            sender, threading.Event(), queue.Queue(maxsize=64),
            capture_fn=screen, template_path=self._case.template_path,
            sleep=lambda seconds: None,
            click_fn=_page_aware_click(pages, lambda x, y: True),
            wheel_fn=lambda x, y, n: True,
            page_fn=pages,
        )
        worker.set_enabled(True)
        worker.press_enter_for_offline_prompt = False
        worker._handle_disconnect()
        self.assertNotIn("prompt", [state for state, _detail in self._reports(worker)])

    @staticmethod
    def _reports(worker):
        reports = []
        while not worker.result_queue.empty():
            reports.append(worker.result_queue.get_nowait())
        return reports


if __name__ == "__main__":
    unittest.main()
