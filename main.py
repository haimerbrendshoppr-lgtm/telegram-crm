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
    raise RuntimeError(
        "Set TELEGRAM_BOT_TOKEN in environment variables."
    )

if not DB_PATH:
    raise RuntimeError("CRM_DB_PATH must not be empty.")

bot = telebot.TeleBot(TOKEN, parse_mode="HTML", threaded=True)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("cloud_hub_crm")

db_lock = threading.RLock()
state_lock = threading.RLock()
user_states = {}

reminder_lock = threading.RLock()
stop_event = threading.Event()

ROLES = {
    "owner",
    "manager",
    "staff",
    "collector",
    "shipper",
}

ROLE_LABELS = {
    "owner": "Власник",
    "manager": "Менеджер",
    "staff": "Працівник",
    "collector": "Збирач",
    "shipper": "Дропшипер",
}

PASSWORD_ENV = {
    "owner": os.getenv("CRM_PASSWORD_OWNER", "").strip(),
    "manager": os.getenv("CRM_PASSWORD_MANAGER", "").strip(),
    "staff": os.getenv("CRM_PASSWORD_STAFF", "").strip(),
    "collector": os.getenv("CRM_PASSWORD_COLLECTOR", "").strip(),
    "shipper": os.getenv("CRM_PASSWORD_SHIPPER", "").strip(),
}

PASSWORDS_MAP = {
    password: role
    for role, password in PASSWORD_ENV.items()
    if password
}

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

    "manager": {
        "create_order",
        "manage_order_status",
        "view_unsent_orders",
        "view_all_orders",
        "search_orders",
        "delete_own_order",
        "delete_any_order",
        "view_shifts",
        "use_ai",
        "use_psychologist",
        "view_tickets",
        "manage_sos",
    },

    "staff": {
        "create_order",
        "view_unsent_orders",
        "view_all_orders",
        "search_orders",
        "delete_own_order",
        "view_shifts",
        "use_ai",
        "use_psychologist",
    },

    "collector": {
        "create_order",
        "view_unsent_orders",
        "view_all_orders",
        "search_orders",
        "delete_own_order",
        "view_shifts",
        "use_ai",
        "use_psychologist",
    },

    "shipper": {
        "create_order",
        "view_all_orders",
        "search_orders",
        "delete_own_order",
        "view_shifts",
        "use_ai",
    },
}

ORDER_STATUSES = {
    "Не зібрано": "not_collected",
    "Зібрано": "collected",
    "Нове": "new",
    "В роботі": "working",
    "Надіслано": "sent",
    "Скасовано": "cancelled",
}

SOS_REASONS = [
    "Загроза / небезпека",
    "Конфлікт",
    "Крадіжка",
    "Медична ситуація",
    "Технічна аварія",
    "Інше",
]

TICKET_CATEGORIES = [
    "Зарплата",
    "Графік",
    "Робоче питання",
    "Конфлікт",
    "Пропозиція",
    "Інше",
]

TICKET_STATUSES = [
    "Нове",
    "У роботі",
    "Очікує відповіді",
    "Вирішено",
    "Закрито",
]


def now_local():
    return datetime.now(TIMEZONE)


def now_text():
    return now_local().strftime("%Y-%m-%d %H:%M:%S")


def parse_dt(value):
    if not value:
        return None

    try:
        return datetime.strptime(
            value,
            "%Y-%m-%d %H:%M:%S",
        ).replace(tzinfo=TIMEZONE)

    except (TypeError, ValueError):
        return None


def esc(value):
    return html.escape(str(value or ""))


def log_event(event, user_id=None, details=None):
    logger.info(
        "event=%s user_id=%s details=%s",
        event,
        user_id,
        json.dumps(
            details or {},
            ensure_ascii=False,
            default=str,
        ),
    )

    try:
        with db_connection() as conn:
            conn.execute(
                """
                INSERT INTO audit_log
                    (user_id, event, details, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    user_id,
                    event,
                    json.dumps(
                        details or {},
                        ensure_ascii=False,
                        default=str,
                    ),
                    now_text(),
                ),
            )

    except Exception:
        logger.exception("audit write failed")


def log_exception(event, user_id=None):
    logger.exception(
        "event=%s user_id=%s",
        event,
        user_id,
    )


@contextmanager
def db_connection():
    with db_lock:
        conn = sqlite3.connect(
            DB_PATH,
            timeout=30,
            check_same_thread=False,
        )

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
    return {
        row["name"]
        for row in conn.execute(
            f"PRAGMA table_info({table})"
        ).fetchall()
    }
def init_db():
    os.makedirs(
        os.path.dirname(os.path.abspath(DB_PATH)),
        exist_ok=True,
    )

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

        CREATE INDEX IF NOT EXISTS idx_ai_conversations_user
        ON ai_conversations(user_id, mode, id);

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
                "order_number": "TEXT DEFAULT ''",
                "phone": "TEXT DEFAULT ''",
                "full_name": "TEXT DEFAULT ''",
                "city": "TEXT DEFAULT ''",
                "branch": "TEXT DEFAULT ''",
                "price": "REAL DEFAULT 0",
                "note": "TEXT DEFAULT ''",
                "status": "TEXT DEFAULT 'Нове'",
                "ttn": "TEXT DEFAULT ''",
                "author": "TEXT DEFAULT ''",
                "created_at": "TEXT DEFAULT ''",
                "sent_at": "TEXT DEFAULT ''",
                "author_id": "INTEGER",
            },
            "blocked_users": {
                "blocked_by": "INTEGER",
                "reason": "TEXT DEFAULT ''",
                "blocked_at": "TEXT DEFAULT ''",
                "created_at": "TEXT DEFAULT ''",
            },
            "shifts": {
                "actual_start": "TEXT DEFAULT ''",
                "actual_end": "TEXT DEFAULT ''",
                "reminder_immediate": "INTEGER DEFAULT 0",
                "reminder_30": "INTEGER DEFAULT 0",
                "reminder_5_start": "INTEGER DEFAULT 0",
                "reminder_5_end": "INTEGER DEFAULT 0",
            },
            "sos_incidents": {
                "acknowledged_by": "INTEGER",
                "acknowledged_at": "TEXT DEFAULT ''",
                "closed_by": "INTEGER",
                "closed_at": "TEXT DEFAULT ''",
                "false_alarm": "INTEGER DEFAULT 0",
                "last_repeat_at": "TEXT DEFAULT ''",
                "repeat_count": "INTEGER DEFAULT 0",
            },
            "tickets": {
                "updated_at": "TEXT DEFAULT ''",
                "assigned_to": "INTEGER",
            },
        }

        for table, columns in migrations.items():
            existing = table_columns(conn, table)

            for column, definition in columns.items():
                if column not in existing:
                    conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN "
                        f"{column} {definition}"
                    )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_number "
            "ON orders(order_number)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_phone "
            "ON orders(phone)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_created "
            "ON orders(created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_shifts_date "
            "ON shifts(shift_date)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sos_status "
            "ON sos_incidents(status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tickets_status "
            "ON tickets(status)"
        )


def get_user(user_id):
    with db_connection() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()


def save_user(
    user_id,
    username="",
    full_name="",
    role="",
):
    with db_connection() as conn:
        conn.execute("""
            INSERT INTO users(
                user_id, username, full_name, role, created_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                full_name=excluded.full_name,
                role=CASE
                    WHEN excluded.role != ''
                    THEN excluded.role
                    ELSE users.role
                END
        """, (
            user_id,
            username or "",
            full_name or "",
            role or "",
            now_text(),
        ))


def is_blocked(user_id):
    user = get_user(user_id)

    if user and user["is_blocked"]:
        return True

    with db_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM blocked_users WHERE user_id=?",
            (user_id,),
        ).fetchone()

    return row is not None


def get_role(user_id):
    user = get_user(user_id)

    if user and user["role"]:
        return user["role"]

    return ""


def has_permission(user_id, permission):
    if is_blocked(user_id):
        return False

    role = get_role(user_id)

    if role == "owner":
        return True

    with db_connection() as conn:
        row = conn.execute(
            """
            SELECT allowed
            FROM user_permissions
            WHERE user_id=? AND permission=?
            """,
            (user_id, permission),
        ).fetchone()

    if row:
        return bool(row["allowed"])

    return permission in ROLE_DEFAULT_PERMISSIONS.get(
        role,
        set(),
    )


def set_permission(user_id, permission, allowed):
    with db_connection() as conn:
        conn.execute("""
            INSERT INTO user_permissions(
                user_id, permission, allowed
            )
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, permission)
            DO UPDATE SET allowed=excluded.allowed
        """, (
            user_id,
            permission,
            int(bool(allowed)),
        ))


def get_all_owners():
    with db_connection() as conn:
        rows = conn.execute(
            """
            SELECT user_id
            FROM users
            WHERE role='owner' AND is_blocked=0
            """
        ).fetchall()

    return [int(row["user_id"]) for row in rows]


def notify_user(user_id, message, reply_markup=None):
    try:
        bot.send_message(
            int(user_id),
            message,
            reply_markup=reply_markup,
        )
        return True

    except Exception as exc:
        logger.warning(
            "Cannot notify user %s: %s",
            user_id,
            exc,
        )
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
        return user_states.get(
            int(user_id),
            {"state": "", "data": {}},
        )


def clear_state(user_id):
    with state_lock:
        user_states.pop(int(user_id), None)


def get_state_data(user_id):
    return get_state(user_id).get("data", {})


def update_state_data(user_id, **kwargs):
    with state_lock:
        current = user_states.setdefault(
            int(user_id),
            {"state": "", "data": {}},
        )
        current.setdefault("data", {}).update(kwargs)


def is_logged_in(user_id):
    return bool(get_role(user_id)) and not is_blocked(user_id)


def require_login(message):
    uid = message.from_user.id

    if is_blocked(uid):
        bot.send_message(
            message.chat.id,
            "⛔ Ваш доступ заблоковано.",
        )
        return False

    if not is_logged_in(uid):
        bot.send_message(
            message.chat.id,
            "Спочатку авторизуйтесь командою /start.",
        )
        return False

    return True


def require_permission(message, permission):
    if not require_login(message):
        return False

    if not has_permission(
        message.from_user.id,
        permission,
    ):
        bot.send_message(
            message.chat.id,
            "⛔ У вас немає права на цю дію.",
        )
        return False

    return True


def main_keyboard(user_id):
    keyboard = types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        row_width=2,
    )

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

    options.extend([
        "🚨 SOS OFFICE",
        "📨 Створити звернення",
    ])

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

    keyboard.add(
        types.KeyboardButton("🏠 Головне меню")
    )

    return keyboard


def show_home(chat_id, user_id, text="Головне меню"):
    bot.send_message(
        chat_id,
        text,
        reply_markup=main_keyboard(user_id),
    )


def cancel_keyboard():
    keyboard = types.ReplyKeyboardMarkup(
        resize_keyboard=True,
    )
    keyboard.row(
        "❌ Скасувати",
        "🏠 Головне меню",
    )
    return keyboard


def cancel_or_home(text):
    return text in {
        "❌ Скасувати",
        "🏠 Головне меню",
        "/cancel",
    }


def get_staff_users():
    with db_connection() as conn:
        return conn.execute("""
            SELECT
                user_id,
                username,
                full_name,
                display_name,
                role
            FROM users
            WHERE role IN (
                'manager',
                'staff',
                'collector',
                'shipper'
            )
            AND is_blocked=0
            ORDER BY
                role,
                COALESCE(
                    NULLIF(display_name, ''),
                    full_name,
                    username
                ),
                user_id
        """).fetchall()


def user_label(user):
    if not user:
        return "Невідомий користувач"

    return (
        user["display_name"]
        or user["full_name"]
        or (
            f"@{user['username']}"
            if user["username"]
            else str(user["user_id"])
        )
    )


def user_label_by_id(user_id):
    return user_label(get_user(user_id))
def format_user(user):
    name = user_label(user)
    role = ROLE_LABELS.get(
        user["role"],
        user["role"] or "Не авторизований",
    )

    return (
        f"{esc(name)} · {esc(role)} · "
        f"ID <code>{user['user_id']}</code>"
    )


def audit_order_action(user_id, action, order_id, details=None):
    payload = {
        "order_id": order_id,
        "action": action,
    }

    if details:
        payload.update(details)

    log_event("order_" + action, user_id, payload)


def get_order(order_id):
    with db_connection() as conn:
        return conn.execute(
            "SELECT * FROM orders WHERE id=?",
            (order_id,),
        ).fetchone()


def can_delete_order(user_id, order):
    if (
        get_role(user_id) in {"owner", "manager"}
        and has_permission(user_id, "delete_any_order")
    ):
        return True

    if not has_permission(user_id, "delete_own_order"):
        return False

    if order["author_id"] is None:
        return False

    return int(order["author_id"]) == int(user_id)


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
    clauses = []
    args = []

    if unsent:
        clauses.append(
            "status NOT IN ('Надіслано','Скасовано')"
        )

    if query:
        clauses.append(
            "(order_number LIKE ? "
            "OR full_name LIKE ? "
            "OR phone LIKE ?)"
        )

        pattern = f"%{query}%"
        args.extend([pattern, pattern, pattern])

    if clauses:
        sql += " WHERE " + " AND ".join(clauses)

    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))

    with db_connection() as conn:
        return conn.execute(sql, args).fetchall()


def create_order_record(user_id, data):
    required = (
        "order_number",
        "phone",
        "full_name",
        "city",
        "branch",
        "price",
    )

    for field in required:
        if not str(data.get(field, "")).strip():
            raise ValueError(
                f"Не заповнене поле: {field}"
            )

    try:
        price = float(
            str(data["price"]).replace(",", ".")
        )

        if price < 0:
            raise ValueError(
                "Сума не може бути від'ємною."
            )

    except (ValueError, TypeError):
        raise ValueError(
            "Сума має бути числом."
        )

    role = get_role(user_id)
    author = ROLE_LABELS.get(role, role)

    with db_connection() as conn:
        cursor = conn.execute("""
            INSERT INTO orders (
                order_number,
                phone,
                full_name,
                city,
                branch,
                price,
                note,
                status,
                ttn,
                author,
                created_at,
                sent_at,
                author_id
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, 'Нове', '', ?, ?, '', ?
            )
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

    audit_order_action(
        user_id,
        "created",
        order_id,
    )

    return order_id


def update_order_status(order_id, status, user_id):
    if status not in ORDER_STATUSES:
        raise ValueError(
            "Невідомий статус замовлення."
        )

    if not is_logged_in(user_id):
        raise PermissionError(
            "Потрібна авторизація."
        )

    role = get_role(user_id)

    if role == "owner":
        allowed = True

    elif status == "Зібрано":
        allowed = (
            role == "collector"
            or has_permission(
                user_id,
                "manage_order_status",
            )
        )

    elif status == "Надіслано":
        allowed = (
            role == "shipper"
            or has_permission(
                user_id,
                "manage_order_status",
            )
        )

    elif status in (
        "Нове",
        "В роботі",
        "Не зібрано",
        "Скасовано",
    ):
        allowed = has_permission(
            user_id,
            "manage_order_status",
        )

    else:
        allowed = False

    if not allowed:
        raise PermissionError(
            "У вас немає права встановлювати цей статус."
        )

    with db_connection() as conn:
        order = conn.execute(
            "SELECT * FROM orders WHERE id=?",
            (order_id,),
        ).fetchone()

        if not order:
            return False

        if (
            status == "Надіслано"
            and not str(order["ttn"] or "").strip()
        ):
            raise ValueError(
                "Не можна позначити замовлення "
                "надісланим без ТТН."
            )

        if (
            status == "Зібрано"
            and order["status"] == "Скасовано"
        ):
            raise ValueError(
                "Скасоване замовлення не можна "
                "позначити зібраним."
            )

        sent_at = (
            now_text()
            if status == "Надіслано"
            else order["sent_at"]
        )

        conn.execute(
            """
            UPDATE orders
            SET status=?, sent_at=?
            WHERE id=?
            """,
            (status, sent_at, order_id),
        )

    audit_order_action(
        user_id,
        "status_changed",
        order_id,
        {"status": status},
    )

    return True


def update_order_ttn(order_id, ttn, user_id):
    ttn = str(ttn or "").strip()

    if not ttn:
        raise ValueError(
            "ТТН не може бути порожньою."
        )

    with db_connection() as conn:
        order = conn.execute(
            """
            SELECT id, status
            FROM orders
            WHERE id=?
            """,
            (order_id,),
        ).fetchone()

        if not order:
            return False

        if order["status"] not in (
            "Зібрано",
            "В роботі",
            "Надіслано",
        ):
            raise PermissionError(
                "Спочатку збирач має позначити "
                "замовлення «Зібрано»."
            )

        conn.execute(
            "UPDATE orders SET ttn=? WHERE id=?",
            (ttn, order_id),
        )

    audit_order_action(
        user_id,
        "ttn_updated",
        order_id,
    )

    return True


def delete_order_record(order_id, user_id):
    with db_connection() as conn:
        order = conn.execute(
            "SELECT * FROM orders WHERE id=?",
            (order_id,),
        ).fetchone()

        if not order:
            return False, "Замовлення не знайдено."

        if not can_delete_order(user_id, order):
            return (
                False,
                "⛔ Немає прав на видалення цього замовлення.",
            )

        snapshot = {
            "order_number": order["order_number"],
            "full_name": order["full_name"],
            "phone": order["phone"],
            "price": order["price"],
            "status": order["status"],
        }

        conn.execute(
            "DELETE FROM orders WHERE id=?",
            (order_id,),
        )

    audit_order_action(
        user_id,
        "deleted",
        order_id,
        snapshot,
    )

    return True, "Замовлення видалено."


def order_action_keyboard(user_id, orders):
    keyboard = types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        row_width=2,
    )

    for order in orders:
        order_id = int(order["id"])

        keyboard.add(
            types.KeyboardButton(
                f"🔎 Замовлення #{order_id}"
            )
        )

        keyboard.row(
            types.KeyboardButton(
                f"🔄 Статус #{order_id}"
            ),
            types.KeyboardButton(
                f"🧾 ТТН #{order_id}"
            ),
        )

        if can_delete_order(user_id, order):
            keyboard.add(
                types.KeyboardButton(
                    f"🗑 Видалити #{order_id}"
                )
            )

    keyboard.row(
        types.KeyboardButton("🏠 Головне меню"),
        types.KeyboardButton("❌ Скасувати"),
    )

    return keyboard


def show_orders(
    chat_id,
    user_id,
    unsent=False,
    query=None,
):
    if (
        unsent
        and not has_permission(
            user_id,
            "view_unsent_orders",
        )
    ):
        bot.send_message(
            chat_id,
            "⛔ Немає доступу до ненадісланих замовлень.",
        )
        return

    if (
        not unsent
        and query is None
        and not has_permission(
            user_id,
            "view_all_orders",
        )
    ):
        bot.send_message(
            chat_id,
            "⛔ Немає доступу до всіх замовлень.",
        )
        return

    if (
        query is not None
        and not has_permission(
            user_id,
            "search_orders",
        )
    ):
        bot.send_message(
            chat_id,
            "⛔ Немає доступу до пошуку.",
        )
        return

    orders = orders_page(
        query=query,
        unsent=unsent,
        limit=20,
    )

    if not orders:
        bot.send_message(
            chat_id,
            "Замовлень не знайдено.",
            reply_markup=main_keyboard(user_id),
        )
        return

    set_state(
        user_id,
        "orders_list",
        {
            "order_ids": [
                int(order["id"])
                for order in orders
            ],
        },
    )

    for order in orders:
        bot.send_message(
            chat_id,
            order_summary(order),
        )

    bot.send_message(
        chat_id,
        (
            "Оберіть дію кнопкою нижче. "
            "Для кожного замовлення доступні лише "
            "дозволені вам дії."
        ),
        reply_markup=order_action_keyboard(
            user_id,
            orders,
        ),
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
    data = dict(state.get("data", {}))
    value = (message.text or "").strip()

    fields = {
        "order_number": (
            "phone",
            "Введи телефон клієнта:",
        ),
        "phone": (
            "full_name",
            "Введи ім'я клієнта:",
        ),
        "full_name": (
            "city",
            "Введи місто:",
        ),
        "city": (
            "branch",
            "Введи номер або адресу відділення:",
        ),
        "branch": (
            "price",
            "Введи суму замовлення:",
        ),
        "price": (
            "note",
            "Введи примітку або напиши «-»:",
        ),
    }

    if current not in fields:
        return False

    if not value:
        bot.send_message(
            message.chat.id,
            "Поле не може бути порожнім.",
        )
        return True

    if current == "price":
        try:
            amount = float(value.replace(",", "."))

            if amount < 0:
                raise ValueError

        except ValueError:
            bot.send_message(
                message.chat.id,
                "Введи коректну невід'ємну суму.",
            )
            return True

    data[current] = value
    next_state, prompt = fields[current]

    set_state(uid, next_state, data)

    bot.send_message(
        message.chat.id,
        prompt,
        reply_markup=cancel_keyboard(),
    )

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
        bot.send_message(
            message.chat.id,
            str(exc),
        )
        return True

    except Exception:
        logger.exception("Order creation failed")

        bot.send_message(
            message.chat.id,
            "Не вдалося створити замовлення. "
            "Перевір журнал бота.",
        )
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
        bot.send_message(
            message.chat.id,
            "⛔ Ваш доступ заблоковано.",
        )
        return

    if user and user["role"]:
        show_home(
            message.chat.id,
            uid,
            "Вітаю в Cloud Hub CRM!",
        )
        return

    set_state(uid, "login", {})

    bot.send_message(
        message.chat.id,
        "🔐 <b>Cloud Hub CRM</b>\n"
        "Введи пароль доступу:",
        reply_markup=types.ReplyKeyboardRemove(),
    )


@bot.message_handler(commands=["cancel"])
def handle_cancel(message):
    uid = message.from_user.id
    clear_state(uid)

    if is_logged_in(uid):
        show_home(
            message.chat.id,
            uid,
            "Дію скасовано.",
        )
    else:
        bot.send_message(
            message.chat.id,
            "Дію скасовано. Для входу введи /start.",
        )


@bot.message_handler(
    func=lambda m: m.text == "🏠 Головне меню"
)
def handle_home_button(message):
    uid = message.from_user.id
    clear_state(uid)

    if is_logged_in(uid):
        show_home(message.chat.id, uid)
    else:
        bot.send_message(
            message.chat.id,
            "Для входу введи /start.",
        )


@bot.message_handler(
    func=lambda m: m.text == "❌ Скасувати"
)
def handle_cancel_button(message):
    handle_cancel(message)


@bot.message_handler(
    func=lambda m: (
        get_state(m.from_user.id).get("state") == "login"
    )
)
def handle_login(message):
    uid = message.from_user.id
    password = (message.text or "").strip()

    if is_blocked(uid):
        clear_state(uid)

        bot.send_message(
            message.chat.id,
            "⛔ Ваш доступ заблоковано.",
        )
        return

    role = PASSWORDS_MAP.get(password)

    if not role:
        with db_connection() as conn:
            row = conn.execute(
                """
                SELECT attempts
                FROM login_attempts
                WHERE user_id=?
                """,
                (uid,),
            ).fetchone()

            attempts = (
                row["attempts"] if row else 0
            ) + 1

            conn.execute("""
                INSERT INTO login_attempts(
                    user_id,
                    attempts,
                    last_attempt
                )
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    attempts=excluded.attempts,
                    last_attempt=excluded.last_attempt
            """, (
                uid,
                attempts,
                now_text(),
            ))

        bot.send_message(
            message.chat.id,
            "❌ Невірний пароль. "
            "Спробуй ще раз або введи /cancel.",
        )
        return

    username = message.from_user.username or ""

    full_name = " ".join(
        filter(
            None,
            [
                message.from_user.first_name,
                message.from_user.last_name,
            ],
        )
    )

    save_user(
        uid,
        username,
        full_name,
        role,
    )

    with db_connection() as conn:
        conn.execute(
            "DELETE FROM login_attempts WHERE user_id=?",
            (uid,),
        )

    clear_state(uid)

    log_event(
        "login_success",
        uid,
        {"role": role},
    )

    bot.send_message(
        message.chat.id,
        "✅ Авторизація успішна. "
        f"Роль: <b>{esc(ROLE_LABELS[role])}</b>",
        reply_markup=main_keyboard(uid),
    )


@bot.message_handler(
    func=lambda m: (
        get_state(m.from_user.id).get("state")
        in {
            "order_number",
            "phone",
            "full_name",
            "city",
            "branch",
            "price",
        }
    )
)
def handle_order_creation_fields(message):
    if cancel_or_home(message.text or ""):
        return

    advance_order_creation(message)


@bot.message_handler(
    func=lambda m: (
        get_state(m.from_user.id).get("state") == "note"
    )
)
def handle_order_note(message):
    if cancel_or_home(message.text or ""):
        return

    finish_order_creation(message)


@bot.message_handler(
    func=lambda m: m.text == "➕ Створити замовлення"
)
def handle_create_order_button(message):
    begin_order_creation(message)


@bot.message_handler(
    func=lambda m: m.text == "📦 Ненадіслані"
)
def handle_unsent_orders(message):
    if require_permission(
        message,
        "view_unsent_orders",
    ):
        show_orders(
            message.chat.id,
            message.from_user.id,
            unsent=True,
        )


@bot.message_handler(
    func=lambda m: m.text == "📋 Усі замовлення"
)
def handle_all_orders(message):
    if require_permission(
        message,
        "view_all_orders",
    ):
        show_orders(
            message.chat.id,
            message.from_user.id,
        )


@bot.message_handler(
    func=lambda m: m.text == "🔎 Пошук"
)
def handle_search_orders(message):
    uid = message.from_user.id

    if not require_permission(message, "search_orders"):
        return

    set_state(uid, "search_orders", {})

    bot.send_message(
        message.chat.id,
        "Введи номер замовлення, ім'я клієнта "
        "або телефон для пошуку:",
        reply_markup=cancel_keyboard(),
    )


@bot.message_handler(
    func=lambda m: (
        get_state(m.from_user.id).get("state")
        == "search_orders"
    )
)
def handle_search_orders_query(message):
    uid = message.from_user.id

    if cancel_or_home(message.text or ""):
        return

    query = (message.text or "").strip()

    if not query:
        bot.send_message(
            message.chat.id,
            "Введи текст для пошуку.",
        )
        return

    clear_state(uid)

    show_orders(
        message.chat.id,
        uid,
        query=query,
    )
def begin_status_change(message, order_id):
    uid = message.from_user.id

    if not is_logged_in(uid):
        bot.send_message(
            message.chat.id,
            "Спочатку авторизуйся командою /start.",
        )
        return

    order = get_order(order_id)

    if not order:
        bot.send_message(
            message.chat.id,
            "Замовлення не знайдено.",
        )
        return

    if not (
        has_permission(uid, "view_all_orders")
        or has_permission(uid, "view_unsent_orders")
    ):
        bot.send_message(
            message.chat.id,
            "⛔ Немає доступу до цього замовлення.",
        )
        return

    keyboard = types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        row_width=2,
    )

    for status in ORDER_STATUSES:
        keyboard.add(types.KeyboardButton(status))

    keyboard.row(
        types.KeyboardButton("❌ Скасувати"),
        types.KeyboardButton("🏠 Головне меню"),
    )

    set_state(
        uid,
        "choose_order_status",
        {"order_id": order_id},
    )

    bot.send_message(
        message.chat.id,
        f"Оберіть новий статус для замовлення #{order_id}:",
        reply_markup=keyboard,
    )


def begin_ttn_update(message, order_id):
    uid = message.from_user.id

    if not is_logged_in(uid):
        bot.send_message(
            message.chat.id,
            "Спочатку авторизуйся командою /start.",
        )
        return

    if (
        get_role(uid) not in {"owner", "manager", "shipper"}
        and not has_permission(uid, "manage_users")
    ):
        bot.send_message(
            message.chat.id,
            "⛔ Немає права додавати ТТН.",
        )
        return

    order = get_order(order_id)

    if not order:
        bot.send_message(
            message.chat.id,
            "Замовлення не знайдено.",
        )
        return

    set_state(
        uid,
        "order_ttn",
        {"order_id": order_id},
    )

    bot.send_message(
        message.chat.id,
        f"Введи ТТН для замовлення #{order_id}:",
        reply_markup=cancel_keyboard(),
    )


def begin_order_deletion(message, order_id):
    uid = message.from_user.id

    if not is_logged_in(uid):
        bot.send_message(
            message.chat.id,
            "Спочатку авторизуйся командою /start.",
        )
        return

    order = get_order(order_id)

    if not order:
        bot.send_message(
            message.chat.id,
            "Замовлення не знайдено.",
        )
        return

    if not can_delete_order(uid, order):
        bot.send_message(
            message.chat.id,
            "⛔ Немає прав на видалення цього замовлення.",
        )
        return

    keyboard = types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        row_width=2,
    )

    keyboard.row(
        types.KeyboardButton(
            f"✅ Підтвердити видалення #{order_id}"
        ),
        types.KeyboardButton(
            f"↩️ Скасувати видалення #{order_id}"
        ),
    )

    keyboard.add(
        types.KeyboardButton("🏠 Головне меню")
    )

    set_state(
        uid,
        "confirm_order_deletion",
        {"order_id": order_id},
    )

    bot.send_message(
        message.chat.id,
        f"⚠️ Точно видалити замовлення #{order_id}?\n\n"
        "Після підтвердження запис буде видалено з бази даних.",
        reply_markup=keyboard,
    )


@bot.message_handler(
    func=lambda m: (
        m.text or ""
    ).startswith("🔄 Статус #")
)
def handle_order_status_button(message):
    try:
        order_id = int(
            message.text.rsplit("#", 1)[1]
        )
    except (ValueError, IndexError):
        bot.send_message(
            message.chat.id,
            "Не вдалося визначити номер замовлення.",
        )
        return

    begin_status_change(message, order_id)


@bot.message_handler(
    func=lambda m: (
        get_state(m.from_user.id).get("state")
        == "choose_order_status"
    )
)
def handle_order_status_selection(message):
    uid = message.from_user.id
    text = (message.text or "").strip()

    if cancel_or_home(text):
        return

    state = get_state(uid)
    order_id = state.get("data", {}).get("order_id")

    if text not in ORDER_STATUSES:
        bot.send_message(
            message.chat.id,
            "Оберіть статус кнопкою з клавіатури.",
        )
        return

    try:
        success = update_order_status(
            int(order_id),
            text,
            uid,
        )

        if not success:
            bot.send_message(
                message.chat.id,
                "Замовлення не знайдено.",
            )
            clear_state(uid)
            return

    except (PermissionError, ValueError) as exc:
        bot.send_message(
            message.chat.id,
            f"⛔ {esc(str(exc))}",
        )
        return

    except Exception:
        logger.exception(
            "Reply-keyboard status update failed"
        )
        bot.send_message(
            message.chat.id,
            "Не вдалося змінити статус. "
            "Перевір журнал бота.",
        )
        return

    clear_state(uid)

    bot.send_message(
        message.chat.id,
        f"✅ Статус замовлення #{order_id} змінено: "
        f"<b>{esc(text)}</b>",
        reply_markup=main_keyboard(uid),
    )


@bot.message_handler(
    func=lambda m: (
        (m.text or "").startswith("🧾 ТТН #")
    )
)
def handle_order_ttn_button(message):
    try:
        order_id = int(
            message.text.rsplit("#", 1)[1]
        )
    except (ValueError, IndexError):
        bot.send_message(
            message.chat.id,
            "Не вдалося визначити номер замовлення.",
        )
        return

    begin_ttn_update(message, order_id)


@bot.message_handler(
    func=lambda m: (
        get_state(m.from_user.id).get("state")
        == "order_ttn"
    )
)
def handle_order_ttn_input(message):
    uid = message.from_user.id
    text = (message.text or "").strip()

    if cancel_or_home(text):
        return

    state = get_state(uid)
    order_id = state.get("data", {}).get("order_id")

    if not text:
        bot.send_message(
            message.chat.id,
            "ТТН не може бути порожньою.",
        )
        return

    try:
        success = update_order_ttn(
            int(order_id),
            text,
            uid,
        )

        if not success:
            bot.send_message(
                message.chat.id,
                "Замовлення не знайдено.",
            )
            clear_state(uid)
            return

    except (PermissionError, ValueError) as exc:
        bot.send_message(
            message.chat.id,
            f"⛔ {esc(str(exc))}",
        )
        return

    except Exception:
        logger.exception(
            "Reply-keyboard TTN update failed"
        )
        bot.send_message(
            message.chat.id,
            "Не вдалося зберегти ТТН.",
        )
        return

    clear_state(uid)

    bot.send_message(
        message.chat.id,
        f"✅ ТТН для замовлення #{order_id} збережено.",
        reply_markup=main_keyboard(uid),
    )


@bot.message_handler(
    func=lambda m: (
        (m.text or "").startswith("🗑 Видалити #")
    )
)
def handle_order_delete_button(message):
    try:
        order_id = int(
            message.text.rsplit("#", 1)[1]
        )
    except (ValueError, IndexError):
        bot.send_message(
            message.chat.id,
            "Не вдалося визначити номер замовлення.",
        )
        return

    begin_order_deletion(message, order_id)


@bot.message_handler(
    func=lambda m: (
        get_state(m.from_user.id).get("state")
        == "confirm_order_deletion"
    )
)
def handle_order_deletion_confirmation(message):
    uid = message.from_user.id
    text = (message.text or "").strip()

    if cancel_or_home(text):
        return

    state = get_state(uid)
    order_id = state.get("data", {}).get("order_id")

    expected_confirmation = (
        f"✅ Підтвердити видалення #{order_id}"
    )
    expected_cancel = (
        f"↩️ Скасувати видалення #{order_id}"
    )

    if text == expected_cancel:
        clear_state(uid)

        bot.send_message(
            message.chat.id,
            "Видалення скасовано.",
            reply_markup=main_keyboard(uid),
        )
        return

    if text != expected_confirmation:
        bot.send_message(
            message.chat.id,
            "Оберіть підтвердження або скасування "
            "кнопкою з клавіатури.",
        )
        return

    try:
        success, result = delete_order_record(
            int(order_id),
            uid,
        )

    except Exception:
        logger.exception(
            "Reply-keyboard order deletion failed"
        )
        bot.send_message(
            message.chat.id,
            "Не вдалося видалити замовлення.",
        )
        return

    clear_state(uid)

    bot.send_message(
        message.chat.id,
        esc(result),
        reply_markup=main_keyboard(uid),
    )


@bot.message_handler(
    func=lambda m: (
        (m.text or "").startswith("🔎 Замовлення #")
    )
)
def handle_order_details_button(message):
    uid = message.from_user.id

    try:
        order_id = int(
            message.text.rsplit("#", 1)[1]
        )
    except (ValueError, IndexError):
        bot.send_message(
            message.chat.id,
            "Не вдалося визначити номер замовлення.",
        )
        return

    if not is_logged_in(uid):
        bot.send_message(
            message.chat.id,
            "Спочатку авторизуйся командою /start.",
        )
        return

    order = get_order(order_id)

    if not order:
        bot.send_message(
            message.chat.id,
            "Замовлення не знайдено.",
        )
        return

    if not (
        has_permission(uid, "view_all_orders")
        or has_permission(uid, "view_unsent_orders")
        or has_permission(uid, "search_orders")
    ):
        bot.send_message(
            message.chat.id,
            "⛔ Немає доступу до замовлення.",
        )
        return

    bot.send_message(
        message.chat.id,
        order_summary(order),
        reply_markup=main_keyboard(uid),
    )
def shift_action_keyboard(shifts):
    keyboard = types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        row_width=2,
    )

    for shift in shifts:
        shift_id = int(shift["id"])

        if shift["status"] == "planned":
            keyboard.add(
                types.KeyboardButton(
                    f"▶️ Почати зміну #{shift_id}"
                )
            )

        elif shift["status"] == "started":
            keyboard.add(
                types.KeyboardButton(
                    f"⏹ Завершити зміну #{shift_id}"
                )
            )

    keyboard.row(
        types.KeyboardButton("🏠 Головне меню"),
        types.KeyboardButton("❌ Скасувати"),
    )

    return keyboard


def get_shift_for_user(shift_id, user_id):
    with db_connection() as conn:
        return conn.execute(
            """
            SELECT *
            FROM shifts
            WHERE id=? AND user_id=?
            """,
            (shift_id, user_id),
        ).fetchone()


def start_shift_for_user(shift_id, user_id):
    if not is_logged_in(user_id):
        raise PermissionError("Потрібна авторизація.")

    with db_connection() as conn:
        shift = conn.execute(
            "SELECT * FROM shifts WHERE id=?",
            (shift_id,),
        ).fetchone()

        if not shift:
            return False, "Зміну не знайдено."

        if int(shift["user_id"]) != int(user_id):
            return False, "Це не твоя зміна."

        if shift["status"] != "planned":
            return False, "Зміну вже почато або завершено."

        conn.execute(
            """
            UPDATE shifts
            SET status='started', actual_start=?
            WHERE id=? AND status='planned'
            """,
            (now_text(), shift_id),
        )

    log_event(
        "shift_started",
        user_id,
        {"shift_id": shift_id},
    )

    notify_owners(
        f"▶️ {esc(user_label_by_id(user_id))} "
        f"почав зміну #{shift_id} о {esc(now_text())}."
    )

    return True, f"▶️ Зміну #{shift_id} розпочато."


def end_shift_for_user(shift_id, user_id):
    if not is_logged_in(user_id):
        raise PermissionError("Потрібна авторизація.")

    with db_connection() as conn:
        shift = conn.execute(
            "SELECT * FROM shifts WHERE id=?",
            (shift_id,),
        ).fetchone()

        if not shift:
            return False, "Зміну не знайдено."

        if int(shift["user_id"]) != int(user_id):
            return False, "Це не твоя зміна."

        if shift["status"] != "started":
            return False, "Зміна не позначена як розпочата."

        conn.execute(
            """
            UPDATE shifts
            SET status='completed', actual_end=?
            WHERE id=? AND status='started'
            """,
            (now_text(), shift_id),
        )

    log_event(
        "shift_ended",
        user_id,
        {"shift_id": shift_id},
    )

    notify_owners(
        f"⏹ {esc(user_label_by_id(user_id))} "
        f"завершив зміну #{shift_id} о {esc(now_text())}."
    )

    return True, f"⏹ Зміну #{shift_id} завершено."


@bot.message_handler(
    func=lambda m: m.text == "🕒 Мої зміни"
)
def handle_my_shifts(message):
    uid = message.from_user.id

    if not require_permission(message, "view_shifts"):
        return

    shifts = get_user_shifts(uid)

    if not shifts:
        bot.send_message(
            message.chat.id,
            "У тебе поки немає призначених змін.",
            reply_markup=main_keyboard(uid),
        )
        return

    for shift in shifts:
        bot.send_message(
            message.chat.id,
            format_shift(shift),
        )

    bot.send_message(
        message.chat.id,
        "Обери дію для потрібної зміни:",
        reply_markup=shift_action_keyboard(shifts),
    )


@bot.message_handler(
    func=lambda m: (
        (m.text or "").startswith("▶️ Почати зміну #")
    )
)
def handle_shift_start_button(message):
    uid = message.from_user.id

    try:
        shift_id = int(
            message.text.rsplit("#", 1)[1]
        )
    except (ValueError, IndexError):
        bot.send_message(
            message.chat.id,
            "Не вдалося визначити номер зміни.",
        )
        return

    try:
        success, result = start_shift_for_user(
            shift_id,
            uid,
        )

    except PermissionError as exc:
        bot.send_message(
            message.chat.id,
            esc(str(exc)),
        )
        return

    except Exception:
        logger.exception("Shift start failed")

        bot.send_message(
            message.chat.id,
            "Не вдалося почати зміну.",
        )
        return

    bot.send_message(
        message.chat.id,
        esc(result),
        reply_markup=main_keyboard(uid) if success
        else shift_action_keyboard(get_user_shifts(uid)),
    )


@bot.message_handler(
    func=lambda m: (
        (m.text or "").startswith("⏹ Завершити зміну #")
    )
)
def handle_shift_end_button(message):
    uid = message.from_user.id

    try:
        shift_id = int(
            message.text.rsplit("#", 1)[1]
        )
    except (ValueError, IndexError):
        bot.send_message(
            message.chat.id,
            "Не вдалося визначити номер зміни.",
        )
        return

    try:
        success, result = end_shift_for_user(
            shift_id,
            uid,
        )

    except PermissionError as exc:
        bot.send_message(
            message.chat.id,
            esc(str(exc)),
        )
        return

    except Exception:
        logger.exception("Shift end failed")

        bot.send_message(
            message.chat.id,
            "Не вдалося завершити зміну.",
        )
        return

    bot.send_message(
        message.chat.id,
        esc(result),
        reply_markup=main_keyboard(uid) if success
        else shift_action_keyboard(get_user_shifts(uid)),
    )
def shift_employee_keyboard(users):
    keyboard = types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        row_width=1,
    )

    for user in users:
        name = user_label(user)
        role = ROLE_LABELS.get(
            user["role"],
            user["role"],
        )

        keyboard.add(
            types.KeyboardButton(
                f"👤 {name} · {role} · ID {user['user_id']}"
            )
        )

    keyboard.row(
        types.KeyboardButton("❌ Скасувати"),
        types.KeyboardButton("🏠 Головне меню"),
    )

    return keyboard


@bot.message_handler(
    func=lambda m: m.text in (
        "🗓 Керування змінами",
        "🗓 Призначити зміни",
    )
)
def handle_manage_shifts(message):
    uid = message.from_user.id

    if not require_permission(message, "manage_shifts"):
        return

    users = get_staff_users()

    if not users:
        bot.send_message(
            message.chat.id,
            "Спочатку працівники мають авторизуватися в боті.",
            reply_markup=main_keyboard(uid),
        )
        return

    set_state(
        uid,
        "select_shift_employee",
        {},
    )

    bot.send_message(
        message.chat.id,
        "Оберіть працівника, якому призначити зміну:",
        reply_markup=shift_employee_keyboard(users),
    )


@bot.message_handler(
    func=lambda m: (
        get_state(m.from_user.id).get("state")
        == "select_shift_employee"
    )
)
def handle_shift_employee_selection(message):
    uid = message.from_user.id
    text = (message.text or "").strip()

    if cancel_or_home(text):
        return

    prefix = "👤 "

    if not text.startswith(prefix) or " · ID " not in text:
        bot.send_message(
            message.chat.id,
            "Оберіть працівника кнопкою зі списку.",
        )
        return

    try:
        employee_id = int(text.rsplit(" · ID ", 1)[1])
    except (ValueError, IndexError):
        bot.send_message(
            message.chat.id,
            "Не вдалося визначити працівника. "
            "Відкрийте список ще раз.",
        )
        clear_state(uid)
        return

    employee = get_user(employee_id)

    if (
        not employee
        or employee["role"] not in {
            "manager",
            "staff",
            "collector",
            "shipper",
        }
        or is_blocked(employee_id)
    ):
        bot.send_message(
            message.chat.id,
            "Працівника не знайдено або він недоступний.",
        )
        clear_state(uid)
        return

    set_state(
        uid,
        "shift_date",
        {"employee_id": employee_id},
    )

    bot.send_message(
        message.chat.id,
        "Введи дату зміни у форматі YYYY-MM-DD:",
        reply_markup=cancel_keyboard(),
    )


@bot.message_handler(
    func=lambda m: (
        get_state(m.from_user.id).get("state")
        == "shift_date"
    )
)
def handle_shift_date(message):
    uid = message.from_user.id
    value = (message.text or "").strip()

    if cancel_or_home(value):
        return

    try:
        day = datetime.strptime(
            value,
            "%Y-%m-%d",
        ).date()

        if day < now_local().date():
            bot.send_message(
                message.chat.id,
                "Дата не може бути в минулому.",
            )
            return

    except ValueError:
        bot.send_message(
            message.chat.id,
            "Невірна дата. Приклад: 2026-10-24",
        )
        return

    update_state_data(
        shift_date=day.isoformat(),
    )

    with state_lock:
        user_states[uid]["state"] = "shift_start_time"

    bot.send_message(
        message.chat.id,
        "Введи час початку у форматі HH:MM, наприклад 09:00:",
        reply_markup=cancel_keyboard(),
    )


@bot.message_handler(
    func=lambda m: (
        get_state(m.from_user.id).get("state")
        == "shift_start_time"
    )
)
def handle_shift_start_time(message):
    uid = message.from_user.id
    value = (message.text or "").strip()

    if cancel_or_home(value):
        return

    try:
        datetime.strptime(value, "%H:%M")
    except ValueError:
        bot.send_message(
            message.chat.id,
            "Невірний час. Приклад: 09:00",
        )
        return

    update_state_data(start_time=value)

    with state_lock:
        user_states[uid]["state"] = "shift_end_time"

    bot.send_message(
        message.chat.id,
        "Введи час завершення у форматі HH:MM, наприклад 18:00:",
        reply_markup=cancel_keyboard(),
    )


@bot.message_handler(
    func=lambda m: (
        get_state(m.from_user.id).get("state")
        == "shift_end_time"
    )
)
def handle_shift_end_time(message):
    uid = message.from_user.id
    value = (message.text or "").strip()

    if cancel_or_home(value):
        return

    try:
        end_time = datetime.strptime(
            value,
            "%H:%M",
        ).time()
    except ValueError:
        bot.send_message(
            message.chat.id,
            "Невірний час. Приклад: 18:00",
        )
        return

    state = get_state(uid)
    data = state.get("data", {})

    try:
        start_time = datetime.strptime(
            data["start_time"],
            "%H:%M",
        ).time()

        if end_time <= start_time:
            bot.send_message(
                message.chat.id,
                "Час завершення має бути пізніше часу початку. "
                "Нічні зміни поки не підтримуються.",
            )
            return

        employee_id = int(data["employee_id"])
        day = data["shift_date"]

        with db_connection() as conn:
            overlap = conn.execute(
                """
                SELECT id
                FROM shifts
                WHERE user_id=?
                  AND shift_date=?
                  AND status IN ('planned', 'started')
                  AND start_time < ?
                  AND end_time > ?
                LIMIT 1
                """,
                (
                    employee_id,
                    day,
                    value,
                    data["start_time"],
                ),
            ).fetchone()

            if overlap:
                bot.send_message(
                    message.chat.id,
                    f"У працівника вже є зміна #{overlap['id']} "
                    "у цей час. Нову зміну не створено.",
                )
                return

            cursor = conn.execute(
                """
                INSERT INTO shifts (
                    user_id,
                    shift_date,
                    start_time,
                    end_time,
                    status,
                    created_by,
                    created_at
                )
                VALUES (?, ?, ?, ?, 'planned', ?, ?)
                """,
                (
                    employee_id,
                    day,
                    data["start_time"],
                    value,
                    uid,
                    now_text(),
                ),
            )

            shift_id = cursor.lastrowid

        clear_state(uid)

        log_event(
            "shift_assigned",
            uid,
            {
                "shift_id": shift_id,
                "employee_id": employee_id,
                "date": day,
                "start": data["start_time"],
                "end": value,
            },
        )

        notify_user(
            employee_id,
            f"🗓 <b>Тобі призначено зміну #{shift_id}</b>\n"
            f"Дата: {esc(day)}\n"
            f"Час: {esc(data['start_time'])}–{esc(value)}\n"
            "Переглянути зміну можна в меню «🕒 Мої зміни».",
        )

        bot.send_message(
            message.chat.id,
            f"✅ Зміну #{shift_id} призначено.",
            reply_markup=main_keyboard(uid),
        )

    except Exception:
        logger.exception("Shift creation failed")

        bot.send_message(
            message.chat.id,
            "Не вдалося створити зміну. "
            "Перевір журнал бота.",
        )
def user_management_keyboard(target):
    uid = int(target["user_id"])
    keyboard = types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        row_width=2,
    )

    if is_blocked(uid):
        keyboard.add(
            types.KeyboardButton(f"🔓 Розблокувати ID {uid}")
        )
    else:
        keyboard.add(
            types.KeyboardButton(f"⛔ Заблокувати ID {uid}")
        )

    keyboard.add(
        types.KeyboardButton(f"✏️ Ім'я CRM ID {uid}")
    )

    if has_permission(
        target["user_id"],
        "manage_permissions",
    ):
        keyboard.add(
            types.KeyboardButton(f"🔐 Права ID {uid}")
        )

    keyboard.row(
        types.KeyboardButton("👥 Користувачі"),
        types.KeyboardButton("🏠 Головне меню"),
    )

    return keyboard


@bot.message_handler(
    func=lambda m: m.text == "👥 Користувачі"
)
def handle_users_menu(message):
    uid = message.from_user.id

    if not require_permission(message, "manage_users"):
        return

    with db_connection() as conn:
        users = conn.execute("""
            SELECT user_id, username, full_name,
                   display_name, role, is_blocked
            FROM users
            ORDER BY role, user_id
            LIMIT 100
        """).fetchall()

    if not users:
        bot.send_message(
            message.chat.id,
            "Користувачів поки немає.",
            reply_markup=main_keyboard(uid),
        )
        return

    keyboard = types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        row_width=1,
    )

    for user in users:
        name = user_label(user)
        status = (
            "⛔ заблокований"
            if is_blocked(user["user_id"])
            else "✅ активний"
        )

        keyboard.add(
            types.KeyboardButton(
                f"👤 {name} · ID {user['user_id']} · {status}"
            )
        )

    keyboard.row(
        types.KeyboardButton("🏠 Головне меню"),
        types.KeyboardButton("❌ Скасувати"),
    )

    set_state(uid, "select_managed_user", {})

    bot.send_message(
        message.chat.id,
        "👥 <b>Користувачі CRM</b>\n"
        "Оберіть користувача:",
        reply_markup=keyboard,
    )


@bot.message_handler(
    func=lambda m: (
        get_state(m.from_user.id).get("state")
        == "select_managed_user"
    )
)
def handle_select_managed_user(message):
    uid = message.from_user.id
    text = (message.text or "").strip()

    if cancel_or_home(text):
        return

    if not text.startswith("👤 ") or " · ID " not in text:
        bot.send_message(
            message.chat.id,
            "Оберіть користувача кнопкою зі списку.",
        )
        return

    try:
        target_id = int(
            text.split(" · ID ", 1)[1].split(" · ", 1)[0]
        )
    except (ValueError, IndexError):
        bot.send_message(
            message.chat.id,
            "Не вдалося визначити ID користувача.",
        )
        return

    target = get_user(target_id)

    if not target:
        bot.send_message(
            message.chat.id,
            "Користувача не знайдено.",
        )
        return

    clear_state(uid)

    set_state(
        uid,
        "manage_user",
        {"target_id": target_id},
    )

    bot.send_message(
        message.chat.id,
        format_user(target),
        reply_markup=user_management_keyboard(target),
    )


@bot.message_handler(
    func=lambda m: (
        (m.text or "").startswith("⛔ Заблокувати ID ")
    )
)
def handle_block_user_button(message):
    uid = message.from_user.id

    if not require_permission(message, "manage_users"):
        return

    try:
        target_id = int(
            message.text.rsplit(" ", 1)[1]
        )
    except (ValueError, IndexError):
        bot.send_message(
            message.chat.id,
            "Некоректний ID користувача.",
        )
        return

    if target_id == uid:
        bot.send_message(
            message.chat.id,
            "Не можна заблокувати самого себе.",
        )
        return

    target = get_user(target_id)

    if not target:
        bot.send_message(
            message.chat.id,
            "Користувача не знайдено.",
        )
        return

    with db_connection() as conn:
        conn.execute("""
            INSERT INTO blocked_users(
                user_id, blocked_by, reason, blocked_at, created_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                blocked_by=excluded.blocked_by,
                reason=excluded.reason,
                blocked_at=excluded.blocked_at
        """, (
            target_id,
            uid,
            "Заблоковано адміністратором",
            now_text(),
            now_text(),
        ))

        conn.execute(
            "UPDATE users SET is_blocked=1 WHERE user_id=?",
            (target_id,),
        )

    log_event(
        "user_blocked",
        uid,
        {"target_id": target_id},
    )

    bot.send_message(
        message.chat.id,
        f"⛔ Користувача {target_id} заблоковано.",
        reply_markup=main_keyboard(uid),
    )


@bot.message_handler(
    func=lambda m: (
        (m.text or "").startswith("🔓 Розблокувати ID ")
    )
)
def handle_unblock_user_button(message):
    uid = message.from_user.id

    if not require_permission(message, "manage_users"):
        return

    try:
        target_id = int(
            message.text.rsplit(" ", 1)[1]
        )
    except (ValueError, IndexError):
        bot.send_message(
            message.chat.id,
            "Некоректний ID користувача.",
        )
        return

    with db_connection() as conn:
        conn.execute(
            "DELETE FROM blocked_users WHERE user_id=?",
            (target_id,),
        )
        conn.execute(
            "UPDATE users SET is_blocked=0 WHERE user_id=?",
            (target_id,),
        )

    log_event(
        "user_unblocked",
        uid,
        {"target_id": target_id},
    )

    bot.send_message(
        message.chat.id,
        f"✅ Користувача {target_id} розблоковано.",
        reply_markup=main_keyboard(uid),
    )


@bot.message_handler(
    func=lambda m: (
        (m.text or "").startswith("✏️ Ім'я CRM ID ")
    )
)
def handle_edit_user_name_button(message):
    uid = message.from_user.id

    if not require_permission(message, "manage_users"):
        return

    try:
        target_id = int(
            message.text.rsplit(" ", 1)[1]
        )
    except (ValueError, IndexError):
        bot.send_message(
            message.chat.id,
            "Некоректний ID користувача.",
        )
        return

    if not get_user(target_id):
        bot.send_message(
            message.chat.id,
            "Користувача не знайдено.",
        )
        return

    set_state(
        uid,
        "set_display_name",
        {"target_id": target_id},
    )

    bot.send_message(
        message.chat.id,
        "Введи ім'я для CRM, від 1 до 60 символів:",
        reply_markup=cancel_keyboard(),
    )


@bot.message_handler(
    func=lambda m: (
        get_state(m.from_user.id).get("state")
        == "set_display_name"
    )
)
def handle_set_display_name(message):
    uid = message.from_user.id

    if cancel_or_home(message.text or ""):
        return

    if not require_permission(message, "manage_users"):
        return

    name = (message.text or "").strip()

    if not name or len(name) > 60:
        bot.send_message(
            message.chat.id,
            "Ім'я має містити від 1 до 60 символів.",
        )
        return

    target_id = int(
        get_state_data(uid).get("target_id", 0)
    )

    with db_connection() as conn:
        cur = conn.execute(
            "UPDATE users SET display_name=? WHERE user_id=?",
            (name, target_id),
        )

    if not cur.rowcount:
        bot.send_message(
            message.chat.id,
            "Користувача не знайдено.",
        )
        return

    clear_state(uid)

    log_event(
        "user_display_name_changed",
        uid,
        {
            "target_id": target_id,
            "display_name": name,
        },
    )

    bot.send_message(
        message.chat.id,
        f"✅ Ім'я збережено: <b>{esc(name)}</b>",
        reply_markup=main_keyboard(uid),
    )


def sos_reason_keyboard():
    keyboard = types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        row_width=1,
    )

    for reason in SOS_REASONS:
        keyboard.add(types.KeyboardButton(reason))

    keyboard.row(
        types.KeyboardButton("❌ Скасувати"),
        types.KeyboardButton("🏠 Головне меню"),
    )

    return keyboard


@bot.message_handler(
    func=lambda m: m.text == "🚨 SOS OFFICE"
)
def handle_sos_start(message):
    uid = message.from_user.id

    if not require_login(message):
        return

    set_state(uid, "sos_reason", {})

    bot.send_message(
        message.chat.id,
        "🚨 <b>SOS OFFICE</b>\n"
        "Обери тип інциденту.\n\n"
        "Якщо є безпосередня загроза життю, "
        "телефонуй 112 або 103.",
        reply_markup=sos_reason_keyboard(),
    )


@bot.message_handler(
    func=lambda m: (
        get_state(m.from_user.id).get("state")
        == "sos_reason"
    )
)
def handle_sos_reason(message):
    uid = message.from_user.id
    reason = (message.text or "").strip()

    if cancel_or_home(reason):
        return

    if reason not in SOS_REASONS:
        bot.send_message(
            message.chat.id,
            "Оберіть тип інциденту кнопкою зі списку.",
        )
        return

    set_state(
        uid,
        "sos_description",
        {"reason": reason},
    )

    bot.send_message(
        message.chat.id,
        "Опиши ситуацію або надішли «-», "
        "якщо опису немає:",
        reply_markup=cancel_keyboard(),
    )


@bot.message_handler(
    func=lambda m: (
        get_state(m.from_user.id).get("state")
        == "sos_description"
    )
)
def handle_sos_description(message):
    uid = message.from_user.id
    description = (message.text or "").strip()

    if cancel_or_home(description):
        return

    if not description:
        bot.send_message(
            message.chat.id,
            "Введи опис або «-».",
        )
        return

    state = get_state(uid)
    data = dict(state.get("data", {}))
    data["description"] = (
        "" if description == "-" else description
    )

    set_state(
        uid,
        "sos_confirm",
        data,
    )

    keyboard = types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        row_width=2,
    )

    keyboard.row(
        types.KeyboardButton("🚨 Підтвердити SOS"),
        types.KeyboardButton("❌ Скасувати"),
    )

    bot.send_message(
        message.chat.id,
        "Перевір SOS-запит:\n"
        f"Категорія: <b>{esc(data['reason'])}</b>\n"
        f"Опис: <b>{esc(data['description'] or 'Не вказано')}</b>\n\n"
        "Якщо ситуація невідкладна, не чекай відповіді в боті.",
        reply_markup=keyboard,
    )


@bot.message_handler(
    func=lambda m: (
        get_state(m.from_user.id).get("state")
        == "sos_confirm"
        and m.text == "🚨 Підтвердити SOS"
    )
)
def handle_sos_confirm(message):
    uid = message.from_user.id

    if not is_logged_in(uid):
        bot.send_message(
            message.chat.id,
            "Спочатку авторизуйся командою /start.",
        )
        return

    state = get_state(uid)

    if state.get("state") != "sos_confirm":
        bot.send_message(
            message.chat.id,
            "Запит застарів. Створи його знову.",
        )
        return

    data = state.get("data", {})

    with db_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO sos_incidents(
                user_id, reason, description, status, created_at
            )
            VALUES (?, ?, ?, 'active', ?)
            """,
            (
                uid,
                data.get("reason", "Інше"),
                data.get("description", ""),
                now_text(),
            ),
        )

        incident_id = cursor.lastrowid

    clear_state(uid)

    log_event(
        "sos_created",
        uid,
        {"incident_id": incident_id},
    )

    user = get_user(uid)

    display_name = (
        user["full_name"] or user["username"] or str(uid)
        if user
        else str(uid)
    )

    notification = (
        f"🚨 <b>SOS OFFICE #{incident_id}</b>\n"
        f"Хто: {esc(display_name)}\n"
        f"ID: <code>{uid}</code>\n"
        f"Причина: {esc(data.get('reason', 'Інше'))}\n"
        f"Час: {esc(now_text())}\n"
        f"Опис: {esc(data.get('description') or 'Не вказано')}\n\n"
        "Для обробки відкрийте меню SOS OFFICE."
    )

    sent = notify_owners(notification)

    bot.send_message(
        message.chat.id,
        (
            f"🚨 SOS-запит #{incident_id} створено. "
            f"Власників повідомлено: {sent}."
        ),
        reply_markup=main_keyboard(uid),
    )
def ticket_category_keyboard():
    keyboard = types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        row_width=1,
    )
    for category in TICKET_CATEGORIES:
        keyboard.add(types.KeyboardButton(category))
    keyboard.row(
        types.KeyboardButton("❌ Скасувати"),
        types.KeyboardButton("🏠 Головне меню"),
    )
    return keyboard


@bot.message_handler(func=lambda m: m.text == "📨 Створити звернення")
def handle_ticket_start(message):
    uid = message.from_user.id
    if not require_login(message):
        return

    set_state(uid, "ticket_category", {})
    bot.send_message(
        message.chat.id,
        "📨 Обери категорію звернення:",
        reply_markup=ticket_category_keyboard(),
    )


@bot.message_handler(
    func=lambda m: get_state(m.from_user.id).get("state") == "ticket_category"
)
def handle_ticket_category(message):
    uid = message.from_user.id
    category = (message.text or "").strip()

    if cancel_or_home(category):
        return

    if category not in TICKET_CATEGORIES:
        bot.send_message(
            message.chat.id,
            "Обери категорію кнопкою зі списку.",
        )
        return

    set_state(uid, "ticket_message", {"category": category})
    bot.send_message(
        message.chat.id,
        f"Категорія: <b>{esc(category)}</b>\nОпиши звернення:",
        reply_markup=cancel_keyboard(),
    )


@bot.message_handler(
    func=lambda m: get_state(m.from_user.id).get("state") == "ticket_message"
)
def handle_ticket_message(message):
    uid = message.from_user.id
    content = (message.text or "").strip()

    if cancel_or_home(content):
        return

    if len(content) < 3:
        bot.send_message(
            message.chat.id,
            "Опиши звернення трохи детальніше.",
        )
        return

    state = get_state(uid)
    category = state.get("data", {}).get("category", "Інше")

    with db_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO tickets(
                user_id, category, message, status, created_at, updated_at
            )
            VALUES (?, ?, ?, 'Нове', ?, ?)
            """,
            (uid, category, content, now_text(), now_text()),
        )
        ticket_id = cursor.lastrowid

    clear_state(uid)
    log_event(
        "ticket_created",
        uid,
        {"ticket_id": ticket_id, "category": category},
    )

    user = get_user(uid)
    name = (
        user["full_name"] or user["username"] or str(uid)
        if user else str(uid)
    )

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


def ticket_action_keyboard(ticket_id):
    keyboard = types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        row_width=2,
    )
    keyboard.row(
        types.KeyboardButton(f"✉️ Відповісти #{ticket_id}"),
        types.KeyboardButton(f"🔄 Статус #{ticket_id}"),
    )
    keyboard.row(
        types.KeyboardButton("🎫 Звернення"),
        types.KeyboardButton("🏠 Головне меню"),
    )
    return keyboard


@bot.message_handler(func=lambda m: m.text == "🎫 Звернення")
def handle_tickets_list(message):
    uid = message.from_user.id
    if not require_permission(message, "view_tickets"):
        return

    with db_connection() as conn:
        tickets = conn.execute(
            """
            SELECT t.*, u.full_name, u.username
            FROM tickets t
            LEFT JOIN users u ON u.user_id = t.user_id
            ORDER BY t.id DESC
            LIMIT 30
            """
        ).fetchall()

    if not tickets:
        bot.send_message(
            message.chat.id,
            "Звернень поки немає.",
            reply_markup=main_keyboard(uid),
        )
        return

    for ticket in tickets:
        name = (
            ticket["full_name"]
            or ticket["username"]
            or str(ticket["user_id"])
        )
        ticket_id = ticket["id"]

        bot.send_message(
            message.chat.id,
            f"🎫 <b>Звернення #{ticket_id}</b>\n"
            f"Автор: {esc(name)}\n"
            f"Категорія: {esc(ticket['category'])}\n"
            f"Статус: {esc(ticket['status'])}\n"
            f"Текст: {esc(ticket['message'])}",
            reply_markup=ticket_action_keyboard(ticket_id),
        )


@bot.message_handler(
    func=lambda m: (m.text or "").startswith("✉️ Відповісти #")
)
def handle_ticket_reply_start(message):
    uid = message.from_user.id
    if not require_permission(message, "view_tickets"):
        return

    try:
        ticket_id = int(message.text.rsplit("#", 1)[1])
    except (ValueError, IndexError):
        bot.send_message(message.chat.id, "Некоректний номер звернення.")
        return

    with db_connection() as conn:
        ticket = conn.execute(
            "SELECT id FROM tickets WHERE id = ?",
            (ticket_id,),
        ).fetchone()

    if not ticket:
        bot.send_message(message.chat.id, "Звернення не знайдено.")
        return

    set_state(uid, "ticket_admin_reply", {"ticket_id": ticket_id})
    bot.send_message(
        message.chat.id,
        f"Введи відповідь для звернення #{ticket_id}:",
        reply_markup=cancel_keyboard(),
    )


@bot.message_handler(
    func=lambda m: get_state(m.from_user.id).get("state") == "ticket_admin_reply"
)
def handle_ticket_admin_reply(message):
    uid = message.from_user.id
    reply = (message.text or "").strip()

    if cancel_or_home(reply):
        return

    if not require_permission(message, "view_tickets"):
        clear_state(uid)
        return

    if not reply:
        bot.send_message(message.chat.id, "Відповідь не може бути порожньою.")
        return

    state = get_state(uid)
    ticket_id = int(state.get("data", {}).get("ticket_id", 0))

    with db_connection() as conn:
        ticket = conn.execute(
            "SELECT * FROM tickets WHERE id = ?",
            (ticket_id,),
        ).fetchone()

        if not ticket:
            clear_state(uid)
            bot.send_message(message.chat.id, "Звернення не знайдено.")
            return

        conn.execute(
            """
            INSERT INTO ticket_replies(ticket_id, user_id, message, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (ticket_id, uid, reply, now_text()),
        )
        conn.execute(
            """
            UPDATE tickets
            SET status = 'Очікує відповіді', updated_at = ?, assigned_to = ?
            WHERE id = ?
            """,
            (now_text(), uid, ticket_id),
        )

    clear_state(uid)
    log_event("ticket_replied", uid, {"ticket_id": ticket_id})

    notify_user(
        ticket["user_id"],
        f"✉️ Відповідь на звернення #{ticket_id}:\n\n{esc(reply)}",
    )

    bot.send_message(
        message.chat.id,
        f"✅ Відповідь на звернення #{ticket_id} надіслано.",
        reply_markup=main_keyboard(uid),
    )


@bot.message_handler(
    func=lambda m: (m.text or "").startswith("🔄 Статус #")
)
def handle_ticket_status_start(message):
    uid = message.from_user.id
    if not require_permission(message, "view_tickets"):
        return

    try:
        ticket_id = int(message.text.rsplit("#", 1)[1])
    except (ValueError, IndexError):
        bot.send_message(message.chat.id, "Некоректний номер звернення.")
        return

    with db_connection() as conn:
        ticket = conn.execute(
            "SELECT id FROM tickets WHERE id = ?",
            (ticket_id,),
        ).fetchone()

    if not ticket:
        bot.send_message(message.chat.id, "Звернення не знайдено.")
        return

    set_state(uid, "ticket_set_status", {"ticket_id": ticket_id})

    keyboard = types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        row_width=1,
    )
    for status in TICKET_STATUSES:
        keyboard.add(types.KeyboardButton(status))
    keyboard.row(
        types.KeyboardButton("❌ Скасувати"),
        types.KeyboardButton("🏠 Головне меню"),
    )

    bot.send_message(
        message.chat.id,
        f"Обери новий статус звернення #{ticket_id}:",
        reply_markup=keyboard,
    )


@bot.message_handler(
    func=lambda m: get_state(m.from_user.id).get("state") == "ticket_set_status"
)
def handle_ticket_set_status(message):
    uid = message.from_user.id
    status = (message.text or "").strip()

    if cancel_or_home(status):
        return

    if status not in TICKET_STATUSES:
        bot.send_message(
            message.chat.id,
            "Обери статус кнопкою зі списку.",
        )
        return

    if not require_permission(message, "view_tickets"):
        clear_state(uid)
        return

    state = get_state(uid)
    ticket_id = int(state.get("data", {}).get("ticket_id", 0))

    with db_connection() as conn:
        ticket = conn.execute(
            "SELECT id FROM tickets WHERE id = ?",
            (ticket_id,),
        ).fetchone()

        if not ticket:
            clear_state(uid)
            bot.send_message(message.chat.id, "Звернення не знайдено.")
            return

        conn.execute(
            "UPDATE tickets SET status = ?, updated_at = ? WHERE id = ?",
            (status, now_text(), ticket_id),
        )

    clear_state(uid)
    log_event(
        "ticket_status_changed",
        uid,
        {"ticket_id": ticket_id, "status": status},
    )

    bot.send_message(
        message.chat.id,
        f"✅ Статус звернення #{ticket_id}: <b>{esc(status)}</b>",
        reply_markup=main_keyboard(uid),
    )
@bot.message_handler(func=lambda m: m.text == "🤖 AI-помічник")
def handle_ai_start(message):
    uid = message.from_user.id
    if not require_permission(message, "use_ai"):
        return

    set_state(uid, "ai_question", {})
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.row(
        types.KeyboardButton("🧹 Очистити історію AI"),
        types.KeyboardButton("🏠 Головне меню"),
    )
    keyboard.add(types.KeyboardButton("❌ Скасувати"))

    bot.send_message(
        message.chat.id,
        "🤖 Напиши запитання. Можна ставити кілька запитань поспіль.\n"
        "Щоб завершити, натисни «Скасувати» або «Головне меню».",
        reply_markup=keyboard,
    )


@bot.message_handler(func=lambda m: m.text == "🧹 Очистити історію AI")
def handle_ai_clear_history(message):
    uid = message.from_user.id
    if not require_permission(message, "use_ai"):
        return

    clear_ai_history(uid, "assistant")
    set_state(uid, "ai_question", {})
    bot.send_message(
        message.chat.id,
        "🧹 Історію AI-помічника очищено. Можеш починати нову розмову.",
    )


@bot.message_handler(
    func=lambda m: get_state(m.from_user.id).get("state") == "ai_question"
)
def handle_ai_question(message):
    uid = message.from_user.id
    question = (message.text or "").strip()

    if cancel_or_home(question):
        return

    if question == "🧹 Очистити історію AI":
        clear_ai_history(uid, "assistant")
        bot.send_message(message.chat.id, "Історію очищено.")
        return

    if not require_permission(message, "use_ai"):
        clear_state(uid)
        return

    if not question:
        bot.send_message(message.chat.id, "Напиши запитання.")
        return

    answer = ai_answer(uid, question)
    send_long_message(
        message.chat.id,
        esc(answer),
        reply_markup=cancel_keyboard(),
    )


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
        keyboard = types.ReplyKeyboardMarkup(
            resize_keyboard=True,
            row_width=1,
        )
        keyboard.add(types.KeyboardButton("✅ Погоджуюсь"))
        keyboard.add(types.KeyboardButton("❌ Не погоджуюсь"))

        set_state(uid, "psych_consent", {})
        bot.send_message(
            message.chat.id,
            "💬 Це режим емоційної підтримки, а не заміна психолога чи лікаря.\n\n"
            "Текст повідомлень передаватиметься OpenAI для створення відповіді. "
            "Не надсилай паролі та інші дані, які не хочеш передавати сервісу.\n\n"
            "У разі безпосередньої небезпеки звернися до екстрених служб.",
            reply_markup=keyboard,
        )
        return

    set_state(uid, "psych_question", {})
    bot.send_message(
        message.chat.id,
        "Що тебе зараз турбує? Можеш писати кілька повідомлень поспіль.",
        reply_markup=cancel_keyboard(),
    )


@bot.message_handler(
    func=lambda m: get_state(m.from_user.id).get("state") == "psych_consent"
)
def handle_psych_consent(message):
    uid = message.from_user.id
    answer = (message.text or "").strip()

    if answer == "❌ Не погоджуюсь":
        clear_state(uid)
        bot.send_message(
            message.chat.id,
            "Режим психологічної підтримки не активовано.",
            reply_markup=main_keyboard(uid),
        )
        return

    if answer != "✅ Погоджуюсь":
        bot.send_message(
            message.chat.id,
            "Будь ласка, обери одну з кнопок.",
        )
        return

    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO ai_preferences(user_id, psychologist_consent)
            VALUES (?, 1)
            ON CONFLICT(user_id)
            DO UPDATE SET psychologist_consent = 1
            """,
            (uid,),
        )

    set_state(uid, "psych_question", {})
    bot.send_message(
        message.chat.id,
        "Згоду збережено. Можеш написати, що відчуваєш.",
        reply_markup=cancel_keyboard(),
    )


@bot.message_handler(
    func=lambda m: get_state(m.from_user.id).get("state") == "psych_question"
)
def handle_psych_question(message):
    uid = message.from_user.id
    content = (message.text or "").strip()

    if cancel_or_home(content):
        return

    if not require_permission(message, "use_psychologist"):
        clear_state(uid)
        return

    if not content:
        bot.send_message(message.chat.id, "Напиши повідомлення.")
        return

    urgent_phrases = (
        "хочу вбити себе",
        "покінчити з життям",
        "зараз накладу на себе руки",
        "хочу померти",
    )

    if any(phrase in content.casefold() for phrase in urgent_phrases):
        bot.send_message(
            message.chat.id,
            "Мені шкода, що тобі настільки важко. Якщо ти можеш нашкодити "
            "собі прямо зараз, відійди від небезпечних предметів, звернися "
            "до людини поруч і телефонуй 112 або 103. Я можу підтримати тебе "
            "в розмові, але не можу викликати допомогу замість тебе.",
        )
        return

    if not OPENAI_API_KEY:
        bot.send_message(
            message.chat.id,
            "Режим тимчасово недоступний: не налаштований OPENAI_API_KEY.",
        )
        return

    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=OPENAI_API_KEY,
            timeout=45.0,
            max_retries=2,
        )

        history = get_ai_history(uid, "psychologist", limit=100)
        history.append({
            "role": "user",
            "content": content[:12000],
        })

        response = client.responses.create(
            model=OPENAI_MODEL,
            instructions=(
                "Ти уважний помічник емоційної підтримки. Відповідай українською, "
                "співчутливо та без осуду. Не став діагнозів і не призначай ліків. "
                "Підтримуй діалог і враховуй попередні репліки. Якщо є ризик "
                "самоушкодження, заохочуй звернутися до людини поруч та місцевої "
                "екстреної допомоги. Не стверджуй, що викликав допомогу."
            ),
            input=history,
        )

        answer = response.output_text or "Не вдалося сформувати відповідь."
        save_ai_turn(
            uid,
            "psychologist",
            content[:12000],
            answer[:20000],
        )

        send_long_message(
            message.chat.id,
            esc(answer),
            reply_markup=cancel_keyboard(),
        )

    except Exception:
        logger.exception("Psychology assistant failed")
        bot.send_message(
            message.chat.id,
            "Зараз не вдалося отримати відповідь. Спробуй пізніше.",
        )
BROADCAST_ROLE_LABELS = {
    "all": "👥 Усі користувачі",
    "owner": "👑 Власники",
    "manager": "🧑‍💼 Менеджери",
    "staff": "👤 Працівники",
    "collector": "📦 Комплектувальники",
    "shipper": "🚚 Відправники",
    "individual": "🎯 Окремим людям",
}


def broadcast_role_keyboard():
    keyboard = types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        row_width=2,
    )
    keyboard.row(
        types.KeyboardButton(BROADCAST_ROLE_LABELS["all"]),
        types.KeyboardButton(BROADCAST_ROLE_LABELS["individual"]),
    )
    for role in ("owner", "manager", "staff", "collector", "shipper"):
        keyboard.add(types.KeyboardButton(BROADCAST_ROLE_LABELS[role]))
    keyboard.row(
        types.KeyboardButton("❌ Скасувати"),
        types.KeyboardButton("🏠 Головне меню"),
    )
    return keyboard


def broadcast_confirmation_keyboard():
    keyboard = types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        row_width=2,
    )
    keyboard.row(
        types.KeyboardButton("✅ Підтвердити розсилку"),
        types.KeyboardButton("❌ Скасувати"),
    )
    return keyboard


@bot.message_handler(func=lambda m: m.text == "📣 Розсилка")
def handle_broadcast_start(message):
    uid = message.from_user.id
    if not require_permission(message, "broadcast"):
        return

    set_state(uid, "broadcast_recipients", {})
    bot.send_message(
        message.chat.id,
        "📣 Обери аудиторію для розсилки:",
        reply_markup=broadcast_role_keyboard(),
    )


@bot.message_handler(
    func=lambda m: get_state(m.from_user.id).get("state") == "broadcast_recipients"
)
def handle_broadcast_recipients(message):
    uid = message.from_user.id
    choice = (message.text or "").strip()

    if cancel_or_home(choice):
        return

    if not require_permission(message, "broadcast"):
        clear_state(uid)
        return

    role_by_label = {
        label: role for role, label in BROADCAST_ROLE_LABELS.items()
    }
    role = role_by_label.get(choice)

    if role is None:
        bot.send_message(
            message.chat.id,
            "Обери аудиторію кнопкою зі списку.",
        )
        return

    if role == "individual":
        set_state(uid, "broadcast_individual_ids", {})
        bot.send_message(
            message.chat.id,
            "Введи ID, @username або точне відображуване ім’я отримувачів "
            "через кому.\n"
            "Бот шукатиме серед користувачів, які вже авторизувалися.",
            reply_markup=cancel_keyboard(),
        )
        return

    set_state(uid, "broadcast_message", {"role": role})
    bot.send_message(
        message.chat.id,
        "Введи текст розсилки. Перед відправленням буде попередній перегляд.",
        reply_markup=cancel_keyboard(),
    )


@bot.message_handler(
    func=lambda m: get_state(m.from_user.id).get("state") == "broadcast_individual_ids"
)
def handle_broadcast_individual_ids(message):
    uid = message.from_user.id
    content = (message.text or "").strip()

    if cancel_or_home(content):
        return

    if not require_permission(message, "broadcast"):
        clear_state(uid)
        return

    tokens = [
        token.strip().lstrip("@")
        for token in content.split(",")
        if token.strip()
    ]

    if not tokens:
        bot.send_message(message.chat.id, "Вкажи хоча б одного отримувача.")
        return

    with db_connection() as conn:
        users = conn.execute(
            """
            SELECT user_id, username, full_name, display_name
            FROM users
            WHERE role != '' AND is_blocked = 0
            """
        ).fetchall()

    found = []
    missing = []

    for token in tokens:
        normalized = token.casefold()
        matches = []

        for user in users:
            candidates = {
                str(user["user_id"]).casefold(),
                str(user["username"] or "").lstrip("@").casefold(),
                str(user["full_name"] or "").casefold(),
                str(user["display_name"] or "").casefold(),
            }
            if normalized in candidates:
                matches.append(int(user["user_id"]))

        if matches:
            found.extend(matches)
        else:
            missing.append(token)

    found = sorted(set(found))

    if not found:
        bot.send_message(
            message.chat.id,
            "Не знайшов отримувачів. Перевір ID, username або ім’я.",
        )
        return

    set_state(
        uid,
        "broadcast_message",
        {
            "role": "individual",
            "recipients": found,
        },
    )

    extra = ""
    if missing:
        extra = "\nНе знайдено: " + ", ".join(esc(x) for x in missing)

    bot.send_message(
        message.chat.id,
        f"Знайдено отримувачів: {len(found)}.{extra}\n"
        "Тепер введи текст розсилки.",
        reply_markup=cancel_keyboard(),
    )


@bot.message_handler(
    func=lambda m: get_state(m.from_user.id).get("state") == "broadcast_message"
)
def handle_broadcast_message(message):
    uid = message.from_user.id
    content = (message.text or "").strip()

    if cancel_or_home(content):
        return

    if not require_permission(message, "broadcast"):
        clear_state(uid)
        return

    if not content:
        bot.send_message(message.chat.id, "Текст не може бути порожнім.")
        return

    state = get_state(uid)
    data = state.get("data", {})
    role = data.get("role", "all")

    if role == "individual":
        recipients = [int(x) for x in data.get("recipients", [])]
    else:
        with db_connection() as conn:
            if role == "all":
                rows = conn.execute(
                    """
                    SELECT user_id FROM users
                    WHERE role != '' AND is_blocked = 0
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT user_id FROM users
                    WHERE role = ? AND is_blocked = 0
                    """,
                    (role,),
                ).fetchall()

        recipients = [int(row["user_id"]) for row in rows]

    if not recipients:
        clear_state(uid)
        bot.send_message(
            message.chat.id,
            "Немає доступних отримувачів для цієї розсилки.",
            reply_markup=main_keyboard(uid),
        )
        return

    set_state(
        uid,
        "broadcast_confirm",
        {
            "role": role,
            "content": content,
            "recipients": recipients,
        },
    )

    preview = content[:1000]
    if len(content) > 1000:
        preview += "\n… (текст скорочено для попереднього перегляду)"

    bot.send_message(
        message.chat.id,
        f"📣 <b>Підтвердження розсилки</b>\n"
        f"Отримувачів: <b>{len(recipients)}</b>\n\n"
        f"Попередній перегляд:\n{esc(preview)}",
        reply_markup=broadcast_confirmation_keyboard(),
    )


@bot.message_handler(
    func=lambda m: (
        get_state(m.from_user.id).get("state") == "broadcast_confirm"
        and m.text == "❌ Скасувати"
    )
)
def handle_broadcast_cancel(message):
    uid = message.from_user.id
    clear_state(uid)
    bot.send_message(
        message.chat.id,
        "Розсилку скасовано.",
        reply_markup=main_keyboard(uid),
    )


@bot.message_handler(
    func=lambda m: (
        get_state(m.from_user.id).get("state") == "broadcast_confirm"
        and m.text == "✅ Підтвердити розсилку"
    )
)
def handle_broadcast_confirm(message):
    uid = message.from_user.id

    if not require_permission(message, "broadcast"):
        clear_state(uid)
        return

    state = get_state(uid)
    if state.get("state") != "broadcast_confirm":
        bot.send_message(message.chat.id, "Розсилка застаріла.")
        return

    data = state.get("data", {})
    content = data.get("content", "")
    recipients = sorted(set(
        int(x) for x in data.get("recipients", [])
    ))

    if not content or not recipients:
        clear_state(uid)
        bot.send_message(
            message.chat.id,
            "Немає тексту або отримувачів. Створи розсилку заново.",
            reply_markup=main_keyboard(uid),
        )
        return

    sent = 0
    failed = 0

    for recipient_id in recipients:
        # Перевіряємо, що користувач досі не заблокований.
        with db_connection() as conn:
            current_user = conn.execute(
                "SELECT is_blocked FROM users WHERE user_id = ?",
                (recipient_id,),
            ).fetchone()

        if not current_user or current_user["is_blocked"]:
            ok = False
        else:
            ok = notify_user(recipient_id, content)

        status = "sent" if ok else "failed"

        with db_connection() as conn:
            conn.execute(
                """
                INSERT INTO broadcast_log(
                    sender_id, recipient_id, message, status, created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (uid, recipient_id, content, status, now_text()),
            )

        if ok:
            sent += 1
        else:
            failed += 1

        time.sleep(0.05)

    clear_state(uid)
    log_event(
        "broadcast_completed",
        uid,
        {"sent": sent, "failed": failed},
    )

    bot.send_message(
        message.chat.id,
        "📣 Розсилку завершено.\n"
        f"Успішно: {sent}\n"
        f"Не доставлено: {failed}",
        reply_markup=main_keyboard(uid),
    )


@bot.message_handler(
    func=lambda m: m.text == "❌ Скасувати"
    and get_state(m.from_user.id).get("state")
    in ("broadcast_recipients", "broadcast_individual_ids", "broadcast_message")
)
def handle_broadcast_cancel_before_confirm(message):
    uid = message.from_user.id
    clear_state(uid)
    bot.send_message(
        message.chat.id,
        "Розсилку скасовано.",
        reply_markup=main_keyboard(uid),
    )
def permissions_keyboard(target_id):
    keyboard = types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        row_width=1,
    )

    for permission, label in PERMISSIONS.items():
        allowed = has_permission(target_id, permission)
        mark = "✅" if allowed else "❌"
        keyboard.add(
            types.KeyboardButton(
                f"{mark} {label} · {permission}"
            )
        )

    keyboard.row(
        types.KeyboardButton("⬅️ До користувача"),
        types.KeyboardButton("🏠 Головне меню"),
    )
    return keyboard


def show_user_permissions(chat_id, target_id):
    user = get_user(target_id)
    if not user:
        bot.send_message(chat_id, "Користувача не знайдено.")
        return

    lines = [
        f"🔐 <b>Права користувача</b>",
        f"ID: <code>{target_id}</code>",
        f"Роль: {esc(get_role(target_id) or 'не призначена')}",
        "",
    ]

    for permission, label in PERMISSIONS.items():
        status = "✅ Дозволено" if has_permission(target_id, permission) else "❌ Заборонено"
        lines.append(f"{esc(label)}: {status}")

    bot.send_message(
        chat_id,
        "\n".join(lines),
        reply_markup=permissions_keyboard(target_id),
    )


@bot.message_handler(func=lambda m: m.text == "🔐 Права користувача")
def handle_permissions_menu(message):
    uid = message.from_user.id

    if not require_permission(message, "manage_permissions"):
        return

    set_state(uid, "permissions_target", {})
    bot.send_message(
        message.chat.id,
        "Введи Telegram ID користувача, права якого потрібно переглянути:",
        reply_markup=cancel_keyboard(),
    )


@bot.message_handler(
    func=lambda m: get_state(m.from_user.id).get("state") == "permissions_target"
)
def handle_permissions_target(message):
    uid = message.from_user.id
    value = (message.text or "").strip()

    if cancel_or_home(value):
        return

    if not require_permission(message, "manage_permissions"):
        clear_state(uid)
        return

    try:
        target_id = int(value)
    except ValueError:
        bot.send_message(message.chat.id, "Введи числовий Telegram ID.")
        return

    if not get_user(target_id):
        bot.send_message(message.chat.id, "Користувача з таким ID не знайдено.")
        return

    clear_state(uid)

    set_state(
        uid,
        "manage_permissions",
        {"target_id": target_id},
    )
    show_user_permissions(message.chat.id, target_id)


@bot.message_handler(
    func=lambda m: (
        get_state(m.from_user.id).get("state") == "manage_permissions"
        and (m.text or "").startswith(("✅ ", "❌ "))
    )
)
def handle_permission_toggle(message):
    uid = message.from_user.id
    value = (message.text or "").strip()

    if not require_permission(message, "manage_permissions"):
        clear_state(uid)
        return

    state = get_state(uid)
    target_id = int(state.get("data", {}).get("target_id", 0))

    if not target_id or not get_user(target_id):
        clear_state(uid)
        bot.send_message(message.chat.id, "Користувача не знайдено.")
        return

    if get_role(target_id) == "owner":
        bot.send_message(
            message.chat.id,
            "Права власника не змінюються через це меню.",
        )
        return

    permission = None
    for key, label in PERMISSIONS.items():
        if value.endswith(f"· {key}"):
            permission = key
            break

    if permission not in PERMISSIONS:
        bot.send_message(message.chat.id, "Не вдалося визначити право.")
        return

    new_value = not has_permission(target_id, permission)
    set_permission(target_id, permission, new_value)

    log_event(
        "permission_changed",
        uid,
        {
            "target_id": target_id,
            "permission": permission,
            "allowed": new_value,
        },
    )

    bot.send_message(
        message.chat.id,
        f"{'✅ Дозволено' if new_value else '❌ Заборонено'}: "
        f"{esc(PERMISSIONS[permission])}",
    )
    show_user_permissions(message.chat.id, target_id)


@bot.message_handler(
    func=lambda m: (
        m.text == "⬅️ До користувача"
        and get_state(m.from_user.id).get("state") == "manage_permissions"
    )
)
def handle_permissions_back(message):
    uid = message.from_user.id
    state = get_state(uid)
    target_id = int(state.get("data", {}).get("target_id", 0))

    if not require_permission(message, "manage_permissions"):
        clear_state(uid)
        return

    if not target_id:
        clear_state(uid)
        bot.send_message(message.chat.id, "Спочатку обери користувача.")
        return

    set_state(uid, "manage_user", {"target_id": target_id})

    user = get_user(target_id)
    bot.send_message(
        message.chat.id,
        format_user(user),
        reply_markup=user_management_keyboard(user),
    )
def shift_reminder_keyboard():
    keyboard = types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        row_width=2,
    )
    keyboard.row(
        types.KeyboardButton("▶️ Відкрити мою зміну"),
        types.KeyboardButton("⏹ Закрити мою зміну"),
    )
    keyboard.add(types.KeyboardButton("🏠 Головне меню"))
    return keyboard


@bot.message_handler(func=lambda m: m.text == "▶️ Відкрити мою зміну")
def handle_reminder_start_shift(message):
    uid = message.from_user.id
    if not require_login(message):
        return
    start_shift_for_user(message, uid)


@bot.message_handler(func=lambda m: m.text == "⏹ Закрити мою зміну")
def handle_reminder_end_shift(message):
    uid = message.from_user.id
    if not require_login(message):
        return
    end_shift_for_user(message, uid)


def sos_owner_keyboard():
    keyboard = types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        row_width=1,
    )
    keyboard.add(types.KeyboardButton("🚨 Активні SOS"))
    keyboard.add(types.KeyboardButton("🏠 Головне меню"))
    return keyboard


@bot.message_handler(func=lambda m: m.text == "🚨 Активні SOS")
def handle_active_sos(message):
    uid = message.from_user.id
    if not require_permission(message, "manage_sos"):
        return

    with db_connection() as conn:
        incidents = conn.execute(
            """
            SELECT *
            FROM sos_incidents
            WHERE status != 'closed' AND false_alarm = 0
            ORDER BY id DESC
            LIMIT 30
            """
        ).fetchall()

    if not incidents:
        bot.send_message(
            message.chat.id,
            "Активних SOS немає.",
            reply_markup=main_keyboard(uid),
        )
        return

    for incident in incidents:
        keyboard = types.ReplyKeyboardMarkup(
            resize_keyboard=True,
            row_width=1,
        )
        keyboard.add(
            types.KeyboardButton(
                f"✅ Підтвердити SOS #{incident['id']}"
            )
        )
        keyboard.add(
            types.KeyboardButton(
                f"🔒 Закрити SOS #{incident['id']}"
            )
        )
        keyboard.add(types.KeyboardButton("🚨 Активні SOS"))
        keyboard.add(types.KeyboardButton("🏠 Головне меню"))

        bot.send_message(
            message.chat.id,
            f"🚨 <b>SOS #{incident['id']}</b>\n"
            f"Користувач: <code>{incident['user_id']}</code>\n"
            f"Причина: {esc(incident['reason'])}\n"
            f"Опис: {esc(incident['description'] or 'Не вказано')}\n"
            f"Статус: {esc(incident['status'])}\n"
            f"Створено: {esc(incident['created_at'])}",
            reply_markup=keyboard,
        )


@bot.message_handler(
    func=lambda m: (m.text or "").startswith("✅ Підтвердити SOS #")
)
def handle_sos_ack_button(message):
    uid = message.from_user.id
    if not require_permission(message, "manage_sos"):
        return

    try:
        incident_id = int(message.text.rsplit("#", 1)[1])
    except (ValueError, IndexError):
        bot.send_message(message.chat.id, "Некоректний номер SOS.")
        return

    with db_connection() as conn:
        incident = conn.execute(
            "SELECT * FROM sos_incidents WHERE id = ?",
            (incident_id,),
        ).fetchone()

        if not incident:
            bot.send_message(message.chat.id, "Інцидент не знайдено.")
            return

        if incident["status"] == "closed":
            bot.send_message(message.chat.id, "Інцидент уже закрито.")
            return

        conn.execute(
            """
            UPDATE sos_incidents
            SET status = 'acknowledged',
                acknowledged_by = ?,
                acknowledged_at = ?
            WHERE id = ?
            """,
            (uid, now_text(), incident_id),
        )

    log_event("sos_acknowledged", uid, {"incident_id": incident_id})

    notify_user(
        incident["user_id"],
        f"✅ SOS #{incident_id}: отримання підтверджено. "
        "Якщо небезпека триває, звернися до екстрених служб.",
    )

    bot.send_message(
        message.chat.id,
        f"✅ Отримання SOS #{incident_id} підтверджено.",
        reply_markup=sos_owner_keyboard(),
    )


@bot.message_handler(
    func=lambda m: (m.text or "").startswith("🔒 Закрити SOS #")
)
def handle_sos_close_button(message):
    uid = message.from_user.id
    if not require_permission(message, "manage_sos"):
        return

    try:
        incident_id = int(message.text.rsplit("#", 1)[1])
    except (ValueError, IndexError):
        bot.send_message(message.chat.id, "Некоректний номер SOS.")
        return

    with db_connection() as conn:
        incident = conn.execute(
            "SELECT * FROM sos_incidents WHERE id = ?",
            (incident_id,),
        ).fetchone()

        if not incident:
            bot.send_message(message.chat.id, "Інцидент не знайдено.")
            return

        if incident["status"] == "closed":
            bot.send_message(message.chat.id, "Інцидент уже закрито.")
            return

        conn.execute(
            """
            UPDATE sos_incidents
            SET status = 'closed'
            WHERE id = ?
            """,
            (incident_id,),
        )

    log_event("sos_closed", uid, {"incident_id": incident_id})

    notify_user(
        incident["user_id"],
        f"🔒 SOS #{incident_id} закрито відповідальною особою. "
        "Якщо небезпека триває, телефонуй до екстрених служб.",
    )

    bot.send_message(
        message.chat.id,
        f"🔒 SOS #{incident_id} закрито.",
        reply_markup=sos_owner_keyboard(),
    )
def run_bot():
    validate_configuration()
    init_db()
    logger.info("Cloud Hub CRM starting. DB=%s", DB_PATH)

    worker = threading.Thread(
        target=reminder_worker,
        daemon=True,
        name="crm-reminders",
    )
    worker.start()

    try:
        bot.infinity_polling(
            skip_pending=False,
            timeout=30,
            long_polling_timeout=30,
        )
    finally:
        stop_event.set()


if __name__ == "__main__":
    run_bot()