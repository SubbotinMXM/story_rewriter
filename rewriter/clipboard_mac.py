"""Cmd+C/V/X/A для CustomTkinter на macOS.

Tk на Aqua часто теряет CLIPBOARD после первой вставки; bind_all + пустой
clipboard_get() + return 'break' убивает последующие Cmd+V. Лечится так:
- трекаем последний focused Entry/Text
- читаем буфер через Tk, иначе через pbpaste
- 'break' только если реально вставили текст

Русская раскладка: НЕ биндим кириллические keysym (<Command-м> и т.п.) —
часть Tcl/Tk (CLT Python) падает с TclError. Ловим через KeyPress + keycode/char.
"""

from __future__ import annotations

import subprocess
import tkinter as tk
from typing import Any, Callable

import customtkinter as ctk

_STATE: dict[str, Any] = {
    "root": None,
    "last": None,
}

# Только ASCII — безопасно на любом Tk
_ASCII_SEQUENCES: tuple[tuple[str, str], ...] = (
    ("<Command-v>", "paste"),
    ("<Command-V>", "paste"),
    ("<Command-c>", "copy"),
    ("<Command-C>", "copy"),
    ("<Command-x>", "cut"),
    ("<Command-X>", "cut"),
    ("<Command-a>", "select_all"),
    ("<Command-A>", "select_all"),
    ("<Meta-v>", "paste"),
    ("<Meta-c>", "copy"),
    ("<Meta-x>", "cut"),
    ("<Meta-a>", "select_all"),
)

# ANSI keycodes на Mac (не зависят от раскладки)
_KEYCODE_A = 0
_KEYCODE_C = 8
_KEYCODE_V = 9
_KEYCODE_X = 7

# Физические клавиши QWERTY при русской раскладке (event.char / keysym)
_RU_CHARS_PASTE = frozenset("мМ")
_RU_CHARS_COPY = frozenset("сС")
_RU_CHARS_CUT = frozenset("чЧ")
_RU_CHARS_SELECT = frozenset("фФ")


def _handler_for(name: str) -> Callable:
    return {
        "paste": _paste,
        "copy": _copy,
        "cut": _cut,
        "select_all": _select_all,
    }[name]


def _safe_bind(widget: tk.Misc, seq: str, handler: Callable, *, add: str | None = None) -> bool:
    """bind без краша: не-ASCII keysym на части Tk → TclError."""
    try:
        if add is None:
            widget.bind(seq, handler)
        else:
            widget.bind(seq, handler, add=add)
        return True
    except tk.TclError:
        return False


def _safe_bind_all(root: tk.Misc, seq: str, handler: Callable, *, add: str | None = None) -> bool:
    try:
        if add is None:
            root.bind_all(seq, handler)
        else:
            root.bind_all(seq, handler, add=add)
        return True
    except tk.TclError:
        return False


def enable_mac_clipboard(root: ctk.CTk) -> None:
    """Никогда не должен валить старт UI — все бинды best-effort."""
    _STATE["root"] = root
    _STATE["last"] = None

    try:
        for seq, name in _ASCII_SEQUENCES:
            _safe_bind_all(root, seq, _handler_for(name))

        # Русская раскладка / любой keysym: keycode + char, без кириллицы в bind-строке
        _safe_bind_all(root, "<Command-KeyPress>", _command_keypress)
        _safe_bind_all(root, "<Meta-KeyPress>", _command_keypress)

        _safe_bind_all(root, "<FocusIn>", _on_focus_in, add="+")
        try:
            _bind_tree(root)
        except Exception:
            pass
    except Exception:
        # полный fail-soft: UI уже собран
        pass


def bind_widget(widget: tk.Misc) -> None:
    native = _native(widget)
    if native is None:
        return
    for seq, name in _ASCII_SEQUENCES:
        _safe_bind(native, seq, _handler_for(name), add="+")
    _safe_bind(native, "<Command-KeyPress>", _command_keypress, add="+")
    _safe_bind(native, "<Meta-KeyPress>", _command_keypress, add="+")
    _safe_bind(native, "<FocusIn>", _on_focus_in, add="+")


def _clipboard_action_from_event(event) -> str | None:
    """paste|copy|cut|select_all по keycode / char / keysym (ASCII или кириллица)."""
    if event is None:
        return None
    code = getattr(event, "keycode", None)
    if code == _KEYCODE_V:
        return "paste"
    if code == _KEYCODE_C:
        return "copy"
    if code == _KEYCODE_X:
        return "cut"
    if code == _KEYCODE_A:
        return "select_all"

    ch = getattr(event, "char", None) or ""
    if ch in _RU_CHARS_PASTE or ch in ("v", "V"):
        return "paste"
    if ch in _RU_CHARS_COPY or ch in ("c", "C"):
        return "copy"
    if ch in _RU_CHARS_CUT or ch in ("x", "X"):
        return "cut"
    if ch in _RU_CHARS_SELECT or ch in ("a", "A"):
        return "select_all"

    sym = (getattr(event, "keysym", None) or "").lower()
    if sym in ("v", "м"):
        return "paste"
    if sym in ("c", "с"):
        return "copy"
    if sym in ("x", "ч"):
        return "cut"
    if sym in ("a", "ф"):
        return "select_all"
    return None


def _command_keypress(event=None):
    action = _clipboard_action_from_event(event)
    if action is None:
        return None
    return _handler_for(action)(event)


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
        return None
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
