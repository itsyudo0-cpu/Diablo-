"""
Smart Collector Plugin - Auto-detect File/Link Mode
Replaces old episode → links flow with intelligent collector.
Session state stored in context.user_data (NOT MongoDB).

IMPROVEMENTS:
  - Single live-updating collector panel (no message spam)
  - Visual quality grid with 🟢/⚪ indicators
  - Auto-preview when 480p + 720p + 1080p + HDRip are all collected
  - Immediate UI update before file processing (fast feeling)
  - Collector message deleted cleanly when preview appears
"""

import asyncio
import re
import base64
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
from telegram.ext import CallbackQueryHandler

from plugins.helpers import (
    is_admin,
    get_session,
    update_session,
    clean_session,
    build_quality_buttons,
    adjust_caption_entities,
    build_main_menu_buttons,
    build_menu_text,
    escape_md,
    channel_keyboard,
    detect_channel_from_filename,
    channel_ready,
)
from db import channels, formats, posts, menu_messages
from config import FILESTORE_CHANNEL_ID, FILESTORE_BOT_USERNAME

logger = logging.getLogger(__name__)

# ==================== CONSTANTS ====================

KNOWN_QUALITIES = ["480p", "720p", "1080p", "2160p", "4K", "HDRip", "144FPS"]

# All four must be present to trigger auto-preview
AUTO_PREVIEW_REQUIRED = {"480p", "720p", "1080p", "HDRip"}

EPISODE_PATTERNS = [
    re.compile(r'S\d+E(\d+)', re.IGNORECASE),
    re.compile(r'E(\d+)', re.IGNORECASE),
    re.compile(r'Ep(\d+)', re.IGNORECASE),
    re.compile(r'[\s._-](\d+)[\s._-]', re.IGNORECASE),
]

QUALITY_PATTERN = re.compile(
    r'(480p|720p|1080p|2160p|4[Kk]|HDRip|144FPS)',
    re.IGNORECASE
)

TELEGRAM_LINK_PATTERN = re.compile(
    r'https?://t\.me/\S+',
    re.IGNORECASE
)

# Display order for the quality grid
QUALITY_GRID_ORDER = ["480p", "720p", "1080p", "HDRip", "4K", "2160p", "144FPS"]


# ==================== REGISTRATION ====================

def register(app):
    """Register smart collector callback handlers"""
    app.add_handler(CallbackQueryHandler(collector_done, pattern="^collector:done$"))
    app.add_handler(CallbackQueryHandler(collector_cancel, pattern="^collector:cancel$"))


# ==================== FILESTORE LINK GENERATION ====================

async def encode(string):
    """
    Encode string exactly like FileStore bot.
    base64 urlsafe encoding WITHOUT '=' padding.
    """
    string_bytes = string.encode("ascii")
    base64_bytes = base64.urlsafe_b64encode(string_bytes)
    base64_string = (base64_bytes.decode("ascii")).strip("=")
    return base64_string


async def build_filestore_link(message_id):
    """
    Generate a FileStore share link using the same math as the FileStore bot.

    Formula:
        converted_id = message_id * abs(FILESTORE_CHANNEL_ID)
        string       = f"get-{converted_id}"
        payload      = base64_urlsafe(string).strip("=")
        link         = https://t.me/{BOT_USERNAME}?start={payload}
    """
    converted_id = message_id * abs(FILESTORE_CHANNEL_ID)
    string = f"get-{converted_id}"
    payload = await encode(string)
    return f"https://t.me/{FILESTORE_BOT_USERNAME}?start={payload}"


# ==================== COLLECTOR PANEL UI ====================

def build_collector_text(collector):
    """
    Build the live collector panel text.

    Shows:
      - Episode number (or ❓ if not yet detected)
      - Quality grid with 🟢 (collected) / ⚪ (missing) for each quality
      - Count of collected vs total known qualities

    Args:
        collector: dict from context.user_data["collector"]

    Returns:
        Formatted string ready for Markdown send/edit.
    """
    collected = collector.get("qualities", {})
    episode = collector.get("episode")

    ep_display = f"`{episode}`" if episode else "❓ *Not detected yet*"

    if collector.get("channel"):
        tag = " _(auto)_" if collector.get("auto_channel") else ""
        channel_line = f"📺 *Channel:* {escape_md(collector.get('channel_title') or collector['channel'])}{tag}\n"
    else:
        channel_line = "🔍 *Channel:* auto-detect from file name\n"

    # Build quality grid line
    grid_parts = []
    for q in QUALITY_GRID_ORDER:
        if q in collected:
            grid_parts.append(f"🟢 {q}")
        else:
            grid_parts.append(f"⚪ {q}")

    # Also show any unknown qualities that were collected
    for q in collected:
        if q not in QUALITY_GRID_ORDER:
            grid_parts.append(f"🟢 {q}")

    grid_line = "  ".join(grid_parts)

    count = len(collected)
    mode = collector.get("mode", "file")
    mode_label = "📁 File Mode" if mode == "file" else "🔗 Link Mode"

    # Check if auto-preview will trigger next
    auto_note = ""
    missing = AUTO_PREVIEW_REQUIRED - set(collected.keys())
    if missing and count > 0:
        missing_str = ", ".join(f"`{q}`" for q in sorted(missing))
        auto_note = f"\n⚡ *Auto-preview triggers when:* {missing_str} collected"
    elif AUTO_PREVIEW_REQUIRED.issubset(set(collected.keys())):
        auto_note = "\n✅ *All required qualities collected — generating preview...*"

    text = (
        f"📦 *Smart Collector* — {mode_label}\n"
        f"{'─' * 30}\n"
        f"{channel_line}"
        f"🎬 *Episode:* {ep_display}\n\n"
        f"{grid_line}\n\n"
        f"*Collected:* {count} qualit{'y' if count == 1 else 'ies'}"
        f"{auto_note}\n\n"
        f"_Upload files or press ✅ Done when finished._"
    )

    return text


def build_collector_buttons():
    """
    Build the Done / Cancel inline keyboard for the collector panel.
    """
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ ᴅᴏɴᴇ", callback_data="collector:done"),
            InlineKeyboardButton("❌ ᴄᴀɴᴄᴇʟ", callback_data="collector:cancel"),
        ]
    ])


async def update_collector_panel(context, chat_id, collector):
    """
    Edit the live collector panel message in-place.
    Silently ignores edit failures (e.g. message unchanged).

    Args:
        context: PTB context
        chat_id: User's chat_id
        collector: Current collector state dict
    """
    msg_id = context.user_data.get("collector_msg_id")
    if not msg_id:
        return

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=build_collector_text(collector),
            reply_markup=build_collector_buttons(),
            parse_mode="Markdown"
        )
    except Exception as e:
        # Ignore "message is not modified" errors — they're harmless
        if "message is not modified" not in str(e).lower():
            logger.warning(f"Collector panel update failed: {e}")


async def delete_collector_panel(context, chat_id):
    """
    Delete the live collector panel message after preview is shown.
    """
    msg_id = context.user_data.pop("collector_msg_id", None)
    if not msg_id:
        return
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except Exception as e:
        logger.warning(f"Could not delete collector panel: {e}")


async def ensure_collector_panel(context, chat_id, collector):
    """Create the live panel if it doesn't exist yet (auto-detect mode)."""
    if context.user_data.get("collector_msg_id"):
        await update_collector_panel(context, chat_id, collector)
        return
    panel_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=build_collector_text(collector),
        reply_markup=build_collector_buttons(),
        parse_mode="Markdown"
    )
    context.user_data["collector_msg_id"] = panel_msg.message_id


# ==================== COLLECTOR STATE HELPERS ====================

def get_collector(context):
    """Get collector state from context.user_data"""
    return context.user_data.get("collector")


def init_collector(context, channel_id=None):
    """
    Initialize fresh collector state.
    channel_id=None  -> AUTO mode: channel is detected from the first file name.
    """
    title = None
    if channel_id:
        ch = channels.find_one({"_id": channel_id})
        title = ch.get("title", channel_id) if ch else channel_id
    context.user_data["collector"] = {
        "mode": None,
        "episode": None,
        "qualities": {},
        "channel": channel_id,
        "channel_title": title,
        "auto_channel": False,
        "preview_triggered": False,   # guard: prevent duplicate auto-preview
    }
    return context.user_data["collector"]


def clear_collector(context):
    """Clear all collector state"""
    context.user_data.pop("collector", None)
    context.user_data.pop("collector_msg_id", None)


# ==================== ENTRY POINT ====================

async def enter_collector(update, context):
    """
    Called after channel selection in create_post.
    Sets session step to 'collector' and sends the ONE persistent panel message.
    """
    query = update.callback_query
    user_id = query.from_user.id
    channel_id = get_session(user_id).get("channel")

    # Init collector in memory
    collector = init_collector(context, channel_id)

    # Update MongoDB session step
    update_session(user_id, {"step": "collector"})

    # Send the ONE persistent collector panel (channel line is part of the panel text)
    panel_msg = await query.message.reply_text(
        build_collector_text(collector),
        reply_markup=build_collector_buttons(),
        parse_mode="Markdown"
    )

    # Store panel message_id for future edits
    context.user_data["collector_msg_id"] = panel_msg.message_id

    logger.info(f"Collector entered for channel {channel_id} by user {user_id}, panel_msg={panel_msg.message_id}")


# ==================== SMART INPUT DISPATCHER ====================

async def smart_collect_input(update, context):
    """
    Called from message router when step == 'collector'.
    Text messages only. Detects link mode.
    """
    user_id = update.effective_user.id
    collector = get_collector(context)

    if not collector:
        return

    text = update.message.text or ""

    # Delete the user's text message to keep chat clean
    try:
        await update.message.delete()
    except Exception:
        pass

    # Check if text contains Telegram links
    if TELEGRAM_LINK_PATTERN.search(text):
        # Mode lock check
        if collector["mode"] == "file":
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "❌ *Mode Locked: FILE MODE*\n\n"
                    "You started uploading files.\n"
                    "Cannot switch to link mode.\n\n"
                    "Continue uploading files or press ✅ Done."
                ),
                parse_mode="Markdown"
            )
            return

        if not collector.get("channel"):
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "⚠️ *Channel not selected*\n\n"
                    "Link mode cannot auto-detect the channel.\n"
                    "Please send the episode file instead."
                ),
                parse_mode="Markdown"
            )
            return

        collector["mode"] = "link"
        await handle_link_paste(update, context, text=text)
    else:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "❌ *Invalid Input*\n\n"
                "Send episode files (video/document)\n"
                "OR paste quality links containing `https://t.me/...`"
            ),
            parse_mode="Markdown"
        )


# ==================== FILE MODE HANDLER ====================

async def handle_file_upload(update, context):
    """
    Handle file/video/document upload in collector mode.

    IMPROVEMENT: Two-phase processing for fast UI feel:
      Phase 1 (immediate): Validate file, update collector panel right away.
      Phase 2 (background): copy_message + link generation via asyncio.create_task().

    This means the admin sees instant feedback before the file is even stored.
    """
    user_id = update.effective_user.id
    s = get_session(user_id)
    if not s or s.get("step") != "collector":
        return  # edit_post ya dual_update ka message hai
    collector = get_collector(context)

    if not collector:
        # bot restarted / memory lost but Mongo session still alive -> rebuild
        collector = init_collector(context, s.get("channel"))

    # Mode lock check
    if collector["mode"] == "link":
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "❌ *Mode Locked: LINK MODE*\n\n"
                "You started pasting links.\n"
                "Cannot switch to file mode.\n\n"
                "Paste more links or wait for preview."
            ),
            parse_mode="Markdown"
        )
        return

    # ---- Get file info ----
    if update.message.video:
        file_obj = update.message.video
        file_name = file_obj.file_name or ""
    elif update.message.document:
        file_obj = update.message.document
        file_name = file_obj.file_name or ""
    else:
        await context.bot.send_message(chat_id=user_id, text="❌ Unsupported file type.")
        return

    if not file_name:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "⚠️ *File has no filename.*\n"
                "Please rename the file before uploading.\n"
                "Example: `Show.S01E05.720p.mkv`"
            ),
            parse_mode="Markdown"
        )
        return

    logger.info(f"File received: {file_name}")

    # ---- Detect Quality ----
    quality_match = QUALITY_PATTERN.search(file_name)
    if not quality_match:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"⚠️ *Quality not detected in:* `{file_name}`\n\n"
                f"Supported qualities: {', '.join(KNOWN_QUALITIES)}\n"
                f"File skipped. Rename and re-upload."
            ),
            parse_mode="Markdown"
        )
        return

    quality = quality_match.group(1)
    quality_upper = quality.upper()
    if quality_upper == "4K":
        quality = "4K"
    elif quality_upper == "HDRIP":
        quality = "HDRip"
    elif quality_upper == "144FPS":
        quality = "144FPS"
    else:
        quality = quality.lower()

    # ---- Duplicate Quality Check ----
    if quality in collector["qualities"]:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"❌ *Duplicate Quality:* `{quality}`\n\n"
                f"This quality was already uploaded.\n"
                f"Skipping this file."
            ),
            parse_mode="Markdown"
        )
        return

    # ---- AUTO-DETECT CHANNEL (from file name == channel title) ----
    if not collector.get("channel"):
        status, ch_doc, candidates = detect_channel_from_filename(file_name)

        if status != "found":
            if status == "ambiguous":
                names = "\n".join(f"• {escape_md(c.get('title', c['_id']))}" for c in candidates[:8])
                reason = f"Multiple channels match this file name:\n{names}"
            else:
                reason = "No channel title matches this file name."
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"⚠️ *Channel not detected*\n\n"
                    f"File: `{file_name}`\n"
                    f"{reason}\n\n"
                    f"Rename the file so it contains the channel title "
                    f"and upload again."
                ),
                parse_mode="Markdown"
            )
            return

        if not channel_ready(ch_doc["_id"]):
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"❌ *Channel setup incomplete*\n\n"
                    f"Detected channel: *{escape_md(ch_doc.get('title', ch_doc['_id']))}*\n"
                    f"Set both *Format* and *Update Format* first (Settings → Set Channel)."
                ),
                parse_mode="Markdown"
            )
            return

        collector["channel"] = ch_doc["_id"]
        collector["channel_title"] = ch_doc.get("title", ch_doc["_id"])
        collector["auto_channel"] = True
        update_session(user_id, {"channel": ch_doc["_id"]})
        logger.info(f"Channel auto-detected: {ch_doc['_id']} ({collector['channel_title']}) from '{file_name}'")

        # collapse the channel-list message (if any) into a short note
        list_msg = context.user_data.pop("channel_list_msg", None)
        if list_msg:
            try:
                await context.bot.edit_message_text(
                    chat_id=list_msg[0],
                    message_id=list_msg[1],
                    text=f"✅ *Channel auto-detected:* {escape_md(collector['channel_title'])}",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

    elif collector.get("auto_channel"):
        # later files must belong to the same channel
        status, ch_doc, _ = detect_channel_from_filename(file_name)
        if status == "found" and ch_doc["_id"] != collector["channel"]:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"❌ *Channel Mismatch!*\n\n"
                    f"Current: `{collector.get('channel_title')}`\n"
                    f"This file: `{ch_doc.get('title', ch_doc['_id'])}`\n\n"
                    f"All files must be for the same channel.\n"
                    f"File rejected."
                ),
                parse_mode="Markdown"
            )
            return

    # ---- Detect Episode ----
    episode = _extract_episode(file_name)

    if episode is not None:
        if collector["episode"] is not None and collector["episode"] != episode:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"❌ *Episode Mismatch!*\n\n"
                    f"Previous files: Episode `{collector['episode']}`\n"
                    f"This file: Episode `{episode}`\n\n"
                    f"All files must be for the same episode.\n"
                    f"File rejected."
                ),
                parse_mode="Markdown"
            )
            return
        collector["episode"] = episode

    collector["mode"] = "file"

    # ---- PHASE 1: Mark quality as "pending" with a placeholder link ----
    # Store immediately so UI updates right away.
    # The placeholder will be replaced by the real link in Phase 2.
    collector["qualities"][quality] = "__pending__"

    # ---- IMMEDIATE UI UPDATE (Phase 1) ----
    await ensure_collector_panel(context, user_id, collector)

    # ---- PHASE 2: Background — copy file and generate real link ----
    # Capture everything needed before handing off to the task
    from_chat_id = update.message.chat_id
    message_id = update.message.message_id

    async def _store_and_link():
        """Background task: copy to FileStore, generate link, update panel."""
        try:
            store_msg = await context.bot.copy_message(
                chat_id=FILESTORE_CHANNEL_ID,
                from_chat_id=from_chat_id,
                message_id=message_id
            )
            logger.info(f"File copied to FileStore: {file_name} → msg_id={store_msg.message_id}")

            real_link = await build_filestore_link(store_msg.message_id)
            logger.info(f"Share link built for {file_name} [{quality}]: {real_link}")

        except Exception as e:
            logger.error(f"Failed to copy/link file {file_name}: {e}")
            # Remove the pending entry — upload failed
            if collector.get("qualities", {}).get(quality) == "__pending__":
                collector["qualities"].pop(quality, None)
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"❌ *Failed to store `{quality}` file*\n\n"
                    f"Error: `{str(e)}`\n\n"
                    f"Make sure bot is admin in FileStore channel.\n"
                    f"Please re-upload this file."
                ),
                parse_mode="Markdown"
            )
            await update_collector_panel(context, user_id, collector)
            return

        # Replace placeholder with real link
        collector["qualities"][quality] = real_link

        # Update panel again now that real link is ready
        await update_collector_panel(context, user_id, collector)

        # ---- AUTO-PREVIEW TRIGGER ----
        # Fire only once, only when all required qualities are collected,
        # and only when no quality is still pending.
        has_pending = any(v == "__pending__" for v in collector["qualities"].values())
        all_required = AUTO_PREVIEW_REQUIRED.issubset(set(collector["qualities"].keys()))

        if all_required and not has_pending and not collector.get("preview_triggered"):
            collector["preview_triggered"] = True  # guard against duplicate trigger
            logger.info(f"Auto-preview triggered: collected={list(collector['qualities'].keys())}")

            if collector.get("episode") is not None:
                await delete_collector_panel(context, user_id)
                await _trigger_auto_preview(context, user_id)
            else:
                # Episode still unknown — ask for it
                update_session(user_id, {"step": "collector_episode_fallback"})
                await context.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "🎉 *All required qualities collected!*\n\n"
                        "🔢 Episode number not detected from filenames.\n\n"
                        "Please send the episode number to generate preview:"
                    ),
                    parse_mode="Markdown"
                )

    # Kick off background task — handler returns immediately (fast feeling)
    asyncio.create_task(_store_and_link())


# ==================== LINK MODE HANDLER ====================

async def handle_link_paste(update, context, text=None):
    """
    Handle pasted quality links in collector mode.
    Parses quality→link pairs.
    Always asks for episode number (cannot auto-detect from pre-built links).
    """
    user_id = update.effective_user.id
    collector = get_collector(context)

    if not collector:
        return

    if text is None:
        text = update.message.text or ""

    text_normalized = text.replace("\n", " && ")
    text_normalized = text_normalized.replace("|", " && ")
    parts = text_normalized.split("&&")

    parsed_count = 0
    skipped = []

    for part in parts:
        part = part.strip()
        if " - " not in part:
            continue

        quality, url = part.split(" - ", 1)
        quality = quality.strip()
        url = url.strip()

        if not quality or not url:
            continue

        if not TELEGRAM_LINK_PATTERN.match(url):
            continue

        quality = _normalize_quality(quality)

        if quality in collector["qualities"]:
            skipped.append(quality)
            continue

        collector["qualities"][quality] = url
        parsed_count += 1

    if parsed_count == 0:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "❌ *No valid links parsed.*\n\n"
                "Use format:\n"
                "`480p - https://t.me/... && 720p - https://t.me/...`\n\n"
                "Or multi-line:\n"
                "```\n"
                "480p - https://t.me/...\n"
                "720p - https://t.me/...\n"
                "```"
            ),
            parse_mode="Markdown"
        )
        return

    logger.info(f"Link mode: parsed {parsed_count} qualities → {list(collector['qualities'].keys())}")

    if skipped:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"⚠️ Skipped duplicate qualities: {', '.join(f'`{q}`' for q in skipped)}",
            parse_mode="Markdown"
        )

    # Update the live panel
    await update_collector_panel(context, user_id, collector)

    # Always ask for episode in link mode
    await ask_episode_fallback_msg(context, user_id, collector)


# ==================== COLLECTOR DONE ====================

async def collector_done(update, context):
    """Handle Done button press — fallback when auto-preview hasn't fired."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    collector = get_collector(context)

    if not collector:
        await query.answer("❌ No active collector session.", show_alert=True)
        return

    # Check if any quality is still being processed
    pending = [q for q, v in collector.get("qualities", {}).items() if v == "__pending__"]
    if pending:
        await query.answer(
            f"⏳ Still processing: {', '.join(pending)} — please wait a moment.",
            show_alert=True
        )
        return

    if not collector["qualities"]:
        await query.answer("❌ No qualities collected yet!", show_alert=True)
        return

    if collector.get("preview_triggered"):
        await query.answer("✅ Preview already being generated.", show_alert=True)
        return

    collector["preview_triggered"] = True  # prevent double trigger

    if collector["episode"] is None:
        update_session(user_id, {"step": "collector_episode_fallback"})
        await query.message.reply_text(
            "🔢 *Episode Not Detected*\n\n"
            "Could not detect episode number from filenames.\n\n"
            "Please send the episode number:",
            parse_mode="Markdown"
        )
        return

    # Delete panel and show preview
    await delete_collector_panel(context, user_id)
    await _show_preview_from_callback(query, context)


# ==================== COLLECTOR CANCEL ====================

async def collector_cancel(update, context):
    """Cancel collector and clear all state."""
    query = update.callback_query
    await query.answer("❌ Cancelled")

    user_id = query.from_user.id

    # Delete the panel message
    await delete_collector_panel(context, user_id)

    clear_collector(context)
    clean_session(user_id)

    logger.info(f"Collector cancelled by user {user_id}")

    await context.bot.send_message(
        chat_id=user_id,
        text="❌ *Collector Cancelled*\n\nNo post was created.",
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


# ==================== EPISODE FALLBACK ====================

async def ask_episode_fallback_msg(context, user_id, collector):
    """Send episode-request message (used from both file and link mode)."""
    update_session(user_id, {"step": "collector_episode_fallback"})
    collected = list(collector["qualities"].keys())

    await context.bot.send_message(
        chat_id=user_id,
        text=(
            f"📦 *Qualities Collected:* {', '.join(f'`{q}`' for q in collected)}\n\n"
            f"🔢 *Episode number not detected.*\n\n"
            f"Please send the episode number:"
        ),
        parse_mode="Markdown"
    )


async def ask_episode_fallback(update, context):
    """Legacy entry point — delegates to ask_episode_fallback_msg."""
    user_id = update.effective_user.id
    collector = get_collector(context)
    if collector:
        await ask_episode_fallback_msg(context, user_id, collector)


async def receive_episode_fallback(update, context):
    """Receive manually entered episode number and proceed to preview."""
    user_id = update.effective_user.id
    collector = get_collector(context)

    if not collector:
        await update.message.reply_text("❌ No active collector session.")
        return

    text = update.message.text.strip()

    if not text.isdigit():
        await update.message.reply_text(
            "❌ *Invalid episode number.*\n\n"
            "Please send a valid number (e.g., `5`, `12`):",
            parse_mode="Markdown"
        )
        return

    # Zero-pad to 2 digits
    episode = str(text).zfill(2)
    collector["episode"] = episode
    logger.info(f"Episode set manually: {episode}")

    update_session(user_id, {"step": "collector"})

    # Update panel to show detected episode
    await update_collector_panel(context, user_id, collector)

    # Now show preview
    await delete_collector_panel(context, user_id)
    await show_collector_preview(update, context)


# ==================== AUTO-PREVIEW TRIGGER (internal) ====================

async def _trigger_auto_preview(context, user_id):
    """
    Internal helper: build a fake update-like object to call
    show_collector_preview after auto-trigger in a background task.
    Uses context.bot.send_message directly since we have no update object.
    """
    collector = context.user_data.get("collector")
    if not collector:
        return

    channel_id = collector["channel"]
    episode = collector["episode"]
    qualities = collector["qualities"]

    if not qualities:
        return

    fmt = formats.find_one({"_id": channel_id})
    if not fmt:
        await context.bot.send_message(
            chat_id=user_id,
            text="❌ Format not found for this channel.",
            parse_mode="Markdown"
        )
        clear_collector(context)
        clean_session(user_id)
        return

    links = _build_ordered_quality_links(qualities)

    update_session(user_id, {
        "step": "preview",
        "episode": episode,
        "links": links,
        "notify": True,
    })

    caption_template = fmt["caption"]
    entities_template = fmt.get("caption_entities")

    from plugins.helpers import adjust_caption_entities, build_quality_buttons
    final_caption, final_entities = adjust_caption_entities(
        caption=caption_template,
        entities=entities_template,
        placeholder="{episode}",
        replacement=str(episode)
    )

    quality_rows = build_quality_buttons(links, buttons_per_row=3)

    control_rows = [
        [InlineKeyboardButton("🔔 ɴᴏᴛɪꜰʏ: ᴏɴ", callback_data="notify")],
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

    await context.bot.send_message(
        chat_id=user_id,
        text=(
            "🎉 *Auto-Preview Generated!*\n\n"
            "ᴛʜɪꜱ ɪꜱ ᴇxᴀᴄᴛʟʏ ʜᴏᴡ ʏᴏᴜʀ ᴘᴏꜱᴛ ᴡɪʟʟ ᴀᴘᴘᴇᴀʀ ɪɴ ᴛʜᴇ ᴄʜᴀɴɴᴇʟ.\n"
            "ʀᴇᴠɪᴇᴡ ᴄᴀʀᴇꜰᴜʟʟʏ ʙᴇꜰᴏʀᴇ ꜱᴇɴᴅɪɴɢ."
        ),
        parse_mode="Markdown"
    )

    try:
        preview_msg = await context.bot.send_photo(
            chat_id=user_id,
            photo=fmt["poster_file_id"],
            caption=final_caption,
            caption_entities=entities_objects,
            reply_markup=InlineKeyboardMarkup(all_buttons)
        )

        update_session(user_id, {"preview_message_id": preview_msg.message_id})
        clear_collector(context)

        logger.info(f"Auto-preview shown: channel={channel_id}, episode={episode}, qualities={list(qualities.keys())}")

    except Exception as e:
        logger.error(f"Error showing auto-preview: {e}")
        await context.bot.send_message(
            chat_id=user_id,
            text=f"❌ *Error Creating Preview*\n\nError: `{str(e)}`",
            parse_mode="Markdown"
        )
        clear_collector(context)
        clean_session(user_id)


# ==================== PREVIEW ====================

async def show_collector_preview(update, context):
    """Generate and show post preview from collected qualities (message path)."""
    user_id = update.effective_user.id
    collector = get_collector(context)

    if not collector:
        return

    channel_id = collector["channel"]
    episode = collector["episode"]
    qualities = collector["qualities"]

    if not qualities:
        await update.message.reply_text("❌ No qualities to preview.")
        return

    fmt = formats.find_one({"_id": channel_id})
    if not fmt:
        await update.message.reply_text("❌ Format not found for this channel.")
        clear_collector(context)
        clean_session(user_id)
        return

    links = _build_ordered_quality_links(qualities)

    update_session(user_id, {
        "step": "preview",
        "episode": episode,
        "links": links,
        "notify": True,
    })

    caption_template = fmt["caption"]
    entities_template = fmt.get("caption_entities")

    final_caption, final_entities = adjust_caption_entities(
        caption=caption_template,
        entities=entities_template,
        placeholder="{episode}",
        replacement=str(episode)
    )

    quality_rows = build_quality_buttons(links, buttons_per_row=3)

    control_rows = [
        [InlineKeyboardButton("🔔 ɴᴏᴛɪꜰʏ: ᴏɴ", callback_data="notify")],
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
        clear_collector(context)

        logger.info(f"Preview shown: channel={channel_id}, episode={episode}, qualities={list(qualities.keys())}")

    except Exception as e:
        logger.error(f"Error showing collector preview: {e}")
        await update.message.reply_text(
            f"❌ *Error Creating Preview*\n\nError: `{str(e)}`",
            parse_mode="Markdown"
        )
        clear_collector(context)
        clean_session(user_id)


async def _show_preview_from_callback(query, context):
    """Show preview triggered from a callback query (Done button)."""
    user_id = query.from_user.id
    collector = get_collector(context)

    if not collector:
        return

    channel_id = collector["channel"]
    episode = collector["episode"]
    qualities = collector["qualities"]

    if not qualities:
        await query.message.reply_text("❌ No qualities to preview.")
        return

    fmt = formats.find_one({"_id": channel_id})
    if not fmt:
        await query.message.reply_text("❌ Format not found for this channel.")
        clear_collector(context)
        clean_session(user_id)
        return

    links = _build_ordered_quality_links(qualities)

    update_session(user_id, {
        "step": "preview",
        "episode": episode,
        "links": links,
        "notify": True,
    })

    caption_template = fmt["caption"]
    entities_template = fmt.get("caption_entities")

    final_caption, final_entities = adjust_caption_entities(
        caption=caption_template,
        entities=entities_template,
        placeholder="{episode}",
        replacement=str(episode)
    )

    quality_rows = build_quality_buttons(links, buttons_per_row=3)

    control_rows = [
        [InlineKeyboardButton("🔔 ɴᴏᴛɪꜰʏ: ᴏɴ", callback_data="notify")],
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

    await context.bot.send_message(
        chat_id=user_id,
        text=(
            "📺 *PREVIEW*\n\n"
            "ᴛʜɪꜱ ɪꜱ ᴇxᴀᴄᴛʟʏ ʜᴏᴡ ʏᴏᴜʀ ᴘᴏꜱᴛ ᴡɪʟʟ ᴀᴘᴘᴇᴀʀ ɪɴ ᴛʜᴇ ᴄʜᴀɴɴᴇʟ.\n"
            "ʀᴇᴠɪᴇᴡ ᴄᴀʀᴇꜰᴜʟʟʏ ʙᴇꜰᴏʀᴇ ꜱᴇɴᴅɪɴɢ."
        ),
        parse_mode="Markdown"
    )

    try:
        preview_msg = await context.bot.send_photo(
            chat_id=user_id,
            photo=fmt["poster_file_id"],
            caption=final_caption,
            caption_entities=entities_objects,
            reply_markup=InlineKeyboardMarkup(all_buttons)
        )

        update_session(user_id, {"preview_message_id": preview_msg.message_id})
        clear_collector(context)

        logger.info(f"Preview shown (callback): channel={channel_id}, episode={episode}")

    except Exception as e:
        logger.error(f"Error showing collector preview (callback): {e}")
        await context.bot.send_message(
            chat_id=user_id,
            text=f"❌ *Error Creating Preview*\n\nError: `{str(e)}`",
            parse_mode="Markdown"
        )
        clear_collector(context)
        clean_session(user_id)


# ==================== INTERNAL HELPERS ====================

def _extract_episode(filename):
    """
    Extract episode number from filename using multiple regex patterns.
    Returns episode as zero-padded 2-digit string (e.g. "05", "12").
    Never returns an int. Never strips leading zeros.
    """
    for pattern in EPISODE_PATTERNS:
        match = pattern.search(filename)
        if match:
            raw = match.group(1)
            return str(raw).zfill(2)
    return None


def _normalize_quality(quality):
    """Normalize quality string to standard format."""
    q = quality.strip()
    q_upper = q.upper()

    if q_upper in ("4K", "2160P"):
        return "4K"
    elif q_upper == "HDRIP":
        return "HDRip"
    elif q_upper == "144FPS":
        return "144FPS"
    elif q_upper in ("480P", "720P", "1080P", "2160P"):
        return q.lower()

    return q


QUALITY_ORDER = ["480p", "720p", "1080p", "2160p", "4K", "HDRip", "144FPS"]


def _build_ordered_quality_links(qualities_dict):
    """
    Build ordered list of [quality, url] pairs.
    Skips any entries still marked as '__pending__'.
    """
    ordered = []
    remaining = dict(qualities_dict)

    for q in QUALITY_ORDER:
        if q in remaining and remaining[q] != "__pending__":
            ordered.append([q, remaining.pop(q)])
        elif q in remaining:
            remaining.pop(q)  # drop pending entries

    for q, url in remaining.items():
        if url != "__pending__":
            ordered.append([q, url])

    return ordered
