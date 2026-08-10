"""Тесты: хук в мастер-промпт, секции плана, smoke FullRunRequest."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rewriter.full_pipeline import FullRunRequest
from rewriter.hook_story import (
    HOOK_PLACEHOLDER,
    characters_bible_from_plan,
    extract_plan_section,
    fill_hook_prompt,
    load_master_prompt,
    writing_instruction_from_plan,
)


class HookPromptTests(unittest.TestCase):
    def test_fill_prompt_injects_hook(self) -> None:
        hook = (
            "«Сними каску, любовница сына будет инженером!» "
            "— сказала свекровь на стройке, но чертежи были мои."
        )
        filled = fill_hook_prompt(load_master_prompt(), hook)
        self.assertIn(hook, filled)
        self.assertNotIn(HOOK_PLACEHOLDER, filled)
        self.assertIn("ИСХОДНЫЙ ХУК", filled)
        self.assertIn("Не пиши сам рассказ", filled)

    def test_fill_prompt_rejects_empty(self) -> None:
        with self.assertRaises(ValueError):
            fill_hook_prompt(load_master_prompt(), "  ")

    def test_extract_plan_section_and_writing_instr(self) -> None:
        plan = (
            "1. ОТРЕДАКТИРОВАННЫЙ ХУК\nХук текст\n\n"
            "2. ОСНОВА ИСТОРИИ\nГероиня — инженер\n\n"
            "4. СПИСОК ПЕРСОНАЖЕЙ\nАнна, 52\nСын Павел\n\n"
            "14. ИНСТРУКЦИЯ ДЛЯ НАПИСАНИЯ РАССКАЗА\n"
            "Пиши от третьего лица, 8000–10000 слов.\n"
        )
        self.assertIn("Анна", extract_plan_section(plan, 4))
        instr = writing_instruction_from_plan(plan)
        self.assertIn("третьего лица", instr)
        bible = characters_bible_from_plan(plan)
        self.assertIn("Анна", bible)
        self.assertIn("инженер", bible)


class FullPipelineHookSmokeTests(unittest.TestCase):
    def test_full_run_request_hook_mode(self) -> None:
        req = FullRunRequest(
            source_text="",
            prefix="",
            overlay_text="тест",
            gpt_api_key="k",
            gpt_base_url="https://example.com/v1",
            gpt_model="gpt-test",
            lumean_api_key="l",
            template_id="t",
            voice_id="v",
            broll_dir=Path("/tmp"),
            head_dir=Path("/tmp"),
            subscribe=False,
            story_mode="hook",
            hook="Хук-фраза для теста",
        )
        self.assertEqual(req.story_mode, "hook")
        self.assertEqual(req.hook, "Хук-фраза для теста")


class ImportSmokeTests(unittest.TestCase):
    def test_imports(self) -> None:
        import rewriter.hook_story  # noqa: F401
        import rewriter.full_pipeline  # noqa: F401
        import rewriter.ui  # noqa: F401


if __name__ == "__main__":
    unittest.main()
