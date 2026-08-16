"""Tests for the cross-process input-control mutex."""

import unittest

from input_mutex import InputControlMutex, MUTEX_NAME


class InputControlMutexTests(unittest.TestCase):
    def test_acquire_and_release(self):
        mutex = InputControlMutex(MUTEX_NAME + ".test1")
        try:
            self.assertFalse(mutex.held)
            self.assertTrue(mutex.try_acquire(0))
            self.assertTrue(mutex.held)
            mutex.release()
            self.assertFalse(mutex.held)
        finally:
            mutex.close()

    def test_second_thread_excluded_while_held(self):
        # Two SEPARATE mutex objects bound to the same name (like two
        # processes): while the main thread holds it, another thread cannot
        # acquire - the cross-process exclusion model.
        import threading
        import time

        name = MUTEX_NAME + ".test2"
        holder = InputControlMutex(name)
        contender_obj = InputControlMutex(name)
        result = {}

        def contender():
            ok = contender_obj.try_acquire(200)
            result["acquired"] = ok
            if ok:
                contender_obj.release()

        try:
            self.assertTrue(holder.try_acquire(0))
            thread = threading.Thread(target=contender)
            thread.start()
            time.sleep(0.05)
            # The contender must time out while we hold it.
            self.assertTrue(holder.held)
            self.assertFalse(result.get("acquired", False))
            holder.release()
            thread.join(1)
            # After release the contender's wait (200ms) succeeds.
            self.assertTrue(result.get("acquired", False))
        finally:
            holder.close()
            contender_obj.close()

    def test_acquire_with_timeout_wins_after_release(self):
        first = InputControlMutex(MUTEX_NAME + ".test3")
        second = InputControlMutex(MUTEX_NAME + ".test3")
        try:
            self.assertTrue(first.try_acquire(0))
            # Same-thread recursive acquisition succeeds (Windows mutexes
            # are per-thread), so exclusion is verified across threads;
            # here we only verify the same handle releases cleanly.
            first.release()
            # After release, a timed wait succeeds promptly.
            self.assertTrue(second.try_acquire(1000))
            second.release()
        finally:
            first.close()
            second.close()

    def test_context_manager(self):
        mutex = InputControlMutex(MUTEX_NAME + ".test4")
        try:
            with mutex:
                self.assertTrue(mutex.held)
            self.assertFalse(mutex.held)
        finally:
            mutex.close()


if __name__ == "__main__":
    unittest.main()
