import os
import time
import json
import html
import sqlite3
import logging
import threading
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from contextlib import contextmanager

import telebot
from telebot import types

# Cloud Hub CRM: one-file implementation.
# Secrets must be supplied through environment variables.
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DB_PATH = os.getenv("CRM_DB_PATH", "/var/crm_bot.db")
TIMEZONE = ZoneInfo("Europe/Kyiv")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()

if not TOKEN:
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN in environment variables.")
if not DB_PATH:
    raise RuntimeError("CRM_DB_PATH must not be empty.")

bot = telebot.TeleBot(TOKEN, parse_mode="HTML", threaded=True)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("cloud_hub_crm")
db_lock = threading.RLock()
state_lock = threading.RLock()
user_states = {}
reminder_lock = threading.RLock()
stop_event = threading.Event()

ROLES = {"owner", "manager", "staff", "collector", "shipper"}
ROLE_LABELS = {"owner": "Власник", "manager": "Менеджер",
               "staff": "Працівник", "collector": "Збирач",
               "shipper": "Дропшипер"}
PASSWORD_ENV = {
    "owner": os.getenv("CRM_PASSWORD_OWNER", "").strip(),
    "manager": os.getenv("CRM_PASSWORD_MANAGER", "").strip(),
    "staff": os.getenv("CRM_PASSWORD_STAFF", "").strip(),
    "collector": os.getenv("CRM_PASSWORD_COLLECTOR", "").strip(),
    "shipper": os.getenv("CRM_PASSWORD_SHIPPER", "").strip(),
}
PASSWORDS_MAP = {password: role for role, password in PASSWORD_ENV.items() if password}
PERMISSIONS = {
    "create_order": "Створювати замовлення",
    "manage_order_status": "Змінювати будь-які статуси замовлень",
    "view_unsent_orders": "Переглядати ненадіслані замовлення",
    "view_all_orders": "Переглядати всі замовлення",
    "search_orders": "Шукати замовлення",
    "delete_own_order": "Видаляти власні замовлення",
    "delete_any_order": "Видаляти чужі замовлення",
    "statistics": "Переглядати статистику",
    "manage_users": "Керувати користувачами",
    "manage_permissions": "Розподіляти права",
    "manage_shifts": "Керувати змінами",
    "view_shifts": "Переглядати зміни",
    "broadcast": "Робити розсилки",
    "view_tickets": "Обробляти звернення",
    "manage_sos": "Керувати SOS OFFICE",
    "use_ai": "Користуватися AI-помічником",
    "use_psychologist": "Психологічна підтримка",
}
ROLE_DEFAULT_PERMISSIONS = {
    "owner": set(PERMISSIONS),
    "manager": {"create_order", "manage_order_status", "view_unsent_orders", "view_all_orders",
                "search_orders", "delete_own_order", "delete_any_order",
                "view_shifts", "use_ai", "use_psychologist", "view_tickets",
                "manage_sos"},
    "staff": {"create_order", "view_unsent_orders", "view_all_orders",
              "search_orders", "delete_own_order", "view_shifts",
              "use_ai", "use_psychologist"},
    "collector": {"create_order", "view_unsent_orders", "view_all_orders",
                  "search_orders", "delete_own_order", "view_shifts",
                  "use_ai", "use_psychologist"},
    "shipper": {"create_order", "view_all_orders", "search_orders",
                "delete_own_order", "view_shifts", "use_ai"},
}
ORDER_STATUSES = {
    "Не зібрано": "not_collected",
    "Зібрано": "collected",
    "Нове": "new",
    "В роботі": "working",
    "Надіслано": "sent",
    "Скасовано": "cancelled",
}
SOS_REASONS = ["Загроза / небезпека", "Конфлікт", "Крадіжка",
               "Медична ситуація", "Технічна аварія", "Інше"]
TICKET_CATEGORIES = ["Зарплата", "Графік", "Робоче питання",
                     "Конфлікт", "Пропозиція", "Інше"]
TICKET_STATUSES = ["Нове", "У роботі", "Очікує відповіді",
                   "Вирішено", "Закрито"]

def now_local():
    return datetime.now(TIMEZONE)

def now_text():
    return now_local().strftime("%Y-%m-%d %H:%M:%S")

def parse_dt(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TIMEZONE)
    except (TypeError, ValueError):
        return None

def esc(value):
    return html.escape(str(value or ""))

def log_event(event, user_id=None, details=None):
    logger.info("event=%s user_id=%s details=%s", event, user_id,
                json.dumps(details or {}, ensure_ascii=False, default=str))
    try:
        with db_connection() as conn:
            conn.execute("INSERT INTO audit_log(user_id,event,details,created_at) VALUES(?,?,?,?)",
                         (user_id, event, json.dumps(details or {}, ensure_ascii=False, default=str), now_text()))
    except Exception:
        logger.exception("audit write failed")

def log_exception(event, user_id=None):
    logger.exception("event=%s user_id=%s", event, user_id)

@contextmanager
def db_connection():
    with db_lock:
        conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

def table_columns(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
def init_db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with db_connection() as conn:
        conn.executescript(""" CREATE TABLE IF NOT EXISTS users ( user_id INTEGER PRIMARY KEY, username TEXT DEFAULT '', full_name TEXT DEFAULT '', display_name TEXT DEFAULT '', role TEXT DEFAULT '', is_blocked INTEGER DEFAULT 0, created_at TEXT DEFAULT '' ); CREATE TABLE IF NOT EXISTS orders ( id INTEGER PRIMARY KEY AUTOINCREMENT, order_number TEXT DEFAULT '', phone TEXT DEFAULT '', full_name TEXT DEFAULT '', city TEXT DEFAULT '', branch TEXT DEFAULT '', price REAL DEFAULT 0, note TEXT DEFAULT '', status TEXT DEFAULT 'Нове', ttn TEXT DEFAULT '', author TEXT DEFAULT '', created_at TEXT DEFAULT '', sent_at TEXT DEFAULT '', author_id INTEGER ); CREATE TABLE IF NOT EXISTS audit_log ( id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, event TEXT NOT NULL, details TEXT DEFAULT '{}', created_at TEXT NOT NULL ); CREATE TABLE IF NOT EXISTS user_permissions ( user_id INTEGER NOT NULL, permission TEXT NOT NULL, allowed INTEGER NOT NULL DEFAULT 1, PRIMARY KEY (user_id, permission) ); CREATE TABLE IF NOT EXISTS blocked_users ( user_id INTEGER PRIMARY KEY, blocked_by INTEGER, reason TEXT DEFAULT '', blocked_at TEXT DEFAULT '', created_at TEXT DEFAULT '' ); CREATE TABLE IF NOT EXISTS login_attempts ( user_id INTEGER PRIMARY KEY, attempts INTEGER DEFAULT 0, last_attempt TEXT DEFAULT '' ); CREATE TABLE IF NOT EXISTS shifts ( id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, shift_date TEXT NOT NULL, start_time TEXT NOT NULL, end_time TEXT NOT NULL, status TEXT DEFAULT 'planned', actual_start TEXT DEFAULT '', actual_end TEXT DEFAULT '', reminder_immediate INTEGER DEFAULT 0, reminder_30 INTEGER DEFAULT 0, reminder_5_start INTEGER DEFAULT 0, reminder_5_end INTEGER DEFAULT 0, created_by INTEGER, created_at TEXT DEFAULT '' ); CREATE TABLE IF NOT EXISTS sos_incidents ( id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, reason TEXT NOT NULL, description TEXT DEFAULT '', status TEXT DEFAULT 'active', created_at TEXT NOT NULL, acknowledged_by INTEGER, acknowledged_at TEXT DEFAULT '', closed_by INTEGER, closed_at TEXT DEFAULT '', false_alarm INTEGER DEFAULT 0, last_repeat_at TEXT DEFAULT '', repeat_count INTEGER DEFAULT 0 ); CREATE TABLE IF NOT EXISTS tickets ( id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, category TEXT NOT NULL, message TEXT NOT NULL, status TEXT DEFAULT 'Нове', created_at TEXT NOT NULL, updated_at TEXT DEFAULT '', assigned_to INTEGER ); CREATE TABLE IF NOT EXISTS ticket_replies ( id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL, user_id INTEGER NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL ); CREATE TABLE IF NOT EXISTS broadcast_log ( id INTEGER PRIMARY KEY AUTOINCREMENT, sender_id INTEGER NOT NULL, recipient_id INTEGER NOT NULL, message TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL ); CREATE TABLE IF NOT EXISTS ai_preferences ( user_id INTEGER PRIMARY KEY, psychologist_consent INTEGER DEFAULT 0 ); CREATE TABLE IF NOT EXISTS ai_conversations ( id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, mode TEXT NOT NULL DEFAULT 'assistant', role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL ); CREATE INDEX IF NOT EXISTS idx_ai_conversations_user ON ai_conversations(user_id, mode, id); CREATE TABLE IF NOT EXISTS daily_report_log ( report_date TEXT PRIMARY KEY, sent_at TEXT NOT NULL, pending_count INTEGER NOT NULL DEFAULT 0 ); CREATE TABLE IF NOT EXISTS daily_report_delivery ( report_date TEXT NOT NULL, user_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, last_attempt TEXT DEFAULT '', last_error TEXT DEFAULT '', PRIMARY KEY (report_date, user_id) ); CREATE TABLE IF NOT EXISTS ai_issue_categories ( id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, category TEXT DEFAULT '', created_at TEXT DEFAULT '' ); """)
        # Add missing columns to older installations without deleting records.
        migrations = {
            "users": {
                "username": "TEXT DEFAULT ''",
                "full_name": "TEXT DEFAULT ''",
                "display_name": "TEXT DEFAULT ''",
                "role": "TEXT DEFAULT ''",
                "is_blocked": "INTEGER DEFAULT 0",
                "created_at": "TEXT DEFAULT ''",
            },
            "orders": {
                "order_number": "TEXT DEFAULT ''", "phone": "TEXT DEFAULT ''",
                "full_name": "TEXT DEFAULT ''", "city": "TEXT DEFAULT ''",
                "branch": "TEXT DEFAULT ''", "price": "REAL DEFAULT 0",
                "note": "TEXT DEFAULT ''", "status": "TEXT DEFAULT 'Нове'",
                "ttn": "TEXT DEFAULT ''", "author": "TEXT DEFAULT ''",
                "created_at": "TEXT DEFAULT ''", "sent_at": "TEXT DEFAULT ''",
                "author_id": "INTEGER",
            },
            "blocked_users": {
                "blocked_by": "INTEGER", "reason": "TEXT DEFAULT ''",
                "blocked_at": "TEXT DEFAULT ''", "created_at": "TEXT DEFAULT ''",
            },
            "shifts": {
                "actual_start": "TEXT DEFAULT ''", "actual_end": "TEXT DEFAULT ''",
                "reminder_immediate": "INTEGER DEFAULT 0",
                "reminder_30": "INTEGER DEFAULT 0",
                "reminder_5_start": "INTEGER DEFAULT 0",
                "reminder_5_end": "INTEGER DEFAULT 0",
            },
            "sos_incidents": {
                "acknowledged_by": "INTEGER", "acknowledged_at": "TEXT DEFAULT ''",
                "closed_by": "INTEGER", "closed_at": "TEXT DEFAULT ''",
                "false_alarm": "INTEGER DEFAULT 0",
                "last_repeat_at": "TEXT DEFAULT ''", "repeat_count": "INTEGER DEFAULT 0",
            },
            "tickets": {
                "updated_at": "TEXT DEFAULT ''", "assigned_to": "INTEGER",
            },
        }
        for table, columns in migrations.items():
            existing = table_columns(conn, table)
            for column, definition in columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_number ON orders(order_number)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_phone ON orders(phone)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_shifts_date ON shifts(shift_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sos_status ON sos_incidents(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status)")

def get_user(user_id):
    with db_connection() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

def save_user(user_id, username="", full_name="", role=""):
    with db_connection() as conn:
        conn.execute(""" INSERT INTO users(user_id,username,full_name,role,created_at) VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name, role=CASE WHEN excluded.role!='' THEN excluded.role ELSE users.role END """, (user_id, username or "", full_name or "", role or "", now_text()))

def is_blocked(user_id):
    user = get_user(user_id)
    if user and user["is_blocked"]:
        return True
    with db_connection() as conn:
        return conn.execute("SELECT 1 FROM blocked_users WHERE user_id=?", (user_id,)).fetchone() is not None

def get_role(user_id):
    user = get_user(user_id)
    return user["role"] if user and user["role"] else ""

def has_permission(user_id, permission):
    # Block status always wins, including for owner callbacks.
    if is_blocked(user_id):
        return False
    role = get_role(user_id)
    if role == "owner":
        return True
    with db_connection() as conn:
        row = conn.execute(
            "SELECT allowed FROM user_permissions WHERE user_id=? AND permission=?",
            (user_id, permission)
        ).fetchone()
    if row:
        return bool(row["allowed"])
    return permission in ROLE_DEFAULT_PERMISSIONS.get(role, set())

def set_permission(user_id, permission, allowed):
    with db_connection() as conn:
        conn.execute(""" INSERT INTO user_permissions(user_id,permission,allowed) VALUES(?,?,?) ON CONFLICT(user_id,permission) DO UPDATE SET allowed=excluded.allowed """, (user_id, permission, int(bool(allowed))))
def get_all_owners():
    with db_connection() as conn:
        rows = conn.execute(
            "SELECT user_id FROM users WHERE role='owner' AND is_blocked=0"
        ).fetchall()
    return [int(row["user_id"]) for row in rows]

def notify_user(user_id, message, reply_markup=None):
    try:
        bot.send_message(int(user_id), message, reply_markup=reply_markup)
        return True
    except Exception as exc:
        logger.warning("Cannot notify user %s: %s", user_id, exc)
        return False

def notify_owners(message, reply_markup=None):
    sent = 0
    for owner_id in get_all_owners():
        if notify_user(owner_id, message, reply_markup):
            sent += 1
    return sent

def set_state(user_id, state, data=None):
    with state_lock:
        user_states[int(user_id)] = {
            "state": state,
            "data": data or {},
        }

def get_state(user_id):
    with state_lock:
        return user_states.get(int(user_id), {"state": "", "data": {}})

def clear_state(user_id):
    with state_lock:
        user_states.pop(int(user_id), None)

def get_state_data(user_id):
    return get_state(user_id).get("data", {})

def update_state_data(user_id, **kwargs):
    with state_lock:
        current = user_states.setdefault(int(user_id), {"state": "", "data": {}})
        current.setdefault("data", {}).update(kwargs)

def is_logged_in(user_id):
    return bool(get_role(user_id)) and not is_blocked(user_id)

def require_login(message):
    uid = message.from_user.id
    if is_blocked(uid):
        bot.send_message(message.chat.id, "⛔ Ваш доступ заблоковано.")
        return False
    if not is_logged_in(uid):
        bot.send_message(message.chat.id, "Спочатку авторизуйтесь командою /start.")
        return False
    return True

def require_permission(message, permission):
    if not require_login(message):
        return False
    if not has_permission(message.from_user.id, permission):
        bot.send_message(message.chat.id, "⛔ У вас немає права на цю дію.")
        return False
    return True

def main_keyboard(user_id):
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    options = []
    if has_permission(user_id, "create_order"):
        options.append("➕ Створити замовлення")
    if has_permission(user_id, "view_unsent_orders"):
        options.append("📦 Ненадіслані")
    if has_permission(user_id, "view_all_orders"):
        options.append("📋 Усі замовлення")
    if has_permission(user_id, "search_orders"):
        options.append("🔎 Пошук")
    if has_permission(user_id, "statistics"):
        options.append("📊 Статистика")
    if has_permission(user_id, "view_shifts"):
        options.append("🕒 Мої зміни")
    options.extend(["🚨 SOS OFFICE", "📨 Створити звернення"])
    if has_permission(user_id, "view_tickets"):
        options.append("🎫 Звернення")
    if has_permission(user_id, "manage_users"):
        options.append("👥 Користувачі")
    if has_permission(user_id, "manage_shifts"):
        options.append("🗓 Призначити зміни")
        options.append("📅 Графік команди")
    if has_permission(user_id, "broadcast"):
        options.append("📣 Розсилка")
    if has_permission(user_id, "use_ai"):
        options.append("🤖 AI-помічник")
    if has_permission(user_id, "use_psychologist"):
        options.append("💬 Психологічна підтримка")
    for option in options:
        keyboard.add(types.KeyboardButton(option))
    keyboard.add(types.KeyboardButton("🏠 Головне меню"))
    return keyboard

def show_home(chat_id, user_id, text="Головне меню"):
    bot.send_message(chat_id, text, reply_markup=main_keyboard(user_id))

def cancel_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.row("❌ Скасувати", "🏠 Головне меню")
    return keyboard

def cancel_or_home(text="Скасовано."):
    return text in {"❌ Скасувати", "🏠 Головне меню", "/cancel"}

def get_staff_users():
    # Only active operational users are eligible for shift assignment.
    with db_connection() as conn:
        return conn.execute(""" SELECT user_id,username,full_name,display_name,role FROM users WHERE role IN ('manager','staff','collector','shipper') AND is_blocked=0 ORDER BY role, COALESCE(NULLIF(display_name,''),full_name,username), user_id """).fetchall()

def user_label(user):
    if not user:
        return "Невідомий користувач"
    return (user["display_name"] or user["full_name"] or
            (f"@{user['username']}" if user["username"] else str(user["user_id"])))

def user_label_by_id(user_id):
    return user_label(get_user(user_id))


def format_user(user):
    name = user_label(user)
    role = ROLE_LABELS.get(user["role"], user["role"] or "Не авторизований")
    return f"{esc(name)} · {esc(role)} · ID <code>{user['user_id']}</code>"

def audit_order_action(user_id, action, order_id, details=None):
    payload = {"order_id": order_id, "action": action}
    if details:
        payload.update(details)
    log_event("order_" + action, user_id, payload)

def get_order(order_id):
    with db_connection() as conn:
        return conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()

def can_delete_order(user_id, order):
    if get_role(user_id) in {"owner", "manager"} and has_permission(user_id, "delete_any_order"):
        return True
    if not has_permission(user_id, "delete_own_order"):
        return False
    # Legacy records without author_id cannot safely be assigned to a user.
    return order["author_id"] is not None and int(order["author_id"]) == int(user_id)

def order_summary(order):
    return (
        f"📦 <b>Замовлення #{order['id']}</b>\n"
        f"Номер: {esc(order['order_number'])}\n"
        f"Клієнт: {esc(order['full_name'])}\n"
        f"Телефон: {esc(order['phone'])}\n"
        f"Місто: {esc(order['city'])}\n"
        f"Відділе