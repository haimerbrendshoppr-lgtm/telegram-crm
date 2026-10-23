
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


# ============================================================
# CLOUD HUB CRM: CONFIGURATION
# ============================================================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DB_PATH = os.getenv("CRM_DB_PATH", "/var/crm_bot.db")
TIMEZONE = ZoneInfo("Europe/Kyiv")

if not TOKEN:
    raise RuntimeError(
        "Не задано TELEGRAM_BOT_TOKEN у змінних середовища Render."
    )

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("cloud_hub_crm")


# ============================================================
# ROLES AND PASSWORD CONFIGURATION
# ============================================================

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

PASSWORDS_MAP = {
    os.getenv("CRM_PASSWORD_OWNER", ""): "owner",
    os.getenv("CRM_PASSWORD_MANAGER", ""): "manager",
    os.getenv("CRM_PASSWORD_STAFF", ""): "staff",
    os.getenv("CRM_PASSWORD_COLLECTOR", ""): "collector",
    os.getenv("CRM_PASSWORD_SHIPPER", ""): "shipper",
}
PASSWORDS_MAP.pop("", None)


# ============================================================
# SHARED STATE
# ============================================================

user_states = {}
state_lock = threading.RLock()
db_lock = threading.RLock()
reminder_lock = threading.RLock()

active_reminders = {}


# ============================================================
# TIME AND LOGGING HELPERS
# ============================================================

def now_local():
    return datetime.now(TIMEZONE)


def now_text():
    return now_local().strftime("%Y-%m-%d %H:%M:%S")


def log_event(event, user_id=None, details=None):
    """Записує службові події без секретів і приватних діалогів."""
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


def log_exception(event, user_id=None):
    logger.error(
        "event=%s user_id=%s\n%s",
        event,
        user_id,
        traceback.format_exc(),
    )


# ============================================================
# SQLITE CONNECTION
# ============================================================

@contextmanager
def db_connection():
    """Єдина точка доступу до SQLite."""
    with db_lock:
        conn = sqlite3.connect(
            DB_PATH,
            timeout=30,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row

        try:
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def table_columns(conn, table_name):
    rows = conn.execute(
        f"PRAGMA table_info({table_name})"
    ).fetchall()
    return {row["name"] for row in rows}


def add_column_if_missing(
    conn,
    table_name,
    column_name,
    definition,
):
    if column_name not in table_columns(conn, table_name):
        conn.execute(
            f"ALTER TABLE {table_name} "
            f"ADD COLUMN {column_name} {definition}"
        )


# ============================================================
# DATABASE INITIALIZATION
# ============================================================

def init_db():
    """
    Створює таблиці та додає відсутні колонки.
    Існуючі замовлення не видаляє.
    """
    with db_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                role TEXT,
                name TEXT,
                failed_attempts INTEGER DEFAULT 0,
                blocked_until TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_number TEXT,
                phone TEXT,
                full_name TEXT,
                city TEXT,
                branch TEXT,
                price TEXT,
                note TEXT,
                status TEXT DEFAULT 'Нове',
                ttn TEXT DEFAULT '',
                author TEXT,
                created_at TEXT,
                sent_at TEXT DEFAULT ''
            )
        """)

        for column, definition in {
            "role": "TEXT",
            "name": "TEXT",
            "failed_attempts": "INTEGER DEFAULT 0",
            "blocked_until": "TEXT",
        }.items():
            add_column_if_missing(
                conn, "users", column, definition
            )

        for column, definition in {
            "order_number": "TEXT",
            "phone": "TEXT",
            "full_name": "TEXT",
            "city": "TEXT",
            "branch": "TEXT",
            "price": "TEXT",
            "note": "TEXT",
            "status": "TEXT DEFAULT 'Нове'",
            "ttn": "TEXT DEFAULT ''",
            "author": "TEXT",
            "created_at": "TEXT",
            "sent_at": "TEXT DEFAULT ''",
        }.items():
            add_column_if_missing(
                conn, "orders", column, definition
            )

        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                event TEXT NOT NULL,
                details TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_permissions (
                user_id INTEGER NOT NULL,
                permission TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, permission)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS blocked_users (
                user_id INTEGER PRIMARY KEY,
                blocked_by INTEGER NOT NULL,
                reason TEXT DEFAULT '',
                blocked_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_orders_number
            ON orders(order_number)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_orders_status
            ON orders(status)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_orders_created
            ON orders(created_at)
        """)

    log_event(
        "database_initialized",
        details={"database": DB_PATH},
    )


# ============================================================
# COMMON STATE HELPERS
# ============================================================

def set_state(user_id, step, data=None, history=None):
    with state_lock:
        user_states[user_id] = {
            "step": step,
            "data": data or {},
            "history": history or [],
        }


def get_state(user_id):
    with state_lock:
        state = user_states.get(user_id)
        return dict(state) if state else None


def clear_state(user_id):
    with state_lock:
        user_states.pop(user_id, None)


def escape_text(value):
    return html.escape(str(value or ""))


# Ініціалізацію init_db() та bot.infinity_polling()
# додамо у фінальній частині повного файлу.

# ============================================================
# PART 2/8: AUTHENTICATION, ROLES AND ACCESS CONTROL
# ============================================================

MAX_LOGIN_ATTEMPTS = 3
LOGIN_LOCK_MINUTES = 60


def init_auth_db():
    """Додаткові таблиці авторизації."""
    with db_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS login_attempts (
                user_id INTEGER PRIMARY KEY,
                attempts INTEGER NOT NULL DEFAULT 0,
                blocked_until TEXT DEFAULT NULL
            )
        """)


def get_user(user_id):
    """Повертає користувача або None. Немає автоматичної ролі owner."""
    with db_connection() as conn:
        return conn.execute(
            """
            SELECT user_id, role, name, failed_attempts, blocked_until
            FROM users
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()


def get_user_role(user_id):
    user = get_user(user_id)
    if not user or user["role"] not in ROLES:
        return None
    return user["role"]


def is_user_blocked(user_id):
    """Перевіряє постійне блокування власником."""
    with db_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM blocked_users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return row is not None


def get_login_block(user_id):
    with db_connection() as conn:
        row = conn.execute(
            """
            SELECT attempts, blocked_until
            FROM login_attempts
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()

    if not row:
        return 0, None

    return row["attempts"], row["blocked_until"]


def set_login_attempts(user_id, attempts, blocked_until=None):
    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO login_attempts (user_id, attempts, blocked_until)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                attempts = excluded.attempts,
                blocked_until = excluded.blocked_until
            """,
            (user_id, attempts, blocked_until),
        )


def reset_login_attempts(user_id):
    with db_connection() as conn:
        conn.execute(
            "DELETE FROM login_attempts WHERE user_id = ?",
            (user_id,),
        )


def user_has_permission(user_id, permission):
    """
    Перевіряє індивідуальне право.
    Базові права для ролей визначимо окремо в модулі дозволів.
    """
    if is_user_blocked(user_id):
        return False

    role = get_user_role(user_id)
    if not role:
        return False

    with db_connection() as conn:
        row = conn.execute(
            """
            SELECT enabled
            FROM user_permissions
            WHERE user_id = ? AND permission = ?
            """,
            (user_id, permission),
        ).fetchone()

    if row is not None:
        return bool(row["enabled"])

    # За замовчуванням доступність конкретних функцій
    # визначатиметься спільною матрицею прав у наступній частині.
    return False


def require_user(message):
    """Повертає авторизованого користувача або None."""
    user_id = message.from_user.id

    if is_user_blocked(user_id):
        bot.send_message(
            message.chat.id,
            "⛔ Ваш доступ до CRM заблоковано. Зверніться до керівника.",
        )
        return None

    user = get_user(user_id)
    if not user or user["role"] not in ROLES:
        bot.send_message(
            message.chat.id,
            "🔐 Спочатку авторизуйтеся через /start.",
        )
        return None

    return user


def require_role(message, allowed_roles):
    """Перевірка ролі на сервері, а не лише приховування кнопок."""
    user = require_user(message)
    if user is None:
        return None

    if user["role"] not in set(allowed_roles):
        bot.send_message(
            message.chat.id,
            "⛔ У вас недостатньо прав для цієї дії.",
        )
        return None

    return user


# ============================================================
# LOGIN FLOW
# ============================================================

@bot.message_handler(commands=["start"])
def cmd_start(message):
    user_id = message.from_user.id

    if is_user_blocked(user_id):
        bot.send_message(
            message.chat.id,
            "⛔ Ваш доступ до CRM заблоковано. Зверніться до керівника.",
        )
        return

    attempts, blocked_until = get_login_block(user_id)

    if blocked_until:
        try:
            until = datetime.strptime(
                blocked_until, "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=TIMEZONE)

            if now_local() < until:
                remaining = max(
                    1, int((until - now_local()).total_seconds() // 60)
                )
                bot.send_message(
                    message.chat.id,
                    f"⏳ Забагато невдалих спроб. "
                    f"Спробуйте приблизно через {remaining} хв.",
                )
                return

            reset_login_attempts(user_id)
        except ValueError:
            reset_login_attempts(user_id)

    user = get_user(user_id)
    if user and user["role"] in ROLES:
        bot.send_message(
            message.chat.id,
            f"Ви авторизовані як: "
            f"<b>{ROLE_LABELS.get(user['role'], user['role'])}</b>",
            reply_markup=get_main_menu(user["role"]),
        )
        return

    set_state(user_id, "waiting_password")
    bot.send_message(
        message.chat.id,
        "Привіт! Будь ласка, введіть пароль доступу до системи:",
        reply_markup=types.ReplyKeyboardRemove(),
    )


@bot.message_handler(
    func=lambda message: (
        bool(message.text)
        and get_state(message.from_user.id) is not None
        and get_state(message.from_user.id).get("step") == "waiting_password"
    )
)
def process_password(message):
    user_id = message.from_user.id
    password = message.text.strip()

    if is_user_blocked(user_id):
        clear_state(user_id)
        bot.send_message(
            message.chat.id,
            "⛔ Ваш доступ до CRM заблоковано.",
        )
        return

    attempts, blocked_until = get_login_block(user_id)

    if blocked_until:
        try:
            until = datetime.strptime(
                blocked_until, "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=TIMEZONE)
            if now_local() < until:
                clear_state(user_id)
                bot.send_message(
                    message.chat.id,
                    "⏳ Вхід тимчасово заблоковано. Спробуйте пізніше.",
                )
                return
            reset_login_attempts(user_id)
            attempts = 0
        except ValueError:
            reset_login_attempts(user_id)
            attempts = 0

    role = PASSWORDS_MAP.get(password)

    if role not in ROLES:
        attempts += 1

        if attempts >= MAX_LOGIN_ATTEMPTS:
            until = now_local() + timedelta(minutes=LOGIN_LOCK_MINUTES)
            set_login_attempts(
                user_id,
                attempts,
                until.strftime("%Y-%m-%d %H:%M:%S"),
            )
            clear_state(user_id)
            log_event("login_temporarily_blocked", user_id)
            bot.send_message(
                message.chat.id,
                "⛔ Три неправильні спроби. "
                "Вхід заблоковано на 60 хвилин.",
            )
            return

        set_login_attempts(user_id, attempts)
        bot.send_message(
            message.chat.id,
            f"❌ Неправильний пароль. "
            f"Залишилось спроб: {MAX_LOGIN_ATTEMPTS - attempts}.",
        )
        return

    username = message.from_user.username
    display_name = f"@{username}" if username else f"id_{user_id}"

    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO users
                (user_id, role, name, failed_attempts, blocked_until)
            VALUES (?, ?, ?, 0, NULL)
            ON CONFLICT(user_id) DO UPDATE SET
                role = excluded.role,
                name = excluded.name,
                failed_attempts = 0,
                blocked_until = NULL
            """,
            (user_id, role, display_name),
        )

    reset_login_attempts(user_id)
    clear_state(user_id)

    log_event("login_success", user_id, {"role": role})

    bot.send_message(
        message.chat.id,
        f"✅ Успішний вхід! Роль: "
        f"<b>{ROLE_LABELS[role]}</b>",
        reply_markup=get_main_menu(role),
    )

# ============================================================
# PART 3/8: PERMISSIONS, MENUS AND NAVIGATION
# ============================================================

PERMISSIONS = {
    "create_order": "Створювати замовлення",
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
    "use_psychologist": "Користуватися психологічним режимом",
}

ROLE_DEFAULT_PERMISSIONS = {
    "owner": set(PERMISSIONS),
    "manager": {
        "create_order", "view_unsent_orders", "view_all_orders",
        "search_orders", "delete_own_order", "delete_any_order",
        "view_shifts", "use_ai", "use_psychologist",
    },
    "staff": {
        "create_order", "view_unsent_orders", "view_all_orders",
        "search_orders", "delete_own_order", "view_shifts",
        "use_ai", "use_psychologist",
    },
    "collector": {
        "create_order", "view_unsent_orders", "view_all_orders",
        "search_orders", "delete_own_order", "view_shifts",
        "use_ai", "use_psychologist",
    },
    "shipper": {
        "create_order", "view_all_orders", "search_orders",
        "delete_own_order", "view_shifts", "use_ai",
    },
}


def has_permission(user_id, permission):
    """Перевіряє блокування, роль і персональний дозвіл."""
    if permission not in PERMISSIONS:
        return False

    if is_user_blocked(user_id):
        return False

    role = get_user_role(user_id)
    if not role:
        return False

    with db_connection() as conn:
        row = conn.execute(
            """
            SELECT enabled FROM user_permissions
            WHERE user_id = ? AND permission = ?
            """,
            (user_id, permission),
        ).fetchone()

    if row is not None:
        return bool(row["enabled"])

    return permission in ROLE_DEFAULT_PERMISSIONS.get(role, set())


def set_permission(user_id, permission, enabled):
    """Зберігає персональний дозвіл у базі."""
    if permission not in PERMISSIONS:
        raise ValueError("Невідомий дозвіл")

    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO user_permissions
                (user_id, permission, enabled, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, permission) DO UPDATE SET
                enabled = excluded.enabled,
                updated_at = excluded.updated_at
            """,
            (user_id, permission, int(enabled), now_text()),
        )

    log_event(
        "permission_changed",
        user_id,
        {"permission": permission, "enabled": bool(enabled)},
    )


def get_main_menu(role):
    """Єдине головне меню для всіх ролей."""
    markup = types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        row_width=2,
    )

    if role not in ROLES:
        return types.ReplyKeyboardRemove()

    if has_menu_permission(role, "create_order"):
        markup.add(types.KeyboardButton("📝 Створити замовлення"))

    if has_menu_permission(role, "view_unsent_orders"):
        markup.add(
            types.KeyboardButton("📦 Переглянути замовлення (не надіслані)")
        )

    if has_menu_permission(role, "view_all_orders"):
        markup.add(
            types.KeyboardButton("📋 Переглянути всі замовлення (50)")
        )

    if has_menu_permission(role, "search_orders"):
        markup.add(types.KeyboardButton("🔍 CRM пошук"))

    if has_menu_permission(role, "statistics"):
        markup.add(types.KeyboardButton("📊 Статистика"))

    if has_menu_permission(role, "manage_users"):
        markup.add(types.KeyboardButton("👥 Керування користувачами"))

    if has_menu_permission(role, "manage_permissions"):
        markup.add(types.KeyboardButton("⚙️ Розподіл прав"))

    if has_menu_permission(role, "manage_shifts"):
        markup.add(types.KeyboardButton("📅 Керування змінами"))

    if has_menu_permission(role, "view_shifts"):
        markup.add(types.KeyboardButton("🕒 Мої зміни"))

    if has_menu_permission(role, "broadcast"):
        markup.add(types.KeyboardButton("📣 Розсилка"))

    markup.add(types.KeyboardButton("📨 Звернення"))
    markup.add(types.KeyboardButton("🚨 SOS OFFICE"))

    if has_menu_permission(role, "use_ai"):
        markup.add(types.KeyboardButton("🤖 AI-помічник"))

    if has_menu_permission(role, "use_psychologist"):
        markup.add(types.KeyboardButton("❤️ Психологічна підтримка"))

    markup.add(types.KeyboardButton("🏠 Головне меню"))
    markup.add(types.KeyboardButton("🚪 Вийти з акаунта"))
    return markup


def has_menu_permission(role, permission):
    """Перевірка базових прав ролі для побудови меню."""
    if role not in ROLES:
        return False
    return permission in ROLE_DEFAULT_PERMISSIONS.get(role, set())


def send_main_menu(chat_id, user_id, message="Головне меню:"):
    """Повертає користувача до головного меню та скидає поточний сценарій."""
    role = get_user_role(user_id)
    if not role or is_user_blocked(user_id):
        clear_state(user_id)
        bot.send_message(
            chat_id,
            "🔐 Потрібно авторизуватися через /start.",
            reply_markup=types.ReplyKeyboardRemove(),
        )
        return

    clear_state(user_id)
    bot.send_message(
        chat_id,
        message,
        reply_markup=get_main_menu(role),
    )


def go_back(chat_id, user_id):
    """Повернення до попереднього кроку сценарію."""
    state = get_state(user_id)
    if not state:
        send_main_menu(chat_id, user_id)
        return

    history = state.get("history", [])
    if not history:
        send_main_menu(chat_id, user_id)
        return

    previous = history.pop()
    with state_lock:
        user_states[user_id] = previous

    bot.send_message(
        chat_id,
        previous.get("prompt", "Повернулися на попередній крок."),
    )


@bot.message_handler(
    func=lambda message: message.text == "🏠 Головне меню"
)
def main_menu_button(message):
    send_main_menu(
        message.chat.id,
        message.from_user.id,
    )


@bot.message_handler(
    func=lambda message: message.text == "🚪 Вийти з акаунта"
)
def logout(message):
    user_id = message.from_user.id

    with db_connection() as conn:
        conn.execute(
            "DELETE FROM users WHERE user_id = ?",
            (user_id,),
        )

    clear_state(user_id)
    log_event("logout", user_id)

    bot.send_message(
        message.chat.id,
        "Ви вийшли з акаунта.",
        reply_markup=types.ReplyKeyboardRemove(),
    )


@bot.message_handler(
    func=lambda message: message.text == "⬅️ Назад"
)
def back_button(message):
    go_back(message.chat.id, message.from_user.id)


@bot.message_handler(
    func=lambda message: message.text == "❌ Скасувати"
)
def cancel_button(message):
    send_main_menu(
        message.chat.id,
        message.from_user.id,
        "❌ Дію скасовано. Головне меню:",
    )

# ============================================================
# PART 4/8: ORDERS, SEARCH, STATUS, TTN AND DELETION
# ============================================================

ORDER_FLOW_STEPS = {
    "order_phone",
    "order_name",
    "order_city",
    "order_branch",
    "order_price",
    "order_id",
    "order_note",
}


def init_orders_db():
    """Додає поля для авторства й часу відправлення."""
    with db_connection() as conn:
        add_column_if_missing(conn, "orders", "author_id", "INTEGER")


def get_order(order_id):
    with db_connection() as conn:
        return conn.execute(
            "SELECT * FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()


def format_order(order):
    """Єдиний формат картки замовлення."""
    return (
        f"📦 <b>Замовлення №{escape_text(order['order_number'])}</b> "
        f"(ID: {order['id']})\n"
        f"👤 Клієнт: {escape_text(order['full_name'])}\n"
        f"📞 Телефон: {escape_text(order['phone'])}\n"
        f"🏙 Місто: {escape_text(order['city'])}, "
        f"відділення: {escape_text(order['branch'])}\n"
        f"💰 Ціна: {escape_text(order['price'])}\n"
        f"📝 Примітка: {escape_text(order['note'])}\n"
        f"📌 Статус: <b>{escape_text(order['status'])}</b>\n"
        f"🚚 ТТН: {escape_text(order['ttn']) or 'Немає'}\n"
        f"🕒 Створено: {escape_text(order['created_at'])}"
    )


def get_order_inline_markup(order):
    markup = types.InlineKeyboardMarkup(row_width=2)
    order_id = order["id"]

    if has_permission(
        order.get("requester_id", 0), "view_all_orders"
    ) if isinstance(order, dict) else True:
        pass

    markup.add(
        types.InlineKeyboardButton(
            "📌 В роботі",
            callback_data=f"ord_status:{order_id}:working",
        ),
        types.InlineKeyboardButton(
            "🚚 Надіслано",
            callback_data=f"ord_status:{order_id}:sent",
        ),
    )
    markup.add(
        types.InlineKeyboardButton(
            "❌ Скасовано",
            callback_data=f"ord_status:{order_id}:cancelled",
        ),
        types.InlineKeyboardButton(
            "✏️ Додати ТТН",
            callback_data=f"ord_ttn:{order_id}",
        ),
    )
    markup.add(
        types.InlineKeyboardButton(
            "🗑 Видалити",
            callback_data=f"ord_delete:{order_id}",
        ),
    )
    return markup


def send_order_card(chat_id, order, requester_id):
    """Надсилає замовлення з кнопками."""
    if not order:
        return

    data = dict(order)
    data["requester_id"] = requester_id

    bot.send_message(
        chat_id,
        format_order(order),
        reply_markup=get_order_inline_markup(data),
    )


def order_status_text(status_key):
    statuses = {
        "working": "В роботі",
        "sent": "Надіслано",
        "cancelled": "Скасовано",
        "new": "Нове",
    }
    return statuses.get(status_key)


# ------------------------------------------------------------
# CREATE ORDER
# ------------------------------------------------------------

@bot.message_handler(
    func=lambda m: m.text == "📝 Створити замовлення"
)
def start_order(message):
    user = require_user(message)
    if not user:
        return

    if not has_permission(user["user_id"], "create_order"):
        bot.send_message(message.chat.id, "⛔ Немає права створювати замовлення.")
        return

    set_state(
        message.from_user.id,
        "order_phone",
        data={},
    )

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("⬅️ Назад"))
    markup.add(types.KeyboardButton("🏠 Головне меню"))
    markup.add(types.KeyboardButton("❌ Скасувати"))

    bot.send_message(
        message.chat.id,
        "Введіть номер телефону клієнта:",
        reply_markup=markup,
    )


@bot.message_handler(
    func=lambda m: (
        bool(m.text)
        and (get_state(m.from_user.id) or {}).get("step")
        in ORDER_FLOW_STEPS
    )
)
def handle_order_flow(message):
    user = require_user(message)
    if not user:
        clear_state(message.from_user.id)
        return

    user_id = message.from_user.id
    state = get_state(user_id)
    if not state:
        return

    step = state["step"]
    data = dict(state.get("data", {}))
    value = message.text.strip()

    if not value:
        bot.send_message(message.chat.id, "Поле не може бути порожнім.")
        return

    steps = {
        "order_phone": ("phone", "order_name", "Введіть ПІБ клієнта:"),
        "order_name": ("full_name", "order_city", "Введіть місто:"),
        "order_city": ("city", "order_branch", "Введіть номер відділення:"),
        "order_branch": ("branch", "order_price", "Введіть ціну замовлення:"),
        "order_price": ("price", "order_id", "Введіть номер замовлення:"),
        "order_id": ("order_number", "order_note", "Введіть примітку або «-», якщо її немає:"),
    }

    if step in steps:
        field, next_step, prompt = steps[step]
        data[field] = value
        set_state(user_id, next_step, data=data)
        bot.send_message(message.chat.id, prompt)
        return

    if step == "order_note":
        data["note"] = "" if value == "-" else value

        with db_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO orders (
                    order_number, phone, full_name, city, branch,
                    price, note, status, ttn, author, created_at,
                    sent_at, author_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'Нове', '', ?, ?, '', ?)
                """,
                (
                    data["order_number"],
                    data["phone"],
                    data["full_name"],
                    data["city"],
                    data["branch"],
                    data["price"],
                    data["note"],
                    user["role"].upper(),
                    now_text(),
                    user_id,
                ),
            )
            order_id = cursor.lastrowid

        clear_state(user_id)
        log_event("order_created", user_id, {"order_id": order_id})

        bot.send_message(
            message.chat.id,
            f"✅ Замовлення №{escape_text(data['order_number'])} "
            f"створено та збережено.",
            reply_markup=get_main_menu(user["role"]),
        )


# ------------------------------------------------------------
# VIEW ORDERS
# ------------------------------------------------------------

def list_orders(message, unsent_only=False):
    user = require_user(message)
    if not user:
        return

    permission = (
        "view_unsent_orders" if unsent_only else "view_all_orders"
    )
    if not has_permission(user["user_id"], permission):
        bot.send_message(message.chat.id, "⛔ Немає права переглядати ці замовлення.")
        return

    with db_connection() as conn:
        if unsent_only:
            rows = conn.execute(
                """
                SELECT * FROM orders
                WHERE status != 'Надіслано'
                ORDER BY id DESC LIMIT 50
                """
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM orders ORDER BY id DESC LIMIT 50"
            ).fetchall()

    if not rows:
        bot.send_message(
            message.chat.id,
            "Немає замовлень для відображення.",
            reply_markup=get_main_menu(user["role"]),
        )
        return

    for order in rows:
        send_order_card(message.chat.id, order, user["user_id"])


@bot.message_handler(
    func=lambda m: m.text == "📦 Переглянути замовлення (не надіслані)"
)
def view_unsent_orders(message):
    list_orders(message, unsent_only=True)


@bot.message_handler(
    func=lambda m: m.text == "📋 Переглянути всі замовлення (50)"
)
def view_last_50(message):
    list_orders(message, unsent_only=False)


# ------------------------------------------------------------
# CRM SEARCH
# ------------------------------------------------------------

@bot.message_handler(func=lambda m: m.text == "🔍 CRM пошук")
def crm_search_menu(message):
    user = require_user(message)
    if not user:
        return

    if not has_permission(user["user_id"], "search_orders"):
        bot.send_message(message.chat.id, "⛔ Немає права шукати замовлення.")
        return

    set_state(message.from_user.id, "waiting_search", data={})

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("⬅️ Назад"))
    markup.add(types.KeyboardButton("🏠 Головне меню"))
    markup.add(types.KeyboardButton("❌ Скасувати"))

    bot.send_message(
        message.chat.id,
        "Введіть ім'я, телефон або номер замовлення:",
        reply_markup=markup,
    )


@bot.message_handler(
    func=lambda m: (
        bool(m.text)
        and (get_state(m.from_user.id) or {}).get("step") == "waiting_search"
    )
)
def perform_search(message):
    user = require_user(message)
    if not user:
        clear_state(message.from_user.id)
        return

    query = f"%{message.text.strip()}%"

    with db_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM orders
            WHERE order_number LIKE ?
               OR full_name LIKE ?
               OR phone LIKE ?
            ORDER BY id DESC LIMIT 50
            """,
            (query, query, query),
        ).fetchall()

    clear_state(message.from_user.id)

    if not rows:
        bot.send_message(
            message.chat.id,
            "Нічого не знайдено.",
            reply_markup=get_main_menu(user["role"]),
        )
        return

    for order in rows:
        send_order_card(message.chat.id, order, user["user_id"])


# ------------------------------------------------------------
# ORDER CALLBACKS
# ------------------------------------------------------------

@bot.callback_query_handler(
    func=lambda call: call.data.startswith("ord_")
)
def order_callback_handler(call):
    user_id = call.from_user.id
    role = get_user_role(user_id)

    if not role or is_user_blocked(user_id):
        bot.answer_callback_query(
            call.id,
            "Немає доступу. Авторизуйтеся знову.",
            show_alert=True,
        )
        return

    parts = call.data.split(":")
    action = parts[0]

    try:
        order_id = int(parts[1])
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id, "Некоректний номер замовлення.")
        return

    order = get_order(order_id)
    if not order:
        bot.answer_callback_query(
            call.id, "Замовлення не знайдено.", show_alert=True
        )
        return

    if action == "ord_status":
        if len(parts) != 3:
            bot.answer_callback_query(call.id, "Некоректний статус.")
            return

        if not has_permission(user_id, "view_all_orders"):
            bot.answer_callback_query(
                call.id, "Недостатньо прав.", show_alert=True
            )
            return

        new_status = order_status_text(parts[2])
        if not new_status:
            bot.answer_callback_query(call.id, "Невідомий статус.")
            return

        with db_connection() as conn:
            if new_status == "Надіслано":
                conn.execute(
                    """
                    UPDATE orders
                    SET status = ?, sent_at = ?
                    WHERE id = ?
                    """,
                    (new_status, now_text(), order_id),
                )
            else:
                conn.execute(
                    "UPDATE orders SET status = ? WHERE id = ?",
                    (new_status, order_id),
                )

        log_event(
            "order_status_changed",
            user_id,
            {"order_id": order_id, "status": new_status},
        )

        updated = get_order(order_id)
        try:
            bot.edit_message_text(
                format_order(updated),
                call.message.chat.id,
                call.message.message_id,
                reply_markup=get_order_inline_markup(dict(updated)),
            )
        except Exception:
            bot.send_message(
                call.message.chat.id,
                format_order(updated),
                reply_markup=get_order_inline_markup(dict(updated)),
            )

        bot.answer_callback_query(call.id, f"Статус: {new_status}")
        return

    if action == "ord_ttn":
        if not has_permission(user_id, "view_all_orders"):
            bot.answer_callback_query(
                call.id, "Недостатньо прав.", show_alert=True
            )
            return

        set_state(
            user_id,
            "waiting_ttn",
            data={"order_id": order_id},
        )
        bot.send_message(
            call.message.chat.id,
            f"Введіть номер ТТН для замовлення №"
            f"{escape_text(order['order_number'])}:",
        )
        bot.answer_callback_query(call.id)
        return

    if action == "ord_delete":
        is_owner_or_manager = role in {"owner", "manager"}
        is_author = order["author_id"] == user_id

        if is_owner_or_manager:
            allowed = has_permission(user_id, "delete_any_order")
        else:
            allowed = (
                is_author
                and has_permission(user_id, "delete_own_order")
            )

        if not allowed:
            bot.answer_callback_query(
                call.id,
                "⛔ Немає права видаляти це замовлення.",
                show_alert=True,
            )
            return

        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton(
                "✅ Так, видалити",
                callback_data=f"ord_confirm_delete:{order_id}",
            ),
            types.InlineKeyboardButton(
                "↩️ Ні",
                callback_data=f"ord_cancel_delete:{order_id}",
            ),
        )
        bot.send_message(
            call.message.chat.id,
            f"Видалити замовлення №"
            f"{escape_text(order['order_number'])}?",
            reply_markup=markup,
        )
        bot.answer_callback_query(call.id)
        return

    if action == "ord_confirm_delete":
        # Обробляється нижче окремою гілкою після повторної перевірки прав.
        is_owner_or_manager = role in {"owner", "manager"}
        is_author = order["author_id"] == user_id
        allowed = (
            has_permission(user_id, "delete_any_order")
            if is_owner_or_manager
            else is_author and has_permission(user_id, "delete_own_order")
        )

        if not allowed:
            bot.answer_callback_query(
                call.id, "Недостатньо прав.", show_alert=True
            )
            return

        with db_connection() as conn:
            conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))

        log_event("order_deleted", user_id, {"order_id": order_id})
        bot.answer_callback_query(call.id, "Замовлення видалено.")
        bot.send_message(call.message.chat.id, "🗑 Замовлення видалено.")
        return

    if action == "ord_cancel_delete":
        bot.answer_callback_query(call.id, "Видалення скасовано.")
        return


@bot.message_handler(
    func=lambda m: (
        bool(m.text)
        and (get_state(m.from_user.id) or {}).get("step") == "waiting_ttn"
    )
)
def save_ttn_input(message):
    user = require_user(message)
    if not user:
        clear_state(message.from_user.id)
        return

    if not has_permission(user["user_id"], "view_all_orders"):
        clear_state(message.from_user.id)
        bot.send_message(message.chat.id, "⛔ Недостатньо прав.")
        return

    state = get_state(message.from_user.id)
    order_id = state.get("data", {}).get("order_id")
    ttn = message.text.strip()

    if not order_id or not ttn:
        bot.send_message(message.chat.id, "Введіть коректний номер ТТН.")
        return

    with db_connection() as conn:
        conn.execute(
            "UPDATE orders SET ttn = ? WHERE id = ?",
            (ttn, order_id),
        )

    clear_state(message.from_user.id)
    log_event("order_ttn_updated", message.from_user.id, {"order_id": order_id})

    bot.send_message(
        message.chat.id,
        "✅ ТТН збережено.",
        reply_markup=get_main_menu(user["role"]),
    )

# ============================================================
# PART 5/8: STATISTICS AND SHIFTS
# ============================================================

def init_shifts_db():
    with db_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shifts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                created_by INTEGER NOT NULL,
                shift_date TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                actual_start TEXT DEFAULT '',
                actual_end TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'planned',
                created_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_shifts_date
            ON shifts(shift_date)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_shifts_user
            ON shifts(user_id, shift_date)
        """)


def get_day_statistics(day):
    with db_connection() as conn:
        orders = conn.execute("""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'Надіслано' THEN 1 ELSE 0 END) AS sent,
                SUM(CASE WHEN status != 'Надіслано' THEN 1 ELSE 0 END) AS unsent
            FROM orders
            WHERE substr(created_at, 1, 10) = ?
        """, (day,)).fetchone()

        amount = conn.execute("""
            SELECT price FROM orders
            WHERE substr(created_at, 1, 10) = ?
        """, (day,)).fetchall()

        shifts = conn.execute("""
            SELECT s.*, u.name
            FROM shifts s
            LEFT JOIN users u ON u.user_id = s.user_id
            WHERE s.shift_date = ?
            ORDER BY s.start_time
        """, (day,)).fetchall()

    total_amount = 0.0
    for row in amount:
        try:
            total_amount += float(
                str(row["price"]).replace(" ", "").replace(",", ".")
            )
        except (ValueError, TypeError):
            pass

    return orders, total_amount, shifts


@bot.message_handler(func=lambda m: m.text == "📊 Статистика")
def show_statistics(message):
    user = require_role(message, {"owner"})
    if not user:
        return

    set_state(message.from_user.id, "statistics_date", data={})

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("📅 Сьогодні"))
    markup.add(types.KeyboardButton("📅 Вчора"))
    markup.add(types.KeyboardButton("❌ Скасувати"))

    bot.send_message(
        message.chat.id,
        "Оберіть період або введіть дату у форматі YYYY-MM-DD:",
        reply_markup=markup,
    )


@bot.message_handler(
    func=lambda m: (
        bool(m.text)
        and (get_state(m.from_user.id) or {}).get("step") == "statistics_date"
    )
)
def process_statistics_date(message):
    user = require_role(message, {"owner"})
    if not user:
        clear_state(message.from_user.id)
        return

    value = message.text.strip()

    if value == "📅 Сьогодні":
        day = now_local().strftime("%Y-%m-%d")
    elif value == "📅 Вчора":
        day = (now_local() - timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        try:
            datetime.strptime(value, "%Y-%m-%d")
            day = value
        except ValueError:
            bot.send_message(
                message.chat.id,
                "Невірний формат. Введіть дату як YYYY-MM-DD.",
            )
            return

    orders, total_amount, shifts = get_day_statistics(day)
    clear_state(message.from_user.id)

    text = (
        f"📊 <b>Статистика за {day}</b>\n\n"
        f"📦 Всього замовлень: {orders['total'] or 0}\n"
        f"🚚 Надіслано: {orders['sent'] or 0}\n"
        f"⏳ Не надіслано: {orders['unsent'] or 0}\n"
        f"💰 Сума замовлень: {total_amount:.2f}\n\n"
        f"🕒 <b>Зміни працівників:</b>\n"
    )

    if not shifts:
        text += "За цей день змін немає.\n"
    else:
        for shift in shifts:
            start = shift["actual_start"] or "не почато"
            end = shift["actual_end"] or "не завершено"
            text += (
                f"\n👤 {escape_text(shift['name'] or shift['user_id'])}\n"
                f"План: {shift['start_time']}–{shift['end_time']}\n"
                f"Факт: {start}–{end}\n"
                f"Статус: {escape_text(shift['status'])}\n"
            )

    bot.send_message(
        message.chat.id,
        text,
        reply_markup=get_main_menu(user["role"]),
    )


@bot.message_handler(func=lambda m: m.text == "📅 Керування змінами")
def manage_shifts_menu(message):
    user = require_role(message, {"owner"})
    if not user:
        return

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("➕ Створити зміну"))
    markup.add(types.KeyboardButton("📋 Переглянути зміни"))
    markup.add(types.KeyboardButton("🏠 Головне меню"))

    bot.send_message(
        message.chat.id,
        "📅 Керування змінами:",
        reply_markup=markup,
    )


@bot.message_handler(func=lambda m: m.text == "➕ Створити зміну")
def create_shift_start(message):
    user = require_role(message, {"owner"})
    if not user:
        return

    with db_connection() as conn:
        staff = conn.execute("""
            SELECT user_id, role, name FROM users
            WHERE role IN ('manager', 'staff', 'collector', 'shipper')
            ORDER BY name
        """).fetchall()

    if not staff:
        bot.send_message(message.chat.id, "Немає авторизованих працівників.")
        return

    markup = types.InlineKeyboardMarkup()
    for person in staff:
        label = f"{person['name'] or person['user_id']} | ID {person['user_id']}"
        markup.add(types.InlineKeyboardButton(
            label[:60],
            callback_data=f"shift_user:{person['user_id']}",
        ))

    set_state(message.from_user.id, "shift_select_user", data={})
    bot.send_message(
        message.chat.id,
        "Оберіть працівника для зміни:",
        reply_markup=markup,
    )


@bot.message_handler(func=lambda m: m.text == "📋 Переглянути зміни")
def view_shifts_owner(message):
    user = require_role(message, {"owner"})
    if not user:
        return

    with db_connection() as conn:
        rows = conn.execute("""
            SELECT s.*, u.name
            FROM shifts s
            LEFT JOIN users u ON u.user_id = s.user_id
            ORDER BY s.shift_date DESC, s.start_time DESC
            LIMIT 50
        """).fetchall()

    if not rows:
        bot.send_message(message.chat.id, "Змін поки немає.")
        return

    for shift in rows:
        text = (
            f"📅 <b>{shift['shift_date']}</b>\n"
            f"👤 {escape_text(shift['name'] or shift['user_id'])}\n"
            f"План: {shift['start_time']}–{shift['end_time']}\n"
            f"Факт: {shift['actual_start'] or 'не почато'}–"
            f"{shift['actual_end'] or 'не завершено'}\n"
            f"Статус: {escape_text(shift['status'])}"
        )
        bot.send_message(message.chat.id, text)


@bot.message_handler(func=lambda m: m.text == "🕒 Мої зміни")
def my_shifts(message):
    user = require_user(message)
    if not user:
        return

    with db_connection() as conn:
        rows = conn.execute("""
            SELECT * FROM shifts
            WHERE user_id = ?
            ORDER BY shift_date DESC, start_time DESC
            LIMIT 30
        """, (message.from_user.id,)).fetchall()

    if not rows:
        bot.send_message(message.chat.id, "У вас поки немає призначених змін.")
        return

    for shift in rows:
        markup = types.InlineKeyboardMarkup()
        if shift["status"] == "planned":
            markup.add(types.InlineKeyboardButton(
                "▶️ Почати зміну",
                callback_data=f"shift_start:{shift['id']}",
            ))
        elif shift["status"] == "started":
            markup.add(types.InlineKeyboardButton(
                "⏹ Завершити зміну",
                callback_data=f"shift_end:{shift['id']}",
            ))

        bot.send_message(
            message.chat.id,
            (
                f"📅 <b>{shift['shift_date']}</b>\n"
                f"План: {shift['start_time']}–{shift['end_time']}\n"
                f"Фактичний початок: {shift['actual_start'] or 'не почато'}\n"
                f"Фактичне завершення: {shift['actual_end'] or 'не завершено'}"
            ),
            reply_markup=markup,
        )

# ============================================================
# PART 6/8: SOS OFFICE, EMPLOYEE TICKETS AND BROADCASTS
# ============================================================

def init_communication_db():
    with db_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sos_incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                description TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                acknowledged_at TEXT DEFAULT '',
                acknowledged_by INTEGER,
                closed_at TEXT DEFAULT '',
                closed_by INTEGER,
                repeat_count INTEGER DEFAULT 0
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                assigned_to INTEGER,
                resolution TEXT DEFAULT ''
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS ticket_replies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                sender_id INTEGER NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(ticket_id) REFERENCES tickets(id)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS broadcast_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL,
                recipient_id INTEGER NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)


# ------------------------------------------------------------
# SOS OFFICE
# ------------------------------------------------------------

SOS_REASONS = [
    "Загроза / небезпека",
    "Конфлікт",
    "Крадіжка",
    "Медична ситуація",
    "Технічна аварія",
    "Інше",
]


@bot.message_handler(func=lambda m: m.text == "🚨 SOS OFFICE")
def sos_start(message):
    user = require_user(message)
    if not user:
        return

    markup = types.InlineKeyboardMarkup(row_width=1)
    for index, reason in enumerate(SOS_REASONS):
        markup.add(types.InlineKeyboardButton(
            reason,
            callback_data=f"sos_reason:{index}",
        ))

    bot.send_message(
        message.chat.id,
        "🚨 <b>SOS OFFICE</b>\nОберіть причину тривоги:",
        reply_markup=markup,
    )


def create_sos_incident(user_id, reason, description=""):
    with db_connection() as conn:
        cursor = conn.execute("""
            INSERT INTO sos_incidents
                (user_id, reason, description, status, created_at)
            VALUES (?, ?, ?, 'active', ?)
        """, (user_id, reason, description, now_text()))
        return cursor.lastrowid


def notify_owner_sos(incident_id):
    with db_connection() as conn:
        incident = conn.execute(
            "SELECT * FROM sos_incidents WHERE id = ?",
            (incident_id,),
        ).fetchone()
        owners = conn.execute(
            "SELECT user_id FROM users WHERE role = 'owner'"
        ).fetchall()

    if not incident:
        return

    text = (
        f"🚨 <b>SOS OFFICE №{incident_id}</b>\n"
        f"👤 Telegram ID: <code>{incident['user_id']}</code>\n"
        f"Причина: {escape_text(incident['reason'])}\n"
        f"Опис: {escape_text(incident['description']) or 'Не вказано'}\n"
        f"Час: {escape_text(incident['created_at'])}\n"
        f"Статус: {escape_text(incident['status'])}"
    )

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(
        "✅ Прийняв тривогу",
        callback_data=f"sos_ack:{incident_id}",
    ))
    markup.add(types.InlineKeyboardButton(
        "🔒 Закрити тривогу",
        callback_data=f"sos_close:{incident_id}",
    ))

    for owner in owners:
        try:
            bot.send_message(
                owner["user_id"],
                text,
                reply_markup=markup,
            )
        except Exception:
            log_exception("sos_owner_notification_failed", owner["user_id"])


# ------------------------------------------------------------
# EMPLOYEE TICKETS
# ------------------------------------------------------------

TICKET_CATEGORIES = [
    "Зарплата",
    "Графік",
    "Робоче питання",
    "Конфлікт",
    "Пропозиція",
    "Інше",
]


@bot.message_handler(func=lambda m: m.text == "📨 Звернення")
def ticket_start(message):
    user = require_user(message)
    if not user:
        return

    markup = types.InlineKeyboardMarkup(row_width=1)
    for index, category in enumerate(TICKET_CATEGORIES):
        markup.add(types.InlineKeyboardButton(
            category,
            callback_data=f"ticket_category:{index}",
        ))

    bot.send_message(
        message.chat.id,
        "📨 <bЗвернення до керівництва</b>\nОберіть категорію:",
        reply_markup=markup,
    )


def create_ticket(user_id, category, message_text):
    with db_connection() as conn:
        cursor = conn.execute("""
            INSERT INTO tickets
                (user_id, category, message, status, created_at, updated_at)
            VALUES (?, ?, ?, 'new', ?, ?)
        """, (user_id, category, message_text, now_text(), now_text()))
        return cursor.lastrowid


def notify_owners_ticket(ticket_id):
    with db_connection() as conn:
        ticket = conn.execute(
            "SELECT * FROM tickets WHERE id = ?",
            (ticket_id,),
        ).fetchone()
        owners = conn.execute(
            "SELECT user_id FROM users WHERE role = 'owner'"
        ).fetchall()

    if not ticket:
        return

    text = (
        f"📨 <bНове звернення №{ticket_id}</b>\n"
        f"👤 Telegram ID: <code>{ticket['user_id']}</code>\n"
        f"Категорія: {escape_text(ticket['category'])}\n"
        f"Текст: {escape_text(ticket['message'])}\n"
        f"Час: {escape_text(ticket['created_at'])}"
    )

    for owner in owners:
        try:
            bot.send_message(owner["user_id"], text)
        except Exception:
            log_exception("ticket_owner_notification_failed", owner["user_id"])


# ------------------------------------------------------------
# BROADCASTS
# ------------------------------------------------------------

@bot.message_handler(func=lambda m: m.text == "📣 Розсилка")
def broadcast_start(message):
    user = require_role(message, {"owner"})
    if not user:
        return

    set_state(message.from_user.id, "broadcast_message", data={})

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("🏠 Головне меню"))
    markup.add(types.KeyboardButton("❌ Скасувати"))

    bot.send_message(
        message.chat.id,
        "Введіть текст розсилки:",
        reply_markup=markup,
    )


def send_broadcast(sender_id, message_text, recipient_ids):
    success = 0
    failed = 0

    for recipient_id in recipient_ids:
        try:
            bot.send_message(recipient_id, message_text)
            status = "sent"
            success += 1
        except Exception:
            status = "failed"
            failed += 1
            log_exception("broadcast_delivery_failed", recipient_id)

        with db_connection() as conn:
            conn.execute("""
                INSERT INTO broadcast_log
                    (sender_id, recipient_id, message, status, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (
                sender_id,
                recipient_id,
                message_text,
                status,
                now_text(),
            ))

        time.sleep(0.05)

    return success, failed


# Конкретні callback-переходи для SOS, звернень і розсилки
# підключимо в централізованому обробнику в наступній частині.

# ============================================================
# PART 7/8: AI ASSISTANT, PSYCHOLOGICAL SUPPORT, USER MANAGEMENT
# ============================================================

# AI API key is configured in Render Environment Variables.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()


def init_ai_db():
    with db_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_preferences (
                user_id INTEGER PRIMARY KEY,
                psychologist_consent INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_issue_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)


def ai_is_available():
    return bool(OPENAI_API_KEY)


def call_ai(messages, model="gpt-6"):
    """
    Викликає OpenAI API.
    Повний текст психологічної розмови не записується в CRM.
    """
    if not OPENAI_API_KEY:
        return (
            "AI ще не налаштований. Додайте OPENAI_API_KEY "
            "у змінні середовища Render."
        )

    try:
        from openai import OpenAI

        client = OpenAI(api_key=OPENAI_API_KEY)
        response = client.responses.create(
            model=model,
            input=messages,
        )

        return response.output_text or "Не вдалося сформувати відповідь."
    except Exception:
        log_exception("ai_request_failed")
        return (
            "Не вдалося отримати відповідь від AI. "
            "Спробуйте ще раз трохи пізніше."
        )


def get_ai_consent(user_id):
    with db_connection() as conn:
        row = conn.execute(
            """
            SELECT psychologist_consent
            FROM ai_preferences
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()

    return bool(row and row["psychologist_consent"])


def set_ai_consent(user_id, accepted):
    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO ai_preferences
                (user_id, psychologist_consent, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                psychologist_consent = excluded.psychologist_consent,
                updated_at = excluded.updated_at
            """,
            (user_id, int(accepted), now_text()),
        )

    log_event(
        "psychologist_consent_changed",
        user_id,
        {"accepted": bool(accepted)},
    )


def record_issue_category(user_id, category):
    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO ai_issue_categories (user_id, category, created_at)
            VALUES (?, ?, ?)
            """,
            (user_id, category, now_text()),
        )


# ------------------------------------------------------------
# AI ASSISTANT
# ------------------------------------------------------------

@bot.message_handler(func=lambda m: m.text == "🤖 AI-помічник")
def ai_assistant_start(message):
    user = require_user(message)
    if not user:
        return

    if not has_permission(user["user_id"], "use_ai"):
        bot.send_message(message.chat.id, "⛔ Немає доступу до AI.")
        return

    set_state(message.from_user.id, "ai_chat", data={"history": []})

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("🏠 Головне меню"))
    markup.add(types.KeyboardButton("❌ Скасувати"))

    bot.send_message(
        message.chat.id,
        "🤖 <b>Cloud Hub AI</b>\n"
        "Питай про CRM, робочі процеси або правила компанії. "
        "Не надсилай паролі, банківські дані чи іншу секретну інформацію.",
        reply_markup=markup,
    )


@bot.message_handler(
    func=lambda m: (
        bool(m.text)
        and (get_state(m.from_user.id) or {}).get("step") == "ai_chat"
    )
)
def ai_assistant_message(message):
    user = require_user(message)
    if not user:
        clear_state(message.from_user.id)
        return

    state = get_state(message.from_user.id)
    history = list(state.get("data", {}).get("history", []))

    history.append({
        "role": "user",
        "content": message.text,
    })

    answer = call_ai([
        {
            "role": "system",
            "content": (
                "Ти Cloud Hub AI, помічник працівників компанії. "
                "Відповідай українською, доброзичливо та конкретно. "
                "Не вигадуй внутрішні правила, зарплати або дані CRM. "
                "Якщо не знаєш факту, прямо скажи про це."
            ),
        },
        *history,
    ])

    history.append({
        "role": "assistant",
        "content": answer,
    })

    # Історія залишається тільки в оперативній пам'яті.
    # У SQLite тексти діалогу не записуємо.
    set_state(
        message.from_user.id,
        "ai_chat",
        data={"history": history},
    )

    bot.send_message(message.chat.id, escape_text(answer))


# ------------------------------------------------------------
# PSYCHOLOGICAL SUPPORT
# ------------------------------------------------------------

@bot.message_handler(
    func=lambda m: m.text == "❤️ Психологічна підтримка"
)
def psychologist_start(message):
    user = require_user(message)
    if not user:
        return

    if not has_permission(user["user_id"], "use_psychologist"):
        bot.send_message(message.chat.id, "⛔ Немає доступу.")
        return

    if get_ai_consent(message.from_user.id):
        begin_psychologist_chat(message.chat.id, message.from_user.id)
        return

    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton(
            "❤️ Погоджуюся",
            callback_data="psych_consent:yes",
        ),
        types.InlineKeyboardButton(
            "Не погоджуюся",
            callback_data="psych_consent:no",
        ),
    )

    bot.send_message(
        message.chat.id,
        "Привіт ❤️ Я тут, щоб вислухати тебе й допомогти "
        "розібратися з тим, що турбує.\n\n"
        "Текст нашої розмови не зберігатиметься в CRM. "
        "У базі зберігатиметься лише категорія проблеми. "
        "Водночас повідомлення передаються до AI-сервісу для "
        "формування відповідей. Це не професійна психотерапія "
        "і не гарантована служба екстреного реагування.\n\n"
        "Якщо ти погоджуєшся, можемо починати.",
        reply_markup=markup,
    )


def begin_psychologist_chat(chat_id, user_id):
    set_state(user_id, "psych_chat", data={"history": []})

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("🏠 Головне меню"))
    markup.add(types.KeyboardButton("❌ Скасувати"))

    bot.send_message(
        chat_id,
        "Я слухаю ❤️ Що зараз найбільше турбує?",
        reply_markup=markup,
    )


@bot.message_handler(
    func=lambda m: (
        bool(m.text)
        and (get_state(m.from_user.id) or {}).get("step") == "psych_chat"
    )
)
def psychologist_message(message):
    user = require_user(message)
    if not user:
        clear_state(message.from_user.id)
        return

    if not get_ai_consent(message.from_user.id):
        clear_state(message.from_user.id)
        bot.send_message(
            message.chat.id,
            "Для цього режиму потрібна згода. Поверніться в меню.",
        )
        return

    state = get_state(message.from_user.id)
    history = list(state.get("data", {}).get("history", []))
    history.append({
        "role": "user",
        "content": message.text,
    })

    answer = call_ai([
        {
            "role": "system",
            "content": (
                "Ти підтримувальний співрозмовник, не лікар і не "
                "психотерапевт. Слухай без осуду, допомагай розкласти "
                "проблему на кроки. Не став діагнозів і не обіцяй "
                "абсолютної конфіденційності. Якщо людина повідомляє "
                "про безпосередню небезпеку або намір завдати шкоди "
                "собі чи іншим, спокійно заохоть звернутися до місцевих "
                "екстрених служб або довіреної людини. В Україні можна "
                "телефонувати 112 або 103."
            ),
        },
        *history,
    ])

    history.append({
        "role": "assistant",
        "content": answer,
    })

    # Не записуємо історію діалогу до бази.
    set_state(
        message.from_user.id,
        "psych_chat",
        data={"history": history},
    )

    bot.send_message(message.chat.id, escape_text(answer))


# ------------------------------------------------------------
# USER MANAGEMENT
# ------------------------------------------------------------

@bot.message_handler(
    func=lambda m: m.text == "👥 Керування користувачами"
)
def user_management_menu(message):
    user = require_role(message, {"owner"})
    if not user:
        return

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("🚫 Заблокувати користувача"))
    markup.add(types.KeyboardButton("🔓 Розблокувати користувача"))
    markup.add(types.KeyboardButton("📋 Список заблокованих"))
    markup.add(types.KeyboardButton("🏠 Головне меню"))

    bot.send_message(
        message.chat.id,
        "👥 Керування користувачами:",
        reply_markup=markup,
    )


@bot.message_handler(
    func=lambda m: m.text == "🚫 Заблокувати користувача"
)
def block_user_start(message):
    user = require_role(message, {"owner"})
    if not user:
        return

    with db_connection() as conn:
        users = conn.execute(
            """
            SELECT user_id, role, name FROM users
            WHERE user_id != ?
            ORDER BY name
            """,
            (message.from_user.id,),
        ).fetchall()

    markup = types.InlineKeyboardMarkup()
    for target in users:
        markup.add(types.InlineKeyboardButton(
            f"{target['name'] or target['user_id']} ({target['role']})",
            callback_data=f"user_block:{target['user_id']}",
        ))

    bot.send_message(
        message.chat.id,
        "Оберіть користувача для блокування:",
        reply_markup=markup,
    )


@bot.message_handler(
    func=lambda m: m.text == "🔓 Розблокувати користувача"
)
def unblock_user_start(message):
    user = require_role(message, {"owner"})
    if not user:
        return

    with db_connection() as conn:
        users = conn.execute(
            """
            SELECT b.user_id, u.name
            FROM blocked_users b
            LEFT JOIN users u ON u.user_id = b.user_id
            ORDER BY b.blocked_at DESC
            """
        ).fetchall()

    if not users:
        bot.send_message(message.chat.id, "Немає заблокованих користувачів.")
        return

    markup = types.InlineKeyboardMarkup()
    for target in users:
        markup.add(types.InlineKeyboardButton(
            f"{target['name'] or target['user_id']} ({target['user_id']})",
            callback_data=f"user_unblock:{target['user_id']}",
        ))

    bot.send_message(
        message.chat.id,
        "Оберіть користувача для розблокування:",
        reply_markup=markup,
    )


@bot.message_handler(
    func=lambda m: m.text == "📋 Список заблокованих"
)
def blocked_users_list(message):
    user = require_role(message, {"owner"})
    if not user:
        return

    with db_connection() as conn:
        rows = conn.execute(
            """
            SELECT user_id, blocked_by, reason, blocked_at
            FROM blocked_users ORDER BY blocked_at DESC
            """
        ).fetchall()

    if not rows:
        bot.send_message(message.chat.id, "Список заблокованих порожній.")
        return

    for row in rows:
        bot.send_message(
            message.chat.id,
            f"🚫 ID: <code>{row['user_id']}</code>\n"
            f"Ким заблоковано: {row['blocked_by']}\n"
            f"Причина: {escape_text(row['reason']) or 'Не вказана'}\n"
            f"Дата: {escape_text(row['blocked_at'])}",
        )


# Callback-підтвердження згоди психологічного режиму,
# SOS, звернень і керування користувачами підключимо
# в єдиному фінальному callback-обробнику.

# =========================
# PART 8: CALLBACK HANDLERS
# =========================

@bot.callback_query_handler(
    func=lambda call: call.data.startswith("psych_consent:")
)
def handle_psych_consent(call):
    user_id = call.from_user.id
    answer = call.data.split(":", 1)[1]

    if not get_user(user_id) or is_user_blocked(user_id):
        bot.answer_callback_query(
            call.id, "Немає доступу", show_alert=True
        )
        return

    if answer == "yes":
        with db_connection() as conn:
            conn.execute("""
                INSERT INTO ai_preferences
                    (user_id, psychologist_consent, updated_at)
                VALUES (?, 1, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    psychologist_consent=1,
                    updated_at=excluded.updated_at
            """, (user_id, now_text()))

        bot.answer_callback_query(call.id, "Згоду збережено")
        begin_psychologist_chat(user_id)
        bot.send_message(
            call.message.chat.id,
            "Можеш написати, що тебе турбує. Повідомлення передаються "
            "до OpenAI для формування відповіді. Це не замінює "
            "професійну психологічну чи медичну допомогу.",
            reply_markup=types.ReplyKeyboardMarkup(
                resize_keyboard=True
            ).add(
                types.KeyboardButton("🏠 Головне меню"),
                types.KeyboardButton("❌ Скасувати")
            )
        )
    else:
        clear_state(user_id)
        bot.answer_callback_query(call.id, "Режим не активовано")
        bot.send_message(
            call.message.chat.id,
            "Режим психологічної підтримки не активовано."
        )


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("sos_reason:")
)
def handle_sos_reason(call):
    user_id = call.from_user.id

    if not get_user(user_id) or is_user_blocked(user_id):
        bot.answer_callback_query(
            call.id, "Спочатку авторизуйся", show_alert=True
        )
        return

    try:
        index = int(call.data.split(":", 1)[1])
        reason = SOS_REASONS[index]
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Невірна категорія")
        return

    set_state(user_id, "sos_description", {"reason": reason})

    markup = types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        one_time_keyboard=True
    )
    markup.row(
        types.KeyboardButton("Без опису"),
        types.KeyboardButton("❌ Скасувати")
    )
    markup.row(types.KeyboardButton("🏠 Головне меню"))

    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        f"Причина: <b>{escape_text(reason)}</b>\n"
        "Опиши ситуацію або натисни «Без опису».",
        reply_markup=markup
    )


@bot.message_handler(
    func=lambda message: (
        get_state(message.from_user.id) or {}
    ).get("step") == "sos_description",
    content_types=["text"]
)
def handle_sos_description(message):
    user_id = message.from_user.id

    if not get_user(user_id) or is_user_blocked(user_id):
        clear_state(user_id)
        return

    text_value = (message.text or "").strip()

    if text_value in ("🏠 Головне меню", "❌ Скасувати"):
        clear_state(user_id)
        send_main_menu(message.chat.id, user_id)
        return

    state = get_state(user_id) or {}
    data = state.get("data", {})
    reason = data.get("reason", "Інше")
    description = "" if text_value == "Без опису" else text_value

    set_state(
        user_id,
        "sos_confirm",
        {"reason": reason, "description": description}
    )

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(
        "🚨 Підтвердити тривогу",
        callback_data="sos_confirm:yes"
    ))
    markup.add(types.InlineKeyboardButton(
        "❌ Скасувати",
        callback_data="sos_confirm:no"
    ))

    bot.send_message(
        message.chat.id,
        "Перевір дані:\n"
        f"<b>Причина:</b> {escape_text(reason)}\n"
        f"<b>Опис:</b> {escape_text(description or 'Не вказано')}",
        reply_markup=markup
    )


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("sos_confirm:")
)
def handle_sos_confirm(call):
    user_id = call.from_user.id
    choice = call.data.split(":", 1)[1]
    state = get_state(user_id) or {}

    if not get_user(user_id) or is_user_blocked(user_id):
        bot.answer_callback_query(
            call.id, "Немає доступу", show_alert=True
        )
        return

    if state.get("step") != "sos_confirm":
        bot.answer_callback_query(
            call.id, "Запит застарів. Створи тривогу заново."
        )
        return

    if choice != "yes":
        clear_state(user_id)
        bot.answer_callback_query(call.id, "Тривогу скасовано")
        bot.send_message(
            call.message.chat.id,
            "Тривогу не створено.",
            reply_markup=get_main_menu(get_user_role(user_id))
        )
        return

    data = state.get("data", {})

    try:
        incident_id = create_sos_incident(
            user_id=user_id,
            reason=data.get("reason", "Інше"),
            description=data.get("description", "")
        )
        clear_state(user_id)
        log_event(
            "sos_created",
            user_id,
            {"incident_id": incident_id}
        )
        notify_owner_sos(incident_id)

        bot.answer_callback_query(call.id, "Тривогу зареєстровано")
        bot.send_message(
            call.message.chat.id,
            f"🚨 Тривогу №{incident_id} зареєстровано. "
            "Відповідальних сповіщено.\n"
            "За безпосередньої небезпеки телефонуй 112, "
            "за невідкладної медичної ситуації в Україні також 103.",
            reply_markup=get_main_menu(get_user_role(user_id))
        )
    except Exception:
        log_exception("sos_create_failed", user_id)
        bot.answer_callback_query(
            call.id, "Не вдалося створити тривогу", show_alert=True
        )


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("ticket_category:")
)
def handle_ticket_category(call):
    user_id = call.from_user.id

    if not get_user(user_id) or is_user_blocked(user_id):
        bot.answer_callback_query(
            call.id, "Немає доступу", show_alert=True
        )
        return

    try:
        index = int(call.data.split(":", 1)[1])
        category = TICKET_CATEGORIES[index]
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Невірна категорія")
        return

    set_state(user_id, "ticket_description", {"category": category})
    bot.answer_callback_query(call.id)

    markup = types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        one_time_keyboard=True
    )
    markup.row(
        types.KeyboardButton("🏠 Головне меню"),
        types.KeyboardButton("❌ Скасувати")
    )

    bot.send_message(
        call.message.chat.id,
        f"Категорія: <b>{escape_text(category)}</b>\n"
        "Опиши звернення одним повідомленням.",
        reply_markup=markup
    )


@bot.message_handler(
    func=lambda message: (
        get_state(message.from_user.id) or {}
    ).get("step") == "ticket_description",
    content_types=["text"]
)
def handle_ticket_description(message):
    user_id = message.from_user.id

    if not get_user(user_id) or is_user_blocked(user_id):
        clear_state(user_id)
        return

    text_value = (message.text or "").strip()

    if text_value in ("🏠 Головне меню", "❌ Скасувати"):
        clear_state(user_id)
        send_main_menu(message.chat.id, user_id)
        return

    if not text_value:
        bot.send_message(message.chat.id, "Напиши текст звернення.")
        return

    state = get_state(user_id) or {}
    category = state.get("data", {}).get("category", "Інше")

    try:
        ticket_id = create_ticket(
            user_id=user_id,
            category=category,
            description=text_value
        )
        clear_state(user_id)
        log_event(
            "ticket_created",
            user_id,
            {"ticket_id": ticket_id, "category": category}
        )
        notify_owners_ticket(ticket_id)

        bot.send_message(
            message.chat.id,
            f"Звернення №{ticket_id} створено. Відповідальних сповіщено.",
            reply_markup=get_main_menu(get_user_role(user_id))
        )
    except Exception:
        log_exception("ticket_create_failed", user_id)
        bot.send_message(
            message.chat.id,
            "Не вдалося створити звернення. Спробуй пізніше."
        )


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("user_block:")
)
def handle_user_block(call):
    owner_id = call.from_user.id

    if get_user_role(owner_id) != "owner":
        bot.answer_callback_query(
            call.id, "Недостатньо прав", show_alert=True
        )
        return

    try:
        target_id = int(call.data.split(":", 1)[1])
    except ValueError:
        bot.answer_callback_query(call.id, "Невірний ID")
        return

    if target_id == owner_id:
        bot.answer_callback_query(
            call.id, "Не можна заблокувати самого себе", show_alert=True
        )
        return

    with db_connection() as conn:
        target = conn.execute(
            "SELECT user_id FROM users WHERE user_id=?",
            (target_id,)
        ).fetchone()

        if not target:
            bot.answer_callback_query(
                call.id, "Користувача не знайдено", show_alert=True
            )
            return

        conn.execute("""
            INSERT INTO blocked_users (user_id, blocked_by, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                blocked_by=excluded.blocked_by,
                created_at=excluded.created_at
        """, (target_id, owner_id, now_text()))

    clear_state(target_id)
    log_event("user_blocked", owner_id, {"target_id": target_id})
    bot.answer_callback_query(call.id, "Користувача заблоковано")
    bot.send_message(
        call.message.chat.id,
        f"Користувача {target_id} заблоковано."
    )


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("user_unblock:")
)
def handle_user_unblock(call):
    owner_id = call.from_user.id

    if get_user_role(owner_id) != "owner":
        bot.answer_callback_query(
            call.id, "Недостатньо прав", show_alert=True
        )
        return

    try:
        target_id = int(call.data.split(":", 1)[1])
    except ValueError:
        bot.answer_callback_query(call.id, "Невірний ID")
        return

    with db_connection() as conn:
        conn.execute(
            "DELETE FROM blocked_users WHERE user_id=?",
            (target_id,)
        )

    log_event("user_unblocked", owner_id, {"target_id": target_id})
    bot.answer_callback_query(call.id, "Блокування знято")
    bot.send_message(
        call.message.chat.id,
        f"Блокування користувача {target_id} знято."
    )


# Кінець частини 8.
# Тут навмисно немає ініціалізації БД чи запуску бота.
