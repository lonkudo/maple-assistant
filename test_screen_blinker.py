import threading
import time
import unittest

from screen_blinker import ScreenBlinker


class ScreenBlinkerTests(unittest.TestCase):
    def test_only_enabled_alerts_are_queued_and_rendered(self):
        stop = threading.Event()
        blinker = ScreenBlinker(stop, enabled=False)
        rendered = threading.Event()
        blinker._blink_twice = rendered.set
        blinker.start()
        try:
            blinker.request_blink()
            self.assertFalse(rendered.wait(.05))
            blinker.set_enabled(True)
            blinker.request_blink()
            self.assertTrue(rendered.wait(.5))
        finally:
            stop.set()
            blinker._wake_event.set()
            blinker.join(1.0)

    def test_disabling_drops_pending_alerts(self):
        blinker = ScreenBlinker(threading.Event(), enabled=True)
        blinker.request_blink()
        blinker.set_enabled(False)
        self.assertEqual(blinker._pending, 0)

    def test_detection_overlay_converts_client_pixels_and_starts_once(self):
        blinker = ScreenBlinker(threading.Event())
        rendered = threading.Event()
        received = []

        def render(regions):
            received.extend(regions)
            rendered.set()

        blinker._flash_detection_regions = render
        blinker.show_detection_regions(
            (100, 200, 1100, 700), (500, 250),
            (("minimap", (0, 0, 100, 50), 0x0000FF00),),
        )

        self.assertTrue(rendered.wait(.5))
        self.assertEqual(
            received,
            [("minimap", (100, 200, 300, 300), 0x0000FF00)],
        )


if __name__ == "__main__":
    unittest.main()
