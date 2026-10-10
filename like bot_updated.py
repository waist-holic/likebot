 
import os
import shutil
import time
import threading
import sqlite3
import html
from datetime import datetime, timedelta

import pytz
import requests
import telebot
from telebot import types

# ================= CONFIGURATION =================
# SECURITY: keep secrets in environment variables instead of hard-coding them.
BOT_TOKEN = os.getenv("BOT_TOKEN", "8730313708:AAFnv7TwmivuGVdWcZP213eWCxkCJD4zns0")
OWNER_IDS = [7210827441]

# Groups where normal users may use the bot.
DEFAULT_ALLOWED_GROUPS = [-1004312733221, -1004471462195, -1004483377381]

# Each command has its own independent API slot.
# Put any provider key in the complete URL if the provider requires one.
LIKE_API_URL = os.getenv(
    "LIKE_API_URL",
    "https://jwtlikeapi.vercel.app/like?uid={uid}&server_name={region}&key=HOLIC",
)
LIKEL_API_URL = os.getenv(
    "LIKEL_API_URL",
    "https://jwtlikeapi.vercel.app/like?uid={uid}&server_name={region}&key=HOLIC",
)
V1_LIKE_API_URL = os.getenv(
    "V1_LIKE_API_URL",
    "https://jwtlikeapi.vercel.app/like?uid={uid}&server_name={region}&key=HOLIC",
)
V2_LIKE_API_URL = os.getenv(
    "V2_LIKE_API_URL",
    "https://jwtlikeapi.vercel.app/like?uid={uid}&server_name={region}&key=HOLIC",
)

# Separate API used ONLY by the AutoLike scheduler.
AUTO_LIKE_API_URL = os.getenv(
    "AUTO_LIKE_API_URL",
    "https://jwtlikeapi.vercel.app/like?uid={uid}&server_name={region}&key=HOLIC",
)
V1_AUTO_LIKE_API_URL = os.getenv(
    "V1_AUTO_LIKE_API_URL",
    "https://jwtlikeapi.vercel.app/like?uid={uid}&server_name={region}&key=HOLIC",
)
V2_AUTO_LIKE_API_URL = os.getenv(
    "V2_AUTO_LIKE_API_URL",
    "https://jwtlikeapi.vercel.app/like?uid={uid}&server_name={region}&key=HOLIC",
)

# Separate Free Fire player-info and visit APIs. Set these to your providers.
INFO_API_URL = os.getenv("INFO_API_URL", "")
VISIT_API_URL = os.getenv("VISIT_API_URL", "")
RESULT_PHOTO_URL = os.getenv("RESULT_PHOTO_URL", "https://holic17.wordpress.com/wp-content/uploads/2026/09/photo_2026-09-17_10-54-27.jpg")

IST = pytz.timezone("Asia/Kolkata")
DEFAULT_HOUR = 4
DEFAULT_MINUTE = 0
MAX_DAILY_LIMIT = 400
MAX_UID_SLOTS = 100000
LIKE_ENABLED = True  # Public /like defaults to ON; persisted setting is loaded after DB init.

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ================= DATABASE =================
db = sqlite3.connect("autolikes.db", check_same_thread=False)
db_lock = threading.RLock()


def q(sql, params=(), fetchone=False, fetchall=False, commit=False):
    with db_lock:
        cur = db.cursor()
        cur.execute(sql, params)
        if commit:
            db.commit()
        if fetchone:
            return cur.fetchone()
        if fetchall:
            return cur.fetchall()
        return None


def init_db():
    q("""CREATE TABLE IF NOT EXISTS autolikes (
        uid TEXT PRIMARY KEY,
        region TEXT NOT NULL,
        chat_id INTEGER NOT NULL,
        expire TEXT NOT NULL,
        hour INTEGER NOT NULL DEFAULT 4,
        minute INTEGER NOT NULL DEFAULT 0,
        added_date TEXT,
        requested_likes INTEGER NOT NULL DEFAULT 1,
        notify_user INTEGER,
        notify_enabled INTEGER NOT NULL DEFAULT 1,
        api_version TEXT NOT NULL DEFAULT 'auto'
    )""", commit=True)

    # Migrate older versions of the user's database.
    columns = {r[1] for r in q("PRAGMA table_info(autolikes)", fetchall=True)}
    migrations = {
        "added_date": "ALTER TABLE autolikes ADD COLUMN added_date TEXT",
        "requested_likes": "ALTER TABLE autolikes ADD COLUMN requested_likes INTEGER DEFAULT 1",
        "notify_user": "ALTER TABLE autolikes ADD COLUMN notify_user INTEGER",
        "notify_enabled": "ALTER TABLE autolikes ADD COLUMN notify_enabled INTEGER DEFAULT 1",
        "api_version": "ALTER TABLE autolikes ADD COLUMN api_version TEXT DEFAULT 'auto'",
    }
    for name, sql in migrations.items():
        if name not in columns:
            try:
                q(sql, commit=True)
            except sqlite3.OperationalError:
                pass

    q("""CREATE TABLE IF NOT EXISTS daily_usage (
        date TEXT PRIMARY KEY,
        count INTEGER NOT NULL DEFAULT 0
    )""", commit=True)

    q("""CREATE TABLE IF NOT EXISTS user_daily_usage (
        user_id INTEGER NOT NULL,
        date TEXT NOT NULL,
        count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(user_id, date)
    )""", commit=True)

    q("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )""", commit=True)

    q("""CREATE TABLE IF NOT EXISTS allowed_groups (
        group_id INTEGER PRIMARY KEY
    )""", commit=True)

    for gid in DEFAULT_ALLOWED_GROUPS:
        q("INSERT OR IGNORE INTO allowed_groups(group_id) VALUES (?)", (gid,), commit=True)

    q("""CREATE TABLE IF NOT EXISTS force_join (
        chat_id INTEGER PRIMARY KEY,
        title TEXT,
        invite_link TEXT
    )""", commit=True)


init_db()

# Keep public /like availability across bot restarts. If this is a fresh database,
# default to ON so users do not lose access after a restart/deploy.
_like_enabled_row = q("SELECT value FROM settings WHERE key='like_enabled'", fetchone=True)
if _like_enabled_row is None:
    q("INSERT OR IGNORE INTO settings(key, value) VALUES('like_enabled', '1')", commit=True)
    LIKE_ENABLED = True
else:
    LIKE_ENABLED = str(_like_enabled_row[0]).strip().lower() in ("1", "true", "yes", "on")

# Runtime cache to prevent duplicate scheduler execution in the same day.
last_run = {}


# ================= HELPERS =================
def setting(key, default=None):
    row = q("SELECT value FROM settings WHERE key=?", (key,), fetchone=True)
    return row[0] if row else default


def set_setting(key, value):
    q(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
        commit=True,
    )


def get_allowed_groups():
    return [r[0] for r in q("SELECT group_id FROM allowed_groups", fetchall=True)]


def is_owner(user_id):
    return user_id in OWNER_IDS


def is_admin(user_id):
    return is_owner(user_id) or user_id in get_admins()


def get_admins():
    rows = q("SELECT value FROM settings WHERE key='admins'", fetchone=True)
    if not rows or not rows[0]:
        return []
    try:
        return [int(x) for x in rows[0].split(",") if x.strip()]
    except ValueError:
        return []


def save_admins(admins):
    set_setting("admins", ",".join(str(x) for x in sorted(set(admins))))


def chat_allowed(msg):
    # Normal bot commands work ONLY in explicitly authorized groups.
    # Admin/owner status does not bypass the group whitelist.
    if msg.chat.type == "private":
        return False
    return msg.chat.id in get_allowed_groups()


def require_admin(msg):
    if not is_admin(msg.from_user.id):
        bot.reply_to(msg, "❌ Admin only.")
        return False

    # Admin commands are also blocked in unauthorized groups.
    # Private admin use remains allowed for management.
    if msg.chat.type != "private" and msg.chat.id not in get_allowed_groups():
        bot.reply_to(msg, "🚫 This bot is not authorized in this group.")
        return False

    return True


def require_owner(msg):
    if not is_owner(msg.from_user.id):
        bot.reply_to(msg, "❌ Owner only.")
        return False
    return True


def to_fancy_font(text):
    normal = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    fancy = "𝖺𝖻𝖼𝖽𝖾𝖿𝗀𝗁𝗂𝗃𝗄𝗅𝗆𝗇𝗈𝗉𝗊𝗋𝗌𝗍𝗎𝗏𝗐𝗑𝗒𝗓𝖠𝖡𝖢𝖣𝖤𝖥𝖦𝖧𝖨𝖩𝖪𝖫𝖬𝖭𝖮𝖯𝖰𝖱𝖲𝖳𝖴𝖵𝖶𝖷𝖸𝖹𝟢𝟣𝟤𝟥𝟦𝟧𝟨𝟩𝟪𝟫"
    return str(text).translate(str.maketrans(normal, fancy))


def get_global_daily_limit():
    try:
        return max(1, int(setting("global_daily_limit", MAX_DAILY_LIMIT)))
    except (TypeError, ValueError):
        return MAX_DAILY_LIMIT


def get_daily_usage():
    today = datetime.now(IST).strftime("%Y-%m-%d")
    row = q("SELECT count FROM daily_usage WHERE date=?", (today,), fetchone=True)
    if row:
        return int(row[0])
    q("INSERT OR IGNORE INTO daily_usage(date,count) VALUES(?,0)", (today,), commit=True)
    return 0


def update_usage(amount=1):
    today = datetime.now(IST).strftime("%Y-%m-%d")
    q(
        "INSERT INTO daily_usage(date,count) VALUES(?,?) "
        "ON CONFLICT(date) DO UPDATE SET count=count+excluded.count",
        (today, amount),
        commit=True,
    )


def get_user_daily_usage(user_id):
    today = datetime.now(IST).strftime("%Y-%m-%d")
    row = q(
        "SELECT count FROM user_daily_usage WHERE user_id=? AND date=?",
        (user_id, today),
        fetchone=True,
    )
    if row:
        return int(row[0])
    q(
        "INSERT OR IGNORE INTO user_daily_usage(user_id,date,count) VALUES(?,?,0)",
        (user_id, today),
        commit=True,
    )
    return 0


def update_user_usage(user_id, amount=1):
    today = datetime.now(IST).strftime("%Y-%m-%d")
    q(
        "INSERT INTO user_daily_usage(user_id,date,count) VALUES(?,?,?) "
        "ON CONFLICT(user_id,date) DO UPDATE SET count=count+excluded.count",
        (user_id, today, amount),
        commit=True,
    )


# ================= API ADAPTERS =================
def notify_owner_api_error(api_name, error, msg=None, uid=None, region=None):
    """Send detailed API failure information privately to the owner."""
    details = [f"⚠️ <b>{html.escape(str(api_name))} API ERROR</b>"]
    if uid:
        details.append(f"🆔 UID: <code>{html.escape(str(uid))}</code>")
    if region:
        details.append(f"🌍 Region: <code>{html.escape(str(region).upper())}</code>")
    if msg is not None:
        details.append(f"💬 Chat ID: <code>{msg.chat.id}</code>")
        details.append(f"👤 User ID: <code>{msg.from_user.id}</code>")
    details.append(f"❌ Error: <code>{html.escape(str(error))}</code>")
    for owner_id in OWNER_IDS:
        try:
            bot.send_message(owner_id, "\n".join(details))
        except Exception as notify_error:
            print("Owner API alert error:", notify_error)


def _get_json(url, params=None):
    """Call an external API without allowing a dead API to hang the bot.

    A separate connect/read timeout is used so /like, /v1like and /v2like
    always return a useful error instead of sitting on PROCESSING forever.
    """
    if not url or not str(url).strip():
        return None, "API is not configured."

    try:
        r = requests.get(
            url,
            params=params,
            timeout=(4, 12),  # 4s connect + 12s server/read timeout
            headers={"User-Agent": "HoLiC-LiKe-Bot/1.0"},
        )
        r.raise_for_status()
        try:
            data = r.json()
        except ValueError:
            preview = (r.text or "").strip().replace("\n", " ")[:180]
            return None, f"Invalid JSON response from API: {preview or 'empty response'}"
        if not isinstance(data, dict):
            return None, "Invalid API response: expected a JSON object."
        return data, None
    except requests.exceptions.Timeout:
        return None, "API timeout: the Like API did not respond within 12 seconds. Check the API URL/key with /api."
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "HTTP"
        return None, f"API HTTP error {status}. Check the API URL/key with /api."
    except requests.exceptions.RequestException as e:
        return None, f"Network Error: {e}"


def get_like_api(version="like"):
    version = version.lower()
    if version == "v2":
        return setting("v2_like_api", V2_LIKE_API_URL)
    if version == "v1":
        return setting("v1_like_api", V1_LIKE_API_URL)
    if version == "likel":
        return setting("likel_api", LIKEL_API_URL)
    return setting("like_api", LIKE_API_URL)


def call_like_api(uid, region, version="like"):
    api_url = get_like_api(version)
    if not api_url or not str(api_url).strip():
        return None, f"{version.upper()} Like API is not configured."

    try:
        url = api_url.format(uid=uid, region=region)
    except KeyError as e:
        return None, f"Like API template is missing {{{e.args[0]}}}"
    except Exception as e:
        return None, f"Invalid Like API URL: {e}"

    return _get_json(url)


def get_autolike_api(version="auto"):
    version = (version or "auto").lower()
    if version == "v1":
        return setting("v1_autolike_api", V1_AUTO_LIKE_API_URL)
    if version == "v2":
        return setting("v2_autolike_api", V2_AUTO_LIKE_API_URL)
    return setting("autolike_api", AUTO_LIKE_API_URL)

def call_autolike_api(uid, region, likes, version="auto"):
    api_url = get_autolike_api(version)
    if not api_url or not str(api_url).strip():
        return None, f"{version.upper()} AutoLike API is not configured."
    try:
        url = api_url.format(uid=uid, region=region, likes=likes)
    except KeyError as e:
        return None, f"AutoLike API template is missing {{{e.args[0]}}}"
    except Exception as e:
        return None, f"Invalid AutoLike API URL: {e}"
    return _get_json(url)


def likes_delta(data):
    try:
        return int(data.get("LikesafterCommand", 0)) - int(
            data.get("LikesbeforeCommand", 0)
        )
    except (TypeError, ValueError):
        return 0


# ================= FORCE JOIN =================
def get_force_join_targets():
    return q(
        "SELECT chat_id,title,invite_link FROM force_join ORDER BY chat_id",
        fetchall=True,
    )


def user_has_joined(user_id, chat_id):
    try:
        member = bot.get_chat_member(chat_id, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        return False


def check_force_join(msg):
    targets = get_force_join_targets()
    missing = []
    for chat_id, title, invite_link in targets:
        if not user_has_joined(msg.from_user.id, chat_id):
            missing.append((chat_id, title or str(chat_id), invite_link))
    if not missing:
        return True

    markup = types.InlineKeyboardMarkup()
    for chat_id, title, invite_link in missing:
        if invite_link:
            markup.add(types.InlineKeyboardButton(f"🔗 Join {title}", url=invite_link))

    markup.add(types.InlineKeyboardButton("✅ I've Joined — Check Again", callback_data="check_force_join"))
    bot.reply_to(
        msg,
        "🔒 <b>Force Join Required</b>\n\n"
        "Please join all required Telegram channels/groups, then press "
        "<b>I've Joined — Check Again</b> and request the like again.",
        reply_markup=markup,
    )
    return False


@bot.callback_query_handler(func=lambda call: call.data == "check_force_join")
def force_join_callback(call):
    if not get_force_join_targets():
        bot.answer_callback_query(call.id, "No force-join targets configured.")
        return
    if all(user_has_joined(call.from_user.id, row[0]) for row in get_force_join_targets()):
        bot.answer_callback_query(call.id, "Verified! You can use /like now.")
        try:
            bot.edit_message_reply_markup(
                call.message.chat.id, call.message.message_id, reply_markup=None
            )
        except Exception:
            pass
    else:
        bot.answer_callback_query(call.id, "You still need to join all required chats.", show_alert=True)


# ================= START / HELP =================
@bot.message_handler(commands=["start", "help"])
def welcome(msg):
    if not chat_allowed(msg):
        bot.reply_to(msg, "🚫 This bot is available only in authorized groups.")
        return

    text = (
        "╔══════════════════════╗\n"
        "      👑  HoLiC LiKe 👑\n"
        "╚══════════════════════╝\n\n"
        "📌 <b>USER COMMANDS</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "➥ 🚀 /like &lt;region&gt; &lt;uid&gt;\n"
        "➥ 📊 /status\n"
        "➥ 🤖 /myautolike\n"
        
        "👑 <b>OWNER COMMANDS</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "➥ ➕ /addadmin &lt;user_id&gt;\n"
        "➥ ❌ /removeadmin &lt;user_id&gt;\n"
        "➥ 👥 /adminlist\n"
        "➥ 🌍 /setgloballimit Days;\n"
        "➥ 🔌 /setlikeapi &lt;api_url&gt;  → /like\n"
        "➥ 🔌 /setv1api &lt;api_url&gt;    → /v1like\n"
        "➥ 🔌 /setv2api &lt;api_url&gt;    → /v2like\n"
        "➥ 🤖 /setautolikeapi &lt;api_url&gt; → /autolike\n"
        "➥ 🤖 /setv1autolikeapi &lt;api_url&gt; → /v1autolike\n"
        "➥ 🤖 /setv2autolikeapi &lt;api_url&gt; → /v2autolike\n"
        "➥ 📡 /api\n\n"
        "⚡ <b>ADMIN COMMANDS</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "➥ 🚀 /like &lt;region&gt; &lt;uid&gt;\n"
        "➥ 🚀 /v1like &lt;region&gt; &lt;uid&gt;\n"
        "➥ 🚀 /v2like &lt;region&gt; &lt;uid&gt;\n"
        "➥ 🤖 /autolike &lt;region&gt; &lt;uid&gt; &lt;likes&gt; &lt;days&gt; [user_id]\n"
        "➥ 🤖 /v1autolike &lt;region&gt; &lt;uid&gt; &lt;likes&gt; &lt;days&gt; [user_id]\n"
        "➥ 🤖 /v2autolike &lt;region&gt; &lt;uid&gt; &lt;likes&gt; &lt;days&gt; [user_id]\n"
        "➥ ⏩ /extenduid &lt;uid&gt; &lt;likes&gt;\n"
        "➥ ❌ /remove_autolike &lt;uid&gt;\n"
        "➥ 📋 /autolike_list — AutoLike UIDs\n"
        "➥ 📋 /v1autolike_list — V1 AutoLike UIDs\n"
        "➥ 📋 /v2autolike_list — V2 AutoLike UIDs\n"
        "➥ ⚡ /force_like  (alias: /runnow)\n"
        "➥ ⏰ /settime &lt;HH:MM&gt;\n"
        "➥ 🎯 /setslots &lt;number&gt;\n"
        "➥ ➕ /addgroup &lt;group_id&gt;\n"
        "➥ ❌ /removegroup &lt;group_id&gt;\n"
        "➥ 🏘️ /grouplist\n"
        "➥ ⚙️ /settings\n"
        "➥ 🔔 /toggle_user_notify\n"
        "➥ ✉️ /sndmsg &lt;group_id&gt; &lt;message&gt;\n"
        "➥ 📁 /getfile\n"
        "➥ ♻️ /restore\n"
        "➥ 🚀 /likeon /likeoff\n"
        "➥ 📢 /spam &lt;message&gt; [1-3] (admin test only)\n\n"
        "🔒 <b>FORCE JOIN</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "➥ ➕ /addforcejoin &lt;chat_id&gt; [invite_link]\n"
        "➥ ❌ /removeforcejoin &lt;chat_id&gt;\n"
        "➥ 📢 /forcejoinlist\n"
    )
    bot.reply_to(msg, text)


# ================= OWNER / ADMIN =================
@bot.message_handler(commands=["addadmin"])
def add_admin(msg):
    if not require_owner(msg):
        return
    args = msg.text.split()
    if len(args) != 2:
        bot.reply_to(msg, "Usage: /addadmin USER_ID")
        return
    try:
        uid = int(args[1])
        admins = get_admins()
        if uid in OWNER_IDS or uid in admins:
            bot.reply_to(msg, "⚠️ User is already an admin.")
            return
        admins.append(uid)
        save_admins(admins)
        bot.reply_to(msg, f"✅ Added admin: <code>{uid}</code>")
    except ValueError:
        bot.reply_to(msg, "❌ USER_ID must be a number.")


@bot.message_handler(commands=["removeadmin"])
def remove_admin(msg):
    if not require_owner(msg):
        return
    args = msg.text.split()
    if len(args) != 2:
        bot.reply_to(msg, "Usage: /removeadmin USER_ID")
        return
    try:
        uid = int(args[1])
        admins = get_admins()
        if uid not in admins:
            bot.reply_to(msg, "❌ User is not an admin.")
            return
        admins.remove(uid)
        save_admins(admins)
        bot.reply_to(msg, f"✅ Removed admin: <code>{uid}</code>")
    except ValueError:
        bot.reply_to(msg, "❌ USER_ID must be a number.")


@bot.message_handler(commands=["adminlist"])
def admin_list1(msg):
    if not require_owner(msg):
        return
    admins = get_admins()
    if not admins:
        bot.reply_to(msg, "👑 No extra admins configured.")
        return
    bot.reply_to(msg, "👑 <b>Admins</b>\n\n" + "\n".join(f"• <code>{x}</code>" for x in admins))


@bot.message_handler(commands=["addgroup"])
def add_group(msg):
    if not require_owner(msg):
        return
    args = msg.text.split()
    if len(args) != 2:
        bot.reply_to(msg, "Usage: /addgroup GROUP_ID")
        return
    try:
        gid = int(args[1])
        if gid in get_allowed_groups():
            bot.reply_to(msg, "⚠️ Group already exists.")
            return
        q("INSERT OR IGNORE INTO allowed_groups(group_id) VALUES(?)", (gid,), commit=True)
        bot.reply_to(msg, f"✅ Group added: <code>{gid}</code>")
    except ValueError:
        bot.reply_to(msg, "❌ GROUP_ID must be a number.")


@bot.message_handler(commands=["removegroup"])
def remove_group(msg):
    if not require_owner(msg):
        return
    args = msg.text.split()
    if len(args) != 2:
        bot.reply_to(msg, "Usage: /removegroup GROUP_ID")
        return
    try:
        gid = int(args[1])
        q("DELETE FROM allowed_groups WHERE group_id=?", (gid,), commit=True)
        bot.reply_to(msg, f"✅ Group removed: <code>{gid}</code>")
    except ValueError:
        bot.reply_to(msg, "❌ GROUP_ID must be a number.")


@bot.message_handler(commands=["grouplist"])
def group_list(msg):
    if not require_owner(msg):
        return
    groups = get_allowed_groups()
    bot.reply_to(msg, "🏢 <b>Authorized Groups</b>\n\n" + "\n".join(f"• <code>{x}</code>" for x in groups))


# ================= FORCE JOIN ADMIN COMMANDS =================
@bot.message_handler(commands=["addforcejoin", "forcejoin"])
def add_force_join(msg):
    if not require_admin(msg):
        return
    args = msg.text.split(maxsplit=2)
    if len(args) < 2:
        bot.reply_to(msg, "Usage:\n/addforcejoin CHAT_ID [INVITE_LINK]")
        return
    try:
        chat_id = int(args[1])
    except ValueError:
        bot.reply_to(msg, "❌ CHAT_ID must be numeric, e.g. -1001234567890")
        return

    invite = args[2].strip() if len(args) == 3 else ""
    try:
        chat = bot.get_chat(chat_id)
        title = chat.title or chat.username or str(chat_id)
    except Exception as e:
        bot.reply_to(
            msg,
            "❌ Bot cannot access that Telegram chat.\n"
            "Add the bot to the channel/group first and make sure it can check members.\n\n"
            f"<code>{e}</code>",
        )
        return

    q(
        "INSERT INTO force_join(chat_id,title,invite_link) VALUES(?,?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, invite_link=excluded.invite_link",
        (chat_id, title, invite),
        commit=True,
    )
    bot.reply_to(msg, f"✅ Force-join added: <b>{title}</b>\nID: <code>{chat_id}</code>")


@bot.message_handler(commands=["removeforcejoin"])
def remove_force_join(msg):
    if not require_admin(msg):
        return
    args = msg.text.split()
    if len(args) != 2:
        bot.reply_to(msg, "Usage: /removeforcejoin CHAT_ID")
        return
    try:
        chat_id = int(args[1])
    except ValueError:
        bot.reply_to(msg, "❌ CHAT_ID must be numeric.")
        return
    q("DELETE FROM force_join WHERE chat_id=?", (chat_id,), commit=True)
    bot.reply_to(msg, f"✅ Force-join removed: <code>{chat_id}</code>")


@bot.message_handler(commands=["forcejoinlist"])
def force_join_list(msg):
    if not require_admin(msg):
        return
    rows = get_force_join_targets()
    if not rows:
        bot.reply_to(msg, "🔒 No force-join channels/groups configured.")
        return
    out = ["🔒 <b>Force Join Targets</b>\n"]
    for chat_id, title, link in rows:
        out.append(f"• {title} — <code>{chat_id}</code>\n  {link or 'No invite link set'}")
    bot.reply_to(msg, "\n".join(out))


# ================= LIKE COMMANDS =================
def process_manual_like(msg, balance_tracking=False, api_version="like", command_name=None):
    if not chat_allowed(msg):
        bot.reply_to(msg, "🚫 This bot is available only in authorized groups.")
        return
    if not LIKE_ENABLED and not is_admin(msg.from_user.id):
        bot.reply_to(msg, "❌ Like command is currently disabled.")
        return
    if not check_force_join(msg):
        return

    args = msg.text.split()
    if len(args) != 3:
        command_name = command_name or ("like" if api_version == "like" else f"{api_version}like")
        bot.reply_to(msg, f"Usage: /{command_name} REGION UID")
        return

    region, uid = args[1].lower(), args[2]
    # Public /like limit: 1 successful /like per user per IST day.
    # Admins remain exempt, consistent with the existing admin bypasses.
    if not is_admin(msg.from_user.id) and get_user_daily_usage(msg.from_user.id) >= 1:
        bot.reply_to(msg, "❌ Your daily /like limit is 1 per day. Please try again tomorrow.")
        return

    if get_daily_usage() >= get_global_daily_limit() and not is_admin(msg.from_user.id):
        bot.reply_to(msg, f"❌ Daily limit reached ({get_global_daily_limit()}).")
        return

    processing = bot.reply_to(
        msg,
        "╔══════════════════╗\n"
        "    ⏳ PROCESSING LIKE\n"
        "╚══════════════════╝\n\n"
        f"🆔 UID : <code>{uid}</code>\n"
        f"🌍 Region : {region.upper()}\n\n"
        f"🔌 API : {api_version.upper()}\n"
        "⚡ Contacting Like API...",
    )

    try:
        data, err = call_like_api(uid, region, api_version)
    except Exception as e:
        data, err = None, f"Unexpected API error: {type(e).__name__}: {e}"

    if err:
        notify_owner_api_error(
            api_version.upper() + " Like", err, msg=msg, uid=uid, region=region
        )
        bot.edit_message_text(
            "❌ Like service is currently unavailable.\n"
            "👑 Owner has been notified.",
            msg.chat.id,
            processing.message_id,
        )
        return
    if not data:
        bot.edit_message_text(
            "❌ Like API returned an empty response.",
            msg.chat.id,
            processing.message_id,
        )
        return

    before_value = (
        data.get("LikesbeforeCommand")
        if data.get("LikesbeforeCommand") is not None
        else data.get("likesbeforecommand",
        data.get("likes_before",
        data.get("before",
        data.get("likesBefore"))))
    )
    after_value = (
        data.get("LikesafterCommand")
        if data.get("LikesafterCommand") is not None
        else data.get("likesaftercommand",
        data.get("likes_after",
        data.get("after",
        data.get("likesAfter"))))
    )

    if before_value is None or after_value is None:
        keys = ", ".join(list(data.keys())[:12]) if isinstance(data, dict) else "none"
        bot.edit_message_text(
            "❌ Like API responded, but its result fields were not recognized.\n"
            f"Returned fields: <code>{keys or 'none'}</code>\n"
            "Set the API URL with the correct endpoint using /setlikeapi, /setv1api or /setv2api.",
            msg.chat.id,
            processing.message_id,
        )
        return

    try:
        before = int(before_value)
        after = int(after_value)
    except (TypeError, ValueError):
        bot.edit_message_text(
            "❌ Like API returned invalid like counts.",
            msg.chat.id,
            processing.message_id,
        )
        return
    added = after - before

    if added > 0:
        # The public /like command is limited to one successful use per
        # non-admin user per IST day. Count any successful delivery.
        if added > 0:
            update_user_usage(msg.from_user.id)

        # Keep the existing global balance rule: only 4+ delivered likes
        # consume one unit of the global daily balance.
        counted = added >= 4
        if counted:
            update_usage()

        text = (
            "✅✨ ʟɪᴋᴇ sᴜᴄᴄᴇssғᴜʟ  🎉🚀\n\n"
            f"👤 ᴘʟᴀʏᴇʀɴᴀᴍᴇ : {data.get('PlayerNickname','N/A')}\n"
            f"🆔 ᴜɪᴅ : <code>{uid}</code>\n"
            f"⭐️ ʟᴇᴠᴇʟ : {data.get('playerLevel','N/A')}\n"
            f"🌍 ʀᴇɢɪᴏɴ : {region.upper()}\n\n"
            f"👍 ʟɪᴋᴇs ʙᴇғᴏʀᴇ : {before}\n"
            f"🔥 ʟɪᴋᴇs ᴀғᴛᴇʀ : {after}\n"
            f"🎯 ʟɪᴋᴇs ɢɪᴠᴇɴ : {added}\n"
        )
        if counted:
            left = max(get_global_daily_limit() - get_daily_usage(), 0)
            text += (
                f"\n📊 ɢʟᴏʙᴀʟ ʀᴇᴍᴀɪɴɪɴɢ ᴛᴏᴅᴀʏ : {left}/{MAX_DAILY_LIMIT} 🌍\n"
                "⚙️ sᴛᴀᴛᴜs : sᴜᴄᴄᴇss ✅🔥\n\n"
            )
        else:
            text += "⚙️ sᴛᴀᴛᴜs : sᴜᴄᴄᴇss ✅🔥\n\n"
        text += "━━━━━━━━━━━━━━\n👑 ᴏᴡɴᴇʀ : @HeR_HoLiC18"
    else:
        # Use the same layout as the normal successful result, even when
        # the API adds 0 likes. Do not expose messages like
        # "likes_already_send" to the user.
        left = max(get_global_daily_limit() - get_daily_usage(), 0)
        text = (
            "✅✨ ʟɪᴋᴇ sᴜᴄᴄᴇssғᴜʟ  🎉🚀\n\n"
            f"👤 ᴘʟᴀʏᴇʀɴᴀᴍᴇ : {data.get('PlayerNickname','N/A')}\n"
            f"🆔 ᴜɪᴅ : <code>{uid}</code>\n"
            f"⭐️ ʟᴇᴠᴇʟ : {data.get('playerLevel','N/A')}\n"
            f"🌍 ʀᴇɢɪᴏɴ : {region.upper()}\n\n"
            f"👍 ʟɪᴋᴇs ʙᴇғᴏʀᴇ : {before}\n"
            f"🔥 ʟɪᴋᴇs ᴀғᴛᴇʀ : {after}\n"
            f"🎯 ʟɪᴋᴇs ɢɪᴠᴇɴ : {added}\n\n"
            f"📊 ɢʟᴏʙᴀʟ ʀᴇᴍᴀɪɴɪɴɢ ᴛᴏᴅᴀʏ : {left}/{MAX_DAILY_LIMIT} 🌍\n"
            "⚙️ sᴛᴀᴛᴜs : sᴜᴄᴄᴇss ✅🔥\n\n"
            "━━━━━━━━━━━━━━\n👑 ᴏᴡɴᴇʀ : @HeR_HoLiC18"
        )

    bot.delete_message(msg.chat.id, processing.message_id)
    bot.send_photo(msg.chat.id, RESULT_PHOTO_URL, caption=text, parse_mode="HTML")


@bot.message_handler(commands=["like"])
def manual_like(msg):
    # Normal /like has its own API slot and its own usage text.
    process_manual_like(msg, balance_tracking=False, api_version="like", command_name="like")


@bot.message_handler(commands=["likel"])
def like_with_balance(msg):
    process_manual_like(msg, balance_tracking=True, api_version="likel", command_name="likel")


@bot.message_handler(commands=["v1like"])
def v1_like(msg):
    if not require_admin(msg):
        return
    process_manual_like(msg, balance_tracking=False, api_version="v1")


@bot.message_handler(commands=["v2like"])
def v2_like(msg):
    if not require_admin(msg):
        return
    process_manual_like(msg, balance_tracking=False, api_version="v2")


def call_external_template(api_url, uid, region):
    if not api_url:
        return None, "API is not configured. Set INFO_API_URL / VISIT_API_URL."
    try:
        url = api_url.format(uid=uid, region=region)
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        return r.json(), None
    except requests.RequestException as e:
        return None, f"Network Error: {e}"
    except ValueError:
        return None, "Invalid JSON response from API."


@bot.message_handler(commands=["spam"])
def controlled_spam_test(msg):
    # Safe, admin-only test: maximum 3 messages in the current chat.
    if not is_admin(msg.from_user.id):
        return
    if not chat_allowed(msg):
        bot.reply_to(msg, "🚫 This bot is available only in authorized groups.")
        return
    parts = msg.text.split()[1:]
    if not parts:
        bot.reply_to(msg, "Usage: /spam MESSAGE [1-3]")
        return
    count = 1
    if parts[-1].isdigit():
        count = max(1, min(int(parts[-1]), 3))
        parts = parts[:-1]
    message = " ".join(parts).strip()
    if not message:
        bot.reply_to(msg, "Usage: /spam MESSAGE [1-3]")
        return
    for _ in range(count):
        bot.send_message(msg.chat.id, message)
        time.sleep(0.5)


@bot.message_handler(commands=["status"])
def status_system(msg):
    if not chat_allowed(msg):
        bot.reply_to(msg, "🚫 This bot is available only in authorized groups.")
        return
    total = q("SELECT COUNT(*) FROM autolikes", fetchone=True)[0]
    used = get_daily_usage()
    bot.reply_to(
        msg,
        "╔══════════════════╗\n"
        "       🤖 BOT STATUS\n"
        "╚══════════════════╝\n\n"
        "✅ Status: ONLINE\n"
        f"📊 AutoLike UIDs: {total}/{MAX_UID_SLOTS}\n"
        f"⏰ Default time: {int(setting('default_hour', DEFAULT_HOUR)):02d}:{int(setting('default_minute', DEFAULT_MINUTE)):02d} IST\n"
        f"🔥 /like daily API usage: {used}/{get_global_daily_limit()}\n"
        f"⚡ AutoLike API: {'CONFIGURED' if get_autolike_api('auto') else 'NOT CONFIGURED'}\n"
        f"⚡ V1 AutoLike API: {'CONFIGURED' if get_autolike_api('v1') else 'NOT CONFIGURED'}\n"
        f"⚡ V2 AutoLike API: {'CONFIGURED' if get_autolike_api('v2') else 'NOT CONFIGURED'}\n"
        f"🔒 Force Join targets: {len(get_force_join_targets())}",
    )


# ================= AUTOLIKE COMMANDS =================
def parse_int(value, name):
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"{name} must be a number.")


@bot.message_handler(commands=["autolike", "v1autolike", "v2autolike"])
def add_autolike(msg):
    if not require_admin(msg):
        return
    args = msg.text.split()
    command = args[0].split("@", 1)[0].lower()
    api_version = (
        "v1" if command == "/v1autolike"
        else "v2" if command == "/v2autolike"
        else "auto"
    )
    if len(args) not in (5, 6):
        bot.reply_to(msg, f"Usage:\n{command} REGION UID LIKES DAYS [USER_ID]")
        return
    try:
        region = args[1].lower()
        uid = args[2]
        likes = max(1, parse_int(args[3], "LIKES"))
        days = max(1, parse_int(args[4], "DAYS"))
        notify_user = int(args[5]) if len(args) == 6 else msg.from_user.id

        current = q("SELECT COUNT(*) FROM autolikes", fetchone=True)[0]
        exists = q("SELECT 1 FROM autolikes WHERE uid=?", (uid,), fetchone=True)
        if not exists and current >= int(setting("max_slots", MAX_UID_SLOTS)):
            bot.reply_to(msg, "❌ AutoLike UID slot limit reached. Use /setslots.")
            return

        hour = int(setting("default_hour", DEFAULT_HOUR))
        minute = int(setting("default_minute", DEFAULT_MINUTE))
        expire = (datetime.now(IST) + timedelta(days=days)).strftime("%Y-%m-%d")
        added_date = datetime.now(IST).strftime("%Y-%m-%d")

        q(
            """INSERT INTO autolikes
            (uid,region,chat_id,expire,hour,minute,added_date,requested_likes,notify_user,notify_enabled,api_version)
            VALUES(?,?,?,?,?,?,?,?,?,1,?)
            ON CONFLICT(uid) DO UPDATE SET
                region=excluded.region,
                chat_id=excluded.chat_id,
                expire=excluded.expire,
                hour=excluded.hour,
                minute=excluded.minute,
                added_date=COALESCE(autolikes.added_date, excluded.added_date),
                requested_likes=excluded.requested_likes,
                notify_user=excluded.notify_user,
                notify_enabled=1,
                api_version=excluded.api_version""",
            (uid, region, msg.chat.id, expire, hour, minute, added_date, likes, notify_user, api_version),
            commit=True,
        )
        bot.reply_to(
            msg,
            "✅ <b>AutoLike Added/Renewed</b>\n\n"
            f"🆔 UID: <code>{uid}</code>\n"
            f"🌍 Region: {region.upper()}\n"
            f"❤️ Requested likes: {likes}\n"
            f"📅 Days: {days}\n"
            f"⏰ Time: {hour:02d}:{minute:02d} IST\n"
            f"🔌 API: <code>{api_version.upper()}</code> AutoLike\n"
            f"🔔 Notify user: <code>{notify_user}</code>",
        )
    except Exception as e:
        bot.reply_to(msg, f"❌ Error: {e}")


@bot.message_handler(commands=["extenduid"])
def extend_uid(msg):
    if not require_admin(msg):
        return
    args = msg.text.split()
    if len(args) != 3:
        bot.reply_to(msg, "Usage: /extenduid UID LIKES")
        return
    try:
        uid = args[1]
        likes = max(1, int(args[2]))
        row = q("SELECT requested_likes FROM autolikes WHERE uid=?", (uid,), fetchone=True)
        if not row:
            bot.reply_to(msg, "❌ UID not found.")
            return
        new_likes = int(row[0] or 0) + likes
        q("UPDATE autolikes SET requested_likes=? WHERE uid=?", (new_likes, uid), commit=True)
        bot.reply_to(msg, f"✅ Extended <code>{uid}</code> by {likes} likes. Total setting: {new_likes}.")
    except ValueError:
        bot.reply_to(msg, "❌ LIKES must be a number.")


@bot.message_handler(commands=["remove_autolike", "remove"])
def remove_autolike(msg):
    if not require_admin(msg):
        return
    args = msg.text.split()
    if len(args) != 2:
        bot.reply_to(msg, "Usage: /remove_autolike UID")
        return
    uid = args[1]
    q("DELETE FROM autolikes WHERE uid=?", (uid,), commit=True)
    last_run.pop(uid, None)
    bot.reply_to(msg, f"✅ AutoLike UID removed: <code>{uid}</code>")


@bot.message_handler(commands=["autolike_list"])
def autolike_list(msg):
    if not require_admin(msg):
        return
    rows = q(
        "SELECT uid,region,expire,requested_likes FROM autolikes WHERE api_version=? ORDER BY expire",
        ("auto",), fetchall=True,
    )
    if not rows:
        bot.reply_to(msg, "📋 No active AutoLike UIDs using AUTO API.")
        return
    out = ["📋 <b>AUTOLIKE UIDs (AUTO)</b>\n"]
    now = datetime.now(IST).date()
    for uid, region, expire, likes in rows:
        try:
            days_left = max((datetime.strptime(expire[:10], "%Y-%m-%d").date() - now).days, 0)
        except Exception:
            days_left = 0
        out.append(f"🆔 <code>{uid}</code> | 🌍 {region.upper()} | ❤️ {likes} | ⏳ {days_left} days")
    bot.reply_to(msg, "\n".join(out))


@bot.message_handler(commands=["v1autolike_list"])
def v1autolike_list(msg):
    if not require_admin(msg):
        return
    rows = q(
        "SELECT uid,region,expire,requested_likes FROM autolikes WHERE api_version=? ORDER BY expire",
        ("v1",), fetchall=True,
    )
    if not rows:
        bot.reply_to(msg, "📋 No active V1 AutoLike UIDs.")
        return
    out = ["📋 <b>V1 AUTOLIKE UIDs</b>\n"]
    now = datetime.now(IST).date()
    for uid, region, expire, likes in rows:
        try:
            days_left = max((datetime.strptime(expire[:10], "%Y-%m-%d").date() - now).days, 0)
        except Exception:
            days_left = 0
        out.append(f"🆔 <code>{uid}</code> | 🌍 {region.upper()} | ❤️ {likes} | ⏳ {days_left} days")
    bot.reply_to(msg, "\n".join(out))


@bot.message_handler(commands=["v2autolike_list"])
def v2autolike_list(msg):
    if not require_admin(msg):
        return
    rows = q(
        "SELECT uid,region,expire,requested_likes FROM autolikes WHERE api_version=? ORDER BY expire",
        ("v2",), fetchall=True,
    )
    if not rows:
        bot.reply_to(msg, "📋 No active V2 AutoLike UIDs.")
        return
    out = ["📋 <b>V2 AUTOLIKE UIDs</b>\n"]
    now = datetime.now(IST).date()
    for uid, region, expire, likes in rows:
        try:
            days_left = max((datetime.strptime(expire[:10], "%Y-%m-%d").date() - now).days, 0)
        except Exception:
            days_left = 0
        out.append(f"🆔 <code>{uid}</code> | 🌍 {region.upper()} | ❤️ {likes} | ⏳ {days_left} days")
    bot.reply_to(msg, "\n".join(out))


@bot.message_handler(commands=["like_list", "list"])
def like_list(msg):
    if not require_admin(msg):
        return
    rows = q(
        "SELECT uid,region,expire,hour,minute,requested_likes,notify_user,notify_enabled,api_version "
        "FROM autolikes ORDER BY expire",
        fetchall=True,
    )
    if not rows:
        bot.reply_to(msg, "📋 No active AutoLike UIDs.")
        return
    out = ["📋 <b>ACTIVE AUTOLIKE UIDS</b>\n"]
    now = datetime.now(IST).date()
    for uid, region, expire, hour, minute, likes, notify_user, notify_enabled, api_version in rows:
        try:
            days_left = max((datetime.strptime(expire[:10], "%Y-%m-%d").date() - now).days, 0)
        except Exception:
            days_left = 0
        out.append(
            f"🆔 <code>{uid}</code>\n"
            f"🌍 {region.upper()} | ❤️ {likes}\n"
            f"⏰ {hour:02d}:{minute:02d} IST | ⏳ {days_left} days\n"
            f"🔌 API: {str(api_version).upper()} | 🔔 Notify: {'ON' if notify_enabled else 'OFF'} | User: {notify_user}\n"
        )
    bot.reply_to(msg, "\n".join(out))


@bot.message_handler(commands=["myautolike"])
def my_autolikes(msg):
    rows = q(
        "SELECT uid,region,expire,hour,minute,requested_likes,notify_enabled,api_version "
        "FROM autolikes WHERE notify_user=?",
        (msg.from_user.id,),
        fetchall=True,
    )
    if not rows:
        bot.reply_to(msg, "📅 You have no AutoLike entries.")
        return
    out = ["📅 <b>YOUR AUTOLIKE STATUS</b>\n"]
    today = datetime.now(IST).date()
    for uid, region, expire, hour, minute, likes, notify, api_version in rows:
        try:
            left = max((datetime.strptime(expire[:10], "%Y-%m-%d").date() - today).days, 0)
        except Exception:
            left = 0
        out.append(
            f"🆔 <code>{uid}</code> | {region.upper()}\n"
            f"❤️ {likes} | ⏰ {hour:02d}:{minute:02d}\n"
            f"⏳ {left} days | 🔌 {str(api_version).upper()} | 🔔 {'ON' if notify else 'OFF'}\n"
        )
    bot.reply_to(msg, "\n".join(out))


@bot.message_handler(commands=["force_like", "runnow"])
def force_like(msg):
    if not require_admin(msg):
        return
    rows = q(
        "SELECT uid,region,chat_id,expire,requested_likes,notify_user,notify_enabled,api_version "
        "FROM autolikes",
        fetchall=True,
    )
    if not rows:
        bot.reply_to(msg, "❌ No AutoLike UIDs.")
        return
    threading.Thread(target=process_autolikes, args=(rows, msg.chat.id), daemon=True).start()
    bot.reply_to(msg, "⚡ Force AutoLike processing started.")


@bot.message_handler(commands=["settime"])
def set_time(msg):
    if not require_admin(msg):
        return
    args = msg.text.split()
    if len(args) != 2:
        bot.reply_to(msg, "Usage: /settime HH:MM")
        return
    try:
        h, m = map(int, args[1].split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
        set_setting("default_hour", h)
        set_setting("default_minute", m)
        # Apply to current UIDs too, as the command is described as a global daily time.
        q("UPDATE autolikes SET hour=?,minute=?", (h, m), commit=True)
        bot.reply_to(msg, f"⏰ AutoLike time set to <b>{h:02d}:{m:02d} IST</b>.")
    except ValueError:
        bot.reply_to(msg, "❌ Invalid time. Use HH:MM, e.g. 04:00")


@bot.message_handler(commands=["setgloballimit"])
def set_global_limit(msg):
    if not require_owner(msg):
        return

    args = msg.text.split()
    if len(args) != 2:
        bot.reply_to(msg, "Usage: /setgloballimit NUMBER\nExample: /setgloballimit 50")
        return

    try:
        limit = int(args[1])
        if limit < 1:
            raise ValueError
    except ValueError:
        bot.reply_to(msg, "❌ Limit must be a whole number greater than 0.")
        return

    set_setting("global_daily_limit", limit)
    bot.reply_to(
        msg,
        f"✅ Global daily limit changed to <b>{limit}</b>.\n"
        "This limit is persistent and applies to the global daily balance."
    )


@bot.message_handler(commands=["setslots"])
def set_slots(msg):
    if not require_admin(msg):
        return
    args = msg.text.split()
    if len(args) != 2:
        bot.reply_to(msg, "Usage: /setslots NUMBER")
        return
    try:
        slots = max(1, int(args[1]))
        set_setting("max_slots", slots)
        bot.reply_to(msg, f"✅ Max AutoLike UID slots: <b>{slots}</b>")
    except ValueError:
        bot.reply_to(msg, "❌ NUMBER must be numeric.")


@bot.message_handler(commands=["toggle_user_notify"])
def toggle_user_notify(msg):
    if not require_admin(msg):
        return
    rows = q(
        "SELECT DISTINCT notify_enabled FROM autolikes WHERE notify_user=?",
        (msg.from_user.id,),
        fetchall=True,
    )
    current = bool(rows[0][0]) if rows else True
    new_value = 0 if current else 1
    q(
        "UPDATE autolikes SET notify_enabled=? WHERE notify_user=?",
        (new_value, msg.from_user.id),
        commit=True,
    )
    bot.reply_to(msg, f"🔔 Your AutoLike notifications are now <b>{'ON' if new_value else 'OFF'}</b>.")


@bot.message_handler(commands=["settings"])
def settings_cmd(msg):
    if not require_admin(msg):
        return
    h = int(setting("default_hour", DEFAULT_HOUR))
    m = int(setting("default_minute", DEFAULT_MINUTE))
    slots = int(setting("max_slots", MAX_UID_SLOTS))
    v1_config = bool(get_like_api("v1"))
    v2_config = bool(get_like_api("v2"))
    bot.reply_to(
        msg,
        "⚙️ <b>BOT SETTINGS</b>\n\n"
        f"⏰ AutoLike time: {h:02d}:{m:02d} IST\n"
        f"📦 UID slots: {slots}\n"
        f"📊 Daily API limit: {get_global_daily_limit()}\n"
        f"👤 Public /like user limit: 1 per day\n"
        f"❤️ Public /like: {'ON' if LIKE_ENABLED else 'OFF'}\n"
        f"🔌 V1 Like API: {'CONFIGURED' if v1_config else 'MISSING'}\n"
        f"🔌 V2 Like API: {'CONFIGURED' if v2_config else 'MISSING'}\n"
        f"🔌 AutoLike API: {'CONFIGURED' if get_autolike_api('auto') else 'MISSING'}\n"
         f"🔌 V1 AutoLike API: {'CONFIGURED' if get_autolike_api('v1') else 'MISSING'}\n"
         f"🔌 V2 AutoLike API: {'CONFIGURED' if get_autolike_api('v2') else 'MISSING'}\n"
        f"👀 Visit API: {'CONFIGURED' if visit_config else 'MISSING'}\n"
        f"👤 Info API: {'CONFIGURED' if info_config else 'MISSING'}\n"
        f"🔒 Force Join: {len(get_force_join_targets())} target(s)\n"
        f"🏢 Allowed groups: {len(get_allowed_groups())}",
    )


@bot.message_handler(commands=["likeon"])
def enable_like(msg):
    global LIKE_ENABLED
    if not require_admin(msg):
        return
    LIKE_ENABLED = True
    set_setting("like_enabled", "1")
    bot.reply_to(msg, "✅ Public /like ENABLED. This setting will remain ON after bot restarts.")


@bot.message_handler(commands=["likeoff"])
def disable_like(msg):
    global LIKE_ENABLED
    if not require_admin(msg):
        return
    LIKE_ENABLED = False
    set_setting("like_enabled", "0")
    bot.reply_to(msg, "⛔ Public /like DISABLED. This setting will remain OFF until /likeon is used.")


# ================= API MANAGEMENT =================
@bot.message_handler(commands=["setv1api"])
def set_v1_api(msg):
    if not require_owner(msg):
        return
    args = msg.text.split(maxsplit=1)
    if len(args) != 2 or not args[1].strip():
        bot.reply_to(msg, "Usage: /setv1api API_URL\nUse {uid} and {region} placeholders.")
        return
    set_setting("v1_like_api", args[1].strip())
    bot.reply_to(msg, "✅ V1 Like API updated.")


@bot.message_handler(commands=["setlikelapi"])
def set_likel_api(msg):
    if not require_owner(msg):
        return
    args = msg.text.split(maxsplit=1)
    if len(args) != 2 or not args[1].strip():
        bot.reply_to(msg, "Usage: /setlikelapi API_URL\\nUse {uid} and {region} placeholders.")
        return
    set_setting("likel_api", args[1].strip())
    bot.reply_to(msg, "✅ /likel API updated.")


@bot.message_handler(commands=["setv2api"])
def set_v2_api(msg):
    if not require_owner(msg):
        return
    args = msg.text.split(maxsplit=1)
    if len(args) != 2 or not args[1].strip():
        bot.reply_to(msg, "Usage: /setv2api API_URL\nUse {uid} and {region} placeholders.")
        return
    set_setting("v2_like_api", args[1].strip())
    bot.reply_to(msg, "✅ V2 Like API updated.")




@bot.message_handler(commands=["setlikeapi"])
def set_like_api(msg):
    if not require_owner(msg):
        return
    args = msg.text.split(maxsplit=1)
    if len(args) != 2 or not args[1].strip():
        bot.reply_to(msg, "Usage: /setlikeapi API_URL\\nUse {uid} and {region} placeholders.")
        return
    set_setting("like_api", args[1].strip())
    bot.reply_to(msg, "✅ Normal /like API updated.")


@bot.message_handler(commands=["setautolikeapi"])
def set_autolike_api(msg):
    if not require_owner(msg):
        return
    args = msg.text.split(maxsplit=1)
    if len(args) != 2 or not args[1].strip():
        bot.reply_to(
            msg,
            "Usage: /setautolikeapi API_URL\\n"
            "Use {uid}, {region} and optionally {likes} placeholders."
        )
        return
    set_setting("autolike_api", args[1].strip())
    bot.reply_to(msg, "✅ AutoLike API updated.")


@bot.message_handler(commands=["setv1autolikeapi"])
def set_v1_autolike_api(msg):
    if not require_owner(msg):
        return
    args = msg.text.split(maxsplit=1)
    if len(args) != 2 or not args[1].strip():
        bot.reply_to(msg, "Usage: /setv1autolikeapi API_URL\nUse {uid}, {region} and optionally {likes} placeholders.")
        return
    set_setting("v1_autolike_api", args[1].strip())
    bot.reply_to(msg, "✅ V1 AutoLike API updated.")


@bot.message_handler(commands=["setv2autolikeapi"])
def set_v2_autolike_api(msg):
    if not require_owner(msg):
        return
    args = msg.text.split(maxsplit=1)
    if len(args) != 2 or not args[1].strip():
        bot.reply_to(msg, "Usage: /setv2autolikeapi API_URL\nUse {uid}, {region} and optionally {likes} placeholders.")
        return
    set_setting("v2_autolike_api", args[1].strip())
    bot.reply_to(msg, "✅ V2 AutoLike API updated.")


@bot.message_handler(commands=["setvisitapi"])
def set_visit_api(msg):
    if not require_owner(msg):
        return
    args = msg.text.split(maxsplit=1)
    if len(args) != 2 or not args[1].strip():
        return
    set_setting("visit_api", args[1].strip())
    bot.reply_to(msg, "✅ Visit API updated.")


@bot.message_handler(commands=["setinfoapi"])
def set_info_api(msg):
    if not require_owner(msg):
        return
    args = msg.text.split(maxsplit=1)
    if len(args) != 2 or not args[1].strip():
        return
    set_setting("info_api", args[1].strip())
    bot.reply_to(msg, "✅ Info API updated.")


@bot.message_handler(commands=["api"])
def api_status(msg):
    if not require_admin(msg):
        return
    like = get_like_api("like")
    likel = get_like_api("likel")
    v1 = get_like_api("v1")
    v2 = get_like_api("v2")
    autolike = get_autolike_api()
    v1_autolike = get_autolike_api("v1")
    v2_autolike = get_autolike_api("v2")

    def mask(url):
        if not url:
            return "NOT CONFIGURED"
        url = str(url)
        if "key=" in url:
            prefix, rest = url.split("key=", 1)
            key_value, suffix = (rest.split("&", 1) + [""])[:2]
            return prefix + "key=***" + (("&" + suffix) if suffix else "")
        return url

    bot.reply_to(
        msg,
        "🔌 <b>API SETTINGS</b>\n\n"
        f"❤️ /like: <code>{mask(like)}</code>\n"
        f"❤️ /v1like: <code>{mask(v1)}</code>\n"
        f"❤️ /v2like: <code>{mask(v2)}</code>\n"
        f"🤖 /autolike: <code>{mask(autolike)}</code>\n"
        f"🤖 /v1autolike: <code>{mask(get_autolike_api('v1'))}</code>\n"
        "Each command has its own API slot.\n"
        "Use /setlikeapi, /setlikelapi, /setv1api, /setv2api, "
    )


# ================= PLANS =================
@bot.message_handler(commands=["plans"])
def plans(msg):
    bot.reply_to(
        msg,
        "💎 <b>AUTOLIKE PLANS</b>\n\n"
        "Plans are controlled by the bot owner/admin.\n"
        "Use /myautolike to view an active AutoLike attached to your Telegram ID.",
    )


# ================= MESSAGING / FILE =================
@bot.message_handler(commands=["sndmsg"])
def send_group_message(msg):
    if not require_admin(msg):
        return
    args = msg.text.split(maxsplit=2)
    if len(args) != 3:
        bot.reply_to(msg, "Usage: /sndmsg GROUP_ID MESSAGE")
        return
    try:
        gid = int(args[1])
        bot.send_message(gid, args[2])
        bot.reply_to(msg, "✅ Message sent.")
    except Exception as e:
        bot.reply_to(msg, f"❌ Failed: {e}")


# Users/admins who have issued /restore and are expected to upload the DB next.
pending_restore = set()


def _restore_database_from_document(msg, document):
    """Download, validate and replace autolikes.db from a Telegram document."""
    filename = document.file_name or ""
    if filename.lower() != "autolikes.db":
        bot.reply_to(msg, "❌ Please send the actual autolikes.db file.")
        return

    temp_path = "autolikes.restore.tmp"
    backup_path = "autolikes.db.before_restore"

    try:
        file_info = bot.get_file(document.file_id)
        downloaded = bot.download_file(file_info.file_path)

        with open(temp_path, "wb") as f:
            f.write(downloaded)

        # Validate that the uploaded file is a readable SQLite database
        # before replacing the current database.
        test_conn = sqlite3.connect(temp_path)
        try:
            result = test_conn.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise ValueError("SQLite integrity check failed.")
        finally:
            test_conn.close()

        if os.path.exists("autolikes.db"):
            shutil.copy2("autolikes.db", backup_path)

        # Replace the database while holding the same lock used by q().
        # Re-open the shared connection immediately so polling/schedulers do
        # not keep using a closed SQLite connection after /restore.
        global db
        with db_lock:
            try:
                db.commit()
            except sqlite3.Error:
                pass
            try:
                db.close()
            except sqlite3.Error:
                pass

            os.replace(temp_path, "autolikes.db")
            db = sqlite3.connect("autolikes.db", check_same_thread=False)

        bot.reply_to(
            msg,
            "✅ Database restored successfully.\n"
            "🔄 Restart the bot/server to load the restored database.\n\n"
            "🛟 Previous database backup: autolikes.db.before_restore"
        )
    except Exception as e:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass

        bot.reply_to(msg, f"❌ Restore failed: {e}")


@bot.message_handler(commands=["restore"])
def restore_database(msg):
    if not require_admin(msg):
        return

    # /restore can be used in either of these ways:
    # 1) Reply to an existing autolikes.db file with /restore
    # 2) Send /restore first, then send autolikes.db as the next message
    if msg.reply_to_message and getattr(msg.reply_to_message, "document", None):
        _restore_database_from_document(msg, msg.reply_to_message.document)
        return

    pending_restore.add((msg.chat.id, msg.from_user.id))
    bot.reply_to(
        msg,
        "📥 <b>Restore mode enabled.</b>\n\n"
        "Now send the <code>autolikes.db</code> file in your next message.\n"
        "I will restore it automatically.\n\n"
        "❌ Send only the database file, not another command."
    )


@bot.message_handler(content_types=["document"])
def receive_restore_file(msg):
    key = (msg.chat.id, msg.from_user.id)
    if key not in pending_restore:
        return

    if not require_admin(msg):
        pending_restore.discard(key)
        return

    pending_restore.discard(key)
    _restore_database_from_document(msg, msg.document)


@bot.message_handler(commands=["getfile"])
def get_file(msg):
    if not require_admin(msg):
        return

    # /getfile sends the CURRENT DATABASE, not the Python bot source file.
    db_path = os.path.abspath("autolikes.db")

    if not os.path.exists(db_path):
        bot.reply_to(msg, "❌ Database file <code>autolikes.db</code> was not found on the server.")
        return

    try:
        with open(db_path, "rb") as f:
            bot.send_document(
                msg.chat.id,
                f,
                visible_file_name="autolikes.db",
                caption="🗄️ Current HoLiC LiKeS database backup"
            )
        bot.reply_to(msg, "✅ <code>autolikes.db</code> sent successfully.")
    except Exception as e:
        print(f"getfile database error: {e}")
        bot.reply_to(msg, f"❌ Failed to send database: {e}")


# ================= AUTOLIKE ENGINE =================
def send_autolike_notification(target_chat, data, uid, region, days_left, total_days, requested):
    before = int(data.get("LikesbeforeCommand", 0))
    after = int(data.get("LikesafterCommand", 0))
    added = after - before

    text = (
        "🔄✨ ᴀᴜᴛᴏʟɪᴋᴇ ᴅᴇʟɪᴠᴇʀᴇᴅ ✨🔄\n\n"
        f"👑 ᴘʟᴀʏᴇʀɴᴀᴍᴇ : {data.get('PlayerNickname','N/A')}\n"
        f"🆔 ᴜɪᴅ : <code>{uid}</code>\n"
        f"🌍 ʀᴇɢɪᴏɴ : {region.upper()}\n"
        f"⭐️ ʟᴇᴠᴇʟ : {data.get('playerLevel','N/A')}\n\n"
        f"👍 ʟɪᴋᴇs ʙᴇғᴏʀᴇ : {before}\n"
        f"🔥 ʟɪᴋᴇs ᴀғᴛᴇʀ : {after}\n"
        f"🎯 ʟɪᴋᴇs ɢɪᴠᴇɴ : {added}\n"
    )
    text += (
        "⚙️ sᴛᴀᴛᴜs : sᴜᴄᴄᴇss ✅🔥\n\n"
        "━━━━━━━━━━━━━━\n"
        "👑 ᴏᴡɴᴇʀ : @HeR_HoLiC18"
    )
    try:
        bot.send_photo(target_chat, RESULT_PHOTO_URL, caption=text, parse_mode="HTML")
    except Exception as e:
        print("Notification error:", e)


def process_autolikes(rows, report_chat=None):
    success = 0
    failed = 0
    for row in rows:
        uid, region, chat_id, expire_str, requested, notify_user, notify_enabled, api_version = row
        try:
            now = datetime.now(IST)
            expire_date = datetime.strptime(expire_str[:10], "%Y-%m-%d").date()
            if expire_date < now.date():
                q("DELETE FROM autolikes WHERE uid=?", (uid,), commit=True)
                continue


            data, err = call_autolike_api(uid, region, requested, api_version)
            if err or not data or "LikesbeforeCommand" not in data:
                failed += 1
                # Report AutoLike failures to both the configured user and
                # the originating group.
                targets = []
                if notify_enabled and notify_user:
                    targets.append(int(notify_user))
                if chat_id is not None:
                    targets.append(int(chat_id))

                actual_error = err or "Invalid API response"
                notify_owner_api_error("AutoLike", actual_error, uid=uid, region=region)
                for target in dict.fromkeys(targets):
                    try:
                        bot.send_message(
                            target,
                            f"⚠️ AutoLike service is currently unavailable for <code>{html.escape(str(uid))}</code>.\n"
                            "👑 Owner has been notified.",
                        )
                    except Exception:
                        pass
                continue

            added = likes_delta(data)
            if added > 0:
                success += 1

            added_str = q("SELECT added_date FROM autolikes WHERE uid=?", (uid,), fetchone=True)
            added_date = (
                datetime.strptime(added_str[0][:10], "%Y-%m-%d").date()
                if added_str and added_str[0]
                else expire_date - timedelta(days=30)
            )
            total_days = max((expire_date - added_date).days, 1)
            # AutoLike progress is reported to both:
            #   1) the configured user ID (when notifications are enabled)
            #   2) the group where the AutoLike was configured
            # Avoid sending the same report twice when both IDs are identical.
            targets = []
            if notify_enabled and notify_user:
                targets.append(int(notify_user))
            if chat_id is not None:
                targets.append(int(chat_id))

            for target in dict.fromkeys(targets):
                send_autolike_notification(
                    target,
                    data,
                    uid,
                    region,
                    max((expire_date - now.date()).days, 0),
                    total_days,
                    requested,
                )
        except Exception as e:
            failed += 1
            print("AutoLike error:", uid, e)

    if report_chat:
        try:
            bot.send_message(
                report_chat,
                f"⚡ <b>Force AutoLike Completed</b>\n\n"
                f"✔ Success: {success}\n"
                f"❌ Failed: {failed}",
            )
        except Exception:
            pass


def run_scheduler():
    while True:
        try:
            now = datetime.now(IST)
            rows = q(
                "SELECT uid,region,chat_id,expire,hour,minute,requested_likes,notify_user,notify_enabled,api_version "
                "FROM autolikes",
                fetchall=True,
            )
            for uid, region, chat_id, expire, hour, minute, requested, notify_user, notify_enabled, api_version in rows:
                try:
                    expire_date = datetime.strptime(expire[:10], "%Y-%m-%d").date()
                except Exception:
                    continue

                if expire_date < now.date():
                    q("DELETE FROM autolikes WHERE uid=?", (uid,), commit=True)
                    continue

                if now.hour == hour and now.minute == minute and last_run.get(uid) != now.date():
                    last_run[uid] = now.date()
                    process_autolikes(
                        [(uid, region, chat_id, expire, requested, notify_user, notify_enabled, api_version)]
                    )
            time.sleep(20)
        except Exception as e:
            print("Scheduler Error:", e)
            time.sleep(10)


# ================= START =================
if __name__ == "__main__":
    if BOT_TOKEN == "":
        raise SystemExit(
            "Set BOT_TOKEN environment variable before starting the bot."
        )
    print(f"[{datetime.now()}] HoLiC LiKeS bot starting...")
    threading.Thread(target=run_scheduler, daemon=True).start()

# ─────────────────────────────────────────────────────────────
# DAILY DATABASE BACKUP
# Sends autolikes.db to all configured owners every day at 11:50 PM.
# Uses the server's local timezone.
# ─────────────────────────────────────────────────────────────
DAILY_BACKUP_HOUR = 23
DAILY_BACKUP_MINUTE = 50

def _daily_database_backup_loop():
    last_backup_date = None

    while True:
        try:
            now = datetime.now()
            today = now.date()

            if (
                now.hour == DAILY_BACKUP_HOUR
                and now.minute == DAILY_BACKUP_MINUTE
                and last_backup_date != today
            ):
                db_path = os.path.abspath("autolikes.db")

                if os.path.exists(db_path):
                    sent = 0
                    for owner_id in OWNER_IDS:
                        try:
                            with open(db_path, "rb") as f:
                                bot.send_document(
                                    owner_id,
                                    f,
                                    caption=(
                                        f"🗄️ Daily database backup\n"
                                        f"📅 {now.strftime('%Y-%m-%d')}\n"
                                        f"⏰ 11:50 PM"
                                    )
                                )
                            sent += 1
                        except Exception as e:
                            print(f"Daily DB backup error for {owner_id}: {e}")

                    print(f"✅ Daily DB backup sent to {sent}/{len(OWNER_IDS)} owner(s).")
                else:
                    print("⚠️ Daily DB backup skipped: autolikes.db not found.")

                last_backup_date = today

            # Check every few seconds so the exact minute is not missed.
            time.sleep(10)

        except Exception as e:
            print(f"Daily DB backup scheduler error: {e}")
            time.sleep(10)

# Start the scheduler once when the bot starts.
threading.Thread(target=_daily_database_backup_loop, daemon=True).start()

bot.infinity_polling(timeout=60, long_polling_timeout=30)
