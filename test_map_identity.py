import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from map_identity import MapIdentityStore


class MapIdentityStoreTests(unittest.TestCase):
    @staticmethod
    def title(text: str) -> Image.Image:
        image = np.zeros((50, 220, 3), dtype=np.uint8)
        cv2.putText(image, text, (8, 34), cv2.FONT_HERSHEY_SIMPLEX,
                    0.8, (240, 240, 240), 2, cv2.LINE_AA)
        return Image.fromarray(image, "RGB")

    def test_recorded_name_matches_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = MapIdentityStore(root)
            store.record("Training I", self.title("TRAINING I"))
            restored = MapIdentityStore(root)
            matched, score = restored.matches(
                "Training I", self.title("TRAINING I")
            )
            self.assertTrue(matched)
            self.assertGreater(score, .9)
            self.assertTrue(restored.index_path.is_file())

    def test_different_map_title_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MapIdentityStore(Path(directory))
            store.record("Map A", self.title("MAP A"))
            matched, _score = store.matches("Map A", self.title("OTHER"))
            self.assertFalse(matched)

    def test_removing_one_map_preserves_other_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MapIdentityStore(Path(directory))
            store.record("Map A", self.title("MAP A"))
            store.record("Map B", self.title("MAP B"))
            store.remove("Map A")
            self.assertFalse(store.has_reference("Map A"))
            self.assertTrue(store.has_reference("Map B"))


if __name__ == "__main__":
    unittest.main()
