"""Тесты: слоты профессии, парсер мета, smoke FullRunRequest."""

from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rewriter.full_pipeline import FullRunRequest
from rewriter.profession_story import (
    TARGET_MIN_WORDS,
    fill_master_prompt,
    load_master_prompt,
    parse_meta_block,
    parse_story_and_meta,
    pick_slots,
)


class ProfessionSlotsTests(unittest.TestCase):
    def test_pick_slots_fills_all(self) -> None:
        slots = pick_slots("медсестра", rng=random.Random(42))
        self.assertEqual(slots.profession, "медсестра")
        self.assertTrue(slots.name)
        self.assertGreaterEqual(slots.age, 45)
        self.assertLessEqual(slots.age, 65)
        for field in (
            "place",
            "dream",
            "idealized",
            "undervalued",
            "conflict",
            "incident",
            "antagonist",
            "ending",
        ):
            self.assertTrue(getattr(slots, field), msg=field)

    def test_pick_slots_rejects_empty_profession(self) -> None:
        with self.assertRaises(ValueError):
            pick_slots("  ", rng=random.Random(0))

    def test_fill_prompt_injects_profession_and_length(self) -> None:
        slots = pick_slots("библиотекарь", rng=random.Random(1))
        filled = fill_master_prompt(load_master_prompt(), slots)
        self.assertIn("библиотекарь", filled)
        self.assertIn(slots.name, filled)
        self.assertIn(f"{TARGET_MIN_WORDS}–", filled)
        self.assertIn("## ИСХОДНЫЕ ДАННЫЕ", filled)
        self.assertNotIn("[имя, возраст, профессия]", filled)


class MetaParserTests(unittest.TestCase):
    def test_parse_story_and_meta_split(self) -> None:
        raw = (
            "Ребёнка нашли у Марины, а директор уже вызвал полицию.\n\n"
            "Дальше долгий рассказ про больницу и ложное обвинение.\n\n"
            "1. Пять кликбейтных названий\n"
            "• Чужой ингалятор\n"
            "• Ночной шкафчик\n"
            "• Её обвинили\n"
            "• След в палате\n"
            "• Не та смена\n\n"
            "2. Пять заголовков для YouTube\n"
            "• Медсестру обвинили в пропаже ребёнка — улика в шкафчике\n"
            "• В больнице пропал мальчик, а камера показала её\n"
            "• Коллеги отвернулись, когда нашли ингалятор\n"
            "• Она верила начальнику — и почти потеряла всё\n"
            "• Правда вышла только после третьей смены\n\n"
            "3. Краткое эмоциональное описание ролика\n"
            "История о женщине, которую подставили на работе, "
            "и о цене слепой веры в «нужного» человека. "
            "Напряжение растёт с каждой уликой.\n\n"
            "4. Пять вариантов текста для превью\n"
            "• Её подставили\n"
            "• Улика в шкафу\n"
            "• Ребёнок пропал\n"
            "• Не верьте ей\n"
            "• Ночная смена\n\n"
            "5. Краткое содержание рассказа\n"
            "Медсестру обвиняют в исчезновении ребёнка, "
            "но настоящий виновник ближе, чем кажется.\n\n"
            "6. Список главных сюжетных поворотов\n"
            "• Находка ингалятора\n"
            "• Ложное алиби коллеги\n"
            "• Разоблачение антагониста\n"
        )
        story, meta = parse_story_and_meta(raw)
        self.assertIn("Ребёнка нашли", story)
        self.assertNotIn("кликбейтных", story)
        self.assertEqual(len(meta.titles), 5)
        self.assertEqual(len(meta.yt_titles), 5)
        self.assertGreater(len(meta.description), 40)
        self.assertEqual(len(meta.preview_phrases), 5)
        self.assertIn("Медсестру", meta.summary)
        self.assertGreaterEqual(len(meta.plot_turns), 3)

    def test_parse_meta_block_numbered(self) -> None:
        raw = (
            "1. Названия\nА\nБ\nВ\nГ\nД\n"
            "2. YouTube\nY1\nY2\nY3\nY4\nY5\n"
            "3. Описание\nДлинное описание ролика для карточки.\n"
            "4. Превью\nРаз два три\nЧетыре слова тут\n"
            "5. Содержание\nКратко о сюжете.\n"
            "6. Повороты\nПоворот один\nПоворот два\n"
        )
        meta = parse_meta_block(raw)
        self.assertEqual(meta.titles[:2], ["А", "Б"])
        self.assertEqual(meta.yt_titles[0], "Y1")
        self.assertIn("Длинное описание", meta.description)
        self.assertIn("Раз два три", meta.preview_phrases)
        self.assertIn("Кратко", meta.summary)
        self.assertIn("Поворот один", meta.plot_turns)


class FullPipelineSmokeTests(unittest.TestCase):
    def test_full_run_request_mode2_fields(self) -> None:
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
            story_mode="profession",
            profession="санитарка",
        )
        self.assertEqual(req.story_mode, "profession")
        self.assertEqual(req.profession, "санитарка")

    def test_full_run_request_hook_field(self) -> None:
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
            hook="Тестовый хук",
        )
        self.assertEqual(req.story_mode, "hook")
        self.assertEqual(req.hook, "Тестовый хук")


if __name__ == "__main__":
    unittest.main()
