"""Тесты: двухшаговая генерация рассказа по хуку."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rewriter.full_pipeline import FullRunRequest
from rewriter.hook_story import (
    TARGET_MIN_WORDS,
    TARGET_TOTAL_WORDS,
    build_plan_prompt,
    build_story_continue_prompt,
    build_story_part_prompt,
    generate_hook_plan,
    generate_hook_story,
    generate_hook_story_text,
    narrative_arc_rules,
    parse_plan_parts,
    part_word_targets,
)


SAMPLE_PLAN = """
1. Хук: муж уходит к молодой — героиня остаётся одна (~8%)
2. Первые дни тишины и унижение от родных (~7%)
3. Неожиданный звонок от старой подруги (~8%)
4. Обнаружение подписанных бумаг на квартиру (~10%)
5. Попытка сына убедить «простить отца» (~9%)
6. Поход к нотариусу и перерая улика (~10%)
7. Ложь любовницы на людях (~9%)
8. Сбор свидетелей и переписки (~10%)
9. Муж возвращается с чемоданом — испытание (~9%)
10. Публичное разоблачение (~10%)
11. Справедливое решение и новая жизнь (~10%)
"""


class HookPromptTests(unittest.TestCase):
    def test_build_plan_prompt_injects_hook(self) -> None:
        hook = "«Кому ты нужна в шестьдесят?» — сказал муж."
        prompt = build_plan_prompt(hook)
        self.assertIn(hook, prompt)
        self.assertIn("9-12", prompt)
        self.assertIn("план", build_plan_prompt("хук").lower())
        self.assertIn("последняя часть", build_plan_prompt("хук").lower())

    def test_build_plan_prompt_rejects_empty(self) -> None:
        with self.assertRaises(ValueError):
            build_plan_prompt("  ")

    def test_build_story_part_prompt_first(self) -> None:
        p = build_story_part_prompt(
            hook="Тестовый хук",
            part_spec="1. Начало (~8%)",
            part_index=1,
            part_total=11,
            target_words=800,
            prev_tail="",
            is_first=True,
            is_last=False,
        )
        self.assertIn("СВЯЗАННЫМ МЕЖДУ СОБОЙ", p)
        self.assertIn("Тестовый хук", p)
        self.assertIn("ЗАВЯЗКА", p)
        self.assertIn("ЗАПРЕЩЕНО", p)

    def test_narrative_arc_middle_vs_final(self) -> None:
        mid = narrative_arc_rules(5, 11)
        fin = narrative_arc_rules(11, 11)
        self.assertIn("ОСЛОЖНЕНИЯ", mid)
        self.assertIn("ЗАПРЕЩЕНО", mid)
        self.assertIn("ФИНАЛ", fin)
        self.assertIn("единственная развязка", fin)

    def test_continue_after_finale_no_second_ending(self) -> None:
        tail = "Она простила сына, но больше не доверяла. Наступило спокойное утро новой жизни."
        p = build_story_continue_prompt(
            plan="план",
            story_so_far="начало\n" + tail,
            words_needed=500,
            has_finale=True,
        )
        self.assertIn("НЕ пиши второй финал", p)


class PlanParsingTests(unittest.TestCase):
    def test_parse_plan_parts_numbered(self) -> None:
        parts = parse_plan_parts(SAMPLE_PLAN)
        self.assertGreaterEqual(len(parts), 9)

    def test_part_word_targets_from_percent(self) -> None:
        parts = parse_plan_parts(SAMPLE_PLAN)
        targets = part_word_targets(parts)
        self.assertEqual(len(targets), len(parts))
        self.assertGreaterEqual(sum(targets), TARGET_MIN_WORDS - 500)
        self.assertLessEqual(sum(targets), TARGET_TOTAL_WORDS + 1500)


class GenerationTests(unittest.TestCase):
    def test_generate_hook_plan_single_call(self) -> None:
        client = MagicMock()
        client.complete.return_value = SAMPLE_PLAN.strip()

        plan = generate_hook_plan(client, hook="Тестовый хук")
        self.assertEqual(client.complete.call_count, 1)
        self.assertEqual(client.complete.call_args.kwargs["label"], "hook-plan")
        self.assertIn("1.", plan)

    def test_generate_hook_story_text_by_parts(self) -> None:
        client = MagicMock()
        parts = parse_plan_parts(SAMPLE_PLAN)
        # чтобы гарантировать достижение TARGET_MIN_WORDS
        client.complete.side_effect = [
            " ".join(f"слово{i}" for i in range(1000)) for _ in parts
        ]

        body, written = generate_hook_story_text(
            client,
            hook="Хук",
            plan=SAMPLE_PLAN,
        )
        self.assertEqual(client.complete.call_count, len(parts))
        labels = [c.kwargs["label"] for c in client.complete.call_args_list]
        self.assertTrue(all(l.startswith("hook-part-") for l in labels))
        self.assertEqual(len(written), len(parts))
        self.assertGreater(word_count(body), TARGET_MIN_WORDS)

    def test_generate_hook_story_text_small_continue(self) -> None:
        client = MagicMock()
        parts = parse_plan_parts(SAMPLE_PLAN)
        short = " ".join(f"слово{i}" for i in range(300))
        cont = " ".join(f"добор{i}" for i in range(1000))
        client.complete.side_effect = [short] * len(parts) + [cont] * 20

        body, _ = generate_hook_story_text(
            client,
            hook="Хук",
            plan=SAMPLE_PLAN,
        )
        cont_labels = [
            c.kwargs["label"]
            for c in client.complete.call_args_list
            if "continue" in c.kwargs["label"]
        ]
        self.assertGreater(len(cont_labels), 0)
        for lbl in cont_labels:
            self.assertIn("hook-story-continue-", lbl)
        # continue-запросы просят не больше 1000 слов
        first_cont = next(
            c.kwargs["user"]
            for c in client.complete.call_args_list
            if "continue" in c.kwargs["label"]
        )
        self.assertIn("1000 слов", first_cont)

    def test_generate_hook_story_end_to_end(self) -> None:
        client = MagicMock()
        parts = parse_plan_parts(SAMPLE_PLAN)
        story_chunks = [
            " ".join(f"слово{i}" for i in range(1000)) for _ in parts
        ]
        meta = (
            "1. Названия\n— A\n2. Заголовки\n— B\n3. Описание\nТекст\n"
            "4. Превью\n— C\n5. Содержание\nD\n6. Повороты\n— E"
        )
        client.complete.side_effect = [SAMPLE_PLAN.strip(), *story_chunks, meta]

        from rewriter import hook_story as hs

        orig = hs.OpenAIRewriter

        class FakeRewriter:
            def __init__(self, **kwargs):
                self.complete = client.complete

            def close(self):
                pass

            def register(self, _cancel):
                pass

        hs.OpenAIRewriter = FakeRewriter
        try:
            result = generate_hook_story(
                hook="Хук для теста",
                api_key="k",
                model="gpt-test",
            )
        finally:
            hs.OpenAIRewriter = orig

        self.assertGreaterEqual(result.word_count, TARGET_MIN_WORDS)
        self.assertTrue(result.plan.strip())
        self.assertGreater(len(result.parts), 1)


def word_count(text: str) -> int:
    from rewriter.profession_story import word_count as wc

    return wc(text)


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


class ImportSmokeTests(unittest.TestCase):
    def test_imports(self) -> None:
        import rewriter.hook_story  # noqa: F401
        import rewriter.full_pipeline  # noqa: F401
        import rewriter.ui  # noqa: F401


if __name__ == "__main__":
    unittest.main()
