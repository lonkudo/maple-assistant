import unittest

from audio import channel_announcement, number_to_chinese


class BossTrackerAudioTests(unittest.TestCase):
    def test_chinese_channel_numbers(self) -> None:
        expected = {
            1: "一",
            9: "九",
            10: "十",
            11: "十一",
            20: "二十",
            35: "三十五",
            60: "六十",
        }
        for number, spoken in expected.items():
            with self.subTest(number=number):
                self.assertEqual(number_to_chinese(number), spoken)

    def test_each_expired_channel_is_announced_twice(self) -> None:
        self.assertEqual(
            channel_announcement(["1", "12"]),
            "频道一，频道一，频道十二，频道十二",
        )


if __name__ == "__main__":
    unittest.main()
