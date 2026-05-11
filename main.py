import os
from telegram import Update, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

TOKEN = os.getenv("BOT_TOKEN")

# ---------------- START ----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Bot is Alive!")

# ---------------- HELP ----------------
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start - Start bot\n"
        "/help - Commands\n"
        "/ban - Reply to ban\n"
        "/mute - Reply to mute\n"
        "/warn - Reply to warn"
    )

# ---------------- ADMIN CHECK ----------------
async def is_admin(update, context):
    user_id = update.effective_user.id
    admins = await context.bot.get_chat_administrators(update.effective_chat.id)
    return any(a.user.id == user_id for a in admins)

# ---------------- BAN ----------------
async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    if not update.message.reply_to_message:
        return await update.message.reply_text("Reply to user")
    
    user = update.message.reply_to_message.from_user
    await context.bot.ban_chat_member(update.effective_chat.id, user.id)
    await update.message.reply_text(f"🚫 Banned {user.first_name}")

# ---------------- MUTE ----------------
async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    if not update.message.reply_to_message:
        return await update.message.reply_text("Reply to user")

    user = update.message.reply_to_message.from_user
    await context.bot.restrict_chat_member(
        update.effective_chat.id,
        user.id,
        ChatPermissions(can_send_messages=False)
    )

    btn = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔊 Unmute", callback_data=f"unmute_{user.id}")]
    ])

    await update.message.reply_text("🔇 Muted", reply_markup=btn)

# ---------------- WARN ----------------
warns = {}

async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    if not update.message.reply_to_message:
        return

    user = update.message.reply_to_message.from_user
    warns[user.id] = warns.get(user.id, 0) + 1

    await update.message.reply_text(f"⚠️ Warn {warns[user.id]}/3")

    if warns[user.id] >= 3:
        await context.bot.restrict_chat_member(
            update.effective_chat.id,
            user.id,
            ChatPermissions(can_send_messages=False)
        )
        await update.message.reply_text("🚫 Auto muted")

# ---------------- BUTTON ----------------
async def callback_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data.startswith("unmute_"):
        user_id = int(query.data.split("_")[1])
        await context.bot.restrict_chat_member(
            query.message.chat.id,
            user_id,
            ChatPermissions(can_send_messages=True)
        )
        await query.edit_message_text("✅ Unmuted")

# ---------------- MESSAGE FILTER ----------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower()

    if "hello" in text:
        await update.message.reply_text("Hi there 👋")

# ---------------- MAIN ----------------
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("warn", cmd_warn))

    app.add_handler(CallbackQueryHandler(callback_button))

    app.add_handler(MessageHandler(filters.TEXT, handle_message))

    print("🤖 Bot Started...")
    app.run_polling()

if __name__ == "__main__":
    main()
