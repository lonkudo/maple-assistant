from pathlib import Path
import tempfile
import unittest

from versioning import next_version, parse_version, read_version, version_label


class VersioningTests(unittest.TestCase):
    def test_missing_version_starts_at_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "VERSION"
            self.assertEqual(next_version(path), "0000")
            self.assertEqual(read_version(path), "0000")
            self.assertEqual(version_label(path), "v0000")

    def test_existing_version_advances_with_zero_padding(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "VERSION"
            path.write_text("0042\n", encoding="ascii")
            self.assertEqual(next_version(path), "0043")

    def test_version_9999_cannot_wrap(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "VERSION"
            path.write_text("9999\n", encoding="ascii")
            with self.assertRaises(OverflowError):
                next_version(path)

    def test_version_requires_exactly_four_digits(self):
        for invalid in ("1", "10000", "v0001", "abcd"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    parse_version(invalid)


if __name__ == "__main__":
    unittest.main()
