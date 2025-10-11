import json
import sqlite3
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from openai import OpenAI
import asyncio
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import matplotlib.pyplot as plt
import io
import hashlib
from urllib.parse import urlencode
from flask import Flask, request
import threading
import os
import logging

# --- Импортируем конфигурацию ---
from config import API_TOKEN, OPENROUTER_API_KEY, ROBOKASSA_LOGIN, ROBOKASSA_PASS1, ROBOKASSA_PASS2, WEBHOOK_URL, ADMIN_PASSWORD

# --- Настройка логирования ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Инициализация ---
bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# --- OpenAI клиент ---
client = OpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url='https://openrouter.ai/api/v1/' # Исправлено: убран пробел в конце
)

# --- Подключение к SQLite ---
conn = sqlite3.connect('trainer_bot.db', check_same_thread=False)
cur = conn.cursor()

# --- Создание таблиц ---
cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT,
    age INTEGER,
    weight REAL,
    height REAL,
    goal TEXT,
    subscription_end_date TEXT DEFAULT NULL,
    trial_used BOOLEAN DEFAULT FALSE,
    last_weight_entry TEXT DEFAULT NULL,
    gender TEXT DEFAULT NULL,
    training_location TEXT DEFAULT NULL,
    level TEXT DEFAULT NULL
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS progress (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    weight REAL,
    date TEXT,
    FOREIGN KEY (user_id) REFERENCES users (id)
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS achievements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    title TEXT,
    description TEXT,
    unlocked_date TEXT,
    FOREIGN KEY (user_id) REFERENCES users (id)
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS training_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    date TEXT,
    status TEXT, -- 'completed', 'skipped', 'scheduled'
    FOREIGN KEY (user_id) REFERENCES users (id)
)
""")

conn.commit()

# --- Глобальные переменные ---
user_states = {}
scheduler = AsyncIOScheduler()
reminder_times = {} # {user_id: time_str}
loop = None # <-- Глобальная переменная для asyncio цикла

# --- Вспомогательные функции ---
def get_user_profile(user_id):
    cur.execute("SELECT name, age, weight, height, goal, gender, training_location, level FROM users WHERE id = ?", (user_id,))
    row = cur.fetchone()
    if row:
        return {
            "name": row[0],
            "age": row[1],
            "weight": row[2],
            "height": row[3],
            "goal": row[4],
            "gender": row[5],
            "training_location": row[6],
            "level": row[7]
        }
    return None

def is_subscribed(user_id):
    cur.execute("SELECT subscription_end_date FROM users WHERE id = ?", (user_id,))
    result = cur.fetchone()
    if result and result[0]:
        sub_end = datetime.fromisoformat(result[0])
        now = datetime.now()
        return now < sub_end
    return False

def get_trial_days_left(user_id):
    cur.execute("SELECT trial_used, subscription_end_date FROM users WHERE id = ?", (user_id,))
    result = cur.fetchone()
    if result:
        trial_used, sub_end_str = result
        if trial_used and sub_end_str:
            sub_end = datetime.fromisoformat(sub_end_str)
            now = datetime.now()
            if now < sub_end:
                delta = sub_end - now
                return delta.days + 1
    return 0

def add_achievement(user_id, title, description):
    now = datetime.now().isoformat()
    cur.execute("INSERT INTO achievements (user_id, title, description, unlocked_date) VALUES (?, ?, ?, ?)",
                (user_id, title, description, now))
    conn.commit()

def check_and_award_achievements(user_id, profile):
    # Пример: достижение за заполнение анкеты
    if profile and profile.get("name"):
        cur.execute("SELECT id FROM achievements WHERE user_id = ? AND title = ?", (user_id, "Первые шаги"))
        if cur.fetchone() is None:
            add_achievement(user_id, "Первые шаги", "Заполнил анкету и начал путь к здоровью!")

def save_training_log(user_id, date_str, status):
    cur.execute("INSERT INTO training_logs (user_id, date, status) VALUES (?, ?, ?)",
                (user_id, date_str, status))
    conn.commit()

def get_recent_training_status(user_id, days=3):
    since_date = datetime.now() - timedelta(days=days)
    cur.execute("SELECT status FROM training_logs WHERE user_id = ? AND date >= ? ORDER BY date DESC", (user_id, since_date.isoformat()))
    return [row[0] for row in cur.fetchall()]

def add_message_id(user_id, message_id):
    # Простая реализация: хранение в памяти. Для продакшена лучше в БД.
    if user_id not in user_states:
        user_states[user_id] = {"messages": []}
    user_states[user_id]["messages"].append(message_id)

async def delete_old_messages(user_id, keep_last=1):
    if user_id in user_states and "messages" in user_states[user_id]:
        messages = user_states[user_id]["messages"]
        if len(messages) > keep_last:
            to_delete = messages[:-keep_last]
            user_states[user_id]["messages"] = messages[-keep_last:]
            for msg_id in to_delete:
                try:
                    await bot.delete_message(chat_id=user_id, message_id=msg_id)
                except Exception as e:
                    logger.warning(f"Could not delete message {msg_id} for user {user_id}: {e}")

# --- Обработчики команд ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.first_name

    # Проверяем, есть ли пользователь в базе
    cur.execute("SELECT id, trial_used FROM users WHERE id = ?", (user_id,))
    result = cur.fetchone()

    if not result:
        # Новый пользователь
        logger.info(f"Новый пользователь: {user_id} ({username})")
        cur.execute("INSERT INTO users (id, name) VALUES (?, ?)", (user_id, username))
        conn.commit()

        # Выдаём пробную неделю, если не использовалась
        cur.execute("SELECT trial_used FROM users WHERE id = ?", (user_id,))
        trial_used_result = cur.fetchone()
        if not trial_used_result or not trial_used_result[0]:
            trial_end_date = datetime.now() + timedelta(days=7)
            cur.execute("UPDATE users SET trial_used = TRUE, subscription_end_date = ? WHERE id = ?", (trial_end_date.isoformat(), user_id))
            conn.commit()
            logger.info(f"Выдана пробная неделя пользователю {user_id}")
            msg = await message.answer("Привет! 🏋️‍♂️ Я твой ИИ-тренер. Ты получил <b>бесплатную неделю подписки</b>! Для начала, давай заполним анкету. Как тебя зовут?")
            add_message_id(user_id, msg.message_id)
            user_states[user_id] = {"step": "name", "data": {}}
        else:
            msg = await message.answer("Привет! 🏋️‍♂️ Я твой ИИ-тренер. Для начала, давай заполним анкету. Как тебя зовут?")
            add_message_id(user_id, msg.message_id)
            user_states[user_id] = {"step": "name", "data": {}}
    else:
        # Старый пользователь
        logger.info(f"Старый пользователь: {user_id} ({username})")
        msg = await message.answer("Привет! 🏋️‍♂️ С возвращением! Используй /menu для навигации.")
        add_message_id(user_id, msg.message_id)

@dp.message(Command("subscribe"))
async def cmd_subscribe(message: types.Message):
    if is_subscribed(message.from_user.id):
        days_left = get_trial_days_left(message.from_user.id)
        sub_end = datetime.fromisoformat(cur.execute("SELECT subscription_end_date FROM users WHERE id = ?", (message.from_user.id,)).fetchone()[0])
        msg = await message.answer(f"У вас активна подписка до {sub_end.strftime('%d.%m.%Y')}.")
        add_message_id(message.from_user.id, msg.message_id)
        if days_left > 0:
            msg = await message.answer(f"Из них <b>{days_left} дней</b> — осталось от пробной недели.")
            add_message_id(message.from_user.id, msg.message_id)
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 месяц - 499 руб", callback_data="buy_month")],
        [InlineKeyboardButton(text="6 месяцев - 2499 руб", callback_data="buy_half_year")],
        [InlineKeyboardButton(text="1 год - 4999 руб", callback_data="buy_year")],
    ])
    msg = await message.answer("<b>Тарифы подписки:</b>\n\n"
                         "1 месяц — 499 руб\n"
                         "6 месяцев — 2499 руб (416 руб/мес)\n"
                         "1 год — 4999 руб (416 руб/мес)\n\n"
                         "<a href='https://docs.google.com/document/d/14NrOTKOJ2Dcd5-guVZGU7fRj9gj-wS1X/edit?usp=drive_link&ouid=111319375229341079989&rtpof=true&sd=true'>Оферта</a>\n\n"
                         "Выберите тариф:", reply_markup=keyboard)
    add_message_id(message.from_user.id, msg.message_id)

@dp.callback_query(lambda c: c.data.startswith('buy_'))
async def process_subscription_choice(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    choice = callback_query.data

    prices = {
        "buy_month": 499,
        "buy_half_year": 2499,
        "buy_year": 4999
    }
    months = {
        "buy_month": 1,
        "buy_half_year": 6,
        "buy_year": 12
    }

    price = prices[choice]
    months_added = months[choice]

    # Подготовка параметров для Robokassa
    inv_id = user_id # Можно использовать ID пользователя как InvoiceID
    desc = f"Подписка на {months_added} месяцев"
    signature = hashlib.md5(f"{ROBOKASSA_LOGIN}:{price}:{inv_id}:{ROBOKASSA_PASS1}:shp_userid={user_id}".encode()).hexdigest()

    params = {
        'MerchantLogin': ROBOKASSA_LOGIN,
        'OutSum': price,
        'InvId': inv_id,
        'Desc': desc,
        'SignatureValue': signature,
        'Shp_userid': user_id,
        'Culture': 'ru',
        'Encoding': 'utf-8'
    }
    robokassa_url = "https://auth.robokassa.ru/Merchant/Index.aspx?" + urlencode(params) # Исправлено: убраны пробелы

    msg = await bot.send_message(user_id, f"Ссылка для оплаты подписки на {months_added} месяцев ({price} руб):\n{robokassa_url}\n\nПосле оплаты подпишитесь повторно, чтобы обновить статус.")
    add_message_id(user_id, msg.message_id)
    await callback_query.answer()

# --- Обработчики анкеты ---
@dp.message(lambda message: message.from_user.id in user_states and 'step' in user_states[message.from_user.id])
async def process_profile(message: types.Message):
    user_id = message.from_user.id
    if user_id not in user_states:
        return
    state = user_states[user_id]
    step = state["step"]
    data = state["data"]

    if step == "name":
        name = message.text.strip()
        if len(name) < 2:
            msg = await message.answer("Пожалуйста, введи настоящее имя (минимум 2 символа).")
            add_message_id(user_id, msg.message_id)
            return
        data["name"] = name
        state["step"] = "age"
        msg = await message.answer(f"Отлично, {name}! Сколько тебе лет? (введите число)")
        add_message_id(user_id, msg.message_id)
        await delete_old_messages(user_id, keep_last=0)

    elif step == "age":
        try:
            age = int(message.text.strip())
            if age < 10 or age > 100:
                msg = await message.answer("Пожалуйста, введи реальный возраст (от 10 до 100).")
                add_message_id(user_id, msg.message_id)
                return
            data["age"] = age
            state["step"] = "gender"
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Мужской", callback_data="gender_male")],
                [InlineKeyboardButton(text="Женский", callback_data="gender_female")]
            ])
            msg = await message.answer("Какой у тебя пол?", reply_markup=keyboard)
            add_message_id(user_id, msg.message_id)
            await delete_old_messages(user_id, keep_last=0)
        except ValueError:
            msg = await message.answer("Пожалуйста, введи число.")
            add_message_id(user_id, msg.message_id)

    elif step == "weight":
        try:
            weight = float(message.text.strip().replace(',', '.'))
            if weight < 30 or weight > 300:
                msg = await message.answer("Пожалуйста, введи реальный вес (от 30 до 300 кг).")
                add_message_id(user_id, msg.message_id)
                return
            data["weight"] = weight
            state["step"] = "height"
            msg = await message.answer("Какой у тебя рост (в см)?")
            add_message_id(user_id, msg.message_id)
            await delete_old_messages(user_id, keep_last=0)
        except ValueError:
            msg = await message.answer("Пожалуйста, введи число (можно с точкой).")
            add_message_id(user_id, msg.message_id)

    elif step == "height":
        try:
            height = int(message.text.strip())
            if height < 100 or height > 250:
                msg = await message.answer("Пожалуйста, введи реальный рост в см (от 100 до 250).")
                add_message_id(user_id, msg.message_id)
                return
            data["height"] = height
            state["step"] = "goal"
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Похудеть", callback_data="goal_lose_weight")],
                [InlineKeyboardButton(text="Набрать массу", callback_data="goal_gain_muscle")],
                [InlineKeyboardButton(text="Поддерживать", callback_data="goal_maintain")]
            ])
            msg = await message.answer("Какая у тебя цель?", reply_markup=keyboard)
            add_message_id(user_id, msg.message_id)
            await delete_old_messages(user_id, keep_last=0)
        except ValueError:
            msg = await message.answer("Пожалуйста, введи число.")
            add_message_id(user_id, msg.message_id)

# --- Обработка callback'ов анкеты ---
@dp.callback_query(lambda c: c.data in ['gender_male', 'gender_female', 'goal_lose_weight', 'goal_gain_muscle', 'goal_maintain'])
async def process_profile_callback(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    if user_id not in user_states or 'step' not in user_states[user_id]:
        await callback_query.answer("Сначала заполните анкету.")
        return

    state = user_states[user_id]
    step = state["step"]
    data = state["data"]

    if step == "gender":
        data["gender"] = "мужской" if callback_query.data == "gender_male" else "женский"
        state["step"] = "weight"
        msg = await callback_query.message.edit_text("Какой у тебя вес (в кг)?")
        add_message_id(user_id, msg.message_id)
        await delete_old_messages(user_id, keep_last=0)

    elif step == "goal":
        goals = {
            "goal_lose_weight": "Похудеть",
            "goal_gain_muscle": "Набрать массу",
            "goal_maintain": "Поддерживать"
        }
        data["goal"] = goals[callback_query.data]
        state["step"] = "training_location"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Дома", callback_data="location_home")],
            [InlineKeyboardButton(text="В спортзале", callback_data="location_gym")],
            [InlineKeyboardButton(text="На улице", callback_data="location_outdoor")]
        ])
        msg = await callback_query.message.edit_text("Где ты обычно тренируешься?", reply_markup=keyboard)
        add_message_id(user_id, msg.message_id)
        await delete_old_messages(user_id, keep_last=0)

    elif step == "training_location":
        locations = {
            "location_home": "Дома",
            "location_gym": "В спортзале",
            "location_outdoor": "На улице"
        }
        data["training_location"] = locations[callback_query.data]
        state["step"] = "level"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Новичок", callback_data="level_beginner")],
            [InlineKeyboardButton(text="Средний", callback_data="level_intermediate")],
            [InlineKeyboardButton(text="Продвинутый", callback_data="level_advanced")]
        ])
        msg = await callback_query.message.edit_text("Какой у тебя уровень подготовки?", reply_markup=keyboard)
        add_message_id(user_id, msg.message_id)
        await delete_old_messages(user_id, keep_last=0)

    elif step == "level":
        levels = {
            "level_beginner": "Новичок",
            "level_intermediate": "Средний",
            "level_advanced": "Продвинутый"
        }
        data["level"] = levels[callback_query.data]

        # Сохраняем данные в базу
        cur.execute("""
            UPDATE users SET name=?, age=?, gender=?, weight=?, height=?, goal=?, training_location=?, level=? WHERE id = ?
        """, (data["name"], data["age"], data["gender"], data["weight"], data["height"], data["goal"], data["training_location"], data["level"], user_id))
        conn.commit()

        # Удаляем пользователя из состояний
        if user_id in user_states:
            del user_states[user_id]

        # Проверяем и выдаём достижение
        profile = get_user_profile(user_id)
        check_and_award_achievements(user_id, profile)

        msg = await callback_query.message.edit_text(
            f"Анкета заполнена! Имя: {data['name']}, Возраст: {data['age']}, Пол: {data['gender']}, Вес: {data['weight']} кг, Рост: {data['height']} см, Цель: {data['goal']}, Место: {data['training_location']}, Уровень: {data['level']}. Используй /menu для навигации."
        )
        add_message_id(user_id, msg.message_id)

# --- Остальные команды (требуют подписки) ---
@dp.message(Command("training"))
async def cmd_training(message: types.Message):
    user_id = message.from_user.id
    if not is_subscribed(user_id):
        msg = await message.answer("Для доступа к тренировкам нужна подписка. /subscribe")
        add_message_id(user_id, msg.message_id)
        return

    profile = get_user_profile(user_id)
    if not profile:
        msg = await message.answer("Сначала заполните анкету /start")
        add_message_id(user_id, msg.message_id)
        return

    print("✅ Сработал обработчик /training") # Лог
    print(f"Профиль пользователя: {profile}") # Лог
    print(f"Подписка: {is_subscribed(user_id)}") # Лог

    # Определяем сложность на основе прогресса
    recent_statuses = get_recent_training_status(user_id, days=3)
    completed_count = recent_statuses.count('completed')
    if completed_count < 3:
        difficulty = "лёгкие и простые упражнения"
    else:
        difficulty = "средние или сложные упражнения"

    prompt = f"""
    Ты ИИ-тренер. Создай индивидуальную тренировку на 1 день для человека:
    - Имя: {profile['name']}
    - Пол: {profile['gender']}
    - Возраст: {profile['age']}
    - Вес: {profile['weight']} кг
    - Рост: {profile['height']} см
    - Цель: {profile['goal']}
    - Место тренировки: {profile['training_location']}
    - Уровень подготовки: {profile['level']}
    - Тренировка должна быть {difficulty} и подходящей для указанного пола и возраста.
    - Длительность: 30-45 минут.
    Верни ПЛАН ТРЕНИРОВКИ в виде нумерованного списка упражнений с описанием, количеством подходов/повторений и отдыхом между.
    """

    try:
        print("Отправляем запрос к API...") # Лог
        response = client.chat.completions.create(
            model="openchat/openchat-7b",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=3000
        )
        print("Ответ от API получен") # Лог
        training_plan = response.choices[0].message.content
        msg = await message.answer(f"<b>Ваша тренировка на сегодня:</b>\n\n{training_plan}")
        add_message_id(user_id, msg.message_id)

        # Логируем запланированную тренировку
        today = datetime.now().date().isoformat()
        save_training_log(user_id, today, 'scheduled')

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Выполнил", callback_data=f"training_completed_{today}")],
            [InlineKeyboardButton(text=" сделаю позже ", callback_data=f"training_later_{today}")]
        ])
        msg2 = await message.answer("Выполнили тренировку?", reply_markup=keyboard)
        add_message_id(user_id, msg2.message_id)

    except Exception as e:
        print(f"❌ Ошибка при генерации тренировки: {e}") # Лог
        msg = await message.answer("❌ Ошибка при создании тренировки. Попробуйте позже.")
        add_message_id(user_id, msg.message_id)

@dp.message(Command("food"))
async def cmd_food(message: types.Message):
    user_id = message.from_user.id
    if not is_subscribed(user_id):
        msg = await message.answer("Для доступа к питанию нужна подписка. /subscribe")
        add_message_id(user_id, msg.message_id)
        return

    profile = get_user_profile(user_id)
    if not profile:
        msg = await message.answer("Сначала заполните анкету /start")
        add_message_id(user_id, msg.message_id)
        return

    prompt = f"""
    Ты ИИ-тренер. Создай примерное меню на 1 день для человека:
    - Имя: {profile['name']}
    - Пол: {profile['gender']}
    - Возраст: {profile['age']}
    - Вес: {profile['weight']} кг
    - Рост: {profile['height']} см
    - Цель: {profile['goal']}
    - Диетические ограничения: нет
    - Предпочтения: здоровая, сбалансированная пища
    Верни ПЛАН ПИТАНИЯ в виде: Завтрак, Обед, Ужин, Перекусы (если нужно) с описанием блюд и примерной калорийностью.
    """

    try:
        print("Отправляем запрос к API (питание)...") # Лог
        response = client.chat.completions.create(
            model="openchat/openchat-7b",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=3000
        )
        print("Ответ от API (питание) получен") # Лог
        food_plan = response.choices[0].message.content
        msg = await message.answer(f"<b>Ваш план питания на сегодня:</b>\n\n{food_plan}")
        add_message_id(user_id, msg.message_id)
        await delete_old_messages(user_id)
    except Exception as e:
        print(f"❌ Ошибка при генерации питания: {e}") # Лог
        msg = await message.answer("❌ Ошибка при создании плана питания. Попробуйте позже.")
        add_message_id(user_id, msg.message_id)

@dp.message(Command("progress"))
async def cmd_progress(message: types.Message):
    user_id = message.from_user.id
    if not is_subscribed(user_id):
        msg = await message.answer("Для доступа к прогрессу нужна подписка. /subscribe")
        add_message_id(user_id, msg.message_id)
        return

    text = message.text.split()
    if len(text) != 2:
        msg = await message.answer("Используйте команду: /progress <вес>")
        add_message_id(user_id, msg.message_id)
        return

    try:
        weight = float(text[1].replace(',', '.'))
        if not (30 <= weight <= 300):
            raise ValueError("Неверный вес")
    except ValueError:
        msg = await message.answer("Введите корректный вес (например, 70.5).")
        add_message_id(user_id, msg.message_id)
        return

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("INSERT INTO progress (user_id, weight, date) VALUES (?, ?, ?)", (user_id, weight, now))
    conn.commit()

    # Проверяем достижения (например, "Вес в норме")
    profile = get_user_profile(user_id)
    if profile:
        height_m = profile['height'] / 100
        bmi = weight / (height_m ** 2)
        if 18.5 <= bmi <= 24.9:
            cur.execute("SELECT id FROM achievements WHERE user_id = ? AND title = ?", (user_id, "Золотая середина"))
            if cur.fetchone() is None:
                add_achievement(user_id, "Золотая середина", "BMI в норме!")

    msg = await message.answer(f"Вес {weight} кг записан. Дата: {now.split()[0]}")
    add_message_id(user_id, msg.message_id)

@dp.message(Command("profile"))
async def cmd_profile(message: types.Message):
    user_id = message.from_user.id
    if not is_subscribed(user_id):
        msg = await message.answer("Для доступа к профилю нужна подписка. /subscribe")
        add_message_id(user_id, msg.message_id)
        return

    profile = get_user_profile(user_id)
    if not profile:
        msg = await message.answer("Сначала заполните анкету /start")
        add_message_id(user_id, msg.message_id)
        return

    sub_end = None
    cur.execute("SELECT subscription_end_date FROM users WHERE id = ?", (user_id,))
    sub_result = cur.fetchone()
    if sub_result and sub_result[0]:
        sub_end = datetime.fromisoformat(sub_result[0])

    days_left = get_trial_days_left(user_id)
    sub_info = ""
    if sub_end:
        sub_info = f"\nПодписка до: {sub_end.strftime('%d.%m.%Y')}"
    if days_left > 0:
        sub_info += f"\n(из них {days_left} дней — пробная неделя)"

    msg = await message.answer(f"<b>Профиль:</b>\n"
                         f"Имя: {profile['name']}\n"
                         f"Возраст: {profile['age']}\n"
                         f"Пол: {profile['gender']}\n"
                         f"Вес: {profile['weight']} кг\n"
                         f"Рост: {profile['height']} см\n"
                         f"Цель: {profile['goal']}\n"
                         f"Место тренировки: {profile['training_location']}\n"
                         f"Уровень: {profile['level']}{sub_info}")
    add_message_id(user_id, msg.message_id)

@dp.message(Command("achievements"))
async def cmd_achievements(message: types.Message):
    user_id = message.from_user.id
    if not is_subscribed(user_id):
        msg = await message.answer("Для доступа к достижениям нужна подписка. /subscribe")
        add_message_id(user_id, msg.message_id)
        return

    cur.execute("SELECT title, description, unlocked_date FROM achievements WHERE user_id = ?", (user_id,))
    achs = cur.fetchall()

    if not achs:
        msg = await message.answer("У вас пока нет достижений. Продолжайте тренироваться!")
        add_message_id(user_id, msg.message_id)
        return

    ach_text = "<b>Ваши достижения:</b>\n\n"
    for title, desc, date in achs:
        ach_text += f"🏆 <b>{title}</b>\n<i>{desc}</i>\nДата: {date[:10]}\n\n"

    msg = await message.answer(ach_text)
    add_message_id(user_id, msg.message_id)

# --- Чат с ИИ ---
@dp.message(Command("chat"))
async def cmd_chat(message: types.Message):
    user_id = message.from_user.id
    if not is_subscribed(user_id):
        msg = await message.answer("Для доступа к чату с ИИ нужна подписка. /subscribe")
        add_message_id(user_id, msg.message_id)
        return

    profile = get_user_profile(user_id)
    if not profile:
        msg = await message.answer("Сначала заполните анкету /start")
        add_message_id(user_id, msg.message_id)
        return

    prompt = f"Ты ИИ-тренер. Пользователь задаёт вопрос. Учитывай его данные: Имя: {profile['name']}, Пол: {profile['gender']}, Возраст: {profile['age']}, Вес: {profile['weight']} кг, Рост: {profile['height']} см, Цель: {profile['goal']}, Место: {profile['training_location']}, Уровень: {profile['level']}. Ответь на вопрос: {message.text[6:]}" # [6:] убирает "/chat "

    try:
        response = client.chat.completions.create(
            model="openchat/openchat-7b",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500
        )
        ai_response = response.choices[0].message.content
        msg = await message.answer(ai_response)
        add_message_id(user_id, msg.message_id)
    except Exception as e:
        logger.error(f"Ошибка при ответе ИИ: {e}")
        msg = await message.answer("Ошибка при ответе ИИ. Попробуйте позже.")
        add_message_id(user_id, msg.message_id)

# --- Анализ рациона ---
@dp.message(Command("analyze_food"))
async def cmd_analyze_food(message: types.Message):
    user_id = message.from_user.id
    if not is_subscribed(user_id):
        msg = await message.answer("Для доступа к анализу питания нужна подписка. /subscribe")
        add_message_id(user_id, msg.message_id)
        return

    food_description = message.text[14:] # Убираем "/analyze_food "
    if not food_description.strip():
        msg = await message.answer("Используйте команду: /analyze_food <описание еды>")
        add_message_id(user_id, msg.message_id)
        return

    profile = get_user_profile(user_id)
    prompt = f"""
    Ты ИИ-тренер. Проанализируй питание, описанное пользователем: "{food_description}".
    Учитывай его данные: Имя: {profile['name']}, Пол: {profile['gender']}, Возраст: {profile['age']}, Вес: {profile['weight']} кг, Рост: {profile['height']} см, Цель: {profile['goal']}.
    Оцени полезность, сбалансированность, калорийность (приблизительно). Дай рекомендации.
    """

    try:
        response = client.chat.completions.create(
            model="openchat/openchat-7b",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500
        )
        analysis = response.choices[0].message.content
        msg = await message.answer(f"<b>Анализ вашего рациона:</b>\n\n{analysis}")
        add_message_id(user_id, msg.message_id)
    except Exception as e:
        logger.error(f"Ошибка при анализе рациона: {e}")
        msg = await message.answer("Ошибка при анализе рациона. Попробуйте позже.")
        add_message_id(user_id, msg.message_id)

# --- График прогресса ---
@dp.message(Command("graph"))
async def cmd_graph(message: types.Message):
    user_id = message.from_user.id
    if not is_subscribed(user_id):
        msg = await message.answer("Для доступа к графику нужна подписка. /subscribe")
        add_message_id(user_id, msg.message_id)
        return

    cur.execute("SELECT date, weight FROM progress WHERE user_id = ? ORDER BY date ASC", (user_id,))
    data = cur.fetchall()

    if not data:
        msg = await message.answer("Нет данных для построения графика. Добавьте вес с помощью /progress.")
        add_message_id(user_id, msg.message_id)
        return

    dates = [datetime.fromisoformat(d[0]) for d in data]
    weights = [d[1] for d in data]

    plt.figure(figsize=(10, 5))
    plt.plot(dates, weights, marker='o')
    plt.title(f'График веса пользователя {message.from_user.first_name}')
    plt.xlabel('Дата')
    plt.ylabel('Вес (кг)')
    plt.grid(True)
    plt.xticks(rotation=45)
    plt.tight_layout()

    img_buffer = io.BytesIO()
    plt.savefig(img_buffer, format='png')
    img_buffer.seek(0)
    plt.close() # ВАЖНО: закрываем фигуру, чтобы освободить память

    input_file = BufferedInputFile(img_buffer.getvalue(), filename='progress_graph.png')
    msg = await message.answer_document(input_file)
    add_message_id(user_id, msg.message_id)

# --- Напоминания ---
@dp.message(Command("set_reminder_time"))
async def cmd_set_reminder_time(message: types.Message):
    user_id = message.from_user.id
    if not is_subscribed(user_id):
        msg = await message.answer("Для настройки напоминаний нужна подписка. /subscribe")
        add_message_id(user_id, msg.message_id)
        return

    text = message.text.split()
    if len(text) != 2:
        msg = await message.answer("Используйте команду: /set_reminder_time HH:MM (например, 19:00)")
        add_message_id(user_id, msg.message_id)
        return

    time_str = text[1]
    try:
        # Проверяем формат времени
        datetime.strptime(time_str, "%H:%M")
        reminder_times[user_id] = time_str
        # Перезапускаем задачу для этого пользователя
        try:
            scheduler.remove_job(job_id=f"remind_{user_id}", jobstore='default')
        except:
            pass
        hour, minute = map(int, time_str.split(':'))
        scheduler.add_job(remind_workout, "cron", hour=hour, minute=minute, id=f"remind_{user_id}", args=[user_id])
        msg = await message.answer(f"Время напоминания установлено на {time_str}.")
        add_message_id(user_id, msg.message_id)
    except ValueError:
        msg = await message.answer("Неверный формат времени. Используйте HH:MM (например, 19:00).")
        add_message_id(user_id, msg.message_id)

async def remind_workout(user_id):
    try:
        await bot.send_message(user_id, "Время тренировки! 💪 Не забудь про /training и /food сегодня!")
    except Exception as e:
        logger.error(f"Ошибка при отправке напоминания пользователю {user_id}: {e}")

# --- Callback для тренировки ---
@dp.callback_query(lambda c: c.data.startswith('training_completed_'))
async def training_completed_callback(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    date_str = callback_query.data.split('_')[-1] # Получаем дату из callback_data

    # Обновляем статус тренировки на 'completed'
    # Используем UPDATE с WHERE для конкретной даты и пользователя
    cur.execute("UPDATE training_logs SET status = 'completed' WHERE user_id = ? AND date = ?", (user_id, date_str))
    conn.commit()

    await callback_query.message.edit_text(f"✅ Отлично! Тренировка на {date_str} засчитана.")
    await callback_query.answer()

@dp.callback_query(lambda c: c.data.startswith('training_later_'))
async def training_later_callback(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    date_str = callback_query.data.split('_')[-1]

    # Обновляем статус тренировки на 'skipped'
    cur.execute("UPDATE training_logs SET status = 'skipped' WHERE user_id = ? AND date = ?", (user_id, date_str))
    conn.commit()

    await callback_query.message.edit_text(f"Понял, тренировку на {date_str} можно сделать позже.")
    await callback_query.answer()

# --- Обработчик всех остальных сообщений (для анкеты и пропуска команд) ---
@dp.message()
async def handle_message(message: types.Message):
    user_id = message.from_user.id
    print(f"Получено сообщение от {user_id}: {message.text}") # Лог

    # Проверяем, является ли сообщение командой
    if message.text and message.text.startswith('/'):
        print(f"Команда '{message.text}' — пропускаем, пусть обработчик команд сработает") # Лог
        # НЕ вызываем await, просто выходим — пусть другие хендлеры обработают команду
        return

    # Если пользователь в анкете, обрабатываем анкету
    if user_id in user_states and 'step' in user_states[user_id]:
        # Обработка уже идёт в process_profile
        pass # Это место не должно сработать, если process_profile привязан к сообщениям
    else:
        # Если не в анкете и не команда, можно игнорировать или отправить приветствие
        pass


# --- Основная функция запуска ---
async def main():
    global loop # <-- Указываем, что будем использовать глобальную переменную
    loop = asyncio.get_running_loop() # <-- Сохраняем текущий цикл

    # --- Планировщик ---
    scheduler.start()
    logger.info("⏰ Планировщик запущен")

    # --- Установка вебхука ---
    try:
        await bot.set_webhook(WEBHOOK_URL)
        logger.info(f"📡 Вебхук установлен на {WEBHOOK_URL}")
    except Exception as e:
        logger.error(f"❌ Ошибка при установке вебхука: {e}")
        return # Выйти, если не удалось установить вебхук

    # --- Запуск Flask в отдельном потоке ---
    def run_flask():
        # Используем waitress для более надежного запуска в продакшене
        from waitress import serve
        logger.info("🌐 Flask (Waitress) запускается на 0.0.0.0:8000...")
        serve(app, host='0.0.0.0', port=8000)

    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True # Поток завершится, когда основной процесс завершится
    flask_thread.start()
    logger.info("🧵 Поток Flask запущен")

    logger.info("🤖 Бот запущен и ожидает сообщений...")

    # --- Бесконечный цикл для удержания основного процесса ---
    # Это необходимо, чтобы скрипт не завершался и asyncio продолжал работать
    try:
        while True:
            await asyncio.sleep(1) # Уступаем контроль, чтобы другие задачи могли работать
    except KeyboardInterrupt:
        logger.info("🛑 Бот остановлен пользователем")

if __name__ == "__main__":
    # Убедимся, что waitress установлен перед запуском
    try:
        import waitress
    except ImportError:
        logger.critical("❌ Модуль 'waitress' не найден. Установите его: pip install waitress")
        exit(1)

    try:
        asyncio.run(main())
    except Exception as e:
        logger.critical(f"💥 Критическая ошибка в основном цикле: {e}", exc_info=True)

# --- Flask приложение для вебхука ---
# Убедись, что это находится в том же файле, что и async def main() выше
app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():
    # Получаем JSON-данные из тела запроса
    json_data = request.get_json()
    if not json_data:
        logger.warning("Получен пустой webhook запрос")
        return 'Bad Request: No JSON data', 400

    try:
        # Преобразуем JSON в объект Update
        update = types.Update.model_validate(json_data)
        # НЕПОСРЕДСТВЕННО обрабатываем обновление в этом синхронном потоке
        # Это работает, потому что `Dispatcher` может обрабатывать обновления синхронно
        # в некоторых случаях, или вы можете использовать `asyncio.run()` внутри,
        # но проще и надежнее запустить обработку в главном asyncio-цикле.
        # Однако, для простоты и совместимости, мы можем просто запланировать его.
        # asyncio.create_task() не работает напрямую в синхронной функции Flask,
        # но мы можем использовать loop.call_soon_threadsafe
        
        # Планируем выполнение в главном цикле asyncio
        asyncio.run_coroutine_threadsafe(dp.feed_update(bot, update), loop)
        logger.debug(f"📥 Обновление {update.update_id} поставлено в очередь")
    except Exception as e:
        logger.error(f"❌ Ошибка обработки webhook: {e}", exc_info=True)
        # Не возвращаем 500, чтобы Telegram не считал это критической ошибкой
        # и не прекращал отправлять обновления
        return 'Internal Server Error', 500 

    # Возвращаем 200 OK, чтобы Telegram знал, что обновление получено
    return 'OK', 200

