# Fuckoff

import os
import sys
import asyncio
import logging
from telegram.ext import Application, MessageHandler, filters
from config import BOT_TOKEN

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

os.makedirs("posters", exist_ok=True)

PID_FILE = "bot.pid"

def check_pid_lock():
    """Check if bot is already running"""
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, 'r') as f:
                old_pid = int(f.read().strip())
            
            try:
                os.kill(old_pid, 0)
                logger.error(f"❌ Bot already running with PID {old_pid}")
                logger.error(f"Kill it first: kill -9 {old_pid}")
                return False
            except OSError:
                logger.warning(f"Removing stale PID file (process {old_pid} not found)")
                os.remove(PID_FILE)
        except Exception as e:
            logger.warning(f"Error reading PID file: {e}")
            os.remove(PID_FILE)
    
    with open(PID_FILE, 'w') as f:
        f.write(str(os.getpid()))
    
    return True

def cleanup_pid():
    """Remove PID file on exit"""
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
            logger.info("✅ PID file removed")
    except Exception as e:
        logger.error(f"Error removing PID file: {e}")

def main():
    """Initialize and run bot"""
    
    # FIX: Python 3.14 no longer creates an event loop automatically,
    # PTB's run_polling() then fails with "There is no current event loop".
    asyncio.set_event_loop(asyncio.new_event_loop())

    # Render web service needs an open port -> tiny health server in background
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _Health(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()
        def log_message(self, *a):
            pass

    try:
        port = int(os.environ.get("PORT", "10000"))
        srv = HTTPServer(("0.0.0.0", port), _Health)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        logger.info(f"🌐 Health server on port {port}")
    except Exception as e:
        logger.warning(f"Health server failed: {e}")

    if not check_pid_lock():
        sys.exit(1)
    
    logger.info("🤖 Telegram Controller Post Bot v3.0 - SMART COLLECTOR")
    
    try:
        from db import db, get_stats
        logger.info(f"📊 Database: {get_stats()}")
    except Exception as e:
        logger.error(f"❌ DB failed: {e}")
        cleanup_pid()
        return
    
    app = Application.builder().token(BOT_TOKEN).build()

    # FIX: make query.answer() safe when a handler chain answers twice
    from plugins.helpers import patch_query_answer
    patch_query_answer()

    async def on_error(update, context):
        logger.error("Unhandled error", exc_info=context.error)
        try:
            from config import OWNER_ID
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=f"⚠️ Bot error: {type(context.error).__name__}: {str(context.error)[:300]}"
            )
        except Exception:
            pass

    app.add_error_handler(on_error)
    
    try:
        from plugins import (
            start,
            settings,
            admins,
            channels,
            formats,
            create_post,
            edit_post,
            followups,
            update_channel,
            saved_posts,
            bulk_replace,
            dual_update,
            smart_collector
        )
        
        start.register(app)
        settings.register(app)
        admins.register(app)
        channels.register(app)
        formats.register(app)
        create_post.register(app)
        edit_post.register(app)
        followups.register(app)
        update_channel.register(app)
        saved_posts.register(app)
        bulk_replace.register(app)
        dual_update.register(app)
        dual_update.register_proceed(app)
        dual_update.register_buttons(app)
        smart_collector.register(app)
        
        logger.info("✅ Plugins registered (smart_collector added)")
    except Exception as e:
        logger.error(f"❌ Plugin registration failed: {e}")
        cleanup_pid()
        return
    
    from plugins.helpers import route_message, get_session
    from plugins.followups import receive_message, receive_sticker
    
    async def unified_text_router(update, context):
        s = get_session(update.effective_user.id)
        
        if s and s.get("step") == "followup_message":
            await receive_message(update, context)
        else:
            await route_message(update, context)
    
    async def unified_photo_router(update, context):
        s = get_session(update.effective_user.id)
        
        if not s:
            return
        
        step = s.get("step")
        
        if step == "followup_message":
            await receive_message(update, context)
        
        elif step == "format":
            if update.message.caption:
                from plugins.formats import receive_format
                await receive_format(update, context)
            else:
                await update.message.reply_text(
                    "❌ *Format requires caption*\n\n"
                    "Send poster WITH caption containing `{episode}`",
                    parse_mode="Markdown"
                )
    
    async def unified_sticker_router(update, context):
        s = get_session(update.effective_user.id)
        
        if s and s.get("step") == "followup_sticker":
            await receive_sticker(update, context)
    
    async def unified_video_router(update, context):
        s = get_session(update.effective_user.id)
        if not s:
            return
        
        step = s.get("step")
        
        if step == "collector":
            from plugins.smart_collector import handle_file_upload
            await handle_file_upload(update, context)
        elif step == "followup_message":
            await receive_message(update, context)
    
    async def unified_document_router(update, context):
        s = get_session(update.effective_user.id)
        if not s:
            return
        
        step = s.get("step")
        
        if step == "collector":
            from plugins.smart_collector import handle_file_upload
            await handle_file_upload(update, context)
        elif step == "followup_message":
            await receive_message(update, context)
    
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        unified_text_router
    ), group=0)
    
    app.add_handler(MessageHandler(
        filters.PHOTO & ~filters.COMMAND,
        unified_photo_router
    ), group=0)
    
    app.add_handler(MessageHandler(
        filters.Sticker.ALL & ~filters.COMMAND,
        unified_sticker_router
    ), group=0)
    
    app.add_handler(MessageHandler(
        filters.VIDEO & ~filters.COMMAND,
        unified_video_router
    ), group=0)
    
    app.add_handler(MessageHandler(
        filters.Document.ALL & ~filters.COMMAND,
        unified_document_router
    ), group=0)
    
    logger.info("✅ Routers registered")
    logger.info("🚀 Bot started with FIXED dual update feature!")
    
    try:
        app.run_polling(drop_pending_updates=True)
    except KeyboardInterrupt:
        logger.info("🛑 Bot stopped by user")
    except Exception as e:
        logger.error(f"❌ Bot crashed: {e}")
    finally:
        cleanup_pid()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("🛑 Bot interrupted")
        cleanup_pid()
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}")
        cleanup_pid()
        sys.exit(1)
    
