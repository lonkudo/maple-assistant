import unittest

from audio import (
    channel_announcement,
    number_to_chinese,
    select_female_chinese_voice,
)


class FakeVoice:
    def __init__(self, language: str, gender: str) -> None:
        self.values = {"Language": language, "Gender": gender}

    def GetAttribute(self, name: str) -> str:
        return self.values[name]


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

    def test_female_chinese_voice_is_preferred(self) -> None:
        english_female = FakeVoice("409", "Female")
        chinese_male = FakeVoice("804", "Male")
        chinese_female = FakeVoice("804", "Female")
        self.assertIs(
            select_female_chinese_voice(
                [english_female, chinese_male, chinese_female]
            ),
            chinese_female,
        )


if __name__ == "__main__":
    unittest.main()
