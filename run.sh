#!/bin/zsh
set -euo pipefail
cd "$(dirname "$0")"

export TK_SILENCE_DEPRECATION="${TK_SILENCE_DEPRECATION:-1}"

MIN_MAJOR=3
MIN_MINOR=11

die() {
  print -u2 "ERROR: $*"
  exit 1
}

py_ok() {
  local bin="$1"
  [[ -x "$bin" ]] || return 1
  "$bin" -c "import sys; raise SystemExit(0 if sys.version_info >= (${MIN_MAJOR}, ${MIN_MINOR}) else 1)" 2>/dev/null
}

find_python() {
  local c
  for c in \
    python3.14 python3.13 python3.12 python3.11 \
    /opt/homebrew/bin/python3.14 \
    /opt/homebrew/bin/python3.13 \
    /opt/homebrew/bin/python3.12 \
    /opt/homebrew/bin/python3.11 \
    /usr/local/bin/python3.14 \
    /usr/local/bin/python3.13 \
    /usr/local/bin/python3.12 \
    /usr/local/bin/python3.11 \
    python3
  do
    if command -v "$c" >/dev/null 2>&1; then
      local resolved
      resolved="$(command -v "$c")"
      if py_ok "$resolved"; then
        print -r -- "$resolved"
        return 0
      fi
    elif [[ -x "$c" ]] && py_ok "$c"; then
      print -r -- "$c"
      return 0
    fi
  done
  return 1
}

VENV_PY=".venv/bin/python"

need_recreate=0
if [[ ! -x "$VENV_PY" ]]; then
  need_recreate=1
elif ! py_ok "$VENV_PY"; then
  print -u2 "WARN: .venv Python слишком старый ($("$VENV_PY" -c 'import sys; print(f\"{sys.version_info.major}.{sys.version_info.minor}\")' 2>/dev/null || echo '?')), пересоздаю ≥${MIN_MAJOR}.${MIN_MINOR}"
  need_recreate=1
fi

if [[ "$need_recreate" -eq 1 ]]; then
  PY="$(find_python)" || die "Нужен Python ≥${MIN_MAJOR}.${MIN_MINOR} (не /usr/bin/python3 3.9). Установи: brew install python@3.12"
  ver="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  print "Using $PY ($ver)"
  rm -rf .venv
  "$PY" -m venv .venv
  "$VENV_PY" -m pip install --upgrade pip
  "$VENV_PY" -m pip install -r requirements.txt
elif [[ ! -f .venv/.deps_ok ]] || [[ requirements.txt -nt .venv/.deps_ok ]]; then
  "$VENV_PY" -m pip install -r requirements.txt
  touch .venv/.deps_ok
fi

# маркер после успешного create-path тоже
[[ -f .venv/.deps_ok ]] || touch .venv/.deps_ok

if ! "$VENV_PY" -c "import tkinter" 2>/dev/null; then
  die "В .venv нет tkinter. На Apple Silicon: brew install python-tk@3.12 (или тот же major, что у venv)"
fi

# Диагностика Tk/CTk: blank gray часто = старый системный Tk / mismatch python-tk
"$VENV_PY" - <<'PY' || die "CTk smoke failed — переустанови python-tk того же major, что venv: brew reinstall python-tk@3.12 && rm -rf .venv && ./run.sh"
import sys
import tkinter as tk
import customtkinter as ctk

print(f"Python {sys.version.split()[0]}  tk={tk.TkVersion} tcl={tk.TclVersion}")
ctk.set_appearance_mode("Light")
ctk.set_default_color_theme("blue")
root = ctk.CTk()
root.withdraw()
btn = ctk.CTkButton(root, text="smoke")
btn.pack()
root.update_idletasks()
n = len(root.winfo_children())
root.destroy()
if n < 1:
    raise SystemExit("CTk created zero children")
print(f"CTk smoke ok (children={n})")
PY

if ! command -v ffmpeg >/dev/null 2>&1; then
  print -u2 "WARN: ffmpeg не найден в PATH (сборка роликов упадёт). brew install ffmpeg"
fi

exec "$VENV_PY" main.py "$@"
