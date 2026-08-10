#!/usr/bin/env python3
"""Story → Video: рерайт + озвучка + сборка ролика."""

from __future__ import annotations


def main() -> None:
    from rewriter.ui import main as run_ui

    run_ui()


if __name__ == "__main__":
    main()
