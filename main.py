import asyncio
import json
import os
import re
import signal
import sys
import time
import traceback
from pathlib import Path
from datetime import datetime

import aiohttp
import pexpect

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
# CONFIGURATION
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "storage"))

SCRIPTS_DIR = DATA_DIR / "scripts"
LOGS_DIR = DATA_DIR / "logs"
CONFIG_FILE = DATA_DIR / "panel_config.json"

SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

# एक साथ अधिकतम कितने process चल सकते हैं
MAX_PROCESSES = int(os.getenv("MAX_PROCESSES", "5"))

# Live output कितने सेकंड में update हो
LIVE_UPDATE_SECONDS = 2

# एक message में अधिकतम कितने characters दिखाने हैं
MAX_OUTPUT_LENGTH = 3500

# केवल Python files की अनुमति
ALLOWED_EXTENSIONS = {".py"}

# खतरनाक filename characters रोकने के लिए
SAFE_FILENAME = re.compile(r"^[a-zA-Z0-9_.-]+$")


# =========================================================
# GLOBAL STATE
# =========================================================

sessions = {}
# Example:
# sessions["sender_bot.py"] = ProcessSession(...)

sessions_lock = asyncio.Lock()


# =========================================================
# HELPERS
# =========================================================

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def format_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))

    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)

    parts = []

    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")

    return " ".join(parts)


def clean_output(text: str) -> str:
    if not text:
        return "अभी तक कोई output नहीं आया।"

    if len(text) > MAX_OUTPUT_LENGTH:
        text = text[-MAX_OUTPUT_LENGTH:]

    return text


def script_path(filename: str) -> Path:
    return SCRIPTS_DIR / filename


def valid_script_name(filename: str) -> bool:
    if not filename:
        return False

    if not SAFE_FILENAME.fullmatch(filename):
        return False

    path = Path(filename)

    if path.name != filename:
        return False

    if path.suffix.lower() not in ALLOWED_EXTENSIONS:
        return False

    return True


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {
            "auto_start": [],
        }

    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {
            "auto_start": [],
        }


def save_config(config: dict):
    CONFIG_FILE.write_text(
        json.dumps(config, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def add_auto_start(filename: str):
    config = load_config()

    if filename not in config["auto_start"]:
        config["auto_start"].append(filename)

    save_config(config)


def remove_auto_start(filename: str):
    config = load_config()

    if filename in config["auto_start"]:
        config["auto_start"].remove(filename)

    save_config(config)


# =========================================================
# PROCESS SESSION
# =========================================================

class ProcessSession:
    def __init__(self, filename: str, chat_id: int):
        self.filename = filename
        self.chat_id = chat_id
        self.path = script_path(filename)

        self.process = None
        self.live_message = None

        self.output = ""
        self.started_at = time.time()
        self.finished_at = None

        self.exit_code = None
        self.running = False
        self.stopping = False

        self.reader_task = None
        self.updater_task = None

        self.log_file = LOGS_DIR / (
            f"{Path(filename).stem}_{int(self.started_at)}.log"
        )

    def append_output(self, text: str):
        if not text:
            return

        self.output += text

        # Output को बहुत बड़ा होने से रोकना
        if len(self.output) > 50000:
            self.output = self.output[-50000:]

        try:
            with self.log_file.open("a", encoding="utf-8", errors="ignore") as f:
                f.write(text)
        except Exception:
            pass

    def duration(self) -> float:
        end_time = self.finished_at or time.time()
        return end_time - self.started_at


# =========================================================
# TELEGRAM MESSAGE HELPERS
# =========================================================

async def safe_edit_message(session: ProcessSession, text: str, keyboard=None):
    if not session.live_message:
        return

    try:
        await session.live_message.edit_text(
            text=text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        # Telegram में "message is not modified" जैसी error को ignore करें
        if "not modified" not in str(exc).lower():
            pass


def session_keyboard(filename: str, running: bool = True):
    buttons = []

    if running:
        buttons.append([
            InlineKeyboardButton(
                "⛔ Stop",
                callback_data=f"stop|{filename}",
            ),
            InlineKeyboardButton(
                "🔄 Restart",
                callback_data=f"restart|{filename}",
            ),
        ])

    buttons.append([
        InlineKeyboardButton(
            "📊 Status",
            callback_data=f"status|{filename}",
        ),
        InlineKeyboardButton(
            "🗑 Log साफ करें",
            callback_data=f"clearlog|{filename}",
        ),
    ])

    return InlineKeyboardMarkup(buttons)


async def send_error(update: Update, text: str):
    await update.effective_message.reply_text(
        f"❌ {text}",
        parse_mode=ParseMode.HTML,
    )


# =========================================================
# PROCESS RUNNER
# =========================================================

async def read_process_output(session: ProcessSession):
    process = session.process

    try:
        while True:
            try:
                data = await asyncio.to_thread(
                    process.read_nonblocking,
                    size=4096,
                    timeout=0.5,
                )

                if data:
                    if isinstance(data, bytes):
                        data = data.decode("utf-8", errors="replace")

                    session.append_output(data)

            except pexpect.TIMEOUT:
                await asyncio.sleep(0.1)
                continue

            except pexpect.EOF:
                break

            except Exception as exc:
                session.append_output(
                    f"\n[Panel Reader Error] {type(exc).__name__}: {exc}\n"
                )
                break

    finally:
        try:
            process.close(force=False)
        except Exception:
            pass

        session.exit_code = process.exitstatus
        session.finished_at = time.time()
        session.running = False

        if session.exit_code is None:
            session.exit_code = 1

        await finish_session(session)


async def update_live_message(session: ProcessSession):
    last_text = ""

    while session.running:
        await asyncio.sleep(LIVE_UPDATE_SECONDS)

        if not session.live_message:
            continue

        output = clean_output(session.output)

        text = (
            f"🟢 <b>Script Running</b>\n\n"
            f"📄 <code>{session.filename}</code>\n"
            f"⏱️ Duration: <code>{format_duration(session.duration())}</code>\n\n"
            f"<pre>{output}</pre>"
        )

        if text == last_text:
            continue

        last_text = text

        await safe_edit_message(
            session,
            text,
            session_keyboard(session.filename, running=True),
        )


async def finish_session(session: ProcessSession):
    async with sessions_lock:
        # केवल उसी object को हटाएँ
        if sessions.get(session.filename) is session:
            del sessions[session.filename]

    output = clean_output(session.output)

    if session.exit_code == 0:
        title = "✅ Script Completed"
    else:
        title = "⚠️ Script Exited"

    text = (
        f"<b>{title}</b>\n\n"
        f"📄 <code>{session.filename}</code>\n"
        f"⏱️ Duration: <code>{format_duration(session.duration())}</code>\n"
        f"🔢 Exit Code: <code>{session.exit_code}</code>\n\n"
        f"<pre>{output}</pre>"
    )

    await safe_edit_message(
        session,
        text,
        session_keyboard(session.filename, running=False),
    )


async def start_script(
    filename: str,
    chat_id: int,
    bot,
    auto_start: bool = False,
):
    path = script_path(filename)

    if not path.exists():
        raise FileNotFoundError(
            f"फाइल नहीं मिली: {filename}"
        )

    if not valid_script_name(filename):
        raise ValueError(
            "केवल सुरक्षित नाम वाली .py फाइल चलाने की अनुमति है।"
        )

    async with sessions_lock:
        if filename in sessions and sessions[filename].running:
            raise RuntimeError(
                f"{filename} पहले से चल रही है।"
            )

        if len(sessions) >= MAX_PROCESSES:
            raise RuntimeError(
                f"अधिकतम {MAX_PROCESSES} scripts एक साथ चल सकती हैं।"
            )

        session = ProcessSession(filename, chat_id)
        sessions[filename] = session

    try:
        # -u से Python output तुरंत मिलता है
        session.process = pexpect.spawn(
            sys.executable,
            ["-u", str(path)],
            cwd=str(SCRIPTS_DIR),
            encoding=None,
            echo=True,
            timeout=0.5,
        )

        session.running = True

        session.live_message = await bot.send_message(
            chat_id=chat_id,
            text=(
                f"🚀 <b>Script Started</b>\n\n"
                f"📄 <code>{filename}</code>\n"
                f"⏱️ Duration: <code>0s</code>\n\n"
                f"<pre>Script शुरू हो रही है...</pre>"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=session_keyboard(filename, running=True),
        )

        session.reader_task = asyncio.create_task(
            read_process_output(session)
        )

        session.updater_task = asyncio.create_task(
            update_live_message(session)
        )

        return session

    except Exception:
        async with sessions_lock:
            sessions.pop(filename, None)

        raise


# =========================================================
# COMMANDS
# =========================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    text = (
        "🖥️ <b>Professional CMD Panel</b>\n\n"
        "एक साथ कई Python scripts चला सकते हैं।\n\n"
        "<b>मुख्य Commands:</b>\n"
        "/files - सभी Python files\n"
        "/run filename.py - Script चलाएँ\n"
        "/processes - चल रही scripts\n"
        "/input filename.py text - Input भेजें\n"
        "/stop filename.py - Script बंद करें\n"
        "/restart filename.py - Script दोबारा चलाएँ\n"
        "/stopall - सभी scripts बंद करें\n"
        "/autostart filename.py - Auto start ON\n"
        "/noautostart filename.py - Auto start OFF\n"
        "/logs filename.py - Script log\n"
        "/help - पूरी जानकारी"
    )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    text = (
        "📚 <b>CMD Panel Help</b>\n\n"
        "1️⃣ Python file upload करें।\n"
        "2️⃣ <code>/files</code> से नाम देखें।\n"
        "3️⃣ <code>/run filename.py</code> से चलाएँ।\n\n"
        "<b>Commands:</b>\n\n"
        "<code>/files</code>\n"
        "सभी uploaded Python files दिखाता है।\n\n"
        "<code>/run sender_bot.py</code>\n"
        "दूसरी Bot file चलाता है।\n\n"
        "<code>/processes</code>\n"
        "सभी running scripts दिखाता है।\n\n"
        "<code>/input sender_bot.py 123456</code>\n"
        "चल रही script में input भेजता है।\n\n"
        "<code>/stop sender_bot.py</code>\n"
        "एक script बंद करता है।\n\n"
        "<code>/restart sender_bot.py</code>\n"
        "Script को restart करता है।\n\n"
        "<code>/stopall</code>\n"
        "सभी scripts बंद करता है।\n\n"
        "<code>/autostart sender_bot.py</code>\n"
        "Panel restart होने पर script अपने-आप शुरू होगी।\n\n"
        "<code>/noautostart sender_bot.py</code>\n"
        "Auto start बंद करता है।\n\n"
        "<code>/logs sender_bot.py</code>\n"
        "Script का log दिखाता है।"
    )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
    )


async def files_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    files = sorted(
        p.name
        for p in SCRIPTS_DIR.iterdir()
        if p.is_file() and p.suffix.lower() == ".py"
    )

    if not files:
        await update.message.reply_text(
            "📂 अभी कोई Python file upload नहीं है।"
        )
        return

    lines = ["📂 <b>Python Files</b>\n"]

    for index, filename in enumerate(files, start=1):
        status = "🟢 Running" if filename in sessions else "⚪ Stopped"
        lines.append(
            f"{index}. <code>{filename}</code> — {status}"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


async def processes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    async with sessions_lock:
        current = list(sessions.values())

    if not current:
        await update.message.reply_text(
            "📊 अभी कोई script नहीं चल रही है।"
        )
        return

    lines = [
        f"📊 <b>Running Processes</b> "
        f"({len(current)}/{MAX_PROCESSES})\n"
    ]

    for session in current:
        lines.append(
            f"🟢 <code>{session.filename}</code>\n"
            f"⏱️ {format_duration(session.duration())}\n"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


async def run_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await send_error(
            update,
            "उदाहरण: <code>/run sender_bot.py</code>",
        )
        return

    filename = context.args[0]

    try:
        await start_script(
            filename=filename,
            chat_id=update.effective_chat.id,
            bot=context.bot,
        )

    except Exception as exc:
        await send_error(
            update,
            f"<code>{type(exc).__name__}: {exc}</code>",
        )


async def input_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    if len(context.args) < 2:
        await send_error(
            update,
            "उदाहरण: <code>/input sender_bot.py 123456</code>",
        )
        return

    filename = context.args[0]
    input_text = " ".join(context.args[1:])

    async with sessions_lock:
        session = sessions.get(filename)

    if not session or not session.running:
        await send_error(
            update,
            f"<code>{filename}</code> अभी नहीं चल रही है।",
        )
        return

    try:
        session.process.sendline(input_text)

        await update.message.reply_text(
            f"✅ Input <code>{filename}</code> में भेज दिया गया।",
            parse_mode=ParseMode.HTML,
        )

    except Exception as exc:
        await send_error(
            update,
            f"Input भेजने में समस्या: <code>{exc}</code>",
        )


async def stop_one_script(filename: str) -> bool:
    async with sessions_lock:
        session = sessions.get(filename)

    if not session or not session.running:
        return False

    session.stopping = True

    try:
        session.process.terminate(force=False)
    except Exception:
        try:
            session.process.kill(signal.SIGTERM)
        except Exception:
            pass

    return True


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await send_error(
            update,
            "उदाहरण: <code>/stop sender_bot.py</code>",
        )
        return

    filename = context.args[0]

    if await stop_one_script(filename):
        await update.message.reply_text(
            f"⛔ <code>{filename}</code> को stop करने का आदेश भेज दिया गया।",
            parse_mode=ParseMode.HTML,
        )
    else:
        await send_error(
            update,
            f"<code>{filename}</code> चल नहीं रही है।",
        )


async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await send_error(
            update,
            "उदाहरण: <code>/restart sender_bot.py</code>",
        )
        return

    filename = context.args[0]

    async with sessions_lock:
        old_session = sessions.get(filename)

    if old_session and old_session.running:
        await stop_one_script(filename)
        await asyncio.sleep(2)

    try:
        await start_script(
            filename=filename,
            chat_id=update.effective_chat.id,
            bot=context.bot,
        )

    except Exception as exc:
        await send_error(
            update,
            f"<code>{type(exc).__name__}: {exc}</code>",
        )


async def stopall_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    async with sessions_lock:
        filenames = list(sessions.keys())

    if not filenames:
        await update.message.reply_text(
            "📊 कोई script नहीं चल रही है।"
        )
        return

    stopped = 0

    for filename in filenames:
        if await stop_one_script(filename):
            stopped += 1

    await update.message.reply_text(
        f"⛔ {stopped} scripts को stop करने का आदेश भेज दिया गया।"
    )


async def autostart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await send_error(
            update,
            "उदाहरण: <code>/autostart sender_bot.py</code>",
        )
        return

    filename = context.args[0]

    if not valid_script_name(filename):
        await send_error(update, "गलत Python filename है।")
        return

    if not script_path(filename).exists():
        await send_error(update, "यह file मौजूद नहीं है।")
        return

    add_auto_start(filename)

    await update.message.reply_text(
        f"✅ <code>{filename}</code> Auto Start में जोड़ दी गई।",
        parse_mode=ParseMode.HTML,
    )


async def noautostart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await send_error(
            update,
            "उदाहरण: <code>/noautostart sender_bot.py</code>",
        )
        return

    filename = context.args[0]
    remove_auto_start(filename)

    await update.message.reply_text(
        f"✅ <code>{filename}</code> Auto Start से हटा दी गई।",
        parse_mode=ParseMode.HTML,
    )


async def logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await send_error(
            update,
            "उदाहरण: <code>/logs sender_bot.py</code>",
        )
        return

    filename = context.args[0]
    matching_logs = sorted(
        LOGS_DIR.glob(f"{Path(filename).stem}_*.log"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    if not matching_logs:
        await update.message.reply_text(
            "इस script का कोई log नहीं मिला।"
        )
        return

    latest_log = matching_logs[0]

    try:
        content = latest_log.read_text(
            encoding="utf-8",
            errors="replace",
        )
    except Exception as exc:
        await send_error(update, str(exc))
        return

    await update.message.reply_text(
        f"📄 <b>{filename} का Log</b>\n\n"
        f"<pre>{clean_output(content)}</pre>",
        parse_mode=ParseMode.HTML,
    )


# =========================================================
# FILE UPLOAD
# =========================================================

async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    document = update.message.document
    filename = document.file_name or ""

    if not valid_script_name(filename):
        await send_error(
            update,
            "केवल सुरक्षित नाम वाली .py फाइल upload करें।",
        )
        return

    destination = script_path(filename)

    try:
        telegram_file = await context.bot.get_file(document.file_id)
        await telegram_file.download_to_drive(custom_path=str(destination))

        await update.message.reply_text(
            f"✅ <code>{filename}</code> upload हो गई।\n\n"
            f"चलाने के लिए:\n"
            f"<code>/run {filename}</code>",
            parse_mode=ParseMode.HTML,
        )

    except Exception as exc:
        await send_error(
            update,
            f"Upload error: <code>{exc}</code>",
        )


# =========================================================
# CALLBACK BUTTONS
# =========================================================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if not is_admin(query.from_user.id):
        await query.answer("Access denied", show_alert=True)
        return

    await query.answer()

    data = query.data.split("|", 1)

    if len(data) != 2:
        return

    action, filename = data

    if not valid_script_name(filename):
        return

    if action == "stop":
        result = await stop_one_script(filename)

        await query.message.reply_text(
            f"⛔ {filename} stop command भेजा गया।"
            if result
            else f"⚪ {filename} चल नहीं रही है।"
        )

    elif action == "restart":
        async with sessions_lock:
            old_session = sessions.get(filename)

        if old_session and old_session.running:
            await stop_one_script(filename)
            await asyncio.sleep(2)

        try:
            await start_script(
                filename=filename,
                chat_id=query.message.chat_id,
                bot=context.bot,
            )

        except Exception as exc:
            await query.message.reply_text(
                f"❌ {type(exc).__name__}: {exc}"
            )

    elif action == "status":
        async with sessions_lock:
            session = sessions.get(filename)

        if not session:
            await query.message.reply_text(
                f"⚪ {filename} अभी बंद है।"
            )
            return

        await query.message.reply_text(
            f"🟢 <b>{filename}</b>\n"
            f"⏱️ {format_duration(session.duration())}\n"
            f"📊 Output: {len(session.output)} characters",
            parse_mode=ParseMode.HTML,
        )

    elif action == "clearlog":
        removed = 0

        for log_file in LOGS_DIR.glob(f"{Path(filename).stem}_*.log"):
            try:
                log_file.unlink()
                removed += 1
            except Exception:
                pass

        await query.message.reply_text(
            f"🗑 {removed} log files साफ कर दी गईं।"
        )


# =========================================================
# AUTO START
# =========================================================

async def auto_start_scripts(application: Application):
    await asyncio.sleep(3)

    config = load_config()
    auto_files = config.get("auto_start", [])

    for filename in auto_files:
        if not script_path(filename).exists():
            continue

        try:
            await start_script(
                filename=filename,
                chat_id=ADMIN_ID,
                bot=application.bot,
                auto_start=True,
            )

            await asyncio.sleep(2)

        except Exception as exc:
            try:
                await application.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        f"⚠️ Auto Start failed\n"
                        f"📄 {filename}\n"
                        f"❌ {type(exc).__name__}: {exc}"
                    ),
                )
            except Exception:
                pass


# =========================================================
# RENDER HEALTH SERVER
# =========================================================

async def health_handler(request):
    return aiohttp.web.Response(
        text="CMD Panel Bot is running",
        status=200,
    )


async def start_health_server():
    port = int(os.getenv("PORT", "10000"))

    app = aiohttp.web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)

    runner = aiohttp.web.AppRunner(app)
    await runner.setup()

    site = aiohttp.web.TCPSite(
        runner,
        host="0.0.0.0",
        port=port,
    )

    await site.start()

    print(f"Health server started on port {port}")


# =========================================================
# MAIN
# =========================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable नहीं मिला।"
        )

    if not ADMIN_ID:
        raise RuntimeError(
            "ADMIN_ID environment variable नहीं मिला।"
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("files", files_command))
    application.add_handler(CommandHandler("run", run_command))
    application.add_handler(CommandHandler("processes", processes_command))
    application.add_handler(CommandHandler("input", input_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("restart", restart_command))
    application.add_handler(CommandHandler("stopall", stopall_command))
    application.add_handler(CommandHandler("autostart", autostart_command))
    application.add_handler(CommandHandler("noautostart", noautostart_command))
    application.add_handler(CommandHandler("logs", logs_command))

    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            document_handler,
        )
    )

    application.add_handler(
        CallbackQueryHandler(callback_handler)
    )

    async def post_init(app: Application):
        await start_health_server()
        asyncio.create_task(auto_start_scripts(app))

    application.post_init = post_init

    print("Multi-Process CMD Panel Bot started...")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
