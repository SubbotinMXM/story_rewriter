"""Отмена длинных операций из UI."""

from __future__ import annotations

import threading
from typing import Protocol


class Closable(Protocol):
    def close(self) -> None: ...


class CancelledError(RuntimeError):
    """Операция остановлена пользователем."""


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._closables: list[Closable] = []

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise CancelledError("Остановлено пользователем")

    def register(self, closable: Closable) -> None:
        with self._lock:
            if self._event.is_set():
                try:
                    closable.close()
                except Exception:
                    pass
                raise CancelledError("Остановлено пользователем")
            self._closables.append(closable)

    def cancel(self) -> None:
        self._event.set()
        with self._lock:
            closables = list(self._closables)
            self._closables.clear()
        for c in closables:
            try:
                c.close()
            except Exception:
                pass
