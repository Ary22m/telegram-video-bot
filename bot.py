import os
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import yt_dlp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("BOT_TOKEN")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 البوت يعمل! أرسل رابط YouTube")

async def download_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    
    if not url.startswith('http'):
        await update.message.reply_text("❌ رابط غير صالح")
        return
    
    msg = await update.message.reply_text("⏳ جاري التحميل...")
    
    try:
        # إعدادات بسيطة جداً
        ydl_opts = {
            'format': 'worst',  # أقل جودة للاختبار
            'outtmpl': '/tmp/video.%(ext)s',
            'quiet': True,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            
            # إذا تغير الامتداد
            if not os.path.exists(filename):
                base = filename.rsplit('.', 1)[0]
                for ext in ['.mp4', '.webm', '.mkv']:
                    if os.path.exists(base + ext):
                        filename = base + ext
                        break
            
            await msg.edit_text("📤 جاري الإرسال...")
            
            with open(filename, 'rb') as f:
                await context.bot.send_video(
                    chat_id=update.effective_chat.id,
                    video=f,
                    caption="✅ تم التحميل!"
                )
            
            os.remove(filename)
            await msg.delete()
            
    except Exception as e:
        logger.error(f"Error: {e}")
        await msg.edit_text(f"❌ خطأ: {str(e)[:200]}")

def main():
    if not TOKEN:
        print("No token!")
        return
    
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download_video))
    
    print("Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
