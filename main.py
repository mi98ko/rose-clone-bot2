import sqlite3
import os
import re
import threading
from flask import Flask
from threading import Thread
from telegram import Update, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)

TOKEN = os.getenv("BOT_TOKEN")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))

# ---------------- DATABASE ----------------
DB_PATH = "bot.db"
db_lock = threading.Lock()

def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def db_exec(query, params=(), fetchone=False, fetchall=False, commit=False):
    """Thread-safe DB executor with its own connection per call."""
    with db_lock:
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute(query, params)
            result = None
            if fetchone:
                result = cur.fetchone()
            elif fetchall:
                result = cur.fetchall()
            if commit:
                conn.commit()
            return result
        finally:
            conn.close()

def db_init():
    with db_lock:
        conn = get_conn()
        cur = conn.cursor()

        # Filters: drop old inconsistent table, recreate cleanly
        cur.execute("DROP TABLE IF EXISTS filters")
        cur.execute("""
            CREATE TABLE filters (
                chat_id    INTEGER NOT NULL,
                keyword    TEXT    NOT NULL,
                reply      TEXT,
                file_id    TEXT,
                type       TEXT    NOT NULL DEFAULT 'text'
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS warns (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                count   INTEGER NOT NULL DEFAULT 0
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS approved_users (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS locks (
                chat_id   INTEGER NOT NULL,
                lock_type TEXT    NOT NULL,
                UNIQUE(chat_id, lock_type)
            )
        """)
        conn.commit()
        conn.close()

db_init()

# ---------------- KEEP ALIVE ----------------
app_web = Flask('')

@app_web.route('/')
def home():
    return "Bot is running!"

def run():
    app_web.run(host='0.0.0.0', port=5000)

def keep_alive():
    Thread(target=run).start()

keep_alive()

# ---------------- HELPERS ----------------
LINK_PATTERN = re.compile(
    r"(https?://|t\.me/|www\.|\.com\b|\.net\b)[\w\-./&?=%+#@!~:,;]*",
    re.IGNORECASE
)

EMOJI_PATTERN = re.compile(
    "[\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF"
    "\U00002702-\U000027B0"
    "\U000024C2-\U0001F251"
    "\U0001F926-\U0001F937"
    "\U00010000-\U0010FFFF"
    "\u2640-\u2642"
    "\u2600-\u2B55"
    "\u23cf\u23e9\u231a\u3030"
    "\ufe0f]+",
    re.UNICODE
)

VALID_LOCKS = {"links", "stickers", "gifs", "photos", "videos", "emoji", "all"}

async def log_action(context, text):
    if LOG_CHANNEL_ID != 0:
        try:
            await context.bot.send_message(LOG_CHANNEL_ID, text)
        except Exception:
            pass

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        return any(a.user.id == user_id for a in admins)
    except Exception:
        return False

def get_warn_count(chat_id, user_id):
    row = db_exec(
        "SELECT count FROM warns WHERE chat_id=? AND user_id=?",
        (chat_id, user_id), fetchone=True
    )
    return row[0] if row else 0

def set_warn_count(chat_id, user_id, count):
    db_exec("DELETE FROM warns WHERE chat_id=? AND user_id=?", (chat_id, user_id), commit=True)
    if count > 0:
        db_exec("INSERT INTO warns VALUES (?, ?, ?)", (chat_id, user_id, count), commit=True)

def is_approved(chat_id, user_id):
    row = db_exec(
        "SELECT 1 FROM approved_users WHERE chat_id=? AND user_id=?",
        (chat_id, user_id), fetchone=True
    )
    return row is not None

def fmt_user(user):
    username = f"@{user.username}" if user.username else "N/A"
    return user.first_name, username, user.id

# ─────────────────────────────────────────
# LOCK DB HELPERS
# ─────────────────────────────────────────

def is_locked(chat_id, lock_type):
    row = db_exec(
        "SELECT 1 FROM locks WHERE chat_id=? AND lock_type=?",
        (chat_id, lock_type), fetchone=True
    )
    return row is not None

def set_lock(chat_id, lock_type):
    db_exec(
        "INSERT OR IGNORE INTO locks (chat_id, lock_type) VALUES (?, ?)",
        (chat_id, lock_type), commit=True
    )

def unset_lock(chat_id, lock_type):
    db_exec(
        "DELETE FROM locks WHERE chat_id=? AND lock_type=?",
        (chat_id, lock_type), commit=True
    )

async def get_admin_ids(context, chat_id):
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        return {a.user.id for a in admins}
    except Exception:
        return set()

# ─────────────────────────────────────────
# FILTER DB HELPERS
# ─────────────────────────────────────────

def filter_save(chat_id, keyword, reply, file_id, ftype):
    db_exec(
        "DELETE FROM filters WHERE chat_id=? AND keyword=?",
        (chat_id, keyword), commit=True
    )
    db_exec(
        "INSERT INTO filters (chat_id, keyword, reply, file_id, type) VALUES (?, ?, ?, ?, ?)",
        (chat_id, keyword, reply, file_id, ftype), commit=True
    )

def filter_delete(chat_id, keyword):
    db_exec(
        "DELETE FROM filters WHERE chat_id=? AND keyword=?",
        (chat_id, keyword), commit=True
    )

def filter_list(chat_id):
    return db_exec(
        "SELECT keyword, type FROM filters WHERE chat_id=?",
        (chat_id,), fetchall=True
    ) or []

def filter_match(chat_id, text_lower):
    rows = db_exec(
        "SELECT keyword, reply, file_id, type FROM filters WHERE chat_id=?",
        (chat_id,), fetchall=True
    ) or []
    for keyword, reply, file_id, ftype in rows:
        if keyword in text_lower:
            return reply, file_id, ftype
    return None, None, None

# ---------------- HELP TEXT ----------------
HELP_TEXT = (
    "🤖 *Bot Commands*\n\n"
    "*👮 Admin:*\n"
    "`/ban` or `!ban` — Ban a user (reply)\n"
    "`/unban` or `!unban` — Unban a user (reply or ID)\n"
    "`/kick` or `!kick` — Kick a user (reply)\n"
    "`/mute` or `!mute` — Mute a user (reply)\n"
    "`/warn` or `!warn` — Warn a user (reply)\n"
    "`/resetwarn` or `!resetwarn` — Reset warnings (reply)\n"
    "`/approve` or `!approve` — Approve user (bypass anti-link)\n"
    "`/unapprove` or `!unapprove` — Remove approval\n\n"
    "*🔒 Locks:*\n"
    "`/lock <type>` — Lock a content type\n"
    "`/unlock <type>` — Unlock a content type\n"
    "_Types:_ `links` `stickers` `gifs` `photos` `videos` `emoji` `all`\n\n"
    "*🔍 Filters:*\n"
    "`/filter <keyword> <reply>` — Add text filter\n"
    "`/filter <keyword>` (reply to photo/video) — Add media filter\n"
    "`/stop <keyword>` — Delete filter\n"
    "`/filters` — List all filters\n\n"
    "*ℹ️ Info:*\n"
    "`/info` or `!info` — Show user info (reply)\n"
    "`/warnings` or `!warnings` — Show warning count (reply)\n"
)

# ---------------- START ----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("📩 Please message me in private to start.")
        return
    btn = InlineKeyboardMarkup([[InlineKeyboardButton("📜 Commands", callback_data="show_help")]])
    await update.message.reply_text(
        "✨ 𝙒𝙀𝙇𝘾𝙊𝙈𝙀 𝙏𝙊 𝙋𝙀𝘼𝘾𝙀 𝙭 𝙍𝙊𝙎𝙀 𝘽𝙊𝙏 ✨\n\n"
        "🤖 Your Advanced Telegram Moderation Bot\n\n"
        "📌 Type /help to see all commands",
        reply_markup=btn
    )

# ---------------- HELP ----------------
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("📩 Check my DM for commands list.")
        return
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")

# ---------------- CALLBACK BUTTON ----------------
async def callback_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "show_help":
        await query.message.reply_text(HELP_TEXT, parse_mode="Markdown")
    elif query.data.startswith("unmute_"):
        user_id = int(query.data.split("_")[1])
        await context.bot.restrict_chat_member(
            query.message.chat.id, user_id,
            ChatPermissions(can_send_messages=True)
        )
        await query.edit_message_text("🔊 *User Unmuted!*", parse_mode="Markdown")

# ---------------- BAN ----------------
async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("⚠️ Reply to a user to ban them.")
        return
    user = update.message.reply_to_message.from_user
    name, username, uid = fmt_user(user)
    await context.bot.ban_chat_member(update.effective_chat.id, uid)
    await update.message.reply_text(
        f"🚫 *Banned!*\n👤 Username: {username}\n🆔 ID: `{uid}`",
        parse_mode="Markdown"
    )
    await log_action(context, f"🚫 {name} ({uid}) banned in {update.effective_chat.id}")

# ---------------- UNBAN ----------------
async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    chat_id = update.effective_chat.id
    if update.message.reply_to_message:
        user = update.message.reply_to_message.from_user
        name, username, uid = fmt_user(user)
    elif context.args:
        try:
            uid = int(context.args[0])
            name, username = str(uid), "N/A"
        except ValueError:
            await update.message.reply_text("⚠️ Provide a valid user ID.")
            return
    else:
        await update.message.reply_text("⚠️ Reply to a user or provide a user ID.")
        return
    await context.bot.unban_chat_member(chat_id, uid)
    await update.message.reply_text(
        f"✅ *Unbanned!*\n👤 Username: {username}\n🆔 ID: `{uid}`",
        parse_mode="Markdown"
    )
    await log_action(context, f"✅ {name} ({uid}) unbanned in {chat_id}")

# ---------------- KICK ----------------
async def cmd_kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("⚠️ Reply to a user to kick them.")
        return
    user = update.message.reply_to_message.from_user
    name, username, uid = fmt_user(user)
    chat_id = update.effective_chat.id
    await context.bot.ban_chat_member(chat_id, uid)
    await context.bot.unban_chat_member(chat_id, uid)
    await update.message.reply_text(
        f"👢 *Kicked!*\n👤 Username: {username}\n🆔 ID: `{uid}`",
        parse_mode="Markdown"
    )
    await log_action(context, f"👢 {name} ({uid}) kicked in {chat_id}")

# ---------------- MUTE ----------------
async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("⚠️ Reply to a user to mute them.")
        return
    user = update.message.reply_to_message.from_user
    name, username, uid = fmt_user(user)
    chat_id = update.effective_chat.id
    await context.bot.restrict_chat_member(chat_id, uid, ChatPermissions(can_send_messages=False))
    btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔊 Unmute", callback_data=f"unmute_{uid}")]])
    await update.message.reply_text(
        f"🔇 *Muted!*\n👤 Username: {username}\n🆔 ID: `{uid}`",
        parse_mode="Markdown",
        reply_markup=btn
    )
    await log_action(context, f"🔇 {name} ({uid}) muted in {chat_id}")

# ---------------- WARN ----------------
async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("⚠️ Reply to a user to warn them.")
        return
    user = update.message.reply_to_message.from_user
    name, username, uid = fmt_user(user)
    chat_id = update.effective_chat.id
    count = get_warn_count(chat_id, uid) + 1
    set_warn_count(chat_id, uid, count)
    await update.message.reply_text(
        f"⚠️ *Warning {count}/3*\n👤 {name}\n🆔 ID: `{uid}`",
        parse_mode="Markdown"
    )
    if count >= 3:
        await context.bot.restrict_chat_member(chat_id, uid, ChatPermissions(can_send_messages=False))
        await update.message.reply_text(
            f"🚫 *Auto-Muted after 3 warnings!*\n👤 {name}",
            parse_mode="Markdown"
        )
        await log_action(context, f"🚫 {name} ({uid}) auto-muted (3 warns) in {chat_id}")

# ---------------- WARNINGS ----------------
async def cmd_warnings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("⚠️ Reply to a user to check their warnings.")
        return
    user = update.message.reply_to_message.from_user
    name, username, uid = fmt_user(user)
    count = get_warn_count(update.effective_chat.id, uid)
    await update.message.reply_text(
        f"⚠️ *Warnings: {count}/3*\n👤 {name}\n🆔 ID: `{uid}`",
        parse_mode="Markdown"
    )

# ---------------- RESET WARN ----------------
async def cmd_resetwarn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("⚠️ Reply to a user to reset their warnings.")
        return
    user = update.message.reply_to_message.from_user
    name, username, uid = fmt_user(user)
    set_warn_count(update.effective_chat.id, uid, 0)
    await update.message.reply_text(
        f"✅ *Warnings Reset!*\n👤 {name}\n🆔 ID: `{uid}`",
        parse_mode="Markdown"
    )

# ---------------- APPROVE ----------------
async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("⚠️ Reply to a user to approve them.")
        return
    user = update.message.reply_to_message.from_user
    name, username, uid = fmt_user(user)
    chat_id = update.effective_chat.id
    if not is_approved(chat_id, uid):
        db_exec("INSERT INTO approved_users VALUES (?, ?)", (chat_id, uid), commit=True)
    await update.message.reply_text(
        f"✅ *Approved!*\n👤 Username: {username}\n🆔 ID: `{uid}`\n\n_This user can now send links._",
        parse_mode="Markdown"
    )

# ---------------- UNAPPROVE ----------------
async def cmd_unapprove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("⚠️ Reply to a user to unapprove them.")
        return
    user = update.message.reply_to_message.from_user
    name, username, uid = fmt_user(user)
    chat_id = update.effective_chat.id
    db_exec("DELETE FROM approved_users WHERE chat_id=? AND user_id=?", (chat_id, uid), commit=True)
    await update.message.reply_text(
        f"❌ *Unapproved!*\n👤 Username: {username}\n🆔 ID: `{uid}`",
        parse_mode="Markdown"
    )

# ---------------- INFO ----------------
async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.reply_to_message.from_user if update.message.reply_to_message else update.effective_user
    name, username, uid = fmt_user(user)
    chat_id = update.effective_chat.id
    count = get_warn_count(chat_id, uid)
    approved = "✅ Yes" if is_approved(chat_id, uid) else "❌ No"
    await update.message.reply_text(
        f"👤 *User Info*\n\n"
        f"*First Name:* {name}\n"
        f"*Username:* {username}\n"
        f"🆔 ID: `{uid}`\n"
        f"⚠️ *Warnings:* {count}/3\n"
        f"✅ *Approved:* {approved}",
        parse_mode="Markdown"
    )

# ─────────────────────────────────────────
# FILTER COMMANDS
# ─────────────────────────────────────────

async def cmd_add_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "⚠️ Usage:\n"
            "`/filter <keyword> <reply text>` — text filter\n"
            "`/filter <keyword>` (reply to photo/video) — media filter",
            parse_mode="Markdown"
        )
        return

    chat_id = update.effective_chat.id
    keyword = args[0].lower().strip()
    reply_msg = update.message.reply_to_message

    file_id   = None
    ftype     = "text"
    reply_txt = " ".join(args[1:]).strip() if len(args) > 1 else ""

    # Media filter: admin replied to a photo or video
    if reply_msg:
        if reply_msg.photo:
            file_id   = reply_msg.photo[-1].file_id
            ftype     = "photo"
            reply_txt = reply_msg.caption or ""
        elif reply_msg.video:
            file_id   = reply_msg.video.file_id
            ftype     = "video"
            reply_txt = reply_msg.caption or ""
        elif reply_msg.text and not reply_txt:
            reply_txt = reply_msg.text

    if not file_id and not reply_txt:
        await update.message.reply_text(
            "⚠️ Provide reply text, or reply to a photo/video when adding the filter."
        )
        return

    filter_save(chat_id, keyword, reply_txt, file_id, ftype)
    await update.message.reply_text(
        f"✅ *Filter Added:* `{keyword}`\n📁 Type: `{ftype}`",
        parse_mode="Markdown"
    )


async def cmd_del_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("⚠️ Usage: `/stop <keyword>`", parse_mode="Markdown")
        return
    chat_id = update.effective_chat.id
    keyword = args[0].lower().strip()
    filter_delete(chat_id, keyword)
    await update.message.reply_text(
        f"❌ *Filter Removed:* `{keyword}`",
        parse_mode="Markdown"
    )


async def cmd_list_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = filter_list(chat_id)
    if not rows:
        await update.message.reply_text("📂 No active filters in this chat.")
        return
    lines = [f"• `{kw}` — _{ft}_" for kw, ft in rows]
    await update.message.reply_text(
        "📂 *Active Filters:*\n\n" + "\n".join(lines),
        parse_mode="Markdown"
    )

# ─────────────────────────────────────────
# LOCK COMMANDS
# ─────────────────────────────────────────

async def cmd_lock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    args = context.args or []
    if not args:
        types = ", ".join(f"`{t}`" for t in sorted(VALID_LOCKS))
        await update.message.reply_text(
            f"⚠️ Usage: `/lock <type>`\n📋 Types: {types}",
            parse_mode="Markdown"
        )
        return
    lock_type = args[0].lower()
    if lock_type not in VALID_LOCKS:
        types = ", ".join(f"`{t}`" for t in sorted(VALID_LOCKS))
        await update.message.reply_text(
            f"⚠️ Invalid type. Valid: {types}",
            parse_mode="Markdown"
        )
        return
    set_lock(update.effective_chat.id, lock_type)
    await update.message.reply_text(
        f"🔒 *{lock_type.capitalize()} Locked!*",
        parse_mode="Markdown"
    )


async def cmd_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    args = context.args or []
    if not args:
        types = ", ".join(f"`{t}`" for t in sorted(VALID_LOCKS))
        await update.message.reply_text(
            f"⚠️ Usage: `/unlock <type>`\n📋 Types: {types}",
            parse_mode="Markdown"
        )
        return
    lock_type = args[0].lower()
    if lock_type not in VALID_LOCKS:
        types = ", ".join(f"`{t}`" for t in sorted(VALID_LOCKS))
        await update.message.reply_text(
            f"⚠️ Invalid type. Valid: {types}",
            parse_mode="Markdown"
        )
        return
    unset_lock(update.effective_chat.id, lock_type)
    await update.message.reply_text(
        f"🔓 *{lock_type.capitalize()} Unlocked!*",
        parse_mode="Markdown"
    )

# ---------------- WELCOME ----------------
async def handle_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for user in update.message.new_chat_members:
        group = update.effective_chat.title or "this group"
        await update.message.reply_text(
            f"🎉 *Welcome {user.first_name} to {group}!*\n🆔 ID: `{user.id}`",
            parse_mode="Markdown"
        )

# ---------------- DISPATCH !COMMANDS ----------------
BANG_COMMAND_MAP = {
    "ban":       cmd_ban,
    "unban":     cmd_unban,
    "kick":      cmd_kick,
    "mute":      cmd_mute,
    "warn":      cmd_warn,
    "warnings":  cmd_warnings,
    "resetwarn": cmd_resetwarn,
    "approve":   cmd_approve,
    "unapprove": cmd_unapprove,
    "info":      cmd_info,
    "filter":    cmd_add_filter,
    "stop":      cmd_del_filter,
    "filters":   cmd_list_filters,
    "lock":      cmd_lock,
    "unlock":    cmd_unlock,
}

# ---------------- MESSAGE HANDLER ----------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    msg     = update.message
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    text    = msg.text or msg.caption or ""

    # --- Dispatch !commands (always allowed) ---
    if text.startswith("!"):
        parts = text[1:].split()
        if parts:
            cmd = parts[0].lower()
            if cmd in BANG_COMMAND_MAP:
                context.args = parts[1:]
                await BANG_COMMAND_MAP[cmd](update, context)
                return

    # --- Fetch admin IDs once for all lock/anti-link checks ---
    admin_ids = await get_admin_ids(context, chat_id)
    if user_id in admin_ids:
        # Admins bypass all locks and anti-link — skip to auto-filter
        pass
    else:
        # --- Lock: all (delete every non-admin message) ---
        if is_locked(chat_id, "all"):
            await msg.delete()
            return

        # --- Lock: links ---
        if msg.text and is_locked(chat_id, "links") and LINK_PATTERN.search(msg.text):
            await msg.delete()
            return

        # --- Lock: emoji ---
        if msg.text and is_locked(chat_id, "emoji") and EMOJI_PATTERN.search(msg.text):
            await msg.delete()
            return

        # --- Anti-link system (independent of lock; skip approved users) ---
        if msg.text and LINK_PATTERN.search(msg.text) and not is_approved(chat_id, user_id):
            await msg.delete()
            return

    # --- Auto-filter: match keywords in text ---
    if msg.text:
        reply, file_id, ftype = filter_match(chat_id, msg.text.lower())
        if reply is not None or file_id is not None:
            if ftype == "photo" and file_id:
                await msg.reply_photo(photo=file_id, caption=reply or None)
            elif ftype == "video" and file_id:
                await msg.reply_video(video=file_id, caption=reply or None)
            elif reply:
                await msg.reply_text(reply)


# ---------------- MEDIA LOCK HANDLER ----------------
async def handle_media_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    msg     = update.message
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    admin_ids = await get_admin_ids(context, chat_id)
    if user_id in admin_ids:
        return

    # "all" lock removes everything
    if is_locked(chat_id, "all"):
        await msg.delete()
        return

    if msg.sticker and is_locked(chat_id, "stickers"):
        await msg.delete()
        return

    if msg.animation and is_locked(chat_id, "gifs"):
        await msg.delete()
        return

    if msg.photo and is_locked(chat_id, "photos"):
        await msg.delete()
        return

    if msg.video and is_locked(chat_id, "videos"):
        await msg.delete()
        return

# ---------------- APP ----------------
app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start",      cmd_start))
app.add_handler(CommandHandler("help",       cmd_help))
app.add_handler(CommandHandler("ban",        cmd_ban))
app.add_handler(CommandHandler("unban",      cmd_unban))
app.add_handler(CommandHandler("kick",       cmd_kick))
app.add_handler(CommandHandler("mute",       cmd_mute))
app.add_handler(CommandHandler("warn",       cmd_warn))
app.add_handler(CommandHandler("warnings",   cmd_warnings))
app.add_handler(CommandHandler("resetwarn",  cmd_resetwarn))
app.add_handler(CommandHandler("approve",    cmd_approve))
app.add_handler(CommandHandler("unapprove",  cmd_unapprove))
app.add_handler(CommandHandler("info",       cmd_info))
app.add_handler(CommandHandler("filter",     cmd_add_filter))
app.add_handler(CommandHandler("stop",       cmd_del_filter))
app.add_handler(CommandHandler("filters",    cmd_list_filters))
app.add_handler(CommandHandler("lock",       cmd_lock))
app.add_handler(CommandHandler("unlock",     cmd_unlock))

app.add_handler(CallbackQueryHandler(callback_button))
app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_welcome))

# Text messages: !commands, anti-link, lock:links, lock:emoji, lock:all, auto-filter
app.add_handler(MessageHandler(
    filters.TEXT & filters.ChatType.GROUPS,
    handle_message
))

# Media messages: lock:stickers, lock:gifs, lock:photos, lock:videos, lock:all
app.add_handler(MessageHandler(
    (filters.PHOTO | filters.VIDEO | filters.Sticker.ALL | filters.ANIMATION)
    & filters.ChatType.GROUPS,
    handle_media_message
))

print("Bot Running...")
app.run_polling()
