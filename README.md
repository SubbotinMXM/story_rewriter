# Story → Video

Desktop-приложение (CustomTkinter): рерайт / профессия / хук → озвучка → ролик.

## Другой Mac (с нуля)

```bash
brew install python@3.12 python-tk@3.12 ffmpeg
git clone <repo-url> story-rewriter
cd story-rewriter
./run.sh
```

`./run.sh` сам:
- ищет Python ≥3.11 (не системный CLT 3.9);
- создаёт `.venv`, ставит `requirements.txt`;
- глушит Tk deprecation warning;
- падает с понятной ошибкой, если Python слишком старый / нет tkinter.

`.config.json` не нужен при первом запуске — ключи вводишь в UI (файл появится после сохранения).

Нужны для ролика: `ffmpeg` в PATH, папки футажей/головы. Опционально: `assets/subscribe.mp4` если включена анимация подписки.

Шрифты/хром/промпты уже в репо (`assets/`, `rewriter/prompts/`).
