import html
import io
import json
import math
import os
import threading
import time
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
import telebot
from telebot import apihelper, types
from flask import Flask

# --- إعداد سيرفر الويب الوهمي لإبقاء البوت نشطاً على Render ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive and connected to Cloud PostgreSQL!"

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

def keep_alive():
    t = threading.Thread(target=run_web_server, daemon=True)
    t.start()
# -------------------------------------------------------------

TOKEN = os.environ.get("BOT_TOKEN", "7959977846:AAEHAi6gP3tP6oWefIX6DovDp6Nl2NqLCyk")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "6704551303"))

# رابط قاعدة البيانات السحابية (يقرأه تلقائياً من إعدادات Render)
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL.startswith("Postgresql://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("Postgresql://"):]

apihelper.ENABLE_MIDDLEWARE = False
bot = telebot.TeleBot(TOKEN, threaded=True, num_threads=16)

OLD_JSON_PATH = "data.json"
VIDEOS_PER_PAGE = 5
USER_COOLDOWN = 5
SUB_CACHE_TTL = 30

user_action_locks = {}
user_locks_guard = threading.Lock()
user_cooldowns = {}
sub_cache = {}
db_pool = None

ADMIN_BUTTONS = {
    "📢 إذاعة للكل", "📊 الإحصائيات", "✅ فك حظر", "🚫 حظر",
    "💬 رسالة لمستخدم", "🔎 معلومات مستخدم", "📌 الاشتراك الإجباري",
    "📄 تصدير المستخدمين", "👮‍♂️ الأدمنية", "⚙️ الإعدادات",
    "🎬 رفع فيديو", "🗑 حذف فيديوهات", "🏠 الرئيسية (عرض الصفحات)",
    "🔙 إلغاء والعودة للوحة"
}


def get_user_lock(user_id):
    with user_locks_guard:
        if user_id not in user_action_locks:
            user_action_locks[user_id] = threading.Lock()
        return user_action_locks[user_id]


# --- إدارة الاتصال السحابي بقاعدة بيانات PostgreSQL ---
def init_pool():
    global db_pool
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL is missing! Please add it in Render Environment Variables.")
    db_pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=1,
        maxconn=15,
        dsn=DATABASE_URL
    )


class DBContext:
    def __enter__(self):
        self.conn = db_pool.getconn()
        self.cur = self.conn.cursor(cursor_factory=RealDictCursor)
        return self.cur

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.conn.rollback()
        else:
            self.conn.commit()
        self.cur.close()
        db_pool.putconn(self.conn)


def init_db():
    init_pool()
    with DBContext() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                is_banned INTEGER DEFAULT 0,
                is_admin INTEGER DEFAULT 0,
                joined_at DOUBLE PRECISION
            );
            CREATE TABLE IF NOT EXISTS videos (
                id SERIAL PRIMARY KEY,
                file_id TEXT NOT NULL,
                caption TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS force_sub (
                chat_id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                title TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending_deletes (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                message_ids TEXT NOT NULL,
                delete_at DOUBLE PRECISION NOT NULL,
                page_num INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_delete_at ON pending_deletes(delete_at);
        """)
        defaults = {
            "bot_active": "1",
            "welcome_msg": "👋 أهلاً بك!",
            "delete_after": "60"
        }
        for k, v in defaults.items():
            cur.execute(
                "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                (k, v)
            )
    migrate_from_old_json()


def migrate_from_old_json():
    """نقل البيانات تلقائياً من ملف data.json القديم إلى السحابة إن وجد"""
    if not os.path.exists(OLD_JSON_PATH):
        return
    try:
        with open(OLD_JSON_PATH, "r", encoding="utf-8") as f:
            old = json.load(f)
        with DBContext() as cur:
            if "welcome_msg" in old:
                cur.execute("UPDATE settings SET value=%s WHERE key='welcome_msg'", (old["welcome_msg"],))
            if "delete_after" in old:
                cur.execute("UPDATE settings SET value=%s WHERE key='delete_after'", (str(old["delete_after"]),))
            if "bot_active" in old:
                cur.execute("UPDATE settings SET value=%s WHERE key='bot_active'", ("1" if old["bot_active"] else "0",))

            for uid in old.get("users", []):
                cur.execute("INSERT INTO users (user_id, joined_at) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING", (int(uid), time.time()))
            for uid in old.get("banned", []):
                cur.execute("INSERT INTO users (user_id, is_banned, joined_at) VALUES (%s, 1, %s) ON CONFLICT (user_id) DO UPDATE SET is_banned=1", (int(uid), time.time()))
            for uid in old.get("admins", []):
                cur.execute("INSERT INTO users (user_id, is_admin, joined_at) VALUES (%s, 1, %s) ON CONFLICT (user_id) DO UPDATE SET is_admin=1", (int(uid), time.time()))
            for vid in old.get("videos", []):
                cur.execute("INSERT INTO videos (file_id, caption) VALUES (%s, %s)", (vid["file_id"], vid.get("caption", "")))
            for ch in old.get("force_sub", []):
                if isinstance(ch, dict):
                    cur.execute("INSERT INTO force_sub (chat_id, url, title) VALUES (%s, %s, %s) ON CONFLICT (chat_id) DO NOTHING", (str(ch["chat_id"]), ch["url"], ch["title"]))
        os.rename(OLD_JSON_PATH, OLD_JSON_PATH + ".bak")
        print("[System] تم نقل البيانات القديمة إلى السحابة بنجاح.")
    except Exception as e:
        print(f"[Migration Warning]: {e}")


# --- دوال مساعدة لقاعدة البيانات ---
def get_setting(key, default=""):
    with DBContext() as cur:
        cur.execute("SELECT value FROM settings WHERE key=%s", (key,))
        row = cur.fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with DBContext() as cur:
        cur.execute(
            "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
            (key, str(value))
        )


def register_user(user_id):
    with DBContext() as cur:
        cur.execute(
            "INSERT INTO users (user_id, joined_at) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING",
            (int(user_id), time.time())
        )


def is_banned(user_id):
    if is_admin(user_id):
        return False
    with DBContext() as cur:
        cur.execute("SELECT is_banned FROM users WHERE user_id=%s", (int(user_id),))
        row = cur.fetchone()
        return bool(row and row["is_banned"] == 1)


def is_admin(user_id):
    if int(user_id) == int(ADMIN_ID):
        return True
    with DBContext() as cur:
        cur.execute("SELECT is_admin FROM users WHERE user_id=%s", (int(user_id),))
        row = cur.fetchone()
        return bool(row and row["is_admin"] == 1)


def get_all_videos():
    with DBContext() as cur:
        cur.execute("SELECT id, file_id, caption FROM videos ORDER BY id ASC")
        return [dict(r) for r in cur.fetchall()]


def get_all_force_sub():
    with DBContext() as cur:
        cur.execute("SELECT chat_id, url, title FROM force_sub")
        return [dict(r) for r in cur.fetchall()]


# --- منع الإغراق ---
def check_rate_limit(user_id):
    if is_admin(user_id):
        return False
    now = time.time()
    last = user_cooldowns.get(user_id, 0)
    if now - last < USER_COOLDOWN:
        return True
    user_cooldowns[user_id] = now
    return False


# --- مقاطعة الخطوات ---
def check_interrupt(message):
    if not message.text:
        return False
    txt = message.text.strip()
    if txt.startswith("/") or txt in ADMIN_BUTTONS:
        bot.clear_step_handler_by_chat_id(message.chat.id)
        if txt.startswith("/start") or txt.startswith("/admin") or txt == "🔙 إلغاء والعودة للوحة":
            start_and_admin_cmd(message)
        elif is_admin(message.from_user.id):
            handle_admin_buttons(message)
        return True
    return False


# --- إرسال آمن ---
def safe_api_call(func, *args, **kwargs):
    for _ in range(3):
        try:
            return func(*args, **kwargs)
        except apihelper.ApiTelegramException as e:
            if e.error_code == 429:
                wait_sec = int(e.result_json.get("parameters", {}).get("retry_after", 3))
                time.sleep(wait_sec + 0.5)
            else:
                raise e
    return None


# --- فحص الاشتراك الإجباري ---
def check_sub(user_id, force_test=False, bypass_cache=False):
    if is_admin(user_id) and not force_test:
        return True, [], None

    channels = get_all_force_sub()
    if not channels:
        return True, [], None

    now = time.time()
    if not force_test and not bypass_cache and user_id in sub_cache:
        cached_ok, cached_missing, exp = sub_cache[user_id]
        if now < exp and cached_ok:
            return True, [], None

    not_joined = []
    for ch_obj in channels:
        target = int(ch_obj["chat_id"]) if ch_obj["chat_id"].lstrip("-").isdigit() else ch_obj["chat_id"]
        try:
            member = safe_api_call(bot.get_chat_member, target, user_id)
            if member and member.status in ["left", "kicked", "restricted"]:
                not_joined.append(ch_obj)
        except Exception:
            return False, channels, f"⚠️ تعذر التحقق من القناة ({ch_obj['title']})! تأكد من رفع البوت مشرفاً."

    is_ok = len(not_joined) == 0
    if not force_test:
        sub_cache[user_id] = (is_ok, not_joined, now + SUB_CACHE_TTL)
    return is_ok, not_joined, None


def send_force_sub_message(chat_id, missing_channels, error_note=None):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for idx, ch_obj in enumerate(missing_channels, start=1):
        btn_title = ch_obj.get("title") or f"قناة رقم {idx}"
        kb.add(types.InlineKeyboardButton(f"📢 اشترك في ({btn_title})", url=ch_obj["url"]))
    kb.add(types.InlineKeyboardButton("✅ تحققت من الاشتراك", callback_data="user_check_sub"))

    text = "⚠️ <b>عذراً! يجب عليك الاشتراك في جميع القنوات التالية أولاً:</b>"
    if error_note:
        text += f"\n\n<code>{html.escape(error_note)}</code>"

    safe_api_call(bot.send_message, chat_id, text, parse_mode="HTML", reply_markup=kb)


# --- القوائم والصفحات ---
def pages_inline_markup():
    videos = get_all_videos()
    total_videos = len(videos)
    kb = types.InlineKeyboardMarkup(row_width=5)

    if total_videos > 0:
        total_pages = math.ceil(total_videos / VIDEOS_PER_PAGE)
        buttons = [
            types.InlineKeyboardButton(f"🎬 {page}", callback_data=f"vpage_{page}")
            for page in range(1, total_pages + 1)
        ]
        kb.add(*buttons)

    kb.add(types.InlineKeyboardButton("📤 إرسال فيديوهات أو ملفات", callback_data="user_send_video"))
    return kb


def send_pages_menu(chat_id, custom_prefix=""):
    welcome = html.escape(get_setting("welcome_msg", "👋 أهلاً بك!"))
    text = f"{custom_prefix}{welcome}\n\n📂 التحكم أو الإرسال 👇"
    safe_api_call(bot.send_message, chat_id, text, parse_mode="HTML", reply_markup=pages_inline_markup())


# --- طابور الحذف السحابي المستمر ---
def enqueue_delete(chat_id, message_ids, delay, page_num):
    with DBContext() as cur:
        cur.execute(
            "INSERT INTO pending_deletes (chat_id, message_ids, delete_at, page_num) VALUES (%s, %s, %s, %s)",
            (chat_id, json.dumps(message_ids), time.time() + delay, page_num)
        )


def background_cleaner_worker():
    while True:
        try:
            now = time.time()
            with DBContext() as cur:
                cur.execute(
                    "SELECT id, chat_id, message_ids, page_num FROM pending_deletes WHERE delete_at <= %s LIMIT 50",
                    (now,)
                )
                tasks = cur.fetchall()

            for task in tasks:
                mids = json.loads(task["message_ids"])
                for mid in mids:
                    try:
                        safe_api_call(bot.delete_message, task["chat_id"], mid)
                    except Exception:
                        pass
                try:
                    send_pages_menu(
                        task["chat_id"],
                        custom_prefix=f"⏳ انتهت مدة مشاهدة <b>صفحة {task['page_num']}</b> وتم حذف الفيديوهات.\n\n"
                    )
                except Exception:
                    pass

                with DBContext() as cur:
                    cur.execute("DELETE FROM pending_deletes WHERE id=%s", (task["id"],))
        except Exception:
            pass
        time.sleep(2.0)


# --- إرسال صفحة الفيديوهات ---
def send_video_page_to_user(chat_id, page_num):
    videos = get_all_videos()
    delay = int(get_setting("delete_after", "60"))

    if not videos:
        send_pages_menu(chat_id, custom_prefix="📭 لا توجد فيديوهات مرفوعة حالياً.\n\n")
        return

    start_idx = (page_num - 1) * VIDEOS_PER_PAGE
    end_idx = start_idx + VIDEOS_PER_PAGE
    page_videos = videos[start_idx:end_idx]

    if not page_videos:
        send_pages_menu(chat_id, custom_prefix="⚠️ هذه الصفحة غير متوفرة.\n\n")
        return

    sent_ids = []
    header_msg = safe_api_call(
        bot.send_message,
        chat_id,
        f"📂 <b>عرض فيديوهات صفحة ({page_num})</b>\n⚠️ ستختفي الفيديوهات تلقائياً بعد <b>{delay} ثانية</b>.",
        parse_mode="HTML"
    )
    if header_msg:
        sent_ids.append(header_msg.message_id)

    for idx, vid in enumerate(page_videos, start=start_idx + 1):
        try:
            msg = safe_api_call(
                bot.send_video,
                chat_id,
                vid["file_id"],
                caption=vid["caption"] or f"🎬 فيديو رقم {idx}",
                protect_content=True
            )
            if msg:
                sent_ids.append(msg.message_id)
            time.sleep(0.15)
        except Exception:
            pass

    if sent_ids:
        enqueue_delete(chat_id, sent_ids, delay, page_num)


# --- أزرار لوحة الأدمن ---
def admin_reply_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        types.KeyboardButton("📢 إذاعة للكل"),
        types.KeyboardButton("📊 الإحصائيات"),
        types.KeyboardButton("✅ فك حظر"),
        types.KeyboardButton("🚫 حظر"),
        types.KeyboardButton("💬 رسالة لمستخدم"),
        types.KeyboardButton("🔎 معلومات مستخدم"),
        types.KeyboardButton("📌 الاشتراك الإجباري"),
        types.KeyboardButton("📄 تصدير المستخدمين"),
        types.KeyboardButton("👮‍♂️ الأدمنية"),
        types.KeyboardButton("⚙️ الإعدادات"),
        types.KeyboardButton("🎬 رفع فيديو"),
        types.KeyboardButton("🗑 حذف فيديوهات"),
    )
    kb.add(types.KeyboardButton("🏠 الرئيسية (عرض الصفحات)"))
    return kb


def cancel_reply_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton("🔙 إلغاء والعودة للوحة"))
    return kb


# --- الأوامر الأساسية ---
@bot.message_handler(commands=["start", "admin"])
def start_and_admin_cmd(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    bot.clear_step_handler_by_chat_id(chat_id)

    if is_admin(user_id):
        safe_api_call(
            bot.send_message,
            chat_id,
            "⚙️ <b>أهلاً بك في لوحة الأدمن (Cloud DB)!</b>\nالأزرار جاهزة بالأسفل للتحكم الكامل:",
            parse_mode="HTML",
            reply_markup=admin_reply_keyboard()
        )
        return

    if is_banned(user_id):
        return

    with get_user_lock(user_id):
        if check_rate_limit(user_id):
            return

        register_user(user_id)

        if get_setting("bot_active", "1") != "1":
            safe_api_call(bot.send_message, chat_id, "🛠 البوت في وضع الصيانة حالياً، يرجى المحاولة لاحقاً.")
            return

        ok, missing, _ = check_sub(user_id, bypass_cache=True)
        if not ok:
            send_force_sub_message(chat_id, missing)
            return

        send_pages_menu(chat_id)


# --- أزرار المستخدم الشفافة ---
@bot.callback_query_handler(func=lambda c: c.data in ["user_check_sub", "user_send_video", "user_cancel_send"] or c.data.startswith("vpage_"))
def user_inline_callbacks(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id

    if is_banned(user_id):
        return

    if get_setting("bot_active", "1") != "1" and not is_admin(user_id):
        bot.answer_callback_query(call.id, "🛠 البوت في وضع الصيانة حالياً.", show_alert=True)
        return

    lock = get_user_lock(user_id)
    if not lock.acquire(blocking=False):
        bot.answer_callback_query(call.id, "⏳ جاري تنفيذ طلبك السابق، انتظر لحظة...", show_alert=False)
        return

    try:
        if call.data != "user_cancel_send" and check_rate_limit(user_id):
            bot.answer_callback_query(call.id, f"⏳ يرجى الانتظار {USER_COOLDOWN} ثوانٍ بين كل طلب!", show_alert=True)
            return

        bypass = (call.data == "user_check_sub")
        ok, missing, _ = check_sub(user_id, bypass_cache=bypass)
        if not ok:
            bot.answer_callback_query(call.id, "❌ يجب الاشتراك في جميع القنوات أولاً!", show_alert=True)
            try:
                bot.delete_message(chat_id, call.message.message_id)
            except Exception:
                pass
            send_force_sub_message(chat_id, missing)
            return

        if call.data == "user_check_sub":
            bot.answer_callback_query(call.id, "✅ تم التحقق من اشتراكك بنجاح!")
            try:
                bot.delete_message(chat_id, call.message.message_id)
            except Exception:
                pass
            send_pages_menu(chat_id)

        elif call.data.startswith("vpage_"):
            page_num = int(call.data.split("_")[1])
            bot.answer_callback_query(call.id, f"🎬 جاري فتح صفحة {page_num}...")
            try:
                bot.delete_message(chat_id, call.message.message_id)
            except Exception:
                pass
            send_video_page_to_user(chat_id, page_num)

        elif call.data == "user_send_video":
            bot.answer_callback_query(call.id)
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("❌ إلغاء والرجوع للقائمة", callback_data="user_cancel_send"))
            msg = safe_api_call(
                bot.edit_message_text,
                "📤 <b>إرسال فيديو أو ملف للمشرف:</b>\n\nأرسل الآن <b>فيديو</b> أو <b>ملف</b> (مثل ZIP) لإرساله للمشرف:",
                chat_id,
                call.message.message_id,
                parse_mode="HTML",
                reply_markup=kb
            )
            if msg:
                bot.register_next_step_handler(msg, step_user_submit_media)

        elif call.data == "user_cancel_send":
            bot.answer_callback_query(call.id, "تم الإلغاء.")
            bot.clear_step_handler_by_chat_id(chat_id)
            try:
                bot.delete_message(chat_id, call.message.message_id)
            except Exception:
                pass
            send_pages_menu(chat_id)
    finally:
        lock.release()


# --- استقبال الفيديوهات والملفات من المستخدم ---
def step_user_submit_media(message):
    user_id = message.from_user.id
    chat_id = message.chat.id

    if is_banned(user_id):
        return

    if message.text and message.text.startswith("/"):
        bot.clear_step_handler_by_chat_id(chat_id)
        start_and_admin_cmd(message)
        return

    if message.content_type not in ["video", "document"]:
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("❌ إلغاء والرجوع للقائمة", callback_data="user_cancel_send"))
        msg = safe_api_call(
            bot.send_message,
            chat_id,
            "⚠️ <b>مسموح بإرسال الفيديوهات أو الملفات (مثل ZIP) فقط!</b>\nأرسل فيديو أو ملف، أو اضغط إلغاء:",
            parse_mode="HTML",
            reply_markup=kb
        )
        if msg:
            bot.register_next_step_handler(msg, step_user_submit_media)
        return

    sender_name = html.escape(message.from_user.first_name or "بدون اسم")
    sender_user = f"@{html.escape(message.from_user.username)}" if message.from_user.username else "لا يوجد"
    item_caption = html.escape(message.caption or "بدون وصف")

    try:
        if message.content_type == "video":
            admin_kb = types.InlineKeyboardMarkup(row_width=2)
            admin_kb.add(
                types.InlineKeyboardButton("✅ نشر الفيديو بالبوت", callback_data=f"adm_approve_vid_{user_id}"),
                types.InlineKeyboardButton("❌ تجاهل / حذف", callback_data="adm_reject_vid")
            )
            safe_api_call(
                bot.send_video,
                ADMIN_ID,
                message.video.file_id,
                caption=(
                    f"📥 <b>فيديو جديد مرسل من مستخدم!</b>\n\n"
                    f"👤 الاسم: {sender_name}\n"
                    f"🔗 اليوزر: {sender_user}\n"
                    f"🆔 الآيدي: <code>{user_id}</code>\n"
                    f"📝 الوصف: {item_caption}"
                ),
                parse_mode="HTML",
                reply_markup=admin_kb
            )
        elif message.content_type == "document":
            file_name = html.escape(message.document.file_name or "ملف")
            admin_kb = types.InlineKeyboardMarkup()
            admin_kb.add(types.InlineKeyboardButton("❌ حذف الرسالة", callback_data="adm_reject_vid"))
            safe_api_call(
                bot.send_document,
                ADMIN_ID,
                message.document.file_id,
                caption=(
                    f"📦 <b>ملف جديد مرسل من مستخدم!</b>\n\n"
                    f"📄 اسم الملف: <code>{file_name}</code>\n"
                    f"👤 الاسم: {sender_name}\n"
                    f"🔗 اليوزر: {sender_user}\n"
                    f"🆔 الآيدي: <code>{user_id}</code>\n"
                    f"📝 الوصف: {item_caption}"
                ),
                parse_mode="HTML",
                reply_markup=admin_kb
            )
    except Exception as e:
        print(f"[Admin Forward Error]: {e}")

    send_pages_menu(chat_id, custom_prefix="✅ <b>تم إرسال ملفك إلى المشرف بنجاح!</b>\n\n")


# --- معالجة أزرار الأدمن ---
@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.content_type == "text")
def handle_admin_buttons(message):
    text = message.text.strip()
    chat_id = message.chat.id
    bot.clear_step_handler_by_chat_id(chat_id)

    if text == "🔙 إلغاء والعودة للوحة":
        bot.send_message(chat_id, "⚙️ تم الرجوع للوحة الأدمن:", reply_markup=admin_reply_keyboard())

    elif text == "📊 الإحصائيات":
        with DBContext() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM users")
            u_count = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM users WHERE is_banned=1")
            b_count = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM users WHERE is_admin=1")
            a_count = cur.fetchone()["c"] + 1
            cur.execute("SELECT COUNT(*) AS c FROM videos")
            v_count = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM force_sub")
            f_count = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM pending_deletes")
            p_count = cur.fetchone()["c"]

        status_txt = "يعمل ✅" if get_setting("bot_active", "1") == "1" else "متوقف (صيانة) 🔴"
        total_p = math.ceil(v_count / VIDEOS_PER_PAGE) if v_count else 0
        msg = (
            f"📊 <b>إحصائيات البوت (Cloud PostgreSQL):</b>\n\n"
            f"👥 إجمالي المستخدمين: <code>{u_count}</code>\n"
            f"🎬 الفيديوهات المحفوظة: <code>{v_count}</code> (<code>{total_p}</code> صفحات)\n"
            f"⏳ مهام الحذف النشطة حالياً: <code>{p_count}</code>\n"
            f"⏱ مدة الاختفاء: <code>{get_setting('delete_after', '60')}</code> ثانية\n"
            f"🚫 المحظورين: <code>{b_count}</code>\n"
            f"👮‍♂️ عدد الأدمنية: <code>{a_count}</code>\n"
            f"📌 قنوات الاشتراك: <code>{f_count}</code>\n"
            f"📡 حالة البوت: {status_txt}"
        )
        bot.send_message(chat_id, msg, parse_mode="HTML", reply_markup=admin_reply_keyboard())

    elif text == "🎬 رفع فيديو":
        v_count = len(get_all_videos())
        msg = bot.send_message(
            chat_id,
            f"🎬 أرسل الآن <b>الفيديو</b> لحفظه في السحابة (الحالي: <code>{v_count}</code>):",
            parse_mode="HTML",
            reply_markup=cancel_reply_keyboard()
        )
        bot.register_next_step_handler(msg, step_add_video)

    elif text == "🗑 حذف فيديوهات":
        videos = get_all_videos()
        if not videos:
            bot.send_message(chat_id, "📭 لا توجد فيديوهات مرفوعة لحذفها.", reply_markup=admin_reply_keyboard())
            return
        kb = types.InlineKeyboardMarkup(row_width=3)
        btns = [
            types.InlineKeyboardButton(f"🗑 #{idx + 1}", callback_data=f"adm_del_id_{v['id']}")
            for idx, v in enumerate(videos)
        ]
        kb.add(*btns)
        kb.add(types.InlineKeyboardButton("🔥 مسح جميع الفيديوهات", callback_data="adm_clear_all_videos"))
        bot.send_message(chat_id, f"🗑 <b>إدارة حذف الفيديوهات ({len(videos)}):</b>", parse_mode="HTML", reply_markup=kb)

    elif text == "📢 إذاعة للكل":
        msg = bot.send_message(chat_id, "📢 أرسل الآن الرسالة التي تريد إذاعتها لجميع المستخدمين:", reply_markup=cancel_reply_keyboard())
        bot.register_next_step_handler(msg, step_broadcast)

    elif text == "🚫 حظر":
        msg = bot.send_message(chat_id, "🚫 أرسل آيدي (ID) المستخدم المراد حظره:", reply_markup=cancel_reply_keyboard())
        bot.register_next_step_handler(msg, step_ban)

    elif text == "✅ فك حظر":
        msg = bot.send_message(chat_id, "✅ أرسل آيدي (ID) المستخدم المراد فك حظره:", reply_markup=cancel_reply_keyboard())
        bot.register_next_step_handler(msg, step_unban)

    elif text == "🔎 معلومات مستخدم":
        msg = bot.send_message(chat_id, "🔎 أرسل آيدي (ID) المستخدم لعرض بياناته:", reply_markup=cancel_reply_keyboard())
        bot.register_next_step_handler(msg, step_user_info)

    elif text == "💬 رسالة لمستخدم":
        msg = bot.send_message(chat_id, "💬 أرسل آيدي (ID) المستخدم الذي تريد مراسلته:", reply_markup=cancel_reply_keyboard())
        bot.register_next_step_handler(msg, step_get_target_user)

    elif text == "📄 تصدير المستخدمين":
        with DBContext() as cur:
            cur.execute("SELECT user_id FROM users")
            rows = cur.fetchall()
        if not rows:
            bot.send_message(chat_id, "❌ لا يوجد مستخدمين لتصديرهم حالياً.", reply_markup=admin_reply_keyboard())
            return
        file_content = "\n".join(str(r["user_id"]) for r in rows)
        bio = io.BytesIO(file_content.encode("utf-8"))
        bio.name = f"users_{len(rows)}.txt"
        bot.send_document(chat_id, bio, caption=f"📄 تم تصدير {len(rows)} مستخدم بنجاح.", reply_markup=admin_reply_keyboard())

    elif text == "📌 الاشتراك الإجباري":
        fsubs = get_all_force_sub()
        if fsubs:
            subs = "\n".join(f"• {html.escape(c['title'])} (<code>{c['chat_id']}</code>)" for c in fsubs)
        else:
            subs = "لا يوجد قنوات مضافة."
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("➕ إضافة قناة (عامة/خاصة)", callback_data="adm_fsub_add"),
            types.InlineKeyboardButton("🗑 حذف قناة", callback_data="adm_fsub_del"),
        )
        kb.add(types.InlineKeyboardButton("🧪 تجربة الاشتراك الإجباري", callback_data="adm_fsub_test"))
        bot.send_message(
            chat_id,
            f"📌 <b>إدارة الاشتراك الإجباري ({len(fsubs)} قنوات):</b>\n\n{subs}",
            parse_mode="HTML",
            reply_markup=kb
        )

    elif text == "👮‍♂️ الأدمنية":
        with DBContext() as cur:
            cur.execute("SELECT user_id FROM users WHERE is_admin=1")
            admins = cur.fetchall()
        admins_list = "\n".join(str(a["user_id"]) for a in admins) if admins else "لا يوجد أدمنية إضافيين."
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("➕ رفع أدمن", callback_data="adm_admin_add"),
            types.InlineKeyboardButton("➖ تنزيل أدمن", callback_data="adm_admin_del"),
        )
        bot.send_message(
            chat_id,
            f"👮‍♂️ <b>إدارة الأدمنية:</b>\n\nالمالك الأساسي: <code>{ADMIN_ID}</code>\nالإضافيين:\n{admins_list}",
            parse_mode="HTML",
            reply_markup=kb
        )

    elif text == "⚙️ الإعدادات":
        is_act = get_setting("bot_active", "1") == "1"
        state_btn = "🔴 إيقاف البوت (صيانة)" if is_act else "🟢 تشغيل البوت"
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton(f"⏱ مدة الاختفاء ({get_setting('delete_after', '60')} ث)", callback_data="adm_set_timer"),
            types.InlineKeyboardButton(state_btn, callback_data="adm_toggle_bot"),
            types.InlineKeyboardButton("✏️ تغيير رسالة الصفحات", callback_data="adm_set_welcome"),
        )
        bot.send_message(chat_id, "⚙️ <b>إعدادات البوت العامة:</b>", parse_mode="HTML", reply_markup=kb)

    elif text == "🏠 الرئيسية (عرض الصفحات)":
        send_pages_menu(chat_id)


# --- الأزرار الفرعية للأدمن ---
@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_"))
def admin_inline_callbacks(call):
    if not is_admin(call.from_user.id):
        return

    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    if call.data.startswith("adm_approve_vid_"):
        sender_id = int(call.data.split("_")[-1])
        if call.message.video:
            with DBContext() as cur:
                cur.execute("INSERT INTO videos (file_id, caption) VALUES (%s, %s)", (call.message.video.file_id, ""))
                cur.execute("SELECT COUNT(*) AS c FROM videos")
                total_v = cur.fetchone()["c"]
            bot.answer_callback_query(call.id, "✅ تم نشر الفيديو في السحابة!", show_alert=True)
            bot.edit_message_caption(
                f"✅ <b>تم قبول ونشر هذا الفيديو!</b> (الإجمالي: <code>{total_v}</code>)",
                chat_id,
                msg_id,
                parse_mode="HTML"
            )
            try:
                safe_api_call(bot.send_message, sender_id, "🎉 تمت الموافقة على الفيديو الذي أرسلته ونشره في البوت!")
            except Exception:
                pass
        return

    elif call.data == "adm_reject_vid":
        bot.answer_callback_query(call.id, "🗑 تم الحذف.")
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
        return

    bot.answer_callback_query(call.id)

    if call.data.startswith("adm_del_id_"):
        vid_db_id = int(call.data.split("_")[-1])
        with DBContext() as cur:
            cur.execute("DELETE FROM videos WHERE id=%s", (vid_db_id,))

        videos = get_all_videos()
        if not videos:
            bot.edit_message_text("📭 تم حذف جميع الفيديوهات.", chat_id, msg_id)
            return
        kb = types.InlineKeyboardMarkup(row_width=3)
        btns = [
            types.InlineKeyboardButton(f"🗑 #{idx + 1}", callback_data=f"adm_del_id_{v['id']}")
            for idx, v in enumerate(videos)
        ]
        kb.add(*btns)
        kb.add(types.InlineKeyboardButton("🔥 مسح جميع الفيديوهات", callback_data="adm_clear_all_videos"))
        bot.edit_message_text(f"🗑 <b>المتبقي ({len(videos)}):</b>", chat_id, msg_id, parse_mode="HTML", reply_markup=kb)

    elif call.data == "adm_clear_all_videos":
        with DBContext() as cur:
            cur.execute("DELETE FROM videos")
        bot.edit_message_text("🔥 تم مسح جميع الفيديوهات بنجاح.", chat_id, msg_id)

    elif call.data == "adm_fsub_add":
        msg = bot.send_message(
            chat_id,
            "➕ أرسل معرف القناة العامة (<code>@MyChannel</code>) أو آيدي القناة الخاصة (<code>-100...</code>):",
            parse_mode="HTML",
            reply_markup=cancel_reply_keyboard()
        )
        bot.register_next_step_handler(msg, step_fsub_add_id)

    elif call.data == "adm_fsub_del":
        fsubs = get_all_force_sub()
        if not fsubs:
            bot.send_message(chat_id, "⚠️ لا توجد قنوات لحذفها.")
            return
        kb = types.InlineKeyboardMarkup(row_width=1)
        for c in fsubs:
            kb.add(types.InlineKeyboardButton(f"🗑 حذف: {c['title']}", callback_data=f"adm_fsub_rm_{c['chat_id']}"))
        bot.send_message(chat_id, "🗑 اختر القناة لحذفها:", reply_markup=kb)

    elif call.data.startswith("adm_fsub_rm_"):
        target_cid = call.data.replace("adm_fsub_rm_", "")
        with DBContext() as cur:
            cur.execute("DELETE FROM force_sub WHERE chat_id=%s", (target_cid,))
        sub_cache.clear()
        bot.edit_message_text("✅ تم حذف القناة من الاشتراك الإجباري.", chat_id, msg_id)

    elif call.data == "adm_fsub_test":
        fsubs = get_all_force_sub()
        if not fsubs:
            bot.send_message(chat_id, "⚠️ لم تقم بإضافة أي قنوات بعد!")
            return
        _, _, err = check_sub(call.from_user.id, force_test=True)
        send_force_sub_message(chat_id, fsubs, err)

    elif call.data == "adm_admin_add":
        if int(call.from_user.id) != int(ADMIN_ID):
            return
        msg = bot.send_message(chat_id, "➕ أرسل آيدي الشخص لرفعه أدمن:", reply_markup=cancel_reply_keyboard())
        bot.register_next_step_handler(msg, step_admin_add)

    elif call.data == "adm_admin_del":
        if int(call.from_user.id) != int(ADMIN_ID):
            return
        msg = bot.send_message(chat_id, "➖ أرسل آيدي الأدمن لتنزيله:", reply_markup=cancel_reply_keyboard())
        bot.register_next_step_handler(msg, step_admin_del)

    elif call.data == "adm_set_timer":
        msg = bot.send_message(
            chat_id,
            f"⏱ أرسل مدة بقاء الفيديوهات <b>بالثواني</b> (الحالي: <code>{get_setting('delete_after', '60')}</code> ث):",
            parse_mode="HTML",
            reply_markup=cancel_reply_keyboard()
        )
        bot.register_next_step_handler(msg, step_set_timer)

    elif call.data == "adm_toggle_bot":
        curr = get_setting("bot_active", "1") == "1"
        new_val = "0" if curr else "1"
        set_setting("bot_active", new_val)
        state_btn = "🔴 إيقاف البوت (صيانة)" if new_val == "1" else "🟢 تشغيل البوت"
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton(f"⏱ مدة الاختفاء ({get_setting('delete_after', '60')} ث)", callback_data="adm_set_timer"),
            types.InlineKeyboardButton(state_btn, callback_data="adm_toggle_bot"),
            types.InlineKeyboardButton("✏️ تغيير رسالة الصفحات", callback_data="adm_set_welcome"),
        )
        bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=kb)

    elif call.data == "adm_set_welcome":
        msg = bot.send_message(
            chat_id,
            f"✏️ أرسل الرسالة الجديدة:\n\nالحالية:\n<code>{html.escape(get_setting('welcome_msg', '👋 أهلاً بك!'))}</code>",
            parse_mode="HTML",
            reply_markup=cancel_reply_keyboard()
        )
        bot.register_next_step_handler(msg, step_set_welcome)


# --- دوال الخطوات للأدمن ---
def step_add_video(message):
    if not is_admin(message.from_user.id) or check_interrupt(message):
        return
    if message.content_type != "video":
        msg = bot.send_message(message.chat.id, "❌ يرجى إرسال <b>فيديو</b> فقط:", parse_mode="HTML", reply_markup=cancel_reply_keyboard())
        bot.register_next_step_handler(msg, step_add_video)
        return

    with DBContext() as cur:
        cur.execute("INSERT INTO videos (file_id, caption) VALUES (%s, %s)", (message.video.file_id, message.caption or ""))
        cur.execute("SELECT COUNT(*) AS c FROM videos")
        total_v = cur.fetchone()["c"]

    msg = bot.send_message(
        message.chat.id,
        f"✅ تم الحفظ في السحابة! (الإجمالي: <code>{total_v}</code>)\nأرسل فيديو آخر أو اضغط إلغاء:",
        parse_mode="HTML",
        reply_markup=cancel_reply_keyboard()
    )
    bot.register_next_step_handler(msg, step_add_video)


def step_set_timer(message):
    if not is_admin(message.from_user.id) or check_interrupt(message):
        return
    if not message.text or not message.text.isdigit() or int(message.text) < 5:
        bot.send_message(message.chat.id, "❌ أدخل رقماً صحيحاً (5 ثوانٍ فأكثر).", reply_markup=admin_reply_keyboard())
        return
    set_setting("delete_after", int(message.text))
    bot.send_message(message.chat.id, f"✅ تم الضبط على <b>{message.text} ثانية</b>.", parse_mode="HTML", reply_markup=admin_reply_keyboard())


def step_broadcast(message):
    if not is_admin(message.from_user.id) or check_interrupt(message):
        return

    def _run_broadcast():
        with DBContext() as cur:
            cur.execute("SELECT user_id FROM users WHERE is_banned=0")
            rows = cur.fetchall()
        sent, failed = 0, 0
        status_msg = bot.send_message(message.chat.id, f"⏳ جاري الإرسال إلى {len(rows)} مستخدم في الخلفية...")
        for r in rows:
            try:
                safe_api_call(bot.copy_message, r["user_id"], message.chat.id, message.message_id)
                sent += 1
                time.sleep(0.04)
            except Exception:
                failed += 1
        try:
            bot.delete_message(message.chat.id, status_msg.message_id)
        except Exception:
            pass
        bot.send_message(message.chat.id, f"✅ اكتملت الإذاعة!\n🟢 نجح: {sent} | 🔴 فشل: {failed}", reply_markup=admin_reply_keyboard())

    threading.Thread(target=_run_broadcast, daemon=True).start()


def step_ban(message):
    if not is_admin(message.from_user.id) or check_interrupt(message):
        return
    if not message.text or not message.text.isdigit():
        bot.send_message(message.chat.id, "❌ أرسل آيدي رقمي صحيح.", reply_markup=admin_reply_keyboard())
        return
    uid = int(message.text)
    if uid == int(ADMIN_ID):
        bot.send_message(message.chat.id, "❌ لا يمكنك حظر المالك.", reply_markup=admin_reply_keyboard())
        return
    with DBContext() as cur:
        cur.execute(
            "INSERT INTO users (user_id, is_banned, joined_at) VALUES (%s, 1, %s) ON CONFLICT (user_id) DO UPDATE SET is_banned=1",
            (uid, time.time())
        )
    bot.send_message(message.chat.id, f"🚫 تم حظر {uid}.", reply_markup=admin_reply_keyboard())


def step_unban(message):
    if not is_admin(message.from_user.id) or check_interrupt(message):
        return
    if not message.text or not message.text.isdigit():
        bot.send_message(message.chat.id, "❌ أرسل آيدي رقمي صحيح.", reply_markup=admin_reply_keyboard())
        return
    uid = int(message.text)
    with DBContext() as cur:
        cur.execute("UPDATE users SET is_banned=0 WHERE user_id=%s", (uid,))
    bot.send_message(message.chat.id, f"✅ تم فك الحظر عن {uid}.", reply_markup=admin_reply_keyboard())


def step_user_info(message):
    if not is_admin(message.from_user.id) or check_interrupt(message):
        return
    if not message.text or not message.text.isdigit():
        bot.send_message(message.chat.id, "❌ أرسل آيدي رقمي صحيح.", reply_markup=admin_reply_keyboard())
        return
    uid = int(message.text)
    with DBContext() as cur:
        cur.execute("SELECT is_banned FROM users WHERE user_id=%s", (uid,))
        row = cur.fetchone()
    in_db = "نعم ✅" if row else "لا ❌"
    banned_txt = "نعم 🚫" if (row and row["is_banned"] == 1) else "لا ✅"
    try:
        chat = bot.get_chat(uid)
        name = html.escape(chat.first_name or "غير متوفر")
        username = f"@{html.escape(chat.username)}" if chat.username else "لا يوجد"
    except Exception:
        name = "غير معروف"
        username = "غير متوفر"
    text = f"🔎 <b>معلومات:</b>\n🆔 <code>{uid}</code>\n👤 {name}\n🔗 {username}\n📂 مسجل: {in_db}\n🚫 محظور: {banned_txt}"
    bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=admin_reply_keyboard())


def step_get_target_user(message):
    if not is_admin(message.from_user.id) or check_interrupt(message):
        return
    if not message.text or not message.text.isdigit():
        bot.send_message(message.chat.id, "❌ أرسل آيدي رقمي صحيح.", reply_markup=admin_reply_keyboard())
        return
    target_id = int(message.text)
    msg = bot.send_message(message.chat.id, f"✍️ أرسل الرسالة إلى <code>{target_id}</code>:", parse_mode="HTML", reply_markup=cancel_reply_keyboard())
    bot.register_next_step_handler(msg, step_send_to_user, target_id)


def step_send_to_user(message, target_id):
    if not is_admin(message.from_user.id) or check_interrupt(message):
        return
    try:
        safe_api_call(bot.copy_message, target_id, message.chat.id, message.message_id)
        bot.send_message(message.chat.id, "✅ تم الإرسال.", reply_markup=admin_reply_keyboard())
    except Exception:
        bot.send_message(message.chat.id, "❌ فشل الإرسال.", reply_markup=admin_reply_keyboard())


def step_fsub_add_id(message):
    if not is_admin(message.from_user.id) or check_interrupt(message) or not message.text:
        return
    raw = message.text.strip()

    if raw.startswith("-100") and raw[1:].isdigit():
        target_chat = int(raw)
        try:
            chat_info = bot.get_chat(target_chat)
            bot_member = bot.get_chat_member(target_chat, bot.get_me().id)
            if bot_member.status not in ["administrator", "creator"]:
                bot.send_message(message.chat.id, "⚠️ البوت ليس مشرفاً في القناة الخاصة!", reply_markup=admin_reply_keyboard())
                return
            ch_title = chat_info.title or "قناة خاصة"
        except Exception:
            bot.send_message(message.chat.id, "❌ تعذر الوصول للقناة الخاصة!", reply_markup=admin_reply_keyboard())
            return

        msg = bot.send_message(
            message.chat.id,
            f"✅ تم التعرف على: <b>{html.escape(ch_title)}</b>\n🔗 أرسل رابط الدعوة الخاص بالقناة:",
            parse_mode="HTML",
            reply_markup=cancel_reply_keyboard()
        )
        bot.register_next_step_handler(msg, step_fsub_add_private_url, str(target_chat), ch_title)
        return

    ch = raw.replace("https://t.me/", "@").replace("http://t.me/", "@")
    if not ch.startswith("@"):
        ch = "@" + ch

    try:
        chat_info = bot.get_chat(ch)
        bot_member = bot.get_chat_member(ch, bot.get_me().id)
        if bot_member.status not in ["administrator", "creator"]:
            bot.send_message(message.chat.id, f"⚠️ البوت ليس مشرفاً في {ch}!", reply_markup=admin_reply_keyboard())
            return
        ch_title = chat_info.title or ch
    except Exception:
        bot.send_message(message.chat.id, f"❌ تعذر الوصول للقناة {ch}!", reply_markup=admin_reply_keyboard())
        return

    with DBContext() as cur:
        cur.execute(
            "INSERT INTO force_sub (chat_id, url, title) VALUES (%s, %s, %s) ON CONFLICT (chat_id) DO UPDATE SET url=EXCLUDED.url, title=EXCLUDED.title",
            (ch, f"https://t.me/{ch.replace('@', '')}", ch_title)
        )
    sub_cache.clear()
    bot.send_message(message.chat.id, f"✅ تم إضافة <b>{html.escape(ch_title)}</b> بنجاح!", parse_mode="HTML", reply_markup=admin_reply_keyboard())


def step_fsub_add_private_url(message, target_chat, ch_title):
    if not is_admin(message.from_user.id) or check_interrupt(message):
        return
    if not message.text or not message.text.strip().startswith("http"):
        msg = bot.send_message(message.chat.id, "❌ أرسل رابط دعوة يبدأ بـ https://t.me/ :", reply_markup=cancel_reply_keyboard())
        bot.register_next_step_handler(msg, step_fsub_add_private_url, target_chat, ch_title)
        return

    with DBContext() as cur:
        cur.execute(
            "INSERT INTO force_sub (chat_id, url, title) VALUES (%s, %s, %s) ON CONFLICT (chat_id) DO UPDATE SET url=EXCLUDED.url, title=EXCLUDED.title",
            (target_chat, message.text.strip(), ch_title)
        )
    sub_cache.clear()
    bot.send_message(message.chat.id, f"✅ تم إضافة القناة الخاصة <b>{html.escape(ch_title)}</b> بنجاح!", parse_mode="HTML", reply_markup=admin_reply_keyboard())


def step_admin_add(message):
    if int(message.from_user.id) != int(ADMIN_ID) or check_interrupt(message):
        return
    if not message.text or not message.text.isdigit():
        return
    uid = int(message.text)
    with DBContext() as cur:
        cur.execute(
            "INSERT INTO users (user_id, is_admin, joined_at) VALUES (%s, 1, %s) ON CONFLICT (user_id) DO UPDATE SET is_admin=1",
            (uid, time.time())
        )
    bot.send_message(message.chat.id, f"✅ تم رفع {uid} أدمن.", reply_markup=admin_reply_keyboard())


def step_admin_del(message):
    if int(message.from_user.id) != int(ADMIN_ID) or check_interrupt(message):
        return
    if not message.text or not message.text.isdigit():
        return
    uid = int(message.text)
    with DBContext() as cur:
        cur.execute("UPDATE users SET is_admin=0 WHERE user_id=%s", (uid,))
    bot.send_message(message.chat.id, f"➖ تم تنزيل {uid}.", reply_markup=admin_reply_keyboard())


def step_set_welcome(message):
    if not is_admin(message.from_user.id) or check_interrupt(message) or not message.text:
        return
    set_setting("welcome_msg", message.text)
    bot.send_message(message.chat.id, "✅ تم تحديث الرسالة.", reply_markup=admin_reply_keyboard())


if __name__ == "__main__":
    keep_alive()
    init_db()
    threading.Thread(target=background_cleaner_worker, daemon=True).start()
    print("Cloud PostgreSQL High-Concurrency Bot is running...")
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=60, skip_pending=True)
        except Exception as e:
            print(f"[Polling Reconnect]: {e}")
            time.sleep(3)
