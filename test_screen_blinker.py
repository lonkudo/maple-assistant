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

    def test_layer_band_overlay_maps_normalized_y_into_minimap(self):
        blinker = ScreenBlinker(threading.Event())
        rendered = threading.Event()
        received = []

        def render(bands):
            received.extend(bands)
            rendered.set()

        blinker._show_layer_band_regions = render
        blinker.show_layer_bands(
            (100, 200, 1100, 700),
            (500, 250),
            (10, 20, 110, 120),
            (
                ("layer2", (.30, .50)),
                ("layer3", (.20, .32)),
            ),
        )

        self.assertTrue(rendered.wait(.5))
        self.assertEqual(received[0][0], "layer2")
        self.assertEqual(received[0][1], (120, 300, 320, 340))
        self.assertEqual(received[1][0], "layer3")
        self.assertEqual(received[1][1], (120, 280, 320, 304))
        # Each layer gets a real vertical gradient, not one flat colour.
        self.assertNotEqual(received[0][2], received[0][3])

    def test_layer_band_overlay_can_wait_until_hidden(self):
        blinker = ScreenBlinker(threading.Event())
        rendered = []
        blinker._show_layer_band_regions = rendered.append

        blinker.show_layer_bands(
            (0, 0, 500, 250),
            (500, 250),
            (10, 20, 110, 120),
            (("layer1", (.4, .6)),),
            wait_until_hidden=True,
        )

        self.assertEqual(len(rendered), 1)
        self.assertEqual(rendered[0][0][0], "layer1")


if __name__ == "__main__":
    unittest.main()
