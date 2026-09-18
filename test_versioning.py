from pathlib import Path
import contextlib
import io
import sys
import tempfile
import unittest
from unittest import mock

from versioning import (
    FIRST_VERSION, _main, next_version, parse_version, read_version, version_key,
    version_label,
)


class VersioningTests(unittest.TestCase):
    def test_missing_version_starts_at_the_first_release(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "VERSION"
            self.assertEqual(next_version(path), "1.0.0")
            self.assertEqual(read_version(path), FIRST_VERSION)
            self.assertEqual(version_label(path), "v1.0.0")

    def test_the_last_number_increases_by_default(self):
        # The operator's rule: only the LAST number moves unless he says otherwise.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "VERSION"
            path.write_text("1.0.0\n", encoding="ascii")
            self.assertEqual(next_version(path), "1.0.1")
            self.assertEqual(next_version(path, part="minor"), "1.1.0")
            self.assertEqual(next_version(path, part="major"), "2.0.0")

    def test_patch_ten_sorts_after_patch_nine(self):
        self.assertGreater(version_key("1.0.10"), version_key("1.0.9"))
        self.assertGreater(version_key("2.0.0"), version_key("1.99.99"))
        self.assertEqual(version_key("rubbish"), (0, 0, 0))

    def test_version_must_be_three_numbers(self):
        for invalid in ("1", "10000", "1.0", "1.0.0.0", "v1.0.0", "abcd"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    parse_version(invalid)

    def test_cli_accepts_an_explicit_part_flag(self):
        # release_now.ps1 always names the part it wants; an omitted flag (--patch) once made the
        # whole release fail after the version had already been validated.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "VERSION"
            path.write_text("1.0.0\n", encoding="ascii")
            for flag, expected in (("--patch", "1.0.1"), ("--minor", "1.1.0"),
                                   ("--major", "2.0.0")):
                with self.subTest(flag=flag):
                    out = io.StringIO()
                    with mock.patch.object(sys, "argv",
                                           ["versioning.py", "next", str(path), flag]), \
                            contextlib.redirect_stdout(out):
                        self.assertEqual(_main(), 0)
                    self.assertEqual(out.getvalue().strip(), expected)

    def test_unknown_part_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "VERSION"
            path.write_text("1.0.0\n", encoding="ascii")
            with self.assertRaises(ValueError):
                next_version(path, part="build")


if __name__ == "__main__":
    unittest.main()
