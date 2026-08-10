"""Cmd+C/V/X/A для CustomTkinter на macOS.

Tk на Aqua часто теряет CLIPBOARD после первой вставки; bind_all + пустой
clipboard_get() + return 'break' убивает последующие Cmd+V. Лечится так:
- трекаем последний focused Entry/Text
- читаем буфер через Tk, иначе через pbpaste
- 'break' только если реально вставили текст
"""

from __future__ import annotations

import subprocess
import tkinter as tk
from typing import Any

import customtkinter as ctk

_STATE: dict[str, Any] = {
    "root": None,
    "last": None,
}


def enable_mac_clipboard(root: ctk.CTk) -> None:
    _STATE["root"] = root
    _STATE["last"] = None

    sequences = (
        ("<Command-v>", _paste),
        ("<Command-V>", _paste),
        ("<Command-c>", _copy),
        ("<Command-C>", _copy),
        ("<Command-x>", _cut),
        ("<Command-X>", _cut),
        ("<Command-a>", _select_all),
        ("<Command-A>", _select_all),
        ("<Meta-v>", _paste),
        ("<Meta-c>", _copy),
        ("<Meta-x>", _cut),
        ("<Meta-a>", _select_all),
        # Русская раскладка: те же физ. клавиши → м/с/ч/ф
        ("<Command-м>", _paste),
        ("<Command-М>", _paste),
        ("<Command-с>", _copy),
        ("<Command-С>", _copy),
        ("<Command-ч>", _cut),
        ("<Command-Ч>", _cut),
        ("<Command-ф>", _select_all),
        ("<Command-Ф>", _select_all),
        ("<Command-KeyPress>", _command_keypress),
        ("<Meta-KeyPress>", _command_keypress),
    )
    for seq, handler in sequences:
        root.bind_all(seq, handler)

    root.bind_all("<FocusIn>", _on_focus_in, add="+")
    _bind_tree(root)


def bind_widget(widget: tk.Misc) -> None:
    native = _native(widget)
    if native is None:
        return
    sequences = (
        ("<Command-v>", _paste),
        ("<Command-V>", _paste),
        ("<Command-c>", _copy),
        ("<Command-C>", _copy),
        ("<Command-x>", _cut),
        ("<Command-X>", _cut),
        ("<Command-a>", _select_all),
        ("<Command-A>", _select_all),
        ("<Meta-v>", _paste),
        ("<Meta-c>", _copy),
        ("<Meta-x>", _cut),
        ("<Meta-a>", _select_all),
        ("<Command-м>", _paste),
        ("<Command-М>", _paste),
        ("<Command-с>", _copy),
        ("<Command-С>", _copy),
        ("<Command-ч>", _cut),
        ("<Command-Ч>", _cut),
        ("<Command-ф>", _select_all),
        ("<Command-Ф>", _select_all),
        ("<Command-KeyPress>", _command_keypress),
        ("<Meta-KeyPress>", _command_keypress),
        ("<FocusIn>", _on_focus_in),
    )
    for seq, handler in sequences:
        native.bind(seq, handler, add="+")


# ANSI keycodes на Mac (не зависят от раскладки)
_KEYCODE_A = 0
_KEYCODE_C = 8
_KEYCODE_V = 9
_KEYCODE_X = 7


def _command_keypress(event=None):
    if event is None:
        return None
    code = getattr(event, "keycode", None)
    if code == _KEYCODE_V:
        return _paste(event)
    if code == _KEYCODE_C:
        return _copy(event)
    if code == _KEYCODE_X:
        return _cut(event)
    if code == _KEYCODE_A:
        return _select_all(event)
    return None


def _bind_tree(widget: tk.Misc) -> None:
    if isinstance(widget, (ctk.CTkEntry, ctk.CTkTextbox)):
        bind_widget(widget)
    for child in widget.winfo_children():
        _bind_tree(child)


def _native(widget: tk.Misc | None) -> tk.Misc | None:
    if widget is None:
        return None
    if isinstance(widget, ctk.CTkEntry):
        return widget._entry  # noqa: SLF001
    if isinstance(widget, ctk.CTkTextbox):
        return widget._textbox  # noqa: SLF001
    try:
        cls = widget.winfo_class()
    except tk.TclError:
        return None
    if cls in {"Entry", "Text"}:
        return widget
    return None


def _on_focus_in(event=None) -> None:
    if event is None:
        return
    native = _native(event.widget)
    if native is not None:
        _STATE["last"] = native
        return
    try:
        if event.widget.winfo_class() in {"Entry", "Text"}:
            _STATE["last"] = event.widget
    except tk.TclError:
        pass


def _resolve_target(event=None) -> tk.Misc | None:
    candidates: list[tk.Misc | None] = []
    if event is not None:
        candidates.append(getattr(event, "widget", None))
    root = _STATE.get("root")
    if root is not None:
        try:
            candidates.append(root.focus_get())
        except tk.TclError:
            pass
    candidates.append(_STATE.get("last"))

    for cand in candidates:
        native = _native(cand)
        if native is not None:
            _STATE["last"] = native
            return native
        if cand is None:
            continue
        try:
            if cand.winfo_class() in {"Entry", "Text"}:
                _STATE["last"] = cand
                return cand
        except tk.TclError:
            continue
    return None


def _pbpaste() -> str:
    try:
        return subprocess.check_output(
            ["pbpaste"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return ""


def read_clipboard(widget: tk.Misc | None = None) -> str:
    """На macOS сначала pbpaste — Tk CLIPBOARD после 1-й вставки часто пустой."""
    text = _pbpaste()
    if text:
        return text

    if widget is not None:
        try:
            text = widget.clipboard_get()
            if text:
                return text
        except tk.TclError:
            pass
    root = _STATE.get("root")
    if root is not None:
        try:
            text = root.clipboard_get()
            if text:
                return text
        except tk.TclError:
            pass
    return ""


def write_clipboard(widget: tk.Misc, data: str) -> None:
    widget.clipboard_clear()
    widget.clipboard_append(data)
    try:
        widget.update_idletasks()
    except tk.TclError:
        pass
    # Дублируем в системный буфер — иначе следующая вставка в Tk пустая
    try:
        subprocess.run(
            ["pbcopy"],
            input=data,
            text=True,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def _clear_ctk_placeholder(widget: tk.Misc) -> None:
    """Если это внутренний Entry CTk с активным placeholder — сбросить."""
    parent = getattr(widget, "master", None)
    # CTkEntry: native._entry sits inside frames; climb a bit
    cur = widget
    for _ in range(5):
        if isinstance(cur, ctk.CTkEntry):
            try:
                cur._deactivate_placeholder()  # noqa: SLF001
            except Exception:
                pass
            return
        cur = getattr(cur, "master", None)
        if cur is None:
            break
    # иногда placeholder живёт на parent chain иначе — no-op
    _ = parent


def insert_clipboard_text(widget: tk.Misc, text: str) -> bool:
    if not text:
        return False
    cls = widget.winfo_class()
    if cls == "Entry":
        _clear_ctk_placeholder(widget)
        try:
            if widget.selection_present():
                widget.delete("sel.first", "sel.last")
        except tk.TclError:
            pass
        widget.insert("insert", text)
        return True
    if cls == "Text":
        try:
            widget.delete("sel.first", "sel.last")
        except tk.TclError:
            pass
        widget.insert("insert", text)
        return True
    return False


def _paste(event=None):
    widget = _resolve_target(event)
    if widget is None:
        return None
    text = read_clipboard(widget)
    if not text:
        return None  # не блокируем дефолтный <<Paste>>
    if insert_clipboard_text(widget, text):
        return "break"
    return None


def _copy(event=None):
    widget = _resolve_target(event)
    if widget is None:
        return None
    cls = widget.winfo_class()
    try:
        if cls == "Entry" and widget.selection_present():
            data = widget.selection_get()
        elif cls == "Text":
            data = widget.get("sel.first", "sel.last")
        else:
            return None
    except tk.TclError:
        return None
    write_clipboard(widget, data)
    return "break"


def _cut(event=None):
    widget = _resolve_target(event)
    if widget is None:
        return None
    cls = widget.winfo_class()
    try:
        if cls == "Entry" and widget.selection_present():
            data = widget.selection_get()
            widget.delete("sel.first", "sel.last")
        elif cls == "Text":
            data = widget.get("sel.first", "sel.last")
            widget.delete("sel.first", "sel.last")
        else:
            return None
    except tk.TclError:
        return None
    write_clipboard(widget, data)
    return "break"


def _select_all(event=None):
    widget = _resolve_target(event)
    if widget is None:
        return None
    cls = widget.winfo_class()
    if cls == "Entry":
        widget.selection_range(0, "end")
        widget.icursor("end")
        return "break"
    if cls == "Text":
        widget.tag_add("sel", "1.0", "end-1c")
        widget.mark_set("insert", "1.0")
        widget.see("insert")
        return "break"
    return None
