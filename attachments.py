"""Конвертация вложений писем в Markdown через MarkItDown.

Поддерживаются PDF, DOCX, PPTX, XLSX/XLS, TXT/CSV/HTML и др. — всё, что умеет MarkItDown.
Картинки и слишком большие файлы пропускаются (текст не извлекается).
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass

from markitdown import MarkItDown

logger = logging.getLogger(__name__)

# Один переиспользуемый конвертер.
_md = MarkItDown()

# Не конвертируем вложения тяжелее этого (байты).
MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024
# Максимум символов извлечённого текста на одно вложение (защита от раздувания запроса к Gemini).
MAX_TEXT_CHARS = 8_000


@dataclass
class ConvertedAttachment:
    filename: str
    content_type: str
    markdown: str  # извлечённый текст; пустая строка, если извлечь не удалось


def _should_convert(filename: str, content_type: str, size: int) -> bool:
    if not filename:
        return False
    if size > MAX_ATTACHMENT_BYTES:
        logger.info("Вложение %s слишком большое (%d байт) — пропускаю", filename, size)
        return False
    # Картинки текстом не извлечь без LLM-описания — пропускаем (обычно это логотипы из подписи).
    if content_type.startswith("image/"):
        return False
    return True


def convert_attachment(filename: str, payload: bytes, content_type: str) -> ConvertedAttachment:
    """Конвертировать одно вложение в Markdown. Никогда не кидает исключение."""
    if not _should_convert(filename, content_type, len(payload)):
        return ConvertedAttachment(filename, content_type, "")

    ext = os.path.splitext(filename)[1]
    text = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tf:
            tf.write(payload)
            tmp_path = tf.name
        try:
            result = _md.convert(tmp_path)
            text = (result.text_content or "").strip()
        finally:
            os.unlink(tmp_path)
    except Exception:  # noqa: BLE001 — кривое вложение не должно ронять бота
        logger.exception("Не удалось конвертировать вложение %s", filename)
        return ConvertedAttachment(filename, content_type, "")

    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + "\n…[вложение обрезано]"
    logger.info("Вложение %s сконвертировано (%d символов)", filename, len(text))
    return ConvertedAttachment(filename, content_type, text)
