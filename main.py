import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton

# Налаштування логування
logging.basicConfig(level=logging.INFO)

# Твій токен бота
TOKEN = "8709866998:AAFOTBJ_QphNhTCSjbo3BySKAeaznxSa9CE"

bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# --- БАЗА ДАНИХ ---
def init_db():
    conn = sqlite3.connect("crm_bot.db")
    cursor = conn.cursor()
    
    # Таблиця користувачів (ролі та безпека)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            role TEXT,
            name TEXT,
            failed_attempts INTEGER DEFAULT 0,
            blocked_until TEXT
        )
    """)
    
    # Таблиця замовлень
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
            sent_at TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

# --- FSM СТАНИ (Wizard для створення замовлення) ---
class OrderStates(StatesGroup):
    waiting_for_phone = State()
    waiting_for_name = State()
    waiting_for_city = State()
    waiting_for_branch = State()
    waiting_for_price = State()
    waiting_for_order_id = State()
    waiting_for_note = State()

class TTNStates(StatesGroup):
    waiting_for_ttn = State()

class SearchStates(StatesGroup):
    waiting_for_query = State()

class AuthStates(StatesGroup):
    waiting_for_password = State()

# --- КЛАВІАТУРИ ---
def get_main_menu(role):
    keyboard = []
    if role in ["owner", "staff", "collector", "manager"]:
        keyboard.append([KeyboardButton(text="📝 Створити замовлення")])
        keyboard.append([KeyboardButton(text="📦 Переглянути замовлення (не надіслані)")])
        keyboard.append([KeyboardButton(text="📋 Переглянути всі замовлення (50)")])
        keyboard.append([KeyboardButton(text="🔍 CRM пошук")])
    elif role == "shipper":
        keyboard.append([KeyboardButton(text="📝 Створити замовлення")])
        
    if role == "owner":
        keyboard.append([KeyboardButton(text="📊 Статистика")])
        
    keyboard.append([KeyboardButton(text="🚪 Вийти з акаунта")])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

def get_wizard_cancel_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="❌ Скасувати замовлення")],
            [KeyboardButton(text="🏠 Головне меню")]
        ],
        resize_keyboard=True
    )

# --- АВТОРИЗАЦІЯ ТА БЕЗПЕКА ---
@dp.message(F.text == "/start")
async def cmd_start(message: types.Message, state: FSMContext):
    conn = sqlite3.connect("crm_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT role FROM users WHERE user_id = ?", (message.from_user.id,))
    user = cursor.fetchone()
    conn.close()

    if user:
        role = user[0]
        await message.answer(f"Ви вже авторизовані як: <b>{role.upper()}</b>", parse_mode="HTML", reply_markup=get_main_menu(role))
    else:
        await message.answer("Привіт! Будь ласка, введіть пароль доступу до системи:")
        await state.set_state(AuthStates.waiting_for_password)

@dp.message(AuthStates.waiting_for_password)
async def process_password(message: types.Message, state: FSMContext):
    password = message.text.strip()
    user_id = message.from_user.id
    username = message.from_user.username or "без username"
    
    conn = sqlite3.connect("crm_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT failed_attempts, blocked_until FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    
    now = datetime.now()
    if row:
        attempts, blocked_until = row[0], row[1]
        if blocked_until and datetime.fromisoformat(blocked_until) > now:
            conn.close()
            await message.answer("stop spam 1 hour")
            return
    else:
        attempts = 0

    passwords_map = {
        "df1317vbnm": "owner",
        "cks+48#$jgcie4": "manager",
        "cloudhub83592jdvo": "staff",
        "cloudsupenci": "collector",
        "dropgjrci385": "shipper"
    }

    if password in passwords_map:
        role = passwords_map[password]
        name = username if role == "shipper" else role
        cursor.execute("INSERT OR REPLACE INTO users (user_id, role, name, failed_attempts, blocked_until) VALUES (?, ?, ?, 0, NULL)", (user_id, role, name))
        conn.commit()
        conn.close()
        await state.clear()
        await message.answer(f"✅ Успішний вхід! Роль: <b>{role.upper()}</b>", parse_mode="HTML", reply_markup=get_main_menu(role))
    else:
        attempts += 1
        blocked_until_str = None
        
        # Захист за рівнями
        if attempts >= 3 and attempts < 5:
            blocked_until_str = (now + timedelta(hours=1)).isoformat()
            cursor.execute("INSERT OR REPLACE INTO users (user_id, role, name, failed_attempts, blocked_until) VALUES (?, 'blocked', ?, ?, ?)", (user_id, username, attempts, blocked_until_str))
            conn.commit()
            conn.close()
            
            # Сповіщення власнику та керівнику про злом
            cursor2 = sqlite3.connect("crm_bot.db").cursor()
            cursor2.execute("SELECT user_id FROM users WHERE role IN ('owner', 'manager')")
            admins = cursor2.fetchall()
            for admin in admins:
                try:
                    await bot.send_message(admin[0], f"УВАГА СПРОБА НЕСАНКЦІОНОВАНОГО ДОСТУПУ ДО СИСТЕМИ ДАННИХ В РАЗІ ПОВТОРЕННЯ ДАННОГО ПОВІДОМЛЕННЯ ТЕРМІНОВО ЗВЕРНУТИСЬ В ІТ ВІДДІЛ ⚠️\nКористувач: @{username} (ID: {user_id})")
                except:
                    pass
            
            await message.answer("stop spam 1 hour")
            await state.clear()
            return
            
        elif attempts >= 5:
            blocked_until_str = (now + timedelta(minutes=1)).isoformat()
            cursor.execute("INSERT OR REPLACE INTO users (user_id, role, name, failed_attempts, blocked_until) VALUES (?, 'blocked', ?, ?, ?)", (user_id, username, attempts, blocked_until_str))
            conn.commit()
            
            # Екстрений алерт власнику з username
            cursor.execute("SELECT user_id FROM users WHERE role = 'owner'")
            owners = cursor.fetchall()
            for owner in owners:
                try:
                    await bot.send_message(owner[0], f"🚨 ЕКСТРЕНА ТРИВОГА! Зловмисник з username @{username} (ID: {user_id}) намагається зламати систему! Зроблено вже {attempts} невірних спроб.")
                except:
                    pass
            conn.close()
            
            await message.answer("Занадто багато невірних спроб. Вхід заблоковано на 1 хвилину.")
            await state.clear()
            return
            
        cursor.execute("INSERT OR REPLACE INTO users (user_id, role, name, failed_attempts, blocked_until) VALUES (?, 'blocked_temp', ?, ?, NULL)", (user_id, username, attempts))
        conn.commit()
        conn.close()
        await message.answer(f"❌ Неправильний пароль! Залишилось спроб до блокування: {3 - attempts}")

@dp.message(F.text == "🚪 Вийти з акаунта")
async def logout(message: types.Message, state: FSMContext):
    conn = sqlite3.connect("crm_bot.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE user_id = ?", (message.from_user.id,))
    conn.commit()
    conn.close()
    await state.clear()
    await message.answer("Ви вийшли з акаунта. Введіть /start для повторного входу.")

# --- СТВОРЕННЯ ЗАМОВЛЕННЯ (WIZARD) ---
@dp.message(F.text == "📝 Створити замовлення")
async def start_order(message: types.Message, state: FSMContext):
    conn = sqlite3.connect("crm_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT role FROM users WHERE user_id = ?", (message.from_user.id,))
    user = cursor.fetchone()
    conn.close()
    
    if not user or user[0] in ["blocked", "blocked_temp"]:
        await message.answer("Будь ласка, авторизуйтесь через /start")
        return

    await state.set_state(OrderStates.waiting_for_phone)
    await message.answer("Введіть номер телефону клієнта:", reply_markup=get_wizard_cancel_menu())

@dp.message(F.text.in_(["❌ Скасувати замовлення", "🏠 Головне меню"]))
async def cancel_order(message: types.Message, state: FSMContext):
    await state.clear()
    conn = sqlite3.connect("crm_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT role FROM users WHERE user_id = ?", (message.from_user.id,))
    user = cursor.fetchone()
    conn.close()
    role = user[0] if user else "owner"
    await message.answer("Дію скасовано. Головне меню:", reply_markup=get_main_menu(role))

@dp.message(OrderStates.waiting_for_phone)
async def process_phone(message: types.Message, state: FSMContext):
    await state.update_data(phone=message.text)
    await state.set_state(OrderStates.waiting_for_name)
    await message.answer("Введіть ПІБ клієнта:")

@dp.message(OrderStates.waiting_for_name)
async def process_name(message: types.Message, state: FSMContext):
    await state.update_data(full_name=message.text)
    await state.set_state(OrderStates.waiting_for_city)
    await message.answer("Введіть місто:")

@dp.message(OrderStates.waiting_for_city)
async def process_city(message: types.Message, state: FSMContext):
    await state.update_data(city=message.text)
    await state.set_state(OrderStates.waiting_for_branch)
    await message.answer("Введіть номер відділення:")

@dp.message(OrderStates.waiting_for_branch)
async def process_branch(message: types.Message, state: FSMContext):
    await state.update_data(branch=message.text)
    await state.set_state(OrderStates.waiting_for_price)
    await message.answer("Введіть ціну замовлення:")

@dp.message(OrderStates.waiting_for_price)
async def process_price(message: types.Message, state: FSMContext):
    await state.update_data(price=message.text)
    await state.set_state(OrderStates.waiting_for_order_id)
    await message.answer("Введіть номер замовлення:")

@dp.message(OrderStates.waiting_for_order_id)
async def process_order_id(message: types.Message, state: FSMContext):
    await state.update_data(order_number=message.text)
    await state.set_state(OrderStates.waiting_for_note)
    await message.answer("Введіть примітки до замовлення:")

@dp.message(OrderStates.waiting_for_note)
async def process_note(message: types.Message, state: FSMContext):
    data = await state.get_data()
    user_id = message.from_user.id
    
    conn = sqlite3.connect("crm_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT role, name FROM users WHERE user_id = ?", (user_id,))
    user = cursor.fetchone()
    conn.close()
    
    author = f"Дропшипер: {user[1]}" if user[0] == "shipper" else f"{user[0].upper()}: {message.from_user.username or user_id}"
    
    conn = sqlite3.connect("crm_bot.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO orders (order_number, phone, full_name, city, branch, price, note, status, author, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'Нове', ?, ?)
    """, (data['order_number'], data['phone'], data['full_name'], data['city'], data['branch'], data['price'], message.text, author, datetime.now().strftime("%Y-%m-%d %H:%M")))
    conn.commit()
    conn.close()
    
    await state.clear()
    await message.answer("✅ Замовлення успішно створено та збережено в базі!", reply_markup=get_main_menu(user[0]))

# --- ПЕРЕГЛЯД ЗАМОВЛЕНЬ ---
@dp.message(F.text == "📦 Переглянути замовлення (не надіслані)")
async def view_unshipped(message: types.Message):
    conn = sqlite3.connect("crm_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT role FROM users WHERE user_id = ?", (message.from_user.id,))
    user = cursor.fetchone()
    
    if not user:
        conn.close()
        return
        
    cursor.execute("SELECT id, order_number, full_name, phone, price, status FROM orders WHERE status != 'Надіслано' ORDER BY id DESC")
    orders = cursor.fetchall()
    conn.close()
    
    if not orders:
        await message.answer("Немає не надісланих замовлень.")
        return
        
    for o in orders[:20]: # Виводимо останні не надіслані
        text = f"📦 <b>Замовлення №{o[1]}</b> (ID: {o[0]})\n👤 Клієнт: {o[2]}\n📞 Тел: {o[3]}\n💰 Ціна: {o[4]}\n📌 Статус: {o[5]}"
        await message.answer(text, parse_mode="HTML")

@dp.message(F.text == "📋 Переглянути всі замовлення (50)")
async def view_last_50(message: types.Message):
    conn = sqlite3.connect("crm_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, order_number, full_name, phone, price, status FROM orders ORDER BY id DESC LIMIT 50")
    orders = cursor.fetchall()
    conn.close()
    
    if not orders:
        await message.answer("База замовлень порожня.")
        return
        
    for o in orders:
        text = f"📦 <b>Замовлення №{o[1]}</b> (ID: {o[0]})\n👤 Клієнт: {o[2]}\n📞 Тел: {o[3]}\n💰 Ціна: {o[4]}\n📌 Статус: {o[5]}"
        await message.answer(text, parse_mode="HTML")

# --- CRM ПОШУК ТА ТТН ---
@dp.message(F.text == "🔍 CRM пошук")
async def crm_search_menu(message: types.Message):
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔎 Пошук за даними"), KeyboardButton(text="📂 Відкрити всі")],
            [KeyboardButton(text="🏠 Головне меню")]
        ],
        resize_keyboard=True
    )
    await message.answer("Оберіть режим пошуку:", reply_markup=keyboard)

@dp.message(F.text == "📂 Відкрити всі")
async def open_all_orders(message: types.Message):
    conn = sqlite3.connect("crm_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, order_number, full_name, phone, price, status, ttn FROM orders ORDER BY id DESC")
    orders = cursor.fetchall()
    conn.close()
    
    if not orders:
        await message.answer("База порожня.")
        return
        
    for o in orders[:30]:
        text = f"📦 <b>Замовлення №{o[1]}</b>\n👤 {o[2]} | 📞 {o[3]}\n💰 {o[4]} | Статус: {o[5]} | ТТН: {o[6] or 'Немає'}"
        await message.answer(text, parse_mode="HTML")

@dp.message(F.text == "🔎 Пошук за даними")
async def ask_search_query(message: types.Message, state: FSMContext):
    await state.set_state(SearchStates.waiting_for_query)
    await message.answer("Введіть дані для пошуку (ім'я, прізвище, номер телефону, номер замовлення або ціну):", reply_markup=get_wizard_cancel_menu())

@dp.message(SearchStates.waiting_for_query)
async def perform_search(message: types.Message, state: FSMContext):
    query = f"%{message.text.strip()}%"
    conn = sqlite3.connect("crm_bot.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, order_number, full_name, phone, city, branch, price, note, status, ttn, author FROM orders 
        WHERE order_number LIKE ? OR full_name LIKE ? OR phone LIKE ? OR price LIKE ?
    """, (query, query, query, query))
    orders = cursor.fetchall()
    
    cursor.execute("SELECT role FROM users WHERE user_id = ?", (message.from_user.id,))
    user = cursor.fetchone()
    conn.close()
    
    role = user[0] if user else "staff"
    await state.clear()
    
    if not orders:
        await message.answer("Нічого не знайдено за вашим запитом.", reply_markup=get_main_menu(role))
        return
        
    for o in orders:
        text = (f"📦 <b>Замовлення №{o[1]}</b> (ID: {o[0]})\n"
                f"👤 ПІБ: {o[2]}\n"
                f"📞 Тел: {o[3]}\n"
                f"🏙 Місто: {o[4]}, Відділення: {o[5]}\n"
                f"💰 Ціна: {o[6]}\n"
                f"📝 Примітки: {o[7]}\n"
                f"📌 Статус: {o[8]}\n"
                f"🚚 ТТН: {o[9] or 'Немає'}\n"
                f"✍️ Автор: {o[10]}")
                
        markup = None
        # Якщо власник або керівник і ТТН порожня — даємо кнопку додавання ТТН
        if role in ["owner", "manager"] and not o[9]:
            markup = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Додати ТТН", callback_data=f"add_ttn_{o[0]}")]
            ])
            
        await message.answer(text, parse_mode="HTML", reply_markup=markup)
    await message.answer("Головне меню:", reply_markup=get_main_menu(role))

# Обробка кнопки додавання ТТН
@dp.callback_query(F.data.startswith("add_ttn_"))
async def callback_add_ttn(callback: types.CallbackQuery, state: FSMContext):
    order_id = callback.data.split("_")[2]
    await state.update_data(target_order_id=order_id)
    await state.set_state(TTNStates.waiting_for_ttn)
    await callback.message.answer("Введіть номер ТТН для цього замовлення:")
    await callback.answer()

@dp.message(TTNStates.waiting_for_ttn)
async def save_ttn(message: types.Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("target_order_id")
    ttn_number = message.text.strip()
    
    conn = sqlite3.connect("crm_bot.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE orders SET ttn = ? WHERE id = ?", (ttn_number, order_id))
    conn.commit()
    conn.close()
    
    await state.clear()
    await message.answer("✅ ТТН успішно додано до замовлення!")

# --- СТАТИСТИКА ДЛЯ ВЛАСНИКА ---
@dp.message(F.text == "📊 Статистика")
async def owner_statistics(message: types.Message):
    conn = sqlite3.connect("crm_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT role FROM users WHERE user_id = ?", (message.from_user.id,))
    user = cursor.fetchone()
    
    if not user or user[0] != "owner":
        conn.close()
        await message.answer("У вас немає доступу до статистики.")
        return
        
    # Статистика за останні 30 днів
    cursor.execute("""
        SELECT date(created_at), COUNT(*) FROM orders 
        WHERE created_at >= date('now', '-30 days') 
        GROUP BY date(created_at) 
        ORDER BY date(created_at) DESC
    """)
    stats = cursor.fetchall()
    conn.close()
    
    if not stats:
        await message.answer("За останні 30 днів немає даних про замовлення.")
        return
        
    text = "📊 <b>Статистика замовлень за останні 30 днів:</b>\n\n"
    for row in stats:
        # Конвертуємо дату з РРРР-ММ-ДД в ДД.ММ.РРРР
        try:
            date_formatted = datetime.strptime(row[0], "%Y-%m-%d").strftime("%d.%m.%Y")
        except:
            date_formatted = row[0]
        text += f"📅 <b>{date_formatted}</b> — {row[1]} замовлень\n"
        
    await message.answer(text, parse_mode="HTML")

# --- ІНТЕРАКТИВНІ КНОПКИ СТАТУСІВ ЗВІТУ О 18:00 ---
@dp.callback_query(F.data.startswith("status_"))
async def change_order_status(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    status_type = parts[1] # gathered, not_gathered, completing
    order_id = parts[2]
    
    status_map = {
        "gathered": "Зібрано",
        "not_gathered": "Не зібрано",
        "completing": "Докомплектовується"
    }
    new_status = status_map.get(status_type, "Нове")
    
    conn = sqlite3.connect("crm_bot.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE orders SET status = ? WHERE id = ?", (new_status, order_id))
    conn.commit()
    conn.close()
    
    await callback.message.edit_text(f"{callback.message.text}\n\n<b>Оновлено статус: {new_status}</b>", parse_mode="HTML")
    await callback.answer("Статус змінено!")

# --- ФОНОВІ ПЛАНУВАЛЬНИКИ (О 18:00 та О 16:00 через 4 дні) ---
async def background_scheduler():
    while True:
        now = da