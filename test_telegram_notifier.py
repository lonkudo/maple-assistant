from datetime import datetime
import threading
import time
import unittest
from unittest.mock import patch
from urllib import error as url_error

from telegram_notifier import (
    TelegramNotifier,
    _detect_working_proxy,
    _telegram_api_request,
    format_alert_message,
)


class ProxyDetectionTests(unittest.TestCase):
    def test_system_proxy_verified_first(self) -> None:
        with patch(
            "telegram_notifier.request.getproxies",
            return_value={"https": "http://127.0.0.1:7890"},
        ), patch(
            "telegram_notifier._proxy_forwarded",
            side_effect=lambda proxy: proxy == "http://127.0.0.1:7890",
        ):
            self.assertEqual(
                _detect_working_proxy(), "http://127.0.0.1:7890"
            )

    def test_falls_back_to_common_local_ports(self) -> None:
        with patch("telegram_notifier.request.getproxies", return_value={}), patch(
            "telegram_notifier._listening_proxy_ports",
            return_value={7891, 7897},
        ), patch(
            "telegram_notifier._proxy_forwarded",
            side_effect=lambda proxy: proxy == "http://127.0.0.1:7891",
        ):
            self.assertEqual(
                _detect_working_proxy(), "http://127.0.0.1:7891"
            )

    def test_direct_used_when_no_proxy_forwards(self) -> None:
        with patch("telegram_notifier.request.getproxies", return_value={}), patch(
            "telegram_notifier._listening_proxy_ports", return_value=set(),
        ), patch(
            "telegram_notifier._proxy_forwarded",
            side_effect=lambda proxy: proxy is None,
        ):
            self.assertIsNone(_detect_working_proxy())


class ApiRequestProxyTests(unittest.TestCase):
    def test_network_failure_invalidates_and_retries_once(self) -> None:
        calls = {"count": 0}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"ok": true, "result": {}}'

        def fake_open(*_args, **_kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise OSError("proxy refused")
            return FakeResponse()

        with patch("telegram_notifier._open", side_effect=fake_open), patch(
            "telegram_notifier._invalidate_proxy_cache"
        ) as invalidated, patch(
            "telegram_notifier._cached_proxy", return_value=None
        ):
            result = _telegram_api_request("token", "getMe", {})
        self.assertTrue(result["ok"])
        invalidated.assert_called_once()
        self.assertEqual(calls["count"], 2)

    def test_http_error_does_not_invalidate_or_retry(self) -> None:
        def fake_open(*_args, **_kwargs):
            raise url_error.HTTPError(
                "https://api.telegram.org/bottoken/getMe", 401,
                "Unauthorized", None, None,
            )

        with patch("telegram_notifier._open", side_effect=fake_open), patch(
            "telegram_notifier._invalidate_proxy_cache"
        ) as invalidated, patch(
            "telegram_notifier._cached_proxy", return_value=None
        ):
            with self.assertRaises(url_error.HTTPError):
                _telegram_api_request("token", "getMe", {})
        invalidated.assert_not_called()


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
