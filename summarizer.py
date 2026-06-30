"""Краткая выжимка письма через Gemini 2.5 Flash (google-genai, async)."""

from __future__ import annotations

import logging

from google import genai
from google.genai import types

from config import Config
from mail_client import Email

logger = logging.getLogger(__name__)


def _build_prompt(email: Email) -> str:
    parts = [
        f"Отправитель: {email.from_}",
        f"Тема: {email.subject}",
        f"Дата: {email.date}",
        "",
        "Текст письма:",
        email.body or "(пустое тело письма)",
    ]
    for att in email.attachments:
        if att.markdown:
            parts.append("")
            parts.append(f"=== Вложение «{att.filename}» (содержимое в Markdown) ===")
            parts.append(att.markdown)
    return "\n".join(parts)


class Summarizer:
    def __init__(self, cfg: Config) -> None:
        self._client = genai.Client(api_key=cfg.gemini_api_key)
        self._model = cfg.gemini_model

        instruction = [
            f"Ты помогаешь владельцу почты быстро понимать входящие письма. "
            f"Пиши на {cfg.summary_language} языке живо и по-человечески, обычным сплошным "
            "текстом — без маркированных списков, заголовков, жирного шрифта и лишних "
            "переносов строк. Коротко, 1–3 предложения. Не указывай отправителя — его "
            "подставят отдельно. Не добавляй вступлений вроде «вот выжимка», сразу по делу.",
            "\n\nПравила:",
            "\n— Если это лекция, конспект, презентация, методичка или другой учебный "
            "материал — сообщи ТОЛЬКО факт, что прислали лекцию/материал, и её общую тему "
            "или название файла. НЕ пересказывай содержание и НЕ перечисляй, какие темы, "
            "разделы или вопросы внутри.",
        ]

        if cfg.tracked_people:
            people = ", ".join(cfg.tracked_people)
            instruction.append(
                "\n— Если в письме или вложении есть таблица или список с баллами, "
                "оценками или результатами, выпиши результаты только этих людей: "
                f"{people}. Для каждого укажи его баллы по каждому заданию и "
                "ОБЯЗАТЕЛЬНО из скольки максимум, если максимум указан (например, в "
                "заголовке столбца вроде «ЛР1 (из 10)» — тогда пиши «9 из 10»). "
                "Учитывай сокращения и разный порядок ФИО. Кого нет в таблице — "
                "просто пропусти, не упоминай."
            )

        instruction.append(
            "\n— В остальных случаях кратко передай суть: что хотят и нужно ли что-то "
            "сделать."
            "\n\nНикогда не выдумывай факты, баллы и оценки, которых нет в письме."
        )
        self._system_instruction = "".join(instruction)

    async def summarize(self, email: Email) -> str:
        """Вернуть текст выжимки. Кидает исключение при ошибке API."""
        response = await self._client.aio.models.generate_content(
            model=self._model,
            contents=_build_prompt(email),
            config=types.GenerateContentConfig(
                system_instruction=self._system_instruction,
                temperature=0.3,
                max_output_tokens=600,
                # Отключаем «размышления» — для выжимок не нужны, так быстрее и дешевле.
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        text = (response.text or "").strip()
        if not text:
            raise RuntimeError("Gemini вернул пустой ответ")
        return text
