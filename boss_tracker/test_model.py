from pathlib import Path
import tempfile
import unittest

from model import BossTrackerModel


class FakeClock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class BossTrackerModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config.json"
        self.clock = FakeClock()
        self.model = BossTrackerModel(self.path, clock=self.clock)

    def test_channels_have_independent_deadlines(self) -> None:
        first = self.model.add_channel("1")
        self.clock.value += 600
        second = self.model.add_channel("2")
        rows = {row["id"]: row for row in self.model.channel_status()}
        self.assertAlmostEqual(rows[first]["remaining"], 3000)
        self.assertAlmostEqual(rows[second]["remaining"], 3600)

    def test_expired_channel_resets_and_reports_once(self) -> None:
        self.model.set_interval_minutes(0.6)
        self.model.add_channel("3")
        self.clock.value += 36
        self.assertEqual(self.model.advance_expired(), ["3"])
        self.assertAlmostEqual(
            self.model.channel_status()[0]["remaining"], 36
        )
        self.assertEqual(self.model.advance_expired(), [])

    def test_interval_change_resets_all_channels(self) -> None:
        self.model.add_channel("1")
        self.clock.value += 100
        self.model.add_channel("2")
        self.model.set_interval_minutes(120)
        self.assertEqual(
            [row["remaining"] for row in self.model.channel_status()],
            [7200, 7200],
        )

    def test_channel_reset_and_delete_are_scoped(self) -> None:
        first = self.model.add_channel("1")
        second = self.model.add_channel("2")
        self.clock.value += 100
        self.assertTrue(self.model.reset_channel(first))
        rows = {row["id"]: row for row in self.model.channel_status()}
        self.assertAlmostEqual(rows[first]["remaining"], 3600)
        self.assertAlmostEqual(rows[second]["remaining"], 3500)
        self.assertTrue(self.model.delete_channel(first))
        self.assertEqual([row["id"] for row in self.model.channel_status()], [second])

    def test_channel_remaining_can_be_dragged_independently(self) -> None:
        first = self.model.add_channel("1")
        second = self.model.add_channel("2")
        self.assertTrue(self.model.set_channel_remaining(first, 900))
        rows = {row["id"]: row for row in self.model.channel_status()}
        self.assertAlmostEqual(rows[first]["remaining"], 900)
        self.assertAlmostEqual(rows[second]["remaining"], 3600)
        self.assertTrue(self.model.set_channel_remaining(first, 99999))
        self.assertAlmostEqual(
            self.model.channel_status()[0]["remaining"], 3600
        )

    def test_channel_number_must_be_unique_integer_from_1_to_60(self) -> None:
        self.model.add_channel("60")
        for invalid in ("", "0", "01", "61", "1.5", "abc", "60"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    self.model.add_channel(invalid)

    def test_statistics_persist_and_never_go_negative(self) -> None:
        self.assertEqual(self.model.change_boss_kills(1), 1)
        self.assertEqual(self.model.change_boss_kills(-5), 0)
        item_id = self.model.add_custom_stat("稀有掉落")
        self.assertEqual(self.model.change_custom_stat(item_id, 3), 3)
        self.assertTrue(self.model.rename_custom_stat(item_id, "核心"))

        restored = BossTrackerModel(self.path, clock=self.clock)
        stats = restored.snapshot()["statistics"]
        self.assertEqual(stats["boss_kills"], 0)
        self.assertEqual(stats["custom"][0]["name"], "核心")
        self.assertEqual(stats["custom"][0]["count"], 3)

    def test_clear_all_data_keeps_universal_settings(self) -> None:
        self.model.set_interval_minutes(150)
        self.model.add_channel("1")
        self.model.change_boss_kills(4)
        self.model.add_custom_stat("核心")
        self.model.clear_all_data()

        data = self.model.snapshot()
        self.assertEqual(data["universal_interval_minutes"], 150)
        self.assertEqual(data["channels"], [])
        self.assertEqual(
            data["statistics"], {"boss_kills": 0, "custom": []}
        )

    def test_malformed_configuration_recovers(self) -> None:
        self.path.write_text("not json", encoding="utf-8")
        recovered = BossTrackerModel(self.path, clock=self.clock)
        recovered.add_channel("1")
        self.assertEqual(recovered.snapshot()["channels"][0]["name"], "1")
        self.assertIn("channels", self.path.read_text(encoding="utf-8"))

    def test_legacy_hour_configuration_migrates_to_minutes(self) -> None:
        self.path.write_text(
            '{"universal_interval_hours": 1.5, "channels": []}',
            encoding="utf-8",
        )
        migrated = BossTrackerModel(self.path, clock=self.clock)
        self.assertEqual(
            migrated.snapshot()["universal_interval_minutes"], 90
        )
        self.assertEqual(migrated.interval_seconds, 5400)


if __name__ == "__main__":
    unittest.main()
