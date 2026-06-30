"""Точка входа: Telegram-бот (aiogram) + фоновый цикл опроса почты.

Логика: раз в POLL_INTERVAL_SECONDS бот проверяет непрочитанные письма,
для каждого делает выжимку через Gemini и присылает её в Telegram,
затем помечает письмо прочитанным.
"""

from __future__ import annotations

import asyncio
import html
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import Config, load_config
from mail_client import (
    Email,
    check_connection,
    fetch_attachment_bytes,
    fetch_unseen,
    list_recent_files,
    mark_seen,
)
from summarizer import Summarizer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("tg_mail_bot")

router = Router()

# Гарантирует, что цикл опроса и команда /check не обрабатывают почту одновременно.
_mail_lock = asyncio.Lock()

# Telegram режет сообщения на 4096 символов.
TELEGRAM_LIMIT = 4096


def _sender_line(email: Email) -> str:
    """Первая строка ответа: почта отправителя и его имя (если есть)."""
    if email.from_name and email.from_name != email.from_:
        return f"{email.from_} ({email.from_name})"
    return email.from_


def _render_summary(email: Email, summary: str) -> str:
    text = f"{html.escape(_sender_line(email))}\n\n{html.escape(summary)}"
    if len(text) > TELEGRAM_LIMIT:
        text = text[: TELEGRAM_LIMIT - 1] + "…"
    return text


def _render_error(email: Email) -> str:
    return (
        f"{html.escape(_sender_line(email))}\n\n"
        f"Пришло письмо «{html.escape(email.subject)}», но не получилось сделать выжимку — "
        f"загляни в почту вручную."
    )


async def process_new_mail(bot: Bot, cfg: Config, summarizer: Summarizer) -> int:
    """Обработать все непрочитанные письма. Возвращает их количество."""
    async with _mail_lock:
        emails = await asyncio.to_thread(fetch_unseen, cfg)
        if not emails:
            return 0

        processed: list[str] = []
        for email in emails:
            try:
                summary = await summarizer.summarize(email)
                await bot.send_message(cfg.telegram_chat_id, _render_summary(email, summary))
                processed.append(email.uid)
            except Exception:  # noqa: BLE001
                logger.exception("Не удалось обработать письмо uid=%s", email.uid)
                try:
                    await bot.send_message(cfg.telegram_chat_id, _render_error(email))
                    processed.append(email.uid)  # уведомили об ошибке — помечаем прочитанным
                except Exception:  # noqa: BLE001
                    logger.exception("Не удалось отправить даже уведомление об ошибке")
                    # uid не помечаем — попробуем в следующем цикле

        await asyncio.to_thread(mark_seen, cfg, processed)
        return len(emails)


async def poll_loop(bot: Bot, cfg: Config, summarizer: Summarizer) -> None:
    logger.info("Цикл опроса почты запущен (интервал %d сек)", cfg.poll_interval_seconds)
    while True:
        try:
            await process_new_mail(bot, cfg, summarizer)
        except Exception:  # noqa: BLE001
            logger.exception("Ошибка в цикле опроса почты")
        await asyncio.sleep(cfg.poll_interval_seconds)


@router.message(CommandStart())
async def cmd_start(message: Message, cfg: Config) -> None:
    await message.answer(
        "👋 Привет! Я слежу за твоей почтой и присылаю краткие выжимки новых писем.\n\n"
        f"Проверяю ящик каждые {cfg.poll_interval_seconds} сек.\n"
        "Команды:\n"
        "• /check — проверить почту прямо сейчас\n"
        "• /files — последние файлы с почты\n"
        "• /help — помощь"
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "Я автоматически проверяю новые непрочитанные письма и присылаю их выжимки.\n\n"
        "• /check — проверить почту немедленно\n"
        "• /files — последние файлы с почты (нажми на файл — пришлю его)\n"
        "• /start — статус и список команд"
    )


@router.message(Command("check"))
async def cmd_check(
    message: Message, bot: Bot, cfg: Config, summarizer: Summarizer
) -> None:
    await message.answer("🔎 Проверяю почту…")
    count = await process_new_mail(bot, cfg, summarizer)
    if count == 0:
        await message.answer("📭 Непрочитанных писем нет.")


def _truncate_label(name: str, limit: int = 40) -> str:
    return name if len(name) <= limit else name[: limit - 1] + "…"


@router.message(Command("files"))
async def cmd_files(message: Message, cfg: Config) -> None:
    await message.answer("📂 Ищу последние файлы…")
    files = await asyncio.to_thread(list_recent_files, cfg)
    if not files:
        await message.answer("На почте не нашлось вложений.")
        return
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_truncate_label(f.filename),
                    callback_data=f"f:{f.uid}:{f.idx}",
                )
            ]
            for f in files
        ]
    )
    await message.answer("Последние файлы с почты — нажми, чтобы получить:", reply_markup=keyboard)


@router.callback_query(F.data.startswith("f:"))
async def on_file_click(callback: CallbackQuery, bot: Bot, cfg: Config) -> None:
    await callback.answer("Загружаю файл…")
    try:
        _, uid, idx = callback.data.split(":", 2)
        result = await asyncio.to_thread(fetch_attachment_bytes, cfg, uid, int(idx))
    except Exception:  # noqa: BLE001
        logger.exception("Ошибка при получении файла по callback %s", callback.data)
        result = None
    if not result:
        await bot.send_message(cfg.telegram_chat_id, "Не удалось получить этот файл 😕")
        return
    filename, payload = result
    await bot.send_document(
        cfg.telegram_chat_id, BufferedInputFile(payload, filename=filename)
    )


async def main() -> None:
    cfg = load_config()

    logger.info("Проверяю подключение к IMAP…")
    try:
        await asyncio.to_thread(check_connection, cfg)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            f"❌ Не удалось войти в Gmail по IMAP: {exc}\n"
            f"   Проверь GMAIL_ADDRESS и GMAIL_APP_PASSWORD (пароль приложения, не обычный)."
        ) from exc
    logger.info("IMAP OK.")

    bot = Bot(
        token=cfg.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    summarizer = Summarizer(cfg)

    dp = Dispatcher()
    dp.include_router(router)
    # Реагируем только на твой чат — и на сообщения, и на нажатия кнопок.
    dp.message.filter(F.chat.id == cfg.telegram_chat_id)
    dp.callback_query.filter(F.from_user.id == cfg.telegram_chat_id)

    poll_task = asyncio.create_task(poll_loop(bot, cfg, summarizer))

    try:
        await bot.send_message(cfg.telegram_chat_id, "✅ Бот запущен и следит за почтой.")
    except Exception:  # noqa: BLE001
        logger.exception(
            "Не удалось отправить стартовое сообщение — проверь TELEGRAM_CHAT_ID "
            "и что ты хотя бы раз написал боту /start."
        )

    logger.info("Запускаю Telegram-бота…")
    try:
        await dp.start_polling(bot, cfg=cfg, summarizer=summarizer)
    finally:
        poll_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit) as exc:
        if isinstance(exc, SystemExit) and exc.code:
            raise
        logger.info("Остановлено.")
