"""Обёртка над OpenAI-совместимым Chat Completions (Codex API / fishappedu)."""

from __future__ import annotations

import time

import httpx
from openai import APIConnectionError, APITimeoutError, Omit, OpenAI

from rewriter.cancel import CancelledError
from rewriter.logutil import log
from rewriter.prompt import GLOSSARY_SYSTEM, SYSTEM_PROMPT

# Из инструкции портала: Base URL + ключ + модель из списка
DEFAULT_BASE_URL = "https://fishappedu.online/v1"
DEFAULT_MODEL = "gpt-5.4"
DEFAULT_MODELS = [
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.5",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
]

# fishappedu WAF режет дефолтные X-Stainless-* заголовки SDK → 403 blocked.
# Omit + curl UA = как их пример curl.
_SAFE_HEADERS = {
    "User-Agent": "curl/8.7.1",
    "X-Stainless-Lang": Omit(),
    "X-Stainless-Package-Version": Omit(),
    "X-Stainless-OS": Omit(),
    "X-Stainless-Arch": Omit(),
    "X-Stainless-Runtime": Omit(),
    "X-Stainless-Runtime-Version": Omit(),
    "X-Stainless-Async": Omit(),
    "x-stainless-retry-count": Omit(),
    "x-stainless-read-timeout": Omit(),
}

# Прокси fishappedu иногда рвёт TLS/TCP — свои ретраи поверх SDK.
_CHAT_ATTEMPTS = 4
# Ping: жёсткий wall-clock, без SDK-ретраев (иначе 15×2 ≈ 30s+).
_PING_TIMEOUT = httpx.Timeout(15.0, connect=8.0)
_CHAT_TIMEOUT = httpx.Timeout(600.0, connect=60.0)

PROXY_UNREACHABLE_HINT = (
    "прокси не отвечает / очередь — попробуй позже или смени base_url"
)


def _exc_chain(exc: BaseException) -> str:
    """Корневая причина: DNS / TLS / proxy / reset, не только обёртка SDK."""
    parts: list[str] = [f"{type(exc).__name__}: {exc}"]
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        nxt = cur.__cause__ or cur.__context__
        if nxt is None or id(nxt) in seen:
            break
        parts.append(f"{type(nxt).__name__}: {nxt}")
        cur = nxt
    return " ← ".join(parts)


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (APIConnectionError, APITimeoutError, httpx.TransportError)):
        return True
    name = type(exc).__name__
    if "Timeout" in name or "Connection" in name:
        return True
    msg = str(exc).lower()
    return any(
        s in msg
        for s in (
            "connection",
            "timed out",
            "timeout",
            "reset by peer",
            "broken pipe",
            "temporarily unavailable",
            "ssl",
            "eof",
        )
    )


def is_expected_network_error(exc: BaseException) -> bool:
    """Ожидаемые сетевые фейлы ping/прокси — без traceback в UI."""
    if isinstance(exc, (APIConnectionError, APITimeoutError, httpx.TransportError)):
        return True
    name = type(exc).__name__
    return "Timeout" in name or "Connection" in name


def is_content_policy_error(exc: BaseException) -> bool:
    """403 / content_policy — не ретраим, вызывающий код может fail-soft."""
    name = type(exc).__name__
    if name in ("PermissionDeniedError", "ContentFilterFinishReasonError"):
        return True
    status = getattr(exc, "status_code", None)
    msg = str(exc).lower()
    if status == 403 and (
        "content_policy" in msg
        or "content policy" in msg
        or "cbrn" in msg
        or "rejected" in msg
    ):
        return True
    return any(
        s in msg
        for s in (
            "content_policy",
            "content policy",
            "content_policy_violation",
            "cbrn",
            "safety system",
        )
    )


def _models_unsupported(exc: BaseException) -> bool:
    """GET /models нет на прокси → можно fallback на chat."""
    status = getattr(exc, "status_code", None)
    if status in (404, 405):
        return True
    name = type(exc).__name__
    return name in ("NotFoundError", "NotFound")


class OpenAIRewriter:
    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        url = (base_url or DEFAULT_BASE_URL).strip().rstrip("/")
        self.base_url = url
        self.model = (model or DEFAULT_MODEL).strip()
        self._glossary_model = self.model

        # trust_env=False — не цеплять системный HTTP_PROXY (часто даёт 403)
        self._http = httpx.Client(timeout=_CHAT_TIMEOUT, trust_env=False)
        self.client = OpenAI(
            api_key=api_key.strip(),
            base_url=url,
            http_client=self._http,
            # Основные ретраи — в _chat (с логом причины). SDK — один быстрый.
            max_retries=1,
            default_headers=_SAFE_HEADERS,
        )
        log(f"Клиент готов: base_url={url} model={self.model}")

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass
        try:
            if not self._http.is_closed:
                self._http.close()
        except Exception:
            pass

    def ping(self) -> str:
        """Быстрый ping: GET /models (без токенов), иначе короткий chat.

        max_retries=0 — иначе timeout × (1+retries) даёт ~90s при 45s.
        """
        # Отдельные опции только для ping; rewrite остаётся на max_retries=1 + 600s.
        client = self.client.with_options(timeout=_PING_TIMEOUT, max_retries=0)
        t0 = time.monotonic()

        log(f"PING → GET {self.base_url}/models")
        try:
            page = client.models.list()
            # Только первая страница — без автопагинации SDK.
            items = getattr(page, "data", None) or []
            ids: list[str] = []
            for item in items:
                mid = str(getattr(item, "id", "") or "").strip()
                if mid:
                    ids.append(mid)
            ms = int((time.monotonic() - t0) * 1000)
            has = self.model in ids if ids else False
            summary = (
                f"OK ({ms} ms). Моделей: {len(ids)}. "
                f"{'Есть' if has else 'Нет'} «{self.model}»."
            )
            if ids:
                shown = ", ".join(ids[:12])
                if len(ids) > 12:
                    shown += "…"
                summary += f" Список: {shown}"
            log(f"PING OK ({ms} ms): models={len(ids)}")
            return summary
        except Exception as models_exc:
            ms = int((time.monotonic() - t0) * 1000)
            if self._http.is_closed:
                log(f"PING aborted ({ms} ms): клиент закрыт (стоп)")
                raise
            if not _models_unsupported(models_exc):
                log(f"PING FAIL ({ms} ms): {_exc_chain(models_exc)}")
                raise

            log(
                f"PING /models недоступен ({ms} ms, {type(models_exc).__name__}) "
                "→ fallback chat"
            )

        log(
            f"PING → POST {self.base_url}/chat/completions "
            f"model={self.model} max_tokens=5"
        )
        t0 = time.monotonic()
        try:
            resp = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "user", "content": "Ответь одним словом: ок"}
                ],
                max_tokens=5,
            )
            content = (resp.choices[0].message.content or "").strip()
            ms = int((time.monotonic() - t0) * 1000)
            log(f"PING OK ({ms} ms): {content[:120]!r}")
            return content or "ok"
        except Exception as exc:
            ms = int((time.monotonic() - t0) * 1000)
            if self._http.is_closed:
                log(f"PING aborted ({ms} ms): клиент закрыт (стоп)")
            else:
                log(f"PING FAIL ({ms} ms): {_exc_chain(exc)}")
            raise

    def rewrite_part(
        self,
        *,
        part_index: int,
        parts_total: int,
        source_fragment: str,
        glossary_block: str,
        asides_count: int,
    ) -> str:
        aside_rule = (
            f"В этой части добавь ровно {asides_count} авторских комментария "
            f"от первого лица (уместные по смыслу)."
            if asides_count > 0
            else (
                "В этой части НЕ добавляй авторских комментариев от первого лица "
                "(квота по всему рассказу уже распределена)."
            )
        )

        user = (
            f"Это часть {part_index}/{parts_total} одного рассказа.\n"
            f"{aside_rule}\n\n"
            f"{glossary_block}\n\n"
            f"Исходный фрагмент для переработки:\n\n{source_fragment}"
        )

        return self._chat(
            label=f"rewrite {part_index}/{parts_total}",
            model=self.model,
            system=SYSTEM_PROMPT,
            user=user,
        )

    def update_glossary(
        self,
        *,
        glossary_json: str,
        source_fragment: str,
        rewritten: str,
    ) -> str:
        user = (
            f"Предыдущий glossary:\n{glossary_json}\n\n"
            f"Исходный фрагмент:\n{source_fragment}\n\n"
            f"Переписанный фрагмент:\n{rewritten}"
        )
        return self._chat(
            label="glossary",
            model=self._glossary_model,
            system=GLOSSARY_SYSTEM,
            user=user,
        )

    def complete(
        self,
        *,
        label: str,
        system: str,
        user: str,
        model: str | None = None,
    ) -> str:
        """Свободный chat completion (план, части рассказа, мета и т.п.)."""
        return self._chat(
            label=label,
            model=(model or self.model).strip() or self.model,
            system=system,
            user=user,
        )

    def write_ending(
        self,
        *,
        rewritten_parts: list[str],
        glossary_block: str,
    ) -> str:
        """4-я часть исходника отбрасывается; пишем новый позитивный финал."""
        story_so_far = "\n".join(p.strip() for p in rewritten_parts if p.strip())
        system = (
            "Ты — автор художественного рассказа. Тебе дан уже написанный текст "
            "первых частей истории. Допиши финал.\n"
            "Правила:\n"
            "— Логично продолжай сюжет из переданного текста.\n"
            "— Закончи историю позитивно.\n"
            "— Сохраняй имена, места и детали из текста и glossary.\n"
            "— Пиши от третьего лица, живо, в том же стиле.\n"
            "— Не пересказывай заново уже написанное — только продолжение и финал.\n"
            "— В ответе только текст финала, без пояснений и заголовков."
        )
        user = (
            f"{glossary_block}\n\n"
            f"Уже написанные части рассказа:\n\n{story_so_far}\n\n"
            "Логично и позитивно закончи эту историю."
        )
        return self._chat(
            label="ending",
            model=self.model,
            system=system,
            user=user,
        )

    def _chat(
        self,
        *,
        label: str,
        model: str,
        system: str,
        user: str,
    ) -> str:
        chars_in = len(system) + len(user)
        log(
            f"REQ [{label}] → {self.base_url}/chat/completions "
            f"model={model} chars≈{chars_in}"
        )
        t0 = time.monotonic()
        last_exc: BaseException | None = None

        for attempt in range(1, _CHAT_ATTEMPTS + 1):
            if self._http.is_closed:
                raise CancelledError("Остановлено пользователем")
            try:
                # Как в curl портала: только model + messages (без stream)
                resp = self.client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                )
                break
            except CancelledError:
                raise
            except Exception as exc:
                last_exc = exc
                ms = int((time.monotonic() - t0) * 1000)
                if self._http.is_closed:
                    log(
                        f"ERR [{label}] ({ms} ms): aborted — клиент закрыт (стоп); "
                        f"{_exc_chain(exc)}"
                    )
                    raise CancelledError("Остановлено пользователем") from exc
                if not _is_transient(exc) or attempt >= _CHAT_ATTEMPTS:
                    log(f"ERR [{label}] ({ms} ms): {_exc_chain(exc)}")
                    raise
                delay = min(2**attempt, 16)
                log(
                    f"RETRY [{label}] {attempt}/{_CHAT_ATTEMPTS} "
                    f"({ms} ms): {_exc_chain(exc)}; sleep {delay}s"
                )
                time.sleep(delay)
        else:
            assert last_exc is not None
            raise last_exc

        content = resp.choices[0].message.content
        ms = int((time.monotonic() - t0) * 1000)
        usage = getattr(resp, "usage", None)
        usage_s = ""
        if usage is not None:
            usage_s = (
                f" tokens in={getattr(usage, 'prompt_tokens', '?')} "
                f"out={getattr(usage, 'completion_tokens', '?')}"
            )
        if not content or not content.strip():
            log(f"ERR [{label}] ({ms} ms): пустой ответ{usage_s}")
            raise RuntimeError(f"Пустой ответ модели {model}")
        log(f"OK  [{label}] ({ms} ms) out_chars={len(content)}{usage_s}")
        return content.strip()
