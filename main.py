import asyncio
import logging
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import venv

from pathlib import Path
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

# =========================================================
# SETTINGS
# =========================================================

# यहां अपने Manager Bot का नया token डालें
MANAGER_BOT_TOKEN = "8400765415:AAEHYxy2k8xBdr2nvy8Y0H894C3__bBfKM0"

# यहां अपना Telegram numeric user ID डालें
ADMIN_ID = 8209524871

BASE_DIR = Path(__file__).resolve().parent
BOTS_DIR = BASE_DIR / "uploaded_bots"
DATABASE_FILE = BASE_DIR / "hosting_manager.db"

STATUS_CHECK_SECONDS = 30
STATUS_DELETE_SECONDS = 3600

MAX_PYTHON_FILE_SIZE = 10 * 1024 * 1024       # 10 MB
MAX_REQUIREMENTS_SIZE = 2 * 1024 * 1024      # 2 MB

BOTS_DIR.mkdir(parents=True, exist_ok=True)

# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("hosting_manager")

# =========================================================
# STATES
# =========================================================

WAITING_PYTHON_FILE = 1
WAITING_REQUIREMENTS_FILE = 2
WAITING_TOKEN = 3
WAITING_BOT_NAME = 4

# =========================================================
# RUNTIME MEMORY
# =========================================================

# bot_id -> subprocess.Popen
RUNNING_PROCESSES = {}

# user_id -> temporary upload data
USER_SESSIONS = {}

# =========================================================
# DATABASE
# =========================================================


def get_db():
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hosted_bots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            bot_name TEXT NOT NULL,
            bot_token TEXT NOT NULL,
            python_file TEXT NOT NULL,
            requirements_file TEXT,
            bot_folder TEXT NOT NULL,
            status TEXT DEFAULT 'stopped',
            process_id INTEGER DEFAULT 0,
            status_message_id INTEGER DEFAULT 0,
            last_status_sent INTEGER DEFAULT 0,
            last_check INTEGER DEFAULT 0,
            restart_count INTEGER DEFAULT 0,
            auto_restart INTEGER DEFAULT 1,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )

    conn.commit()
    conn.close()


def create_hosted_bot(
    user_id,
    bot_name,
    bot_token,
    python_file,
    requirements_file,
    bot_folder,
):
    now = int(time.time())

    conn = get_db()

    cursor = conn.execute(
        """
        INSERT INTO hosted_bots
        (
            user_id,
            bot_name,
            bot_token,
            python_file,
            requirements_file,
            bot_folder,
            status,
            process_id,
            status_message_id,
            last_status_sent,
            last_check,
            restart_count,
            auto_restart,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, 'stopped', 0, 0, 0, 0, 0, 1, ?, ?)
        """,
        (
            user_id,
            bot_name,
            bot_token,
            python_file,
            requirements_file,
            bot_folder,
            now,
            now,
        ),
    )

    bot_id = cursor.lastrowid
    conn.commit()
    conn.close()

    return bot_id


def get_bot(bot_id):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM hosted_bots WHERE id = ?",
        (bot_id,),
    ).fetchone()
    conn.close()
    return row


def get_user_bots(user_id):
    conn = get_db()
    rows = conn.execute(
        """
        SELECT * FROM hosted_bots
        WHERE user_id = ?
        ORDER BY id DESC
        """,
        (user_id,),
    ).fetchall()
    conn.close()
    return rows


def get_all_active_bots():
    conn = get_db()
    rows = conn.execute(
        """
        SELECT * FROM hosted_bots
        WHERE status IN ('running', 'starting')
        ORDER BY id ASC
        """
    ).fetchall()
    conn.close()
    return rows


def update_bot(bot_id, **fields):
    if not fields:
        return

    allowed_fields = {
        "bot_name",
        "bot_token",
        "python_file",
        "requirements_file",
        "bot_folder",
        "status",
        "process_id",
        "status_message_id",
        "last_status_sent",
        "last_check",
        "restart_count",
        "auto_restart",
        "updated_at",
    }

    clean_fields = {
        key: value
        for key, value in fields.items()
        if key in allowed_fields
    }

    if not clean_fields:
        return

    clean_fields["updated_at"] = int(time.time())

    set_clause = ", ".join(
        f"{key} = ?" for key in clean_fields.keys()
    )

    values = list(clean_fields.values())
    values.append(bot_id)

    conn = get_db()
    conn.execute(
        f"UPDATE hosted_bots SET {set_clause} WHERE id = ?",
        values,
    )
    conn.commit()
    conn.close()


def delete_bot_from_db(bot_id):
    conn = get_db()
    conn.execute(
        "DELETE FROM hosted_bots WHERE id = ?",
        (bot_id,),
    )
    conn.commit()
    conn.close()


# =========================================================
# UTILITY FUNCTIONS
# =========================================================


def now_text():
    return datetime.now().strftime("%d-%m-%Y %H:%M:%S")


def safe_name(value):
    value = value.strip()
    value = re.sub(r"[^a-zA-Z0-9_\- ]+", "", value)
    value = value.replace(" ", "_")
    return value[:50] or "my_bot"


def is_admin(user_id):
    return user_id == ADMIN_ID


def get_python_executable(bot_folder):
    bot_folder = Path(bot_folder)

    if os.name == "nt":
        return bot_folder / "venv" / "Scripts" / "python.exe"

    return bot_folder / "venv" / "bin" / "python"


def get_log_file(bot_folder):
    return Path(bot_folder) / "bot.log"


def mask_token(token):
    if not token:
        return "Not Set"

    if len(token) < 12:
        return "********"

    return token[:5] + "..." + token[-5:]


def process_is_running(bot_id):
    process = RUNNING_PROCESSES.get(bot_id)

    if process is None:
        bot = get_bot(bot_id)

        if not bot:
            return False

        process_id = bot["process_id"]

        if not process_id:
            return False

        try:
            if os.name == "nt":
                result = subprocess.run(
                    [
                        "tasklist",
                        "/FI",
                        f"PID eq {process_id}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )

                return str(process_id) in result.stdout

            os.kill(process_id, 0)
            return True

        except Exception:
            return False

    return process.poll() is None


async def safe_delete_message(bot, chat_id, message_id):
    if not message_id:
        return

    try:
        await bot.delete_message(
            chat_id=chat_id,
            message_id=message_id,
        )
    except Exception:
        pass


# =========================================================
# REQUIREMENTS INSTALLATION
# =========================================================


def create_virtual_environment(bot_folder):
    bot_folder = Path(bot_folder)
    venv_path = bot_folder / "venv"

    if venv_path.exists():
        return True, "Virtual environment पहले से मौजूद है।"

    try:
        builder = venv.EnvBuilder(
            with_pip=True,
            clear=False,
            symlinks=False,
            upgrade=False,
        )
        builder.create(str(venv_path))
        return True, "Virtual environment बन गया।"

    except Exception as error:
        logger.exception("Venv creation failed")
        return False, f"Venv बनाने में error: {error}"


def install_requirements(bot_folder, requirements_file):
    if not requirements_file:
        return True, "requirements.txt नहीं दिया गया।"

    requirements_path = Path(bot_folder) / requirements_file

    if not requirements_path.exists():
        return False, "requirements.txt फाइल नहीं मिली।"

    python_exe = get_python_executable(bot_folder)

    if not python_exe.exists():
        return False, "Virtual environment का Python नहीं मिला।"

    try:
        command = [
            str(python_exe),
            "-m",
            "pip",
            "install",
            "--upgrade",
            "pip",
        ]

        upgrade_result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=600,
        )

        if upgrade_result.returncode != 0:
            logger.warning(
                "pip upgrade warning: %s",
                upgrade_result.stderr[-2000:],
            )

        result = subprocess.run(
            [
                str(python_exe),
                "-m",
                "pip",
                "install",
                "-r",
                str(requirements_path),
            ],
            capture_output=True,
            text=True,
            timeout=1200,
        )

        if result.returncode != 0:
            error_text = result.stderr or result.stdout
            return False, (
                "requirements install नहीं हुआ:\n"
                + error_text[-3000:]
            )

        return True, "Requirements successfully install हो गए।"

    except subprocess.TimeoutExpired:
        return False, "Requirements install होने में बहुत समय लग गया।"

    except Exception as error:
        logger.exception("Requirements installation failed")
        return False, f"Requirements error: {error}"


# =========================================================
# BOT PROCESS MANAGEMENT
# =========================================================


def stop_bot_process(bot_id):
    process = RUNNING_PROCESSES.get(bot_id)

    if process:
        try:
            if process.poll() is None:
                process.terminate()

                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()

        except Exception:
            logger.exception("Process stop error")

        RUNNING_PROCESSES.pop(bot_id, None)

    bot = get_bot(bot_id)

    if bot and bot["process_id"]:
        try:
            if os.name == "nt":
                subprocess.run(
                    [
                        "taskkill",
                        "/PID",
                        str(bot["process_id"]),
                        "/T",
                        "/F",
                    ],
                    capture_output=True,
                    timeout=10,
                )
            else:
                os.kill(bot["process_id"], signal.SIGTERM)

        except Exception:
            pass

    update_bot(
        bot_id,
        status="stopped",
        process_id=0,
    )


def start_bot_process(bot_id):
    bot = get_bot(bot_id)

    if not bot:
        return False, "Bot database में नहीं मिला।"

    if process_is_running(bot_id):
        update_bot(bot_id, status="running")
        return True, "Bot पहले से चल रहा है।"

    bot_folder = Path(bot["bot_folder"])
    python_file = bot_folder / bot["python_file"]

    if not bot_folder.exists():
        return False, "Bot folder नहीं मिला।"

    if not python_file.exists():
        return False, "Python file नहीं मिली।"

    venv_ok, venv_message = create_virtual_environment(bot_folder)

    if not venv_ok:
        return False, venv_message

    requirements_ok, requirements_message = install_requirements(
        bot_folder,
        bot["requirements_file"],
    )

    if not requirements_ok:
        return False, requirements_message

    python_exe = get_python_executable(bot_folder)

    if not python_exe.exists():
        return False, "Virtual environment Python executable नहीं मिला।"

    log_file_path = get_log_file(bot_folder)

    try:
        log_file = open(
            log_file_path,
            "a",
            encoding="utf-8",
        )

        log_file.write(
            "\n\n==============================\n"
        )
        log_file.write(
            f"Bot started at {now_text()}\n"
        )
        log_file.write(
            "==============================\n"
        )
        log_file.flush()

        environment = os.environ.copy()

        # Uploaded bot को उसका token environment variable में मिलेगा
        environment["BOT_TOKEN"] = bot["bot_token"]
        environment["TELEGRAM_BOT_TOKEN"] = bot["bot_token"]

        process = subprocess.Popen(
            [
                str(python_exe),
                str(python_file),
            ],
            cwd=str(bot_folder),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP
                if os.name == "nt"
                else 0
            ),
        )

        RUNNING_PROCESSES[bot_id] = process

        update_bot(
            bot_id,
            status="running",
            process_id=process.pid,
        )

        return True, (
            "Bot successfully start हो गया।\n"
            f"PID: {process.pid}\n"
            f"Log: {log_file_path}"
        )

    except Exception as error:
        logger.exception("Bot start failed")
        update_bot(
            bot_id,
            status="error",
            process_id=0,
        )
        return False, f"Bot start error: {error}"


# =========================================================
# STATUS MESSAGE
# =========================================================


def make_status_text(bot):
    bot_id = bot["id"]
    running = process_is_running(bot_id)

    if running:
        status_icon = "🟢"
        status_text = "Running"
    else:
        status_icon = "🔴"
        status_text = "Stopped"

    process_id = bot["process_id"] or "N/A"

    return (
        f"{status_icon} <b>आपके Bot का Status</b>\n\n"
        f"🤖 <b>Bot Name:</b> {bot['bot_name']}\n"
        f"🆔 <b>Bot ID:</b> {bot_id}\n"
        f"📊 <b>Status:</b> {status_text}\n"
        f"⚙️ <b>Process ID:</b> {process_id}\n"
        f"🔄 <b>Auto Restart:</b> "
        f"{'Enabled' if bot['auto_restart'] else 'Disabled'}\n"
        f"🔁 <b>Restart Count:</b> {bot['restart_count']}\n"
        f"🕒 <b>Last Check:</b> {now_text()}\n\n"
        "ℹ️ यह status हर 30 सेकंड में check होता है।\n"
        "🗑️ यह status message 1 घंटे बाद अपने आप delete हो जाएगा।"
    )


async def send_new_status_message(application, bot_id):
    bot_record = get_bot(bot_id)

    if not bot_record:
        return

    user_id = bot_record["user_id"]
    old_message_id = bot_record["status_message_id"]

    await safe_delete_message(
        application.bot,
        user_id,
        old_message_id,
    )

    fresh_bot = get_bot(bot_id)

    if not fresh_bot:
        return

    text = make_status_text(fresh_bot)

    try:
        message = await application.bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode=ParseMode.HTML,
        )

        update_bot(
            bot_id,
            status_message_id=message.message_id,
            last_status_sent=int(time.time()),
        )

        # 1 घंटे बाद इस message को delete करेगा
        application.job_queue.run_once(
            delete_status_job,
            when=STATUS_DELETE_SECONDS,
            data={
                "chat_id": user_id,
                "message_id": message.message_id,
                "bot_id": bot_id,
            },
        )

    except Exception as error:
        logger.warning(
            "Status message send failed for bot %s: %s",
            bot_id,
            error,
        )


async def delete_status_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data

    await safe_delete_message(
        context.bot,
        data["chat_id"],
        data["message_id"],
    )

    bot_id = data.get("bot_id")

    if bot_id:
        bot_record = get_bot(bot_id)

        if bot_record and bot_record["status_message_id"] == data["message_id"]:
            update_bot(
                bot_id,
                status_message_id=0,
            )


# =========================================================
# PERIODIC HEALTH CHECK
# =========================================================


async def health_check_job(context: ContextTypes.DEFAULT_TYPE):
    application = context.application

    conn = get_db()
    rows = conn.execute(
        """
        SELECT * FROM hosted_bots
        WHERE status IN ('running', 'starting', 'error')
        ORDER BY id ASC
        """
    ).fetchall()
    conn.close()

    for bot in rows:
        bot_id = bot["id"]

        try:
            running = process_is_running(bot_id)

            if running:
                update_bot(
                    bot_id,
                    status="running",
                    last_check=int(time.time()),
                )

            else:
                update_bot(
                    bot_id,
                    status="stopped",
                    last_check=int(time.time()),
                )

                if bot["auto_restart"]:
                    current_bot = get_bot(bot_id)
                    restart_count = current_bot["restart_count"] + 1

                    update_bot(
                        bot_id,
                        status="starting",
                        restart_count=restart_count,
                    )

                    success, message = start_bot_process(bot_id)

                    logger.info(
                        "Auto restart bot %s: %s - %s",
                        bot_id,
                        success,
                        message,
                    )

            # हर 30 सेकंड में user को status भेजना
            await send_new_status_message(
                application,
                bot_id,
            )

        except Exception:
            logger.exception(
                "Health check failed for bot %s",
                bot_id,
            )


# =========================================================
# COMMANDS
# =========================================================


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    text = (
        "👋 <b>Python Hosting Manager में आपका स्वागत है।</b>\n\n"
        "इस Bot से आप अपनी Python Telegram Bot file चला सकते हैं।\n\n"
        "📤 /upload - नई Python Bot upload करें\n"
        "📋 /mybots - अपनी Bots देखें\n"
        "▶️ /startbot BOT_ID - Bot start करें\n"
        "⏹️ /stopbot BOT_ID - Bot stop करें\n"
        "🔄 /restartbot BOT_ID - Bot restart करें\n"
        "🗑️ /deletebot BOT_ID - Bot delete करें\n"
        "📄 /logs BOT_ID - Bot log की जानकारी\n"
        "❓ /help - Help\n\n"
        "⚠️ Bot Token किसी अन्य व्यक्ति को न भेजें।"
    )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📚 <b>Commands Help</b>\n\n"
        "/upload - नई Bot upload करें\n"
        "/mybots - अपनी सभी Bots देखें\n"
        "/startbot 1 - Bot ID 1 को start करें\n"
        "/stopbot 1 - Bot ID 1 को stop करें\n"
        "/restartbot 1 - Bot ID 1 को restart करें\n"
        "/deletebot 1 - Bot ID 1 को delete करें\n"
        "/logs 1 - Bot की log file देखें\n\n"
        "हर 30 सेकंड में Manager Bot आपके Bot का status check करता है।\n"
        "Status message 1 घंटे बाद अपने आप delete हो जाता है।"
    )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
    )


async def mybots_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    bots = get_user_bots(user_id)

    if not bots:
        await update.message.reply_text(
            "आपने अभी तक कोई Bot upload नहीं किया है।\n/upload दबाएँ।"
        )
        return

    lines = ["📋 <b>आपकी Bots</b>\n"]

    for bot in bots:
        running = process_is_running(bot["id"])
        status = "🟢 Running" if running else "🔴 Stopped"

        lines.append(
            f"🆔 <b>ID:</b> {bot['id']}\n"
            f"🤖 <b>Name:</b> {bot['bot_name']}\n"
            f"📊 <b>Status:</b> {status}\n"
            f"🔁 <b>Restarts:</b> {bot['restart_count']}\n"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


async def upload_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    USER_SESSIONS[user_id] = {
        "python_file": None,
        "requirements_file": None,
        "bot_token": None,
        "bot_name": None,
    }

    await update.message.reply_text(
        "📤 अपनी Python file भेजें।\n\n"
        "उदाहरण: `mybot.py`",
        parse_mode=ParseMode.MARKDOWN,
    )

    return WAITING_PYTHON_FILE


async def receive_python_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id
    document = update.message.document

    if not document:
        await update.message.reply_text(
            "कृपया केवल `.py` file भेजें।"
        )
        return WAITING_PYTHON_FILE

    file_name = document.file_name or ""

    if not file_name.lower().endswith(".py"):
        await update.message.reply_text(
            "❌ केवल `.py` file स्वीकार की जाती है।"
        )
        return WAITING_PYTHON_FILE

    if document.file_size and document.file_size > MAX_PYTHON_FILE_SIZE:
        await update.message.reply_text(
            "❌ Python file बहुत बड़ी है। अधिकतम सीमा 10 MB है।"
        )
        return WAITING_PYTHON_FILE

    session = USER_SESSIONS.get(user_id)

    if not session:
        await update.message.reply_text(
            "Session समाप्त हो गया। फिर से /upload करें।"
        )
        return ConversationHandler.END

    session["python_file"] = file_name
    session["python_file_id"] = document.file_id

    keyboard = [
        [
            InlineKeyboardButton(
                "📄 requirements.txt भेजूँगा",
                callback_data="requirements_yes",
            )
        ],
        [
            InlineKeyboardButton(
                "⏭️ requirements.txt नहीं है",
                callback_data="requirements_no",
            )
        ],
    ]

    await update.message.reply_text(
        "✅ Python file मिल गई।\n\n"
        "क्या आपके पास `requirements.txt` है?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN,
    )

    return WAITING_REQUIREMENTS_FILE


async def requirements_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    session = USER_SESSIONS.get(user_id)

    if not session:
        await query.edit_message_text(
            "Session समाप्त हो गया। फिर से /upload करें।"
        )
        return ConversationHandler.END

    if query.data == "requirements_no":
        session["requirements_file"] = None

        await query.edit_message_text(
            "ठीक है। अब अपना Bot Token भेजें।\n\n"
            "BotFather से मिला हुआ token भेजें।"
        )

        return WAITING_TOKEN

    session["waiting_requirements"] = True

    await query.edit_message_text(
        "अब अपनी `requirements.txt` file भेजें।",
        parse_mode=ParseMode.MARKDOWN,
    )

    return WAITING_REQUIREMENTS_FILE


async def receive_requirements_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id
    document = update.message.document

    if not document:
        await update.message.reply_text(
            "कृपया `requirements.txt` file भेजें।"
        )
        return WAITING_REQUIREMENTS_FILE

    file_name = document.file_name or ""

    if file_name.lower() != "requirements.txt":
        await update.message.reply_text(
            "❌ File का नाम बिल्कुल `requirements.txt` होना चाहिए।"
        )
        return WAITING_REQUIREMENTS_FILE

    if document.file_size and document.file_size > MAX_REQUIREMENTS_SIZE:
        await update.message.reply_text(
            "❌ requirements.txt बहुत बड़ी है।"
        )
        return WAITING_REQUIREMENTS_FILE

    session = USER_SESSIONS.get(user_id)

    if not session:
        await update.message.reply_text(
            "Session समाप्त हो गया। फिर से /upload करें।"
        )
        return ConversationHandler.END

    session["requirements_file"] = file_name
    session["requirements_file_id"] = document.file_id

    await update.message.reply_text(
        "✅ requirements.txt मिल गई।\n\n"
        "अब अपना Bot Token भेजें।"
    )

    return WAITING_TOKEN


async def receive_token(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id
    token = update.message.text.strip()

    session = USER_SESSIONS.get(user_id)

    if not session:
        await update.message.reply_text(
            "Session समाप्त हो गया। फिर से /upload करें।"
        )
        return ConversationHandler.END

    if not re.match(r"^\d+:[A-Za-z0-9_-]{20,}$", token):
        await update.message.reply_text(
            "❌ Token का format सही नहीं लग रहा है।\n"
            "BotFather से मिला पूरा token भेजें।"
        )
        return WAITING_TOKEN

    session["bot_token"] = token

    await update.message.reply_text(
        "अब अपने Bot का नाम भेजें।\n\n"
        "उदाहरण: Study Bot"
    )

    return WAITING_BOT_NAME


async def receive_bot_name(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id
    bot_name = safe_name(update.message.text)

    session = USER_SESSIONS.get(user_id)

    if not session:
        await update.message.reply_text(
            "Session समाप्त हो गया। फिर से /upload करें।"
        )
        return ConversationHandler.END

    session["bot_name"] = bot_name

    await update.message.reply_text(
        "⏳ आपकी files तैयार की जा रही हैं..."
    )

    try:
        bot_folder_name = (
            f"user_{user_id}_"
            f"{int(time.time())}_"
            f"{safe_name(bot_name)}"
        )

        bot_folder = BOTS_DIR / bot_folder_name
        bot_folder.mkdir(parents=True, exist_ok=True)

        telegram_file = await context.bot.get_file(
            session["python_file_id"]
        )

        python_file_path = bot_folder / session["python_file"]

        await telegram_file.download_to_drive(
            custom_path=str(python_file_path)
        )

        requirements_file_name = session.get("requirements_file")

        if requirements_file_name:
            requirements_telegram_file = await context.bot.get_file(
                session["requirements_file_id"]
            )

            requirements_path = bot_folder / requirements_file_name

            await requirements_telegram_file.download_to_drive(
                custom_path=str(requirements_path)
            )

        bot_id = create_hosted_bot(
            user_id=user_id,
            bot_name=bot_name,
            bot_token=session["bot_token"],
            python_file=session["python_file"],
            requirements_file=requirements_file_name,
            bot_folder=str(bot_folder),
        )

        success, message = start_bot_process(bot_id)

        if success:
            status_text = (
                "🟢 <b>आपका Bot शुरू हो गया है।</b>\n\n"
                f"🆔 Bot ID: <code>{bot_id}</code>\n"
                f"🤖 Name: <b>{bot_name}</b>\n\n"
                "🔄 Manager हर 30 सेकंड में status check करेगा।\n"
                "🗑️ Status message 1 घंटे बाद delete होगा।\n\n"
                "Commands:\n"
                f"/stopbot {bot_id}\n"
                f"/restartbot {bot_id}\n"
                f"/logs {bot_id}"
            )
        else:
            status_text = (
                "⚠️ Bot save हो गया, लेकिन start नहीं हो पाया।\n\n"
                f"🆔 Bot ID: {bot_id}\n"
                f"❌ Error:\n{message}\n\n"
                f"/restartbot {bot_id}\n"
                f"/logs {bot_id}"
            )

        await update.message.reply_text(
            status_text,
            parse_mode=ParseMode.HTML,
        )

        USER_SESSIONS.pop(user_id, None)

        return ConversationHandler.END

    except Exception as error:
        logger.exception("Upload failed")

        await update.message.reply_text(
            "❌ Upload करते समय error आया:\n\n"
            f"{error}"
        )

        return ConversationHandler.END


async def cancel_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id
    USER_SESSIONS.pop(user_id, None)

    await update.message.reply_text(
        "❌ Upload process cancel कर दिया गया।"
    )

    return ConversationHandler.END


# =========================================================
# BOT CONTROL COMMANDS
# =========================================================


def parse_bot_id(context):
    if not context.args:
        return None

    try:
        return int(context.args[0])
    except ValueError:
        return None


def user_owns_bot(user_id, bot_id):
    bot = get_bot(bot_id)

    if not bot:
        return False, None

    if bot["user_id"] != user_id and not is_admin(user_id):
        return False, bot

    return True, bot


async def startbot_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id
    bot_id = parse_bot_id(context)

    if not bot_id:
        await update.message.reply_text(
            "उपयोग: /startbot BOT_ID"
        )
        return

    allowed, bot = user_owns_bot(user_id, bot_id)

    if not allowed:
        await update.message.reply_text(
            "❌ यह Bot ID आपकी नहीं है या मौजूद नहीं है।"
        )
        return

    success, message = start_bot_process(bot_id)

    await update.message.reply_text(
        ("🟢 " if success else "❌ ") + message
    )


async def stopbot_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id
    bot_id = parse_bot_id(context)

    if not bot_id:
        await update.message.reply_text(
            "उपयोग: /stopbot BOT_ID"
        )
        return

    allowed, bot = user_owns_bot(user_id, bot_id)

    if not allowed:
        await update.message.reply_text(
            "❌ यह Bot ID आपकी नहीं है या मौजूद नहीं है।"
        )
        return

    stop_bot_process(bot_id)

    await update.message.reply_text(
        f"⏹️ Bot {bot_id} बंद कर दिया गया है।"
    )


async def restartbot_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id
    bot_id = parse_bot_id(context)

    if not bot_id:
        await update.message.reply_text(
            "उपयोग: /restartbot BOT_ID"
        )
        return

    allowed, bot = user_owns_bot(user_id, bot_id)

    if not allowed:
        await update.message.reply_text(
            "❌ यह Bot ID आपकी नहीं है या मौजूद नहीं है।"
        )
        return

    stop_bot_process(bot_id)
    await asyncio.sleep(2)

    success, message = start_bot_process(bot_id)

    await update.message.reply_text(
        ("🔄 " if success else "❌ ") + message
    )


async def deletebot_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id
    bot_id = parse_bot_id(context)

    if not bot_id:
        await update.message.reply_text(
            "उपयोग: /deletebot BOT_ID"
        )
        return

    allowed, bot = user_owns_bot(user_id, bot_id)

    if not allowed:
        await update.message.reply_text(
            "❌ यह Bot ID आपकी नहीं है या मौजूद नहीं है।"
        )
        return

    stop_bot_process(bot_id)

    try:
        folder = Path(bot["bot_folder"])

        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)

    except Exception:
        logger.exception("Folder delete failed")

    delete_bot_from_db(bot_id)

    await update.message.reply_text(
        f"🗑️ Bot {bot_id} और उसकी files delete कर दी गई हैं।"
    )


async def logs_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id
    bot_id = parse_bot_id(context)

    if not bot_id:
        await update.message.reply_text(
            "उपयोग: /logs BOT_ID"
        )
        return

    allowed, bot = user_owns_bot(user_id, bot_id)

    if not allowed:
        await update.message.reply_text(
            "❌ यह Bot ID आपकी नहीं है या मौजूद नहीं है।"
        )
        return

    log_path = get_log_file(bot["bot_folder"])

    if not log_path.exists():
        await update.message.reply_text(
            "अभी log file नहीं बनी है।"
        )
        return

    try:
        content = log_path.read_text(
            encoding="utf-8",
            errors="replace",
        )

        if len(content) > 3500:
            content = content[-3500:]

        await update.message.reply_text(
            "📄 <b>Last Bot Logs:</b>\n\n"
            f"<pre>{content}</pre>",
            parse_mode=ParseMode.HTML,
        )

    except Exception as error:
        await update.message.reply_text(
            f"Log पढ़ने में error: {error}"
        )


# =========================================================
# ADMIN COMMANDS
# =========================================================


async def admin_bots_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "❌ केवल Admin के लिए।"
        )
        return

    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM hosted_bots ORDER BY id DESC"
    ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text(
            "अभी कोई hosted Bot नहीं है।"
        )
        return

    lines = ["👑 <b>All Hosted Bots</b>\n"]

    for bot in rows:
        status = (
            "🟢 Running"
            if process_is_running(bot["id"])
            else "🔴 Stopped"
        )

        lines.append(
            f"🆔 {bot['id']} | "
            f"User: {bot['user_id']}\n"
            f"🤖 {bot['bot_name']} | {status}\n"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


async def admin_stop_all_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "❌ केवल Admin के लिए।"
        )
        return

    conn = get_db()
    rows = conn.execute(
        "SELECT id FROM hosted_bots"
    ).fetchall()
    conn.close()

    count = 0

    for row in rows:
        stop_bot_process(row["id"])
        count += 1

    await update.message.reply_text(
        f"⏹️ {count} Bots बंद कर दिए गए हैं।"
    )


# =========================================================
# STARTUP RESTORE
# =========================================================


async def restore_active_bots(application):
    bots = get_all_active_bots()

    if not bots:
        logger.info("Restore करने के लिए कोई active Bot नहीं है।")
        return

    logger.info(
        "Restoring %s active bots...",
        len(bots),
    )

    for bot in bots:
        try:
            update_bot(
                bot["id"],
                status="starting",
            )

            success, message = start_bot_process(bot["id"])

            logger.info(
                "Restore bot %s: %s - %s",
                bot["id"],
                success,
                message,
            )

        except Exception:
            logger.exception(
                "Restore failed for bot %s",
                bot["id"],
            )


async def post_init(application):
    init_db()

    await restore_active_bots(application)

    # हर 30 सेकंड में health check
    application.job_queue.run_repeating(
        health_check_job,
        interval=STATUS_CHECK_SECONDS,
        first=10,
        name="health_check",
    )

    logger.info("Health check job started.")


# =========================================================
# MAIN
# =========================================================


def main():
    if MANAGER_BOT_TOKEN == "YAHAN_MANAGER_BOT_TOKEN_DALEIN":
        print(
            "ERROR: MANAGER_BOT_TOKEN में अपना Manager Bot token डालें।"
        )
        return

    if ADMIN_ID == 123456789:
        print(
            "WARNING: ADMIN_ID में अपना Telegram numeric ID डालें।"
        )

    application = (
        Application.builder()
        .token(MANAGER_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    upload_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("upload", upload_command),
        ],
        states={
            WAITING_PYTHON_FILE: [
                MessageHandler(
                    filters.Document.ALL,
                    receive_python_file,
                ),
            ],
            WAITING_REQUIREMENTS_FILE: [
                CallbackQueryHandler(
                    requirements_callback,
                    pattern="^requirements_(yes|no)$",
                ),
                MessageHandler(
                    filters.Document.ALL,
                    receive_requirements_file,
                ),
            ],
            WAITING_TOKEN: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_token,
                ),
            ],
            WAITING_BOT_NAME: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_bot_name,
                ),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_command),
        ],
        allow_reentry=True,
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("mybots", mybots_command))

    application.add_handler(upload_conversation)

    application.add_handler(
        CommandHandler("startbot", startbot_command)
    )
    application.add_handler(
        CommandHandler("stopbot", stopbot_command)
    )
    application.add_handler(
        CommandHandler("restartbot", restartbot_command)
    )
    application.add_handler(
        CommandHandler("deletebot", deletebot_command)
    )
    application.add_handler(
        CommandHandler("logs", logs_command)
    )

    application.add_handler(
        CommandHandler("adminbots", admin_bots_command)
    )
    application.add_handler(
        CommandHandler("stopall", admin_stop_all_command)
    )

    logger.info("Hosting Manager Bot starting...")
    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
