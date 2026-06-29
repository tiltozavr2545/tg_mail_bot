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
            f"Ты — ассистент, который делает короткие выжимки входящих писем "
            f"на {cfg.summary_language} языке. ",
            "Кратко передай суть: о чём письмо, ключевые пункты и нужны ли действия "
            "от получателя (и какие). Используй маркированный список из 2–5 пунктов. ",
            "Если к письму приложены вложения (их содержимое дано в Markdown), "
            "учитывай их в выжимке и кратко отметь, что в каждом вложении. ",
        ]

        if cfg.owner_name:
            instruction.append(
                f"\n\nВладелец почты — {cfg.owner_name}. "
                "Тебя в первую очередь интересует всё, что относится лично к нему. Правила:\n"
                f"1. Если в письме или вложении есть данные сразу по многим людям "
                f"(например, список/таблица баллов или оценок всей группы), найди строку именно "
                f"«{cfg.owner_name}» — учитывай сокращения и разный порядок слов "
                f"(«Русаков Т.А.», «Тимофей Русаков», «Русаков Тимофей» и т.п.). "
                "В выжимке укажи ИМЕННО его результат: сколько баллов за какое задание "
                "(если в таблице указано название/номер задания) и из скольки максимум "
                "(если максимум указан). Если заданий несколько — перечисли по каждому.\n"
                "2. Если про владельца в письме напрямую ничего нет, всё равно сделай обычную "
                "краткую выжимку и в конце явно отметь, что лично его это, похоже, не касается.\n"
                "3. Не путай владельца с другими людьми. Если есть несколько похожих фамилий "
                "или данные неоднозначны — укажи это, а не угадывай.\n"
                "4. Никогда не выдумывай баллы/оценки: пиши только то, что реально есть в письме."
            )
            if cfg.owner_context:
                instruction.append(f"\nДополнительный контекст о владельце: {cfg.owner_context}")

        instruction.append(
            "\n\nБудь лаконичным, не выдумывай факты, которых нет в письме. "
            "Не добавляй вступлений вроде «Вот выжимка» — сразу по делу."
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
