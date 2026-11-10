"""
UPDATED: Added auto dual detection after post send
         BUG FIX: follow-up always fires after successful send (not step-gated)
         BUG FIX: episode zero-padded in receive_episode
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
from telegram.ext import CallbackQueryHandler
from plugins.helpers import (
    is_admin,
    get_session,
    update_session,
    clean_session,
    channel_keyboard,
    build_quality_buttons,
    parse_quality_links,
    adjust_caption_entities,
    build_main_menu_buttons,
    build_menu_text
)
from db import channels, formats, posts, menu_messages
import logging

logger = logging.getLogger(__name__)

def register(app):
    app.add_handler(CallbackQueryHandler(show_channel_list, pattern="^menu:create$"))
    app.add_handler(CallbackQueryHandler(select_channel, pattern="^post:"))
    app.add_handler(CallbackQueryHandler(toggle_notify, pattern="^notify$"))
    app.add_handler(CallbackQueryHandler(send_post, pattern="^send$"))
    app.add_handler(CallbackQueryHandler(cancel, pattern="^cancel$"))
    app.add_handler(CallbackQueryHandler(pagination, pattern="^page:"))

async def show_channel_list(update, context):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Unauthorized", show_alert=True)
        return
    
    user_id = query.from_user.id
    clean_session(user_id)

    # AUTO MODE: upload the episode file(s) directly -> channel is detected
    # from the file name (channel title == video title). Manual selection
    # from the list below still works as a fallback.
    update_session(user_id, {
        "step": "collector",
        "channel": None,
        "notify": True
    })
    from plugins.smart_collector import init_collector
    init_collector(context, None)
    context.user_data.pop("collector_msg_id", None)
    context.user_data["channel_list_msg"] = (query.message.chat_id, query.message.message_id)
    
    try:
        await query.message.edit_text(
            "📺 *ᴄʀᴇᴀᴛᴇ ᴘᴏsᴛ*\n\n"
            "📤 *Send the episode file(s) now*\n\n"
            "The channel is auto-detected from the file name (channel title).",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data="menu:back")
            ]]),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error showing channel list: {e}")

async def select_channel(update, context):
    query = update.callback_query
    await query.answer()
    
    parts = query.data.split(":")
    channel_id = parts[1]
    
    from plugins.helpers import channel_ready
    fmt = formats.find_one({"_id": channel_id})
    if not fmt or not channel_ready(channel_id):
        await query.answer(
            "❌ ᴄʜᴀɴɴᴇʟ sᴇᴛᴜᴘ ɪɴᴄᴏᴍᴘʟᴇᴛᴇ!\n"
            "sᴇᴛ ʙᴏᴛʜ: ꜰᴏʀᴍᴀᴛ + ᴜᴘᴅᴀᴛᴇ ꜰᴏʀᴍᴀᴛ",
            show_alert=True
        )
        return
    
    update_session(query.from_user.id, {
        "step": "collector",
        "channel": channel_id,
        "notify": True
    })
    
    # Enter Smart Collector mode
    from plugins.smart_collector import enter_collector
    await enter_collector(update, context)

async def receive_episode(update, context):
    user_id = update.effective_user.id
    
    raw_episode = update.message.text.strip()
    
    if not raw_episode:
        await update.message.reply_text("❌ Episode number cannot be empty.")
        return
    
    # FIX BUG #1: always store episode as zero-padded 2-digit string
    episode = str(raw_episode).zfill(2)
    
    update_session(user_id, {
        "episode": episode,
        "step": "links"
    })
    
    await update.message.reply_text(
        f"🔗 *Send me URL buttons for this episode*\n\n"
        f"Please use this format:\n\n"
        f"`Button text 1 - https://link1.com && Button text 2 - https://link2.com`\n\n"
        f"or multi-line:\n"
        f"```\n"
        f"480p - https://link\n"
        f"720p - https://link\n"
        f"1080p - https://link\n"
        f"HDRip - https://link\n"
        f"```\n\n"
        f"⚠️ Button text can be anything (480p, 720p, Drive, Download, etc)\n"
        f"⚠️ You can send unlimited buttons\n\n"
        f"Choose *Cancel* to go back to post creation.",
        parse_mode="Markdown"
    )
    
    logger.info(f"Episode set: {episode}")

async def receive_links(update, context):
    user_id = update.effective_user.id
    
    links = parse_quality_links(update.message.text)
    
    if not links:
        await update.message.reply_text(
            "❌ *Invalid Format*\n\n"
            "Use: `quality - url && quality - url`\n\n"
            "Example:\n"
            "`480p - https://link1.com && 720p - https://link2.com`",
            parse_mode="Markdown"
        )
        return
    
    update_session(user_id, {
        "links": links,
        "step": "preview"
    })
    
    await show_preview(update, context)

async def show_preview(update, context):
    user_id = update.effective_user.id
    s = get_session(user_id)
    
    if not s:
        return
    
    channel_id = s["channel"]
    episode = s["episode"]
    links = s["links"]
    notify = s.get("notify", True)
    
    fmt = formats.find_one({"_id": channel_id})
    if not fmt:
        await update.message.reply_text("❌ Format not found")
        clean_session(user_id)
        return
    
    caption_template = fmt["caption"]
    entities_template = fmt.get("caption_entities")
    
    final_caption, final_entities = adjust_caption_entities(
        caption=caption_template,
        entities=entities_template,
        placeholder="{episode}",
        replacement=episode
    )
    
    quality_rows = build_quality_buttons(links, buttons_per_row=3)
    
    control_rows = [
        [InlineKeyboardButton(
            "🔔 ɴᴏᴛɪꜰʏ: ᴏɴ" if notify else "🔕 ɴᴏᴛɪꜰʏ: ᴏꜰꜰ",
            callback_data="notify"
        )],
        [
            InlineKeyboardButton("✅ ꜱᴇɴᴅ ᴘᴏꜱᴛ", callback_data="send"),
            InlineKeyboardButton("❌ ᴄᴀɴᴄᴇʟ", callback_data="cancel")
        ]
    ]
    
    all_buttons = quality_rows + control_rows
    
    entities_objects = None
    if final_entities:
        entities_objects = [
            MessageEntity(
                type=e["type"],
                offset=e["offset"],
                length=e["length"],
                url=e.get("url"),
                user=e.get("user"),
                language=e.get("language"),
                custom_emoji_id=e.get("custom_emoji_id")
            )
            for e in final_entities
        ]
    
    await update.message.reply_text(
        "📺 *PREVIEW*\n\n"
        "ᴛʜɪꜱ ɪꜱ ᴇxᴀᴄᴛʟʏ ʜᴏᴡ ʏᴏᴜʀ ᴘᴏꜱᴛ ᴡɪʟʟ ᴀᴘᴘᴇᴀʀ ɪɴ ᴛʜᴇ ᴄʜᴀɴɴᴇʟ.\n"
        "ʀᴇᴠɪᴇᴡ ᴄᴀʀᴇꜰᴜʟʟʏ ʙᴇꜰᴏʀᴇ ꜱᴇɴᴅɪɴɢ.",
        parse_mode="Markdown"
    )
    
    try:
        preview_msg = await update.message.reply_photo(
            photo=fmt["poster_file_id"],
            caption=final_caption,
            caption_entities=entities_objects,
            reply_markup=InlineKeyboardMarkup(all_buttons)
        )
        
        update_session(user_id, {"preview_message_id": preview_msg.message_id})
        
        logger.info(f"Preview shown for channel {channel_id}, episode {episode}")
        
    except Exception as e:
        logger.error(f"Error showing preview: {e}")
        await update.message.reply_text(
            f"❌ *Error Creating Preview*\n\n"
            f"Error: `{str(e)}`",
            parse_mode="Markdown"
        )
        clean_session(user_id)

async def toggle_notify(update, context):
    query = update.callback_query
    await query.answer()
    
    s = get_session(query.from_user.id)
    if not s or s.get("step") != "preview":
        return
    
    current = s.get("notify", True)
    new_value = not current
    
    update_session(query.from_user.id, {"notify": new_value})
    
    links = s["links"]
    quality_rows = build_quality_buttons(links, buttons_per_row=3)
    
    control_rows = [
        [InlineKeyboardButton(
            "🔔 ɴᴏᴛɪꜰʏ: ᴏɴ" if new_value else "🔕 ɴᴏᴛɪꜰʏ: ᴏꜰꜰ",
            callback_data="notify"
        )],
        [
            InlineKeyboardButton("✅ ꜱᴇɴᴅ ᴘᴏꜱᴛ", callback_data="send"),
            InlineKeyboardButton("❌ ᴄᴀɴᴄᴇʟ", callback_data="cancel")
        ]
    ]
    
    all_buttons = quality_rows + control_rows
    
    try:
        await query.message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(all_buttons)
        )
    except Exception as e:
        logger.error(f"Error toggling notify: {e}")

async def send_post(update, context):
    query = update.callback_query
    await query.answer("📤 ꜱᴇɴᴅɪɴɢ ᴘᴏꜱᴛ...")
    
    user_id = query.from_user.id
    s = get_session(user_id)
    
    if not s or s.get("step") != "preview":
        await query.answer("❌ Session expired", show_alert=True)
        return
    
    channel_id = s["channel"]
    episode = s["episode"]
    links = s["links"]
    notify = s.get("notify", True)
    preview_message_id = s.get("preview_message_id")
    
    fmt = formats.find_one({"_id": channel_id})
    if not fmt:
        clean_session(user_id)
        await context.bot.send_message(
            chat_id=user_id,
            text="❌ *Format Not Found*",
            parse_mode="Markdown"
        )
        return
    
    caption_template = fmt["caption"]
    entities_template = fmt.get("caption_entities")
    
    final_caption, final_entities = adjust_caption_entities(
        caption=caption_template,
        entities=entities_template,
        placeholder="{episode}",
        replacement=episode
    )
    
    quality_rows = build_quality_buttons(links, buttons_per_row=3)
    
    entities_objects = None
    if final_entities:
        entities_objects = [
            MessageEntity(
                type=e["type"],
                offset=e["offset"],
                length=e["length"],
                url=e.get("url"),
                user=e.get("user"),
                language=e.get("language"),
                custom_emoji_id=e.get("custom_emoji_id")
            )
            for e in final_entities
        ]
    
    try:
        sent_msg = await context.bot.send_photo(
            chat_id=channel_id,
            photo=fmt["poster_file_id"],
            caption=final_caption,
            caption_entities=entities_objects,
            reply_markup=InlineKeyboardMarkup(quality_rows),
            disable_notification=not notify
        )
        
        posts.insert_one({
            "channel": channel_id,
            "message_id": sent_msg.message_id,
            "episode": episode,
            "caption": final_caption,
            "caption_entities": final_entities,
            "buttons": links,
            "poster_file_id": fmt["poster_file_id"]
        })
        
        logger.info(f"✅ ᴘᴏꜱᴛ ꜱᴇɴᴛ: {channel_id}/{sent_msg.message_id}")
        
        # ========== SAVE POST HOOK ==========
        from plugins.saved_posts import save_post_to_db
        await save_post_to_db(
            context=context,
            channel_id=channel_id,
            original_message_id=sent_msg.message_id,
            episode=episode,
            poster_file_id=fmt["poster_file_id"],
            caption=final_caption,
            caption_entities=final_entities,
            buttons=links
        )
        
        # ========== AUTO DUAL DETECTION ==========
        from plugins.dual_update import auto_detect_and_send_dual
        await auto_detect_and_send_dual(
            context=context,
            channel_id=channel_id,
            message_id=sent_msg.message_id,
            caption=final_caption,
            caption_entities=final_entities,
            poster_file_id=fmt["poster_file_id"]
        )
        
        # ========== EXISTING UPDATE CHANNEL LOGIC ==========
        try:
            from db import update_settings, update_channel_prefs
            
            update_config = update_settings.find_one({"_id": "global"})
            
            if update_config and update_config.get("update_channel_id"):
                update_channel_id = update_config.get("update_channel_id")
                
                user_pref = update_channel_prefs.find_one({"_id": user_id})
                user_enabled = user_pref.get("enabled", True) if user_pref else True
                
                if user_enabled:
                    ch = channels.find_one({"_id": channel_id})
                    channel_username = None
                    
                    if ch and ch.get("title"):
                        try:
                            chat = await context.bot.get_chat(channel_id)
                            if hasattr(chat, 'username') and chat.username:
                                channel_username = chat.username
                        except:
                            pass
                    
                    if channel_username:
                        post_link = f"https://t.me/{channel_username}/{sent_msg.message_id}"
                    else:
                        post_link = f"https://t.me/c/{channel_id.replace('-100', '')}/{sent_msg.message_id}"
                    
                    redirect_button = [[InlineKeyboardButton("⛩ Download Now ⛩", url=post_link)]]

                    redirect_caption = final_caption
                    redirect_entities = entities_objects

                    # Channel-wise custom redirect format (supports {episode} + entities)
                    channel_templates = update_config.get("update_channel_formats") or {}
                    channel_template = channel_templates.get(channel_id)

                    if isinstance(channel_template, dict) and channel_template.get("caption"):
                        adjusted_caption, adjusted_entities = adjust_caption_entities(
                            caption=channel_template["caption"],
                            entities=channel_template.get("entities"),
                            placeholder="{episode}",
                            replacement=str(episode)
                        )

                        redirect_caption = adjusted_caption
                        if adjusted_entities:
                            redirect_entities = [
                                MessageEntity(
                                    type=e["type"],
                                    offset=e["offset"],
                                    length=e["length"],
                                    url=e.get("url"),
                                    user=e.get("user"),
                                    language=e.get("language"),
                                    custom_emoji_id=e.get("custom_emoji_id")
                                )
                                for e in adjusted_entities
                            ]
                        else:
                            redirect_entities = None

                    # Legacy global custom redirect format fallback
                    redirect_template = update_config.get("update_caption_template")
                    if (
                        not (isinstance(channel_template, dict) and channel_template.get("caption"))
                        and isinstance(redirect_template, str)
                        and redirect_template.strip()
                    ):
                        redirect_caption, _ = adjust_caption_entities(
                            caption=redirect_template,
                            entities=None,
                            placeholder="{episode}",
                            replacement=str(episode)
                        )
                        redirect_entities = None
                    
                    await context.bot.send_photo(
                        chat_id=update_channel_id,
                        photo=fmt["poster_file_id"],
                        caption=redirect_caption,
                        caption_entities=redirect_entities,
                        reply_markup=InlineKeyboardMarkup(redirect_button),
                        disable_notification=True
                    )
                    
                    logger.info(f"✅ Redirect post sent to update channel: {update_channel_id}")
        
        except Exception as e:
            logger.error(f"⚠️ Update channel send failed (non-critical): {e}", exc_info=True)
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"⚠️ Update channel post failed (main post was sent):\n{str(e)[:300]}"
                )
            except Exception:
                pass
        
        # ========== FIX BUG #2: FOLLOW-UPS ==========
        # Always run follow-ups after a successful post send.
        # No longer gated on session step — fires unconditionally here.
        try:
            from plugins.followups import send_followups
            await send_followups(context, channel_id)
        except Exception as e:
            logger.error(f"⚠️ Follow-up send failed (non-critical): {e}")
        
        ch = channels.find_one({"_id": channel_id})
        channel_title = ch.get("title", channel_id) if ch else channel_id
        
        if preview_message_id:
            try:
                await context.bot.delete_message(
                    chat_id=user_id,
                    message_id=preview_message_id
                )
            except:
                pass
        
        clean_session(user_id)
        
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"✅ *Post Sent Successfully!*\n\n"
                f"*Channel:* {channel_title}\n"
                f"*Episode:* {episode}\n"
                f"*Message ID:* `{sent_msg.message_id}`\n"
                f"*Notification:* {'ON' if notify else 'OFF'}"
            ),
            parse_mode="Markdown"
        )
        
        menu_text = build_menu_text()
        menu_buttons = build_main_menu_buttons(user_id)
        
        menu_msg = await context.bot.send_message(
            chat_id=user_id,
            text=menu_text,
            reply_markup=InlineKeyboardMarkup(menu_buttons),
            parse_mode="Markdown"
        )
        
        menu_messages.update_one(
            {"_id": str(user_id)},
            {"$set": {"message_id": menu_msg.message_id}},
            upsert=True
        )
        
        logger.info(f"✅ Post sent + Menu sent to user {user_id}")
        
    except Exception as e:
        logger.error(f"Error sending post: {e}")
        
        if preview_message_id:
            try:
                await context.bot.delete_message(
                    chat_id=user_id,
                    message_id=preview_message_id
                )
            except:
                pass
        
        clean_session(user_id)
        
        await context.bot.send_message(
            chat_id=user_id,
            text=f"❌ *Failed to Send Post*\n\nError: `{str(e)}`",
            parse_mode="Markdown"
        )
        
        menu_text = build_menu_text()
        menu_buttons = build_main_menu_buttons(user_id)
        
        menu_msg = await context.bot.send_message(
            chat_id=user_id,
            text=menu_text,
            reply_markup=InlineKeyboardMarkup(menu_buttons),
            parse_mode="Markdown"
        )
        
        menu_messages.update_one(
            {"_id": str(user_id)},
            {"$set": {"message_id": menu_msg.message_id}},
            upsert=True
        )

async def cancel(update, context):
    query = update.callback_query
    await query.answer("❌ Cancelled")
    
    user_id = query.from_user.id
    s = get_session(user_id)
    
    preview_message_id = s.get("preview_message_id") if s else None
    
    if preview_message_id:
        try:
            await context.bot.delete_message(
                chat_id=user_id,
                message_id=preview_message_id
            )
        except:
            pass
    
    clean_session(user_id)
    
    logger.info(f"Post cancelled by user {user_id}")
    
    await context.bot.send_message(
        chat_id=user_id,
        text="❌ *Post Cancelled*\n\nNo post was sent to the channel.",
        parse_mode="Markdown"
    )
    
    menu_text = build_menu_text()
    menu_buttons = build_main_menu_buttons(user_id)
    
    menu_msg = await context.bot.send_message(
        chat_id=user_id,
        text=menu_text,
        reply_markup=InlineKeyboardMarkup(menu_buttons),
        parse_mode="Markdown"
    )
    
    menu_messages.update_one(
        {"_id": str(user_id)},
        {"$set": {"message_id": menu_msg.message_id}},
        upsert=True
    )
    
    logger.info(f"✅ Menu sent after cancel")

async def pagination(update, context):
    query = update.callback_query
    await query.answer()
    
    parts = query.data.split(":")
    page = int(parts[1])
    prefix = parts[2]
    
    try:
        await query.message.edit_reply_markup(
            reply_markup=channel_keyboard(page, prefix)
        )
    except Exception as e:
        logger.error(f"Pagination error: {e}")
