import os
import sys
import time
import asyncio
import logging
import signal
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import pexpect
from aiohttp import web

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "8400765415:AAEHYxy2k8xBdr2nvy8Y0H894C3__bBfKM0").strip()
ADMIN_ID_TEXT = os.getenv("ADMIN_ID", "8209524871").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")

if not ADMIN_ID_TEXT.isdigit():
    raise RuntimeError("ADMIN_ID must be numeric")

ADMIN_ID = int(ADMIN_ID_TEXT)

PORT = int(os.getenv("PORT", "10000"))

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "storage")))

SCRIPT_DIR = DATA_DIR / "scripts"
LOG_DIR = DATA_DIR / "logs"

SCRIPT_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

MAX_SCRIPT_SIZE = 5 * 1024 * 1024
MAX_OUTPUT_SIZE = 45000
LIVE_EDIT_INTERVAL = 1.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("telegram-cmd-panel")


# =========================================================
# SESSION
# =========================================================

@dataclass
class TerminalSession:
    chat_id: int
    file_name: str
    file_path: Path

    process: Optional[pexpect.spawn] = None

    started_at: float = field(default_factory=time.time)
    output: str = ""

    live_message_id: Optional[int] = None
    last_sent_output: str = ""

    running: bool = False
    waiting_for_input: bool = False
    stopping: bool = False

    reader_task: Optional[asyncio.Task] = None
    updater_task: Optional[asyncio.Task] = None


active_session: Optional[TerminalSession] = None
session_lock = asyncio.Lock()


# =========================================================
# HELPERS
# =========================================================

def is_admin(update: Update) -> bool:
    return bool(
        update.effective_user
        and update.effective_user.id == ADMIN_ID
    )


async def reject_user(update: Update):
    if update.effective_message:
        await update.effective_message.reply_text(
            "⛔ यह bot केवल admin के लिए है।"
        )


def safe_filename(name: str) -> Optional[str]:
    name = Path(name).name.strip()

    if not name:
        return None

    if len(name) > 150:
        return None

    if not name.lower().endswith(".py"):
        return None

    allowed = (
        "abcdefghijklmnopqrstuvwxyz"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "0123456789"
        "._-"
    )

    if any(char not in allowed for char in name):
        return None

    return name


def get_file_path(file_name: str) -> Path:
    return SCRIPT_DIR / file_name


def duration_text(seconds: float) -> str:
    seconds = int(max(0, seconds))

    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    seconds = seconds % 60

    if hours:
        return f"{hours}h {minutes}m {seconds}s"

    if minutes:
        return f"{minutes}m {seconds}s"

    return f"{seconds}s"


def output_tail(text: str, limit: int = 3500) -> str:
    if not text:
        return "Output की प्रतीक्षा है..."

    if len(text) <= limit:
        return text

    return (
        "… पुराना output ऊपर से हटाया गया …\n\n"
        + text[-limit:]
    )


def main_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📂 Files",
                    callback_data="files",
                ),
                InlineKeyboardButton(
                    "📊 Status",
                    callback_data="status",
                ),
            ],
            [
                InlineKeyboardButton(
                    "⌨️ Input Help",
                    callback_data="input_help",
                ),
                InlineKeyboardButton(
                    "▶️ Run Help",
                    callback_data="run_help",
                ),
            ],
            [
                InlineKeyboardButton(
                    "⛔ Stop",
                    callback_data="stop",
                ),
                InlineKeyboardButton(
                    "🔄 Restart",
                    callback_data="restart",
                ),
            ],
            [
                InlineKeyboardButton(
                    "ℹ️ Help",
                    callback_data="help",
                ),
            ],
        ]
    )


def running_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⌨️ Input Help",
                    callback_data="input_help",
                ),
                InlineKeyboardButton(
                    "⛔ Stop",
                    callback_data="stop",
                ),
            ],
            [
                InlineKeyboardButton(
                    "📊 Status",
                    callback_data="status",
                ),
                InlineKeyboardButton(
                    "🔄 Restart",
                    callback_data="restart",
                ),
            ],
        ]
    )


# =========================================================
# RENDER HEALTH SERVER
# =========================================================

async def health_handler(request):
    return web.json_response(
        {
            "status": "ok",
            "bot": "running",
            "script_running": bool(
                active_session and active_session.running
            ),
        }
    )


async def start_health_server(application: Application):
    health_app = web.Application()
    health_app.router.add_get("/", health_handler)
    health_app.router.add_get("/health", health_handler)

    runner = web.AppRunner(health_app)
    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT,
    )

    await site.start()

    application.bot_data["health_runner"] = runner

    logger.info("Health server started on port %s", PORT)


async def stop_health_server(application: Application):
    runner = application.bot_data.get("health_runner")

    if runner:
        await runner.cleanup()


# =========================================================
# LIVE OUTPUT MESSAGE
# =========================================================

async def update_live_output(
    context: ContextTypes.DEFAULT_TYPE,
    session: TerminalSession,
    force: bool = False,
):
    if not session.live_message_id:
        return

    visible_output = output_tail(session.output)

    if (
        not force
        and visible_output == session.last_sent_output
    ):
        return

    if session.running:
        status = "🟢 RUNNING"
    else:
        status = "🔴 FINISHED"

    if session.waiting_for_input:
        input_status = (
            "⌨️ <b>Input की प्रतीक्षा है</b>\n"
            "भेजें: <code>/input आपका_text</code>"
        )
    else:
        input_status = (
            "⌨️ Input भेजने के लिए "
            "<code>/input आपका_text</code> लिखें"
        )

    text = (
        f"🖥️ <b>Python Terminal</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📄 File: <code>{session.file_name}</code>\n"
        f"📌 Status: <b>{status}</b>\n"
        f"⏱️ Running: "
        f"{duration_text(time.time() - session.started_at)}\n"
        f"{input_status}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<pre>{visible_output}</pre>"
    )

    if len(text) > 4096:
        text = (
            f"🖥️ <b>Python Terminal</b>\n"
            f"📄 <code>{session.file_name}</code>\n"
            f"📌 <b>{status}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<pre>{visible_output[-3500:]}</pre>"
        )

    try:
        await context.bot.edit_message_text(
            chat_id=session.chat_id,
            message_id=session.live_message_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=running_keyboard()
            if session.running
            else main_keyboard(),
        )

        session.last_sent_output = visible_output

    except Exception as exc:
        if "message is not modified" not in str(exc).lower():
            logger.warning("Live output update failed: %s", exc)


async def output_updater(
    context: ContextTypes.DEFAULT_TYPE,
    session: TerminalSession,
):
    while session.running:
        await update_live_output(context, session)
        await asyncio.sleep(LIVE_EDIT_INTERVAL)

    await update_live_output(
        context,
        session,
        force=True,
    )


# =========================================================
# INPUT DETECTION
# =========================================================

def looks_like_input_prompt(output: str) -> bool:
    """
    यह केवल संकेत देता है कि script input माँग सकती है।
    /input command हमेशा काम करेगा, चाहे prompt detect हो या नहीं।
    """

    if not output:
        return False

    last_part = output[-300:].rstrip()

    prompt_endings = (
        ":",
        ": ",
        "?",
        "? ",
        ">",
        "> ",
        "number",
        "mobile",
        "phone",
        "otp",
        "password",
        "code",
        "enter",
        "input",
    )

    lowered = last_part.lower()

    return any(
        lowered.endswith(item)
        for item in prompt_endings
    )


# =========================================================
# SCRIPT READER
# =========================================================

async def read_script_output(
    context: ContextTypes.DEFAULT_TYPE,
    session: TerminalSession,
):
    process = session.process

    if not process:
        return

    log_path = LOG_DIR / (
        f"{int(session.started_at)}_{session.file_name}.log"
    )

    try:
        with log_path.open(
            "a",
            encoding="utf-8",
            errors="replace",
        ) as log:

            while True:
                try:
                    data = await asyncio.to_thread(
                        process.read_nonblocking,
                        4096,
                        0.25,
                    )

                    if data:
                        if isinstance(data, bytes):
                            text = data.decode(
                                "utf-8",
                                errors="replace",
                            )
                        else:
                            text = str(data)

                        session.output += text
                        session.output = session.output[
                            -MAX_OUTPUT_SIZE:
                        ]

                        log.write(text)
                        log.flush()

                        if looks_like_input_prompt(
                            session.output
                        ):
                            session.waiting_for_input = True

                except pexpect.TIMEOUT:
                    if not process.isalive():
                        break

                except pexpect.EOF:
                    break

                except Exception as exc:
                    session.output += (
                        "\n\n❌ READER ERROR:\n"
                        f"{exc}\n"
                    )
                    break

    finally:
        session.running = False
        session.waiting_for_input = False

        try:
            exit_code = process.exitstatus
            signal_code = process.signalstatus

            if exit_code == 0:
                session.output += (
                    "\n\n━━━━━━━━━━━━━━━━━━━━\n"
                    "✅ Script successfully finished.\n"
                    f"Exit code: {exit_code}\n"
                )
            else:
                session.output += (
                    "\n\n━━━━━━━━━━━━━━━━━━━━\n"
                    "❌ Script error के साथ बंद हुई।\n"
                    f"Exit code: {exit_code}\n"
                    f"Signal: {signal_code}\n"
                )

        except Exception:
            session.output += (
                "\n\n━━━━━━━━━━━━━━━━━━━━\n"
                "⚠️ Script समाप्त हो गई।\n"
            )


# =========================================================
# STOP SESSION
# =========================================================

async def stop_active_session(
    reason: str = "Admin stopped",
):
    global active_session

    session = active_session

    if not session:
        return False, "अभी कोई script नहीं चल रही है।"

    if session.stopping:
        return False, "Script पहले से stop हो रही है।"

    session.stopping = True
    session.running = False

    if session.process:
        try:
            if session.process.isalive():
                session.process.terminate(force=True)
        except Exception as exc:
            logger.warning("Stop error: %s", exc)

    session.output += (
        "\n\n━━━━━━━━━━━━━━━━━━━━\n"
        f"⛔ {reason}\n"
    )

    if session.reader_task:
        try:
            await asyncio.wait_for(
                session.reader_task,
                timeout=4,
            )
        except Exception:
            pass

    active_session = None

    return True, "Script stop कर दी गई।"


# =========================================================
# RUN SCRIPT
# =========================================================

async def run_script(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_name: str,
):
    global active_session

    file_name = safe_filename(file_name)

    if not file_name:
        await update.effective_message.reply_text(
            "❌ सही `.py` filename दें।\n\n"
            "उदाहरण:\n"
            "`/run test.py`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    file_path = get_file_path(file_name)

    if not file_path.exists():
        await update.effective_message.reply_text(
            f"❌ File नहीं मिली:\n`{file_name}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    async with session_lock:
        if active_session and active_session.running:
            await update.effective_message.reply_text(
                "⚠️ अभी एक script चल रही है।\n"
                "पहले `/stop` करें।"
            )
            return

        session = TerminalSession(
            chat_id=update.effective_chat.id,
            file_name=file_name,
            file_path=file_path,
        )

        active_session = session

    try:
        process = pexpect.spawn(
            sys.executable,
            ["-u", str(file_path)],
            cwd=str(SCRIPT_DIR),
            encoding=None,
            echo=True,
            timeout=0.25,
        )

        session.process = process
        session.running = True

        live_message = await update.effective_message.reply_text(
            f"🚀 Script शुरू हो गई:\n"
            f"📄 `{file_name}`\n\n"
            f"यदि script number या OTP माँगे तो भेजें:\n"
            f"`/input आपका_text`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=running_keyboard(),
        )

        session.live_message_id = live_message.message_id

        session.reader_task = asyncio.create_task(
            read_script_output(
                context,
                session,
            )
        )

        session.updater_task = asyncio.create_task(
            output_updater(
                context,
                session,
            )
        )

        await session.reader_task

        if session.updater_task:
            try:
                await session.updater_task
            except Exception:
                pass

        await update_live_output(
            context,
            session,
            force=True,
        )

        await update.effective_message.reply_text(
            f"🏁 Script समाप्त हो गई:\n"
            f"`{file_name}`\n\n"
            f"ऊपर अंतिम output और error देखें।",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_keyboard(),
        )

    except Exception as exc:
        logger.exception("Script run error")

        session.running = False
        session.output += (
            "\n\n❌ SCRIPT START ERROR:\n"
            f"{exc}\n"
        )

        await update.effective_message.reply_text(
            f"❌ Script शुरू नहीं हो सकी:\n"
            f"`{exc}`",
            parse_mode=ParseMode.MARKDOWN,
        )

    finally:
        if active_session is session:
            active_session = None


# =========================================================
# COMMANDS
# =========================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_admin(update):
        await reject_user(update)
        return

    await update.message.reply_text(
        "🖥️ <b>Python CMD Panel</b>\n\n"
        "यह bot आपकी Python script को Telegram से चलाता है।\n\n"
        "📂 Python file upload करें\n"
        "▶️ `/run filename.py`\n"
        "⌨️ `/input number_or_otp`\n"
        "⛔ `/stop`\n"
        "🔄 `/restart`\n"
        "📊 `/status`\n"
        "📁 `/files`\n"
        "🗑️ `/delete filename.py`\n"
        "ℹ️ `/help`",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(),
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_admin(update):
        await reject_user(update)
        return

    await update.message.reply_text(
        "ℹ️ <b>पूरा उपयोग तरीका</b>\n\n"
        "1. अपनी Python file Document के रूप में upload करें।\n"
        "2. `/files` से filename देखें।\n"
        "3. `/run filename.py` भेजें।\n"
        "4. Script number माँगे तो:\n"
        "`/input 9876543210`\n"
        "5. Script OTP माँगे तो:\n"
        "`/input 123456`\n"
        "6. Script का output live message में दिखेगा।\n"
        "7. Error होने पर traceback और exit code दिखेगा।\n\n"
        "Commands:\n"
        "/files\n"
        "/run filename.py\n"
        "/input text\n"
        "/status\n"
        "/stop\n"
        "/restart\n"
        "/delete filename.py\n\n"
        "⚠️ OTP और password Telegram chat history में दिखाई दे सकते हैं।",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(),
    )


async def files_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_admin(update):
        await reject_user(update)
        return

    files = sorted(
        [
            item.name
            for item in SCRIPT_DIR.iterdir()
            if item.is_file()
            and item.suffix.lower() == ".py"
        ]
    )

    if not files:
        await update.effective_message.reply_text(
            "📂 अभी कोई Python file upload नहीं है।"
        )
        return

    text = "📂 <b>Python Files</b>\n\n"

    for number, file_name in enumerate(files, start=1):
        text += f"{number}. <code>{file_name}</code>\n"

    text += (
        "\n▶️ Run करने के लिए:\n"
        "<code>/run filename.py</code>"
    )

    await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(),
    )


async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_admin(update):
        await reject_user(update)
        return

    session = active_session

    if not session or not session.running:
        await update.effective_message.reply_text(
            "🔴 कोई script नहीं चल रही है।",
            reply_markup=main_keyboard(),
        )
        return

    await update.effective_message.reply_text(
        f"🟢 <b>Script Running</b>\n\n"
        f"📄 File: <code>{session.file_name}</code>\n"
        f"⏱️ Time: "
        f"{duration_text(time.time() - session.started_at)}\n"
        f"⌨️ Input detected: "
        f"{'हाँ' if session.waiting_for_input else 'नहीं'}",
        parse_mode=ParseMode.HTML,
        reply_markup=running_keyboard(),
    )


async def input_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_admin(update):
        await reject_user(update)
        return

    session = active_session

    if not session or not session.running:
        await update.effective_message.reply_text(
            "⚠️ अभी कोई script नहीं चल रही है।"
        )
        return

    if not session.process:
        await update.effective_message.reply_text(
            "❌ Process उपलब्ध नहीं है।"
        )
        return

    input_text = update.message.text.partition(" ")[2]

    if not input_text:
        await update.message.reply_text(
            "उदाहरण:\n"
            "`/input 9876543210`\n\n"
            "या:\n"
            "`/input 123456`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    try:
        session.process.sendline(input_text)
        session.waiting_for_input = False

        await update.message.reply_text(
            "⌨️ Input script को भेज दिया गया।"
        )

    except Exception as exc:
        await update.message.reply_text(
            f"❌ Input भेजने में error:\n`{exc}`",
            parse_mode=ParseMode.MARKDOWN,
        )


async def stop_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_admin(update):
        await reject_user(update)
        return

    success, message = await stop_active_session(
        "Admin ने script stop की"
    )

    await update.message.reply_text(
        ("✅ " if success else "ℹ️ ") + message,
        reply_markup=main_keyboard(),
    )


async def restart_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_admin(update):
        await reject_user(update)
        return

    session = active_session

    if not session:
        await update.message.reply_text(
            "⚠️ Restart करने के लिए active script नहीं है।"
        )
        return

    file_name = session.file_name

    await stop_active_session("Restart requested")
    await asyncio.sleep(0.5)

    await run_script(
        update,
        context,
        file_name,
    )


async def delete_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_admin(update):
        await reject_user(update)
        return

    if not context.args:
        await update.message.reply_text(
            "उदाहरण:\n`/delete test.py`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    file_name = safe_filename(context.args[0])

    if not file_name:
        await update.message.reply_text(
            "❌ Invalid filename."
        )
        return

    if active_session and active_session.file_name == file_name:
        await update.message.reply_text(
            "⛔ यह file अभी चल रही है। पहले `/stop` करें।"
        )
        return

    file_path = get_file_path(file_name)

    if not file_path.exists():
        await update.message.reply_text(
            "❌ File नहीं मिली।"
        )
        return

    try:
        file_path.unlink()

        await update.message.reply_text(
            f"🗑️ File delete हो गई:\n`{file_name}`",
            parse_mode=ParseMode.MARKDOWN,
        )

    except Exception as exc:
        await update.message.reply_text(
            f"❌ Delete error:\n`{exc}`",
            parse_mode=ParseMode.MARKDOWN,
        )


# =========================================================
# DOCUMENT UPLOAD
# =========================================================

async def document_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_admin(update):
        await reject_user(update)
        return

    document = update.message.document

    if not document:
        return

    file_name = safe_filename(
        document.file_name or ""
    )

    if not file_name:
        await update.message.reply_text(
            "❌ केवल `.py` file upload करें।"
        )
        return

    if (
        document.file_size
        and document.file_size > MAX_SCRIPT_SIZE
    ):
        await update.message.reply_text(
            "❌ File 5 MB से बड़ी है।"
        )
        return

    destination = get_file_path(file_name)

    try:
        telegram_file = await context.bot.get_file(
            document.file_id
        )

        await telegram_file.download_to_drive(
            custom_path=str(destination)
        )

        await update.message.reply_text(
            f"✅ File upload हो गई:\n"
            f"`{file_name}`\n\n"
            f"चलाने के लिए:\n"
            f"`/run {file_name}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_keyboard(),
        )

    except Exception as exc:
        logger.exception("Upload error")

        await update.message.reply_text(
            f"❌ Upload error:\n`{exc}`",
            parse_mode=ParseMode.MARKDOWN,
        )


# =========================================================
# BUTTONS
# =========================================================

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        await query.edit_message_text(
            "⛔ केवल admin access कर सकता है।"
        )
        return

    action = query.data

    if action == "files":
        await files_command(update, context)

    elif action == "status":
        await status_command(update, context)

    elif action == "stop":
        success, message = await stop_active_session(
            "Admin ने button से stop किया"
        )

        await query.message.reply_text(
            ("✅ " if success else "ℹ️ ") + message,
            reply_markup=main_keyboard(),
        )

    elif action == "restart":
        await restart_command(update, context)

    elif action == "help":
        await help_command(update, context)

    elif action == "run_help":
        await query.message.reply_text(
            "▶️ Script चलाने का तरीका:\n\n"
            "`/run filename.py`\n\n"
            "उदाहरण:\n"
            "`/run login.py`",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif action == "input_help":
        await query.message.reply_text(
            "⌨️ बार-बार input देने के लिए:\n\n"
            "पहला number:\n"
            "`/input 9876543210`\n\n"
            "फिर OTP:\n"
            "`/input 123456`\n\n"
            "फिर अगला input:\n"
            "`/input अपना_text`",
            parse_mode=ParseMode.MARKDOWN,
        )


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.exception(
        "Unhandled bot error",
        exc_info=context.error,
    )


# =========================================================
# MAIN
# =========================================================

def main():
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(start_health_server)
        .post_shutdown(stop_health_server)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start_command)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CommandHandler("files", files_command)
    )

    application.add_handler(
        CommandHandler("status", status_command)
    )

    application.add_handler(
        CommandHandler("run", run_script_command)
    )

    application.add_handler(
        CommandHandler("input", input_command)
    )

    application.add_handler(
        CommandHandler("stop", stop_command)
    )

    application.add_handler(
        CommandHandler("restart", restart_command)
    )

    application.add_handler(
        CommandHandler("delete", delete_command)
    )

    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            document_handler,
        )
    )

    application.add_handler(
        CallbackQueryHandler(callback_handler)
    )

    application.add_error_handler(error_handler)

    logger.info("Bot starting...")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


async def run_script_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_admin(update):
        await reject_user(update)
        return

    if not context.args:
        await update.message.reply_text(
            "उदाहरण:\n`/run test.py`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await run_script(
        update,
        context,
        context.args[0],
    )


if __name__ == "__main__":
    main()
