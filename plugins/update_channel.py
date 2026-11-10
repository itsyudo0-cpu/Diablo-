"""
Update Channel Plugin - STABLE
"""

from telegram.ext import CallbackQueryHandler, CommandHandler
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from plugins.helpers import (
    is_admin,
    update_session,
    get_session,
    clean_session,
    show_menu_safe,
    channel_keyboard,
    escape_md,
    sync_channel_setup,
    build_setup_buttons,
    build_setup_text
)
from db import update_settings, update_channel_prefs, channels
import logging
import re

logger = logging.getLogger(__name__)

# ==================== REGISTRATION ====================
def register(app):
    """Register update channel handlers"""
    app.add_handler(CallbackQueryHandler(start_set_channel, pattern="^settings:updatechannel$"))
    app.add_handler(CallbackQueryHandler(toggle_update_channel, pattern="^settings:toggleupdate$"))
    app.add_handler(CallbackQueryHandler(delete_update_channel, pattern="^settings:deleteupdate$"))
    app.add_handler(CallbackQueryHandler(cancel_set_channel, pattern="^updatech:cancel$"))
    app.add_handler(CallbackQueryHandler(select_update_format_channel, pattern="^updatefmt:"))
    app.add_handler(CommandHandler("setupdateformat", start_set_update_format))
    app.add_handler(CallbackQueryHandler(show_update_format_channels, pattern="^settings:setupdateformat$"))

# ==================== START SET CHANNEL ====================
async def start_set_channel(update, context):
    """Start update channel setup"""
    query = update.callback_query
    await query.answer()
    
    if not is_admin(query.from_user.id):
        return
    
    config = update_settings.find_one({"_id": "global"})
    current_channel = config.get("update_channel_username") or config.get("update_channel_id") if config else None
    
    status_text = ""
    if current_channel:
        status_text = f"\n\n*Current:* `{current_channel}`"
    
    update_session(query.from_user.id, {
        "step": "update_channel"
    })
    
    try:
        await query.message.edit_text(
            "📢 *Set Update Channel*\n\n"
            "Send channel username or channel ID.\n\n"
            "**Format:**\n"
            "• Username: `@channelusername`\n"
            "• Channel ID: `-1001234567890`"
            f"{status_text}\n\n"
            "⚠️ Bot must be admin in the channel",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Cancel", callback_data="updatech:cancel")
            ]]),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error editing menu for update channel: {e}")

# ==================== CANCEL SET CHANNEL ====================
async def cancel_set_channel(update, context):
    """Cancel update channel setup and return to settings menu"""
    query = update.callback_query
    await query.answer("❌ Cancelled")
    
    user_id = query.from_user.id
    
    clean_session(user_id)
    
    logger.info(f"Update channel setup cancelled by user {user_id}")
    
    from plugins.settings import show_menu
    await show_menu(update, context)
    
# ==================== SETTINGS BUTTON: SET UPDATE FORMAT ====================
async def show_update_format_channels(update, context):
    """Settings -> Set Update Format: pick a channel, then send its update template"""
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        return

    clean_session(query.from_user.id)

    await query.message.edit_text(
        "📝 *Set Update Format*\n\n"
        "Select the channel whose update-channel format you want to set:",
        reply_markup=channel_keyboard(0, "updatefmt"),
        parse_mode="Markdown"
    )

# ==================== SET UPDATE FORMAT ====================
async def start_set_update_format(update, context):
    """Start channel-wise update channel template flow via /setupdateformat"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        return
    
    update_session(user_id, {
        "step": "update_format_select_channel"
    })
    
    await update.message.reply_text(
        "📝 *Set Update Channel Format*\n\n"
        "Flow: `/setupdateformat` → Select Channel → Send Format → Saved ✅\n\n"
        "Please select the channel for which you want to save update format:",
        reply_markup=channel_keyboard(0, "updatefmt"),
        parse_mode="Markdown"
    )

async def select_update_format_channel(update, context):
    """Select source channel for update redirect template"""
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        return

    parts = query.data.split(":")
    channel_id = parts[1]

    ch = channels.find_one({"_id": channel_id})
    channel_title = ch.get("title", channel_id) if ch else channel_id

    config = update_settings.find_one({"_id": "global"}) or {}
    channel_templates = config.get("update_channel_formats") or {}
    current = channel_templates.get(channel_id)

    current_text = "\n\n*Current template:*\n_Not set_"
    if isinstance(current, dict) and current.get("caption"):
        current_text = f"\n\n*Current template:*\n`{current['caption'].replace(chr(96), chr(39))}`"

    update_session(query.from_user.id, {
        "step": "update_channel_format",
        "source_channel_for_update_format": channel_id
    })

    await query.message.reply_text(
        f"📝 *Set Update Format For:* {escape_md(channel_title)}\n\n"
        "Send caption template for update channel post.\n"
        "Template must contain `{episode}`.\n\n"
        "✅ Text formatting will be preserved (bold/italic/mono).\n\n"
        "Example:\n"
        "`Episode {episode} Added ✅`"
        f"{current_text}\n\n"
        "To remove this channel's custom format, send: `OFF`",
        parse_mode="Markdown"
    )

# ==================== RECEIVE CHANNEL ====================
async def receive_update_channel(update, context):
    """Receive and validate update channel"""
    user_id = update.effective_user.id
    
    channel_input = update.message.text.strip()
    
    channel_id = None
    channel_username = None
    
    if channel_input.startswith("@"):
        channel_username = channel_input
        try:
            chat = await context.bot.get_chat(channel_username)
            channel_id = str(chat.id)
        except Exception as e:
            await update.message.reply_text(
                f"❌ *Cannot Access Channel*\n\n"
                f"Username: `{channel_username}`\n"
                f"Error: `{str(e)}`\n\n"
                f"Make sure bot is admin in the channel.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back to Settings", callback_data="menu:settings")
                ]]),
                parse_mode="Markdown"
            )
            clean_session(user_id)
            return
    
    elif re.match(r'^-100\d{10,}$', channel_input):
        channel_id = channel_input
        try:
            chat = await context.bot.get_chat(channel_id)
            if hasattr(chat, 'username') and chat.username:
                channel_username = f"@{chat.username}"
        except Exception as e:
            await update.message.reply_text(
                f"❌ *Cannot Access Channel*\n\n"
                f"ID: `{channel_id}`\n"
                f"Error: `{str(e)}`\n\n"
                f"Make sure bot is admin in the channel.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back to Settings", callback_data="menu:settings")
                ]]),
                parse_mode="Markdown"
            )
            clean_session(user_id)
            return
    
    else:
        await update.message.reply_text(
            "❌ *Invalid Format*\n\n"
            "Send either:\n"
            "• `@channelusername`\n"
            "• `-1001234567890`",
            parse_mode="Markdown"
        )
        return
    
    try:
        chat_member = await context.bot.get_chat_member(channel_id, context.bot.id)
        if chat_member.status not in ["administrator", "creator"]:
            await update.message.reply_text(
                "❌ *Bot Not Admin*\n\n"
                "Bot must be admin in the update channel.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back to Settings", callback_data="menu:settings")
                ]]),
                parse_mode="Markdown"
            )
            clean_session(user_id)
            return
        
        update_settings.update_one(
            {"_id": "global"},
            {"$set": {
                "update_channel_id": channel_id,
                "update_channel_username": channel_username,
                "update_channel_enabled": True
            }},
            upsert=True
        )
        
        clean_session(user_id)
        
        display_name = channel_username or channel_id
        
        await update.message.reply_text(
            f"✅ *Update Channel Set*\n\n"
            f"*Channel:* `{display_name}`\n"
            f"*ID:* `{channel_id}`\n"
            f"*Status:* ✅ Enabled\n\n"
            f"Redirect posts will be sent here after each episode.",
            parse_mode="Markdown"
        )
        
        await show_menu_safe(update, context, user_id)
        
        logger.info(f"Update channel set: {channel_id} by user {user_id}")
        
    except Exception as e:
        logger.error(f"Error setting update channel: {e}")
        clean_session(user_id)
        
        await update.message.reply_text(
            f"❌ *Error Setting Channel*\n\n"
            f"Error: `{str(e)}`",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back to Settings", callback_data="menu:settings")
            ]]),
            parse_mode="Markdown"
        )
async def receive_update_channel_format(update, context):
    """Receive and save channel-wise redirect caption template"""
    user_id = update.effective_user.id
    s = update_settings.find_one({"_id": "global"}) or {}
    session = get_session(user_id)

    source_channel_id = session.get("source_channel_for_update_format") if session else None

    if not source_channel_id:
        await update.message.reply_text(
            "❌ No channel selected. Run `/setupdateformat` first.",
            parse_mode="Markdown"
        )
        clean_session(user_id)
        return

    text = update.message.text.strip()
    channel_templates = s.get("update_channel_formats") or {}
    
    if text.upper() == "OFF":
        channel_templates.pop(source_channel_id, None)
        update_settings.update_one(
            {"_id": "global"},
            {"$set": {"update_channel_formats": channel_templates}},
            upsert=True
        )
        clean_session(user_id)

        ch = channels.find_one({"_id": source_channel_id})
        channel_title = ch.get("title", source_channel_id) if ch else source_channel_id

        await update.message.reply_text(
            f"✅ Custom update format removed for *{escape_md(channel_title)}*.\n"
            "Now redirect caption will use source post caption.",
            parse_mode="Markdown"
        )
        return
    
    if "{episode}" not in text:
        await update.message.reply_text(
            "❌ Template must contain `{episode}`.\n\n"
            "Example: `Episode {episode} Added ✅`",
            parse_mode="Markdown"
        )
        return
    
    entities_data = None
    if update.message.entities:
        entities_data = [
            {
                "type": e.type,
                "offset": e.offset,
                "length": e.length,
                "url": e.url if e.url else None,
                "user": e.user.id if e.user else None,
                "language": e.language if e.language else None,
                "custom_emoji_id": e.custom_emoji_id if e.custom_emoji_id else None
            }
            for e in update.message.entities
        ]

    channel_templates[source_channel_id] = {
        "caption": text,
        "entities": entities_data
    }

    update_settings.update_one(
        {"_id": "global"},
        {"$set": {"update_channel_formats": channel_templates}},
        upsert=True
    )
    
    clean_session(user_id)
    
    ch = channels.find_one({"_id": source_channel_id})
    channel_title = ch.get("title", source_channel_id) if ch else source_channel_id

    # New channel still in setup -> continue the 2-step setup
    if ch and ch.get("setup_done") is False:
        _fmt_ok, _upd_ok, _done = sync_channel_setup(source_channel_id)
        if _done:
            await update.message.reply_text(
                f"🎉 *Channel Set Successfully!*\n\n"
                f"*Channel:* {escape_md(channel_title)}\n"
                f"✅ Format\n✅ Update Format\n\n"
                f"This channel is now ready for posting.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Add Next Channel", callback_data="addnext")],
                    [InlineKeyboardButton("✅ Done", callback_data="cancelafter")]
                ]),
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                build_setup_text(source_channel_id, "✅ *Update Format Saved!*"),
                reply_markup=build_setup_buttons(source_channel_id),
                parse_mode="Markdown"
            )
        return

    await update.message.reply_text(
        f"✅ Update channel format saved for *{escape_md(channel_title)}*.\n\n"
        f"*Template:* `{text.replace(chr(96), chr(39))}`\n\n"
        "Redirect posts will replace `{episode}` automatically and keep text formatting.",
        parse_mode="Markdown"
    )

# ==================== TOGGLE UPDATE CHANNEL ====================
async def toggle_update_channel(update, context):
    """Toggle update channel on/off FOR CURRENT USER"""
    query = update.callback_query
    
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Unauthorized", show_alert=True)
        return
    
    config = update_settings.find_one({"_id": "global"})
    
    if not config or not config.get("update_channel_id"):
        await query.answer("⚠️ Set update channel first!", show_alert=True)
        return
    
    user_id = query.from_user.id
    user_pref = update_channel_prefs.find_one({"_id": user_id})
    
    current = user_pref.get("enabled", True) if user_pref else True
    new_status = not current
    
    update_channel_prefs.update_one(
        {"_id": user_id},
        {"$set": {"enabled": new_status}},
        upsert=True
    )
    
    await query.answer(f"✅ Update channel {'enabled' if new_status else 'disabled'} for you")
    
    from plugins.settings import show_menu
    await show_menu(update, context)
    
    logger.info(f"Update channel toggled to {new_status} by user {user_id}")

# ==================== DELETE UPDATE CHANNEL ====================
async def delete_update_channel(update, context):
    """Delete/reset update channel completely"""
    query = update.callback_query
    
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Unauthorized", show_alert=True)
        return
    
    config = update_settings.find_one({"_id": "global"})
    
    if not config or not config.get("update_channel_id"):
        await query.answer("⚠️ No update channel set!", show_alert=True)
        return
    
    channel_name = config.get("update_channel_username") or config.get("update_channel_id")
    
    update_settings.update_one(
        {"_id": "global"},
        {"$set": {
            "update_channel_id": None,
            "update_channel_username": None,
            "update_channel_enabled": False
        }}
    )
    
    update_channel_prefs.delete_many({})
    
    await query.answer("✅ Update channel deleted", show_alert=False)
    
    logger.info(f"Update channel deleted: {channel_name} by user {query.from_user.id}")
    
    from plugins.settings import show_menu
    await show_menu(update, context)
