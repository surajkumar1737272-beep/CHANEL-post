#!/usr/bin/env python3
"""
Telegram Channel & Group Post Maker Bot
Supports Channels, Supergroups & Groups.
Features: Emoji buttons, button color styling, clean cancel, target removal.
Credit: ALEX-LX
"""

from __future__ import annotations

import html
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import aiosqlite
from telegram import (
    ChatMemberAdministrator,
    ChatMemberOwner,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# Paste your NEW Telegram bot token between the quotes below.
# The old empty environment-variable assignment was overwriting this value.
import os

BOT_TOKEN = "8808899682:AAFxU61K-c99y2H4JQK0mcB8-GDOYHfEo7c"
OWNER_ID = int(os.getenv("OWNER_ID", "7851342220") or "7851342220")

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is not set.")

DB_FILE = os.getenv("DB_FILE", "bot_data.db")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("PostMakerBot")


def btn(
    text: str,
    callback_data: Optional[str] = None,
    url: Optional[str] = None,
    style: Optional[str] = None,
) -> InlineKeyboardButton:
    """
    Build InlineKeyboardButton with optional color style.
    style can be 'primary' | 'success' | 'danger'.
    Silently falls back if PTB version doesn't support it.
    """
    kwargs: Dict[str, Any] = {"text": text}
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    if url is not None:
        kwargs["url"] = url

    if style:
        try:
            return InlineKeyboardButton(**kwargs, style=style)
        except TypeError:
            pass
    return InlineKeyboardButton(**kwargs)


COLOR_EMOJI = {"primary": "🔵", "success": "🟢", "danger": "🔴"}


def track_msg(context: ContextTypes.DEFAULT_TYPE, message_id: Optional[int]) -> None:
    """Track a message ID so it can be deleted on cancel/publish."""
    if not message_id:
        return
    lst = context.user_data.setdefault("_trash", [])
    if message_id not in lst:
        lst.append(message_id)


async def send_tracked(target, context: ContextTypes.DEFAULT_TYPE, text: str, **kwargs):
    """reply_text + auto-track for cleanup."""
    sent = await target.reply_text(text, **kwargs)
    track_msg(context, sent.message_id)
    return sent


async def purge_trash(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Delete every tracked message and clear the list."""
    ids = context.user_data.get("_trash", [])
    for mid in ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except TelegramError:
            pass
    context.user_data.pop("_trash", None)


async def init_db() -> None:
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                channel_title TEXT NOT NULL,
                chat_type TEXT NOT NULL DEFAULT 'channel',
                created_at TEXT NOT NULL,
                UNIQUE(user_id, channel_id)
            );
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                channel_title TEXT NOT NULL,
                post_type TEXT NOT NULL,
                content_preview TEXT NOT NULL,
                published_at TEXT NOT NULL,
                telegram_message_id INTEGER NOT NULL
            );
            """
        )
        async with db.execute("PRAGMA table_info(channels);") as cursor:
            columns = [row[1] for row in await cursor.fetchall()]
            if "chat_type" not in columns:
                await db.execute(
                    "ALTER TABLE channels ADD COLUMN chat_type TEXT NOT NULL DEFAULT 'channel';"
                )
        await db.execute("""
            CREATE TABLE IF NOT EXISTS force_targets (
                chat_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                chat_type TEXT NOT NULL,
                added_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_stats (
                chat_id INTEGER PRIMARY KEY,
                chat_type TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                started_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        await db.commit()


async def save_channel(user_id: int, channel_id: int, title: str, chat_type: str) -> None:
    async with aiosqlite.connect(DB_FILE) as db:
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            """
            INSERT INTO channels (user_id, channel_id, channel_title, chat_type, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, channel_id) DO UPDATE SET
                channel_title = excluded.channel_title,
                chat_type = excluded.chat_type;
            """,
            (user_id, channel_id, title, chat_type, now),
        )
        await db.commit()


async def delete_channel(user_id: int, channel_id: int) -> None:
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "DELETE FROM channels WHERE user_id = ? AND channel_id = ?",
            (user_id, channel_id),
        )
        await db.commit()


async def get_user_channels(user_id: int) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT channel_id, channel_title, chat_type FROM channels WHERE user_id = ? ORDER BY id DESC",
            (user_id,),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def get_user_channel(user_id: int, channel_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT channel_id, channel_title, chat_type FROM channels WHERE user_id = ? AND channel_id = ?",
            (user_id, channel_id),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def save_published_post(
    user_id: int,
    channel_id: int,
    channel_title: str,
    post_type: str,
    content_preview: str,
    telegram_message_id: int,
) -> None:
    async with aiosqlite.connect(DB_FILE) as db:
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            """
            INSERT INTO posts (user_id, channel_id, channel_title, post_type, content_preview, published_at, telegram_message_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, channel_id, channel_title, post_type, content_preview[:120], now, telegram_message_id),
        )
        await db.commit()


async def get_user_posts(user_id: int, limit: int = 10) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT channel_title, post_type, content_preview, published_at, telegram_message_id
            FROM posts WHERE user_id = ? ORDER BY id DESC LIMIT ?
            """,
            (user_id, limit),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]



def is_owner(user_id: int) -> bool:
    return OWNER_ID > 0 and user_id == OWNER_ID


async def track_start(update: Update) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            """INSERT INTO bot_stats(chat_id, chat_type, user_id, started_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET
                 chat_type=excluded.chat_type, user_id=excluded.user_id""",
            (chat.id, chat.type, user.id, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()


async def setting_get(key: str) -> Optional[str]:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT value FROM bot_settings WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def setting_set(key: str, value: str) -> None:
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO bot_settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await db.commit()


async def owner_only(update: Update) -> bool:
    user = update.effective_user
    if not user or not is_owner(user.id):
        if update.effective_message:
            await update.effective_message.reply_text("⛔ This command is for the BOT OWNER only.")
        return False
    return True


async def get_force_targets() -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT chat_id,title,chat_type FROM force_targets ORDER BY added_at") as cur:
            return [dict(row) for row in await cur.fetchall()]


async def force_join_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user or is_owner(user.id):
        return True
    targets = await get_force_targets()
    missing = []
    for target in targets:
        try:
            member = await context.bot.get_chat_member(target["chat_id"], user.id)
            if member.status not in ("member", "administrator", "creator"):
                missing.append(target)
        except TelegramError:
            missing.append(target)
    if not missing:
        return True
    rows = []
    for target in missing:
        try:
            chat = await context.bot.get_chat(target["chat_id"])
            if chat.username:
                rows.append([InlineKeyboardButton(f"📢 Join {target['title']}", url=f"https://t.me/{chat.username}")])
            else:
                rows.append([InlineKeyboardButton(f"📢 {target['title']}", callback_data="force_join_info")])
        except TelegramError:
            rows.append([InlineKeyboardButton(f"📢 {target['title']}", callback_data="force_join_info")])
    rows.append([InlineKeyboardButton("🔄 I HAVE JOINED — CHECK", callback_data="force_recheck")])
    if update.effective_message:
        await update.effective_message.reply_text(
            "🔒 <b>JOIN REQUIRED</b>\\n\\nPlease join the channel/group below, then tap CHECK.",
            reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML,
        )
    return False


async def force_join_info_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query:
        await query.answer(
            "Please contact the bot owner for the required channel/group link.",
            show_alert=True,
        )


async def force_recheck_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if await force_join_check(update, context):
        await q.message.reply_text("✅ Membership verified. Now send /start.")
    else:
        await q.answer("Please join all required channels/groups first.", show_alert=True)


async def set_welcome_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await owner_only(update):
        return
    context.user_data["awaiting_welcome_media"] = True
    await update.effective_message.reply_text(
        "📩 Send the welcome PHOTO, VIDEO, or GIF/ANIMATION now.\\n"
        "Send /cancel to keep the current welcome."
    )


async def welcome_media_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.get("awaiting_welcome_media") or not is_owner(update.effective_user.id):
        return
    msg = update.effective_message
    if msg.photo:
        value = f"photo|{msg.photo[-1].file_id}"
    elif msg.video:
        value = f"video|{msg.video.file_id}"
    elif msg.animation:
        value = f"animation|{msg.animation.file_id}"
    else:
        await msg.reply_text("Please send a photo, video, or GIF animation.")
        return
    await setting_set("welcome_media", value)
    context.user_data.pop("awaiting_welcome_media", None)
    await msg.reply_text("✅ Welcome media updated successfully.")


async def add_forcement_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await owner_only(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /add_forcement -1001234567890")
        return
    try:
        chat_id = int(context.args[0])
        chat = await context.bot.get_chat(chat_id)
        if chat.type not in ("channel", "group", "supergroup"):
            raise ValueError("Only channels and groups are supported.")
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "INSERT OR REPLACE INTO force_targets(chat_id,title,chat_type,added_at) VALUES(?,?,?,?)",
                (chat_id, chat.title or str(chat_id), chat.type, datetime.now(timezone.utc).isoformat()),
            )
            await db.commit()
        await update.effective_message.reply_text(
            f"✅ Force-join target added: {html.escape(chat.title or str(chat_id))}\\n"
            "Make sure the bot is an administrator there."
        )
    except (ValueError, TelegramError) as exc:
        await update.effective_message.reply_text(f"❌ Could not add target: {html.escape(str(exc))}")


async def remove_forcement_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await owner_only(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /remove_forcement -1001234567890")
        return
    try:
        chat_id = int(context.args[0])
        async with aiosqlite.connect(DB_FILE) as db:
            cur = await db.execute("DELETE FROM force_targets WHERE chat_id=?", (chat_id,))
            await db.commit()
        await update.effective_message.reply_text("✅ Removed." if cur.rowcount else "Target not found.")
    except ValueError:
        await update.effective_message.reply_text("Send a numeric channel/group ID.")


async def forcement_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await owner_only(update):
        return
    targets = await get_force_targets()
    if not targets:
        await update.effective_message.reply_text("No force-join targets configured.")
        return
    body = "\\n".join(f"• {html.escape(t['title'])} — <code>{t['chat_id']}</code>" for t in targets)
    await update.effective_message.reply_text("📋 <b>FORCE-JOIN LIST</b>\\n\\n" + body, parse_mode=ParseMode.HTML)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await owner_only(update):
        return
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT chat_type, COUNT(*) FROM bot_stats GROUP BY chat_type") as cur:
            rows = await cur.fetchall()
    counts = {kind: count for kind, count in rows}
    text = (
        "📊 <b>BOT STATISTICS</b>\\n━━━━━━━━━━━━━━━━━━\\n"
        f"👤 DM STARTS: <b>{counts.get('private', 0)}</b>\\n"
        f"👥 GROUP STARTS: <b>{counts.get('group', 0) + counts.get('supergroup', 0)}</b>\\n"
        f"📢 CHANNEL STARTS: <b>{counts.get('channel', 0)}</b>\\n"
        f"🔢 TOTAL CHATS: <b>{sum(counts.values())}</b>"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    context.user_data["awaiting_edit_forward"] = True
    await update.effective_message.reply_text(
        "Forward the channel post you want to edit.\n"
        "You must be an Owner/Admin of that channel, and the bot must have permission there.\n"
        "Note: Telegram allows editing only messages originally sent by this bot."
    )


async def edit_forward_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not context.user_data.get("awaiting_edit_forward") or not user:
        return
    msg = update.effective_message
    origin = getattr(msg, "forward_origin", None)
    if not origin or getattr(origin, "type", None) != "channel":
        await msg.reply_text("Please forward a post directly from the channel.")
        return
    channel_id = origin.chat.id
    saved = await get_user_channel(user.id, channel_id)
    if not saved:
        await msg.reply_text("❌ Pehle <b>Set Target</b> se apna channel add karein, phir /edit use karein.", parse_mode=ParseMode.HTML)
        return
    allowed, reason = await verify_user_channel_ownership(context.bot, channel_id, user.id)
    if not allowed:
        await msg.reply_text("❌ Edit allowed nahi: " + html.escape(reason))
        context.user_data.pop("awaiting_edit_forward", None)
        return
    channel_id = origin.chat.id
    message_id = origin.message_id
    context.user_data.pop("awaiting_edit_forward", None)
    context.user_data["edit_target"] = (channel_id, message_id)
    context.user_data["awaiting_edit_content"] = True
    await msg.reply_text("Now send the NEW text/caption. For media replacement, send a new photo/video with caption.")


async def edit_content_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not context.user_data.get("awaiting_edit_content") or not user:
        return
    msg = update.effective_message
    chat_id, message_id = context.user_data["edit_target"]
    saved = await get_user_channel(user.id, chat_id)
    allowed, reason = await verify_user_channel_ownership(context.bot, chat_id, user.id) if saved else (False, "Target is not saved to your account.")
    if not allowed:
        await msg.reply_text("❌ Edit denied: " + html.escape(reason))
        context.user_data.pop("awaiting_edit_content", None)
        context.user_data.pop("edit_target", None)
        return
    try:
        if msg.photo:
            from telegram import InputMediaPhoto
            await context.bot.edit_message_media(
                chat_id=chat_id, message_id=message_id,
                media=InputMediaPhoto(msg.photo[-1].file_id, caption=msg.caption_html, parse_mode=ParseMode.HTML),
            )
        elif msg.video:
            from telegram import InputMediaVideo
            await context.bot.edit_message_media(
                chat_id=chat_id, message_id=message_id,
                media=InputMediaVideo(msg.video.file_id, caption=msg.caption_html, parse_mode=ParseMode.HTML),
            )
        elif msg.text:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id,
                text=msg.text_html, parse_mode=ParseMode.HTML,
            )
        else:
            await msg.reply_text("Send text, photo, or video.")
            return
        await msg.reply_text("✅ Edit request completed.")
    except TelegramError as exc:
        await msg.reply_text(
            "❌ Telegram could not edit this post. The bot can edit only its own channel messages.\n"
            + html.escape(str(exc))
        )
    finally:
        context.user_data.pop("awaiting_edit_content", None)
        context.user_data.pop("edit_target", None)


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await owner_only(update):
        return
    context.user_data["awaiting_broadcast_message"] = True
    await update.effective_message.reply_text(
        "📢 Broadcast ke liye ab message, photo, video ya forwarded post bhejein.\n"
        "Broadcast sirf un private users ko jayega jinhone bot start kiya hai.\n"
        "Forwarded post forward ke roop mein jayegi; saath mein Owner button bhi aayega."
    )


async def broadcast_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.get("awaiting_broadcast_message") or not is_owner(update.effective_user.id):
        return
    msg = update.effective_message
    if not msg:
        return
    context.user_data.pop("awaiting_broadcast_message", None)
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT chat_id FROM bot_stats WHERE chat_type='private'") as cur:
            recipients = [row[0] for row in await cur.fetchall()]

    keyboard = InlineKeyboardMarkup([[btn("👑 Owner", url="https://t.me/BLOOD_VORTEX_1", style="primary")]])
    sent = 0
    failed = 0
    for recipient in recipients:
        try:
            # Forwarding from the owner's chat preserves Telegram's forwarded-message attribution.
            if getattr(msg, "forward_origin", None):
                await context.bot.forward_message(
                    chat_id=recipient, from_chat_id=msg.chat_id, message_id=msg.message_id
                )
                await context.bot.send_message(chat_id=recipient, text="👆 More information:", reply_markup=keyboard)
            else:
                await context.bot.copy_message(
                    chat_id=recipient, from_chat_id=msg.chat_id, message_id=msg.message_id,
                    reply_markup=keyboard,
                )
            sent += 1
        except TelegramError:
            failed += 1
    await msg.reply_text(f"✅ Broadcast complete.\n📨 Sent: {sent}\n⚠️ Failed: {failed}")



def is_valid_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    if parsed.scheme.lower() not in ("http", "https", "tg"):
        return False
    if parsed.scheme.lower() in ("http", "https") and not parsed.netloc:
        return False
    return True


(
    STATE_SET_CHANNEL,
    STATE_SELECT_CHANNEL,
    STATE_SELECT_TYPE,
    STATE_ENTER_CONTENT,
    STATE_ENTER_CAPTION,
    STATE_ADD_BUTTONS,
    STATE_SELECT_BUTTON_COLOR,
    STATE_CONFIRM_PUBLISH,
) = range(8)


def kb_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [btn("⚙️ Set Target", callback_data="menu_set_channel", style="primary"),
         btn("📝 Create Post", callback_data="menu_create_post", style="success")],
        [btn("📋 My Targets", callback_data="menu_my_channels", style="primary"),
         btn("📚 History", callback_data="menu_my_posts", style="primary")],
        [btn("❓ Help & Guide", callback_data="menu_help", style="primary")],
        [btn("👑 Owner", url="https://t.me/BLOOD_VORTEX_1", style="success"),
         btn("🛠 Owner Panel", callback_data="owner_panel", style="primary")],
    ])


def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [btn("❌ Cancel", callback_data="cancel_action", style="danger")]
    ])


def kb_skip_cancel(skip_cb: str, skip_label: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [btn(skip_label, callback_data=skip_cb, style="primary")],
        [btn("❌ Cancel", callback_data="cancel_action", style="danger")],
    ])


def build_post_markup(buttons: List[List[Dict[str, str]]]) -> Optional[InlineKeyboardMarkup]:
    if not buttons:
        return None
    keyboard = []
    for row in buttons:
        btn_row = [btn(text=b["text"], url=b["url"], style=b.get("style")) for b in row]
        if btn_row:
            keyboard.append(btn_row)
    return InlineKeyboardMarkup(keyboard) if keyboard else None


async def check_bot_posting_permission(bot, target_id: int) -> tuple[bool, str]:
    try:
        member = await bot.get_chat_member(chat_id=target_id, user_id=bot.id)
        if isinstance(member, ChatMemberOwner):
            return True, "Owner"
        if isinstance(member, ChatMemberAdministrator):
            can_post = getattr(member, "can_post_messages", None)
            if can_post is False:
                return False, "Bot is Admin but lacks 'Post Messages' permission."
            return True, "Administrator"
        return False, "Bot is not an Administrator in this Channel/Group."
    except BadRequest as e:
        if "chat not found" in e.message.lower():
            return False, "Bot is not added to this chat. Pehle bot ko Admin banayein!"
        return False, f"Telegram error: {e.message}"
    except TelegramError as e:
        return False, f"Telegram API error: {e.message}"
    except Exception as e:
        logger.error("Error checking bot permission: %s", e)
        return False, "Verification failed. Check the ID."


async def verify_user_channel_ownership(bot, target_id: int, user_id: int) -> tuple[bool, str]:
    try:
        user_member = await bot.get_chat_member(chat_id=target_id, user_id=user_id)
        if isinstance(user_member, ChatMemberOwner):
            return True, "Owner"
        if isinstance(user_member, ChatMemberAdministrator):
            return True, "Administrator"
        return False, "Aap is Channel/Group ke Owner ya Admin nahi hain."
    except BadRequest as e:
        if "user not found" in e.message.lower() or "participant" in e.message.lower():
            return False, "Aap is Channel/Group ke member/admin nahi hain."
        return False, f"Verification failed: {e.message}"
    except Exception as e:
        logger.error("Error checking user ownership: %s", e)
        return False, "User verification error."


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await track_start(update)
    if not await force_join_check(update, context):
        return ConversationHandler.END
    context.user_data.clear()
    welcome_text = (
        "👋 <b>✨ W E L C O M E  T O  P O S T  M A K E R ✨</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📢 <b>CREATE & PUBLISH PROFESSIONAL POSTS</b>\n"
        "Text • Photo • Video • URL Buttons\n\n"
        "🔐 <b>SECURITY:</b> Add only channels/groups where you are Owner/Admin.\n"
        "🎨 <b>BUTTON STYLES:</b> Primary • Success • Danger\n"
        "💬 <b>QUOTE:</b> Use Telegram blockquote formatting in your post text."
    )
    if update.message:
        media = await setting_get("welcome_media")
        if media:
            kind, file_id = media.split("|", 1)
            sender = {"photo": update.message.reply_photo, "video": update.message.reply_video,
                      "animation": update.message.reply_animation}.get(kind)
            if sender:
                await sender(file_id, caption=welcome_text, reply_markup=kb_main_menu(), parse_mode=ParseMode.HTML)
            else:
                await update.message.reply_text(welcome_text, reply_markup=kb_main_menu(), parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text(welcome_text, reply_markup=kb_main_menu(), parse_mode=ParseMode.HTML)
    elif update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(
                welcome_text, reply_markup=kb_main_menu(), parse_mode=ParseMode.HTML
            )
        except BadRequest:
            await update.callback_query.message.reply_text(
                welcome_text, reply_markup=kb_main_menu(), parse_mode=ParseMode.HTML
            )
    return ConversationHandler.END


async def owner_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not update.effective_user or not is_owner(update.effective_user.id):
        if query:
            await query.answer("Owner only panel.", show_alert=True)
        return
    if query:
        await query.answer()
    panel_text = (
        "🛠 <b>OWNER PANEL — COMMANDS</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<code>/broadcast</code> — send a message to registered private users.\n"
        "<code>/stats</code> — bot usage statistics.\n"
        "<code>/set_welcome</code> — set welcome media.\n"
        "<code>/add_forcement ID</code> — add force-join target.\n"
        "<code>/remove_forcement ID</code> — remove force-join target.\n"
        "<code>/forcement_list</code> — list force-join targets.\n"
        "<code>/edit</code> — edit bot-authored posts in your own admin targets."
    )
    keyboard = InlineKeyboardMarkup([[btn("🔙 Back to Menu", callback_data="main_menu", style="primary")]])
    if query:
        await query.edit_message_text(panel_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query:
        await query.answer()
    help_text = (
        "📖 <b>HOW TO USE THIS BOT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>🔹 Channel / Group ID kaise nikalein?</b>\n"
        "1. Bot ko target me <b>Admin</b> banayein.\n"
        "2. Telegram channel/group details se numeric ID lein, ya target setup ke waqt bot ke instructions follow karein.\n"
        "3. Example ID: <code>-1001234567890</code>.\n\n"
        "<b>🔹 Post kaise banayein?</b>\n"
        "1. <b>Create Post</b> → target select karein.\n"
        "2. Format chunein (Text / Photo / Video).\n"
        "3. Content + caption add karein.\n"
        "4. URL buttons daalein aur unka <b>color</b> chunein.\n"
        "5. <b>🚀 Publish Now</b> dabayein.\n\n"
        "<b>🔹 OWNER COMMANDS:</b>\n"
        "<code>/set_welcome</code> — set welcome photo/video/GIF (send media after command).\n"
        "<code>/stats</code> — DM/group/channel start counts.\n"
        "<code>/add_forcement ID</code> — require joining a channel/group.\n"
        "<code>/remove_forcement ID</code> — remove force-join target.\n"
        "<code>/forcement_list</code> — show required targets.\n"
        "<code>/broadcast</code> — owner-only broadcast to users who started the bot; send/forward the message after the command.\n"
        "<code>/edit</code> — public; users must add their own target and be its Owner/Admin. Bot can edit only its own posts.\n\n"
        "<b>🔹 QUOTE FORMAT:</b> Use <code>&lt;blockquote&gt;quoted text&lt;/blockquote&gt;</code> for Telegram's blue quote block.\n\n"
        "<b>🔹 Button format:</b>\n"
        "<code>Label - https://link.com</code>\n"
        "Ek row me 2 buttons: <code>A - url | B - url</code>\n"
        "Emoji allowed: <code>🔥 Join - https://t.me/x</code>"
    )
    keyboard = InlineKeyboardMarkup([
        [btn("🔙 Back to Menu", callback_data="main_menu", style="primary")]
    ])
    if query:
        await query.edit_message_text(help_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    elif update.effective_message:
        await update.effective_message.reply_text(help_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


async def _render_my_channels(query, user_id: int) -> None:
    channels = await get_user_channels(user_id)

    if not channels:
        msg = (
            "📋 <b>My Targets</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Abhi tak koi target add nahi kiya.\n\n"
            "👉 <b>Set Target</b> se shuru karein."
        )
        keyboard = InlineKeyboardMarkup([
            [btn("⚙️ Set Target", callback_data="menu_set_channel", style="primary")],
            [btn("🔙 Back to Menu", callback_data="main_menu", style="primary")],
        ])
    else:
        msg = "📋 <b>My Verified Targets</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        for idx, ch in enumerate(channels, 1):
            title = html.escape(ch.get("channel_title") or "Unnamed")
            c_type = ch.get("chat_type", "Channel").capitalize()
            icon = "📢" if c_type == "Channel" else "👥"
            msg += f"{icon} <b>{idx}. {title}</b>\n<code>ID: {ch['channel_id']}</code>\n\n"

        keyboard = InlineKeyboardMarkup([
            [btn("⚙️ Add New", callback_data="menu_set_channel", style="primary"),
             btn("🗑 Remove", callback_data="remove_target_menu", style="danger")],
            [btn("🔙 Back to Menu", callback_data="main_menu", style="primary")],
        ])

    await query.edit_message_text(msg, reply_markup=keyboard, parse_mode=ParseMode.HTML)


async def my_channels_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await _render_my_channels(query, query.from_user.id)


async def remove_target_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    channels = await get_user_channels(query.from_user.id)

    if not channels:
        await query.answer("Koi target remove karne ke liye nahi hai.", show_alert=True)
        return
    await query.answer()

    keyboard = []
    for ch in channels:
        icon = "📢" if ch.get("chat_type") == "Channel" else "👥"
        keyboard.append([
            btn(f"🗑 {icon} {ch['channel_title']}",
                callback_data=f"del_chan_{ch['channel_id']}",
                style="danger")
        ])
    keyboard.append([btn("🔙 Back", callback_data="menu_my_channels", style="primary")])

    await query.edit_message_text(
        "🗑 <b>Remove Target</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Jis target ko remove karna hai, uspe tap karein:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML,
    )


async def delete_channel_prompt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    try:
        channel_id = int(query.data.split("_")[2])
    except (IndexError, ValueError):
        await query.edit_message_text("❌ Invalid selection.", reply_markup=kb_main_menu())
        return

    channel = await get_user_channel(query.from_user.id, channel_id)
    if not channel:
        await query.answer("Target mila nahi.", show_alert=True)
        return

    icon = "📢" if channel["chat_type"] == "Channel" else "👥"
    keyboard = InlineKeyboardMarkup([
        [btn("✅ Yes, Remove", callback_data=f"confirm_del_{channel_id}", style="danger")],
        [btn("🔙 Cancel", callback_data="menu_my_channels", style="primary")],
    ])

    await query.edit_message_text(
        "⚠️ <b>Confirm Removal</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{icon} <b>{html.escape(channel['channel_title'])}</b>\n"
        f"🆔 <code>{channel['channel_id']}</code>\n\n"
        "Pakka ye target remove karna hai?",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )


async def confirm_delete_channel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        channel_id = int(query.data.split("_")[2])
    except (IndexError, ValueError):
        await query.answer("Invalid.", show_alert=True)
        return

    await delete_channel(query.from_user.id, channel_id)
    await query.answer("✅ Target removed.", show_alert=False)
    await _render_my_channels(query, query.from_user.id)


async def my_posts_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    posts = await get_user_posts(query.from_user.id, limit=10)

    if not posts:
        msg = (
            "📚 <b>Post History</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Abhi tak koi post publish nahi kiya."
        )
    else:
        msg = "📚 <b>Last 10 Published Posts</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        for idx, post in enumerate(posts, 1):
            ch_title = html.escape(post.get("channel_title") or "Target")
            p_type = post.get("post_type", "").capitalize()
            preview = html.escape(post.get("content_preview") or "")
            pub_date = post.get("published_at", "")[:16].replace("T", " ")
            msg += (
                f"<b>{idx}. [{p_type}]</b> → {ch_title}\n"
                f"🗓 <code>{pub_date} UTC</code>\n"
                f"💬 <i>{preview}</i>\n\n"
            )

    await query.edit_message_text(
        msg,
        reply_markup=InlineKeyboardMarkup([
            [btn("🔙 Back to Menu", callback_data="main_menu", style="primary")]
        ]),
        parse_mode=ParseMode.HTML,
    )


async def set_channel_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    msg = (
        "⚙️ <b>Set Target — Channel / Group</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>Steps:</b>\n"
        "1️⃣ Bot ko apne target me <b>Administrator</b> banayein.\n"
        "2️⃣ Us channel/group ki numeric ID copy karein.\n"
        "3️⃣ Neeche paste karke bhej dein.\n\n"
        "📌 <b>Format:</b> <code>-1001234567890</code>"
    )
    await query.edit_message_text(msg, reply_markup=kb_cancel(), parse_mode=ParseMode.HTML)
    return STATE_SET_CHANNEL


async def set_channel_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    track_msg(context, update.message.message_id)
    user_id = update.effective_user.id
    text_val = (update.message.text or "").strip()

    if text_val.startswith("@") or not (text_val.startswith("-") or text_val.isdigit()):
        await send_tracked(
            update.message, context,
            "❌ <b>Invalid input</b>\n\n"
            "Sirf numeric Chat ID bhejein — jaise <code>-1001234567890</code>.",
            reply_markup=kb_cancel(), parse_mode=ParseMode.HTML,
        )
        return STATE_SET_CHANNEL

    try:
        target_id = int(text_val)
    except ValueError:
        await send_tracked(
            update.message, context,
            "❌ Valid numeric ID bhejein.",
            reply_markup=kb_cancel(),
        )
        return STATE_SET_CHANNEL

    try:
        chat = await context.bot.get_chat(target_id)
    except BadRequest as e:
        await send_tracked(
            update.message, context,
            f"❌ <b>Chat not found</b>\n\n{html.escape(e.message)}\n\n"
            "Confirm karein ki bot us channel/group me <b>Admin</b> hai.",
            reply_markup=kb_cancel(), parse_mode=ParseMode.HTML,
        )
        return STATE_SET_CHANNEL

    if chat.type not in ("channel", "supergroup", "group"):
        await send_tracked(
            update.message, context,
            f"❌ Yeh ek <b>{chat.type}</b> hai. Sirf Channels aur Groups support hain.",
            reply_markup=kb_cancel(), parse_mode=ParseMode.HTML,
        )
        return STATE_SET_CHANNEL

    bot_ok, bot_reason = await check_bot_posting_permission(context.bot, target_id)
    if not bot_ok:
        await send_tracked(
            update.message, context,
            f"❌ <b>Bot permission missing</b>\n\n{html.escape(bot_reason)}\n\n"
            "Bot ko Admin banakar <b>Post Messages</b> permission enable karein.",
            reply_markup=kb_cancel(), parse_mode=ParseMode.HTML,
        )
        return STATE_SET_CHANNEL

    user_ok, user_reason = await verify_user_channel_ownership(context.bot, target_id, user_id)
    if not user_ok:
        await send_tracked(
            update.message, context,
            f"⛔ <b>Access Denied</b>\n\n{html.escape(user_reason)}\n\n"
            "Aap sirf apna hi channel/group add kar sakte hain.",
            reply_markup=kb_cancel(), parse_mode=ParseMode.HTML,
        )
        return STATE_SET_CHANNEL

    chat_type_str = "Group" if chat.type in ("group", "supergroup") else "Channel"
    await save_channel(user_id, target_id, chat.title or chat_type_str, chat_type_str)

    await purge_trash(context, update.effective_chat.id)

    icon = "📢" if chat_type_str == "Channel" else "👥"
    await update.message.reply_text(
        f"✅ <b>Target Linked</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{icon} <b>{html.escape(chat.title or 'Unnamed')}</b>\n"
        f"🆔 <code>{target_id}</code>\n"
        f"👤 <b>Your role:</b> {user_reason}\n\n"
        "Ab aap is target par post kar sakte hain!",
        reply_markup=kb_main_menu(),
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


async def create_post_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    channels = await get_user_channels(query.from_user.id)

    if not channels:
        await query.edit_message_text(
            "⚠️ <b>No target added</b>\n\n"
            "Post banane se pehle ek Channel ya Group add karein.",
            reply_markup=InlineKeyboardMarkup([
                [btn("⚙️ Set Target", callback_data="menu_set_channel", style="primary")],
                [btn("🔙 Main Menu", callback_data="main_menu", style="primary")],
            ]),
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    keyboard = []
    for ch in channels:
        icon = "📢" if ch.get("chat_type") == "Channel" else "👥"
        keyboard.append([
            btn(f"{icon} {ch['channel_title']}",
                callback_data=f"sel_chan_{ch['channel_id']}",
                style="primary")
        ])
    keyboard.append([btn("❌ Cancel", callback_data="cancel_action", style="danger")])

    await query.edit_message_text(
        "📝 <b>Create Post — 1/6</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Kis target par post karni hai?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML,
    )
    return STATE_SELECT_CHANNEL


async def select_channel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    try:
        channel_id = int(data.split("_")[2])
    except (IndexError, ValueError):
        await query.edit_message_text("❌ Invalid selection.", reply_markup=kb_main_menu())
        return ConversationHandler.END

    channel = await get_user_channel(query.from_user.id, channel_id)
    if not channel:
        await query.edit_message_text(
            "⛔ Yeh target aapke account se linked nahi hai.",
            reply_markup=kb_main_menu(),
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    context.user_data["draft"] = {
        "channel_id": channel["channel_id"],
        "channel_title": channel["channel_title"],
        "chat_type": channel.get("chat_type", "Channel"),
        "post_type": None,
        "text": None,
        "media_file_id": None,
        "caption": None,
        "buttons": [],
        "button_style": None,
    }

    keyboard = InlineKeyboardMarkup([
        [btn("📄 Text", callback_data="post_type_text", style="primary"),
         btn("🖼 Photo", callback_data="post_type_photo", style="primary"),
         btn("🎥 Video", callback_data="post_type_video", style="primary")],
        [btn("❌ Cancel", callback_data="cancel_action", style="danger")],
    ])

    await query.edit_message_text(
        "📝 <b>Create Post — 2/6</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🎯 <b>Target:</b> {html.escape(channel['channel_title'])}\n\n"
        "Post ka format chunein:",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )
    return STATE_SELECT_TYPE


async def select_post_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    post_type = query.data.replace("post_type_", "")

    draft = context.user_data.get("draft")
    if not draft:
        await query.edit_message_text("❌ Session expired.", reply_markup=kb_main_menu())
        return ConversationHandler.END

    draft["post_type"] = post_type

    prompts = {
        "text": ("📝 <b>Create Post — 3/6</b>\n"
                 "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                 "Apna message text bhejein.\n"
                 "<i>HTML supported — &lt;b&gt;, &lt;i&gt;, &lt;a href&gt;</i>"),
        "photo": ("🖼 <b>Create Post — 3/6</b>\n"
                  "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                  "Photo upload karein.\n"
                  "<i>Caption saath me bhej sakte hain.</i>"),
        "video": ("🎥 <b>Create Post — 3/6</b>\n"
                  "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                  "Video upload karein.\n"
                  "<i>Caption saath me bhej sakte hain.</i>"),
    }

    await query.edit_message_text(
        prompts.get(post_type, "Content bhejein:"),
        reply_markup=kb_cancel(),
        parse_mode=ParseMode.HTML,
    )
    return STATE_ENTER_CONTENT


async def enter_content_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    track_msg(context, update.message.message_id)
    draft = context.user_data.get("draft")
    if not draft:
        await update.message.reply_text("❌ Session expired.", reply_markup=kb_main_menu())
        return ConversationHandler.END

    post_type = draft["post_type"]

    if post_type == "text":
        if not update.message.text:
            await send_tracked(update.message, context, "❌ Text message bhejein.", reply_markup=kb_cancel())
            return STATE_ENTER_CONTENT
        draft["text"] = update.message.text_html
        return await ask_button_step(update, context)

    if post_type == "photo":
        if not update.message.photo:
            await send_tracked(update.message, context, "❌ Photo bhejein.", reply_markup=kb_cancel())
            return STATE_ENTER_CONTENT
        draft["media_file_id"] = update.message.photo[-1].file_id
        if update.message.caption:
            draft["caption"] = update.message.caption_html
            return await ask_button_step(update, context)
        return await ask_caption_step(update, context)

    if post_type == "video":
        if not update.message.video:
            await send_tracked(update.message, context, "❌ Video bhejein.", reply_markup=kb_cancel())
            return STATE_ENTER_CONTENT
        draft["media_file_id"] = update.message.video.file_id
        if update.message.caption:
            draft["caption"] = update.message.caption_html
            return await ask_button_step(update, context)
        return await ask_caption_step(update, context)

    return ConversationHandler.END


async def ask_caption_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_tracked(
        update.message, context,
        "📝 <b>Create Post — 4/6</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Caption add karna chahenge?\n"
        "Text bhejein ya <b>Skip</b> dabayein.",
        reply_markup=kb_skip_cancel("skip_caption", "⏭ Skip Caption"),
        parse_mode=ParseMode.HTML,
    )
    return STATE_ENTER_CAPTION


async def enter_caption_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    track_msg(context, update.message.message_id)
    draft = context.user_data.get("draft")
    if not draft:
        await update.message.reply_text("❌ Session expired.", reply_markup=kb_main_menu())
        return ConversationHandler.END

    if update.message.text:
        draft["caption"] = update.message.text_html

    return await ask_button_step(update, context)


async def skip_caption_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    draft = context.user_data.get("draft")
    if not draft:
        await query.edit_message_text("❌ Session expired.", reply_markup=kb_main_menu())
        return ConversationHandler.END
    draft["caption"] = None
    return await ask_button_step(query, context, is_query=True)


async def ask_button_step(source: Any, context: ContextTypes.DEFAULT_TYPE, is_query: bool = False) -> int:
    msg = (
        "🔗 <b>Create Post — 5/6</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>URL Buttons (optional)</b>\n\n"
        "Format: <code>Label - https://link.com</code>\n\n"
        "<b>Examples:</b>\n"
        "• 1 button: <code>Website - https://example.com</code>\n"
        "• Emoji bhi allowed: <code>🔥 Join - https://t.me/x</code>\n"
        "• 1 row me 2: <code>Site - https://a.com | Join - https://t.me/x</code>\n\n"
        "Button nahi chahiye to <b>Skip</b> dabayein."
    )
    keyboard = kb_skip_cancel("skip_buttons", "⏭ Skip Buttons")

    if is_query:
        await source.edit_message_text(msg, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    else:
        await send_tracked(source.message, context, msg, reply_markup=keyboard, parse_mode=ParseMode.HTML)

    return STATE_ADD_BUTTONS


async def add_buttons_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    track_msg(context, update.message.message_id)
    draft = context.user_data.get("draft")
    if not draft:
        await update.message.reply_text("❌ Session expired.", reply_markup=kb_main_menu())
        return ConversationHandler.END

    text = update.message.text or ""
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    rows: List[List[Dict[str, str]]] = []

    for line in lines:
        row_items = []
        for raw_btn in line.split("|"):
            parts = re.split(r"\s+-\s+", raw_btn.strip(), maxsplit=1)
            if len(parts) != 2:
                await send_tracked(
                    update.message, context,
                    f"❌ <b>Format galat</b>\n\n"
                    f"Sahi: <code>Label - https://link.com</code>\n"
                    f"Aapne: <code>{html.escape(raw_btn.strip())}</code>",
                    parse_mode=ParseMode.HTML,
                )
                return STATE_ADD_BUTTONS

            btn_label = parts[0].strip()
            btn_url = parts[1].strip()

            if not is_valid_url(btn_url):
                await send_tracked(
                    update.message, context,
                    f"❌ <b>Invalid URL</b>: <code>{html.escape(btn_url)}</code>\n\n"
                    "Must start with <code>http://</code>, <code>https://</code> ya <code>tg://</code>",
                    parse_mode=ParseMode.HTML,
                )
                return STATE_ADD_BUTTONS

            row_items.append({"text": btn_label, "url": btn_url})
        if row_items:
            rows.append(row_items)

    draft["buttons"] = rows

    keyboard = InlineKeyboardMarkup([
        [btn("🔵 Primary", callback_data="btncolor_primary", style="primary"),
         btn("🟢 Success", callback_data="btncolor_success", style="success"),
         btn("🔴 Danger", callback_data="btncolor_danger", style="danger")],
        [btn("❌ Cancel", callback_data="cancel_action", style="danger")],
    ])

    await send_tracked(
        update.message, context,
        "🎨 <b>Create Post — 6/6</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Buttons ban gaye! Ab inka <b>color</b> chunein:\n\n"
        "🔵 <b>Primary</b> — blue tone\n"
        "🟢 <b>Success</b> — green tone\n"
        "🔴 <b>Danger</b> — red tone",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )
    return STATE_SELECT_BUTTON_COLOR


async def skip_buttons_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    draft = context.user_data.get("draft")
    if not draft:
        await query.edit_message_text("❌ Session expired.", reply_markup=kb_main_menu())
        return ConversationHandler.END
    draft["buttons"] = []
    draft["button_style"] = None
    return await show_preview(query, context, is_query=True)


async def select_button_color_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    style = query.data.replace("btncolor_", "")
    draft = context.user_data.get("draft")
    if not draft:
        await query.edit_message_text("❌ Session expired.", reply_markup=kb_main_menu())
        return ConversationHandler.END

    draft["button_style"] = style
    for row in draft.get("buttons", []):
        for b in row:
            b["style"] = style

    return await show_preview(query, context, is_query=True)


async def show_preview(source: Any, context: ContextTypes.DEFAULT_TYPE, is_query: bool = False) -> int:
    draft = context.user_data.get("draft")
    if not draft:
        return ConversationHandler.END

    post_type = draft["post_type"]
    post_markup = build_post_markup(draft.get("buttons", []))

    target_msg = source.message

    header = (
        "👁️ <b>Post Preview</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>Target:</b> {html.escape(draft['channel_title'])} "
        f"(<code>{draft['channel_id']}</code>)\n"
    )
    if draft.get("button_style"):
        header += f"🎨 <b>Button color:</b> {COLOR_EMOJI.get(draft['button_style'], '')} {draft['button_style'].capitalize()}\n"
    header += "━━━━━━━━━━━━━━━━━━━━━━━\n\nNeeche preview 👇"

    sent_header = await target_msg.reply_text(header, parse_mode=ParseMode.HTML)
    track_msg(context, sent_header.message_id)

    try:
        if post_type == "text":
            sent = await target_msg.reply_text(
                draft["text"],
                parse_mode=ParseMode.HTML,
                reply_markup=post_markup,
                disable_web_page_preview=False,
            )
        elif post_type == "photo":
            sent = await target_msg.reply_photo(
                photo=draft["media_file_id"],
                caption=draft.get("caption"),
                parse_mode=ParseMode.HTML,
                reply_markup=post_markup,
            )
        elif post_type == "video":
            sent = await target_msg.reply_video(
                video=draft["media_file_id"],
                caption=draft.get("caption"),
                parse_mode=ParseMode.HTML,
                reply_markup=post_markup,
            )
        else:
            sent = None
        if sent:
            track_msg(context, sent.message_id)
    except TelegramError as e:
        sent = await target_msg.reply_text(f"⚠️ Preview error: {html.escape(e.message)}")
        track_msg(context, sent.message_id)

    confirm_keyboard = InlineKeyboardMarkup([
        [btn("🚀 Publish Now", callback_data="publish_confirm", style="success")],
        [btn("❌ Cancel", callback_data="cancel_action", style="danger")],
    ])

    sent_confirm = await target_msg.reply_text(
        "Sab theek lag raha hai?\nPublish karne ke liye <b>🚀 Publish Now</b> dabayein.",
        reply_markup=confirm_keyboard,
        parse_mode=ParseMode.HTML,
    )
    track_msg(context, sent_confirm.message_id)

    return STATE_CONFIRM_PUBLISH


async def publish_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    draft = context.user_data.get("draft")

    if not draft:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ Draft expired.",
            reply_markup=kb_main_menu(),
        )
        return ConversationHandler.END

    channel_id = draft["channel_id"]
    user_ok, _ = await verify_user_channel_ownership(context.bot, channel_id, user_id)
    if not user_ok:
        await purge_trash(context, update.effective_chat.id)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⛔ <b>Security Block</b>\n\nAap ab is target ke Admin nahi hain.",
            reply_markup=kb_main_menu(),
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    post_type = draft["post_type"]
    post_markup = build_post_markup(draft.get("buttons", []))
    published_msg = None
    content_preview = ""

    try:
        if post_type == "text":
            published_msg = await context.bot.send_message(
                chat_id=channel_id, text=draft["text"],
                parse_mode=ParseMode.HTML, reply_markup=post_markup,
            )
            content_preview = re.sub(r"<[^>]*>", "", draft["text"])
        elif post_type == "photo":
            published_msg = await context.bot.send_photo(
                chat_id=channel_id, photo=draft["media_file_id"],
                caption=draft.get("caption"),
                parse_mode=ParseMode.HTML, reply_markup=post_markup,
            )
            content_preview = re.sub(r"<[^>]*>", "", draft.get("caption") or "[Photo]")
        elif post_type == "video":
            published_msg = await context.bot.send_video(
                chat_id=channel_id, video=draft["media_file_id"],
                caption=draft.get("caption"),
                parse_mode=ParseMode.HTML, reply_markup=post_markup,
            )
            content_preview = re.sub(r"<[^>]*>", "", draft.get("caption") or "[Video]")
    except TelegramError as e:
        logger.error("Failed to post: %s", e.message)
        await purge_trash(context, update.effective_chat.id)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ <b>Publishing Failed</b>\n\n{html.escape(e.message)}",
            reply_markup=kb_main_menu(),
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    if published_msg:
        await save_published_post(
            user_id=user_id, channel_id=channel_id,
            channel_title=draft["channel_title"],
            post_type=post_type, content_preview=content_preview,
            telegram_message_id=published_msg.message_id,
        )

    draft_title = draft["channel_title"]
    context.user_data.pop("draft", None)

    await purge_trash(context, update.effective_chat.id)

    try:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                "🎉 <b>Post Published Successfully!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🎯 <b>Target:</b> {html.escape(draft_title)}\n"
                f"🆔 <b>Message ID:</b> <code>{published_msg.message_id if published_msg else 'N/A'}</code>\n\n"
                "Main menu:"
            ),
            reply_markup=kb_main_menu(),
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as e:
        logger.error("Failed to send success message: %s", e)

    return ConversationHandler.END


async def cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id

    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.message.delete()
        except TelegramError:
            pass
    elif update.message:
        try:
            await update.message.delete()
        except TelegramError:
            pass

    await purge_trash(context, chat_id)
    context.user_data.pop("draft", None)

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text="✅ <b>Action cancelled.</b>\n\nMain menu:",
            reply_markup=kb_main_menu(),
            parse_mode=ParseMode.HTML,
        )
    except TelegramError:
        pass

    return ConversationHandler.END


def main() -> None:
    token = BOT_TOKEN.strip()
    if not token:
        sys.stderr.write("CRITICAL ERROR: BOT_TOKEN is empty.\n")
        sys.exit(1)

    async def _post_init(app: Application) -> None:
        await init_db()

    app: Application = ApplicationBuilder().token(token).post_init(_post_init).build()

    # Keep callback failures visible instead of leaving Telegram buttons spinning.
    async def _button_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.exception("Unhandled bot update error", exc_info=context.error)
        if isinstance(update, Update) and update.callback_query:
            try:
                await update.callback_query.answer(
                    "This button hit an error. Please try again or send /start.",
                    show_alert=True,
                )
            except TelegramError:
                pass

    app.add_error_handler(_button_error_handler)

    create_post_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(create_post_start, pattern="^menu_create_post$")],
        states={
            STATE_SELECT_CHANNEL: [
                CallbackQueryHandler(select_channel_callback, pattern=r"^sel_chan_")
            ],
            STATE_SELECT_TYPE: [
                CallbackQueryHandler(select_post_type_callback, pattern=r"^post_type_")
            ],
            STATE_ENTER_CONTENT: [
                MessageHandler(
                    (filters.TEXT | filters.PHOTO | filters.VIDEO) & ~filters.COMMAND,
                    enter_content_process,
                )
            ],
            STATE_ENTER_CAPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_caption_process),
                CallbackQueryHandler(skip_caption_callback, pattern="^skip_caption$"),
            ],
            STATE_ADD_BUTTONS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_buttons_process),
                CallbackQueryHandler(skip_buttons_callback, pattern="^skip_buttons$"),
            ],
            STATE_SELECT_BUTTON_COLOR: [
                CallbackQueryHandler(select_button_color_callback, pattern="^btncolor_")
            ],
            STATE_CONFIRM_PUBLISH: [
                CallbackQueryHandler(publish_confirm_callback, pattern="^publish_confirm$")
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_action),
            CommandHandler("start", start_command),
        ],
        per_user=True,
        per_chat=True,
    )

    set_channel_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(set_channel_start, pattern="^menu_set_channel$")],
        states={
            STATE_SET_CHANNEL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_channel_process)
            ]
        },
        fallbacks=[
            CommandHandler("cancel", cancel_action),
            CommandHandler("start", start_command),
        ],
        per_user=True,
        per_chat=True,
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_callback))
    app.add_handler(CommandHandler("set_welcome", set_welcome_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("add_forcement", add_forcement_command))
    app.add_handler(CommandHandler("remove_forcement", remove_forcement_command))
    app.add_handler(CommandHandler("forcement_list", forcement_list_command))
    app.add_handler(CommandHandler("edit", edit_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CallbackQueryHandler(owner_panel_callback, pattern="^owner_panel$"), group=1)
    app.add_handler(CallbackQueryHandler(force_recheck_callback, pattern="^force_recheck$"), group=1)
    app.add_handler(CallbackQueryHandler(force_join_info_callback, pattern="^force_join_info$"), group=1)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, broadcast_message_handler, block=False), group=-1)
    app.add_handler(MessageHandler(filters.FORWARDED & ~filters.COMMAND, edit_forward_handler, block=False), group=2)
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.VIDEO) & ~filters.COMMAND,
        edit_content_handler, block=False,
    ), group=2)
    app.add_handler(MessageHandler(
        (filters.PHOTO | filters.VIDEO | filters.ANIMATION) & ~filters.COMMAND,
        welcome_media_handler, block=False,
    ), group=2)
    app.add_handler(set_channel_conv)
    app.add_handler(create_post_conv)

    app.add_handler(CallbackQueryHandler(start_command, pattern="^main_menu$"), group=1)
    app.add_handler(CallbackQueryHandler(help_callback, pattern="^menu_help$"), group=1)
    app.add_handler(CallbackQueryHandler(my_channels_callback, pattern="^menu_my_channels$"), group=1)
    app.add_handler(CallbackQueryHandler(my_posts_callback, pattern="^menu_my_posts$"), group=1)
    app.add_handler(CallbackQueryHandler(remove_target_menu_callback, pattern="^remove_target_menu$"), group=1)
    app.add_handler(CallbackQueryHandler(delete_channel_prompt_callback, pattern=r"^del_chan_"), group=1)
    app.add_handler(CallbackQueryHandler(confirm_delete_channel_callback, pattern=r"^confirm_del_"), group=1)
    app.add_handler(CallbackQueryHandler(cancel_action, pattern="^cancel_action$"), group=1)

    logger.info("Bot starting — emoji buttons + fixed publish success message...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
