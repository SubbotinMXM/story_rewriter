"""Тесты clipboard / paste на macOS + CustomTkinter."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import customtkinter as ctk

from rewriter.clipboard_mac import (
    enable_mac_clipboard,
    insert_clipboard_text,
    read_clipboard,
    write_clipboard,
)
from rewriter import clipboard_mac as cm


class ClipboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = ctk.CTk()
        cls.root.withdraw()
        enable_mac_clipboard(cls.root)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.root.destroy()

    def setUp(self) -> None:
        self.entry = ctk.CTkEntry(self.root)
        self.entry.pack()
        self.box = ctk.CTkTextbox(self.root, height=80)
        self.box.pack()
        self.root.update_idletasks()
        cm.bind_widget(self.entry)
        cm.bind_widget(self.box)
        self.native_entry = self.entry._entry
        self.native_box = self.box._textbox
        self.native_entry.delete(0, "end")
        self.native_box.delete("1.0", "end")
        cm._STATE["last"] = None
        self.root.update()

    def tearDown(self) -> None:
        self.entry.destroy()
        self.box.destroy()

    def _focus(self, native) -> None:
        native.focus_force()
        self.root.update()
        cm._STATE["last"] = native

    def test_read_clipboard_falls_back_to_pbpaste(self) -> None:
        subprocess.run(["pbcopy"], input="from-pbcopy", text=True, check=True)
        with mock.patch.object(
            self.root,
            "clipboard_get",
            side_effect=cm.tk.TclError("CLIPBOARD"),
        ):
            # widget.clipboard_get тоже упадёт — эмулируем через read без живого tk
            text = read_clipboard(None)
        self.assertEqual(text, "from-pbcopy")

    def test_paste_into_entry_twice(self) -> None:
        self._focus(self.native_entry)
        write_clipboard(self.native_entry, "AAA")
        self.assertTrue(insert_clipboard_text(self.native_entry, read_clipboard(self.native_entry)))
        write_clipboard(self.native_entry, "BBB")
        # имитируем потерю Tk CLIPBOARD после первой вставки
        with mock.patch.object(
            type(self.native_entry),
            "clipboard_get",
            side_effect=cm.tk.TclError("gone"),
        ):
            text = read_clipboard(self.native_entry)
        self.assertEqual(text, "BBB")
        self.assertTrue(insert_clipboard_text(self.native_entry, text))
        self.assertEqual(self.native_entry.get(), "AAABBB")

    def test_paste_into_textbox_twice(self) -> None:
        self._focus(self.native_box)
        write_clipboard(self.native_box, "раз ")
        insert_clipboard_text(self.native_box, read_clipboard(self.native_box))
        write_clipboard(self.native_box, "два")
        insert_clipboard_text(self.native_box, read_clipboard(self.native_box))
        got = self.native_box.get("1.0", "end-1c")
        self.assertEqual(got, "раз два")

    def test_cmd_v_handler_twice_with_empty_tk_clipboard(self) -> None:
        """Главный регресс: пустой/битый Tk clipboard не должен ломать 2-ю вставку."""
        self._focus(self.native_box)
        self.root.clipboard_clear()
        self.root.update()
        subprocess.run(["pbcopy"], input="FIRST", text=True, check=True)
        self.assertEqual(subprocess.check_output(["pbpaste"], text=True), "FIRST")

        class Ev:
            widget = self.native_box

        self.assertEqual(cm._paste(Ev()), "break")
        self.assertIn("FIRST", self.native_box.get("1.0", "end-1c"))

        self.root.clipboard_clear()
        self.root.update()
        subprocess.run(["pbcopy"], input="SECOND", text=True, check=True)
        self.assertEqual(subprocess.check_output(["pbpaste"], text=True), "SECOND")
        self.assertEqual(cm._paste(Ev()), "break")
        body = self.native_box.get("1.0", "end-1c").replace("\n", "")
        self.assertEqual(body, "FIRSTSECOND")

    def test_paste_uses_last_focus_when_event_widget_is_frame(self) -> None:
        self._focus(self.native_entry)
        write_clipboard(self.native_entry, "FOCUSOK")

        class Ev:
            widget = self.root  # не Entry

        self.assertEqual(cm._paste(Ev()), "break")
        self.assertEqual(self.native_entry.get(), "FOCUSOK")

    def test_empty_clipboard_does_not_break(self) -> None:
        self._focus(self.native_entry)
        subprocess.run(["pbcopy"], input="", text=True, check=True)
        self.root.clipboard_clear()

        class Ev:
            widget = self.native_entry

        self.assertIsNone(cm._paste(Ev()))


if __name__ == "__main__":
    unittest.main()
