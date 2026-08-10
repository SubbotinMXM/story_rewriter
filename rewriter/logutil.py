"""Логи в файл + опциональный callback в UI."""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Callable

LogCb = Callable[[str], None]

_lock = threading.Lock()
_callback: LogCb | None = None

LOG_PATH = Path(__file__).resolve().parent.parent / "rewriter.log"


def set_log_callback(cb: LogCb | None) -> None:
    global _callback
    _callback = cb


def log(msg: str) -> None:
    line = f"{datetime.now().strftime('%H:%M:%S')}  {msg}"
    with _lock:
        try:
            with LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        cb = _callback
    print(line, flush=True)
    if cb:
        try:
            cb(line)
        except Exception:
            pass
