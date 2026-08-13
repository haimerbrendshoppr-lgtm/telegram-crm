import time
import sqlite3
from datetime import datetime
import telebot
from telebot import types

TOKEN = "8709866998:AAFOTBJ_QphNhTCSjbo3BySKAeaznxSa9CE"
bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

def init_db():
    conn = sqlite3.connect("crm_bot.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            role TEXT,
            name TEXT,
            failed_attempts INTEGER DEFAULT 0,
            blocked_until TEXT
        )
    """)
    cursor.execute("""
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
    conn.commit()
    conn.close()

init_db()
user_states = {}

def get_main_menu(role):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    if role in ["owner", "staff", "collector", "manager"]:
        markup.add(types.KeyboardButton("📝 Створити замовлення"))
        markup.add(types.KeyboardButton("📦 Переглянути замовлення (не надіслані)"))
        markup.add(types.KeyboardButton("📋 Переглянути всі замовлення (50)"))
        markup.add(types.KeyboardButton("🔍 CRM пошук"))
    elif role == "shipper":
        markup.add(types.KeyboardButton("📝 Створити замовлення"))
        
    if role == "owner":
        markup.add(types.KeyboardButton("📊 Статистика"))
        markup.add(types.KeyboardButton("👥 Хто заходив у систему"))
        
    markup.add(types.KeyboardButton("🚪 Вийти з акаунта"))
    return markup

def get_order_inline_markup(order_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("📌 В роботі", callback_data=f"status_{order_id}_В роботі"),
        types.InlineKeyboardButton("🚚 Надіслано", callback_data=f"status_{order_id}_Надіслано")
    )
    markup.add(
        types.InlineKeyboardButton("❌ Скасовано", callback_data=f"status_{order_id}_Скасовано"),
        types.InlineKeyboardButton("✏️ Додати ТТН", callback_data=f"ttn_{order_id}")
    )
    return markup

@bot.message_handler(commands=['start'])
def cmd_start(message):
    conn = sqlite3.connect("crm_bot.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("SELECT role FROM users WHERE user_id = ?", (message.from_user.id,))
    user = cursor.fetchone()
    conn.close()

    if user:
        role = user[0]
        bot.send_message(message.chat.id, f"Ви авторизовані як: <b>{role.upper()}</b>", reply_markup=get_main_menu(role))
    else:
        user_states[message.from_user.id] = {"step": "waiting_password"}
        bot.send_message(message.chat.id, "Привіт! Будь ласка, введіть пароль доступу до системи:")

@bot.message_handler(func=lambda msg: msg.from_user.id in user_states and user_states[msg.from_user.id].get("step") == "waiting_password")
def process_password(message):
    password = message.text.strip()
    user_id = message.from_user.id
    username = message.from_user.username or f"id_{user_id}"
    
    passwords_map = {
        "df1317vbnm": "owner",
        "cks+48#$jgcie4": "manager",
        "cloudhub83592jdvo": "staff",
        "cloudsupenci": "collector",
        "dropgjrci385": "shipper"
    }

    conn = sqlite3.connect("crm_bot.db", check_same_thread=False)
    cursor = conn.cursor()

    if password in passwords_map:
        role = passwords_map[password]
        name = f"@{username}" if not username.startswith("id_") else username
        cursor.execute("INSERT OR REPLACE INTO users (user_id, role, name, failed_attempts, blocked_until) VALUES (?, ?, ?, 0, NULL)", (user_id, role, name))
        conn.commit()
        conn.close()
        del user_states[user_id]
        bot.send_message(message.chat.id, f"✅ Успішний вхід! Роль: <b>{role.upper()}</b>", reply_markup=get_main_menu(role))
    else:
        cursor.execute("SELECT failed_attempts FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        attempts = (row[0] + 1) if row else 1
        
        if attempts >= 3:
            conn.close()
            bot.send_message(message.chat.id, "stop spam 1 hour")
            if user_id in user_states:
                del user_states[user_id]
            return
            
        cursor.execute("INSERT OR REPLACE INTO users (user_id, role, name, failed_attempts, blocked_until) VALUES (?, 'blocked_temp', ?, ?, NULL)", (user_id, username, attempts))
        conn.commit()
        conn.close()
        bot.send_message(message.chat.id, f"❌ Неправильний пароль! Залишилось спроб: {3 - attempts}")

@bot.message_handler(func=lambda msg: msg.text == "🚪 Вийти з акаунта")
def logout(message):
    conn = sqlite3.connect("crm_bot.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE user_id = ?", (message.from_user.id,))
    conn.commit()
    conn.close()
    if message.from_user.id in user_states:
        del user_states[message.from_user.id]
    bot.send_message(message.chat.id, "Ви вийшли з акаунта.", reply_markup=types.ReplyKeyboardRemove())

@bot.message_handler(func=lambda msg: msg.text in ["❌ Скасувати пошук", "❌ Скасувати замовлення"])
def cancel_all_actions(message):
    user_id = message.from_user.id
    if user_id in user_states:
        del user_states[user_id]
        
    conn = sqlite3.connect("crm_bot.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("SELECT role FROM users WHERE user_id = ?", (user_id,))
    user = cursor.fetchone()
    conn.close()
    
    role = user[0] if user else "owner"
    bot.send_message(message.chat.id, "Дію скасовано. Головне меню:", reply_markup=get_main_menu(role))

@bot.message_handler(func=lambda msg: msg.text == "📝 Створити замовлення")
def start_order(message):
    user_states[message.from_user.id] = {"step": "order_phone", "data": {}}
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("❌ Скасувати замовлення"))
    bot.send_message(message.chat.id, "Введіть номер телефону клієнта:", reply_markup=markup)

@bot.message_handler(func=lambda msg: message_in_order_flow(msg))
def handle_order_flow(message):
    user_id = message.from_user.id
    state = user_states[user_id]
    step = state["step"]
    
    if step == "order_phone":
        state["data"]["phone"] = message.text
        state["step"] = "order_name"
        bot.send_message(message.chat.id, "Введіть ПІБ клієнта:")
    elif step == "order_name":
        state["data"]["full_name"] = message.text
        state["step"] = "order_city"
        bot.send_message(message.chat.id, "Введіть місто:")
    elif step == "order_city":
        state["data"]["city"] = message.text
        state["step"] = "order_branch"
        bot.send_message(message.chat.id, "Введіть номер відділення:")
    elif step == "order_branch":
        state["data"]["branch"] = message.text
        state["step"] = "order_price"
        bot.send_message(message.chat.id, "Введіть ціну замовлення:")
    elif step == "order_price":
        state["data"]["price"] = message.text
        state["step"] = "order_id"
        bot.send_message(message.chat.id, "Введіть номер замовлення:")
    elif step == "order_id":
        state["data"]["order_number"] = message.text
        state["step"] = "order_note"
        bot.send_message(message.chat.id, "Введіть примітки до замовлення:")
    elif step == "order_note":
        data = state["data"]
        note = message.text
        
        conn = sqlite3.connect("crm_bot.db", check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("SELECT role, name FROM users WHERE user_id = ?", (user_id,))
        user = cursor.fetchone()
        
        author = f"Дропшипер: {user[1]}" if user and user[0] == "shipper" else f"{user[0].upper() if user else 'OWNER'}"
        
        cursor.execute("""
            INSERT INTO orders (order_number, phone, full_name, city, branch, price, note, status, author, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'Нове', ?, ?)
        """, (data['order_number'], data['phone'], data['full_name'], data['city'], data['branch'], data['price'], note, author, datetime.now().strftime("%Y-%m-%d %H:%M")))
        conn.commit()
        conn.close()
        
        role = user[0] if user else "owner"
        del user_states[user_id]
        bot.send_message(message.chat.id, "✅ Замовлення успішно створено та збережено в базі!", reply_markup=get_main_menu(role))

def message_in_order_flow(message):
    if message.from_user.id not in user_states:
        return False
    return user_states[message.from_user.id].get("step", "").startswith("order_")

@bot.message_handler(func=lambda msg: msg.text == "📦 Переглянути замовлення (не надіслані)")
def view_unsent_orders(message):
    conn = sqlite3.connect("crm_bot.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("SELECT id, order_number, full_name, phone, price, status, city, branch, note, ttn FROM orders WHERE status != 'Надіслано' ORDER BY id DESC LIMIT 50")
    orders = cursor.fetchall()
    conn.close()
    
    if not orders:
        bot.send_message(message.chat.id, "Немає ненадісланих замовлень.")
        return
        
    for o in orders:
        text = (f"📦 <b>Замовлення №{o[1]}</b> (ID: {o[0]})\n"
                f"👤 Клієнт: {o[2]}\n"
                f"📞 Тел: {o[3]}\n"
                f"🏙 Місто: {o[6]}, Відд: {o[7]}\n"
                f"💰 Ціна: {o[4]}\n"
                f"📝 Примітки: {o[8]}\n"
                f"📌 Статус: {o[5]}\n"
                f"🚚 ТТН: {o[9] or 'Немає'}")
        bot.send_message(message.chat.id, text, reply_markup=get_order_inline_markup(o[0]))

@bot.message_handler(func=lambda msg: msg.text == "📋 Переглянути всі замовлення (50)")
def view_last_50(message):
    conn = sqlite3.connect("crm_bot.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("SELECT id, order_number, full_name, phone, price, status, city, branch, note, ttn FROM orders ORDER BY id DESC LIMIT 50")
    orders = cursor.fetchall()
    conn.close()
    
    if not orders:
        bot.send_message(message.chat.id, "База замовлень порожня.")
        return
        
    for o in orders:
        text = (f"📦 <b>Замовлення №{o[1]}</b> (ID: {o[0]})\n"
                f"👤 Клієнт: {o[2]}\n"
                f"📞 Тел: {o[3]}\n"
                f"🏙 Місто: {o[6]}, Відд: {o[7]}\n"
                f"💰 Ціна: {o[4]}\n"
                f"📝 Примітки: {o[8]}\n"
                f"📌 Статус: {o[5]}\n"
                f"🚚 ТТН: {o[9] or 'Немає'}")
        bot.send_message(message.chat.id, text, reply_markup=get_order_inline_markup(o[0]))

@bot.message_handler(func=lambda msg: msg.text == "📊 Статистика")
def show_statistics(message):
    conn = sqlite3.connect("crm_bot.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM orders")
    total_orders = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM orders WHERE status = 'Надіслано'")
    sent_orders = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM orders WHERE status != 'Надіслано'")
    unsent_orders = cursor.fetchone()[0]
    conn.close()

    text = (f"📊 <b>Статистика системи:</b>\n\n"
            f"📦 Всього замовлень: {total_orders}\n"
            f"🚚 Надіслано: {sent_orders}\n"
            f"⏳ Ненадіслано: {unsent_orders}")
    bot.send_message(message.chat.id, text)

@bot.message_handler(func=lambda msg: msg.text == "👥 Хто заходив у систему")
def show_logged_users(message):
    conn = sqlite3.connect("crm_bot.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, role, name FROM users WHERE role != 'blocked_temp'")
    users = cursor.fetchall()
    conn.close()

    if not users:
        bot.send_message(message.chat.id, "Ще ніхто не заходив у систему або таблиця порожня.")
        return

    text = "👥 <b>Користувачі, які авторизувалися в системі:</b>\n\n"
    for u in users:
        text += f"👤 Акаунт: <b>{u[2]}</b>\n🆔 ID: <code>{u[0]}</code>\n🔹 Роль: {u[1].upper()}\n\n"
    
    bot.send_message(message.chat.id, text)

@bot.message_handler(func=lambda msg: msg.text == "🔍 CRM пошук")
def crm_search_menu(message):
    user_states[message.from_user.id] = {"step": "waiting_search"}
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("❌ Скасувати пошук"))
    bot.send_message(message.chat.id, "Введіть дані для пошуку (ім'я, телефон або номер замовлення):", reply_markup=markup)

@bot.message_handler(func=lambda msg: msg.from_user.id in user_states and user_states[msg.from_user.id].get("step") == "waiting_search")
def perform_search(message):
    query = f"%{message.text.strip()}%"
    conn = sqlite3.connect("crm_bot.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, order_number, full_name, phone, city, branch, price, note, status, ttn, author FROM orders 
        WHERE order_number LIKE ? OR full_name LIKE ? OR phone LIKE ?
    """, (query, query, query))
    orders = cursor.fetchall()
    
    cursor.execute("SELECT role FROM users WHERE user_id = ?", (message.from_user.id,))
    user = cursor.fetchone()
    conn.close()
    
    role = user[0] if user else "owner"
    del user_states[message.from_user.id]
    
    if not orders:
        bot.send_message(message.chat.id, "Нічого не знайдено за вашим запитом.", reply_markup=get_main_menu(role))
        return
        
    for o in orders:
        text = (f"📦 <b>Замовлення №{o[1]}</b> (ID: {o[0]})\n"
                f"👤 ПІБ: {o[2]}\n"
                f"📞 Тел: {o[3]}\n"
                f"🏙 Місто: {o[4]}, Відділення: {o[5]}\n"
                f"💰 Ціна: {o[6]}\n"
                f"📝 Примітки: {o[7]}\n"
                f"📌 Статус: {o[8]}\n"
                f"🚚 ТТН: {o[9] or 'Немає'}")
        bot.send_message(message.chat.id, text, reply_markup=get_order_inline_markup(o[0]))
    bot.send_message(message.chat.id, "Головне меню:", reply_markup=get_main_menu(role))

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    data = call.data
    user_id = call.from_user.id
    
    if data.startswith("status_"):
        parts = data.split("_", 2)
        order_id = parts[1]
        new_status = parts[2]
        
        conn = sqlite3.connect("crm_bot.db", check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute("UPDATE orders SET status = ? WHERE id = ?", (new_status, order_id))
        conn.commit()
        
        cursor.execute("SELECT id, order_number, full_name, phone, price, status, city, branch, note, ttn FROM orders WHERE id = ?", (order_id,))
        o = cursor.fetchone()
        conn.close()
        
        if o:
            text = (f"📦 <b>Замовлення №{o[1]}</b> (ID: {o[0]})\n"
                    f"👤 Клієнт: {o[2]}\n"
                    f"📞 Тел: {o[3]}\n"
                    f"🏙 Місто: {o[6]}, Відд: {o[7]}\n"
                    f"💰 Ціна: {o[4]}\n"
                    f"📝 Примітки: {o[8]}\n"
                    f"📌 Статус: <b>{o[5]}</b>\n"
                    f"🚚 ТТН: {o[9] or 'Немає'}")
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=get_order_inline_markup(o[0]))
        bot.answer_callback_query(call.id, f"Статус змінено на: {new_status}")

    elif data.startswith("ttn_"):
        order_id = data.split("_")[1]
        user_states[user_id] = {"step": "waiting_ttn", "order_id": order_id}
        bot.send_message(call.message.chat.id, "Введіть номер ТТН для цього замовлення:")
        bot.answer_callback_query(call.id)

@bot.message_handler(func=lambda msg: msg.from_user.id in user_states and user_states[msg.from_user.id].get("step") == "waiting_ttn")
def save_ttn_input(message):
    user_id = message.from_user.id
    ttn = message.text.strip()
    order_id = user_states[user_id]["order_id"]
    
    conn = sqlite3.connect("crm_bot.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("UPDATE orders SET ttn = ? WHERE id = ?", (ttn, order_id))
    conn.commit()
    
    cursor.execute("SELECT id, order_number, full_name, phone, price, status, city, branch, note, ttn FROM orders WHERE id = ?", (order_id,))
    o = cursor.fetchone()
    conn.close()
    
    del user_states[user_id]
    bot.send_message(message.chat.id, f"✅ ТТН успішно оновлено для замовлення №{o[1]}!")

if __name__ == "__main__":
    bot.infinity_polling()
