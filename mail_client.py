"""IMAP-клиент: чтение непрочитанных писем из Gmail и пометка их прочитанными.

imap-tools синхронная, поэтому функции рассчитаны на вызов из потока
(через asyncio.to_thread) — IMAP-соединение открывается на каждый вызов,
что делает клиент устойчивым к разрывам соединения.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser

from imap_tools import AND, MailBox, MailMessageFlags

from attachments import ConvertedAttachment, convert_attachment
from config import Config

logger = logging.getLogger(__name__)

# Сколько символов тела письма максимум отдаём дальше (защита от гигантских писем).
MAX_BODY_CHARS = 12_000


@dataclass
class Email:
    uid: str
    subject: str
    from_: str
    from_name: str  # отображаемое имя отправителя (может быть пустым)
    date: str
    body: str
    attachments: list[ConvertedAttachment]


@dataclass
class RecentFile:
    uid: str
    idx: int  # индекс вложения внутри письма
    filename: str
    size: int


class _TextExtractor(HTMLParser):
    """Минимальный HTML→текст: выкидывает теги, script/style и схлопывает пробелы."""

    _SKIP = {"script", "style", "head", "title"}
    _BREAK = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skipping = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._SKIP:
            self._skipping = True
        elif tag in self._BREAK:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skipping = False
        elif tag in self._BREAK:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self._chunks.append(data)

    def get_text(self) -> str:
        return "".join(self._chunks)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 — кривой HTML не должен ронять бота
        return re.sub(r"<[^>]+>", " ", html)
    text = parser.get_text()
    # Схлопываем подряд идущие пустые строки и лишние пробелы.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*", "\n\n", text)
    return text.strip()


def _extract_body(msg) -> str:
    """Берём текстовую часть письма; если её нет — конвертируем HTML."""
    if msg.text and msg.text.strip():
        body = msg.text
    elif msg.html:
        body = html_to_text(msg.html)
    else:
        body = ""
    body = body.strip()
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n…[письмо обрезано]"
    return body


def _extract_attachments(msg) -> list[ConvertedAttachment]:
    """Сконвертировать все вложения письма в Markdown."""
    converted: list[ConvertedAttachment] = []
    for att in msg.attachments:
        converted.append(
            convert_attachment(att.filename or "", att.payload, att.content_type or "")
        )
    return converted


def fetch_unseen(cfg: Config) -> list[Email]:
    """Вернуть все непрочитанные письма из INBOX, НЕ помечая их прочитанными."""
    emails: list[Email] = []
    with MailBox(cfg.imap_host).login(
        cfg.gmail_address, cfg.gmail_app_password, initial_folder="INBOX"
    ) as mailbox:
        for msg in mailbox.fetch(AND(seen=False), mark_seen=False, bulk=True):
            emails.append(
                Email(
                    uid=msg.uid,
                    subject=msg.subject or "(без темы)",
                    from_=msg.from_ or "(неизвестный отправитель)",
                    from_name=(msg.from_values.name if msg.from_values else "") or "",
                    date=msg.date.strftime("%Y-%m-%d %H:%M") if msg.date else "",
                    body=_extract_body(msg),
                    attachments=_extract_attachments(msg),
                )
            )
    if emails:
        logger.info("Найдено непрочитанных писем: %d", len(emails))
    return emails


def mark_seen(cfg: Config, uids: list[str]) -> None:
    """Пометить письма с указанными UID как прочитанные."""
    if not uids:
        return
    with MailBox(cfg.imap_host).login(
        cfg.gmail_address, cfg.gmail_app_password, initial_folder="INBOX"
    ) as mailbox:
        mailbox.flag(uids, MailMessageFlags.SEEN, True)
    logger.info("Помечено прочитанными: %d", len(uids))


def check_connection(cfg: Config) -> None:
    """Проверка логина в IMAP — кидает исключение, если креды неверные."""
    with MailBox(cfg.imap_host).login(
        cfg.gmail_address, cfg.gmail_app_password, initial_folder="INBOX"
    ):
        pass


def _is_file_attachment(att) -> bool:
    """Настоящее вложение-файл (не встроенная картинка из подписи)."""
    if not att.filename:
        return False
    disp = (att.content_disposition or "").lower()
    if disp == "inline" and (att.content_type or "").startswith("image/"):
        return False
    return True


def list_recent_files(cfg: Config, scan_limit: int = 20, max_files: int = 10) -> list[RecentFile]:
    """Вернуть последние max_files вложений из почты (новые сверху)."""
    files: list[RecentFile] = []
    with MailBox(cfg.imap_host).login(
        cfg.gmail_address, cfg.gmail_app_password, initial_folder="INBOX"
    ) as mailbox:
        try:
            # Gmail-поиск только писем с вложениями — быстрее, чем тянуть все подряд.
            messages = mailbox.fetch(
                'X-GM-RAW "has:attachment"',
                reverse=True, limit=scan_limit, mark_seen=False, bulk=True,
            )
        except Exception:  # noqa: BLE001 — если сервер не Gmail, берём просто последние письма
            messages = mailbox.fetch(reverse=True, limit=scan_limit, mark_seen=False, bulk=True)
        for msg in messages:
            for idx, att in enumerate(msg.attachments):
                if not _is_file_attachment(att):
                    continue
                files.append(
                    RecentFile(
                        uid=msg.uid,
                        idx=idx,
                        filename=att.filename,
                        size=att.size or len(att.payload or b""),
                    )
                )
                if len(files) >= max_files:
                    return files
    return files


def fetch_attachment_bytes(cfg: Config, uid: str, idx: int) -> tuple[str, bytes] | None:
    """Скачать конкретное вложение (по uid письма и индексу). None, если не найдено."""
    with MailBox(cfg.imap_host).login(
        cfg.gmail_address, cfg.gmail_app_password, initial_folder="INBOX"
    ) as mailbox:
        for msg in mailbox.fetch(AND(uid=uid), mark_seen=False, bulk=True):
            atts = msg.attachments
            if 0 <= idx < len(atts):
                att = atts[idx]
                return (att.filename or f"file_{idx}", att.payload)
    return None
