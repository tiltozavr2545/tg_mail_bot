"""Загрузка и валидация конфигурации из переменных окружения (.env)."""

from __future__ import annotations

import sys
from dataclasses import dataclass

from dotenv import load_dotenv
import os

load_dotenv()


def _require(name: str) -> str:
    """Вернуть обязательную переменную окружения или завершить программу с понятной ошибкой."""
    value = os.getenv(name, "").strip()
    if not value:
        sys.exit(
            f"❌ Не задана обязательная переменная окружения {name}.\n"
            f"   Скопируй .env.example в .env и заполни значения."
        )
    return value


@dataclass(frozen=True)
class Config:
    # Telegram
    telegram_bot_token: str
    telegram_chat_id: int

    # Gmail / IMAP
    gmail_address: str
    gmail_app_password: str
    imap_host: str

    # Gemini
    gemini_api_key: str
    gemini_model: str

    # Поведение
    poll_interval_seconds: int
    summary_language: str

    # Персонализация
    owner_name: str  # ФИО владельца — бот выделяет всё, что относится лично к нему
    owner_context: str  # доп. контекст о владельце (необязательно)


def load_config() -> Config:
    chat_id_raw = _require("TELEGRAM_CHAT_ID")
    try:
        chat_id = int(chat_id_raw)
    except ValueError:
        sys.exit(f"❌ TELEGRAM_CHAT_ID должен быть числом, а не '{chat_id_raw}'.")

    try:
        poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
    except ValueError:
        poll_interval = 60
    poll_interval = max(15, poll_interval)  # не чаще раза в 15 секунд, чтобы не злить Gmail

    return Config(
        telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=chat_id,
        gmail_address=_require("GMAIL_ADDRESS"),
        gmail_app_password=_require("GMAIL_APP_PASSWORD").replace(" ", ""),
        imap_host=os.getenv("IMAP_HOST", "imap.gmail.com").strip(),
        gemini_api_key=_require("GEMINI_API_KEY"),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip(),
        poll_interval_seconds=poll_interval,
        summary_language=os.getenv("SUMMARY_LANGUAGE", "русском").strip(),
        owner_name=os.getenv("OWNER_NAME", "").strip(),
        owner_context=os.getenv("OWNER_CONTEXT", "").strip(),
    )
