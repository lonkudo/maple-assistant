from datetime import datetime
import threading
import time
import unittest

from telegram_notifier import TelegramNotifier, format_alert_message


class TelegramNotifierTests(unittest.TestCase):
    def test_message_contains_machine_event_and_time(self) -> None:
        message = format_alert_message(
            "电脑A", "掉线警报", datetime(2026, 8, 30, 12, 34, 56)
        )
        self.assertEqual(message, "电脑A 掉线警报 时间 2026-08-30 12:34:56")

    def test_learns_latest_chat_and_sends_non_blocking_alert(self) -> None:
        calls = []

        def api(method, params):
            calls.append((method, params))
            if method == "getMe":
                return {"ok": True, "result": {"username": "maple_bot"}}
            if method == "getUpdates":
                return {"ok": True, "result": [
                    {"message": {"chat": {"id": 12345}}}
                ]}
            return {"ok": True, "result": {}}

        stop = threading.Event()
        worker = TelegramNotifier(stop, api_request=api)
        worker.configure("token", machine_name="电脑A")
        worker.set_enabled(True)
        worker.start()
        try:
            worker.notify("测谎报警")
            deadline = time.monotonic() + 1.0
            while not any(call[0] == "sendMessage" for call in calls) \
                    and time.monotonic() < deadline:
                time.sleep(0.01)
            sent = next(params for method, params in calls
                        if method == "sendMessage")
            self.assertEqual(sent["chat_id"], "12345")
            self.assertIn("电脑A 测谎报警 时间 ", sent["text"])
            self.assertTrue(worker.is_alive())
        finally:
            stop.set()
            worker.join(1.0)

    def test_bad_token_only_updates_status_and_worker_survives(self) -> None:
        def broken(_method, _params):
            raise OSError("Unauthorized")

        stop = threading.Event()
        worker = TelegramNotifier(stop, api_request=broken)
        worker.configure("wrong-token", machine_name="电脑B")
        worker.set_enabled(True)
        worker.start()
        try:
            deadline = time.monotonic() + 1.0
            while "失败" not in worker.snapshot()["status"] \
                    and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIn("失败", worker.snapshot()["status"])
            self.assertTrue(worker.is_alive())
            worker.notify("掉线警报")
            time.sleep(0.05)
            self.assertTrue(worker.is_alive())
        finally:
            stop.set()
            worker.join(1.0)


if __name__ == "__main__":
    unittest.main()
