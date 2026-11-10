"""
Settings Menu Plugin - FIXED: Removed Manual Update Dual (now in main menu)
Dual Update Settings button remains for channel configuration only
"""

from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import CallbackQueryHandler
from plugins.helpers import is_admin, is_owner
from db import update_settings
import logging

logger = logging.getLogger(__name__)

def register(app):
    """Register settings handlers"""
    app.add_handler(CallbackQueryHandler(show_menu, pattern="^menu:settings$"))
    app.add_handler(CallbackQueryHandler(delete_all_confirm, pattern="^settings:deleteallchannels$"))
    app.add_handler(CallbackQueryHandler(delete_all_execute, pattern="^deleteall:yes$"))
    app.add_handler(CallbackQueryHandler(delete_all_cancel, pattern="^deleteall:no$"))

async def show_menu(update, context):
    """
    Show settings menu with all options
    FIXED: Dual Update Settings is only for channel config, not manual dual
    """
    query = update.callback_query
    await query.answer()
    
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Unauthorized", show_alert=True)
        return
    
    user_id = query.from_user.id
    from db import update_channel_prefs
    
    user_pref = update_channel_prefs.find_one({"_id": user_id})
    update_enabled = user_pref.get("enabled", True) if user_pref else True
    
    config = update_settings.find_one({"_id": "global"})
    has_update_channel = config and config.get("update_channel_id") is not None
    
    buttons = [
        [InlineKeyboardButton(
            "sᴇᴛ ᴄʜᴀɴɴᴇʟ",
            callback_data="settings:setchannel"
        )],
        [InlineKeyboardButton(
            "sᴇᴛ ғᴏʀᴍᴀᴛ",
            callback_data="settings:setformat"
        )],
        [InlineKeyboardButton(
            "📝 sᴇᴛ ᴜᴘᴅᴀᴛᴇ ғᴏʀᴍᴀᴛ",
            callback_data="settings:setupdateformat"
        )],
        [InlineKeyboardButton(
            "ᴠɪᴇᴡ ғᴏʀᴍᴀᴛs",
            callback_data="settings:viewformats"
        )],
        [InlineKeyboardButton(
            "ᴇᴅɪᴛ ᴘᴏsᴛ",
            callback_data="settings:editpost"
        )],
        [InlineKeyboardButton(
            "💾 sᴀᴠᴇᴅ ᴘᴏsᴛs",
            callback_data="settings:savedposts"
        )],
        [InlineKeyboardButton(
            "ᴅᴇʟᴇᴛᴇ ᴄʜᴀɴɴᴇʟ",
            callback_data="settings:deletechannel"
        )],
        [InlineKeyboardButton(
            "🗑 ᴅᴇʟᴇᴛᴇ ᴀʟʟ ᴄʜᴀɴɴᴇʟs",
            callback_data="settings:deleteallchannels"
        )],
        [InlineKeyboardButton(
            "ғᴏʟʟᴏᴡ ᴜᴘ ᴍᴇssᴀɢᴇ",
            callback_data="settings:followups"
        )],
        [InlineKeyboardButton(
            "📢 sᴇᴛ ᴜᴘᴅᴀᴛᴇ ᴄʜᴀɴɴᴇʟ",
            callback_data="settings:updatechannel"
        )],
        # FIXED: This is ONLY for dual channel configuration, not manual dual
        [InlineKeyboardButton(
            "⚙️ ᴅᴜᴀʟ ᴜᴘᴅᴀᴛᴇ sᴇᴛᴛɪɴɢs",
            callback_data="settings:dualsettings"
        )]
    ]
    
    if has_update_channel:
        buttons.append([InlineKeyboardButton(
            f"{'✅' if update_enabled else '❌'} ᴜᴘᴅᴀᴛᴇ ᴄʜᴀɴɴᴇʟ: {'ᴏɴ' if update_enabled else 'ᴏғғ'}",
            callback_data="settings:toggleupdate"
        )])
        
        buttons.append([InlineKeyboardButton(
            "🗑️ ᴅᴇʟᴇᴛᴇ ᴜᴘᴅᴀᴛᴇ ᴄʜᴀɴɴᴇʟ",
            callback_data="settings:deleteupdate"
        )])
    
    buttons.append([InlineKeyboardButton(
        "ʙᴀᴄᴋ",
        callback_data="menu:back"
    )])
    
    try:
        await query.message.edit_text(
            "ᴄʜᴏᴏsᴇ ᴡʜᴀᴛ ʏᴏᴜ ᴡᴀɴᴛ ᴛᴏ ᴄʜᴀɴɢᴇ.",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Settings menu edit failed: {e}")
        await query.message.reply_text(
            "⚙️ *Settings Menu*\n\n"
            "Configure your bot settings:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown"
        )

async def delete_all_confirm(update, context):
    """Show confirmation dialog for deleting all channels"""
    query = update.callback_query
    await query.answer()
    
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Unauthorized", show_alert=True)
        return
    
    from db import channels
    channel_count = channels.count_documents({})
    
    if channel_count == 0:
        await query.answer("⚠️ No channels to delete", show_alert=True)
        return
    
    try:
        await query.message.edit_text(
            f"⚠️ *DELETE ALL CHANNELS*\n\n"
            f"**This will:**\n"
            f"• Delete {channel_count} channel(s)\n"
            f"• Delete all formats\n"
            f"• Clear channel database\n\n"
            f"**This will NOT:**\n"
            f"• Delete post history\n"
            f"• Affect Edit Post feature\n"
            f"• Delete messages from channels\n\n"
            f"**Are you sure?**",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Yes, Delete All", callback_data="deleteall:yes"),
                    InlineKeyboardButton("❌ Cancel", callback_data="deleteall:no")
                ]
            ]),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error showing delete all confirm: {e}")

async def delete_all_execute(update, context):
    """Execute deletion of all channels and formats"""
    query = update.callback_query
    await query.answer()
    
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Unauthorized", show_alert=True)
        return
    
    from db import channels, formats
    
    channel_count = channels.count_documents({})
    format_count = formats.count_documents({})
    
    channels.delete_many({})
    formats.delete_many({})
    
    logger.info(f"Deleted {channel_count} channels and {format_count} formats by user {query.from_user.id}")
    
    try:
        await query.message.edit_text(
            f"✅ *All Channels Deleted*\n\n"
            f"**Deleted:**\n"
            f"• {channel_count} channel(s)\n"
            f"• {format_count} format(s)\n\n"
            f"**Preserved:**\n"
            f"• Post history intact\n"
            f"• Edit Post still works\n\n"
            f"You can now add new channels.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back to Settings", callback_data="menu:settings")
            ]]),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error showing delete all success: {e}")

async def delete_all_cancel(update, context):
    """Cancel delete all operation"""
    query = update.callback_query
    await query.answer("❌ Cancelled")
    
    await show_menu(update, context)
