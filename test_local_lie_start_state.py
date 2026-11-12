"""The local lie service start state machine.

A first start after a package update is slow (the 339 MB vendor folder is being
scanned by Defender, and the service validates its monthly card and does a WS
handshake before it listens).  Two defects made that look like a rejection and
never healed until the app was restarted:

* a timeout was latched as ``card_rejected`` (the panel then says 本地测谎启动失败
  and greys the row) although the service was still booting, and
* the supervisor's next tick spawned a SECOND vendor process on top of the first,
  and the two collide on port 8765 (the vendor binary is a single-instance
  service), so neither ever listened.

These cases pin the states: starting / running / failed-with-exit-code, and the
one-launcher-at-a-time rule.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import local_lie_service as service


class FakeProcess:
    """Stands in for the launcher: alive until ``finish`` is called."""

    def __init__(self, pid: int = 4242) -> None:
        self.pid = int(pid)
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def finish(self, code: int = 1) -> None:
        self.returncode = int(code)


class StartStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ct-service-"))
        (self.root / "启动_后台.bat").write_text("@echo off\n", encoding="utf-8")
        service.reset_for_tests()
        # hint_state()/hint_text() only speak for a machine that SELECTED 本地测谎.
        self.previous_desired = service._desired_active
        service._desired_active = True
        self.probe_states: list[bool] = []
        self.launches = 0
        self.process = FakeProcess()

        def launch(_root):
            self.launches += 1
            return self.process

        def probe():
            if self.probe_states:
                return self.probe_states.pop(0), "http://127.0.0.1:8765 就绪"
            return False, "http://127.0.0.1:8765 未就绪"

        self.patches = [
            mock.patch.object(service, "find_service_root", lambda _r: self.root),
            mock.patch.object(service, "_probe", probe),
            mock.patch.object(service, "_launch", launch),
            # A service process only exists once we launched one; before that the
            # folder is empty, so the stale-instance grace must not fire.
            mock.patch.object(
                service, "_service_pids",
                lambda _r: [4242] if self.launches else [],
            ),
            mock.patch.object(service, "has_online_card", lambda: True),
            mock.patch.object(service, "config_card", lambda _r=None: "CT_IN-TEST-TEST-TEST-TEST"),
            mock.patch.object(service, "normalize_auth_server", lambda _r: False),
            mock.patch.object(service, "normalize_placeholder_ws_key", lambda _r: False),
            mock.patch.object(service, "PROBE_INTERVAL_SECONDS", 0.02),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in reversed(self.patches):
            patch.stop()
        service.reset_for_tests()
        service._desired_active = self.previous_desired

    def test_a_still_booting_service_reads_starting_not_rejected(self) -> None:
        ok, note = service.ensure_started(self.root, wait_seconds=0.05)
        self.assertFalse(ok)
        self.assertIn("正在启动", note)
        self.assertEqual(service._state["state"], "starting")
        self.assertFalse(service._state["card_rejected"])
        self.assertEqual(service.hint_state(), "starting")
        self.assertIn("正在启动", service.hint_text())

    def test_no_second_launcher_while_one_is_booting(self) -> None:
        service.ensure_started(self.root, wait_seconds=0.05)
        self.assertEqual(self.launches, 1)
        service.ensure_started(self.root, wait_seconds=0.05)
        service.ensure_started(self.root, wait_seconds=0.05)
        self.assertEqual(self.launches, 1, "a second vendor instance would collide on 8765")
        self.assertEqual(service._state["state"], "starting")

    def test_a_launcher_that_exits_is_a_failure_with_the_exit_code(self) -> None:
        self.process.finish(3)
        ok, note = service.ensure_started(self.root, wait_seconds=0.05)
        self.assertFalse(ok)
        self.assertIn("代码 3", note)
        self.assertEqual(service._state["state"], "failed")
        self.assertTrue(service._state["card_rejected"])
        self.assertEqual(service.hint_state(), "failed")

    def test_the_service_answering_clears_everything(self) -> None:
        # The entry probe sees nothing, the wait loop then sees the service up.
        self.probe_states = [False, True]
        ok, note = service.ensure_started(self.root, wait_seconds=1.0)
        self.assertTrue(ok, note)
        self.assertEqual(service._state["state"], "running")
        self.assertEqual(service._state["pids"], [4242])
        self.assertFalse(service._state["card_rejected"])
        self.assertIsNone(service._launch_still_booting())
        # ...and a later call sees it already running instead of launching again.
        self.probe_states = [True]
        service.ensure_started(self.root, wait_seconds=0.2)
        self.assertEqual(self.launches, 1)

    def test_a_booting_launcher_is_still_killed_with_the_app(self) -> None:
        # state "starting" has no pid list yet, so the stop path must fall back to
        # the executable path; otherwise the vendor process outlives the app and
        # the next start collides with it.
        service.ensure_started(self.root, wait_seconds=0.05)
        self.assertEqual(service._state["state"], "starting")
        with mock.patch.object(service.os, "kill") as kill:
            stopped = service.stop_started_service()
        self.assertEqual(stopped, 1)
        kill.assert_called_once_with(4242, mock.ANY)


    def test_a_leftover_instance_that_answers_is_adopted_not_duplicated(self) -> None:
        # The instance from the previous app run (or the one the update replaced) is
        # still shutting down / booting.  It must be waited for - never a second
        # vendor process on the same port.
        self.launches = 1            # a process of the folder already exists
        self.probe_states = [False, True]
        ok, note = service.ensure_started(self.root, wait_seconds=1.0)
        self.assertTrue(ok, note)
        self.assertEqual(service._state["state"], "external")
        self.assertFalse(service._state["started_by_app"])
        self.assertEqual(service._state["pids"], [4242])


if __name__ == "__main__":
    unittest.main()
