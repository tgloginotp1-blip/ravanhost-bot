import os
import sys
import logging
import asyncio
import sqlite3
import zipfile
import shutil
import subprocess
import signal
from pathlib import Path

# Speed Optimization: High-performance uvloop setup
try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    print("⚡ Ultra-Fast uvloop event loop activated!")
except ImportError:
    print("⚠️ uvloop missing. Using standard asyncio policy.")

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import BadRequest

# Logging Setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Config & Limits
BOT_TOKEN = "8776155019:AAEfOduyraBmtAhxNsl2tNaAqnBJEKhxWLI"
BASE_DIR = Path(__file__).resolve().parent
HOSTED_BOTS_DIR = BASE_DIR / "hosted_bots"
DB_PATH = BASE_DIR / "ravan_host.db"

MAX_ZIP_SIZE = 50 * 1024 * 1024      # 50 MB
MAX_EXTRACT_SIZE = 200 * 1024 * 1024 # 200 MB
MAX_ARCHIVE_ENTRIES = 1000
MAX_BOTS_PER_USER = 2

# Database Setup
def init_db():
    HOSTED_BOTS_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bots (
            bot_id TEXT PRIMARY KEY,
            user_id INTEGER,
            project_path TEXT,
            entrypoint TEXT,
            status TEXT,
            pid INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_db()

def get_main_keyboard():
    keyboard = [
        [InlineKeyboardButton("📦 Host Bot", callback_data="btn_host_bot")],
        [InlineKeyboardButton("🤖 My Bots", callback_data="btn_my_bots")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_bot_control_keyboard(bot_id):
    keyboard = [
        [
            InlineKeyboardButton("▶️ Start", callback_data=f"start_{bot_id}"),
            InlineKeyboardButton("⏹ Stop", callback_data=f"stop_{bot_id}"),
        ],
        [
            InlineKeyboardButton("🔄 Restart", callback_data=f"restart_{bot_id}"),
            InlineKeyboardButton("📜 Logs", callback_data=f"logs_{bot_id}"),
        ],
        [
            InlineKeyboardButton("🗑 Delete", callback_data=f"delete_{bot_id}"),
            InlineKeyboardButton("⬅️ My Bots", callback_data="btn_my_bots"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "🔥 *Welcome to Ravan Host Manager!*\n\n"
        "Aap yahan apne High-Load Python Telegram Bots ko fast & secure tarike se manage kar sakte hain.\n"
        "Niche diye gaye options me se chunein:"
    )
    if update.message:
        await update.message.reply_text(welcome_text, parse_mode="Markdown", reply_markup=get_main_keyboard())
    elif update.callback_query:
        await update.callback_query.edit_message_text(welcome_text, parse_mode="Markdown", reply_markup=get_main_keyboard())

def is_process_running(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

async def async_db_query(query_func, *args):
    return await asyncio.to_thread(query_func, *args)

def _get_bot_info(bot_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, entrypoint, status, pid, project_path FROM bots WHERE bot_id = ?", (bot_id,))
    row = cursor.fetchone()
    conn.close()
    return row

async def safe_answer_query(query, text=None, show_alert=False):
    try:
        await query.answer(text=text, show_alert=show_alert)
    except BadRequest:
        pass

async def show_bot_panel(update: Update, bot_id: str, user_id: int, message_obj=None):
    row = await async_db_query(_get_bot_info, bot_id)

    if not row:
        text = "❌ Bot record nahi mila."
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=get_main_keyboard())
        return

    owner_id, entrypoint, db_status, pid, _ = row

    if owner_id != user_id:
        if update.callback_query:
            await safe_answer_query(update.callback_query, "⚠️ Aap is bot ke owner nahi hain!", show_alert=True)
        return

    current_status = "Running" if (pid and is_process_running(pid)) else "Stopped"

    panel_text = (
        f"🤖 *Bot Management Panel*\n\n"
        f"• *Bot ID:* `{bot_id}`\n"
        f"• *Status:* `{current_status}`\n"
        f"• *Entrypoint:* `{entrypoint}`"
    )

    keyboard = get_bot_control_keyboard(bot_id)

    if message_obj:
        try:
            await message_obj.edit_text(panel_text, parse_mode="Markdown", reply_markup=keyboard)
            return
        except Exception:
            pass

    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(panel_text, parse_mode="Markdown", reply_markup=keyboard)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise e

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer_query(query)
    data = query.data
    user_id = update.effective_user.id

    if data == "btn_host_bot":
        context.user_data['awaiting_zip'] = True
        await query.edit_message_text(
            "📦 *Host Bot - ZIP Upload*\n\n"
            "Kripya apne Python Bot ki `.zip` file bhejien.\n\n"
            "⚠️ *Limits:*\n"
            "• Max ZIP Size: 50 MB\n"
            "• Max Extracted Size: 200 MB\n"
            "• Max Bots per User: 2",
            parse_mode="Markdown"
        )
    elif data in ["btn_my_bots", "btn_back"]:
        await list_user_bots(update, context)

    elif data.startswith("manage_"):
        bot_id = data.replace("manage_", "")
        await show_bot_panel(update, bot_id, user_id)

    elif data.startswith("start_"):
        bot_id = data.replace("start_", "")
        await start_bot_process(update, context, bot_id)

    elif data.startswith("stop_"):
        bot_id = data.replace("stop_", "")
        await stop_bot_process(update, context, bot_id)

    elif data.startswith("restart_"):
        bot_id = data.replace("restart_", "")
        await stop_bot_process(update, context, bot_id, silent=True)
        await start_bot_process(update, context, bot_id)

    elif data.startswith("logs_"):
        bot_id = data.replace("logs_", "")
        await show_bot_logs(update, context, bot_id)

    elif data.startswith("delete_"):
        bot_id = data.replace("delete_", "")
        await delete_bot_process(update, context, bot_id)

def extract_zip_blocking(zip_path, project_dir):
    py_files = []
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        entries = zip_ref.infolist()
        if len(entries) > MAX_ARCHIVE_ENTRIES:
            raise Exception("Archive me bahut zyada files hain!")

        total_size = sum(e.file_size for e in entries)
        if total_size > MAX_EXTRACT_SIZE:
            raise Exception("Extracted files size 200MB limit se badi hai!")

        for member in entries:
            filename = member.filename
            if filename.startswith('/') or '..' in filename:
                raise Exception("Unsafe file path Detected!")

            zip_ref.extract(member, project_dir)
            if filename.endswith('.py') and not filename.startswith('__'):
                py_files.append(filename)
    return py_files

async def start_bot_process(update: Update, context: ContextTypes.DEFAULT_TYPE, bot_id: str):
    user_id = update.effective_user.id
    row = await async_db_query(_get_bot_info, bot_id)

    if not row or row[0] != user_id:
        return

    _, entrypoint, _, pid, project_path_str = row
    if pid and is_process_running(pid):
        await safe_answer_query(update.callback_query, "⚠️ Bot pehle se hi running hai!", show_alert=True)
        return

    status_msg = await update.callback_query.message.reply_text(
        f"⏳ *Bot ({bot_id}) Start ho raha hai...*\n[⏱ Elapsed: 0s] System site packages check ho rahe hain...",
        parse_mode="Markdown"
    )

    start_time = asyncio.get_event_loop().time()
    stop_timer = asyncio.Event()

    async def update_timer():
        while not stop_timer.is_set():
            await asyncio.sleep(2)
            if stop_timer.is_set():
                break
            elapsed = int(asyncio.get_event_loop().time() - start_time)
            try:
                await status_msg.edit_text(
                    f"⏳ *Bot ({bot_id}) Start ho raha hai...*\n[⏱ Elapsed: {elapsed}s] Dependencies check & start progress me hai...",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

    timer_task = asyncio.create_task(update_timer())

    try:
        project_dir = Path(project_path_str)
        venv_dir = project_dir / "venv"
        venv_python = venv_dir / "bin" / "python"
        req_file = project_dir / "requirements.txt"
        log_file = project_dir / "bot.log"

        def background_setup():
            # Create venv with Termux System Packages (Bypasses compilation!)
            if not venv_dir.exists():
                subprocess.run(
                    [sys.executable, "-m", "venv", "--system-site-packages", str(venv_dir)],
                    check=True
                )

            # Smart Cache & Fast Fast Installation (No redundant downloads)
            if req_file.exists():
                pip_path = venv_dir / "bin" / "pip"
                with open(log_file, "a") as lf:
                    lf.write("\n--- Starting Smart Fast Pip Install ---\n")
                    # Try local cache first to make restarts instant
                    subprocess.run(
                        [str(pip_path), "install", "--prefer-binary", "--no-index", "--find-links", str(HOSTED_BOTS_DIR), "-r", str(req_file)],
                        stdout=lf, stderr=lf
                    )
                    # Fallback to fast binary if not cached locally
                    subprocess.run(
                        [str(pip_path), "install", "--prefer-binary", "-r", str(req_file)],
                        stdout=lf, stderr=lf
                    )

        await asyncio.to_thread(background_setup)

        cmd_parts = entrypoint.split()
        if cmd_parts[0] == "python":
            cmd_parts[0] = str(venv_python)

        lf = open(log_file, "a")
        proc = subprocess.Popen(
            cmd_parts,
            cwd=str(project_dir),
            stdout=lf,
            stderr=lf,
            preexec_fn=os.setsid
        )

        await asyncio.sleep(2.0)
        stop_timer.set()
        await timer_task

        if not is_process_running(proc.pid):
            with open(log_file, "r") as f:
                err_output = "".join(f.readlines()[-15:])
            await status_msg.edit_text(
                f"❌ *Bot Startup Fail Ho Gaya!*\n\nLogs:\n```text\n{err_output[-1200:]}\n```",
                parse_mode="Markdown"
            )
            return

        def _update_pid():
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("UPDATE bots SET status = 'Running', pid = ? WHERE bot_id = ?", (proc.pid, bot_id))
            conn.commit()
            conn.close()

        await async_db_query(_update_pid)
        await show_bot_panel(update, bot_id, user_id, message_obj=status_msg)

    except Exception as e:
        stop_timer.set()
        await timer_task
        try:
            await status_msg.edit_text(f"❌ Start karne me error aaya: {str(e)}")
        except Exception:
            pass

async def stop_bot_process(update: Update, context: ContextTypes.DEFAULT_TYPE, bot_id: str, silent: bool = False):
    user_id = update.effective_user.id
    row = await async_db_query(_get_bot_info, bot_id)

    if not row or row[0] != user_id:
        return

    _, _, _, pid, _ = row
    if pid and is_process_running(pid):
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            pass

    def _update_stop():
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("UPDATE bots SET status = 'Stopped', pid = NULL WHERE bot_id = ?", (bot_id,))
        conn.commit()
        conn.close()

    await async_db_query(_update_stop)

    if not silent:
        await safe_answer_query(update.callback_query, "🛑 Bot Stop Ho Gaya!")
        await show_bot_panel(update, bot_id, user_id)

async def show_bot_logs(update: Update, context: ContextTypes.DEFAULT_TYPE, bot_id: str):
    row = await async_db_query(_get_bot_info, bot_id)
    if not row:
        return

    log_file = Path(row[4]) / "bot.log"
    logs_text = "📜 Log file khali hai."
    if log_file.exists():
        with open(log_file, "r") as f:
            lines = f.readlines()
            logs_text = "".join(lines[-35:]) if lines else logs_text

    if len(logs_text) > 3500:
        logs_text = logs_text[-3500:]

    keyboard = [[InlineKeyboardButton("⬅️ Control Panel", callback_data=f"manage_{bot_id}")]]
    
    try:
        await update.callback_query.edit_message_text(
            f"📜 *Logs for `{bot_id}`:*\n\n```text\n{logs_text}\n```",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise e

async def delete_bot_process(update: Update, context: ContextTypes.DEFAULT_TYPE, bot_id: str):
    user_id = update.effective_user.id
    await stop_bot_process(update, context, bot_id, silent=True)

    row = await async_db_query(_get_bot_info, bot_id)

    if row:
        project_dir = Path(row[4])
        if project_dir.exists():
            await asyncio.to_thread(shutil.rmtree, project_dir)
        def _delete_rec():
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM bots WHERE bot_id = ?", (bot_id,))
            conn.commit()
            conn.close()
        await async_db_query(_delete_rec)

    await safe_answer_query(update.callback_query, "🗑 Bot Delete Ho Gaya!")
    await list_user_bots(update, context)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_zip'):
        return

    user_id = update.effective_user.id
    doc = update.message.document

    def _check_count():
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM bots WHERE user_id = ?", (user_id,))
        count = cursor.fetchone()[0]
        conn.close()
        return count

    bot_count = await async_db_query(_check_count)

    if bot_count >= MAX_BOTS_PER_USER:
        await update.message.reply_text("❌ Aap maximum 2 bots hi host kar sakte hain!")
        return

    if not doc.file_name.endswith('.zip'):
        await update.message.reply_text("❌ Kripya sirf valid `.zip` file hi upload karein!")
        return

    if doc.file_size > MAX_ZIP_SIZE:
        await update.message.reply_text("❌ File size 50 MB limit se zyada hai!")
        return

    status_msg = await update.message.reply_text("📥 Fast downloading heavy ZIP...")

    bot_id = f"bot_{user_id}_{int(asyncio.get_event_loop().time())}"
    project_dir = HOSTED_BOTS_DIR / bot_id
    project_dir.mkdir(parents=True, exist_ok=True)
    zip_path = project_dir / "project.zip"

    tg_file = await context.bot.get_file(doc.file_id)
    await tg_file.download_to_drive(zip_path)

    try:
        py_files = await asyncio.to_thread(extract_zip_blocking, zip_path, project_dir)

        if zip_path.exists():
            os.remove(zip_path)

        if not py_files:
            await asyncio.to_thread(shutil.rmtree, project_dir)
            await status_msg.edit_text("❌ Zip ke andar koi `.py` file nahi mili!")
            return

        context.user_data['temp_bot'] = {
            'bot_id': bot_id,
            'path': str(project_dir),
            'py_files': py_files
        }
        context.user_data['awaiting_zip'] = False
        context.user_data['awaiting_entrypoint'] = True

        files_list = "\n".join([f"• `{f}`" for f in py_files[:15]])
        if len(py_files) > 15:
            files_list += f"\n... aur baaki {len(py_files)-15} files."

        await status_msg.edit_text(
            f"✅ *Zip Extraction Successful!*\n\n"
            f"*Python files found:*\n{files_list}\n\n"
            f"Ab apna entrypoint command bhejien, jaise:\n"
            f"`python main.py` ya `python folder/bot.py`",
            parse_mode="Markdown"
        )

    except Exception as e:
        if project_dir.exists():
            await asyncio.to_thread(shutil.rmtree, project_dir)
        await status_msg.edit_text(f"❌ Zip extraction me error aaya: {str(e)}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('awaiting_entrypoint'):
        text = update.message.text.strip()
        temp_bot = context.user_data.get('temp_bot')

        if not text.startswith("python "):
            await update.message.reply_text("❌ Invalid format! Entrypoint `python file.py` format me hona chahiye.")
            return

        selected_file = text.replace("python ", "").strip()
        project_dir = Path(temp_bot['path'])
        target_file = project_dir / selected_file

        if not target_file.exists() or not selected_file.endswith('.py'):
            await update.message.reply_text("❌ Bheji gayi Python file ZIP me nahi mil saki. Kripya path sahi likhein!")
            return

        user_id = update.effective_user.id
        def _insert_bot():
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO bots (bot_id, user_id, project_path, entrypoint, status) VALUES (?, ?, ?, ?, ?)",
                (temp_bot['bot_id'], user_id, temp_bot['path'], text, "Uploaded")
            )
            conn.commit()
            conn.close()

        await async_db_query(_insert_bot)

        context.user_data['awaiting_entrypoint'] = False
        await update.message.reply_text(
            f"🎉 *Bot Configured Successfully!*\n\n"
            f"*Bot ID:* `{temp_bot['bot_id']}`\n"
            f"*Entrypoint:* `{text}`\n\n"
            f"Aap *My Bots* menu me jaakar ise Start kar sakte hain.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )

async def list_user_bots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    def _fetch_user_bots():
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT bot_id, entrypoint, status, pid FROM bots WHERE user_id = ?", (user_id,))
        bots = cursor.fetchall()
        conn.close()
        return bots

    bots = await async_db_query(_fetch_user_bots)

    if not bots:
        text = "🤖 Aapke paas koi hosted bot nahi hai."
        keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="btn_back")]]
    else:
        text = "🤖 *Aapke Hosted Bots:*\n"
        keyboard = []
        for b_id, ep, st, pid in bots:
            curr_st = "Running" if (pid and is_process_running(pid)) else "Stopped"
            text += f"\n• *ID:* `{b_id}` | *Status:* `{curr_st}`"
            keyboard.append([InlineKeyboardButton(f"Manage {b_id}", callback_data=f"manage_{b_id}")])
        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="btn_back")])

    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise e
    elif update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=get_main_keyboard())

def main():
    if not BOT_TOKEN:
        print("Error: HOST_BOT_TOKEN environment variable set nahi hai!")
        sys.exit(1)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🚀 Ravan Host Ultra-Fast Live Timer Mode me start ho raha hai...")
    app.run_polling()

if __name__ == "__main__":
    main()
