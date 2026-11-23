"""Проверка редактора новостей без Telegram и базы."""
from __future__ import annotations

import argparse
import asyncio
import sys
from time import monotonic

from app.config import Settings, load_profiles
from app.rewriter import OpenAICompatBackend, Rewriter, format_news

SAMPLE = (
    "В учебном примере игрок Анна Миронова выиграла турнир «Весенний кубок». "
    "Турнир собрал 120 входов. Победительница получила 4500 евро, а второе место занял Иван Петров."
)


async def check(label, settings, profile) -> bool:
    name = settings.claude_model if settings.llm_provider == "anthropic" else settings.llm_model
    started = monotonic()
    print(f"{label} ({name})", flush=True)
    editor = None
    try:
        editor = Rewriter(settings)
        post = await asyncio.wait_for(editor.rewrite_news(profile, SAMPLE), timeout=180)
        if not post.is_news:
            print(f"Проверочный материал отклонён: {post.reject_reason}")
            return False
        print(f"OK за {monotonic() - started:.1f} с\n{format_news(post)}")
        return True
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        response = getattr(exc, "response", None)
        retry_after = response.headers.get("retry-after") if response is not None else None
        print(f"Ошибка: {type(exc).__name__}" + (f", HTTP {status}" if status else ""))
        if retry_after:
            print(f"Retry-After: {retry_after}")
        return False
    finally:
        backend = editor.backend if editor else None
        if isinstance(backend, OpenAICompatBackend):
            await backend.client.close()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fallback", action="store_true", help="проверить только резерв")
    args = parser.parse_args()
    settings = Settings()
    profile = load_profiles()["thpoker"]
    if settings.llm_provider == "none":
        print("LLM_PROVIDER=none. Укажите провайдера для проверки редактора.")
        return 1

    primary = settings.model_copy(update={"llm_fallback_base_url": None})
    ok = args.fallback or await check("Основная модель", primary, profile)
    if settings.llm_provider == "openai" and settings.llm_fallback_base_url and settings.llm_fallback_model:
        fallback = primary.model_copy(update={
            "llm_base_url": settings.llm_fallback_base_url,
            "llm_model": settings.llm_fallback_model,
            "llm_api_key": settings.llm_fallback_api_key,
            "llm_proxy": settings.llm_fallback_proxy,
            "llm_rpm": settings.llm_fallback_rpm,
            "llm_vision": settings.llm_vision if settings.llm_fallback_vision is None else settings.llm_fallback_vision,
        })
        fallback_ok = await check("Резервная модель", fallback, profile)
        ok = ok and fallback_ok
    elif args.fallback or settings.llm_provider == "openai":
        print("Резервная модель не настроена.")
        return 1 if args.fallback else int(not ok)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
