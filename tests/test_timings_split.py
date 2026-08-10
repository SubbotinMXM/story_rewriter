"""Тесты strip timings / split."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rewriter.pipeline import build_final_text
from rewriter.split import split_into_parts
from rewriter.timings import strip_timings

SAMPLE = """0:1414 секундчто это ошибка. Муж погиб год назад. Но дежурная назвала его полное имя и добавила, что ячейку оплачивали до
0:2222 секундывчерашнего дня. А когда Анна открыла дверцу камеры, у неё подкосились ноги.
0:2828 секундПрежде чем начнём, напишите, откуда вы нас смотрите. Желаем вам приятного прослушивания.
0:3535 секундАнна стояла у кухонного окна и смотрела, как ветер гоняет по двору сухие листья.
0:4141 секундаЧайник на плите уже давно вскипел и щёлкнул, но она не двинулась с места"""


class TimingsSplitTests(unittest.TestCase):
    def test_strip_keeps_anna(self) -> None:
        cleaned = strip_timings(SAMPLE)
        self.assertNotIn("секунд", cleaned)
        self.assertIn("Анна стояла", cleaned)
        self.assertIn("Чайник", cleaned)

    def test_split_four(self) -> None:
        parts = split_into_parts(strip_timings(SAMPLE), 4)
        self.assertEqual(len(parts), 4)
        self.assertTrue(all(parts))

    def test_join(self) -> None:
        self.assertEqual(build_final_text("P", ["A", "B"]), "PA\nB")


if __name__ == "__main__":
    unittest.main()
