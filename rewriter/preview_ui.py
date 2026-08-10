"""Единый UI-модуль превью: встраивается в полный флоу и во вкладку «Превью»."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal

import customtkinter as ctk
from PIL import Image

from rewriter.preset_titles import has_title_catalog
from rewriter.thumbnail_presets import default_preset_id, get_preset, get_presets

Mode = Literal["embedded", "standalone"]


@dataclass
class PreviewUI:
    """Один контроллер — два (и больше) места монтирования с общими настройками."""

    preset_var: ctk.StringVar
    count_var: ctk.StringVar
    on_count_change: Callable[[], None] | None = None
    on_preset_change: Callable[[], None] | None = None

    phrase_boxes: list = field(default_factory=list)
    results_boxes: list = field(default_factory=list)
    preset_frames: list = field(default_factory=list)
    count_frames: list = field(default_factory=list)
    example_slots: list = field(default_factory=list)  # (desc_label, img_label, image_holder)
    generate_btn: object | None = None
    phrase_headers: list = field(default_factory=list)

    def mount_style_block(self, parent, *, title: str | None = None) -> None:
        pad = {"padx": 12, "pady": 4}
        if title:
            ctk.CTkLabel(parent, text=title, font=ctk.CTkFont(weight="bold")).pack(
                anchor="w", **pad
            )

        ctk.CTkLabel(parent, text="Стиль обложки").pack(anchor="w", padx=12, pady=(4, 2))
        preset_frame = ctk.CTkFrame(parent, fg_color="transparent")
        preset_frame.pack(fill="x", padx=12, pady=(0, 6))
        self.preset_frames.append(preset_frame)

        desc = ctk.CTkLabel(parent, text="Пример стиля")
        desc.pack(anchor="w", padx=12, pady=(0, 4))
        img = ctk.CTkLabel(parent, text="")
        img.pack(anchor="w", padx=12, pady=(0, 8))
        holder: list = [None]
        self.example_slots.append((desc, img, holder))

        ctk.CTkLabel(parent, text="Сколько вариантов (обязательно)").pack(
            anchor="w", padx=12, pady=(0, 2)
        )
        count_frame = ctk.CTkFrame(parent, fg_color="transparent")
        count_frame.pack(fill="x", padx=12, pady=(0, 8))
        self.count_frames.append(count_frame)

        self.refresh_controls()

    def mount_phrases_block(self, parent) -> None:
        header = ctk.CTkLabel(parent, text="Фразы превью (из GPT)")
        header.pack(anchor="w", padx=12, pady=(4, 2))
        self.phrase_headers.append(header)
        phrase = ctk.CTkTextbox(parent, height=90)
        phrase.pack(fill="x", padx=12, pady=(0, 6))
        self.phrase_boxes.append(phrase)

    def mount_results_block(self, parent) -> None:
        ctk.CTkLabel(parent, text="Результаты (пути)").pack(
            anchor="w", padx=12, pady=(4, 2)
        )
        results = ctk.CTkTextbox(parent, height=70)
        results.pack(fill="x", padx=12, pady=(0, 6))
        self.results_boxes.append(results)

    def refresh_controls(self) -> None:
        presets = get_presets() or [get_preset(default_preset_id())]
        current = self.preset_var.get()
        ids = {p.id for p in presets}
        if current not in ids:
            self.preset_var.set(default_preset_id())

        for frame in self.preset_frames:
            for child in frame.winfo_children():
                child.destroy()
            for i, preset in enumerate(presets, start=1):
                if has_title_catalog(preset.id):
                    suffix = " · каталог+PIL"
                elif preset.needs_image_prep():
                    suffix = " · портрет"
                elif not preset.needs_story_input():
                    suffix = " · без текста"
                else:
                    suffix = ""
                ctk.CTkRadioButton(
                    frame,
                    text=f"{i}. {preset.name}{suffix}",
                    variable=self.preset_var,
                    value=preset.id,
                    command=self._on_preset_selected,
                ).pack(anchor="w", pady=2)

        for frame in self.count_frames:
            for child in frame.winfo_children():
                child.destroy()
            row = ctk.CTkFrame(frame, fg_color="transparent")
            row.pack(anchor="w")
            for n in (1, 2, 3):
                ctk.CTkRadioButton(
                    row,
                    text=str(n),
                    variable=self.count_var,
                    value=str(n),
                    command=self.on_count_change,
                ).pack(side="left", padx=(0, 16))

        self.refresh_examples()
        if self.on_preset_change:
            try:
                self.on_preset_change()
            except Exception:
                pass

    def _on_preset_selected(self) -> None:
        self.refresh_examples()
        if self.on_preset_change:
            try:
                self.on_preset_change()
            except Exception:
                pass

    def refresh_examples(self) -> None:
        preset = get_preset(self.preset_var.get())
        if has_title_catalog(preset.id):
            phrases_title = "Заголовки (очередь titles.txt)"
        elif preset.needs_story_input():
            phrases_title = "Фразы превью (из GPT)"
        else:
            phrases_title = "Фразы превью"
        for header in self.phrase_headers:
            try:
                header.configure(text=phrases_title)
            except Exception:
                pass
        for desc, img_label, holder in self.example_slots:
            desc.configure(text=f"Пример: {preset.description}")
            if not preset.example_image.is_file():
                img_label.configure(text="(пример не найден)", image=None)
                holder[0] = None
                continue
            try:
                im = Image.open(preset.example_image)
                im.thumbnail((440, 248))
                ctk_img = ctk.CTkImage(light_image=im, dark_image=im, size=im.size)
                holder[0] = ctk_img
                img_label.configure(image=ctk_img, text="")
            except Exception:
                img_label.configure(text="(ошибка загрузки примера)", image=None)
                holder[0] = None

    def clear_output(self) -> None:
        for box in self.phrase_boxes:
            box.delete("1.0", "end")
        for box in self.results_boxes:
            box.delete("1.0", "end")

    def set_phrases(self, text: str) -> None:
        for box in self.phrase_boxes:
            box.delete("1.0", "end")
            if text:
                box.insert("1.0", text)

    def set_phrases_list(self, phrases: list[str]) -> None:
        lines = [f"#{i}: {p}" for i, p in enumerate(phrases, start=1)]
        self.set_phrases("\n".join(lines))

    def set_results(self, text: str) -> None:
        for box in self.results_boxes:
            box.delete("1.0", "end")
            if text:
                box.insert("1.0", text)

    def selected_count(self) -> int | None:
        raw = (self.count_var.get() or "").strip()
        if raw in ("1", "2", "3"):
            return int(raw)
        return None
