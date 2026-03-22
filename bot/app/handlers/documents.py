"""
Обработчик загрузки документов (/upload).
Поддерживает PDF и фото. Извлекает дедлайны и расписание через pdfplumber + агент.
"""

import io
import logging
import tempfile
import os

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

logger = logging.getLogger(__name__)
router = Router()

# Состояния ожидания документа (user_id -> True)
_waiting_upload: set[int] = set()


@router.message(Command("upload"))
async def cmd_upload(message: Message) -> None:
    """Переводит пользователя в режим ожидания документа."""
    user_id = message.from_user.id
    _waiting_upload.add(user_id)
    await message.answer(
        "📎 Отправь PDF-файл или фото с расписанием/заданием.\n\n"
        "Я извлеку дедлайны и задания и предложу добавить их в календарь."
    )


@router.message(F.document)
async def handle_document(message: Message) -> None:
    """Обрабатывает загруженный документ (PDF)."""
    user_id = message.from_user.id

    # Принимаем документы только в режиме ожидания или PDF явно
    doc = message.document
    is_pdf = doc.mime_type == "application/pdf" or (
        doc.file_name and doc.file_name.lower().endswith(".pdf")
    )

    if not is_pdf and user_id not in _waiting_upload:
        return

    _waiting_upload.discard(user_id)

    await message.answer("⏳ Загружаю и анализирую документ...")

    # Скачиваем файл
    try:
        file = await message.bot.get_file(doc.file_id)
        file_bytes = await message.bot.download_file(file.file_path)
        pdf_bytes = file_bytes.read()
    except Exception as e:
        logger.error("Ошибка загрузки документа: %s", e)
        await message.answer(f"❌ Ошибка загрузки файла: {e}")
        return

    # Извлекаем текст из PDF
    extracted_text = await _extract_pdf_text(pdf_bytes)

    if not extracted_text.strip():
        await message.answer(
            "❌ Не удалось извлечь текст из документа. "
            "Убедись что PDF содержит текст, а не только изображения."
        )
        return

    # Передаём тексту агенту для анализа
    await _analyze_with_agent(message, user_id, extracted_text, doc.file_name or "document.pdf")


@router.message(F.photo)
async def handle_photo(message: Message) -> None:
    """Обрабатывает загруженное фото."""
    user_id = message.from_user.id

    if user_id not in _waiting_upload:
        return

    _waiting_upload.discard(user_id)
    await message.answer(
        "📷 Фото получено. К сожалению, точный OCR фото пока не поддерживается.\n"
        "Попробуй загрузить PDF-версию документа.\n\n"
        "Если у тебя есть текст из документа — просто вставь его в сообщение, я помогу создать события и задачи."
    )


async def _extract_pdf_text(pdf_bytes: bytes) -> str:
    """Извлекает текст из PDF с помощью pdfplumber."""
    import asyncio

    def _extract():
        try:
            import pdfplumber

            text_parts = []
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for page_num, page in enumerate(pdf.pages, 1):
                    page_text = page.extract_text()
                    if page_text:
                        text_parts.append(f"--- Страница {page_num} ---\n{page_text}")
            return "\n\n".join(text_parts)
        except ImportError:
            logger.error("pdfplumber не установлен")
            return ""
        except Exception as e:
            logger.error("Ошибка извлечения текста из PDF: %s", e)
            return ""

    return await asyncio.to_thread(_extract)


async def _analyze_with_agent(
    message: Message, user_id: int, text: str, filename: str
) -> None:
    """Передаёт извлечённый текст агенту для анализа."""
    from app.services.agent import run_agent
    from app.handlers.messages import handle_agent_response

    # Ограничиваем длину текста для промпта
    max_chars = 4000
    truncated = text[:max_chars]
    if len(text) > max_chars:
        truncated += f"\n... [текст обрезан, всего {len(text)} символов]"

    prompt = (
        f"Я загрузил документ: {filename}\n\n"
        f"Вот его содержимое:\n\n{truncated}\n\n"
        "Пожалуйста:\n"
        "1. Найди все дедлайны, задания, экзамены и события с датами\n"
        "2. Для каждого определи: это Задача (Google Tasks) или Событие (Google Calendar)?\n"
        "3. Предложи конкретный список что создать, с датами и временем\n"
        "4. Спроси моё подтверждение перед созданием"
    )

    try:
        response = await run_agent(user_id, prompt)
    except Exception as e:
        logger.error("Ошибка агента при анализе документа: %s", e)
        await message.answer(f"❌ Ошибка при анализе документа: {e}")
        return

    await handle_agent_response(message, response, user_id)
