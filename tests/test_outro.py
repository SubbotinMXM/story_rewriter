"""Аутро: пустой путь → skip; выбор файла из папки."""

from __future__ import annotations

import random
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from compositor.picker import pick_outro
from compositor.pipeline import _maybe_append_outro


class OutroPickTests(unittest.TestCase):
    def test_pick_outro_none_when_unset(self) -> None:
        self.assertIsNone(pick_outro(None))

    def test_pick_outro_none_when_missing_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(pick_outro(Path(td) / "nope"))

    def test_pick_outro_none_when_empty_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "readme.txt").write_text("x")
            self.assertIsNone(pick_outro(d))

    def test_pick_outro_chooses_video(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            a = d / "a.mp4"
            b = d / "b.mov"
            a.write_bytes(b"x")
            b.write_bytes(b"y")
            (d / "ignore.txt").write_text("no")
            picked = pick_outro(d, rng=random.Random(0))
            self.assertIn(picked, {a, b})

    def test_maybe_append_skips_when_outro_unset(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            main = Path(td) / "main.mp4"
            main.write_bytes(b"fake")
            before = main.read_bytes()
            self.assertIsNone(
                _maybe_append_outro(main, None, rng=random.Random(0), on_progress=None)
            )
            self.assertEqual(main.read_bytes(), before)

    def test_maybe_append_skips_empty_folder(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            main = root / "main.mp4"
            main.write_bytes(b"fake")
            empty = root / "empty"
            empty.mkdir()
            msgs: list[str] = []
            self.assertIsNone(
                _maybe_append_outro(
                    main,
                    empty,
                    rng=random.Random(0),
                    on_progress=lambda _p, m: msgs.append(m),
                )
            )
            self.assertTrue(any("пропускаю" in m for m in msgs))


if __name__ == "__main__":
    unittest.main()
