from __future__ import annotations

import asyncio
import logging
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from audio import AudioProcessingError, convert_and_measure
from config import Config
from roblox_client import AssetDownloadError, AssetUnavailable, RobloxClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)
ID_PATTERN = re.compile(r"^[1-9]\d*$")


@dataclass(slots=True)
class Stats:
    total: int = 0
    sent: int = 0
    unavailable: int = 0
    processing_errors: int = 0
    send_errors: int = 0
    duplicates: int = 0
    invalid: int = 0


@dataclass(slots=True)
class Job:
    token: str
    chat_id: int
    user_id: int
    ids_path: Path
    workspace: Path
    stats: Stats
    task: asyncio.Task[None] | None = None
    started: bool = False
    cancelled: bool = False


class JobRegistry:
    def __init__(self) -> None:
        self.by_token: dict[str, Job] = {}
        self.by_chat: dict[int, Job] = {}

    def add(self, job: Job) -> None:
        self.by_token[job.token] = job
        self.by_chat[job.chat_id] = job

    def remove(self, job: Job) -> None:
        self.by_token.pop(job.token, None)
        if self.by_chat.get(job.chat_id) is job:
            self.by_chat.pop(job.chat_id, None)
        shutil.rmtree(job.workspace, ignore_errors=True)


def registry(context: ContextTypes.DEFAULT_TYPE) -> JobRegistry:
    return context.application.bot_data["jobs"]


def parse_to_file(text: str, destination: Path) -> Stats:
    stats = Stats()
    seen: set[int] = set()
    with destination.open("w", encoding="utf-8") as output:
        for token in text.split():
            if not ID_PATTERN.fullmatch(token):
                stats.invalid += 1
                continue
            asset_id = int(token)
            if asset_id in seen:
                stats.duplicates += 1
                continue
            seen.add(asset_id)
            output.write(f"{asset_id}\n")
            stats.total += 1
    return stats


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "Пришлите Roblox audio asset ID текстом или UTF-8 TXT-файлом. "
        "ID можно разделять пробелами или переносами строк. После этого выберите OGG или MP3.\n\n"
        "Бот скачивает только доступные через официальный Roblox API аудио. "
        "Приватные, удалённые, отклонённые и не-аудио assets пропускаются."
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    job = registry(context).by_chat.get(update.effective_chat.id)
    if not job:
        await update.message.reply_text("Активной задачи нет.")
        return
    job.cancelled = True
    if job.task and not job.task.done():
        job.task.cancel()
    else:
        registry(context).remove(job)
    await update.message.reply_text("Задача отменена.")


async def accept_ids(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not update.effective_chat or not update.effective_user:
        return
    jobs = registry(context)
    if update.effective_chat.id in jobs.by_chat:
        await message.reply_text("В этом чате уже есть задача. Сначала завершите её или используйте /cancel.")
        return

    workspace = Path(tempfile.mkdtemp(prefix="roblox-audio-"))
    ids_path = workspace / "ids.txt"
    try:
        if message.document:
            name = (message.document.file_name or "").lower()
            if not name.endswith(".txt"):
                await message.reply_text("Поддерживаются только TXT-файлы.")
                shutil.rmtree(workspace, ignore_errors=True)
                return
            incoming = workspace / "incoming.txt"
            telegram_file = await context.bot.get_file(message.document.file_id)
            await telegram_file.download_to_drive(custom_path=incoming)
            try:
                text = incoming.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                await message.reply_text("TXT-файл должен быть в кодировке UTF-8.")
                shutil.rmtree(workspace, ignore_errors=True)
                return
            finally:
                incoming.unlink(missing_ok=True)
        else:
            text = message.text or ""

        stats = parse_to_file(text, ids_path)
        if not stats.total:
            await message.reply_text("Не найдено ни одного корректного положительного asset ID.")
            shutil.rmtree(workspace, ignore_errors=True)
            return

        token = uuid.uuid4().hex[:12]
        job = Job(token, update.effective_chat.id, update.effective_user.id, ids_path, workspace, stats)
        jobs.add(job)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("OGG", callback_data=f"fmt:{token}:ogg"),
            InlineKeyboardButton("MP3", callback_data=f"fmt:{token}:mp3"),
        ]])
        await message.reply_text(
            f"Принято уникальных ID: {stats.total}. Выберите формат:",
            reply_markup=keyboard,
        )
    except TelegramError:
        shutil.rmtree(workspace, ignore_errors=True)
        logger.exception("Could not receive ID list")
        await message.reply_text("Не удалось получить файл. Попробуйте ещё раз.")


async def choose_format(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data or not update.effective_user:
        return
    parts = query.data.split(":")
    if len(parts) != 3 or parts[0] != "fmt" or parts[2] not in {"ogg", "mp3"}:
        await query.answer("Некорректная кнопка.", show_alert=True)
        return
    job = registry(context).by_token.get(parts[1])
    if not job or job.user_id != update.effective_user.id:
        await query.answer("Эта задача устарела или принадлежит другому пользователю.", show_alert=True)
        return
    if job.started:
        await query.answer("Задача уже запущена.")
        return

    job.started = True
    await query.answer()
    await query.edit_message_text(f"Запускаю проверку {job.stats.total} ID в формате {parts[2].upper()}…")
    job.task = asyncio.create_task(process_job(context.application, job, parts[2]))


async def send_with_retry(application: Application, job: Job, output: Path, asset_id: int, fmt: str, caption: str) -> None:
    for attempt in range(3):
        try:
            with output.open("rb") as audio_file:
                if fmt == "mp3":
                    await application.bot.send_audio(
                        chat_id=job.chat_id, audio=audio_file,
                        filename=f"{asset_id}.mp3", title=str(asset_id), caption=caption,
                        read_timeout=120, write_timeout=120,
                    )
                else:
                    await application.bot.send_document(
                        chat_id=job.chat_id, document=audio_file,
                        filename=f"{asset_id}.ogg", caption=caption,
                        read_timeout=120, write_timeout=120,
                    )
            return
        except RetryAfter as exc:
            if attempt == 2:
                raise
            await asyncio.sleep(float(exc.retry_after) + 0.5)


async def process_job(application: Application, job: Job, fmt: str) -> None:
    config: Config = application.bot_data["config"]
    client = RobloxClient(config.roblox_api_key, config.request_timeout, config.max_retries, config.max_asset_bytes)
    status = await application.bot.send_message(job.chat_id, "Проверено: 0")
    processed = 0
    try:
        with job.ids_path.open("r", encoding="utf-8") as ids_file:
            for line in ids_file:
                if job.cancelled:
                    break
                asset_id = int(line)
                source = job.workspace / f"{asset_id}.source"
                output = job.workspace / f"{asset_id}.{fmt}"
                try:
                    await application.bot.send_chat_action(job.chat_id, ChatAction.UPLOAD_AUDIO)
                    url = await client.get_download_url(asset_id)
                    await client.download(url, source)
                    loudness = await convert_and_measure(
                        source, output, fmt, config.ffmpeg_path, config.ffprobe_path
                    )
                    caption = (
                        f"ID: {asset_id}\n"
                        f"Средняя громкость: {loudness.mean_dbfs} dBFS\n"
                        f"Пиковая громкость: {loudness.peak_dbfs} dBFS"
                    )
                    try:
                        await send_with_retry(application, job, output, asset_id, fmt, caption)
                        job.stats.sent += 1
                    except TelegramError:
                        job.stats.send_errors += 1
                        logger.exception("Telegram send failed for asset %s", asset_id)
                except AssetUnavailable:
                    job.stats.unavailable += 1
                except (AssetDownloadError, AudioProcessingError, OSError):
                    job.stats.processing_errors += 1
                    logger.warning("Asset %s could not be processed", asset_id, exc_info=True)
                finally:
                    source.unlink(missing_ok=True)
                    output.unlink(missing_ok=True)

                processed += 1
                if processed % config.progress_every == 0 or processed == job.stats.total:
                    try:
                        await status.edit_text(
                            f"Проверено: {processed}/{job.stats.total}\nОтправлено: {job.stats.sent}"
                        )
                    except TelegramError:
                        pass

        heading = "Задача отменена." if job.cancelled else "Проверка завершена."
        await application.bot.send_message(
            job.chat_id,
            f"{heading}\n"
            f"Уникальных корректных ID: {job.stats.total}\n"
            f"Отправлено: {job.stats.sent}\n"
            f"Недоступно или не аудио: {job.stats.unavailable}\n"
            f"Ошибки загрузки/конвертации: {job.stats.processing_errors}\n"
            f"Ошибки отправки: {job.stats.send_errors}\n"
            f"Дубликатов пропущено: {job.stats.duplicates}\n"
            f"Некорректных значений: {job.stats.invalid}"
        )
    except asyncio.CancelledError:
        job.cancelled = True
        raise
    finally:
        await client.close()
        application.bot_data["jobs"].remove(job)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled bot error", exc_info=context.error)


def main() -> None:
    config = Config.from_env()
    application = ApplicationBuilder().token(config.telegram_bot_token).build()
    application.bot_data["config"] = config
    application.bot_data["jobs"] = JobRegistry()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CallbackQueryHandler(choose_format, pattern=r"^fmt:"))
    application.add_handler(MessageHandler(filters.Document.ALL | (filters.TEXT & ~filters.COMMAND), accept_ids))
    application.add_error_handler(error_handler)
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
