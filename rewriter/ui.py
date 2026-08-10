"""UI: рерайт/видео + отдельная вкладка превью."""

from __future__ import annotations

import threading
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
from PIL import Image
from tkinterdnd2 import DND_FILES, TkinterDnD

from compositor.defaults import AUDIO_EXTS
from rewriter import checkpoint as cp
from rewriter.cancel import CancelToken, CancelledError
from rewriter.clipboard_mac import enable_mac_clipboard
from rewriter.config import load_config, save_config
from rewriter.full_pipeline import FullRunRequest, run_full_pipeline
from rewriter.logutil import LOG_PATH, log, set_log_callback
from rewriter.lumean import DEFAULT_TEMPLATE_ID, DEFAULT_VOICE_ID, LumeanClient
from rewriter.openai_client import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_MODELS,
    PROXY_UNREACHABLE_HINT,
    OpenAIRewriter,
    is_expected_network_error,
)
from rewriter.profession_story import StoryMeta
from rewriter.preview_ui import PreviewUI
from rewriter.thumbnail import (
    DEFAULT_IMAGE_API_KEY,
    DEFAULT_IMAGE_BASE_URL,
    DEFAULT_IMAGE_MODEL,
    PreviewGenerator,
    ping_image_api,
)
from rewriter.thumbnail_presets import (
    default_preset_id,
    delete_user_preset,
    get_preset,
    get_presets,
    save_user_preset,
)
from rewriter.video_pipeline import (
    ComposeParams,
    ThumbnailParams,
    run_video_from_audio,
    run_video_from_text,
)

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_NEW_DRAFT_KEY = "__new__"

TAB_FULL = "Полный пайплайн"
TAB_TEXT = "Ролик по тексту"
TAB_AUDIO = "Ролик по аудио"
TAB_PREVIEW = "Превью"
TAB_PROMPTS = "Промпты"

MODE_REWRITE = "rewrite"
MODE_PROFESSION = "profession"
MODE_HOOK = "hook"
_STORY_MODES = {MODE_REWRITE, MODE_PROFESSION, MODE_HOOK}


def _resolve_model(saved: str | None) -> str:
    if saved and saved.strip() in DEFAULT_MODELS:
        return saved.strip()
    if saved and saved.strip():
        return saved.strip()
    return DEFAULT_MODEL


def _model_menu_values(current: str) -> list[str]:
    values = list(DEFAULT_MODELS)
    cur = (current or "").strip()
    if cur and cur not in values:
        values.insert(0, cur)
    return values


class App(ctk.CTk, TkinterDnD.DnDWrapper):
    def __init__(self) -> None:
        super().__init__()
        self.TkdndVersion = TkinterDnD._require(self)
        self.title("Story -> Video")
        self.geometry("1100x980")
        self.minsize(920, 800)
        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")

        cfg = load_config()
        self.api_key_var = ctk.StringVar(value=str(cfg.get("api_key", "")))
        self.base_url_var = ctk.StringVar(
            value=str(cfg.get("base_url") or DEFAULT_BASE_URL)
        )
        self.model_var = ctk.StringVar(value=_resolve_model(cfg.get("model")))
        self.lumean_key_var = ctk.StringVar(value=str(cfg.get("lumean_api_key", "")))
        self.template_id_var = ctk.StringVar(
            value=str(cfg.get("lumean_template_id") or DEFAULT_TEMPLATE_ID)
        )
        self.voice_id_var = ctk.StringVar(
            value=str(cfg.get("lumean_voice_id") or DEFAULT_VOICE_ID)
        )
        self.broll_var = ctk.StringVar(value=str(cfg.get("broll_dir", "")))
        self.head_var = ctk.StringVar(value=str(cfg.get("head_dir", "")))
        self.outro_var = ctk.StringVar(value=str(cfg.get("outro_dir", "")))
        self.subscribe_var = ctk.BooleanVar(value=bool(cfg.get("subscribe", False)))

        self.image_api_key_var = ctk.StringVar(
            value=str(cfg.get("image_api_key") or DEFAULT_IMAGE_API_KEY)
        )
        self.image_base_url_var = ctk.StringVar(
            value=str(cfg.get("image_base_url") or DEFAULT_IMAGE_BASE_URL)
        )
        self.thumb_image_model_var = ctk.StringVar(
            value=str(cfg.get("thumbnail_image_model") or DEFAULT_IMAGE_MODEL)
        )
        self.thumb_preset_var = ctk.StringVar(
            value=str(cfg.get("thumbnail_preset_id") or default_preset_id())
        )
        saved_count = cfg.get("thumbnail_variant_count")
        count_init = ""
        if saved_count is not None and str(saved_count).strip() in ("1", "2", "3"):
            count_init = str(saved_count).strip()
        self.thumb_count_var = ctk.StringVar(value=count_init)
        self.thumbnail_enabled_var = ctk.BooleanVar(
            value=bool(cfg.get("thumbnail_enabled", False))
        )
        self.thumb_file_var = ctk.StringVar(value="")
        self.from_text_file_var = ctk.StringVar(value="")
        self.from_audio_file_var = ctk.StringVar(value="")
        self.audio_preview_text_var = ctk.StringVar(value="")
        saved_mode = str(cfg.get("story_mode") or MODE_REWRITE).strip()
        if saved_mode not in _STORY_MODES:
            saved_mode = MODE_REWRITE
        self.story_mode_var = ctk.StringVar(value=saved_mode)
        self.profession_var = ctk.StringVar(value=str(cfg.get("profession") or ""))
        self.hook_var = ctk.StringVar(value=str(cfg.get("hook") or ""))

        self.status_var = ctk.StringVar(value="Готов")
        self._busy = False
        self._cancel: CancelToken | None = None
        self._job_gen = 0
        self._log_boxes: list = []
        self._story_for_preview = ""
        self._story_for_text_run = ""
        self._story_for_audio_preview = ""
        self._thumb_frames: list = []
        self._overlay_boxes: list = []
        self._prompt_rows: dict[str, dict] = {}
        self._prompt_expanded: set[str] = {default_preset_id()}
        self._prompt_action_btns: list = []

        self.preview_ui = PreviewUI(
            preset_var=self.thumb_preset_var,
            count_var=self.thumb_count_var,
            on_count_change=self._refresh_generate_btn_text,
            on_preset_change=self._sync_story_input_ui,
        )

        self._build_layout(cfg)
        set_log_callback(self._append_log)
        enable_mac_clipboard(self)
        self._refresh_run_button()
        self._refresh_generate_btn_text()
        self._refresh_all_preset_ui()
        self._update_pipeline_thumb_visibility()
        self._sync_story_input_ui()
        self._sync_story_mode_ui()
        log("Старт UI Story -> Video")

    def _build_layout(self, cfg: dict) -> None:
        root = ctk.CTkFrame(self)
        root.pack(fill="both", expand=True, padx=10, pady=10)

        self.tabs = ctk.CTkTabview(root, command=self._on_tab_changed)
        self.tabs.pack(fill="both", expand=True)
        tab_full = self.tabs.add(TAB_FULL)
        tab_text = self.tabs.add(TAB_TEXT)
        tab_audio = self.tabs.add(TAB_AUDIO)
        tab_thumb = self.tabs.add(TAB_PREVIEW)
        tab_prompts = self.tabs.add(TAB_PROMPTS)

        full_body = ctk.CTkScrollableFrame(tab_full)
        full_body.pack(fill="both", expand=True, padx=4, pady=4)
        self._build_full_form(full_body, cfg)

        text_body = ctk.CTkScrollableFrame(tab_text)
        text_body.pack(fill="both", expand=True, padx=4, pady=4)
        self._build_from_text_tab(text_body, cfg)

        audio_body = ctk.CTkScrollableFrame(tab_audio)
        audio_body.pack(fill="both", expand=True, padx=4, pady=4)
        self._build_from_audio_tab(audio_body, cfg)

        thumb_body = ctk.CTkScrollableFrame(tab_thumb)
        thumb_body.pack(fill="both", expand=True, padx=4, pady=4)
        self._build_thumb_tab(thumb_body)

        prompts_body = ctk.CTkFrame(tab_prompts, fg_color="transparent")
        prompts_body.pack(fill="both", expand=True, padx=4, pady=4)
        self._build_prompts_tab(prompts_body)

        footer = ctk.CTkFrame(root)
        footer.pack(fill="x", pady=(8, 0))
        self.progress = ctk.CTkProgressBar(footer)
        self.progress.pack(fill="x", padx=16, pady=(12, 6))
        self.progress.set(0)
        ctk.CTkLabel(footer, textvariable=self.status_var).pack(
            anchor="w", padx=16, pady=(0, 6)
        )
        btn_row = ctk.CTkFrame(footer, fg_color="transparent")
        btn_row.pack(fill="x", padx=16, pady=(0, 6))

        self.ping_gpt_btn = ctk.CTkButton(
            btn_row, text="Ping GPT", width=120, command=self._on_ping_gpt
        )
        self.ping_gpt_btn.pack(side="left", padx=(0, 8))

        self.ping_tts_btn = ctk.CTkButton(
            btn_row, text="Ping озвучка", width=140, command=self._on_ping_tts
        )
        self.ping_tts_btn.pack(side="left", padx=(0, 8))

        self.ping_image_btn = ctk.CTkButton(
            btn_row, text="Ping картинка", width=140, command=self._on_ping_image
        )
        self.ping_image_btn.pack(side="left", padx=(0, 8))

        self.run_btn = ctk.CTkButton(
            btn_row,
            text="Создать ролик",
            command=self._on_run_dispatch,
            height=44,
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        self.run_btn.pack(side="left", fill="x", expand=True, padx=(0, 8))

        self.stop_btn = ctk.CTkButton(
            btn_row,
            text="Стоп",
            command=self._on_stop,
            width=110,
            height=44,
            fg_color="#8B2E2E",
            hover_color="#6E2222",
            state="disabled",
        )
        self.stop_btn.pack(side="right")

        self.reset_btn = ctk.CTkButton(
            footer,
            text="Сбросить чекпоинт (полный перезапуск)",
            command=self._on_reset_checkpoint,
            height=34,
            fg_color="gray40",
            hover_color="gray30",
        )
        self.reset_btn.pack(fill="x", padx=16, pady=(0, 12))

    def _build_full_form(self, body, cfg: dict) -> None:
        pad = {"padx": 12, "pady": 4}

        ctk.CTkLabel(body, text="GPT API key").pack(anchor="w", **pad)
        ctk.CTkEntry(body, textvariable=self.api_key_var).pack(
            fill="x", padx=12, pady=(0, 4)
        )

        ctk.CTkLabel(body, text="GPT модель").pack(anchor="w", **pad)
        self.model_menu = ctk.CTkOptionMenu(
            body,
            variable=self.model_var,
            values=_model_menu_values(self.model_var.get()),
            width=280,
        )
        self.model_menu.pack(anchor="w", padx=12, pady=(0, 4))

        ctk.CTkLabel(body, text="Lumean API key").pack(anchor="w", **pad)
        ctk.CTkEntry(body, textvariable=self.lumean_key_var).pack(
            fill="x", padx=12, pady=(0, 4)
        )

        ctk.CTkLabel(body, text="Вступление (в начало озвучки, без GPT)").pack(
            anchor="w", **pad
        )
        self.prefix_box = ctk.CTkTextbox(body, height=70)
        self.prefix_box.pack(fill="x", padx=12, pady=(0, 6))
        if cfg.get("prefix"):
            self.prefix_box.insert("1.0", str(cfg["prefix"]))

        ctk.CTkLabel(body, text="Режим создания текста").pack(anchor="w", **pad)
        mode_row = ctk.CTkFrame(body, fg_color="transparent")
        mode_row.pack(fill="x", padx=12, pady=(0, 6))
        ctk.CTkRadioButton(
            mode_row,
            text="Переписывание текста",
            variable=self.story_mode_var,
            value=MODE_REWRITE,
            command=self._on_story_mode_changed,
        ).pack(side="left", padx=(0, 16))
        ctk.CTkRadioButton(
            mode_row,
            text="Рассказ с нуля по профессиям",
            variable=self.story_mode_var,
            value=MODE_PROFESSION,
            command=self._on_story_mode_changed,
        ).pack(side="left", padx=(0, 16))
        ctk.CTkRadioButton(
            mode_row,
            text="Рассказ с нуля по хуку",
            variable=self.story_mode_var,
            value=MODE_HOOK,
            command=self._on_story_mode_changed,
        ).pack(side="left")

        self.rewrite_input_frame = ctk.CTkFrame(body, fg_color="transparent")
        self.rewrite_input_frame.pack(fill="both", expand=True, padx=0, pady=0)
        ctk.CTkLabel(
            self.rewrite_input_frame, text="Рассказ с таймкодами (для рерайта)"
        ).pack(anchor="w", **pad)
        self.source_box = ctk.CTkTextbox(self.rewrite_input_frame, height=200)
        self.source_box.pack(fill="both", expand=True, padx=12, pady=(0, 6))

        self.profession_input_frame = ctk.CTkFrame(body, fg_color="transparent")
        ctk.CTkLabel(
            self.profession_input_frame,
            text="Профессия героини (свободный ввод)",
        ).pack(anchor="w", **pad)
        ctk.CTkEntry(
            self.profession_input_frame, textvariable=self.profession_var
        ).pack(fill="x", padx=12, pady=(0, 6))
        ctk.CTkLabel(
            self.profession_input_frame,
            text="Остальные слоты (имя, место, конфликт…) выбираются случайно",
            text_color="gray60",
        ).pack(anchor="w", padx=12, pady=(0, 6))

        self.hook_input_frame = ctk.CTkFrame(body, fg_color="transparent")
        ctk.CTkLabel(
            self.hook_input_frame,
            text="Фраза-хук (начало рассказа)",
        ).pack(anchor="w", **pad)
        ctk.CTkEntry(
            self.hook_input_frame, textvariable=self.hook_var
        ).pack(fill="x", padx=12, pady=(0, 6))
        ctk.CTkLabel(
            self.hook_input_frame,
            text="План по хуку → рассказ 8–10 тыс. слов; первая сцена = хук",
            text_color="gray60",
        ).pack(anchor="w", padx=12, pady=(0, 6))

        self.story_meta_frame = ctk.CTkFrame(body, fg_color="transparent")
        self.story_meta_frame.pack(fill="x", padx=0, pady=0)
        ctk.CTkLabel(
            self.story_meta_frame,
            text="Названия и мета после генерации",
        ).pack(anchor="w", **pad)
        self.story_meta_box = ctk.CTkTextbox(self.story_meta_frame, height=160)
        self.story_meta_box.pack(fill="x", padx=12, pady=(0, 6))
        self.story_meta_box.insert(
            "1.0",
            "После генерации рассказа здесь появятся названия, "
            "YouTube-заголовки, описание и превью-фразы.",
        )
        self.story_meta_box.configure(state="disabled")

        ctk.CTkLabel(body, text="Текст оверлея (весь ролик)").pack(anchor="w", **pad)
        self.overlay_box = ctk.CTkTextbox(body, height=60)
        self.overlay_box.pack(fill="x", padx=12, pady=(0, 6))
        if cfg.get("overlay_text"):
            self.overlay_box.insert("1.0", str(cfg["overlay_text"]))
        self._overlay_boxes.append(self.overlay_box)

        self._row_path(body, "Папка футажей (b-roll)", self.broll_var, self._pick_broll)
        self._row_path(body, "Папка головы диктора", self.head_var, self._pick_head)
        self._row_path(body, "Папка аутро", self.outro_var, self._pick_outro)
        ctk.CTkCheckBox(
            body, text="Анимация подписки", variable=self.subscribe_var
        ).pack(anchor="w", padx=12, pady=8)

        ctk.CTkCheckBox(
            body,
            text="Генерировать обложку вместе с роликом",
            variable=self.thumbnail_enabled_var,
            command=self._update_pipeline_thumb_visibility,
        ).pack(anchor="w", padx=12, pady=(4, 4))

        self.pipe_thumb_frame = ctk.CTkFrame(body, fg_color="transparent")
        self._thumb_frames.append(self.pipe_thumb_frame)
        self.preview_ui.mount_style_block(self.pipe_thumb_frame)
        self.preview_ui.mount_phrases_block(self.pipe_thumb_frame)
        self.preview_ui.mount_results_block(self.pipe_thumb_frame)

        self.log_label = ctk.CTkLabel(body, text=f"Файл лога: {LOG_PATH}")
        self.log_label.pack(anchor="w", padx=12, pady=(6, 2))
        self.log_box = ctk.CTkTextbox(body, height=120)
        self.log_box.pack(fill="x", padx=12, pady=(0, 10))
        self.log_box.configure(state="disabled")
        self._log_boxes.append(self.log_box)

    def _mount_compose_block(self, body, cfg: dict) -> ctk.CTkTextbox:
        pad = {"padx": 12, "pady": 4}
        ctk.CTkLabel(body, text="Текст оверлея (весь ролик)").pack(anchor="w", **pad)
        overlay = ctk.CTkTextbox(body, height=60)
        overlay.pack(fill="x", padx=12, pady=(0, 6))
        if cfg.get("overlay_text"):
            overlay.insert("1.0", str(cfg["overlay_text"]))
        self._overlay_boxes.append(overlay)
        self._row_path(body, "Папка футажей (b-roll)", self.broll_var, self._pick_broll)
        self._row_path(body, "Папка головы диктора", self.head_var, self._pick_head)
        self._row_path(body, "Папка аутро", self.outro_var, self._pick_outro)
        ctk.CTkCheckBox(
            body, text="Анимация подписки", variable=self.subscribe_var
        ).pack(anchor="w", padx=12, pady=8)
        return overlay

    def _mount_thumb_option(self, body) -> ctk.CTkFrame:
        ctk.CTkCheckBox(
            body,
            text="Генерировать обложку вместе с роликом",
            variable=self.thumbnail_enabled_var,
            command=self._update_pipeline_thumb_visibility,
        ).pack(anchor="w", padx=12, pady=(4, 4))
        frame = ctk.CTkFrame(body, fg_color="transparent")
        self._thumb_frames.append(frame)
        self.preview_ui.mount_style_block(frame)
        self.preview_ui.mount_phrases_block(frame)
        self.preview_ui.mount_results_block(frame)
        return frame

    def _build_from_text_tab(self, body, cfg: dict) -> None:
        pad = {"padx": 12, "pady": 4}
        ctk.CTkLabel(
            body,
            text="Готовый текст рассказа → озвучка → видео (без рерайта)",
            font=ctk.CTkFont(weight="bold"),
        ).pack(anchor="w", **pad)
        ctk.CTkLabel(
            body,
            text="Ключи GPT/Lumean — с вкладки «Полный пайплайн»",
            text_color="gray60",
        ).pack(anchor="w", padx=12, pady=(0, 6))

        ctk.CTkLabel(body, text="Текстовый файл (.txt) — перетащи или выбери").pack(
            anchor="w", **pad
        )
        drop = ctk.CTkFrame(body, height=72, border_width=2, border_color="#666666")
        drop.pack(fill="x", padx=12, pady=(0, 4))
        drop.pack_propagate(False)
        self.from_text_drop_label = ctk.CTkLabel(
            drop, text="⬇  Перетащи .txt сюда", font=ctk.CTkFont(size=14)
        )
        self.from_text_drop_label.pack(expand=True)
        try:
            drop.drop_target_register(DND_FILES)
            drop.dnd_bind("<<Drop>>", self._on_from_text_drop)
            self.from_text_drop_label.drop_target_register(DND_FILES)
            self.from_text_drop_label.dnd_bind("<<Drop>>", self._on_from_text_drop)
        except Exception as exc:
            log(f"DnD (текст): {exc}")

        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(0, 6))
        ctk.CTkEntry(row, textvariable=self.from_text_file_var).pack(
            side="left", fill="x", expand=True, padx=(0, 8)
        )
        ctk.CTkButton(row, text="Файл…", width=80, command=self._pick_from_text_file).pack(
            side="right"
        )

        ctk.CTkLabel(body, text="Превью текста (read-only)").pack(anchor="w", **pad)
        self.from_text_preview = ctk.CTkTextbox(body, height=140)
        self.from_text_preview.pack(fill="x", padx=12, pady=(0, 6))
        self.from_text_preview.configure(state="disabled")

        self._mount_compose_block(body, cfg)
        self.text_thumb_frame = self._mount_thumb_option(body)

    def _build_from_audio_tab(self, body, cfg: dict) -> None:
        pad = {"padx": 12, "pady": 4}
        ctk.CTkLabel(
            body,
            text="Готовая озвучка → видео (без TTS)",
            font=ctk.CTkFont(weight="bold"),
        ).pack(anchor="w", **pad)
        self.audio_thumb_hint = ctk.CTkLabel(
            body,
            text="Для обложки нужен .txt рассказа (опционально). Ключи — с «Полный пайплайн»",
            text_color="gray60",
        )
        self.audio_thumb_hint.pack(anchor="w", padx=12, pady=(0, 6))

        ctk.CTkLabel(body, text="Аудиофайл — перетащи или выбери").pack(anchor="w", **pad)
        drop = ctk.CTkFrame(body, height=72, border_width=2, border_color="#666666")
        drop.pack(fill="x", padx=12, pady=(0, 4))
        drop.pack_propagate(False)
        self.from_audio_drop_label = ctk.CTkLabel(
            drop, text="⬇  Перетащи .mp3/.wav/… сюда", font=ctk.CTkFont(size=14)
        )
        self.from_audio_drop_label.pack(expand=True)
        try:
            drop.drop_target_register(DND_FILES)
            drop.dnd_bind("<<Drop>>", self._on_from_audio_drop)
            self.from_audio_drop_label.drop_target_register(DND_FILES)
            self.from_audio_drop_label.dnd_bind("<<Drop>>", self._on_from_audio_drop)
        except Exception as exc:
            log(f"DnD (аудио): {exc}")

        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(0, 8))
        ctk.CTkEntry(row, textvariable=self.from_audio_file_var).pack(
            side="left", fill="x", expand=True, padx=(0, 8)
        )
        ctk.CTkButton(
            row, text="Файл…", width=80, command=self._pick_from_audio_file
        ).pack(side="right")

        self.audio_preview_story_slot = ctk.CTkFrame(body, fg_color="transparent")
        self.audio_preview_story_slot.pack(fill="x")
        self.audio_preview_story_block = ctk.CTkFrame(
            self.audio_preview_story_slot, fg_color="transparent"
        )
        ctk.CTkLabel(
            self.audio_preview_story_block,
            text="Текст для превью (.txt) — если нужна обложка",
        ).pack(anchor="w", **pad)
        trow = ctk.CTkFrame(self.audio_preview_story_block, fg_color="transparent")
        trow.pack(fill="x", padx=12, pady=(0, 6))
        ctk.CTkEntry(trow, textvariable=self.audio_preview_text_var).pack(
            side="left", fill="x", expand=True, padx=(0, 8)
        )
        ctk.CTkButton(
            trow, text="Файл…", width=80, command=self._pick_audio_preview_text
        ).pack(side="right")

        self._mount_compose_block(body, cfg)
        self.audio_thumb_frame = self._mount_thumb_option(body)

    def _build_thumb_tab(self, body) -> None:
        pad = {"padx": 12, "pady": 4}

        self.preview_ui.mount_style_block(
            body,
            title="Генерация YouTube-превью (тот же модуль, что в полном флоу)",
        )

        ctk.CTkLabel(body, text="GPT модель (фраза для превью)").pack(anchor="w", **pad)
        self.thumb_model_menu = ctk.CTkOptionMenu(
            body,
            variable=self.model_var,
            values=_model_menu_values(self.model_var.get()),
            width=280,
        )
        self.thumb_model_menu.pack(anchor="w", padx=12, pady=(0, 4))

        ctk.CTkLabel(body, text="Image API key").pack(anchor="w", **pad)
        ctk.CTkEntry(body, textvariable=self.image_api_key_var).pack(
            fill="x", padx=12, pady=(0, 4)
        )
        ctk.CTkLabel(body, text="Image base URL").pack(anchor="w", **pad)
        ctk.CTkEntry(body, textvariable=self.image_base_url_var).pack(
            fill="x", padx=12, pady=(0, 4)
        )
        ctk.CTkLabel(body, text="Модель картинки").pack(anchor="w", **pad)
        ctk.CTkEntry(body, textvariable=self.thumb_image_model_var).pack(
            fill="x", padx=12, pady=(0, 6)
        )

        self.preview_story_slot = ctk.CTkFrame(body, fg_color="transparent")
        self.preview_story_slot.pack(fill="x")

        self.preview_story_block = ctk.CTkFrame(
            self.preview_story_slot, fg_color="transparent"
        )
        story_pad = {"padx": 12, "pady": 4}
        ctk.CTkLabel(
            self.preview_story_block,
            text="Текстовый файл рассказа (.txt) — перетащи сюда или выбери",
        ).pack(anchor="w", **story_pad)

        drop = ctk.CTkFrame(
            self.preview_story_block, height=88, border_width=2, border_color="#666666"
        )
        drop.pack(fill="x", padx=12, pady=(0, 4))
        drop.pack_propagate(False)
        self.drop_label = ctk.CTkLabel(
            drop,
            text="⬇  Перетащи .txt с Desktop сюда",
            font=ctk.CTkFont(size=14),
        )
        self.drop_label.pack(expand=True)
        try:
            drop.drop_target_register(DND_FILES)
            drop.dnd_bind("<<Drop>>", self._on_file_drop)
            self.drop_label.drop_target_register(DND_FILES)
            self.drop_label.dnd_bind("<<Drop>>", self._on_file_drop)
        except Exception as exc:
            log(f"DnD недоступен: {exc}")

        row = ctk.CTkFrame(self.preview_story_block, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(0, 6))
        ctk.CTkEntry(row, textvariable=self.thumb_file_var).pack(
            side="left", fill="x", expand=True, padx=(0, 8)
        )
        ctk.CTkButton(row, text="Файл…", width=80, command=self._pick_thumb_file).pack(
            side="right"
        )

        ctk.CTkLabel(
            self.preview_story_block, text="Превью текста файла (read-only)"
        ).pack(anchor="w", **story_pad)
        self.thumb_story_preview = ctk.CTkTextbox(self.preview_story_block, height=120)
        self.thumb_story_preview.pack(fill="x", padx=12, pady=(0, 6))
        self.thumb_story_preview.configure(state="disabled")

        self.preview_no_story_hint = ctk.CTkLabel(
            self.preview_story_slot,
            text="Этот мастер: заголовки из titles.txt / без GPT-фраз — рассказ не нужен",
            text_color="gray60",
        )

        self.preview_ui.mount_phrases_block(body)

        self.thumb_generate_btn = ctk.CTkButton(
            body,
            text="Сгенерировать превью",
            command=self._on_generate_preview,
            height=42,
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        self.thumb_generate_btn.pack(fill="x", padx=12, pady=(0, 8))
        self.preview_ui.generate_btn = self.thumb_generate_btn

        self.preview_ui.mount_results_block(body)

        ctk.CTkLabel(body, text=f"Лог превью: {LOG_PATH}").pack(
            anchor="w", padx=12, pady=(6, 2)
        )
        self.thumb_log_box = ctk.CTkTextbox(body, height=150)
        self.thumb_log_box.pack(fill="both", expand=True, padx=12, pady=(0, 10))
        self.thumb_log_box.configure(state="disabled")
        self._log_boxes.append(self.thumb_log_box)

    def _build_prompts_tab(self, body) -> None:
        pad = {"padx": 12, "pady": 4}

        ctk.CTkLabel(
            body,
            text="Мастер-промпты превью (1, 2, …)",
            font=ctk.CTkFont(weight="bold"),
        ).pack(anchor="w", **pad)
        ctk.CTkLabel(
            body,
            text=(
                "Каждый мастер = опц. текст + опц. image_prep (GPT→промпт) + "
                "обязательная картинка. Пустой текст → без надписи. "
                "image_prep → сначала случайная внешность, потом картинка. "
                "За раз открыт один мастер."
            ),
            wraplength=900,
            justify="left",
        ).pack(anchor="w", padx=12, pady=(0, 6))

        top = ctk.CTkFrame(body, fg_color="transparent")
        top.pack(fill="x", padx=12, pady=(0, 8))
        self.prompt_add_btn = ctk.CTkButton(
            top,
            text="+ Новый мастер-промпт",
            width=200,
            command=self._on_add_prompt_draft,
        )
        self.prompt_add_btn.pack(side="left")

        self.prompts_list = ctk.CTkScrollableFrame(body, fg_color="transparent")
        self.prompts_list.pack(fill="both", expand=True, padx=8, pady=(0, 10))
        self._rebuild_prompts_accordion()

    def _update_pipeline_thumb_visibility(self) -> None:
        enabled = bool(self.thumbnail_enabled_var.get())
        for frame in self._thumb_frames:
            if enabled:
                # full tab: перед логом, если есть
                if frame is getattr(self, "pipe_thumb_frame", None) and hasattr(
                    self, "log_label"
                ):
                    frame.pack(fill="x", before=self.log_label)
                else:
                    frame.pack(fill="x")
            else:
                frame.pack_forget()
        if enabled:
            self.preview_ui.refresh_controls()

    def _sync_story_input_ui(self) -> None:
        """Скрыть/показать блок .txt рассказа в зависимости от мастера."""
        preset = get_preset(self.thumb_preset_var.get() or default_preset_id())
        need = preset.needs_story_input()

        if hasattr(self, "preview_story_block") and hasattr(self, "preview_no_story_hint"):
            if need:
                self.preview_no_story_hint.pack_forget()
                self.preview_story_block.pack(fill="x")
            else:
                self.preview_story_block.pack_forget()
                self.preview_no_story_hint.pack(anchor="w", padx=12, pady=(0, 8))

        if hasattr(self, "audio_preview_story_block"):
            if need:
                self.audio_preview_story_block.pack(fill="x")
            else:
                self.audio_preview_story_block.pack_forget()

        if hasattr(self, "audio_thumb_hint"):
            if need:
                self.audio_thumb_hint.configure(
                    text="Для обложки нужен .txt рассказа (опционально). "
                    "Ключи — с «Полный пайплайн»"
                )
            else:
                self.audio_thumb_hint.configure(
                    text="Обложка без рассказа (titles/image_prep). "
                    "Ключи — с «Полный пайплайн»"
                )

    def _refresh_all_preset_ui(self) -> None:
        self.preview_ui.refresh_controls()
        self._rebuild_prompts_accordion()

    def _selected_variant_count(self) -> int | None:
        return self.preview_ui.selected_count()

    def _require_variant_count(self) -> int | None:
        n = self._selected_variant_count()
        if n is None:
            messagebox.showerror(
                "Обложка",
                "Выбери сколько вариантов превью: 1, 2 или 3",
            )
            return None
        return n

    def _refresh_generate_btn_text(self) -> None:
        if not hasattr(self, "thumb_generate_btn"):
            return
        n = self._selected_variant_count()
        if n is None:
            self.thumb_generate_btn.configure(text="Сгенерировать превью (выбери 1–3)")
        else:
            word = {1: "вариант", 2: "варианта", 3: "варианта"}[n]
            self.thumb_generate_btn.configure(
                text=f"Сгенерировать {n} {word} превью"
            )

    def _on_preview_phrases(self, phrases: list[str]) -> None:
        """Показать фразы сразу (и во вкладке, и в пайплайне) — один модуль."""
        snapshot = list(phrases)
        self.after(0, lambda: self.preview_ui.set_phrases_list(snapshot))

    def _format_preview_results(self, result) -> str:
        lines = []
        for v in result.variants:
            if v.path:
                lines.append(f"OK #{v.index}: {v.path}\n   текст: {v.text}")
            else:
                lines.append(f"FAIL #{v.index}: {v.error}\n   текст: {v.text}")
        return "\n".join(lines)

    def _rebuild_prompts_accordion(self) -> None:
        if not hasattr(self, "prompts_list"):
            return
        # сохранить несохранённые правки (иначе destroy съедает expanded-редакторы)
        snapshots: dict[str, dict] = {}
        for key in list(self._prompt_rows.keys()):
            snap = self._snapshot_prompt_row(key)
            if snap:
                snapshots[key] = snap
                if key != _NEW_DRAFT_KEY and self._prompt_rows[key].get("expanded"):
                    self._prompt_expanded.add(key)

        draft_snapshot = snapshots.get(_NEW_DRAFT_KEY)
        if draft_snapshot is not None:
            self._prompt_expanded.add(_NEW_DRAFT_KEY)

        for child in self.prompts_list.winfo_children():
            child.destroy()
        self._prompt_rows.clear()
        self._prompt_action_btns = []
        if hasattr(self, "prompt_add_btn"):
            self._prompt_action_btns.append(self.prompt_add_btn)

        # один expanded за раз
        expanded_keys = [k for k in self._prompt_expanded if k != _NEW_DRAFT_KEY]
        if len(expanded_keys) > 1:
            keep = expanded_keys[0]
            self._prompt_expanded = {keep}
            if draft_snapshot is not None:
                self._prompt_expanded.add(_NEW_DRAFT_KEY)

        for num, preset in enumerate(get_presets(), start=1):
            snap = snapshots.get(preset.id)
            self._add_prompt_accordion_item(
                key=preset.id,
                number=num,
                title=preset.name,
                name=(snap or {}).get("name", preset.name),
                description=(snap or {}).get("description", preset.description),
                text_prompt=(snap or {}).get("text_prompt", preset.raw_text_template()),
                image_prep=(snap or {}).get(
                    "image_prep", preset.raw_image_prep_template()
                ),
                prompt=(snap or {}).get("prompt", preset.raw_image_template()),
                example=(snap or {}).get(
                    "example",
                    str(preset.example_image) if preset.example_image else "",
                ),
                builtin=preset.builtin,
                expanded=preset.id in self._prompt_expanded,
            )

        if draft_snapshot is not None or _NEW_DRAFT_KEY in self._prompt_expanded:
            snap = draft_snapshot or {
                "name": "",
                "description": "",
                "text_prompt": "",
                "image_prep": "",
                "prompt": "",
                "example": "",
            }
            next_num = len(get_presets()) + 1
            self._add_prompt_accordion_item(
                key=_NEW_DRAFT_KEY,
                number=next_num,
                title="Новый мастер-промпт",
                name=snap.get("name", ""),
                description=snap.get("description", ""),
                text_prompt=snap.get("text_prompt", ""),
                image_prep=snap.get("image_prep", ""),
                prompt=snap.get("prompt", ""),
                example=snap.get("example", ""),
                builtin=False,
                expanded=True,
            )

    def _snapshot_prompt_row(self, key: str) -> dict | None:
        row = self._prompt_rows.get(key)
        if not row:
            return None
        return {
            "name": row["name_var"].get().strip(),
            "description": row["desc_var"].get().strip(),
            "text_prompt": row["text_prompt_box"].get("1.0", "end-1c"),
            "image_prep": row["image_prep_box"].get("1.0", "end-1c"),
            "prompt": row["prompt_box"].get("1.0", "end-1c"),
            "example": row["example_var"].get().strip(),
        }

    def _add_prompt_accordion_item(
        self,
        *,
        key: str,
        number: int,
        title: str,
        name: str,
        description: str,
        text_prompt: str,
        image_prep: str,
        prompt: str,
        example: str,
        builtin: bool,
        expanded: bool,
    ) -> None:
        shell = ctk.CTkFrame(self.prompts_list, border_width=1, border_color="#555555")
        shell.pack(fill="x", padx=4, pady=4)

        header = ctk.CTkFrame(shell, fg_color="transparent")
        header.pack(fill="x", padx=6, pady=4)

        mark = "▼" if expanded else "▶"
        badge = " [встроенный]" if builtin else ""
        display_title = f"{number}. {title}"
        toggle_btn = ctk.CTkButton(
            header,
            text=f"{mark}  {display_title}{badge}",
            anchor="w",
            fg_color="transparent",
            hover_color=("gray80", "gray30"),
            text_color=("gray10", "gray90"),
            height=32,
            command=lambda k=key: self._toggle_prompt_row(k),
        )
        toggle_btn.pack(side="left", fill="x", expand=True)

        body = ctk.CTkFrame(shell, fg_color="transparent")
        name_var = ctk.StringVar(value=name)
        desc_var = ctk.StringVar(value=description)
        example_var = ctk.StringVar(value=example)

        ctk.CTkLabel(body, text="Название").pack(anchor="w", padx=8, pady=(4, 0))
        ctk.CTkEntry(body, textvariable=name_var).pack(fill="x", padx=8, pady=(0, 4))

        ctk.CTkLabel(body, text="Описание").pack(anchor="w", padx=8, pady=(0, 0))
        ctk.CTkEntry(body, textvariable=desc_var).pack(fill="x", padx=8, pady=(0, 4))

        ctk.CTkLabel(
            body,
            text="Промпт текста для превью (пусто = картинка без надписи)",
        ).pack(anchor="w", padx=8, pady=(0, 0))
        text_prompt_box = ctk.CTkTextbox(body, height=280)
        text_prompt_box.pack(fill="both", expand=True, padx=8, pady=(0, 6))
        if text_prompt:
            text_prompt_box.insert("1.0", text_prompt)

        ctk.CTkLabel(
            body,
            text=(
                "Промпт сборки картинки / image_prep (GPT → случайный image-промпт; "
                "пусто = обычный шаблон ниже)"
            ),
        ).pack(anchor="w", padx=8, pady=(0, 0))
        image_prep_box = ctk.CTkTextbox(body, height=320)
        image_prep_box.pack(fill="both", expand=True, padx=8, pady=(0, 6))
        if image_prep:
            image_prep_box.insert("1.0", image_prep)

        ctk.CTkLabel(
            body,
            text="Промпт картинки (обязательно; при image_prep — запасной/краткий)",
        ).pack(anchor="w", padx=8, pady=(0, 0))
        prompt_box = ctk.CTkTextbox(body, height=360)
        prompt_box.pack(fill="both", expand=True, padx=8, pady=(0, 6))
        if prompt:
            prompt_box.insert("1.0", prompt)

        ctk.CTkLabel(body, text="Пример изображения").pack(anchor="w", padx=8)
        preview_label = ctk.CTkLabel(body, text="(нет примера)")
        preview_label.pack(anchor="w", padx=8, pady=(0, 4))
        preview_img_holder: list = [None]

        def _set_preview(path_str: str) -> None:
            p = Path(path_str) if path_str else None
            if not p or not p.is_file():
                preview_label.configure(text="(нет примера)", image=None)
                preview_img_holder[0] = None
                return
            try:
                img = Image.open(p)
                img.thumbnail((360, 202))
                ctk_img = ctk.CTkImage(
                    light_image=img, dark_image=img, size=img.size
                )
                preview_img_holder[0] = ctk_img
                preview_label.configure(image=ctk_img, text="")
            except Exception:
                preview_label.configure(text="(ошибка загрузки)", image=None)
                preview_img_holder[0] = None

        _set_preview(example)

        drop = ctk.CTkFrame(body, height=52, border_width=2, border_color="#666666")
        drop.pack(fill="x", padx=8, pady=(0, 4))
        drop.pack_propagate(False)
        drop_label = ctk.CTkLabel(
            drop,
            text=(
                f"Файл: {Path(example).name}"
                if example and Path(example).name
                else "⬇  Перетащи картинку сюда"
            ),
            font=ctk.CTkFont(size=12),
        )
        drop_label.pack(expand=True)

        def _on_drop(event, k=key):
            self._on_prompt_row_image_drop(k, event)

        try:
            drop.drop_target_register(DND_FILES)
            drop.dnd_bind("<<Drop>>", _on_drop)
            drop_label.drop_target_register(DND_FILES)
            drop_label.dnd_bind("<<Drop>>", _on_drop)
        except Exception as exc:
            log(f"DnD (промпт {key}) недоступен: {exc}")

        path_row = ctk.CTkFrame(body, fg_color="transparent")
        path_row.pack(fill="x", padx=8, pady=(0, 6))
        ctk.CTkEntry(path_row, textvariable=example_var).pack(
            side="left", fill="x", expand=True, padx=(0, 8)
        )
        pick_btn = ctk.CTkButton(
            path_row,
            text="Файл…",
            width=80,
            command=lambda k=key: self._pick_prompt_row_example(k),
        )
        pick_btn.pack(side="right")

        btn_row = ctk.CTkFrame(body, fg_color="transparent")
        btn_row.pack(fill="x", padx=8, pady=(0, 8))
        save_btn = ctk.CTkButton(
            btn_row,
            text="Сохранить",
            height=36,
            command=lambda k=key: self._on_save_prompt_row(k),
        )
        save_btn.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self._prompt_action_btns.append(save_btn)

        delete_btn = None
        if not builtin:
            delete_btn = ctk.CTkButton(
                btn_row,
                text="Удалить",
                height=36,
                fg_color="#8B2E2E",
                hover_color="#6E2222",
                command=lambda k=key: self._on_delete_prompt_row(k),
            )
            delete_btn.pack(side="right", fill="x", expand=True)
            self._prompt_action_btns.append(delete_btn)

        self._prompt_rows[key] = {
            "shell": shell,
            "toggle": toggle_btn,
            "body": body,
            "title": display_title,
            "number": number,
            "builtin": builtin,
            "name_var": name_var,
            "desc_var": desc_var,
            "example_var": example_var,
            "text_prompt_box": text_prompt_box,
            "image_prep_box": image_prep_box,
            "prompt_box": prompt_box,
            "drop_label": drop_label,
            "preview_label": preview_label,
            "preview_img": preview_img_holder,
            "set_preview": _set_preview,
            "save_btn": save_btn,
            "delete_btn": delete_btn,
            "expanded": expanded,
        }

        if expanded:
            body.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        else:
            body.pack_forget()

    def _toggle_prompt_row(self, key: str) -> None:
        row = self._prompt_rows.get(key)
        if not row:
            return
        expanded = not row["expanded"]
        if expanded:
            # один открытый мастер — иначе редакторы душат друг друга
            for other_key, other in self._prompt_rows.items():
                if other_key == key or not other.get("expanded"):
                    continue
                other["expanded"] = False
                self._prompt_expanded.discard(other_key)
                badge = " [встроенный]" if other["builtin"] else ""
                other["toggle"].configure(text=f"▶  {other['title']}{badge}")
                other["body"].pack_forget()

        row["expanded"] = expanded
        badge = " [встроенный]" if row["builtin"] else ""
        mark = "▼" if expanded else "▶"
        row["toggle"].configure(text=f"{mark}  {row['title']}{badge}")
        if expanded:
            self._prompt_expanded.add(key)
            row["body"].pack(fill="both", expand=True, padx=4, pady=(0, 4))
            try:
                row["shell"].tkraise()
                self.prompts_list._parent_canvas.yview_moveto(0)  # type: ignore[attr-defined]
            except Exception:
                pass
        else:
            self._prompt_expanded.discard(key)
            row["body"].pack_forget()

    def _on_add_prompt_draft(self) -> None:
        if _NEW_DRAFT_KEY in self._prompt_rows:
            if not self._prompt_rows[_NEW_DRAFT_KEY]["expanded"]:
                self._toggle_prompt_row(_NEW_DRAFT_KEY)
            return
        self._prompt_expanded.add(_NEW_DRAFT_KEY)
        self._rebuild_prompts_accordion()

    def _pick_prompt_row_example(self, key: str) -> None:
        row = self._prompt_rows.get(key)
        if not row:
            return
        path = filedialog.askopenfilename(
            title="Пример изображения",
            filetypes=[
                ("Images", "*.png *.jpg *.jpeg *.webp *.gif *.bmp"),
                ("All", "*.*"),
            ],
        )
        if not path:
            return
        row["example_var"].set(path)
        row["drop_label"].configure(text=f"Файл: {Path(path).name}")
        row["set_preview"](path)

    def _on_prompt_row_image_drop(self, key: str, event) -> None:
        row = self._prompt_rows.get(key)
        if not row:
            return
        paths = self._parse_drop_paths(getattr(event, "data", "") or "")
        img = next(
            (p for p in paths if p.suffix.lower() in _IMAGE_EXTS),
            None,
        )
        if not img and paths:
            img = paths[0]
        if not img:
            messagebox.showerror("Промпты", "Перетащи файл изображения")
            return
        row["example_var"].set(str(img))
        row["drop_label"].configure(text=f"Файл: {img.name}")
        row["set_preview"](str(img))

    def _on_save_prompt_row(self, key: str) -> None:
        if self._busy:
            return
        row = self._prompt_rows.get(key)
        if not row:
            return
        name = row["name_var"].get().strip()
        desc = row["desc_var"].get().strip()
        text_prompt = row["text_prompt_box"].get("1.0", "end-1c")
        image_prep = row["image_prep_box"].get("1.0", "end-1c")
        prompt = row["prompt_box"].get("1.0", "end-1c").strip()
        ex = row["example_var"].get().strip()
        example = Path(ex) if ex else None
        preset_id = None if key == _NEW_DRAFT_KEY else key
        try:
            saved = save_user_preset(
                name=name,
                prompt=prompt,
                text_prompt=text_prompt,
                image_prep=image_prep,
                description=desc,
                example_image=example if example and example.is_file() else None,
                preset_id=preset_id,
            )
        except Exception as exc:
            messagebox.showerror("Промпты", str(exc))
            return
        self._prompt_expanded.discard(_NEW_DRAFT_KEY)
        self._prompt_expanded.add(saved.id)
        self._refresh_all_preset_ui()
        messagebox.showinfo("Промпты", f"Сохранено: {saved.name}")

    def _on_delete_prompt_row(self, key: str) -> None:
        if self._busy:
            return
        if key == _NEW_DRAFT_KEY:
            self._prompt_expanded.discard(_NEW_DRAFT_KEY)
            self._rebuild_prompts_accordion()
            return
        row = self._prompt_rows.get(key)
        if not row or row["builtin"]:
            messagebox.showerror("Промпты", "Нельзя удалить встроенный пресет")
            return
        if not messagebox.askyesno("Промпты", "Удалить этот пресет?"):
            return
        try:
            delete_user_preset(key)
        except Exception as exc:
            messagebox.showerror("Промпты", str(exc))
            return
        if self.thumb_preset_var.get() == key:
            self.thumb_preset_var.set(default_preset_id())
        self._prompt_expanded.discard(key)
        self._refresh_all_preset_ui()
        messagebox.showinfo("Промпты", "Удалено")

    def _parse_drop_paths(self, data: str) -> list[Path]:
        # tkdnd: {/path with spaces} /other
        raw = (data or "").strip()
        paths: list[str] = []
        cur = ""
        in_brace = False
        for ch in raw:
            if ch == "{":
                in_brace = True
                cur = ""
            elif ch == "}":
                in_brace = False
                paths.append(cur)
                cur = ""
            elif ch == " " and not in_brace:
                if cur:
                    paths.append(cur)
                    cur = ""
            else:
                cur += ch
        if cur:
            paths.append(cur)
        return [Path(p) for p in paths if p]

    def _on_file_drop(self, event) -> None:
        paths = self._parse_drop_paths(getattr(event, "data", "") or "")
        txt = next((p for p in paths if p.suffix.lower() == ".txt"), None)
        if not txt and paths:
            txt = paths[0]
        if not txt:
            messagebox.showerror("Превью", "Перетащи .txt файл")
            return
        self._load_thumb_file(txt)

    def _pick_thumb_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Текстовый файл рассказа",
            filetypes=[("Text", "*.txt"), ("All", "*.*")],
        )
        if path:
            self._load_thumb_file(Path(path))

    def _load_thumb_file(self, path: Path) -> None:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="cp1251")
        except Exception as exc:
            messagebox.showerror("Превью", f"Не удалось прочитать файл:\n{exc}")
            return
        self.thumb_file_var.set(str(path))
        self._story_for_preview = text
        self.thumb_story_preview.configure(state="normal")
        self.thumb_story_preview.delete("1.0", "end")
        preview = text if len(text) <= 4000 else text[:4000] + "\n…"
        self.thumb_story_preview.insert("1.0", preview)
        self.thumb_story_preview.configure(state="disabled")
        self.drop_label.configure(text=f"Файл: {path.name} ({len(text)} символов)")
        log(f"[превью] загружен файл {path} chars={len(text)}")

    def _read_text_file(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return path.read_text(encoding="cp1251")

    def _on_from_text_drop(self, event) -> None:
        paths = self._parse_drop_paths(getattr(event, "data", "") or "")
        txt = next((p for p in paths if p.suffix.lower() == ".txt"), None)
        if not txt and paths:
            txt = paths[0]
        if not txt:
            messagebox.showerror("Ролик по тексту", "Перетащи .txt файл")
            return
        self._load_from_text_file(txt)

    def _pick_from_text_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Готовый текст рассказа",
            filetypes=[("Text", "*.txt"), ("All", "*.*")],
        )
        if path:
            self._load_from_text_file(Path(path))

    def _load_from_text_file(self, path: Path) -> None:
        try:
            text = self._read_text_file(path)
        except Exception as exc:
            messagebox.showerror("Ролик по тексту", f"Не удалось прочитать:\n{exc}")
            return
        self.from_text_file_var.set(str(path))
        self._story_for_text_run = text
        self.from_text_preview.configure(state="normal")
        self.from_text_preview.delete("1.0", "end")
        preview = text if len(text) <= 4000 else text[:4000] + "\n…"
        self.from_text_preview.insert("1.0", preview)
        self.from_text_preview.configure(state="disabled")
        self.from_text_drop_label.configure(
            text=f"Файл: {path.name} ({len(text)} символов)"
        )
        log(f"[ролик по тексту] файл {path} chars={len(text)}")

    def _on_from_audio_drop(self, event) -> None:
        paths = self._parse_drop_paths(getattr(event, "data", "") or "")
        audio = next(
            (p for p in paths if p.suffix.lower() in AUDIO_EXTS),
            None,
        )
        if not audio and paths:
            audio = paths[0]
        if not audio:
            messagebox.showerror("Ролик по аудио", "Перетащи аудиофайл")
            return
        self._load_from_audio_file(audio)

    def _pick_from_audio_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Озвучка",
            filetypes=[
                ("Audio", " ".join(f"*{e}" for e in sorted(AUDIO_EXTS))),
                ("All", "*.*"),
            ],
        )
        if path:
            self._load_from_audio_file(Path(path))

    def _load_from_audio_file(self, path: Path) -> None:
        if not path.is_file():
            messagebox.showerror("Ролик по аудио", f"Файл не найден:\n{path}")
            return
        if path.suffix.lower() not in AUDIO_EXTS:
            messagebox.showerror(
                "Ролик по аудио",
                f"Нужен аудиофайл ({', '.join(sorted(AUDIO_EXTS))})",
            )
            return
        self.from_audio_file_var.set(str(path))
        size = path.stat().st_size
        self.from_audio_drop_label.configure(
            text=f"Файл: {path.name} ({size} bytes)"
        )
        log(f"[ролик по аудио] файл {path} bytes={size}")

    def _pick_audio_preview_text(self) -> None:
        path = filedialog.askopenfilename(
            title="Текст для превью",
            filetypes=[("Text", "*.txt"), ("All", "*.*")],
        )
        if not path:
            return
        p = Path(path)
        try:
            text = self._read_text_file(p)
        except Exception as exc:
            messagebox.showerror("Превью", f"Не удалось прочитать:\n{exc}")
            return
        self.audio_preview_text_var.set(str(p))
        self._story_for_audio_preview = text
        log(f"[ролик по аудио] текст для превью {p} chars={len(text)}")

    def _active_tab(self) -> str:
        return self.tabs.get() if hasattr(self, "tabs") else TAB_FULL

    def _on_tab_changed(self) -> None:
        try:
            self._propagate_overlay(self._read_overlay())
        except Exception:
            pass
        if not self._busy:
            self._refresh_run_button()

    def _read_overlay(self) -> str:
        tab = self._active_tab()
        box = self.overlay_box
        if tab == TAB_TEXT and len(self._overlay_boxes) >= 2:
            box = self._overlay_boxes[1]
        elif tab == TAB_AUDIO and len(self._overlay_boxes) >= 3:
            box = self._overlay_boxes[2]
        return box.get("1.0", "end-1c").strip()

    def _propagate_overlay(self, text: str) -> None:
        """Один оверлей на все вкладки — иначе легко потерять правку."""
        for box in self._overlay_boxes:
            cur = box.get("1.0", "end-1c")
            if cur == text:
                continue
            box.delete("1.0", "end")
            if text:
                box.insert("1.0", text)

    def _overlay_for_persist(self) -> str:
        text = self._read_overlay()
        if not text:
            text = self.overlay_box.get("1.0", "end-1c").strip()
        self._propagate_overlay(text)
        return text

    def _row_path(self, parent, label, var, command) -> None:
        ctk.CTkLabel(parent, text=label).pack(anchor="w", padx=12, pady=(4, 2))
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(0, 4))
        ctk.CTkEntry(row, textvariable=var).pack(
            side="left", fill="x", expand=True, padx=(0, 8)
        )
        ctk.CTkButton(row, text="…", width=40, command=command).pack(side="right")

    def _pick_broll(self) -> None:
        path = filedialog.askdirectory(title="Папка футажей")
        if path:
            self.broll_var.set(path)

    def _pick_head(self) -> None:
        path = filedialog.askdirectory(title="Папка головы")
        if path:
            self.head_var.set(path)

    def _pick_outro(self) -> None:
        path = filedialog.askdirectory(title="Папка аутро")
        if path:
            self.outro_var.set(path)

    def _append_log(self, line: str) -> None:
        def _ui() -> None:
            for box in self._log_boxes:
                try:
                    box.configure(state="normal")
                    box.insert("end", line + "\n")
                    box.see("end")
                    box.configure(state="disabled")
                except Exception:
                    pass

        self.after(0, _ui)

    def _action_widgets(self) -> tuple:
        widgets = [
            self.ping_gpt_btn,
            self.ping_tts_btn,
            self.ping_image_btn,
            self.run_btn,
            self.reset_btn,
            self.thumb_generate_btn,
            *self._prompt_action_btns,
        ]
        return tuple(widgets)

    def _begin_job(self, *, run_btn_text: str) -> int:
        self._job_gen += 1
        job_id = self._job_gen
        self._cancel = CancelToken()
        self._set_busy(True, run_btn_text=run_btn_text)
        return job_id

    def _is_current_job(self, job_id: int) -> bool:
        return job_id == self._job_gen

    def _on_stop(self) -> None:
        if not self._busy:
            return
        log("Стоп: запрошена остановка, сбрасываю UI…")
        if self._cancel:
            self._cancel.cancel()
        # Инвалидируем job и сразу разблокируем кнопки — можно стартовать заново.
        self._job_gen += 1
        self._set_busy(False)
        self.progress.set(0)
        self.status_var.set("Остановлено — можно запускать снова")
        messagebox.showinfo(
            "Стоп",
            "Операция остановлена. Контекст сброшен — можно запускать заново.",
        )

    def _set_busy(self, busy: bool, *, run_btn_text: str | None = None) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        for w in self._action_widgets():
            try:
                w.configure(state=state)
            except Exception:
                pass
        self.stop_btn.configure(state="normal" if busy else "disabled")
        if busy:
            self.run_btn.configure(text=run_btn_text or "Идет создание…")
        else:
            self._cancel = None
            self._refresh_run_button()
            self._refresh_generate_btn_text()

    def _on_story_mode_changed(self) -> None:
        self._sync_story_mode_ui()
        self._refresh_run_button()

    def _sync_story_mode_ui(self) -> None:
        if not hasattr(self, "rewrite_input_frame"):
            return
        mode = self.story_mode_var.get()
        self.rewrite_input_frame.pack_forget()
        self.profession_input_frame.pack_forget()
        self.hook_input_frame.pack_forget()
        before = self.story_meta_frame
        if mode == MODE_PROFESSION:
            self.profession_input_frame.pack(
                fill="x", padx=0, pady=0, before=before
            )
        elif mode == MODE_HOOK:
            self.hook_input_frame.pack(
                fill="x", padx=0, pady=0, before=before
            )
        else:
            self.rewrite_input_frame.pack(
                fill="both", expand=True, padx=0, pady=0, before=before
            )

    def _set_story_meta_ui(self, meta: StoryMeta | None) -> None:
        if not hasattr(self, "story_meta_box"):
            return
        text = (
            meta.format_for_ui()
            if meta is not None and not meta.is_empty()
            else "Мета пустая — модель не вернула названия/описание."
        )
        self.story_meta_box.configure(state="normal")
        self.story_meta_box.delete("1.0", "end")
        self.story_meta_box.insert("1.0", text)
        self.story_meta_box.configure(state="disabled")

    def _clear_story_meta_ui(self) -> None:
        if not hasattr(self, "story_meta_box"):
            return
        self.story_meta_box.configure(state="normal")
        self.story_meta_box.delete("1.0", "end")
        self.story_meta_box.insert(
            "1.0",
            "После генерации рассказа здесь появятся названия, "
            "YouTube-заголовки, описание и превью-фразы.",
        )
        self.story_meta_box.configure(state="disabled")

    def _pipeline_identity_text(self) -> str:
        mode = self.story_mode_var.get()
        if mode == MODE_PROFESSION:
            return f"profession\n{self.profession_var.get().strip()}"
        if mode == MODE_HOOK:
            return f"hook\n{self.hook_var.get().strip()}"
        return self.source_box.get("1.0", "end-1c")

    def _refresh_run_button(self) -> None:
        tab = self._active_tab()
        if tab == TAB_PREVIEW:
            self.run_btn.configure(text="Сгенерировать превью")
            return
        if tab == TAB_PROMPTS:
            self.run_btn.configure(text="Создать ролик")
            return
        if tab == TAB_TEXT:
            self.run_btn.configure(text="Ролик из текста")
            return
        if tab == TAB_AUDIO:
            self.run_btn.configure(text="Ролик из аудио")
            return
        # полный пайплайн — чекпоинт
        chk = cp.load_checkpoint()
        source = self._pipeline_identity_text()
        prefix = self.prefix_box.get("1.0", "end-1c")
        if (
            chk
            and chk.can_resume
            and chk.source_hash == cp.content_hash(source)
            and chk.prefix_hash == cp.content_hash(prefix)
        ):
            self.run_btn.configure(text=chk.resume_button_label)
            hint = chk.next_stage_label or chk.stage
            if not self._busy:
                self.status_var.set(f"Есть чекпоинт -> продолжить с: {hint}")
        else:
            self.run_btn.configure(text="Создать ролик")

    def _on_reset_checkpoint(self) -> None:
        if self._busy:
            return
        cp.clear_checkpoint()
        self._refresh_run_button()
        self.status_var.set("Чекпоинт сброшен")
        messagebox.showinfo("Чекпоинт", "Сброшено. Следующий запуск пойдет с рерайта.")

    def _persist(self) -> None:
        data = {
            "api_key": self.api_key_var.get().strip(),
            "base_url": self.base_url_var.get().strip() or DEFAULT_BASE_URL,
            "model": self.model_var.get().strip() or DEFAULT_MODEL,
            "lumean_api_key": self.lumean_key_var.get().strip(),
            "lumean_template_id": self.template_id_var.get().strip()
            or DEFAULT_TEMPLATE_ID,
            "lumean_voice_id": self.voice_id_var.get().strip() or DEFAULT_VOICE_ID,
            "prefix": self.prefix_box.get("1.0", "end-1c"),
            "overlay_text": self._overlay_for_persist(),
            "broll_dir": self.broll_var.get().strip(),
            "head_dir": self.head_var.get().strip(),
            "outro_dir": self.outro_var.get().strip(),
            "subscribe": bool(self.subscribe_var.get()),
            "image_api_key": self.image_api_key_var.get().strip()
            or DEFAULT_IMAGE_API_KEY,
            "image_base_url": self.image_base_url_var.get().strip()
            or DEFAULT_IMAGE_BASE_URL,
            "thumbnail_image_model": self.thumb_image_model_var.get().strip()
            or DEFAULT_IMAGE_MODEL,
            "thumbnail_preset_id": self.thumb_preset_var.get() or default_preset_id(),
            "thumbnail_enabled": bool(self.thumbnail_enabled_var.get()),
            "story_mode": self.story_mode_var.get() or MODE_REWRITE,
            "profession": self.profession_var.get().strip(),
            "hook": self.hook_var.get().strip(),
        }
        count = self._selected_variant_count()
        if count is not None:
            data["thumbnail_variant_count"] = count
        save_config(data)

    def _on_ping_gpt(self) -> None:
        if self._busy:
            return
        key = self.api_key_var.get().strip()
        if not key:
            messagebox.showerror("Ошибка", "Вставь GPT API key")
            return
        base = self.base_url_var.get().strip() or DEFAULT_BASE_URL
        model = self.model_var.get().strip() or DEFAULT_MODEL
        self._persist()
        job_id = self._begin_job(run_btn_text="Ping…")
        self.status_var.set("Ping GPT…")
        cancel = self._cancel

        def work() -> None:
            rewriter = None
            try:
                rewriter = OpenAIRewriter(api_key=key, model=model, base_url=base)
                if cancel:
                    cancel.register(rewriter)
                reply = rewriter.ping()
                if not self._is_current_job(job_id):
                    return
                self.after(0, lambda: self._ping_done(True, "GPT", reply))
            except CancelledError:
                return
            except Exception as exc:
                if cancel and cancel.is_cancelled():
                    return
                if is_expected_network_error(exc):
                    # ping() уже залогировал короткую строку — без traceback в UI
                    msg = f"{exc}\n\n{PROXY_UNREACHABLE_HINT}"
                else:
                    log(traceback.format_exc())
                    msg = str(exc)
                if self._is_current_job(job_id):
                    self.after(0, lambda m=msg: self._ping_done(False, "GPT", m))
            finally:
                if rewriter is not None:
                    rewriter.close()

        threading.Thread(target=work, daemon=True).start()

    def _on_ping_tts(self) -> None:
        if self._busy:
            return
        key = self.lumean_key_var.get().strip()
        if not key:
            messagebox.showerror("Ошибка", "Вставь Lumean API key")
            return
        self._persist()
        job_id = self._begin_job(run_btn_text="Ping…")
        self.status_var.set("Ping озвучка…")
        cancel = self._cancel

        def work() -> None:
            client = None
            try:
                client = LumeanClient(key)
                if cancel:
                    cancel.register(client)
                reply = client.ping()
                if not self._is_current_job(job_id):
                    return
                self.after(0, lambda: self._ping_done(True, "Озвучка", reply))
            except CancelledError:
                return
            except Exception as exc:
                if cancel and cancel.is_cancelled():
                    return
                log(traceback.format_exc())
                if self._is_current_job(job_id):
                    self.after(0, lambda: self._ping_done(False, "Озвучка", str(exc)))
            finally:
                if client:
                    client.close()

        threading.Thread(target=work, daemon=True).start()

    def _on_ping_image(self) -> None:
        if self._busy:
            return
        key = self.image_api_key_var.get().strip() or DEFAULT_IMAGE_API_KEY
        base = self.image_base_url_var.get().strip() or DEFAULT_IMAGE_BASE_URL
        model = self.thumb_image_model_var.get().strip() or DEFAULT_IMAGE_MODEL
        if not key:
            messagebox.showerror("Ошибка", "Вставь Image API key")
            return
        self._persist()
        job_id = self._begin_job(run_btn_text="Ping…")
        self.status_var.set("Ping картинка…")
        cancel = self._cancel

        def work() -> None:
            try:
                if cancel and cancel.is_cancelled():
                    return
                reply = ping_image_api(
                    api_key=key, base_url=base, image_model=model
                )
                if not self._is_current_job(job_id):
                    return
                self.after(0, lambda: self._ping_done(True, "Картинка", reply))
            except CancelledError:
                return
            except Exception as exc:
                if cancel and cancel.is_cancelled():
                    return
                log(traceback.format_exc())
                if self._is_current_job(job_id):
                    self.after(0, lambda: self._ping_done(False, "Картинка", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _ping_done(self, ok: bool, title: str, msg: str) -> None:
        self._set_busy(False)
        self.status_var.set("Готов" if ok else f"Ошибка {title}")
        if ok:
            messagebox.showinfo(title, msg)
        else:
            messagebox.showerror(title, msg)

    def _optional_dir(self, var: ctk.StringVar) -> Path | None:
        raw = var.get().strip()
        return Path(raw) if raw else None

    def _compose_params(self, overlay: str) -> ComposeParams:
        return ComposeParams(
            overlay_text=overlay,
            broll_dir=Path(self.broll_var.get().strip()),
            head_dir=Path(self.head_var.get().strip()),
            subscribe=bool(self.subscribe_var.get()),
            outro_dir=self._optional_dir(self.outro_var),
        )

    def _thumbnail_params(self, *, enabled: bool, count: int) -> ThumbnailParams:
        return ThumbnailParams(
            enabled=enabled,
            preset_id=self.thumb_preset_var.get().strip() or default_preset_id(),
            variant_count=count,
            gpt_api_key=self.api_key_var.get().strip(),
            gpt_base_url=self.base_url_var.get().strip() or DEFAULT_BASE_URL,
            gpt_model=self.model_var.get().strip() or DEFAULT_MODEL,
            image_api_key=self.image_api_key_var.get().strip() or DEFAULT_IMAGE_API_KEY,
            image_base_url=self.image_base_url_var.get().strip()
            or DEFAULT_IMAGE_BASE_URL,
            image_model=self.thumb_image_model_var.get().strip() or DEFAULT_IMAGE_MODEL,
        )

    def _validate_compose_dirs(self, overlay: str) -> bool:
        if not overlay:
            messagebox.showerror("Ошибка", "Вставь текст оверлея")
            return False
        broll = Path(self.broll_var.get().strip())
        head = Path(self.head_var.get().strip())
        if not broll.is_dir():
            messagebox.showerror("Ошибка", "Укажи папку футажей")
            return False
        if not head.is_dir():
            messagebox.showerror("Ошибка", "Укажи папку головы")
            return False
        return True

    def _require_thumb_ready(self) -> int | None:
        count = self._require_variant_count()
        if count is None:
            return None
        if not get_presets():
            messagebox.showerror("Ошибка", "Нет пресетов обложки")
            return None
        preset = get_preset(self.thumb_preset_var.get() or default_preset_id())
        if preset.needs_gpt() and not self.api_key_var.get().strip():
            messagebox.showerror(
                "Ошибка",
                "У этого мастер-промпта нужен GPT API key "
                "(текст и/или сборка image-промпта)",
            )
            return None
        return count

    def _on_run_dispatch(self) -> None:
        tab = self._active_tab()
        if tab == TAB_TEXT:
            self._on_run_from_text()
        elif tab == TAB_AUDIO:
            self._on_run_from_audio()
        elif tab == TAB_PREVIEW:
            self._on_generate_preview()
        elif tab == TAB_PROMPTS:
            messagebox.showinfo("Промпты", "Пресеты редактируются здесь; запуск — на других вкладках")
        else:
            self._on_run()

    def _on_run(self) -> None:
        if self._busy:
            return

        gpt_key = self.api_key_var.get().strip()
        lumean_key = self.lumean_key_var.get().strip()
        base_url = self.base_url_var.get().strip() or DEFAULT_BASE_URL
        model = self.model_var.get().strip() or DEFAULT_MODEL
        template_id = self.template_id_var.get().strip() or DEFAULT_TEMPLATE_ID
        voice_id = self.voice_id_var.get().strip() or DEFAULT_VOICE_ID
        prefix = self.prefix_box.get("1.0", "end-1c")
        source = self.source_box.get("1.0", "end-1c")
        profession = self.profession_var.get().strip()
        hook = self.hook_var.get().strip()
        story_mode = self.story_mode_var.get() or MODE_REWRITE
        if story_mode not in _STORY_MODES:
            story_mode = MODE_REWRITE
        overlay = self.overlay_box.get("1.0", "end-1c").strip()
        broll = Path(self.broll_var.get().strip())
        head = Path(self.head_var.get().strip())
        subscribe = bool(self.subscribe_var.get())
        thumb_on = bool(self.thumbnail_enabled_var.get())

        if not gpt_key:
            messagebox.showerror("Ошибка", "Вставь GPT API key")
            return
        if not lumean_key:
            messagebox.showerror("Ошибка", "Вставь Lumean API key")
            return
        if story_mode == MODE_PROFESSION:
            if not profession:
                messagebox.showerror("Ошибка", "Укажи профессию героини")
                return
        elif story_mode == MODE_HOOK:
            if not hook:
                messagebox.showerror("Ошибка", "Вставь фразу-хук")
                return
        elif not source.strip():
            messagebox.showerror("Ошибка", "Вставь рассказ для рерайта")
            return
        if not self._validate_compose_dirs(overlay):
            return

        count = None
        if thumb_on:
            count = self._require_thumb_ready()
            if count is None:
                return

        preset_id = self.thumb_preset_var.get().strip() or default_preset_id()

        self._persist()
        job_id = self._begin_job(run_btn_text="Идет создание…")
        self.progress.set(0)
        self.status_var.set("Старт…")
        meta_modes = {MODE_PROFESSION, MODE_HOOK}
        if story_mode in meta_modes:
            self._clear_story_meta_ui()
        if thumb_on:
            self.preview_ui.clear_output()
        cancel = self._cancel

        req = FullRunRequest(
            source_text=source if story_mode == MODE_REWRITE else "",
            prefix=prefix,
            overlay_text=overlay,
            gpt_api_key=gpt_key,
            gpt_base_url=base_url,
            gpt_model=model,
            lumean_api_key=lumean_key,
            template_id=template_id,
            voice_id=voice_id,
            broll_dir=broll,
            head_dir=head,
            subscribe=subscribe,
            outro_dir=self._optional_dir(self.outro_var),
            thumbnail_enabled=thumb_on,
            thumbnail_preset_id=preset_id,
            thumbnail_variant_count=count or 1,
            image_api_key=self.image_api_key_var.get().strip() or DEFAULT_IMAGE_API_KEY,
            image_base_url=self.image_base_url_var.get().strip()
            or DEFAULT_IMAGE_BASE_URL,
            thumbnail_image_model=self.thumb_image_model_var.get().strip()
            or DEFAULT_IMAGE_MODEL,
            story_mode=story_mode,  # type: ignore[arg-type]
            profession=profession,
            hook=hook,
        )

        def work() -> None:
            try:
                result = run_full_pipeline(
                    req,
                    on_progress=self._progress,
                    on_preview_phrases=self._on_preview_phrases if thumb_on else None,
                    on_story_meta=self._on_story_meta_ready
                    if story_mode in meta_modes
                    else None,
                    start_from="auto",
                    cancel=cancel,
                )
                if not self._is_current_job(job_id):
                    return
                self.after(0, lambda: self._done_ok(result))
            except CancelledError:
                return
            except Exception as exc:
                if cancel and cancel.is_cancelled():
                    return
                log(traceback.format_exc())
                if self._is_current_job(job_id):
                    self.after(0, lambda: self._done_err(str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _on_story_meta_ready(self, meta: StoryMeta) -> None:
        self.after(0, lambda m=meta: self._set_story_meta_ui(m))

    def _on_run_from_text(self) -> None:
        if self._busy:
            return
        story = self._story_for_text_run.strip()
        if not story:
            # попробуем дочитать с диска
            path = Path(self.from_text_file_var.get().strip())
            if path.is_file():
                try:
                    story = self._read_text_file(path).strip()
                    self._story_for_text_run = story
                except Exception as exc:
                    messagebox.showerror("Ролик по тексту", str(exc))
                    return
        if not story:
            messagebox.showerror("Ролик по тексту", "Загрузи .txt с рассказом")
            return
        lumean_key = self.lumean_key_var.get().strip()
        if not lumean_key:
            messagebox.showerror("Ошибка", "Вставь Lumean API key (вкладка полного пайплайна)")
            return
        overlay = self._read_overlay()
        if not self._validate_compose_dirs(overlay):
            return
        thumb_on = bool(self.thumbnail_enabled_var.get())
        count = 1
        if thumb_on:
            ready = self._require_thumb_ready()
            if ready is None:
                return
            count = ready

        self._persist()
        job_id = self._begin_job(run_btn_text="Ролик из текста…")
        self.progress.set(0)
        self.status_var.set("Старт…")
        if thumb_on:
            self.preview_ui.clear_output()
        cancel = self._cancel
        compose = self._compose_params(overlay)
        thumb = self._thumbnail_params(enabled=thumb_on, count=count)
        work_audio = cp.RUN_DIR / "voice_from_text.mp3"

        def work() -> None:
            try:
                cp.ensure_run_dir()
                result = run_video_from_text(
                    story_text=story,
                    compose=compose,
                    lumean_api_key=lumean_key,
                    template_id=self.template_id_var.get().strip() or DEFAULT_TEMPLATE_ID,
                    voice_id=self.voice_id_var.get().strip() or DEFAULT_VOICE_ID,
                    thumb=thumb,
                    work_audio=work_audio,
                    on_progress=self._progress,
                    on_preview_phrases=self._on_preview_phrases if thumb_on else None,
                    cancel=cancel,
                )
                if not self._is_current_job(job_id):
                    return
                self.after(0, lambda: self._done_ok(result))
            except CancelledError:
                return
            except Exception as exc:
                if cancel and cancel.is_cancelled():
                    return
                log(traceback.format_exc())
                if self._is_current_job(job_id):
                    self.after(0, lambda: self._done_err(str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _on_run_from_audio(self) -> None:
        if self._busy:
            return
        audio = Path(self.from_audio_file_var.get().strip())
        if not audio.is_file():
            messagebox.showerror("Ролик по аудио", "Загрузи аудиофайл")
            return
        if audio.suffix.lower() not in AUDIO_EXTS:
            messagebox.showerror("Ролик по аудио", "Неподдерживаемый формат аудио")
            return
        overlay = self._read_overlay()
        if not self._validate_compose_dirs(overlay):
            return
        thumb_on = bool(self.thumbnail_enabled_var.get())
        preview_story = self._story_for_audio_preview.strip()
        if not preview_story:
            tpath = Path(self.audio_preview_text_var.get().strip())
            if tpath.is_file():
                try:
                    preview_story = self._read_text_file(tpath).strip()
                    self._story_for_audio_preview = preview_story
                except Exception as exc:
                    messagebox.showerror("Превью", str(exc))
                    return
        count = 1
        if thumb_on:
            preset = get_preset(self.thumb_preset_var.get() or default_preset_id())
            if preset.needs_story_input() and not preview_story:
                messagebox.showerror(
                    "Ролик по аудио",
                    "Мастер-промпт с текстом: укажи .txt рассказа "
                    "(поле «Текст для превью»)",
                )
                return
            ready = self._require_thumb_ready()
            if ready is None:
                return
            count = ready

        self._persist()
        job_id = self._begin_job(run_btn_text="Ролик из аудио…")
        self.progress.set(0)
        self.status_var.set("Старт…")
        if thumb_on:
            self.preview_ui.clear_output()
        cancel = self._cancel
        compose = self._compose_params(overlay)
        thumb = self._thumbnail_params(enabled=thumb_on, count=count)

        def work() -> None:
            try:
                result = run_video_from_audio(
                    audio_path=audio,
                    compose=compose,
                    thumb=thumb,
                    preview_story_text=preview_story,
                    on_progress=self._progress,
                    on_preview_phrases=self._on_preview_phrases if thumb_on else None,
                    cancel=cancel,
                )
                if not self._is_current_job(job_id):
                    return
                self.after(0, lambda: self._done_ok(result))
            except CancelledError:
                return
            except Exception as exc:
                if cancel and cancel.is_cancelled():
                    return
                log(traceback.format_exc())
                if self._is_current_job(job_id):
                    self.after(0, lambda: self._done_err(str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _on_generate_preview(self) -> None:
        if self._busy:
            return
        story = self._story_for_preview.strip()
        preset_id = self.thumb_preset_var.get().strip() or default_preset_id()
        preset = get_preset(preset_id)
        if not story and preset.needs_story_input():
            messagebox.showerror("Превью", "Перетащи или выбери .txt с рассказом")
            return
        if not story:
            # каталог titles / image_prep без text_prompt — рассказ не нужен
            story = ""
        count = self._require_variant_count()
        if count is None:
            return
        gpt_key = self.api_key_var.get().strip()
        if preset.needs_gpt() and not gpt_key:
            messagebox.showerror(
                "Превью",
                "У мастер-промпта нужен GPT API key "
                "(текст и/или сборка image-промпта)",
            )
            return
        if not gpt_key:
            gpt_key = "unused"
        image_key = self.image_api_key_var.get().strip() or DEFAULT_IMAGE_API_KEY
        base_url = self.base_url_var.get().strip() or DEFAULT_BASE_URL
        model = self.model_var.get().strip() or DEFAULT_MODEL
        image_base = self.image_base_url_var.get().strip() or DEFAULT_IMAGE_BASE_URL
        image_model = self.thumb_image_model_var.get().strip() or DEFAULT_IMAGE_MODEL

        self._persist()
        job_id = self._begin_job(run_btn_text="Превью…")
        self.progress.set(0.05)
        self.status_var.set("Генерация превью…")
        self.preview_ui.clear_output()
        cancel = self._cancel

        def work() -> None:
            gen = None
            try:
                gen = PreviewGenerator(
                    text_api_key=gpt_key,
                    text_base_url=base_url,
                    text_model=model,
                    image_api_key=image_key,
                    image_base_url=image_base,
                    image_model=image_model,
                    cancel=cancel,
                    on_progress=lambda m: self.after(
                        0, lambda msg=m: self.status_var.set(msg)
                    ),
                    on_phrases=self._on_preview_phrases,
                )
                result = gen.generate_batch(
                    story_text=story, preset_id=preset_id, variant_count=count
                )
                if not self._is_current_job(job_id):
                    return
                self.after(0, lambda: self._preview_done(result, count))
            except CancelledError:
                return
            except Exception as exc:
                if cancel and cancel.is_cancelled():
                    return
                log(traceback.format_exc())
                if self._is_current_job(job_id):
                    self.after(0, lambda: self._preview_err(str(exc)))
            finally:
                if gen is not None:
                    gen.close()

        threading.Thread(target=work, daemon=True).start()

    def _preview_done(self, result, count: int = 3) -> None:
        self._set_busy(False)
        self.progress.set(1)
        phrases = [v.text for v in result.variants] if result.variants else []
        if phrases:
            self.preview_ui.set_phrases_list(phrases)
        else:
            self.preview_ui.set_phrases(result.text)
        results_text = self._format_preview_results(result)
        self.preview_ui.set_results(results_text)
        ok = len(result.ok_paths)
        self.status_var.set(f"Превью: {ok}/{count} на Desktop")
        msg = f"Фразы:\n{result.text}\n\n" + results_text
        if ok == 0:
            messagebox.showinfo(
                "Превью",
                "Картинки не созданы (ретраев не было).\n\n" + msg,
            )
        elif result.errors:
            messagebox.showinfo(
                "Превью частично",
                f"Успешно {ok}/{count}. Ошибки без ретраев.\n\n" + msg,
            )
        else:
            messagebox.showinfo("Превью", msg)

    def _preview_err(self, err: str) -> None:
        self._set_busy(False)
        self.progress.set(0)
        self.status_var.set("Ошибка превью")
        messagebox.showinfo("Превью", f"Не удалось:\n{err}\n\nРетраев не было.")

    def _done_ok(self, result) -> None:
        self._set_busy(False)
        self.progress.set(1)
        self.status_var.set(f"Готово: {result.output_dir.name}")
        meta = getattr(result, "story_meta", None)
        if meta is not None:
            self._set_story_meta_ui(meta)
        parts = [
            f"Папка:\n{result.output_dir}",
            f"\n\nРолик:\n{result.video}",
        ]
        text_file = getattr(result, "text_file", None)
        if text_file:
            parts.append(f"\nТекст рассказа:\n{text_file}")
        audio_file = getattr(result, "audio_file", None)
        if audio_file:
            parts.append(f"\nОзвучка:\n{audio_file}")
        if meta is not None and not meta.is_empty() and meta.titles:
            parts.append("\n\nНазвания:\n" + "\n".join(f"• {t}" for t in meta.titles))
        if result.preview_error:
            parts.append(f"\n\nОбложка: ошибка — {result.preview_error}")
        elif result.preview is not None:
            phrases = [v.text for v in result.preview.variants]
            if phrases:
                self.preview_ui.set_phrases_list(phrases)
            self.preview_ui.set_results(self._format_preview_results(result.preview))
            ok = len(result.preview.ok_paths)
            total = len(result.preview.variants) or ok
            lines = []
            for v in result.preview.variants:
                if v.path:
                    lines.append(f"OK #{v.index}: {v.path}")
                else:
                    lines.append(f"FAIL #{v.index}: {v.error}")
            parts.append(f"\n\nОбложки: {ok}/{total}\n" + "\n".join(lines))
        messagebox.showinfo("Готово", "".join(parts))

    def _done_err(self, err: str) -> None:
        self._set_busy(False)
        self.progress.set(0)
        self._refresh_run_button()
        self.status_var.set("Ошибка")
        hint = ""
        if self._active_tab() == TAB_FULL:
            hint = (
                f"\n\nМожно нажать «{self.run_btn.cget('text')}» "
                "для продолжения с чекпоинта."
            )
        messagebox.showerror("Ошибка", f"{err}{hint}")

    def _progress(self, pct: float, msg: str) -> None:
        self.after(0, lambda p=pct, m=msg: self._set_progress(p, m))

    def _set_progress(self, pct: float, msg: str) -> None:
        self.progress.set(max(0.0, min(1.0, pct)))
        self.status_var.set(msg)


def main() -> None:
    app = App()
    app.mainloop()
