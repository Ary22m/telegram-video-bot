import os
import logging
import yt_dlp
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# قراءة التوكن من البيئة
TOKEN = os.environ.get("BOT_TOKEN")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 بوت التحميل يعمل!\n\n"
        "أرسل رابط YouTube, TikTok, Instagram, Twitter..."
    )

async def download_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    
    if not url.startswith(('http://', 'https://')):
        await update.message.reply_text("❌ أرسل رابط صالح")
        return
    
    msg = await update.message.reply_text("⏳ جاري التحليل...")
    
    try:
        # إعدادات yt-dlp مبسطة
        ydl_opts = {
            'format': 'best[filesize<50M]',  # أقل من 50MB لتليغرام
            'outtmpl': '/tmp/%(title)s.%(ext)s',
            'quiet': True,
            'no_warnings': True,
        }
        
        await msg.edit_text("⬇️ جاري التحميل...")
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            
            # البحث عن الملف إذا تغير الامتداد
            if not os.path.exists(filename):
                base = filename.rsplit('.', 1)[0]
                for ext in ['.mp4', '.webm', '.mkv']:
                    if os.path.exists(base + ext):
                        filename = base + ext
                        break
            
            # التحقق من الحجم
            file_size = os.path.getsize(filename)
            if file_size > 50 * 1024 * 1024:
                await msg.edit_text("❌ الملف كبير جداً (>50MB)")
                os.remove(filename)
                return
            
            await msg.edit_text("📤 جاري الإرسال...")
            
            with open(filename, 'rb') as f:
                await context.bot.send_video(
                    chat_id=update.effective_chat.id,
                    video=f,
                    caption=f"✅ {info.get('title', 'فيديو')[:50]}",
                    supports_streaming=True
                )
            
            os.remove(filename)
            await msg.delete()
            
    except Exception as e:
        logger.error(f"Error: {e}")
        error_msg = str(e)
        
        if "Sign in to confirm" in error_msg:
            await msg.edit_text(
                "⚠️ YouTube يتطلب تأكيد.\n"
                "💡 جرب رابط TikTok أو Instagram أو Twitter"
            )
        else:
            await msg.edit_text(f"❌ خطأ: {error_msg[:200]}")

def main():
    if not TOKEN:
        print("❌ BOT_TOKEN غير موجود!")
        print("اكتب: export BOT_TOKEN='your_token'")
        return
    
    print("🚀 تشغيل البوت...")
    
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download_video))
    
    app.run_polling()

if __name__ == "__main__":
    main()
