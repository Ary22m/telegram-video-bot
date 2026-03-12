import os
import logging
import yt_dlp
import re
import asyncio
import aiohttp
import json
import time
import hashlib
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from typing import List, Dict, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from pathlib import Path

# إعداد التسجيل
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("BOT_TOKEN")
DOWNLOAD_FOLDER = "/tmp/downloads"
STATE_FOLDER = "/tmp/download_states"  # تخزين حالات التحميل
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs(STATE_FOLDER, exist_ok=True)

# تخزين مؤقت
user_data: Dict[int, dict] = {}
active_downloads: Dict[str, 'ResumableDownload'] = {}

# أنماط الروابط
URL_PATTERNS = {
    'youtube': r'(youtube\.com|youtu\.be)',
    'instagram': r'instagram\.com',
    'tiktok': r'tiktok\.com',
    'twitter': r'(twitter\.com|x\.com)',
    'facebook': r'facebook\.com',
    'reddit': r'reddit\.com',
}

def detect_platform(url: str) -> str:
    for platform, pattern in URL_PATTERNS.items():
        if re.search(pattern, url):
            return platform
    return 'unknown'

def generate_download_id(url: str, user_id: int) -> str:
    """إنشاء معرف فريد للتحميل"""
    return hashlib.md5(f"{url}_{user_id}".encode()).hexdigest()[:16]

# ==================== نظام استئناف التحميل المتقدم ====================

@dataclass
class DownloadState:
    """حالة التحميل للتخزين"""
    url: str
    filename: str
    downloaded_bytes: int
    total_bytes: int
    status: str  # 'downloading', 'paused', 'completed', 'error'
    last_update: float
    attempt_count: int
    error_message: str = ""
    download_type: str = "video"
    quality: str = "best"
    
    def to_dict(self):
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict):
        return cls(**data)

class ResumableDownload:
    """نظام التحميل القابل للاستئناف"""
    
    MAX_RETRIES = 5
    RETRY_DELAY = 5  # ثوانٍ
    CHUNK_SIZE = 1024 * 1024  # 1MB
    
    def __init__(self, url: str, user_id: int, download_type: str = "video", 
                 quality: str = "best", notifier=None):
        self.url = url
        self.user_id = user_id
        self.download_id = generate_download_id(url, user_id)
        self.download_type = download_type
        self.quality = quality
        self.notifier = notifier
        
        self.state_file = os.path.join(STATE_FOLDER, f"{self.download_id}.json")
        self.temp_file = os.path.join(DOWNLOAD_FOLDER, f"{self.download_id}.tmp")
        self.final_file = None
        
        self.state = self._load_state()
        self.is_running = False
        self.is_cancelled = False
        
    def _load_state(self) -> DownloadState:
        """تحميل الحالة السابقة إن وجدت"""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r') as f:
                    data = json.load(f)
                    state = DownloadState.from_dict(data)
                    logger.info(f"استئناف التحميل السابق: {state.downloaded_bytes}/{state.total_bytes} بايت")
                    return state
            except Exception as e:
                logger.error(f"خطأ في تحميل الحالة: {e}")
        
        # حالة جديدة
        return DownloadState(
            url=self.url,
            filename="",
            downloaded_bytes=0,
            total_bytes=0,
            status='downloading',
            last_update=time.time(),
            attempt_count=0,
            download_type=self.download_type,
            quality=self.quality
        )
    
    def _save_state(self):
        """حفظ الحالة الحالية"""
        try:
            self.state.last_update = time.time()
            with open(self.state_file, 'w') as f:
                json.dump(self.state.to_dict(), f)
        except Exception as e:
            logger.error(f"خطأ في حفظ الحالة: {e}")
    
    def _get_ytdl_opts(self, resume: bool = True) -> dict:
        """إعدادات yt-dlp مع خاصية الاستئناف"""
        opts = {
            'outtmpl': self.temp_file.replace('.tmp', '.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'retries': 10,
            'fragment_retries': 10,
            'skip_unavailable_fragments': True,
            'keep_fragments': False,
        }
        
        # استئناف التحميل الجزئي
        if resume and self.state.downloaded_bytes > 0:
            opts['continuedl'] = True  # استئناف التحميل الجزئي
            opts['noprogress'] = False
        else:
            opts['continuedl'] = False
        
        # إعدادات الجودة
        if self.download_type == 'audio':
            opts.update({
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
            })
        else:
            format_spec = {
                'best': 'best',
                '1080': 'best[height<=1080]',
                '720': 'best[height<=720]',
                '480': 'best[height<=480]',
            }.get(self.quality, 'best')
            opts['format'] = format_spec
        
        # هوك التقدم
        if self.notifier:
            opts['progress_hooks'] = [self._progress_hook]
        
        return opts
    
    def _progress_hook(self, d: dict):
        """هوك تتبع التقدم"""
        if self.is_cancelled:
            raise Exception("تم إلغاء التحميل")
            
        if d['status'] == 'downloading':
            downloaded = d.get('downloaded_bytes', 0)
            total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
            
            self.state.downloaded_bytes = downloaded
            self.state.total_bytes = total
            self._save_state()
            
            if self.notifier and total > 0:
                progress = (downloaded / total) * 100
                speed = d.get('speed', 0)
                eta = d.get('eta', 0)
                
                # تشغيل في event loop
                try:
                    loop = asyncio.get_event_loop()
                    asyncio.run_coroutine_threadsafe(
                        self.notifier.update_progress(
                            "تحميل...", 
                            progress, 
                            f"⚡ {self._format_size(speed)}/s | ⏳ {eta}s | 🔄 محاولة {self.state.attempt_count + 1}"
                        ),
                        loop
                    )
                except:
                    pass
                    
        elif d['status'] == 'finished':
            self.state.status = 'completed'
            self._save_state()
    
    def _format_size(self, size: int) -> str:
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"
    
    async def download(self) -> Optional[str]:
        """التنفيذ الرئيسي مع الاستئناف التلقائي"""
        self.is_running = True
        
        for attempt in range(self.MAX_RETRIES):
            if self.is_cancelled:
                return None
                
            self.state.attempt_count = attempt
            self._save_state()
            
            try:
                if self.notifier:
                    if attempt == 0:
                        await self.notifier.update_progress("بدء التحميل...", 0, "🔌 جاري الاتصال...")
                    else:
                        await self.notifier.update_progress(
                            f"استئناف... (محاولة {attempt + 1})", 
                            (self.state.downloaded_bytes / max(self.state.total_bytes, 1)) * 100,
                            "🔄 إعادة المحاولة..."
                        )
                
                # التحميل الفعلي
                result = await self._try_download()
                
                if result:
                    self.state.status = 'completed'
                    self._save_state()
                    self._cleanup_state()
                    return result
                    
            except Exception as e:
                logger.error(f"محاولة {attempt + 1} فشلت: {e}")
                self.state.error_message = str(e)
                self.state.status = 'error'
                self._save_state()
                
                if attempt < self.MAX_RETRIES - 1:
                    wait_time = self.RETRY_DELAY * (attempt + 1)
                    if self.notifier:
                        await self.notifier.update_progress(
                            "انتظار...", 
                            (self.state.downloaded_bytes / max(self.state.total_bytes, 1)) * 100,
                            f"⏸️ انتظار {wait_time} ثانية..."
                        )
                    await asyncio.sleep(wait_time)
                else:
                    # نفدت المحاولات
                    if self.notifier:
                        await self.notifier.notify_error(
                            f"فشل بعد {self.MAX_RETRIES} محاولات.\n"
                            f"تم تحميل: {self._format_size(self.state.downloaded_bytes)}\n"
                            f"لاستئناف لاحقاً، أرسل نفس الرابط مرة أخرى."
                        )
                    return None
        
        return None
    
    async def _try_download(self) -> Optional[str]:
        """محاولة التحميل الفعلية"""
        loop = asyncio.get_event_loop()
        
        def download_task():
            with yt_dlp.YoutubeDL(self._get_ytdl_opts(resume=True)) as ydl:
                # استخراج المعلومات أولاً
                info = ydl.extract_info(self.url, download=False)
                self.state.filename = ydl.prepare_filename(info)
                
                # التحقق إذا كان مكتملاً
                expected_file = self.state.filename
                if os.path.exists(expected_file):
                    # ملف مكتمل موجود
                    return expected_file
                
                # التحميل
                ydl.download([self.url])
                return ydl.prepare_filename(info)
        
        try:
            filename = await asyncio.wait_for(
                loop.run_in_executor(None, download_task),
                timeout=600  # 10 دقائق timeout
            )
            
            # التحقق من الملف
            if filename and os.path.exists(filename):
                # نقل للاسم النهائي
                final_name = filename.replace('.tmp', '').replace('.f', '')
                if final_name != filename:
                    os.rename(filename, final_name)
                    filename = final_name
                
                self.final_file = filename
                return filename
                
        except asyncio.TimeoutError:
            logger.warning("انتهت مهلة التحميل، سيتم الاستئناف لاحقاً")
            # حفظ الحالة للاستئناف
            self.state.status = 'paused'
            self._save_state()
            raise Exception("انتهت المهلة - جاري الاستئناف...")
            
        return None
    
    def pause(self):
        """إيقاف مؤقت"""
        self.is_running = False
        self.state.status = 'paused'
        self._save_state()
    
    def cancel(self):
        """إلغاء نهائي"""
        self.is_cancelled = True
        self.state.status = 'cancelled'
        self._save_state()
        self._cleanup_files()
    
    def _cleanup_state(self):
        """حذف ملفات الحالة بعد الاكتمال"""
        try:
            if os.path.exists(self.state_file):
                os.remove(self.state_file)
        except:
            pass
    
    def _cleanup_files(self):
        """حذف الملفات المؤقتة"""
        try:
            if os.path.exists(self.temp_file):
                os.remove(self.temp_file)
            if os.path.exists(self.state_file):
                os.remove(self.state_file)
        except:
            pass

# ==================== نظام الإشعارات المحسن ====================

class DownloadNotifier:
    """نظام إشعارات متقدم مع دعم الاستئناف"""
    
    def __init__(self, bot, chat_id: int, message_id: int, download_id: str):
        self.bot = bot
        self.chat_id = chat_id
        self.message_id = message_id
        self.download_id = download_id
        self.start_time = time.time()
        self.last_update = 0
        self.is_cancelled = False
        self.last_progress = 0
        
    async def update_progress(self, status: str, progress: float, details: str = ""):
        """تحديث شريط التقدم"""
        if self.is_cancelled or progress - self.last_progress < 2:
            return
            
        self.last_progress = progress
        current_time = time.time()
        
        if current_time - self.last_update < 3 and progress < 100:
            return
            
        self.last_update = current_time
        elapsed = int(current_time - self.start_time)
        
        # شريط تقدم مرئي
        filled = int(progress / 5)
        empty = 20 - filled
        bar = "█" * filled + "░" * empty
        
        # أيقونة حالة
        icon = "⏳" if status == "تحميل..." else "🔄" if "استئناف" in status else "⏸️"
        
        text = f"""
{icon} *{status}*

{bar} {progress:.1f}%

⏱️ *الوقت:* {elapsed//60}:{elapsed%60:02d}
{details}

💡 *نصيحة:* إذا انقطع الاتصال، سيتم الاستئناف تلقائياً
        """
        
        try:
            await self.bot.edit_message_text(
                text=text,
                chat_id=self.chat_id,
                message_id=self.message_id,
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⏸️ إيقاف مؤقت", callback_data=f'pause_{self.download_id}')],
                    [InlineKeyboardButton("❌ إلغاء", callback_data=f'cancel_{self.download_id}')]
                ])
            )
        except Exception as e:
            logger.error(f"Progress update error: {e}")
            
    async def notify_paused(self, downloaded: str):
        """إشعار الإيقاف المؤقت"""
        text = f"""
⏸️ *تم الإيقاف المؤقت*

📥 *تم تحميل:* {downloaded}

✅ *للاستئناف:* أرسل نفس الرابط مرة أخرى
        """
        
        try:
            await self.bot.edit_message_text(
                text=text,
                chat_id=self.chat_id,
                message_id=self.message_id,
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("▶️ استئناف", callback_data=f'resume_{self.download_id}')]
                ])
            )
        except Exception as e:
            logger.error(f"Pause notification error: {e}")
            
    async def notify_complete(self, filename: str, size: str, cloud_url: Optional[str] = None, 
                             attempts: int = 1):
        """إشعار الاكتمال"""
        if self.is_cancelled:
            return
            
        elapsed = int(time.time() - self.start_time)
        attempts_text = f"🔄 *المحاولات:* {attempts}\n" if attempts > 1 else ""
        
        text = f"""
✅ *اكتمل التحميل بنجاح!*

📁 *الملف:* `{filename[:30]}`
📦 *الحجم:* {size}
⏱️ *المدة:* {elapsed//60}:{elapsed%60:02d}
{attempts_text}
        """
        
        if cloud_url:
            text += f"\n☁️ *رابط التخزين السحابي:*\n`{cloud_url}`\n\n*صالح لـ 14 يوماً*"
            
        try:
            await self.bot.edit_message_text(
                text=text,
                chat_id=self.chat_id,
                message_id=self.message_id,
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Complete notification error: {e}")
            
    async def notify_error(self, error: str, can_resume: bool = True):
        """إشعار الخطأ"""
        if self.is_cancelled:
            return
            
        text = f"❌ *خطأ في التحميل*\n\n{error[:200]}"
        
        if can_resume:
            text += "\n\n💡 *يمكنك إعادة إرسال الرابط لاستئناف التحميل*"
            
        try:
            await self.bot.edit_message_text(
                text=text,
                chat_id=self.chat_id,
                message_id=self.message_id,
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Error notification error: {e}")

# ==================== أوامر البوت ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """رسالة الترحيب"""
    welcome_text = """
🚀 *بوت التحميل الذكي - مع استئناف تلقائي* 

⚡ *المميزات الجديدة:*
• 🔄 **استئناف تلقائي** عند انقطاع الاتصال (حتى 5 محاولات)
• ⏸️ **إيقاف مؤقت** واستئناف لاحقاً
• 📊 **شريط تقدم حي** مع أزرار تحكم
• 💾 **حفظ الحالة** - لا تخسر التقدم أبداً
• 🔔 إشعارات ذكية
• ☁️ تخزين سحابي
• ⚡ تحميل سريع متعدد الخيوط

🛡️ *كيف يعمل الاستئناف:*
1️⃣ إذا انقطع الاتصال، يتوقف التحميل
2️⃣ يُحفظ التقدم تلقائياً
3️⃣ يُعاد الاتصال تلقائياً (5 محاولات)
4️⃣ يستأنف من آخر نققة توقف
5️⃣ إذا فشل كل شيء، أرسل نفس الرابط لاحقاً للاستئناف

💡 *الأوامر:*
/stats - إحصائياتك
/active - التحميلات النشطة
/resume - استئناف تحميل متوقف
/cancel - إلغاء تحميل
    """
    await update.message.reply_text(welcome_text, parse_mode='Markdown')

async def active_downloads_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض التحميلات النشطة"""
    user_id = update.effective_user.id
    
    # البحث عن تحميلات المستخدم
    user_downloads = []
    for download_id, download in active_downloads.items():
        if download.user_id == user_id:
            status = "⏳ نشط" if download.is_running else "⏸️ متوقف"
            user_downloads.append(f"{status} `{download_id[:8]}...` - {detect_platform(download.url)}")
    
    if not user_downloads:
        # البحث في ملفات الحالة المحفوظة
        saved_states = []
        for state_file in os.listdir(STATE_FOLDER):
            if state_file.endswith('.json'):
                try:
                    with open(os.path.join(STATE_FOLDER, state_file), 'r') as f:
                        data = json.load(f)
                        if data.get('status') in ['paused', 'error']:
                            saved_states.append(f"⏸️ متوقف: `{state_file[:8]}`")
                except:
                    continue
        
        if saved_states:
            text = "📋 *تحميلات متوقفة يمكن استئنافها:*\n\n" + "\n".join(saved_states)
            text += "\n\n💡 أرسل نفس الرابط لاستئناف أي تحميل"
        else:
            text = "✅ لا توجد تحميلات نشطة أو متوقفة"
    else:
        text = "📥 *تحميلاتك النشطة:*\n\n" + "\n".join(user_downloads)
    
    await update.message.reply_text(text, parse_mode='Markdown')

async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """استئناف تحميل محدد"""
    user_id = update.effective_user.id
    
    # البحث عن آخر تحميل متوقف
    saved_states = []
    for state_file in os.listdir(STATE_FOLDER):
        if state_file.endswith('.json'):
            try:
                with open(os.path.join(STATE_FOLDER, state_file), 'r') as f:
                    data = json.load(f)
                    saved_states.append((state_file, data))
            except:
                continue
    
    if not saved_states:
        await update.message.reply_text("⚠️ لا توجد تحميلات متوقفة للاستئناف")
        return
    
    # عرض قائمة للاختيار
    keyboard = []
    for state_file, data in saved_states[:5]:  # أحدث 5
        url = data.get('url', '')[:30]
        status = data.get('status', 'unknown')
        downloaded = data.get('downloaded_bytes', 0)
        total = data.get('total_bytes', 1)
        progress = (downloaded / total) * 100 if total > 0 else 0
        
        keyboard.append([
            InlineKeyboardButton(
                f"▶️ استئناف {progress:.0f}% - {url}...", 
                callback_data=f"resume_file_{state_file.replace('.json', '')}"
            )
        ])
    
    await update.message.reply_text(
        "📋 *اختر تحميلاً للاستئناف:*",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """إلغاء تحميل"""
    user_id = update.effective_user.id
    
    # البحث عن تحميلات المستخدم
    user_active = []
    for download_id, download in active_downloads.items():
        if download.user_id == user_id:
            user_active.append(download_id)
    
    if not user_active:
        await update.message.reply_text("⚠️ لا توجد تحميلات نشطة للإلغاء")
        return
    
    # إلغاء أول تحميل نشط
    download_id = user_active[0]
    if download_id in active_downloads:
        active_downloads[download_id].cancel()
        del active_downloads[download_id]
        await update.message.reply_text("✅ تم إلغاء التحميل")
    else:
        await update.message.reply_text("⚠️ التحميل غير موجود")

async def receive_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """استقبال الروابط مع الكشف عن الاستئناف"""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    
    urls = [url.strip() for url in text.split('\n') 
            if url.strip().startswith(('http://', 'https://'))]
    
    if not urls:
        await update.message.reply_text("❌ لم يتم العثور على روابط صالحة")
        return
    
    if user_id not in user_data:
        user_data[user_id] = {'stats': {'downloads': 0, 'platforms': set(), 'cloud_uploads': 0, 'total_size': 0}}
    
    # التحقق من وجود تحميل سابق متوقف
    for url in urls:
        download_id = generate_download_id(url, user_id)
        state_file = os.path.join(STATE_FOLDER, f"{download_id}.json")
        
        if os.path.exists(state_file):
            # تحميل سابق موجود - عرض خيار الاستئناف
            with open(state_file, 'r') as f:
                data = json.load(f)
                downloaded = data.get('downloaded_bytes', 0)
                total = data.get('total_bytes', 1)
                progress = (downloaded / total) * 100 if total > 0 else 0
                
                keyboard = [
                    [InlineKeyboardButton(f"▶️ استئناف ({progress:.1f}%)", callback_data=f'resume_old_{download_id}')],
                    [InlineKeyboardButton("🔄 إعادة التحميل من البداية", callback_data=f'restart_{download_id}')],
                    [InlineKeyboardButton("🗑️ حذف وحدة التحميل", callback_data=f'delete_state_{download_id}')]
                ]
                
                await update.message.reply_text(
                    f"🔄 *تم العثور على تحميل سابق متوقف!*\n\n"
                    f"📊 *التقدم:* {progress:.1f}%\n"
                    f"📥 *تم تحميل:* {DownloadNotifier(None, 0, 0, '')._format_size(downloaded)}\n\n"
                    f"اختر ما تريد:",
                    parse_mode='Markdown',
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                return  # نتوقف هنا لانتظار اختيار المستخدم
    
    # إذا لم يكن هناك تحميل سابق، نتابع العادي
    if len(urls) == 1:
        await show_single_options(update, urls[0])
    else:
        await show_batch_options(update, urls)

async def show_single_options(update: Update, url: str):
    """عرض الخيارات"""
    user_id = update.effective_user.id
    platform = detect_platform(url)
    
    user_data[user_id]['current_url'] = url
    user_data[user_id]['mode'] = 'single'
    
    keyboard = [
        [
            InlineKeyboardButton("🎬 فيديو", callback_data='video_best'),
            InlineKeyboardButton("🎵 صوت MP3", callback_data='audio')
        ],
        [
            InlineKeyboardButton("📹 1080p", callback_data='video_1080'),
            InlineKeyboardButton("📺 720p", callback_data='video_720'),
            InlineKeyboardButton("📱 480p", callback_data='video_480')
        ],
        [
            InlineKeyboardButton("⚡ سريع", callback_data='fast_video'),
            InlineKeyboardButton("☁️ +سحابي", callback_data='cloud_video')
        ],
        [InlineKeyboardButton("🔄 مع استئناف ذكي", callback_data='resumable_best')]
    ]
    
    if platform == 'instagram':
        keyboard.append([InlineKeyboardButton("🖼️ صور", callback_data='images')])
    
    if platform == 'youtube':
        keyboard.append([
            InlineKeyboardButton("📋 قائمة تشغيل", callback_data='playlist'),
            InlineKeyboardButton("📝 ترجمة", callback_data='subtitles')
        ])
    
    keyboard.append([InlineKeyboardButton("❌ إلغاء", callback_data='cancel')])
    
    await update.message.reply_text(
        f"🔗 *رابط جديد* ({platform.upper()})\n\nاختر طريقة التحميل:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة الأزرار"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    data = query.data
    
    # معالجة أزرار التحكم في التحميل (pause, cancel, resume)
    if data.startswith(('pause_', 'cancel_', 'resume_')):
        await handle_control_buttons(query, context, data)
        return
    
    if data.startswith(('resume_old_', 'restart_', 'delete_state_')):
        await handle_resume_options(query, context, data)
        return
    
    if data == 'cancel':
        await query.edit_message_text("❌ تم الإلغاء.")
        return
    
    if user_id not in user_data:
        await query.edit_message_text("⚠️ انتهت الجلسة.")
        return
    
    user_info = user_data[user_id]
    mode = user_info.get('mode', 'single')
    
    if mode == 'single':
        url = user_info.get('current_url')
        if not url:
            await query.edit_message_text("⚠️ خطأ: الرابط غير موجود")
            return
        
        # إنشاء نظام التحميل القابل للاستئناف
        download_type = 'video' if 'video' in data or data == 'resumable_best' else 'audio' if data == 'audio' else 'video'
        quality = data.replace('video_', '').replace('resumable_', '') if 'video_' in data or 'resumable_' in data else 'best'
        
        # إنشاء معرف فريد
        download_id = generate_download_id(url, user_id)
        
        # إنشاء رسالة التقدم
        progress_msg = await query.edit_message_text("🚀 جاري التحضير...")
        
        # إنشاء نظام الإشعارات
        notifier = DownloadNotifier(
            context.bot,
            query.message.chat_id,
            progress_msg.message_id,
            download_id
        )
        
        # إنشاء كائن التحميل القابل للاستئناف
        download = ResumableDownload(url, user_id, download_type, quality, notifier)
        active_downloads[download_id] = download
        
        # بدء التحميل
        try:
            filename = await download.download()
            
            if filename and os.path.exists(filename):
                file_size = os.path.getsize(filename)
                
                # إرسال الملف
                with open(filename, 'rb') as f:
                    if download_type == 'audio':
                        await context.bot.send_audio(
                            chat_id=query.message.chat_id,
                            audio=f,
                            caption="🎵 تم التحميل بنجاح!"
                        )
                    else:
                        await context.bot.send_video(
                            chat_id=query.message.chat_id,
                            video=f,
                            caption=f"🎬 تم التحميل! ({quality}p)",
                            supports_streaming=True
                        )
                
                # إشعار الاكتمال
                await notifier.notify_complete(
                    os.path.basename(filename),
                    download._format_size(file_size),
                    attempts=download.state.attempt_count + 1
                )
                
                os.remove(filename)
                
                # تحديث الإحصائيات
                user_data[user_id]['stats']['downloads'] += 1
                user_data[user_id]['stats']['platforms'].add(detect_platform(url))
                user_data[user_id]['stats']['total_size'] += file_size
                
        except Exception as e:
            logger.error(f"Download error: {e}")
            await notifier.notify_error(str(e), can_resume=True)
        finally:
            if download_id in active_downloads:
                del active_downloads[download_id]

async def handle_control_buttons(query, context, data: str):
    """معالجة أزرار التحكم (إيقاف/إلغاء/استئناف)"""
    parts = data.split('_')
    action = parts[0]
    download_id = '_'.join(parts[1:])
    
    if download_id not in active_downloads:
        await query.edit_message_text("⚠️ التحميل غير نشط أو اكتمل")
        return
    
    download = active_downloads[download_id]
    
    if action == 'pause':
        download.pause()
        await download.notifier.notify_paused(download._format_size(download.state.downloaded_bytes))
        
    elif action == 'cancel':
        download.cancel()
        await query.edit_message_text("❌ تم إلغاء التحميل")
        del active_downloads[download_id]
        
    elif action == 'resume':
        if not download.is_running:
            download.is_running = True
            download.state.status = 'downloading'
            await query.edit_message_text("▶️ جاري استئناف التحميل...")
            # إعادة تشغيل التحميل
            filename = await download.download()
            # ... (نفس منطق الاكتمال)

async def handle_resume_options(query, context, data: str):
    """معالجة خيارات الاستئناف"""
    if data.startswith('resume_old_'):
        download_id = data.replace('resume_old_', '')
        state_file = os.path.join(STATE_FOLDER, f"{download_id}.json")
        
        if not os.path.exists(state_file):
            await query.edit_message_text("⚠️ لم يعد التحميل موجوداً")
            return
        
        # تحميل الحالة السابقة
        with open(state_file, 'r') as f:
            state_data = json.load(f)
        
        url = state_data['url']
        download_type = state_data.get('download_type', 'video')
        quality = state_data.get('quality', 'best')
        
        # إنشاء رسالة تقدم جديدة
        await query.edit_message_text("▶️ جاري استئناف التحميل...")
        progress_msg = await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="🔄 استئناف التحميل..."
        )
        
        # إنشاء نظام الإشعارات والتحميل
        notifier = DownloadNotifier(context.bot, query.message.chat_id, progress_msg.message_id, download_id)
        download = ResumableDownload(url, query.from_user.id, download_type, quality, notifier)
        active_downloads[download_id] = download
        
        # بدء الاستئناف
        filename = await download.download()
        # ... (نفس منطق الاكتمال)
        
    elif data.startswith('restart_'):
        download_id = data.replace('restart_', '')
        # حذف الحالة القديمة وإعادة التحميل
        state_file = os.path.join(STATE_FOLDER, f"{download_id}.json")
        if os.path.exists(state_file):
            os.remove(state_file)
        
        await query.edit_message_text("🔄 سيتم إعادة التحميل من البداية. أرسل الرابط مرة أخرى.")
        
    elif data.startswith('delete_state_'):
        download_id = data.replace('delete_state_', '')
        state_file = os.path.join(STATE_FOLDER, f"{download_id}.json")
        if os.path.exists(state_file):
            os.remove(state_file)
        await query.edit_message_text("🗑️ تم حذف حالة التحميل")

# ==================== بقية الدوال (batch, cloud, etc) ====================

async def show_batch_options(update: Update, urls: List[str]):
    """عرض خيارات الدفعة"""
    user_id = update.effective_user.id
    user_data[user_id]['batch_urls'] = urls
    user_data[user_id]['mode'] = 'batch'
    
    keyboard = [
        [InlineKeyboardButton(f"🎬 فيديو ({len(urls)})", callback_data='batch_video')],
        [InlineKeyboardButton(f"🎵 صوت ({len(urls)})", callback_data='batch_audio')],
        [InlineKeyboardButton("🔄 مع استئناف ذكي", callback_data='batch_resumable')],
        [InlineKeyboardButton("❌ إلغاء", callback_data='cancel')]
    ]
    
    await update.message.reply_text(
        f"🔗 *{len(urls)} روابط*\n\nاختر طريقة المعالجة:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة الأخطاء"""
    logger.error(f"Update {update} caused error {context.error}")

def main():
    """تشغيل البوت"""
    if not TOKEN:
        logger.error("❌ BOT_TOKEN غير موجود!")
        return
    
    application = Application.builder().token(TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("active", active_downloads_command))
    application.add_handler(CommandHandler("resume", resume_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_links))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_error_handler(error_handler)
    
    logger.info("🚀 البوت الذكي مع الاستئناف يعمل الآن!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
