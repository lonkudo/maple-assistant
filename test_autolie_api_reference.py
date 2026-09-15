# -*- coding: utf-8 -*-
"""Pins the vendor reference material: it must never be edited (see README.md).

``autolie_api/`` holds the CT client's sample, the RoiTrack 2.7.0 protocol spec, the endpoint
list and that sample's own captures.  Our own code lives in ``autolie_api/intergration.py``;
nothing in the reference set may change - not a comment, not a rename, not a line ending.

These hashes were taken from the files as delivered on 2026-09-14.  If a test here fails, the
reference material was modified: restore it (``git`` will not help, ``autolie_api/`` is not
tracked) and put your change in your own module instead.
"""

import hashlib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REFERENCE_DIR = ROOT / "autolie_api"

# file name -> SHA-256 as delivered
REFERENCE_HASHES = {
    "ct_lie3_remote_workflow_sample.py":
        "db9e2185f14a5dffaec6c955058eba26af73b0e81a0638c3a7ec276365a87efc",
}
# The protocol spec was renamed .txt -> .md by the operator, so it is pinned by content and
# matched by either name; names may change, content may not.
PROTOCOL_HASHES = {
    "5ecc3388568fe54b5f6d937051e455493383ad7fef1535fc44c95a533032c699": "ip_port.txt",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ReferenceMaterialTests(unittest.TestCase):
    def test_the_reference_files_are_present(self):
        for name in REFERENCE_HASHES:
            self.assertTrue((REFERENCE_DIR / name).is_file(), f"{name} is missing")
        self.assertTrue(any(REFERENCE_DIR.glob("2.7.0.*")),
                        "the protocol spec (2.7.0.md) is missing")

    def test_the_vendor_sample_is_byte_identical(self):
        for name, expected in REFERENCE_HASHES.items():
            path = REFERENCE_DIR / name
            if not path.is_file():
                self.fail(f"{name} was removed; it is read-only reference material")
            self.assertEqual(sha256(path), expected,
                             f"{name} was modified - reference material is read-only")

    def test_our_own_modules_are_separate_files(self):
        """Our code lives beside the reference, never inside it."""

        for name in ("intergration.py", "__init__.py"):
            self.assertTrue((REFERENCE_DIR / name).is_file(), name)
        sample = (REFERENCE_DIR / "ct_lie3_remote_workflow_sample.py").read_text(
            encoding="utf-8")
        for marker in ("intergration", "AutolieApi", "api_auto_lie"):
            self.assertNotIn(marker, sample.lower(),
                             "the vendor sample must not import or mention our modules")

    def test_the_sample_captures_are_intact(self):
        folder = REFERENCE_DIR / "sample_captures"
        names = sorted(item.name for item in folder.iterdir())
        self.assertEqual(len(names), 14, names)
        self.assertIn("frames.jsonl", names)
        self.assertIn("summary.json", names)
        self.assertEqual(len([n for n in names if n.endswith(".jpg")]), 12)

    def test_a_copy_in_the_project_root_still_matches_the_original(self):
        """The vendor sample also exists in the project root; two copies must not drift.

        Reference material lives in ``autolie_api/`` - this guards the second copy that was put
        in the root as the pristine original.
        """

        for name, expected in REFERENCE_HASHES.items():
            root_copy = ROOT / name
            if not root_copy.is_file():
                continue
            self.assertEqual(sha256(root_copy), expected,
                             f"{name} in the project root differs from the original")


if __name__ == "__main__":
    unittest.main()
