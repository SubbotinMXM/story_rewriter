"""Тесты чекпоинтов resume."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rewriter import checkpoint as cp


class CheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmpdir.name)
        self.patches = [
            mock.patch.object(cp, "RUN_DIR", self.dir),
            mock.patch.object(cp, "STATE_PATH", self.dir / "state.json"),
            mock.patch.object(cp, "TEXT_PATH", self.dir / "story.txt"),
            mock.patch.object(cp, "AUDIO_PATH", self.dir / "voice.mp3"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self._tmpdir.cleanup()

    def test_resume_after_rewrite(self) -> None:
        cp.mark_rewrite_done(
            text="hello story",
            source_hash="aaa",
            prefix_hash="bbb",
        )
        chk = cp.load_checkpoint()
        assert chk is not None
        self.assertEqual(chk.stage, "rewrite_done")
        self.assertTrue(chk.can_resume)
        self.assertEqual(chk.resume_button_label, "Продолжить с озвучки")
        self.assertEqual(cp.read_checkpoint_text(), "hello story")

    def test_resume_after_tts(self) -> None:
        cp.mark_rewrite_done(text="t", source_hash="a", prefix_hash="b")
        cp.AUDIO_PATH.write_bytes(b"ID3fake")
        cp.mark_tts_done(audio_bytes=7)
        chk = cp.load_checkpoint()
        assert chk is not None
        self.assertEqual(chk.stage, "tts_done")
        self.assertEqual(chk.resume_button_label, "Продолжить со сборки видео")

    def test_clear(self) -> None:
        cp.mark_rewrite_done(text="t", source_hash="a", prefix_hash="b")
        cp.clear_checkpoint()
        self.assertIsNone(cp.load_checkpoint())


if __name__ == "__main__":
    unittest.main()
