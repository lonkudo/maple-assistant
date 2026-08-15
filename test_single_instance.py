import ctypes
import unittest
import uuid

from assistant import (
    _acquire_single_instance_mutex,
    _release_single_instance_mutex,
)


@unittest.skipUnless(hasattr(ctypes, "WinDLL"), "Windows mutex test")
class SingleInstanceTests(unittest.TestCase):
    def test_second_mutex_owner_is_rejected_until_first_releases(self):
        mutex_name = f"Local\\MapleAssistant.Test.{uuid.uuid4()}"
        first = _acquire_single_instance_mutex(mutex_name)
        self.assertIsNotNone(first)
        try:
            self.assertIsNone(_acquire_single_instance_mutex(mutex_name))
        finally:
            _release_single_instance_mutex(first)

        replacement = _acquire_single_instance_mutex(mutex_name)
        self.assertIsNotNone(replacement)
        _release_single_instance_mutex(replacement)


if __name__ == "__main__":
    unittest.main()
