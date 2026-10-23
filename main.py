
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
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            full_name TEXT DEFAULT '',
            display_name TEXT DEFAULT '',
            role TEXT DEFAULT '',
            is_blocked INTEGER DEFAULT 0,
            created_at TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_number TEXT DEFAULT '',
            phone TEXT DEFAULT '',
            full_name TEXT DEFAULT '',
            city TEXT DEFAULT '',
            branch TEXT DEFAULT '',
            price REAL DEFAULT 0,
            note TEXT DEFAULT '',
            status TEXT DEFAULT 'Нове',
            ttn TEXT DEFAULT '',
            author TEXT DEFAULT '',
            created_at TEXT DEFAULT '',
            sent_at TEXT DEFAULT '',
            author_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            event TEXT NOT NULL,
            details TEXT DEFAULT '{}',
            created_at TEXT NOT NULL

        );
        CREATE TABLE IF NOT EXISTS user_permissions (
            user_id INTEGER NOT NULL,
            permission TEXT NOT NULL,
            allowed INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (user_id, permission)
        );
        CREATE TABLE IF NOT EXISTS blocked_users (
            user_id INTEGER PRIMARY KEY,
            blocked_by INTEGER,
            reason TEXT DEFAULT '',
            blocked_at TEXT DEFAULT '',
            created_at TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS login_attempts (
            user_id INTEGER PRIMARY KEY,
            attempts INTEGER DEFAULT 0,
            last_attempt TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS shifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            shift_date TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            status TEXT DEFAULT 'planned',
            actual_start TEXT DEFAULT '',
            actual_end TEXT DEFAULT '',
            reminder_immediate INTEGER DEFAULT 0,
            reminder_30 INTEGER DEFAULT 0,
            reminder_5_start INTEGER DEFAULT 0,
            reminder_5_end INTEGER DEFAULT 0,
            created_by INTEGER,
            created_at TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS sos_incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            description TEXT DEFAULT '',
            status TEXT DEFAULT 'active',
            created_at TEXT NOT NULL,
            acknowledged_by INTEGER,
            acknowledged_at TEXT DEFAULT '',
            closed_by INTEGER,
            closed_at TEXT DEFAULT '',
            false_alarm INTEGER DEFAULT 0,
            last_repeat_at TEXT DEFAULT '',
            repeat_count INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            message TEXT NOT NULL,
            status TEXT DEFAULT 'Нове',
            created_at TEXT NOT NULL,
            updated_at TEXT DEFAULT '',
            assigned_to INTEGER
        );
        CREATE TABLE IF NOT EXISTS ticket_replies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS broadcast_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER NOT NULL,
            recipient_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ai_preferences (
            user_id INTEGER PRIMARY KEY,
            psychologist_consent INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS ai_conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            mode TEXT NOT NULL DEFAULT 'assistant',
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ai_conversations_user ON ai_conversations(user_id, mode, id);
        CREATE TABLE IF NOT EXISTS daily_report_log (
            report_date TEXT PRIMARY KEY,
            sent_at TEXT NOT NULL,
            pending_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS daily_report_delivery (
            report_date TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_attempt TEXT DEFAULT '',
            last_error TEXT DEFAULT '',
            PRIMARY KEY (report_date, user_id)
        );
        CREATE TABLE IF NOT EXISTS ai_issue_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            category TEXT DEFAULT '',
            created_at TEXT DEFAULT ''
        );
        """)
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
        conn.execute("""
            INSERT INTO users(user_id,username,full_name,role,created_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            full_name=excluded.full_name,
            role=CASE WHEN excluded.role!='' THEN excluded.role ELSE users.role END
        """, (user_id, username or "", full_name or "", role or "", now_text()))

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
        conn.execute("""
            INSERT INTO user_permissions(user_id,permission,allowed) VALUES(?,?,?)
            ON CONFLICT(user_id,permission) DO UPDATE SET allowed=excluded.allowed
        """, (user_id, permission, int(bool(allowed))))
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
        return conn.execute("""
            SELECT user_id,username,full_name,display_name,role FROM users
            WHERE role IN ('manager','staff','collector','shipper') AND is_blocked=0
            ORDER BY role, COALESCE(NULLIF(display_name,''),full_name,username), user_id
        """).fetchall()

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
        f"Відділення: {esc(order['branch'])}\n"
        f"Сума: {esc(order['price'])}\n"
        f"Статус: {esc(order['status'])}\n"
        f"ТТН: {esc(order['ttn'])}\n"
        f"Примітка: {esc(order['note'])}\n"
        f"Автор: {esc(order['author'])}\n"
        f"Створено: {esc(order['created_at'])}"
    )

def orders_page(query=None, unsent=False, limit=50):
    sql = "SELECT * FROM orders"
    clauses, args = [], []
    if unsent:
        clauses.append("status NOT IN ('Надіслано','Скасовано')")
    if query:
        clauses.append("(order_number LIKE ? OR full_name LIKE ? OR phone LIKE ?)")
        pattern = f"%{query}%"
        args.extend([pattern, pattern, pattern])
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    with db_connection() as conn:
        return conn.execute(sql, args).fetchall()
def create_order_record(user_id, data):
    required = ("order_number", "phone", "full_name", "city", "branch", "price")
    for field in required:
        if not str(data.get(field, "")).strip():
            raise ValueError(f"Не заповнене поле: {field}")

    try:
        price = float(str(data["price"]).replace(",", "."))
        if price < 0:
            raise ValueError("Сума не може бути від'ємною.")
    except (ValueError, TypeError):
        raise ValueError("Сума має бути числом.")

    role = get_role(user_id)
    author = ROLE_LABELS.get(role, role)
    with db_connection() as conn:
        cursor = conn.execute("""
            INSERT INTO orders (
                order_number, phone, full_name, city, branch, price, note,
                status, ttn, author, created_at, sent_at, author_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'Нове', '', ?, ?, '', ?)
        """, (
            str(data["order_number"]).strip(),
            str(data["phone"]).strip(),
            str(data["full_name"]).strip(),
            str(data["city"]).strip(),
            str(data["branch"]).strip(),
            price,
            str(data.get("note", "")).strip(),
            author,
            now_text(),
            int(user_id),
        ))
        order_id = cursor.lastrowid

    audit_order_action(user_id, "created", order_id)
    return order_id


def update_order_status(order_id, status, user_id):
    """Validate authorization and business rules at the data mutation boundary."""
    if status not in ORDER_STATUSES:
        raise ValueError("Невідомий статус замовлення.")
    if not is_logged_in(user_id):
        raise PermissionError("Потрібна авторизація.")

    role = get_role(user_id)
    if role == "owner":
        allowed = True
    elif status == "Зібрано":
        allowed = role == "collector" or has_permission(user_id, "manage_order_status")
    elif status == "Надіслано":
        allowed = role == "shipper" or has_permission(user_id, "manage_order_status")
    elif status in ("Нове", "В роботі", "Не зібрано", "Скасовано"):
        allowed = has_permission(user_id, "manage_order_status")
    else:
        allowed = False
    if not allowed:
        raise PermissionError("У вас немає права встановлювати цей статус.")

    with db_connection() as conn:
        order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            return False
        if status == "Надіслано" and not str(order["ttn"] or "").strip():
            raise ValueError("Не можна позначити замовлення надісланим без ТТН.")
        if status == "Зібрано" and order["status"] == "Скасовано":
            raise ValueError("Скасоване замовлення не можна позначити зібраним.")
        sent_at = now_text() if status == "Надіслано" else order["sent_at"]
        conn.execute("UPDATE orders SET status=?, sent_at=? WHERE id=?", (status, sent_at, order_id))

    audit_order_action(user_id, "status_changed", order_id, {"status": status})
    return True


def update_order_ttn(order_id, ttn, user_id):
    ttn = str(ttn or "").strip()
    if not ttn:
        raise ValueError("ТТН не може бути порожньою.")
    with db_connection() as conn:
        order = conn.execute(
            "SELECT id,status FROM orders WHERE id=?", (order_id,)
        ).fetchone()
        if not order:
            return False
        if order["status"] not in ("Зібрано", "В роботі", "Надіслано"):
            raise PermissionError("Спочатку збирач має позначити замовлення «Зібрано».")
        conn.execute(
            "UPDATE orders SET ttn=? WHERE id=?",
            (ttn, order_id),
        )

    audit_order_action(user_id, "ttn_updated", order_id)
    return True


def delete_order_record(order_id, user_id):
    with db_connection() as conn:
        order = conn.execute(
            "SELECT * FROM orders WHERE id=?", (order_id,)
        ).fetchone()

        if not order:
            return False, "Замовлення не знайдено."

        if not can_delete_order(user_id, order):
            return False, "⛔ Немає прав на видалення цього замовлення."

        snapshot = {
            "order_number": order["order_number"],
            "full_name": order["full_name"],
            "phone": order["phone"],
            "price": order["price"],
            "status": order["status"],
        }

        conn.execute("DELETE FROM orders WHERE id=?", (order_id,))

    audit_order_action(user_id, "deleted", order_id, snapshot)
    return True, "Замовлення видалено."


def show_orders(chat_id, user_id, unsent=False, query=None):
    if unsent and not has_permission(user_id, "view_unsent_orders"):
        bot.send_message(chat_id, "⛔ Немає доступу до ненадісланих замовлень.")
        return

    if not unsent and query is None and not has_permission(user_id, "view_all_orders"):
        bot.send_message(chat_id, "⛔ Немає доступу до всіх замовлень.")
        return

    if query is not None and not has_permission(user_id, "search_orders"):
        bot.send_message(chat_id, "⛔ Немає доступу до пошуку.")
        return

    orders = orders_page(query=query, unsent=unsent, limit=50)
    if not orders:
        bot.send_message(chat_id, "Замовлень не знайдено.")
        return

    for order in orders:
        keyboard = types.InlineKeyboardMarkup(row_width=2)
        keyboard.add(
            types.InlineKeyboardButton(
                "🔄 Статус", callback_data=f"order_status:{order['id']}"
            ),
            types.InlineKeyboardButton(
                "🧾 Додати ТТН", callback_data=f"order_ttn:{order['id']}"
            ),
        )
        if can_delete_order(user_id, order):
            keyboard.add(types.InlineKeyboardButton(
                "🗑 Видалити", callback_data=f"order_delete:{order['id']}"

            ))
        bot.send_message(
            chat_id, order_summary(order), reply_markup=keyboard
        )


def begin_order_creation(message):
    uid = message.from_user.id
    if not require_permission(message, "create_order"):
        return
    set_state(uid, "order_number", {})
    bot.send_message(
        message.chat.id,
        "Введи номер замовлення:",
        reply_markup=cancel_keyboard(),
    )


def advance_order_creation(message):
    uid = message.from_user.id
    state = get_state(uid)
    current = state.get("state")
    data = state.get("data", {})
    value = (message.text or "").strip()

    fields = {
        "order_number": ("phone", "Введи телефон клієнта:"),
        "phone": ("full_name", "Введи ім'я клієнта:"),
        "full_name": ("city", "Введи місто:"),
        "city": ("branch", "Введи номер або адресу відділення:"),
        "branch": ("price", "Введи суму замовлення:"),
        "price": ("note", "Введи примітку або напиши «-»:"),
    }

    if current not in fields:
        return False

    if not value:
        bot.send_message(message.chat.id, "Поле не може бути порожнім.")
        return True

    if current == "price":
        try:
            amount = float(value.replace(",", "."))
            if amount < 0:
                raise ValueError
        except ValueError:
            bot.send_message(message.chat.id, "Введи коректну невід'ємну суму.")
            return True

    data[current] = "" if value == "-" else value
    next_state, prompt = fields[current]
    if current == "price":
        data["price"] = value
    set_state(uid, next_state, data)

    if next_state == "note":
        bot.send_message(message.chat.id, prompt, reply_markup=cancel_keyboard())
        return True

    if next_state == "note":
        return True

    if next_state == "price":
        bot.send_message(message.chat.id, prompt, reply_markup=cancel_keyboard())
        return True

    bot.send_message(message.chat.id, prompt, reply_markup=cancel_keyboard())
    return True


def finish_order_creation(message):
    uid = message.from_user.id
    state = get_state(uid)
    if state.get("state") != "note":
        return False

    data = dict(state.get("data", {}))
    note = (message.text or "").strip()
    data["note"] = "" if note == "-" else note

    try:
        order_id = create_order_record(uid, data)
    except ValueError as exc:
        bot.send_message(message.chat.id, str(exc))
        return True
    except Exception:
        logger.exception("Order creation failed")
        bot.send_message(message.chat.id, "Не вдалося створити замовлення. Перевір журнал бота.")
        return True

    clear_state(uid)
    bot.send_message(
        message.chat.id,
        f"✅ Замовлення #{order_id} створено.",
        reply_markup=main_keyboard(uid),
    )
    return True
@bot.message_handler(commands=["start"])
def handle_start(message):
    uid = message.from_user.id
    user = get_user(uid)

    if is_blocked(uid):
        bot.send_message(message.chat.id, "⛔ Ваш доступ заблоковано.")
        return

    if user and user["role"]:
        show_home(message.chat.id, uid, "Вітаю в Cloud Hub CRM!")
        return

    set_state(uid, "login", {})
    bot.send_message(
        message.chat.id,
        "🔐 <b>Cloud Hub CRM</b>\nВведи пароль доступу:",
        reply_markup=types.ReplyKeyboardRemove(),
    )


@bot.message_handler(commands=["cancel"])
def handle_cancel(message):
    uid = message.from_user.id
    clear_state(uid)
    if is_logged_in(uid):
        show_home(message.chat.id, uid, "Дію скасовано.")
    else:
        bot.send_message(message.chat.id, "Дію скасовано. Для входу введи /start.")


@bot.message_handler(func=lambda m: m.text == "🏠 Головне меню")
def handle_home_button(message):
    uid = message.from_user.id
    clear_state(uid)
    if is_logged_in(uid):
        show_home(message.chat.id, uid)
    else:
        bot.send_message(message.chat.id, "Для входу введи /start.")


@bot.message_handler(func=lambda m: m.text == "❌ Скасувати")
def handle_cancel_button(message):
    handle_cancel(message)


@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("state") == "login")
def handle_login(message):
    uid = message.from_user.id
    password = (message.text or "").strip()

    if is_blocked(uid):
        clear_state(uid)
        bot.send_message(message.chat.id, "⛔ Ваш доступ заблоковано.")
        return

    role = PASSWORDS_MAP.get(password)
    if not role:
        with db_connection() as conn:
            row = conn.execute(
                "SELECT attempts FROM login_attempts WHERE user_id=?", (uid,)
            ).fetchone()
            attempts = (row["attempts"] if row else 0) + 1
            conn.execute("""
                INSERT INTO login_attempts(user_id,attempts,last_attempt)
                VALUES(?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                attempts=excluded.attempts,last_attempt=excluded.last_attempt
            """, (uid, attempts, now_text()))
        bot.send_message(message.chat.id, "❌ Невірний пароль. Спробуй ще раз або /cancel.")
        return

    username = message.from_user.username or ""
    full_name = " ".join(filter(None, [
        message.from_user.first_name, message.from_user.last_name
    ]))
    save_user(uid, username, full_name, role)

    with db_connection() as conn:
        conn.execute("DELETE FROM login_attempts WHERE user_id=?", (uid,))

    clear_state(uid)
    log_event("login_success", uid, {"role": role})
    bot.send_message(

        message.chat.id,
        f"✅ Авторизація успішна. Роль: <b>{esc(ROLE_LABELS[role])}</b>",
        reply_markup=main_keyboard(uid),
    )


@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("state") == "order_number")
def handle_order_number(message):
    advance_order_creation(message)


@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("state") == "phone")
def handle_order_phone(message):
    advance_order_creation(message)


@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("state") == "full_name")
def handle_order_name(message):
    advance_order_creation(message)


@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("state") == "city")
def handle_order_city(message):
    advance_order_creation(message)


@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("state") == "branch")
def handle_order_branch(message):
    advance_order_creation(message)


@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("state") == "price")
def handle_order_price(message):
    advance_order_creation(message)


@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("state") == "note")
def handle_order_note(message):
    finish_order_creation(message)


@bot.message_handler(func=lambda m: m.text == "➕ Створити замовлення")
def handle_create_order_button(message):
    begin_order_creation(message)


@bot.message_handler(func=lambda m: m.text == "📦 Ненадіслані")
def handle_unsent_orders(message):
    if require_permission(message, "view_unsent_orders"):
        show_orders(message.chat.id, message.from_user.id, unsent=True)


@bot.message_handler(func=lambda m: m.text == "📋 Усі замовлення")
def handle_all_orders(message):
    if require_permission(message, "view_all_orders"):
        show_orders(message.chat.id, message.from_user.id)


@bot.message_handler(func=lambda m: m.text == "🔎 Пошук")
def handle_search_button(message):
    if require_permission(message, "search_orders"):
        set_state(message.from_user.id, "search_orders", {})
        bot.send_message(
            message.chat.id,
            "Введи номер замовлення, ім'я або телефон:",
            reply_markup=cancel_keyboard(),
        )


@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("state") == "search_orders")
def handle_search_query(message):
    uid = message.from_user.id
    query = (message.text or "").strip()
    if not query:
        bot.send_message(message.chat.id, "Введи текст для пошуку.")
        return
    clear_state(uid)
    show_orders(message.chat.id, uid, query=query)
    show_home(message.chat.id, uid, "Пошук завершено.")
@bot.callback_query_handler(func=lambda c: c.data.startswith("order_status:"))
def callback_order_status(call):
    uid = call.from_user.id
    if not is_logged_in(uid):
        bot.answer_callback_query(call.id, "Спочатку авторизуйся.")
        return

    order_id = int(call.data.split(":", 1)[1])
    order = get_order(order_id)
    if not order:
        bot.answer_callback_query(call.id, "Замовлення не знайдено.")
        return

    if not (has_permission(uid, "view_all_orders") or
            has_permission(uid, "view_unsent_orders")):
        bot.answer_callback_query(call.id, "Немає доступу.")
        return

    keyboard = types.InlineKeyboardMarkup(row_width=2)
    for status in ORDER_STATUSES:
        keyboard.add(types.InlineKeyboardButton(
            status, callback_data=f"set_status:{order_id}:{status}"
        ))
    bot.send_message(call.message.chat.id, "Обери новий статус:", reply_markup=keyboard)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("set_status:"))
def callback_set_status(call):
    uid = call.from_user.id
    if not is_logged_in(uid):
        bot.answer_callback_query(call.id, "Спочатку авторизуйся.")
        return

    try:
        _, order_id, status = call.data.split(":", 2)
        order_id = int(order_id)
        if not update_order_status(order_id, status, uid):
            bot.answer_callback_query(call.id, "Замовлення не знайдено.")
            return
        bot.answer_callback_query(call.id, "Статус оновлено.")
        bot.send_message(call.message.chat.id, f"✅ Статус замовлення #{order_id}: {esc(status)}")
    except PermissionError as exc:
        bot.answer_callback_query(call.id, str(exc)[:180], show_alert=True)
    except ValueError as exc:
        bot.answer_callback_query(call.id, str(exc)[:180], show_alert=True)
    except Exception:
        logger.exception("Status update failed")
        bot.answer_callback_query(call.id, "Не вдалося змінити статус.", show_alert=True)


@bot.callback_query_handler(func=lambda c: c.data.startswith("order_ttn:"))
def callback_order_ttn(call):
    uid = call.from_user.id
    if not is_logged_in(uid):
        bot.answer_callback_query(call.id, "Спочатку авторизуйся.")
        return

    try:
        order_id = int(call.data.split(":", 1)[1])
        if get_role(uid) not in ("owner", "manager", "shipper") and not has_permission(uid, "manage_users"):
            bot.answer_callback_query(call.id, "Немає права додавати ТТН.", show_alert=True)
            return
        if not get_order(order_id):
            bot.answer_callback_query(call.id, "Замовлення не знайдено.")
            return
        set_state(uid, "order_ttn", {"order_id": order_id})
        bot.send_message(
            call.message.chat.id,
            f"Введи ТТН для замовлення #{order_id}:",
            reply_markup=cancel_keyboard(),
        )
        bot.answer_callback_query(call.id)
    except Exception:
        logger.exception("TTN callback failed")
        bot.answer_callback_query(call.id, "Помилка.")

@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("state") == "order_ttn")
def handle_order_ttn_input(message):
    uid = message.from_user.id
    state = get_state(uid)
    order_id = state.get("data", {}).get("order_id")
    ttn = (message.text or "").strip()

    if not ttn:
        bot.send_message(message.chat.id, "ТТН не може бути порожньою.")
        return

    if not update_order_ttn(order_id, ttn, uid):
        bot.send_message(message.chat.id, "Замовлення не знайдено.")
        clear_state(uid)
        return

    clear_state(uid)
    bot.send_message(
        message.chat.id,
        f"✅ ТТН для замовлення #{order_id} збережено.",
        reply_markup=main_keyboard(uid),
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("order_delete:"))

def callback_order_delete(call):
    uid = call.from_user.id
    if not is_logged_in(uid):
        bot.answer_callback_query(call.id, "Спочатку авторизуйся.")
        return

    try:
        order_id = int(call.data.split(":", 1)[1])
        order = get_order(order_id)
        if not order:
            bot.answer_callback_query(call.id, "Замовлення не знайдено.")
            return
        if not can_delete_order(uid, order):
            bot.answer_callback_query(call.id, "Немає прав на видалення.")
            return

        keyboard = types.InlineKeyboardMarkup()
        keyboard.row(
            types.InlineKeyboardButton(
                "Так, видалити", callback_data=f"confirm_delete:{order_id}"
            ),
            types.InlineKeyboardButton(
                "Скасувати", callback_data="cancel_delete"
            ),
        )
        bot.send_message(
            call.message.chat.id,
            f"⚠️ Точно видалити замовлення #{order_id}?",
            reply_markup=keyboard,
        )
        bot.answer_callback_query(call.id)
    except Exception:
        logger.exception("Delete callback failed")
        bot.answer_callback_query(call.id, "Помилка.")


@bot.callback_query_handler(func=lambda c: c.data.startswith("confirm_delete:"))
def callback_confirm_delete(call):
    uid = call.from_user.id
    if not is_logged_in(uid):
        bot.answer_callback_query(call.id, "Спочатку авторизуйся.")
        return

    try:
        order_id = int(call.data.split(":", 1)[1])
        success, result = delete_order_record(order_id, uid)
        bot.answer_callback_query(call.id, "Готово" if success else "Відмовлено")
        bot.send_message(call.message.chat.id, result)
    except Exception:
        logger.exception("Delete confirmation failed")
        bot.answer_callback_query(call.id, "Не вдалося видалити.")


@bot.callback_query_handler(func=lambda c: c.data == "cancel_delete")
def callback_cancel_delete(call):
    bot.answer_callback_query(call.id, "Видалення скасовано.")
    try:
        bot.edit_message_reply_markup(
            call.message.chat.id, call.message.message_id, reply_markup=None
        )
    except Exception:
        pass
def get_statistics(day=None):
    day = day or now_local().strftime("%Y-%m-%d")
    with db_connection() as conn:
        orders = conn.execute("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status='Надіслано' THEN 1 ELSE 0 END) AS sent,
                   COALESCE(SUM(price),0) AS revenue
            FROM orders WHERE substr(created_at,1,10)=?
        """, (day,)).fetchone()

        shifts = conn.execute("""
            SELECT s.*, u.full_name, u.username
            FROM shifts s LEFT JOIN users u ON u.user_id=s.user_id
            WHERE s.shift_date=? ORDER BY s.start_time
        """, (day,)).fetchall()

    return {
        "day": day,
        "orders": int(orders["total"] or 0),
        "sent": int(orders["sent"] or 0),
        "revenue": float(orders["revenue"] or 0),
        "shifts": shifts,
    }


def format_statistics(day=None):
    stats = get_statistics(day)
    lines = [
        f"📊 <b>Статистика за {esc(stats['day'])}</b>",
        f"Замовлень: <b>{stats['orders']}</b>",
        f"Надіслано: <b>{stats['sent']}</b>",
        f"Сума замовлень: <b>{stats['revenue']:.2f}</b>",
        "",
        "<b>Зміни:</b>",
    ]

    if not stats["shifts"]:
        lines.append("Змін на цю дату немає.")
    else:
        for shift in stats["shifts"]:
            name = shift["full_name"] or shift["username"] or str(shift["user_id"])
            lines.append(
                f"• {esc(name)}: {esc(shift['start_time'])}–{esc(shift['end_time'])}; "
                f"статус: {esc(shift['status'])}"
            )
            if shift["actual_start"] or shift["actual_end"]:
                lines.append(
                    f"  Фактично: {esc(shift['actual_start'] or 'не почато')} – "
                    f"{esc(shift['actual_end'] or 'ще триває')}"
                )

    return "\n".join(lines)


@bot.message_handler(func=lambda m: m.text == "📊 Статистика")
def handle_statistics(message):
    uid = message.from_user.id
    if not require_permission(message, "statistics"):
        return

    set_state(uid, "statistics_date", {})
    bot.send_message(
        message.chat.id,
        "Введи дату у форматі YYYY-MM-DD або напиши «сьогодні»:",
        reply_markup=cancel_keyboard(),
    )


@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("state") == "statistics_date")
def handle_statistics_date(message):
    uid = message.from_user.id
    raw = (message.text or "").strip().lower()

    if raw == "сьогодні":
        day = now_local().strftime("%Y-%m-%d")
    else:
        try:
            day = datetime.strptime(raw, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            bot.send_message(message.chat.id, "Неправильний формат. Приклад: 2026-10-23")
            return

    clear_state(uid)
    bot.send_message(
        message.chat.id,
        format_statistics(day),
        reply_markup=main_keyboard(uid),
    )


def get_user_shifts(user_id, include_history=True):
    sql = "SELECT * FROM shifts WHERE user_id=?"
    args = [user_id]
    if not include_history:
        sql += " AND shift_date>=?"
        args.append(now_local().strftime("%Y-%m-%d"))
    sql += " ORDER BY shift_date DESC,start_time DESC LIMIT 100"

    with db_connection() as conn:
        return conn.execute(sql, args).fetchall()


def format_shift(shift):
    return (
        f"🗓 <b>Зміна #{shift['id']}</b>\n"
        f"Дата: {esc(shift['shift_date'])}\n"
        f"План: {esc(shift['start_time'])}–{esc(shift['end_time'])}\n"
        f"Статус: {esc(shift['status'])}\n"
        f"Початок фактично: {esc(shift['actual_start'] or 'не зафіксовано')}\n"
        f"Кінець фактично: {esc(shift['actual_end'] or 'не зафіксовано')}"
    )


@bot.message_handler(func=lambda m: m.text == "🕒 Мої зміни")
def handle_my_shifts(message):
    uid = message.from_user.id
    if not require_permission(message, "view_shifts"):
        return

    shifts = get_user_shifts(uid)

    if not shifts:
        bot.send_message(message.chat.id, "У тебе поки немає призначених змін.")
        return

    for shift in shifts:
        keyboard = types.InlineKeyboardMarkup(row_width=2)
        if shift["status"] == "planned":
            keyboard.add(types.InlineKeyboardButton(
                "▶️ Почати зміну", callback_data=f"shift_start:{shift['id']}"
            ))
        elif shift["status"] == "started":
            keyboard.add(types.InlineKeyboardButton(
                "⏹ Завершити зміну", callback_data=f"shift_end:{shift['id']}"
            ))
        bot.send_message(
            message.chat.id, format_shift(shift), reply_markup=keyboard
        )


@bot.callback_query_handler(func=lambda c: c.data.startswith("shift_start:"))
def callback_shift_start(call):
    uid = call.from_user.id
    if not is_logged_in(uid):
        bot.answer_callback_query(call.id, "Спочатку авторизуйся.")
        return

    try:
        shift_id = int(call.data.split(":", 1)[1])
        with db_connection() as conn:
            shift = conn.execute(
                "SELECT * FROM shifts WHERE id=?", (shift_id,)
            ).fetchone()

            if not shift or int(shift["user_id"]) != uid:
                bot.answer_callback_query(call.id, "Це не твоя зміна.")
                return

            if shift["status"] != "planned":
                bot.answer_callback_query(call.id, "Зміну вже почато або завершено.")
                return

            conn.execute(
                "UPDATE shifts SET status='started',actual_start=? WHERE id=?",
                (now_text(), shift_id),
            )

        log_event("shift_started", uid, {"shift_id": shift_id})
        notify_owners(
            f"▶️ {esc(user_label_by_id(uid))} почав зміну #{shift_id} о {esc(now_text())}."
        )
        bot.answer_callback_query(call.id, "Зміну розпочато.")
        bot.send_message(call.message.chat.id, "▶️ Зміну розпочато.")
    except Exception:
        logger.exception("shift start failed")
        bot.answer_callback_query(call.id, "Не вдалося почати зміну.")


@bot.callback_query_handler(func=lambda c: c.data.startswith("shift_end:"))
def callback_shift_end(call):
    uid = call.from_user.id
    if not is_logged_in(uid):
        bot.answer_callback_query(call.id, "Спочатку авторизуйся.")
        return

    try:
        shift_id = int(call.data.split(":", 1)[1])
        with db_connection() as conn:
            shift = conn.execute(
                "SELECT * FROM shifts WHERE id=?", (shift_id,)
            ).fetchone()

            if not shift or int(shift["user_id"]) != uid:
                bot.answer_callback_query(call.id, "Це не твоя зміна.")
                return

            if shift["status"] != "started":
                bot.answer_callback_query(call.id, "Зміна не позначена як розпочата.")
                return

            conn.execute(
                "UPDATE shifts SET status='completed',actual_end=? WHERE id=?",
                (now_text(), shift_id),
            )

        log_event("shift_ended", uid, {"shift_id": shift_id})
        notify_owners(
            f"⏹ {esc(user_label_by_id(uid))} завершив зміну #{shift_id} о {esc(now_text())}."
        )
        bot.answer_callback_query(call.id, "Зміну завершено.")
        bot.send_message(call.message.chat.id, "⏹ Зміну завершено.")
    except Exception:
        logger.exception("shift end failed")
        bot.answer_callback_query(call.id, "Не вдалося завершити зміну.")
@bot.message_handler(func=lambda m: m.text in ("🗓 Керування змінами", "🗓 Призначити зміни"))
def handle_manage_shifts(message):
    uid = message.from_user.id
    if not require_permission(message, "manage_shifts"):
        return

    users = get_staff_users()
    if not users:
        bot.send_message(message.chat.id, "Спочатку працівники мають авторизуватися в боті.")
        return

    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for user in users:
        name = user["full_name"] or user["username"] or str(user["user_id"])
        keyboard.add(types.InlineKeyboardButton(
            f"{name} ({ROLE_LABELS.get(user['role'], user['role'])})",
            callback_data=f"shift_assign:{user['user_id']}"
        ))
    bot.send_message(message.chat.id, "Обери працівника для призначення зміни:", reply_markup=keyboard)


@bot.message_handler(func=lambda m: m.text == "📅 Графік команди")
def handle_team_schedule(message):
    uid = message.from_user.id
    if not require_permission(message, "manage_shifts"):
        return
    with db_connection() as conn:
        shifts = conn.execute("""
            SELECT s.*,u.username,u.full_name,u.display_name,u.role
            FROM shifts s JOIN users u ON u.user_id=s.user_id
            WHERE s.shift_date>=? AND s.status!='cancelled'
            ORDER BY s.shift_date,s.start_time LIMIT 100
        """, (now_local().strftime("%Y-%m-%d"),)).fetchall()
    if not shifts:
        bot.send_message(message.chat.id, "На майбутні дати змін поки немає.")
        return
    for shift in shifts:
        name = user_label(shift)
        keyboard = types.InlineKeyboardMarkup()
        if shift["status"] == "planned":
            keyboard.add(types.InlineKeyboardButton("❌ Скасувати зміну", callback_data=f"shift_cancel:{shift['id']}"))
        bot.send_message(message.chat.id,
            f"{format_shift(shift)}\nПрацівник: <b>{esc(name)}</b> · {esc(ROLE_LABELS.get(shift['role'], shift['role']))}",
            reply_markup=keyboard if shift["status"] == "planned" else None)

@bot.callback_query_handler(func=lambda c: c.data.startswith("shift_assign:"))
def callback_shift_assign(call):
    uid = call.from_user.id
    if not is_logged_in(uid) or not has_permission(uid, "manage_shifts"):
        bot.answer_callback_query(call.id, "Немає доступу.")
        return

    try:
        employee_id = int(call.data.split(":", 1)[1])
        employee = get_user(employee_id)
        if not employee or not employee["role"] or is_blocked(employee_id):
            bot.answer_callback_query(call.id, "Працівника не знайдено або він заблокований.")
            return

        set_state(uid, "shift_date", {"employee_id": employee_id})
        bot.send_message(
            call.message.chat.id,
            "Введи дату зміни у форматі YYYY-MM-DD:",
            reply_markup=cancel_keyboard()
        )
        bot.answer_callback_query(call.id)
    except Exception:
        logger.exception("Shift assignment callback failed")
        bot.answer_callback_query(call.id, "Помилка.")


@bot.callback_query_handler(func=lambda c: c.data.startswith("shift_cancel:"))
def callback_shift_cancel(call):
    uid = call.from_user.id
    if not is_logged_in(uid) or not has_permission(uid, "manage_shifts"):
        bot.answer_callback_query(call.id, "Немає доступу.", show_alert=True)
        return
    try:
        shift_id = int(call.data.split(":", 1)[1])
        with db_connection() as conn:
            shift = conn.execute("SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone()
            if not shift:
                bot.answer_callback_query(call.id, "Зміну не знайдено.", show_alert=True)
                return
            if shift["status"] != "planned":
                bot.answer_callback_query(call.id, "Можна скасувати лише ще не розпочату зміну.", show_alert=True)
                return
            conn.execute("UPDATE shifts SET status='cancelled' WHERE id=? AND status='planned'", (shift_id,))
        log_event("shift_cancelled", uid, {"shift_id": shift_id, "employee_id": int(shift["user_id"])})

        delivered = notify_user(int(shift["user_id"]),
            f"❌ <b>Зміну #{shift_id} скасовано</b>\nДата: {esc(shift['shift_date'])}\nЧас: {esc(shift['start_time'])}–{esc(shift['end_time'])}.")
        bot.answer_callback_query(call.id, "Зміну скасовано.")
        bot.send_message(call.message.chat.id, "Зміну скасовано." + (" Працівника повідомлено." if delivered else " Попередження: працівнику не вдалося надіслати повідомлення."))
    except Exception:
        logger.exception("Shift cancellation failed")
        bot.answer_callback_query(call.id, "Не вдалося скасувати зміну.", show_alert=True)


@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("state") == "shift_date")
def handle_shift_date(message):
    uid = message.from_user.id
    value = (message.text or "").strip()

    try:
        day = datetime.strptime(value, "%Y-%m-%d").date()
        if day < now_local().date():
            bot.send_message(message.chat.id, "Дата не може бути в минулому.")
            return
    except ValueError:
        bot.send_message(message.chat.id, "Невірна дата. Приклад: 2026-10-24")
        return

    update_state_data(shift_date=day.isoformat())
    with state_lock:
        user_states[uid]["state"] = "shift_start_time"

    bot.send_message(message.chat.id, "Введи час початку у форматі HH:MM, наприклад 09:00:")


@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("state") == "shift_start_time")
def handle_shift_start_time(message):
    uid = message.from_user.id
    value = (message.text or "").strip()

    try:
        datetime.strptime(value, "%H:%M")
    except ValueError:
        bot.send_message(message.chat.id, "Невірний час. Приклад: 09:00")
        return

    update_state_data(start_time=value)
    with state_lock:
        user_states[uid]["state"] = "shift_end_time"

    bot.send_message(message.chat.id, "Введи час завершення у форматі HH:MM, наприклад 18:00:")


@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("state") == "shift_end_time")
def handle_shift_end_time(message):
    uid = message.from_user.id
    value = (message.text or "").strip()

    try:
        end = datetime.strptime(value, "%H:%M").time()
    except ValueError:
        bot.send_message(message.chat.id, "Невірний час. Приклад: 18:00")
        return

    state = get_state(uid)
    data = state.get("data", {})
    try:
        start = datetime.strptime(data["start_time"], "%H:%M").time()
        if end <= start:
            bot.send_message(
                message.chat.id,
                "Час завершення має бути пізніше часу початку. Нічні зміни поки не підтримуються."
            )
            return

        employee_id = int(data["employee_id"])
        day = data["shift_date"]
        with db_connection() as conn:
            overlap = conn.execute("""SELECT id FROM shifts
                WHERE user_id=? AND shift_date=? AND status IN ('planned','started')
                AND start_time < ? AND end_time > ? LIMIT 1""",
                (employee_id, day, value, data["start_time"])).fetchone()
            if overlap:
                bot.send_message(message.chat.id, f"У працівника вже є зміна #{overlap['id']} у цей час. Нову зміну не створено.")
                return
            cursor = conn.execute("""
                INSERT INTO shifts (
                    user_id,shift_date,start_time,end_time,status,
                    created_by,created_at
                ) VALUES (?,?,?,?, 'planned',?,?)
            """, (
                employee_id, day, data["start_time"], value, uid, now_text()
            ))
            shift_id = cursor.lastrowid

        clear_state(uid)
        log_event("shift_assigned", uid, {
            "shift_id": shift_id,
            "employee_id": employee_id,
            "date": day,
            "start": data["start_time"],
            "end": value,
        })

        notify_user(
            employee_id,
            f"🗓 <b>Тобі призначено зміну #{shift_id}</b>\n"
            f"Дата: {esc(day)}\n"
            f"Час: {esc(data['start_time'])}–{esc(value)}\n"
            "Переглянути зміну можна в меню «🕒 Мої зміни»."
        )
        bot.send_message(
            message.chat.id,
            f"✅ Зміну #{shift_id} призначено.",
            reply_markup=main_keyboard(uid)
        )
    except Exception:
        logger.exception("Shift creation failed")
        bot.send_message(message.chat.id, "Не вдалося створити зміну. Перевір журнал бота.")


@bot.message_handler(func=lambda m: m.text == "👥 Користувачі")
def handle_users_menu(message):
    uid = message.from_user.id
    if not require_permission(message, "manage_users"):
        return

    users = get_staff_users()
    keyboard = types.InlineKeyboardMarkup(row_width=1)

    for user in users:
        name = user["full_name"] or user["username"] or str(user["user_id"])
        status = "⛔ заблокований" if is_blocked(user["user_id"]) else "✅ активний"
        keyboard.add(types.InlineKeyboardButton(
            f"{name} · {status}",
            callback_data=f"user_manage:{user['user_id']}"
        ))

    bot.send_message(
        message.chat.id,
        "👥 <b>Користувачі CRM</b>\nОбери користувача для керування:",
        reply_markup=keyboard
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("user_manage:"))
def callback_user_manage(call):
    uid = call.from_user.id
    if not is_logged_in(uid) or not has_permission(uid, "manage_users"):
        bot.answer_callback_query(call.id, "Немає доступу.")
        return

    try:
        target_id = int(call.data.split(":", 1)[1])
        target = get_user(target_id)
        if not target:
            bot.answer_callback_query(call.id, "Користувача не знайдено.")
            return

        keyboard = types.InlineKeyboardMarkup(row_width=1)
        if target_id != uid:
            if is_blocked(target_id):
                keyboard.add(types.InlineKeyboardButton(
                    "🔓 Розблокувати",
                    callback_data=f"user_unblock:{target_id}"
                ))
            else:
                keyboard.add(types.InlineKeyboardButton(
                    "⛔ Заблокувати",
                    callback_data=f"user_block:{target_id}"
                ))

        if has_permission(uid, "manage_permissions"):
            keyboard.add(types.InlineKeyboardButton(
                "🔐 Права доступу",
                callback_data=f"permissions:{target_id}"
            ))
        keyboard.add(types.InlineKeyboardButton(
            "✏️ Змінити ім'я в CRM",
            callback_data=f"user_name:{target_id}"
        ))

        bot.send_message(
            call.message.chat.id,
            format_user(target),
            reply_markup=keyboard
        )

        bot.answer_callback_query(call.id)
    except Exception:
        logger.exception("User management callback failed")
        bot.answer_callback_query(call.id, "Помилка.")


@bot.callback_query_handler(func=lambda c: c.data.startswith("user_name:"))
def callback_user_name(call):
    uid = call.from_user.id
    if not is_logged_in(uid) or not has_permission(uid, "manage_users"):
        bot.answer_callback_query(call.id, "Немає доступу.")
        return
    try:
        target_id = int(call.data.split(":", 1)[1])
        if not get_user(target_id):
            bot.answer_callback_query(call.id, "Користувача не знайдено.")
            return
        set_state(uid, "set_display_name", {"target_id": target_id})
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id,
            "Введи ім'я, яке показуватиметься в CRM (до 60 символів).",
            reply_markup=cancel_keyboard())
    except Exception:
        logger.exception("Display name callback failed")
        bot.answer_callback_query(call.id, "Не вдалося відкрити редагування імені.")

@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("state") == "set_display_name")
def handle_set_display_name(message):
    uid = message.from_user.id
    if not require_permission(message, "manage_users"):
        return
    name = (message.text or "").strip()
    if not name or len(name) > 60:
        bot.send_message(message.chat.id, "Ім'я має містити від 1 до 60 символів.")
        return
    target_id = int(get_state_data(uid).get("target_id", 0))
    with db_connection() as conn:
        cur = conn.execute("UPDATE users SET display_name=? WHERE user_id=?", (name, target_id))
    if not cur.rowcount:
        bot.send_message(message.chat.id, "Користувача не знайдено.")
        return
    clear_state(uid)
    log_event("user_display_name_changed", uid, {"target_id": target_id, "display_name": name})
    bot.send_message(message.chat.id, f"✅ Ім'я збережено: <b>{esc(name)}</b>",
                     reply_markup=main_keyboard(uid))

@bot.callback_query_handler(func=lambda c: c.data.startswith("user_block:"))
def callback_user_block(call):
    uid = call.from_user.id
    if not is_logged_in(uid) or not has_permission(uid, "manage_users"):
        bot.answer_callback_query(call.id, "Немає доступу.")
        return

    target_id = int(call.data.split(":", 1)[1])
    if target_id == uid:
        bot.answer_callback_query(call.id, "Не можна заблокувати самого себе.")
        return

    with db_connection() as conn:
        conn.execute("""
            INSERT INTO blocked_users(user_id,blocked_by,reason,blocked_at,created_at)
            VALUES(?,?,?, ?,?)
            ON CONFLICT(user_id) DO UPDATE SET
            blocked_by=excluded.blocked_by,
            reason=excluded.reason,
            blocked_at=excluded.blocked_at
        """, (target_id, uid, "Заблоковано адміністратором", now_text(), now_text()))
        conn.execute("UPDATE users SET is_blocked=1 WHERE user_id=?", (target_id,))

    log_event("user_blocked", uid, {"target_id": target_id})
    bot.answer_callback_query(call.id, "Користувача заблоковано.")
    bot.send_message(call.message.chat.id, f"⛔ Користувача {target_id} заблоковано.")


@bot.callback_query_handler(func=lambda c: c.data.startswith("user_unblock:"))
def callback_user_unblock(call):
    uid = call.from_user.id
    if not is_logged_in(uid) or not has_permission(uid, "manage_users"):
        bot.answer_callback_query(call.id, "Немає доступу.")
        return

    target_id = int(call.data.split(":", 1)[1])
    with db_connection() as conn:
        conn.execute("DELETE FROM blocked_users WHERE user_id=?", (target_id,))
        conn.execute("UPDATE users SET is_blocked=0 WHERE user_id=?", (target_id,))

    log_event("user_unblocked", uid, {"target_id": target_id})
    bot.answer_callback_query(call.id, "Користувача розблоковано.")
    bot.send_message(call.message.chat.id, f"✅ Користувача {target_id} розблоковано.")
@bot.message_handler(func=lambda m: m.text == "🚨 SOS OFFICE")
def handle_sos_start(message):
    uid = message.from_user.id
    if not require_login(message):
        return

    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for i, reason in enumerate(SOS_REASONS):
        keyboard.add(types.InlineKeyboardButton(
            reason, callback_data=f"sos_reason:{i}"
        ))

    bot.send_message(
        message.chat.id,
        "🚨 <b>SOS OFFICE</b>\nОбери тип інциденту. Якщо є безпосередня загроза життю, телефонуй 112 або 103.",
        reply_markup=keyboard,
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("sos_reason:"))
def callback_sos_reason(call):
    uid = call.from_user.id
    if not is_logged_in(uid):
        bot.answer_callback_query(call.id, "Спочатку авторизуйся.")
        return

    try:
        index = int(call.data.split(":", 1)[1])
        reason = SOS_REASONS[index]
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Невідома категорія.")
        return

    set_state(uid, "sos_description", {"reason": reason})
    bot.send_message(
        call.message.chat.id,
        "Опиши ситуацію або надішли «-», якщо опису немає:",
        reply_markup=cancel_keyboard(),
    )
    bot.answer_callback_query(call.id)


@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("state") == "sos_description")
def handle_sos_description(message):
    uid = message.from_user.id
    state = get_state(uid)
    description = (message.text or "").strip()

    if not description:
        bot.send_message(message.chat.id, "Введи опис або «-».")
        return

    data = dict(state.get("data", {}))
    data["description"] = "" if description == "-" else description
    set_state(uid, "sos_confirm", data)

    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("🚨 Підтвердити SOS", callback_data="sos_confirm"),
        types.InlineKeyboardButton("❌ Скасувати", callback_data="sos_cancel"),
    )
    bot.send_message(
        message.chat.id,
        f"Підтверди створення інциденту:\n"
        f"Категорія: <b>{esc(data['reason'])}</b>\n"
        f"Опис: <b>{esc(data['description'] or 'Не вказано')}</b>\n\n"
        "Якщо це невідкладна ситуація, не чекай відповіді в боті.",
        reply_markup=keyboard,
    )


@bot.callback_query_handler(func=lambda c: c.data == "sos_confirm")
def callback_sos_confirm(call):
    uid = call.from_user.id
    if not is_logged_in(uid):
        bot.answer_callback_query(call.id, "Спочатку авторизуйся.")
        return

    state = get_state(uid)
    if state.get("state") != "sos_confirm":
        bot.answer_callback_query(call.id, "Запит застарів. Створи його знову.")
        return

    data = state.get("data", {})
    with db_connection() as conn:
        cursor = conn.execute("""
            INSERT INTO sos_incidents(user_id,reason,description,status,created_at)
            VALUES(?,?,?,'active',?)
        """, (uid, data.get("reason", "Інше"),
              data.get("description", ""), now_text()))
        incident_id = cursor.lastrowid

    clear_state(uid)

    log_event("sos_created", uid, {"incident_id": incident_id})

    user = get_user(uid)
    display_name = (user["full_name"] or user["username"] or str(uid)) if user else str(uid)
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton(
        "✅ Підтвердити отримання",
        callback_data=f"sos_ack:{incident_id}"
    ))
    msg = (
        f"🚨 <b>SOS OFFICE #{incident_id}</b>\n"
        f"Хто: {esc(display_name)}\n"
        f"ID: <code>{uid}</code>\n"
        f"Причина: {esc(data.get('reason', 'Інше'))}\n"
        f"Час: {esc(now_text())}\n"
        f"Опис: {esc(data.get('description') or 'Не вказано')}\n\n"
        "Натисніть підтвердження, щоб зупинити повторні нагадування."
    )
    sent = notify_owners(msg, keyboard)
    bot.answer_callback_query(call.id, "SOS зареєстровано.")
    bot.send_message(
        call.message.chat.id,
        f"🚨 Інцидент #{incident_id} зареєстровано. "
        f"Сповіщення надіслано власникам: {sent}.\n"
        "Цей бот не викликає екстрені служби автоматично. За невідкладної небезпеки телефонуй 112 або 103."
    )


@bot.callback_query_handler(func=lambda c: c.data == "sos_cancel")
def callback_sos_cancel(call):
    clear_state(call.from_user.id)
    bot.answer_callback_query(call.id, "SOS скасовано.")
    try:
        bot.edit_message_reply_markup(
            call.message.chat.id, call.message.message_id, reply_markup=None
        )
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("sos_ack:"))
def callback_sos_ack(call):
    uid = call.from_user.id
    if not is_logged_in(uid) or not has_permission(uid, "manage_sos"):
        bot.answer_callback_query(call.id, "Немає доступу.")
        return

    incident_id = int(call.data.split(":", 1)[1])
    with db_connection() as conn:
        incident = conn.execute(
            "SELECT * FROM sos_incidents WHERE id=?", (incident_id,)
        ).fetchone()
        if not incident:
            bot.answer_callback_query(call.id, "Інцидент не знайдено.")
            return
        if incident["status"] == "closed":
            bot.answer_callback_query(call.id, "Інцидент уже закрито.")
            return
        conn.execute("""
            UPDATE sos_incidents SET status='acknowledged',
            acknowledged_by=?,acknowledged_at=? WHERE id=?
        """, (uid, now_text(), incident_id))

    log_event("sos_acknowledged", uid, {"incident_id": incident_id})
    bot.answer_callback_query(call.id, "Отримання підтверджено.")
    bot.send_message(
        call.message.chat.id,
        f"✅ SOS #{incident_id}: отримання підтверджено. Інцидент ще не закрито."
    )
    notify_user(
        incident["user_id"],
        f"✅ Власник підтвердив отримання SOS #{incident_id}. Якщо небезпека триває, телефонуй 112."
    )


@bot.message_handler(func=lambda m: m.text == "📨 Створити звернення")
def handle_ticket_start(message):
    uid = message.from_user.id
    if not require_login(message):
        return

    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for i, category in enumerate(TICKET_CATEGORIES):
        keyboard.add(types.InlineKeyboardButton(
            category, callback_data=f"ticket_category:{i}"
        ))
    bot.send_message(message.chat.id, "📨 Обери категорію звернення:", reply_markup=keyboard)


@bot.callback_query_handler(func=lambda c: c.data.startswith("ticket_category:"))
def callback_ticket_category(call):
    uid = call.from_user.id
    if not is_logged_in(uid):
        bot.answer_callback_query(call.id, "Спочатку авторизуйся.")
        return

    try:
        category = TICKET_CATEGORIES[int(call.data.split(":", 1)[1])]
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Невідома категорія.")
        return

    set_state(uid, "ticket_message", {"category": category})
    bot.send_message(
        call.message.chat.id,
        f"Категорія: <b>{esc(category)}</b>\nОпиши звернення:",
        reply_markup=cancel_keyboard(),
    )
    bot.answer_callback_query(call.id)


@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("state") == "ticket_message")
def handle_ticket_message(message):
    uid = message.from_user.id
    state = get_state(uid)
    content = (message.text or "").strip()
    if len(content) < 3:
        bot.send_message(message.chat.id, "Опиши звернення трохи детальніше.")
        return

    category = state.get("data", {}).get("category", "Інше")
    with db_connection() as conn:
        cursor = conn.execute("""
            INSERT INTO tickets(user_id,category,message,status,created_at,updated_at)
            VALUES(?,?,?,'Нове',?,?)
        """, (uid, category, content, now_text(), now_text()))
        ticket_id = cursor.lastrowid

    clear_state(uid)
    log_event("ticket_created", uid, {"ticket_id": ticket_id, "category": category})
    user = get_user(uid)
    name = (user["full_name"] or user["username"] or str(uid)) if user else str(uid)
    notify_owners(
        f"📨 <b>Нове звернення #{ticket_id}</b>\n"
        f"Автор: {esc(name)} (<code>{uid}</code>)\n"
        f"Категорія: {esc(category)}\n"
        f"Час: {esc(now_text())}\n"
        f"Текст: {esc(content)}"
    )
    bot.send_message(
        message.chat.id,
        f"✅ Звернення #{ticket_id} створено.",
        reply_markup=main_keyboard(uid),
    )


@bot.message_handler(func=lambda m: m.text == "🎫 Звернення")
def handle_tickets_list(message):
    uid = message.from_user.id
    if not require_permission(message, "view_tickets"):
        return

    with db_connection() as conn:
        tickets = conn.execute("""
            SELECT t.*,u.full_name,u.username
            FROM tickets t LEFT JOIN users u ON u.user_id=t.user_id
            ORDER BY t.id DESC LIMIT 30
        """).fetchall()

    if not tickets:
        bot.send_message(message.chat.id, "Звернень поки немає.")
        return

    for ticket in tickets:
        name = ticket["full_name"] or ticket["username"] or str(ticket["user_id"])
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(
            types.InlineKeyboardButton(
                "✉️ Відповісти", callback_data=f"ticket_reply:{ticket['id']}"
            ),
            types.InlineKeyboardButton(
                "🔄 Статус", callback_data=f"ticket_status:{ticket['id']}"
            ),
        )
        bot.send_message(
            message.chat.id,
            f"🎫 <b>Звернення #{ticket['id']}</b>\n"
            f"Автор: {esc(name)}\nКатегорія: {esc(ticket['category'])}\n"
            f"Статус: {esc(ticket['status'])}\n"
            f"Текст: {esc(ticket['message'])}",
            reply_markup=keyboard,
        )



@bot.callback_query_handler(func=lambda c: c.data.startswith("ticket_reply:"))
def callback_ticket_reply(call):
    uid = call.from_user.id
    if not is_logged_in(uid) or not has_permission(uid, "view_tickets"):
        bot.answer_callback_query(call.id, "Немає доступу.")
        return

    ticket_id = int(call.data.split(":", 1)[1])
    with db_connection() as conn:
        ticket = conn.execute(
            "SELECT id FROM tickets WHERE id=?", (ticket_id,)
        ).fetchone()
    if not ticket:
        bot.answer_callback_query(call.id, "Звернення не знайдено.")
        return

    set_state(uid, "ticket_admin_reply", {"ticket_id": ticket_id})
    bot.send_message(
        call.message.chat.id,
        f"Введи відповідь для звернення #{ticket_id}:",
        reply_markup=cancel_keyboard(),
    )
    bot.answer_callback_query(call.id)


@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("state") == "ticket_admin_reply")
def handle_ticket_admin_reply(message):
    uid = message.from_user.id
    if not has_permission(uid, "view_tickets"):
        clear_state(uid)
        bot.send_message(message.chat.id, "Немає доступу.")
        return

    state = get_state(uid)
    ticket_id = int(state.get("data", {}).get("ticket_id", 0))
    reply = (message.text or "").strip()
    if not reply:
        bot.send_message(message.chat.id, "Відповідь не може бути порожньою.")
        return

    with db_connection() as conn:
        ticket = conn.execute(
            "SELECT * FROM tickets WHERE id=?", (ticket_id,)
        ).fetchone()
        if not ticket:
            clear_state(uid)
            bot.send_message(message.chat.id, "Звернення не знайдено.")
            return
        conn.execute(
            "INSERT INTO ticket_replies(ticket_id,user_id,message,created_at) VALUES(?,?,?,?)",
            (ticket_id, uid, reply, now_text()),
        )
        conn.execute(
            "UPDATE tickets SET status='Очікує відповіді',updated_at=?,assigned_to=? WHERE id=?",
            (now_text(), uid, ticket_id),
        )

    clear_state(uid)
    log_event("ticket_replied", uid, {"ticket_id": ticket_id})
    notify_user(
        ticket["user_id"],
        f"✉️ Відповідь на звернення #{ticket_id}:\n\n{esc(reply)}"
    )
    bot.send_message(
        message.chat.id,
        f"✅ Відповідь на звернення #{ticket_id} надіслано.",
        reply_markup=main_keyboard(uid),
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("ticket_status:"))
def callback_ticket_status(call):
    uid = call.from_user.id
    if not is_logged_in(uid) or not has_permission(uid, "view_tickets"):
        bot.answer_callback_query(call.id, "Немає доступу.")
        return

    ticket_id = int(call.data.split(":", 1)[1])
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for i, status in enumerate(TICKET_STATUSES):
        keyboard.add(types.InlineKeyboardButton(
            status, callback_data=f"ticket_set_status:{ticket_id}:{i}"
        ))
    bot.send_message(call.message.chat.id, "Обери статус звернення:", reply_markup=keyboard)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("ticket_set_status:"))
def callback_ticket_set_status(call):
    uid = call.from_user.id
    if not is_logged_in(uid) or not has_permission(uid, "view_tickets"):
        bot.answer_callback_query(call.id, "Немає доступу.")
        return

    try:
        _, ticket_id, index = call.data.split(":", 2)
        ticket_id, index = int(ticket_id), int(index)
        status = TICKET_STATUSES[index]
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Невідомий статус.")
        return

    with db_connection() as conn:
        ticket = conn.execute(
            "SELECT * FROM tickets WHERE id=?", (ticket_id,)
        ).fetchone()
        if not ticket:
            bot.answer_callback_query(call.id, "Звернення не знайдено.")
            return
        conn.execute(
            "UPDATE tickets SET status=?,updated_at=? WHERE id=?",
            (status, now_text(), ticket_id),
        )

    log_event("ticket_status_changed", uid, {"ticket_id": ticket_id, "status": status})
    bot.answer_callback_query(call.id, "Статус змінено.")
    bot.send_message(call.message.chat.id, f"Звернення #{ticket_id}: {esc(status)}")

def send_long_message(chat_id, text, reply_markup=None, limit=3900):
    """Send long text in Telegram-safe chunks without cutting HTML entities."""
    text = str(text or "")
    if not text:
        return bot.send_message(chat_id, " ")
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    sent = None
    for index, chunk in enumerate(chunks):
        sent = bot.send_message(chat_id, chunk, reply_markup=reply_markup if index == len(chunks)-1 else None)
    return sent

def get_ai_history(user_id, mode="assistant", limit=24):
    with db_connection() as conn:
        rows = conn.execute(
            "SELECT role,content FROM ai_conversations WHERE user_id=? AND mode=? ORDER BY id DESC LIMIT ?",
            (int(user_id), mode, int(limit))
        ).fetchall()
    return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]

def save_ai_turn(user_id, mode, question, answer):
    with db_connection() as conn:
        conn.execute(
            "INSERT INTO ai_conversations(user_id,mode,role,content,created_at) VALUES(?,?,?,?,?)",
            (int(user_id), mode, "user", question, now_text())
        )
        conn.execute(
            "INSERT INTO ai_conversations(user_id,mode,role,content,created_at) VALUES(?,?,?,?,?)",
            (int(user_id), mode, "assistant", answer, now_text())
        )


def clear_ai_history(user_id, mode=None):
    with db_connection() as conn:
        if mode:
            conn.execute("DELETE FROM ai_conversations WHERE user_id=? AND mode=?", (int(user_id), mode))
        else:
            conn.execute("DELETE FROM ai_conversations WHERE user_id=?", (int(user_id),))

def ai_answer(user_id, question, mode="assistant"):
    if not OPENAI_API_KEY:
        return "AI-помічник не налаштований: додай OPENAI_API_KEY у змінні Render."
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY, timeout=45.0, max_retries=2)
        history = get_ai_history(user_id, mode, limit=100)
        history.append({"role": "user", "content": question[:12000]})
        instructions = (
            "Ти AI-помічник українського вейпшопу Haimer Brend Shop (HBS), а не лише CRM. "
            "Допомагай із товарами вейпшопу, поясненнями характеристик, сервісом клієнтів, "
            "роботою магазину, продажами, маркетингом, текстами для соцмереж, організацією "
            "команди та загальними питаннями користувача. Не стверджуй, що знаєш актуальний "
            "асортимент, ціни, залишки, адресу, доставку чи правила магазину, якщо вони не "
            "передані в контексті або не перевірені в інтернеті. Коли потрібна актуальна "

            "інформація, використовуй веб-пошук, якщо він доступний, і відділяй перевірені "
            "факти від припущень. Відповідай природно, доброзичливо, прямо та без зайвих "
            "відмов; якщо запит безпечний, допомагай максимально практично. Дотримуйся "
            "законодавства та правил безпеки, зокрема щодо вікових обмежень на нікотинові "
            "товари; не допомагай обходити перевірку віку чи продавати заборонені товари. "
            "Не вигадуй дані CRM і не стверджуй, що маєш доступ до бази чи інтернету, якщо "
            "інструмент недоступний. Ніколи не проси паролі, токени або API-ключі. "
            "Підтримуй довгий багатокроковий діалог: використовуй попередню історію, "
            "пам'ятай контекст розмови та уточнюй лише те, що справді потрібно."
        )
        response = client.responses.create(
            model=OPENAI_MODEL,
            instructions=instructions,
            input=history,
            tools=[{"type": "web_search"}],
        )
        answer = response.output_text or "Не вдалося сформувати відповідь."
        save_ai_turn(user_id, mode, question[:12000], answer[:20000])
        return answer
    except Exception:
        logger.exception("AI request failed for user %s", user_id)
        return "AI тимчасово недоступний. Перевір ключ API, модель і журнал Render."


@bot.message_handler(func=lambda m: m.text == "🤖 AI-помічник")
def handle_ai_start(message):
    uid = message.from_user.id
    if not require_permission(message, "use_ai"):
        return
    set_state(uid, "ai_question", {})
    bot.send_message(
        message.chat.id,
        "🤖 Напиши запитання. Наприклад: «Як організувати обробку замовлень?»",
        reply_markup=cancel_keyboard(),
    )


@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("state") == "ai_question")
def handle_ai_question(message):
    uid = message.from_user.id
    question = (message.text or "").strip()
    if not question:
        bot.send_message(message.chat.id, "Напиши запитання.")
        return
    answer = ai_answer(uid, question)
    send_long_message(message.chat.id, esc(answer), reply_markup=cancel_keyboard())
    # Keep AI state active for a multi-turn conversation until /cancel or Home.


@bot.message_handler(func=lambda m: m.text == "💬 Психологічна підтримка")
def handle_psych_start(message):
    uid = message.from_user.id
    if not require_permission(message, "use_psychologist"):
        return

    with db_connection() as conn:
        row = conn.execute(
            "SELECT psychologist_consent FROM ai_preferences WHERE user_id=?",
            (uid,),
        ).fetchone()

    if not row or not row["psychologist_consent"]:
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(
            types.InlineKeyboardButton("Погоджуюсь", callback_data="psych_consent"),
            types.InlineKeyboardButton("Не погоджуюсь", callback_data="psych_decline"),
        )
        bot.send_message(
            message.chat.id,
            "💬 Це режим підтримки, а не заміна психолога чи лікаря. "
            "Текст повідомлень надсилатиметься OpenAI для створення відповіді. "
            "Не надсилай паролі або дані, які не хочеш передавати сервісу. "
            "У разі безпосередньої небезпеки звернися до екстрених служб.",
            reply_markup=keyboard,
        )
        return

    set_state(uid, "psych_question", {})
    bot.send_message(message.chat.id, "Що тебе зараз турбує?", reply_markup=cancel_keyboard())


@bot.callback_query_handler(func=lambda c: c.data == "psych_consent")
def callback_psych_consent(call):
    uid = call.from_user.id
    if not is_logged_in(uid):
        bot.answer_callback_query(call.id, "Спочатку авторизуйся.")
        return
    with db_connection() as conn:
        conn.execute("""
            INSERT INTO ai_preferences(user_id,psychologist_consent) VALUES(?,1)
            ON CONFLICT(user_id) DO UPDATE SET psychologist_consent=1
        """, (uid,))
    set_state(uid, "psych_question", {})
    bot.answer_callback_query(call.id, "Згоду збережено.")
    bot.send_message(call.message.chat.id, "Можеш написати, що відчуваєш.")


@bot.callback_query_handler(func=lambda c: c.data == "psych_decline")
def callback_psych_decline(call):
    clear_state(call.from_user.id)
    bot.answer_callback_query(call.id, "Режим не активовано.")
    bot.send_message(call.message.chat.id, "Режим психологічної підтримки не активовано.")


@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("state") == "psych_question")
def handle_psych_question(message):
    uid = message.from_user.id
    text = (message.text or "").strip()
    if not text:
        bot.send_message(message.chat.id, "Напиши повідомлення.")
        return

    if any(word in text.lower() for word in (
        "хочу вбити себе", "покінчити з життям", "зараз накладу на себе руки",
        "хочу померти"
    )):
        bot.send_message(
            message.chat.id,
            "Мені шкода, що тобі настільки важко. Якщо ти можеш нашкодити собі прямо зараз, "
            "відійди від небезпечних предметів, звернися до людини поруч і телефонуй 112 або 103. "
            "Я можу залишитися з тобою в розмові, але не можу викликати допомогу замість тебе."
        )
        return

    if not OPENAI_API_KEY:
        bot.send_message(message.chat.id, "Режим недоступний: не налаштований OPENAI_API_KEY.")
        return

    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        response = client.responses.create(
            model=OPENAI_MODEL,
            instructions=(
                "Ти уважний помічник емоційної підтримки. Відповідай українською, "
                "співчутливо та без осуду. Не став діагнозів і не призначай ліків. "
                "Підтримуй тривалий діалог, враховуй попередні репліки. Якщо є ризик "
                "самоушкодження, заохочуй звернутися до людини поруч та місцевої екстреної допомоги."
            ),
            input=get_ai_history(uid, "psychologist", limit=100) + [{"role": "user", "content": text[:12000]}],
            tools=[{"type": "web_search"}],
        )
        answer = response.output_text or "Не вдалося сформувати відповідь."
        save_ai_turn(uid, "psychologist", text[:12000], answer[:20000])
        send_long_message(message.chat.id, esc(answer))
    except Exception:
        logger.exception("Psychology assistant failed")
        bot.send_message(message.chat.id, "Зараз не вдалося отримати відповідь. Спробуй пізніше.")


@bot.message_handler(func=lambda m: m.text == "📣 Розсилка")
def handle_broadcast_start(message):
    uid = message.from_user.id
    if not require_permission(message, "broadcast"):
        return

    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(types.InlineKeyboardButton("Усім користувачам", callback_data="broadcast_role:all"))
    for role in ("owner", "manager", "staff", "collector", "shipper"):
        keyboard.add(types.InlineKeyboardButton(ROLE_LABELS[role], callback_data=f"broadcast_role:{role}"))
    keyboard.add(types.InlineKeyboardButton("👤 Окремим людям", callback_data="broadcast_role:individual"))
    bot.send_message(message.chat.id, "Обери отримувачів розсилки:", reply_markup=keyboard)


@bot.callback_query_handler(func=lambda c: c.data.startswith("broadcast_role:"))
def callback_broadcast_role(call):
    uid = call.from_user.id
    if not is_logged_in(uid) or not has_permission(uid, "broadcast"):
        bot.answer_callback_query(call.id, "Немає доступу.")
        return

    role = call.data.split(":", 1)[1]
    if role not in ("all", "individual", *ROLES):
        bot.answer_callback_query(call.id, "Невідомий тип отримувачів.", show_alert=True)
        return
    if role == "individual":
        set_state(uid, "broadcast_recipients", {})
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id,
            "Введи ID, @username або відображуване ім’я отримувачів через кому. "
            "Бот знайде лише користувачів, які вже авторизувалися.", reply_markup=cancel_keyboard())
        return

    set_state(uid, "broadcast_message", {"role": role})
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id,
        "Введи текст розсилки. Відправлення почнеться після підтвердження.",
        reply_markup=cancel_keyboard())


@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("state") == "broadcast_recipients")
def handle_broadcast_recipients(message):
    uid = message.from_user.id
    if not is_logged_in(uid) or not has_permission(uid, "broadcast"):
        clear_state(uid)
        bot.send_message(message.chat.id, "⛔ Немає доступу до розсилок.")
        return
    tokens = [x.strip().lstrip("@") for x in (message.text or "").split(",") if x.strip()]
    if not tokens:
        bot.send_message(message.chat.id, "Вкажи хоча б одного отримувача.")
        return
    with db_connection() as conn:
        rows = conn.execute("SELECT user_id,username,full_name,display_name FROM users WHERE role!='' AND is_blocked=0").fetchall()
    found = []
    missing = []
    for token in tokens:
        matches = []
        for row in rows:
            vals = {str(row["user_id"]), str(row["username"] or "").lstrip("@"),
                    str(row["full_name"] or "").casefold(), str(row["display_name"] or "").casefold()}
            if token.casefold() in vals:
                matches.append(int(row["user_id"]))
        if matches:
            found.extend(matches)
        else:
            missing.append(token)
    found = sorted(set(found))
    if not found:
        bot.send_message(message.chat.id, "Не знайшов активних авторизованих отримувачів. Перевір ID, username або ім’я.")
        return
    set_state(uid, "broadcast_message", {"role": "individual", "recipients": found})
    extra = ("\nНе знайдено: " + ", ".join(esc(x) for x in missing)) if missing else ""
    bot.send_message(message.chat.id, f"Знайдено отримувачів: {len(found)}.{extra}\nТепер введи текст розсилки.", reply_markup=cancel_keyboard())


@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("state") == "broadcast_message")
def handle_broadcast_message(message):
    uid = message.from_user.id
    content = (message.text or "").strip()
    if not content:
        bot.send_message(message.chat.id, "Текст не може бути порожнім.")
        return

    state = get_state(uid)
    role = state.get("data", {}).get("role", "all")
    selected = state.get("data", {}).get("recipients") if role == "individual" else None
    if selected is not None:
        recipients = [int(x) for x in selected]
    else:
        with db_connection() as conn:
            if role == "all":
                rows = conn.execute("SELECT user_id FROM users WHERE role!='' AND is_blocked=0").fetchall()
            else:
                rows = conn.execute("SELECT user_id FROM users WHERE role=? AND is_blocked=0", (role,)).fetchall()
        recipients = [int(row["user_id"]) for row in rows]
    set_state(uid, "broadcast_confirm", {"role": role, "content": content, "recipients": recipients})

    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(
        types.InlineKeyboardButton("✅ Підтвердити", callback_data="broadcast_confirm"),
        types.InlineKeyboardButton("❌ Скасувати", callback_data="broadcast_cancel"),
    )
    bot.send_message(
        message.chat.id,
        f"Розсилка охопить {len(recipients)} користувачів.\n\n"
        f"Попередній перегляд:\n{esc(content[:1000])}",
        reply_markup=keyboard,
    )


@bot.callback_query_handler(func=lambda c: c.data == "broadcast_confirm")
def callback_broadcast_confirm(call):
    uid = call.from_user.id
    if not is_logged_in(uid) or not has_permission(uid, "broadcast"):
        bot.answer_callback_query(call.id, "Немає доступу.")
        return

    state = get_state(uid)
    if state.get("state") != "broadcast_confirm":
        bot.answer_callback_query(call.id, "Розсилка застаріла.")
        return

    data = state.get("data", {})
    content = data.get("content", "")
    recipients = data.get("recipients", [])
    sent = 0
    failed = 0

    for recipient_id in recipients:
        ok = notify_user(recipient_id, content)
        status = "sent" if ok else "failed"
        with db_connection() as conn:
            conn.execute("""
                INSERT INTO broadcast_log(sender_id,recipient_id,message,status,created_at)
                VALUES(?,?,?,?,?)
            """, (uid, recipient_id, content, status, now_text()))
        if ok:
            sent += 1
        else:
            failed += 1
        time.sleep(0.05)

    clear_state(uid)
    log_event("broadcast_completed", uid, {"sent": sent, "failed": failed})
    bot.answer_callback_query(call.id, "Розсилку завершено.")
    bot.send_message(
        call.message.chat.id,
        f"📣 Розсилку завершено.\nУспішно: {sent}\nНе доставлено: {failed}",
        reply_markup=main_keyboard(uid),
    )


@bot.callback_query_handler(func=lambda c: c.data == "broadcast_cancel")
def callback_broadcast_cancel(call):
    uid = call.from_user.id
    if not is_logged_in(uid) or not has_permission(uid, "broadcast"):
        bot.answer_callback_query(call.id, "Немає доступу.")
        return
    clear_state(uid)
    bot.answer_callback_query(call.id, "Розсилку скасовано.")
    bot.send_message(call.message.chat.id, "Розсилку скасовано.")


@bot.callback_query_handler(func=lambda c: c.data.startswith("permissions:"))
def callback_permissions(call):
    uid = call.from_user.id
    if not is_logged_in(uid) or not has_permission(uid, "manage_permissions"):
        bot.answer_callback_query(call.id, "Немає доступу.")
        return

    target_id = int(call.data.split(":", 1)[1])
    keyboard = types.InlineKeyboardMarkup(row_width=1)

    for permission, label in PERMISSIONS.items():
        allowed = has_permission(target_id, permission)
        keyboard.add(types.InlineKeyboardButton(
            f"{'✅' if allowed else '❌'} {label}",
            callback_data=f"permission_toggle:{target_id}:{permission}"
        ))

    bot.send_message(
        call.message.chat.id,
        f"🔐 Права користувача <code>{target_id}</code>\nНатисни право, щоб перемкнути його.",
        reply_markup=keyboard,
    )
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("permission_toggle:"))
def callback_permission_toggle(call):
    uid = call.from_user.id
    if not is_logged_in(uid) or not has_permission(uid, "manage_permissions"):
        bot.answer_callback_query(call.id, "Немає доступу.")
        return

    try:
        _, target_id, permission = call.data.split(":", 2)
        target_id = int(target_id)
        if permission not in PERMISSIONS:
            bot.answer_callback_query(call.id, "Невідоме право.")
            return
        if get_role(target_id) == "owner":
            bot.answer_callback_query(call.id, "Права власника не змінюються тут.")
            return

        allowed = not has_permission(target_id, permission)
        set_permission(target_id, permission, allowed)
        log_event("permission_changed", uid, {
            "target_id": target_id, "permission": permission, "allowed": allowed
        })
        bot.answer_callback_query(call.id, "Право оновлено.")
        callback_permissions_refresh(call.message, uid, target_id)
    except Exception:
        logger.exception("Permission toggle failed")
        bot.answer_callback_query(call.id, "Не вдалося змінити право.")

def callback_permissions_refresh(message, uid, target_id):
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for permission, label in PERMISSIONS.items():
        allowed = has_permission(target_id, permission)
        keyboard.add(types.InlineKeyboardButton(
            f"{'✅' if allowed else '❌'} {label}",
            callback_data=f"permission_toggle:{target_id}:{permission}"
        ))
    bot.send_message(
        message.chat.id,
        f"🔐 Права користувача <code>{target_id}</code>",
        reply_markup=keyboard,
    )


def validate_configuration():
    configured = [p for p in PASSWORD_ENV.values() if p]
    if not configured:
        raise RuntimeError("Set at least one CRM role password in environment variables.")
    if len(configured) != len(set(configured)):
        raise RuntimeError("Role passwords must be unique.")
    if not PASSWORD_ENV.get("owner"):
        logger.warning("CRM_PASSWORD_OWNER is not configured.")


def send_daily_order_reminders(current):
    """Retry the daily 18:00 report until every eligible recipient receives it."""
    if current.hour < 18:
        return
    day = current.strftime("%Y-%m-%d")
    with db_connection() as conn:
        uncollected = conn.execute(
            "SELECT * FROM orders WHERE status IN ('Не зібрано','Нове','new') ORDER BY created_at,id"
        ).fetchall()
        awaiting_ttn = conn.execute(
            "SELECT * FROM orders WHERE status IN ('Зібрано','Зібрано без ТТН') AND COALESCE(ttn,'')='' ORDER BY created_at,id"
        ).fetchall()
        recipients = conn.execute(
            "SELECT user_id,role FROM users WHERE is_blocked=0 AND role IN ('collector','shipper')"
        ).fetchall()
        for row in recipients:
            conn.execute("INSERT OR IGNORE INTO daily_report_delivery(report_date,user_id,status) VALUES(?,?,'pending')",
                         (day, int(row['user_id'])))
        delivery_rows = conn.execute(
            "SELECT user_id,status FROM daily_report_delivery WHERE report_date=?", (day,)
        ).fetchall()
    if not recipients:
        return
    lines = [f"📦 <b>Щоденний контроль замовлень · {esc(day)}</b>",
             f"Не зібрано: <b>{len(uncollected)}</b>"]
    for order in uncollected[:50]:
        lines.append(f"• #{esc(order['order_number'] or order['id'])} · {esc(order['full_name'])} · {esc(order['city'])}")
    if len(uncollected) > 50:
        lines.append(f"… та ще {len(uncollected)-50} замовлень")
    shipped_lines = [f"Зібрано без ТТН: <b>{len(awaiting_ttn)}</b>"]
    for order in awaiting_ttn[:50]:
        shipped_lines.append(f"• #{esc(order['order_number'] or order['id'])} · {esc(order['full_name'])} · ТТН відсутня")
    if len(awaiting_ttn) > 50:
        shipped_lines.append(f"… та ще {len(awaiting_ttn)-50} замовлень")
    already_sent = {int(r['user_id']) for r in delivery_rows if r['status'] == 'sent'}
    for row in recipients:
        recipient_id, role = int(row['user_id']), row['role']
        if recipient_id in already_sent:
            continue
        parts = lines if role == 'collector' else lines + shipped_lines
        message = "\n".join(parts)
        ok = notify_user(recipient_id, message)
        with db_connection() as conn:
            conn.execute("""UPDATE daily_report_delivery
                SET status=?, attempts=attempts+1, last_attempt=?, last_error=?
                WHERE report_date=? AND user_id=?""",
                ('sent' if ok else 'pending', now_text(), '' if ok else 'Telegram delivery failed', day, recipient_id))
    with db_connection() as conn:
        pending = conn.execute("SELECT COUNT(*) FROM daily_report_delivery WHERE report_date=? AND status!='sent'", (day,)).fetchone()[0]
        if pending == 0:
            conn.execute("INSERT INTO daily_report_log(report_date,sent_at,pending_count) VALUES(?,?,?) ON CONFLICT(report_date) DO UPDATE SET sent_at=excluded.sent_at,pending_count=excluded.pending_count",
                         (day, now_text(), len(uncollected)+len(awaiting_ttn)))
    log_event("daily_order_reminder_attempt", None, {
        "date": day, "recipients": len(recipients), "pending_delivery": pending,
        "uncollected": len(uncollected), "awaiting_ttn": len(awaiting_ttn)
    })

def reminder_worker():
    while not stop_event.is_set():
        try:
            current = now_local()
            send_daily_order_reminders(current)
            with db_connection() as conn:
                shifts = conn.execute("""
                    SELECT * FROM shifts
                    WHERE shift_date>=? AND status IN ('planned','started')
                """, (current.strftime("%Y-%m-%d"),)).fetchall()

            for shift in shifts:
                day = shift["shift_date"]
                start = datetime.strptime(
                    f"{day} {shift['start_time']}", "%Y-%m-%d %H:%M"
                ).replace(tzinfo=TIMEZONE)
                end = datetime.strptime(
                    f"{day} {shift['end_time']}", "%Y-%m-%d %H:%M"
                ).replace(tzinfo=TIMEZONE)

                reminders = [
                    ("reminder_30", start - timedelta(minutes=30), "Через 30 хвилин починається"),
                    ("reminder_5_start", start - timedelta(minutes=5), "Через 5 хвилин починається"),
                    ("reminder_5_end", end - timedelta(minutes=5), "До завершення зміни залишилося 5 хвилин"),
                ]

                for flag, when, label in reminders:
                    if flag == "reminder_5_end" and shift["status"] != "started":
                        continue
                    if flag != "reminder_5_end" and shift["status"] != "planned":
                        continue
                    if not shift[flag] and when <= current:
                        keyboard = types.InlineKeyboardMarkup()
                        if flag == "reminder_5_start":
                            keyboard.add(types.InlineKeyboardButton("▶️ Відкрити зміну", callback_data=f"shift_start:{shift['id']}"))
                        elif flag == "reminder_5_end":
                            keyboard.add(types.InlineKeyboardButton("⏹ Закрити зміну", callback_data=f"shift_end:{shift['id']}"))
                        ok = notify_user(
                            shift["user_id"],
                            f"⏰ {label} твоя зміна #{shift['id']} ({esc(day)}).",
                            keyboard if flag in ("reminder_5_start", "reminder_5_end") else None
                        )
                        if ok:
                            with db_connection() as conn:
                                conn.execute(
                                    f"UPDATE shifts SET {flag}=1 WHERE id=?",
                                    (shift["id"],)
                                )

            with db_connection() as conn:
                incidents = conn.execute("""
                    SELECT * FROM sos_incidents
                    WHERE status='active' AND false_alarm=0
                """).fetchall()

            for incident in incidents:
                last_repeat = parse_dt(incident["last_repeat_at"])
                if last_repeat and (current - last_repeat).total_seconds() < 300:
                    continue

                keyboard = types.InlineKeyboardMarkup()
                keyboard.add(types.InlineKeyboardButton(
                    "✅ Підтвердити отримання",
                    callback_data=f"sos_ack:{incident['id']}"
                ))
                text = (
                    f"🚨 <b>НАГАДУВАННЯ SOS #{incident['id']}</b>\n"
                    f"Користувач ID: <code>{incident['user_id']}</code>\n"
                    f"Причина: {esc(incident['reason'])}\n"
                    f"Опис: {esc(incident['description'])}\n"
                    f"Створено: {esc(incident['created_at'])}"
                )
                notified = False
                for owner_id in get_all_owners():
                    if notify_user(owner_id, text, keyboard):
                        notified = True

                with db_connection() as conn:
                    conn.execute("""
                        UPDATE sos_incidents
                        SET last_repeat_at=?,repeat_count=repeat_count+1
                        WHERE id=?
                    """, (now_text(), incident["id"]))

            stop_event.wait(15)
        except Exception:
            logger.exception("Reminder worker error")
            stop_event.wait(15)


def run_bot():
    validate_configuration()
    init_db()
    logger.info("Cloud Hub CRM starting. DB=%s", DB_PATH)
    worker = threading.Thread(target=reminder_worker, daemon=True, name="crm-reminders")
    worker.start()
    try:
        bot.infinity_polling(skip_pending=False, timeout=30, long_polling_timeout=30)
    finally:
        stop_event.set()


if __name__ == "__main__":
    run_bot()
