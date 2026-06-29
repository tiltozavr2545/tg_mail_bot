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
from aiogram.types import Message

from config import Config, load_config
from mail_client import Email, check_connection, fetch_unseen, mark_seen
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


def _render_summary(email: Email, summary: str) -> str:
    header = (
        f"📬 <b>Новое письмо</b>\n"
        f"👤 {html.escape(email.from_)}\n"
        f"📌 {html.escape(email.subject)}"
    )
    if email.date:
        header += f"\n🕒 {html.escape(email.date)}"
    if email.attachments:
        names = ", ".join(html.escape(a.filename) for a in email.attachments if a.filename)
        if names:
            header += f"\n📎 {names}"
    text = f"{header}\n\n{html.escape(summary)}"
    if len(text) > TELEGRAM_LIMIT:
        text = text[: TELEGRAM_LIMIT - 1] + "…"
    return text


def _render_error(email: Email) -> str:
    return (
        f"⚠️ <b>Пришло письмо, но не удалось сделать выжимку</b>\n"
        f"👤 {html.escape(email.from_)}\n"
        f"📌 {html.escape(email.subject)}\n\n"
        f"Открой письмо в почте вручную."
    )


async def process_new_mail(
    bot: Bot, cfg: Config, summarizer: Summarizer, ignore_uids: set[str]
) -> int:
    """Обработать новые непрочитанные письма (кроме тех, что были на момент запуска).

    Возвращает количество обработанных писем.
    """
    async with _mail_lock:
        emails = await asyncio.to_thread(fetch_unseen, cfg)
        # Пропускаем письма, которые уже были непрочитанными при старте бота.
        emails = [e for e in emails if e.uid not in ignore_uids]
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


async def poll_loop(
    bot: Bot, cfg: Config, summarizer: Summarizer, ignore_uids: set[str]
) -> None:
    logger.info("Цикл опроса почты запущен (интервал %d сек)", cfg.poll_interval_seconds)
    while True:
        try:
            await process_new_mail(bot, cfg, summarizer, ignore_uids)
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
        "• /help — помощь"
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "Я автоматически проверяю новые непрочитанные письма и присылаю их выжимки.\n\n"
        "• /check — проверить почту немедленно\n"
        "• /start — статус и список команд"
    )


@router.message(Command("check"))
async def cmd_check(
    message: Message, bot: Bot, cfg: Config, summarizer: Summarizer, ignore_uids: set[str]
) -> None:
    await message.answer("🔎 Проверяю почту…")
    count = await process_new_mail(bot, cfg, summarizer, ignore_uids)
    if count == 0:
        await message.answer("📭 Новых писем нет.")


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

    # Запоминаем письма, которые уже непрочитаны при старте, — их не трогаем,
    # обрабатываем только то, что придёт после запуска.
    existing = await asyncio.to_thread(fetch_unseen, cfg)
    ignore_uids: set[str] = {e.uid for e in existing}
    logger.info("Игнорирую %d непрочитанных писем, существовавших до запуска", len(ignore_uids))

    bot = Bot(
        token=cfg.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    summarizer = Summarizer(cfg)

    dp = Dispatcher()
    dp.include_router(router)
    # Обрабатываем сообщения только из твоего чата.
    dp.message.filter(F.chat.id == cfg.telegram_chat_id)

    poll_task = asyncio.create_task(poll_loop(bot, cfg, summarizer, ignore_uids))

    try:
        await bot.send_message(cfg.telegram_chat_id, "✅ Бот запущен и следит за почтой.")
    except Exception:  # noqa: BLE001
        logger.exception(
            "Не удалось отправить стартовое сообщение — проверь TELEGRAM_CHAT_ID "
            "и что ты хотя бы раз написал боту /start."
        )

    logger.info("Запускаю Telegram-бота…")
    try:
        await dp.start_polling(bot, cfg=cfg, summarizer=summarizer, ignore_uids=ignore_uids)
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
