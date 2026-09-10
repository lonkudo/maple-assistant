# -*- coding: utf-8 -*-
"""Targeted tests for the optional auto lie-pass GPU feature.

These cover only dependency-free logic: environment probe shape, seed-mask
geometry, event queueing while disabled/enabled, and the too-small-square
guard.  GPU tracking itself is validated on the development machine with
the real game window / recordings, not in unit tests.
"""

import queue
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from auto_lie_worker import (
    AWAIT_TARGET_SECONDS,
    AutoLieWorker,
    TARGET_TAKEOVER_SECONDS,
    _CANDIDATE_STABLE_FRAMES,
    _LieSequenceEngine,
    _bgr_from_pil,
    _find_lie_window_box,
    _find_target_box,
    _min_lie_target_size,
    _novelty_next_state,
    lie_ui_scale,
    lie_window_box,
    make_seed_mask,
    probe_auto_lie_environment,
)


def _bgr_frame(width=640, height=480, background=(0, 0, 0), rects=()):
    """Synthetic BGR frame; rects are (x1, y1, x2, y2, RGB) tuples."""

    import numpy as np

    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :] = background[::-1]
    for x1, y1, x2, y2, color in rects:
        frame[y1:y2, x1:x2] = color[::-1]
    return frame


class TargetBoxFinderTests(unittest.TestCase):
    def test_finds_centre_white_square(self):
        frame = _bgr_frame(
            rects=[(290, 210, 350, 270, (255, 255, 255))],  # 60x60 at centre
        )
        box = _find_target_box(frame)
        self.assertIsNotNone(box)
        left, top, width, height = box
        self.assertAlmostEqual(left + width / 2.0, 320, delta=12)
        self.assertAlmostEqual(top + height / 2.0, 240, delta=12)
        self.assertGreaterEqual(min(width, height), 48)
        self.assertLessEqual(max(width, height), 76)

    def test_fullwidth_ui_bar_is_ignored(self):
        # A 640x20 white strip spans the whole width (bad UI) plus the real
        # 60x60 target near the centre: only the target may win.
        frame = _bgr_frame(
            rects=[
                (0, 0, 640, 20, (255, 255, 255)),
                (290, 210, 350, 270, (255, 255, 255)),
            ],
        )
        box = _find_target_box(frame)
        self.assertIsNotNone(box)
        left, top, width, height = box
        self.assertAlmostEqual(left + width / 2.0, 320, delta=20)
        self.assertGreater(top, 40)  # not the top strip

    def test_grey_slice_never_matches(self):
        # #c9ced0 slice (V~208) is below the bright-white threshold.
        frame = _bgr_frame(
            rects=[(290, 210, 350, 270, (201, 206, 208))],
        )
        self.assertIsNone(_find_target_box(frame))

    def test_none_on_dark_frame(self):
        self.assertIsNone(_find_target_box(_bgr_frame()))

    def test_too_small_blob_is_ignored(self):
        frame = _bgr_frame(
            rects=[(315, 235, 325, 245, (255, 255, 255))],  # 10x10
        )
        self.assertIsNone(_find_target_box(frame))


def _fake_frame(width=640, height=480, sequence=7, color=(0, 0, 0)):
    image = Image.new("RGB", (width, height), color)
    return SimpleNamespace(
        image=image,
        sequence=sequence,
        window_rect=(0, 0, width, height),
    )


class LieWindowGeometryTests(unittest.TestCase):
    """The tracker is fed the lie popup only, never the whole game client."""

    def test_reference_client_uses_the_measured_popup_box(self):
        left, top, width, height = lie_window_box(1366, 768)
        self.assertEqual((width, height), (767, 598))
        self.assertEqual(left, (1366 - 767) // 2)
        self.assertEqual(top, (768 - 598) // 2)

    def test_smaller_client_scales_and_stays_centred(self):
        left, top, width, height = lie_window_box(640, 480)
        self.assertEqual((width, height), (359, 280))
        self.assertEqual(left, (640 - width) // 2)
        self.assertEqual(top, (480 - height) // 2)

    def test_countdown_guard_scales_with_the_client(self):
        # At the HUD reference the popup's countdown digits (~52x47) are
        # rejected while the real target (~89x99) is accepted.
        self.assertEqual(_min_lie_target_size(1366), 64)
        self.assertGreater(_min_lie_target_size(1366), 53)
        self.assertLess(_min_lie_target_size(1366), 89)
        self.assertGreaterEqual(_min_lie_target_size(320), 16)

    def test_1366_and_1920_presets_are_recalculated(self):
        # 1920x1080 shares the 1366x768 preset but renders it shrunk (the
        # game's 1075-wide UI reference), so every popup number is recomputed.
        self.assertAlmostEqual(lie_ui_scale(1366), 1.0, places=3)
        shrink = 1075.0 / 1366.0
        self.assertAlmostEqual(lie_ui_scale(1920), shrink, places=3)
        # The same UI is drawn at 1075 wide -> same scale as the raw HUD curve.
        self.assertAlmostEqual(lie_ui_scale(1075), shrink, places=3)
        # Above the reference the HUD curve is flat unless a preset says so.
        self.assertAlmostEqual(lie_ui_scale(2560), 1.0, places=3)

        left, top, width, height = lie_window_box(1920, 1080)
        self.assertEqual((width, height),
                         (round(767 * shrink), round(598 * shrink)))
        self.assertEqual(left, (1920 - width) // 2)
        self.assertEqual(top, (1080 - height) // 2)
        # The countdown (~41x37 shrunk) is still rejected, the target
        # (~70x78 shrunk) still accepted.
        self.assertEqual(_min_lie_target_size(1920),
                         int(round(64 * shrink)))
        self.assertGreater(_min_lie_target_size(1920), 41)
        self.assertLess(_min_lie_target_size(1920), 70)

    def test_finds_centred_popup(self):
        import cv2

        frame = _bgr_frame(640, 480)
        cv2.rectangle(frame, (150, 120), (490, 360), (255, 255, 255), 6)
        cv2.rectangle(frame, (160, 130), (480, 150), (255, 255, 255), -1)
        box = _find_lie_window_box(frame)
        self.assertIsNotNone(box)
        left, top, width, height = box
        self.assertAlmostEqual(left + width / 2.0, 320, delta=20)
        self.assertAlmostEqual(top + height / 2.0, 240, delta=20)

    def test_no_popup_on_an_empty_or_small_blob_frame(self):
        self.assertIsNone(_find_lie_window_box(_bgr_frame(640, 480)))
        self.assertIsNone(_find_lie_window_box(_bgr_frame(
            rects=[(300, 220, 360, 280, (255, 255, 255))],
        )))


class ProbeTests(unittest.TestCase):
    def test_probe_returns_bool_reason_pair(self):
        supported, reason = probe_auto_lie_environment()
        self.assertIsInstance(supported, bool)
        self.assertIsInstance(reason, str)


class SeedMaskTests(unittest.TestCase):
    def test_mask_fills_box(self):
        mask = make_seed_mask((100, 120, 3), (10, 20, 30, 40))
        self.assertEqual(mask.dtype.name, "uint8")
        self.assertEqual(mask.shape, (100, 120))
        self.assertEqual(int(mask.sum()), 30 * 40)
        self.assertEqual(int(mask[20, 10]), 1)
        self.assertEqual(int(mask[59, 39]), 1)
        self.assertEqual(int(mask[60, 40]), 0)
        self.assertEqual(int(mask[19, 10]), 0)

    def test_mask_clips_negative_and_overflow(self):
        mask = make_seed_mask((50, 60, 3), (-10, -10, 100, 100))
        self.assertEqual(mask.shape, (50, 60))
        self.assertEqual(int(mask.sum()), 50 * 60)

    def test_mask_never_empty_even_for_insane_box(self):
        mask = make_seed_mask((50, 60, 3), (100, 100, 0, 0))
        self.assertEqual(int(mask.sum()), 1)  # clamped to a single pixel


class WorkerQueueingTests(unittest.TestCase):
    def setUp(self):
        self.stop = threading.Event()
        self.frames = queue.Queue(maxsize=1)
        self.worker = AutoLieWorker(self.frames, self.stop, enabled=False)

    def tearDown(self):
        self.stop.set()

    def test_disabled_worker_ignores_lie_event(self):
        self.worker.set_enabled(False)
        self.worker.on_lie_seen((10, 10, 30, 30), _fake_frame())
        with self.worker._lock:
            self.assertIsNone(self.worker._pending)

    def test_enabled_worker_queues_single_event(self):
        self.worker.set_enabled(True)
        frame = _fake_frame()
        self.worker.on_lie_seen((10, 10, 30, 30), frame)
        with self.worker._lock:
            self.assertIsNotNone(self.worker._pending)
        # A second event while one is pending must be ignored (single-shot).
        self.worker.on_lie_seen((50, 50, 30, 30), _fake_frame())
        with self.worker._lock:
            match, queued_frame = self.worker._pending
            self.assertEqual(match, (10, 10, 30, 30))
            self.assertIs(queued_frame, frame)

    def test_clear_event_marks_active_sequence_only(self):
        self.worker.set_enabled(True)
        # No active sequence -> clear must be a no-op.
        self.worker.on_lie_seen(None, _fake_frame())
        with self.worker._lock:
            self.assertIsNone(self.worker._square_cleared_at)
        # While active it records the clear timestamp.
        with self.worker._lock:
            self.worker._active = True
        self.worker.on_lie_seen(None, _fake_frame())
        with self.worker._lock:
            self.assertIsNotNone(self.worker._square_cleared_at)


class EngineGuardTests(unittest.TestCase):
    def test_too_small_square_rejected_before_gpu_load(self):
        worker = AutoLieWorker(queue.Queue(maxsize=1), threading.Event())
        with self.assertRaises(ValueError):
            _LieSequenceEngine(
                worker,
                (10, 10, 3, 3),  # smaller than _MIN_SEED_SIZE
                _fake_frame(),
            )


class EnginePhaseTests(unittest.TestCase):
    """Two-phase logic without GPU: slice bell -> await white box -> seed."""

    @staticmethod
    def _make_engine(width=640, height=480, *, alarm_elapsed=True):
        frames = queue.Queue()
        worker = AutoLieWorker(frames, threading.Event(), enabled=True)
        engine = _LieSequenceEngine(
            worker,
            (10, 10, 30, 30),  # the #c9ced0 alarm slice
            _fake_frame(width, height, sequence=0),
        )
        if alarm_elapsed:  # pretend the 4s takeover delay has already run
            engine._started_at = time.monotonic() - TARGET_TAKEOVER_SECONDS
        return engine, frames

    @staticmethod
    def _push(frames, count, *, white=False, sequence_start=1):
        from PIL import Image, ImageDraw

        for index in range(count):
            image = Image.new("RGB", (640, 480), (0, 0, 0))
            if white:
                ImageDraw.Draw(image).rectangle(
                    [290, 210, 350, 270], fill=(255, 255, 255)
                )
            frames.put(SimpleNamespace(
                image=image,
                sequence=sequence_start + index,
                window_rect=(0, 0, 640, 480),
            ))

    def test_waits_for_white_box_then_seeds_it(self):
        engine, frames = self._make_engine()
        seen = []

        def fake_seed(frame, bgr, box):
            seen.append(box)
            engine._phase = "tracking"

        engine._start_tracking = fake_seed
        self._push(frames, 12, white=False, sequence_start=1)   # pre-box frames
        self._push(frames, 8, white=True, sequence_start=13)    # box visible
        for _ in range(60):
            if engine._phase == "tracking" or engine._ended:
                break
            engine.step_once()
        self.assertEqual(engine._phase, "tracking")
        self.assertEqual(len(seen), 1)
        # Boxes now live in lie-popup crop coordinates: the 640x480 client
        # crop is (140, 100, 359, 280), so the centred 60x60 white square at
        # (320, 240) of the client sits at (180, 140) of the crop.
        popup_left, popup_top, popup_width, popup_height = lie_window_box(640, 480)
        left, top, width, height = seen[0]
        self.assertAlmostEqual(left + width / 2.0, 320 - popup_left, delta=15)
        self.assertAlmostEqual(top + height / 2.0, 240 - popup_top, delta=15)
        self.assertGreaterEqual(left, 0)
        self.assertGreaterEqual(top, 0)
        self.assertLessEqual(left + width, popup_width)
        self.assertLessEqual(top + height, popup_height)
        engine.end("test complete")

    def test_white_outside_the_popup_is_never_seeded(self):
        engine, frames = self._make_engine()
        seen = []
        engine._start_tracking = lambda frame, bgr, box: seen.append(box)
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (640, 480), (0, 0, 0))
        # Big white square far outside the popup crop (popup starts at x=140).
        ImageDraw.Draw(image).rectangle([5, 400, 130, 470], fill=(255, 255, 255))
        frames.put(SimpleNamespace(
            image=image, sequence=1, window_rect=(0, 0, 640, 480),
        ))
        for _ in range(8):
            engine.step_once()
        self.assertEqual(seen, [])
        self.assertEqual(engine._phase, "await-target")
        engine.end("test complete")

    def test_gives_up_when_no_box_appears(self):
        engine, frames = self._make_engine()
        engine._start_tracking = lambda frame, bgr, box: None
        self._push(frames, 3, white=False, sequence_start=1)
        # Pretend the alarm delay and its grace already expired, then feed one
        # more frame so the await step evaluates the deadline.
        engine._started_at = time.monotonic() - (
            TARGET_TAKEOVER_SECONDS + AWAIT_TARGET_SECONDS + 1.0
        )
        engine.step_once()
        self.assertTrue(engine._ended)
        self.assertEqual(
            engine._end_reason,
            "no white target box appeared after the takeover delay",
        )
        self.assertIsNone(engine._tracker)


class AlarmTimingTests(unittest.TestCase):
    """The #c9ced0 slice is only the alarm; Cutie takes over 4s later."""

    @staticmethod
    def _engine(width=640, height=480):
        frames = queue.Queue()
        worker = AutoLieWorker(frames, threading.Event(), enabled=True)
        return _LieSequenceEngine(
            worker,
            (10, 10, 30, 30),
            _fake_frame(width, height, sequence=0),
        )

    def test_nothing_is_seeded_during_the_alarm_delay(self):
        engine = self._engine()
        seen = []
        engine._start_tracking = lambda frame, bgr, box: seen.append(box)
        # A huge white target is on screen the whole time; it must be ignored
        # until the takeover delay has passed.
        image = Image.new("RGB", (640, 480), (0, 0, 0))
        from PIL import ImageDraw

        ImageDraw.Draw(image).rectangle([230, 190, 290, 250], fill=(255, 255, 255))
        frame = SimpleNamespace(image=image, sequence=1, window_rect=(0, 0, 640, 480))
        bgr = _bgr_from_pil(image)
        for step in range(10):
            engine._await_target_step(frame, bgr)
        self.assertEqual(seen, [], "no seed before the takeover time")
        self.assertEqual(engine._phase, "await-target")

    def test_takeover_delay_seeds_the_target(self):
        engine = self._engine()
        seen = []
        engine._start_tracking = lambda frame, bgr, box: seen.append(box)
        engine._started_at = time.monotonic() - TARGET_TAKEOVER_SECONDS
        image = Image.new("RGB", (640, 480), (0, 0, 0))
        from PIL import ImageDraw

        ImageDraw.Draw(image).rectangle([230, 190, 290, 250], fill=(255, 255, 255))
        frame = SimpleNamespace(image=image, sequence=1, window_rect=(0, 0, 640, 480))
        bgr = _bgr_from_pil(image)
        for _ in range(_CANDIDATE_STABLE_FRAMES):
            engine._await_target_step(frame, bgr)
        self.assertEqual(len(seen), 1)
        self.assertIsNone(engine._end_reason)

    def test_alarm_clear_never_stops_the_sequence(self):
        engine = self._engine()
        worker = engine.owner
        with worker._lock:
            worker._active = True
        # The live detector reports the slice gone right after the popup opens.
        worker.on_lie_seen(None, _fake_frame(640, 480, sequence=1))
        self.assertIsNotNone(worker._square_cleared_at)
        self.assertFalse(engine._is_stop_requested())
        # ... and resetting the checkbox still does.
        worker.set_enabled(False)
        self.assertTrue(engine._is_stop_requested())


class NoveltyBellStateTests(unittest.TestCase):
    def test_stable_sightings_accumulate(self):
        state = _novelty_next_state((10, 10, 40, 40), None, 100.0)
        self.assertEqual(state[1], 1)
        state = _novelty_next_state((10, 10, 40, 40), state, 100.5)
        self.assertEqual(state[1], 2)
        state = _novelty_next_state((10, 10, 40, 40), state, 101.0)
        self.assertEqual(state[1], 3)

    def test_moved_box_restarts_counter(self):
        state = _novelty_next_state((10, 10, 40, 40), None, 100.0)
        state = _novelty_next_state((10, 10, 40, 40), state, 100.5)
        state = _novelty_next_state((220, 60, 40, 40), state, 101.0)
        self.assertEqual(state[1], 1)

    def test_gap_longer_than_forget_window_restarts(self):
        state = _novelty_next_state((10, 10, 40, 40), None, 100.0)
        state = _novelty_next_state((10, 10, 40, 40), state, 104.0)
        self.assertEqual(state[1], 1)

    def test_no_box_clears_state(self):
        self.assertIsNone(_novelty_next_state(None, None, 1.0))
        self.assertIsNone(
            _novelty_next_state(None, ((10, 10, 40, 40), 2, 0.5), 1.0)
        )


if __name__ == "__main__":
    unittest.main()
