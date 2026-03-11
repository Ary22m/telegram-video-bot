import os
import logging
import yt_dlp
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# إعداد التسجيل
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# قراءة التوكن من متغيرات البيئة (للأمان)
TOKEN = os.environ.get("BOT_TOKEN")

# مجلد التحميل
DOWNLOAD_FOLDER = "/tmp/downloads"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """رسالة الترحيب"""
    welcome_text = """
🎬 *بوت تحميل الفيديوهات* 

أرسل لي رابط فيديو من أي منصة:
• YouTube
• TikTok  
• Instagram
• Twitter/X
• Facebook
• Reddit
• و 1000+ موقع آخر!

⚡ سأقوم بتحميله وإرساله لك مباشرة.
    """
    await update.message.reply_text(welcome_text, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """رسالة المساعدة"""
    help_text = """
📖 *طريقة الاستخدام:*

1️⃣ انسخ رابط الفيديو من أي منصة
2️⃣ أرسله هنا في المحادثة
3️⃣ انتظر قليلاً حتى يتم التحميل
4️⃣ ستصلك الفيديو مباشرة!

⚠️ *ملاحظات:*
• الحد الأقصى للفيديو: 50 ميجابايت
• يجب أن يكون الرابط عام (غير خاص)
• قد يستغرق التحميل بضع دقائق حسب حجم الفيديو
    """
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def download_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة روابط الفيديو"""
    url = update.message.text.strip()
    chat_id = update.effective_chat.id
    
    if not url.startswith(('http://', 'https://')):
        await update.message.reply_text("❌ يرجى إرسال رابط صحيح يبدأ بـ http أو https")
        return
    
    wait_message = await update.message.reply_text("⏳ جاري تحليل الرابط...")
    
    try:
        ydl_opts = {
            'format': 'best[filesize<50M]/best',
            'outtmpl': f'{DOWNLOAD_FOLDER}/%(title)s_%(id)s.%(ext)s',
            'quiet': True,
            'no_warnings': True,
        }
        
        await wait_message.edit_text("🔍 جاري جلب معلومات الفيديو...")
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            title = info.get('title', 'فيديو')
            duration = info.get('duration', 0)
            size = info.get('filesize_approx', 0)
            
            if size and size > 50 * 1024 * 1024:
                await wait_message.edit_text("❌ الفيديو كبير جداً (أكبر من 50 ميجابايت)")
                return
            
            await wait_message.edit_text(f"⬇️ جاري تحميل: *{title[:50]}...*", parse_mode='Markdown')
            
            ydl.download([url])
            filename = ydl.prepare_filename(info)
            
            if not os.path.exists(filename):
                base_path = os.path.splitext(filename)[0]
                for file in os.listdir(DOWNLOAD_FOLDER):
                    if file.startswith(os.path.basename(base_path)):
                        filename = os.path.join(DOWNLOAD_FOLDER, file)
                        break
            
            await wait_message.edit_text("📤 جاري إرسال الفيديو...")
            
            with open(filename, 'rb') as video_file:
                await context.bot.send_video(
                    chat_id=chat_id,
                    video=video_file,
                    caption=f"✅ {title}\n⏱️ المدة: {duration//60}:{duration%60:02d}",
                    supports_streaming=True
                )
            
            os.remove(filename)
            await wait_message.delete()
            
    except Exception as e:
        logger.error(f"Error: {str(e)}")
        await wait_message.edit_text(f"❌ حدث خطأ: \n{str(e)[:200]}")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة الأخطاء"""
    logger.error(f"Update {update} caused error {context.error}")

def main():
    """تشغيل البوت"""
    if not TOKEN:
        logger.error("❌ لم يتم تعيين BOT_TOKEN!")
        return
    
    application = Application.builder().token(TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download_video))
    application.add_error_handler(error_handler)
    
    logger.info("🤖 البوت يعمل الآن!")
    application.run_polling()

if __name__ == "__main__":
    main()
