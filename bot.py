import json
import sqlite3
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile, LabeledPrice
from openai import OpenAI
import asyncio
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import matplotlib.pyplot as plt
import io
import hashlib
from urllib.parse import urlencode
from flask import Flask, request, render_template, redirect, url_for, session
import threading
import os
import logging
import traceback
import re # <-- Для валидации времени в /set_reminder_time

# --- Импортируем конфигурацию ---
try:
    from config import API_TOKEN, OPENROUTER_API_KEY, YOOMONEY_SHOP_ID, YOOMONEY_SECRET_KEY, WEBHOOK_URL, ADMIN_PASSWORD, ADMIN_IDS
except ImportError:
    print("❌ Файл config.py не найден или не содержит всех необходимых переменных.")
    exit(1)

# --- Настройка логирования ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Инициализация ---
bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# --- OpenAI клиент ---
# Исправлено: убран пробел в конце base_url
client = OpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url='https://openrouter.ai/api/v1/'  # <-- Исправлено
)

# НОВОЕ: Модель Qwen
MODEL = "qwen/qwen2.5-vl-72b-instruct:free" # <-- Изменено

# --- Подключение к SQLite ---
conn = sqlite3.connect('trainer_bot.db', check_same_thread=False)
cur = conn.cursor()

# --- Создание таблиц ---
cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    user_id INTEGER UNIQUE,
    name TEXT,
    age INTEGER,
    gender TEXT,
    height INTEGER,
    weight REAL,
    goal TEXT,
    training_location TEXT,
    level TEXT,
    last_training_date TIMESTAMP,
    next_training_date TIMESTAMP,
    reminder_time TEXT DEFAULT '08:00',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    trial_granted BOOLEAN DEFAULT 0 -- Новое поле для отслеживания выдачи пробника
);
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS weights (
    id INTEGER PRIMARY KEY,
    user_id INTEGER,
    weight REAL,
    date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users (user_id)
);
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS trainings (
    id INTEGER PRIMARY KEY,
    user_id INTEGER,
    content TEXT,
    date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status TEXT DEFAULT 'pending',  -- 'pending', 'completed', 'missed'
    FOREIGN KEY (user_id) REFERENCES users (user_id)
);
""")

# --- Новые таблицы ---
cur.execute("""
CREATE TABLE IF NOT EXISTS progress (
    id INTEGER PRIMARY KEY,
    user_id INTEGER,
    date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    weight REAL,
    chest REAL,
    waist REAL,
    hips REAL,
    arms REAL,
    shoulders REAL,
    thighs REAL,
    calves REAL,
    squat REAL,
    bench REAL,
    deadlift REAL,
    FOREIGN KEY (user_id) REFERENCES users (user_id)
);
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS achievements (
    id INTEGER PRIMARY KEY,
    user_id INTEGER,
    name TEXT,
    date_achieved TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users (user_id)
);
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS training_schedule (
    id INTEGER PRIMARY KEY,
    user_id INTEGER UNIQUE,
    schedule TEXT, -- JSON строка: {"days_per_week": 3, "days": ["Mon", "Wed", "Fri"]}
    FOREIGN KEY (user_id) REFERENCES users (user_id)
);
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY,
    user_id INTEGER UNIQUE,
    expires_at TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users (user_id)
);
""")
conn.commit()

# --- Глобальные переменные ---
user_states = {}  # {user_id: {"step": "name", "data": {...}}}
scheduler = AsyncIOScheduler()
reminder_times = {} # {user_id: time_str}
loop = None # <-- Глобальная переменная для asyncio цикла

# --- Вспомогательные функции ---
def save_user_profile(user_id, profile):
    cur.execute("""
        INSERT OR REPLACE INTO users (user_id, name, age, gender, height, weight, goal, training_location, level)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (user_id, profile['name'], profile['age'], profile['gender'], profile['height'], profile['weight'], profile['goal'], profile.get('training_location', ''), profile.get('level', '')))
    conn.commit()

def save_weight(user_id, weight):
    """Сохраняет вес пользователя в таблицу weights."""
    cur.execute("INSERT INTO weights (user_id, weight) VALUES (?, ?)", (user_id, weight))
    conn.commit()

def get_weights(user_id):
    cur.execute("SELECT weight, date FROM weights WHERE user_id = ? ORDER BY date", (user_id,))
    return cur.fetchall()

def get_user_profile(user_id):
    cur.execute("SELECT name, age, gender, height, weight, goal, training_location, level, next_training_date, reminder_time FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if row:
        return {
            "name": row[0],
            "age": row[1],
            "gender": row[2],
            "height": row[3],
            "weight": row[4],
            "goal": row[5],
            "training_location": row[6],
            "level": row[7],
            "next_training_date": row[8],
            "reminder_time": row[9]
        }
    return None

def is_subscribed(user_id):
    cur.execute("SELECT expires_at FROM subscriptions WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if row:
        expires_at = datetime.fromisoformat(row[0])
        return datetime.now() < expires_at
    return False

def add_subscription(user_id, months=1):
    expires_at = datetime.now() + timedelta(days=30 * months)
    cur.execute("""
        INSERT OR REPLACE INTO subscriptions (user_id, expires_at)
        VALUES (?, ?)
    """, (user_id, expires_at.isoformat()))
    conn.commit()

# НОВОЕ: Функция для выдачи подписки (пробный период или оплата)
def grant_subscription(user_id, days=7):
    expires_at = datetime.now() + timedelta(days=days)
    cur.execute("""
        INSERT OR REPLACE INTO subscriptions (user_id, expires_at)
        VALUES (?, ?)
    """, (user_id, expires_at.isoformat()))
    conn.commit()
    # Помечаем, что пробный период был выдан
    cur.execute("UPDATE users SET trial_granted = 1 WHERE user_id = ?", (user_id,))
    conn.commit()

def add_message_id(user_id, msg_id):
    if user_id not in user_states:
        user_states[user_id] = {"messages": []}
    user_states[user_id]["messages"].append(msg_id)

async def delete_old_messages(user_id, keep_last=3):
    if user_id in user_states and "messages" in user_states[user_id]:
        messages = user_states[user_id]["messages"]
        if len(messages) > keep_last:
            to_delete = messages[:-keep_last]
            user_states[user_id]["messages"] = messages[-keep_last:]
            for msg_id in to_delete:
                try:
                    await bot.delete_message(chat_id=user_id, message_id=msg_id)
                except Exception:
                    pass  # Сообщение уже удалено или не может быть удалено

# --- Функция проверки достижений ---
def check_achievements(user_id):
    # "Первая тренировка"
    cur.execute("SELECT COUNT(*) FROM trainings WHERE user_id = ? AND status = 'completed'", (user_id,))
    completed_count = cur.fetchone()[0]
    if completed_count == 1:
        cur.execute("INSERT OR IGNORE INTO achievements (user_id, name) VALUES (?, ?)", (user_id, "Первая тренировка"))
        conn.commit()

    # "Неделя без пропусков"
    now = datetime.now()
    week_ago = now - timedelta(days=7)
    cur.execute("""
        SELECT COUNT(*) FROM trainings
        WHERE user_id = ? AND status = 'completed' AND date >= ?
    """, (user_id, week_ago.isoformat()))
    week_completed = cur.fetchone()[0]
    if week_completed >= 7:
        cur.execute("INSERT OR IGNORE INTO achievements (user_id, name) VALUES (?, ?)", (user_id, "Неделя без пропусков"))
        conn.commit()

    # "Похудел на 5 кг"
    cur.execute("""
        SELECT weight FROM weights WHERE user_id = ? ORDER BY date ASC LIMIT 1
    """, (user_id,))
    first_weight_row = cur.fetchone()
    if first_weight_row:
        first_weight = first_weight_row[0]
        cur.execute("""
            SELECT weight FROM weights WHERE user_id = ? ORDER BY date DESC LIMIT 1
        """, (user_id,))
        latest_weight_row = cur.fetchone()
        if latest_weight_row:
            latest_weight = latest_weight_row[0]
            if first_weight - latest_weight >= 5:
                cur.execute("INSERT OR IGNORE INTO achievements (user_id, name) VALUES (?, ?)", (user_id, "Похудел на 5 кг"))
                conn.commit()

# --- Все команды должны быть до @dp.message() ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    # Сбрасываем состояние, если пользователь начал заново
    user_states[user_id] = {"step": "name", "data": {}, "messages": []}
    # Удаляем старые сообщения
    await delete_old_messages(user_id, keep_last=0)
    msg = await message.answer("Привет! Я твой персональный тренер 💪\n\nКак тебя зовут?")
    add_message_id(user_id, msg.message_id)

@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message):
    user_id = message.from_user.id
    if user_id in user_states:
        del user_states[user_id]
        msg = await message.answer("Анкета отменена. Используй /start, чтобы начать заново.")
        add_message_id(user_id, msg.message_id)
    else:
        msg = await message.answer("Нет активной анкеты.")
        add_message_id(user_id, msg.message_id)

# --- YooKassa оплата ---
@dp.message(Command("subscribe"))
async def cmd_subscribe(message: types.Message):
    user_id = message.from_user.id

    # Примеры цен (в копейках: 149.00 руб = 14900 коп)
    prices = [
        LabeledPrice(label='1 месяц', amount=14900),
        # LabeledPrice(label='Скидка', amount=-14900), # Пример скидки, убери, если не нужно
        # Добавь другие позиции при необходимости
    ]

    # Отправляем счёт
    await bot.send_invoice(
        user_id=user_id,
        title="Подписка на 1 месяц",
        description="Доступ к тренировкам и питанию на 30 дней",
        payload="subscribe_1_month", # Уникальный идентификатор заказа
        provider_token=YOOKASSA_PROVIDER_TOKEN, # Токен от YooKassa
        currency="RUB",
        prices=prices,
        start_parameter="subscribe", # Необязательно, для deep-linking
        # photo_url="https://example.com/subscription_image.jpg", # Опционально
        # photo_size=64,
        # photo_width=800,
        # photo_height=450,
        # need_email=True, # Опционально
        # send_email_to_provider=True, # Опционально
        is_flexible=False # True, если нужно рассчитать доставку (не используется для подписки)
    )

    # Сообщение с офертом можно отправить *после* или *вместо* счёта
    oferta_url = "https://docs.google.com/document/d/14NrOTKOJ2Dcd5-guVZGU7fRj9gj-wS1X/edit?usp=drive_link&ouid=111319375229341079989&rtpof=true&sd=true"
    msg = await message.answer(f"При оплате вы соглашаетесь с условиями публичной оферты: {oferta_url}")
    add_message_id(user_id, msg.message_id)

# --- Обработчики оплаты через Telegram Payments (работает с YooKassa) ---
@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: types.PreCheckoutQuery):
    # Всегда отвечаем OK, если не нужно проверять адрес/доставку
    await pre_checkout_query.answer(ok=True)

@dp.message(lambda m: m.content_type == 'successful_payment')
async def process_successful_payment(message: types.Message):
    user_id = message.from_user.id
    # Оформляем подписку на 1 месяц (или сколько нужно)
    add_subscription(user_id, 1)
    msg = await message.answer("✅ Спасибо за покупку! Подписка активна 1 месяц.")
    add_message_id(user_id, msg.message_id)

@dp.message(Command("training"))
async def send_training(message: types.Message):
    logger.info(f"Получена команда /training от {message.from_user.id}")
    user_id = message.from_user.id
    user = get_user_profile(user_id)
    if not user:
        msg = await message.answer("Сначала пройди анкету: /start")
        add_message_id(user_id, msg.message_id)
        return

    if not is_subscribed(user_id):
        msg = await message.answer("🔒 Эта функция доступна только по подписке. Используй /subscribe, чтобы оформить.")
        add_message_id(user_id, msg.message_id)
        return

    # --- Адаптивные тренировки ---
    # Берём историю тренировок
    cur.execute("""
        SELECT status FROM trainings
        WHERE user_id = ? ORDER BY date DESC LIMIT 5
    """, (user_id,))
    recent_trainings = cur.fetchall()
    recent_statuses = [t[0] for t in recent_trainings]

    # Определим сложность
    completed_count = recent_statuses.count('completed')
    if completed_count < 3:
        difficulty = "лёгкие и простые упражнения"
    else:
        difficulty = "средние или сложные упражнения"

    try:
        completion = client.chat.completions.create(
            model=MODEL, # <-- Используем новую модель
            messages=[
                {"role": "system", "content": f"""
Ты — персональный фитнес-тренер. Составь **индивидуальную тренировку на один день** для пользователя:

- Имя: {user['name']}
- Пол: {user['gender']}
- Возраст: {user['age']} лет
- Рост: {user['height']} см
- Вес: {user['weight']} кг
- Цель: {user['goal']}
- Место тренировки: {user['training_location'] or 'не указано'}
- Уровень: {user['level'] or 'не указан'}
- Сложность: {difficulty}

Тренировка должна быть **безопасной**, **эффективной**, **сбалансированной** и **подходящей для указанного пола и возраста**.

Формат ответа:
- Упражнение: [название]
- Подходы: [число]
- Повторы: [число]
- Вес: [рекомендуемый вес в кг, если нужно]
- Примечание: [если нужно]

Пиши на **русском языке**.
"""},  # Новый промт
                {"role": "user", "content": "Создай тренировку."}
            ],
            max_tokens=3000,  # Увеличено
            temperature=0.7
        )
        training = completion.choices[0].message.content

        # Сохраняем тренировку в базу
        cur.execute("INSERT INTO trainings (user_id, content) VALUES (?, ?)", (user_id, training))
        conn.commit()

        # Отправляем тренировку с кнопками
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Выполнил", callback_data="training_completed")],
            [InlineKeyboardButton(text=" сделаю позже", callback_data="training_postpone")]
        ])
        msg = await message.answer(f"Твоя тренировка на сегодня:\n\n{training}", reply_markup=keyboard)
        add_message_id(user_id, msg.message_id)
        await delete_old_messages(user_id)

        # Обновляем дату следующей тренировки
        next_date = datetime.now() + timedelta(days=2)
        cur.execute("UPDATE users SET next_training_date = ? WHERE user_id = ?", (next_date.isoformat(), user_id))
        conn.commit()

    except Exception as e:
        logger.error(f"Ошибка при генерации тренировки: {e}")
        msg = await message.answer(f"❌ Ошибка при генерации тренировки. Попробуй позже.")
        add_message_id(user_id, msg.message_id)

@dp.message(Command("food"))
async def send_food(message: types.Message):
    logger.info(f"Получена команда /food от {message.from_user.id}")
    user_id = message.from_user.id
    user = get_user_profile(user_id)
    if not user:
        msg = await message.answer("Сначала пройди анкету: /start")
        add_message_id(user_id, msg.message_id)
        return

    if not is_subscribed(user_id):
        msg = await message.answer("🔒 Эта функция доступна только по подписке. Используй /subscribe, чтобы оформить.")
        add_message_id(user_id, msg.message_id)
        return

    try:
        completion = client.chat.completions.create(
            model=MODEL, # <-- Используем новую модель
            messages=[
                {"role": "system", "content": f"""
Ты — персональный диетолог. Составь **индивидуальное меню на один день** для пользователя:

- Имя: {user['name']}
- Пол: {user['gender']}
- Возраст: {user['age']} лет
- Рост: {user['height']} см
- Вес: {user['weight']} кг
- Цель: {user['goal']}
- Место тренировки: {user['training_location'] or 'не указано'}
- Уровень: {user['level'] or 'не указан'}

Меню должно быть:
- Сбалансированным
- Подходящим для достижения цели
- Безопасным
- Подходящим по возрасту и полу

Формат ответа:
- Завтрак: [описание]
- Перекус (если нужно): [описание]
- Обед: [описание]
- Перекус (если нужно): [описание]
- Ужин: [описание]
- Полезные напитки: [если нужно]

Пиши на **русском языке**.
"""},  # Новый промт
                {"role": "user", "content": "Создай питание."}
            ],
            max_tokens=3000,  # Увеличено
            temperature=0.7
        )
        food = completion.choices[0].message.content
        msg = await message.answer(f"Твоё питание на сегодня:\n\n{food}")
        add_message_id(user_id, msg.message_id)
        await delete_old_messages(user_id)
    except Exception as e:
        logger.error(f"Ошибка при генерации питания: {e}")
        msg = await message.answer(f"❌ Ошибка при генерации питания. Попробуй позже.")
        add_message_id(user_id, msg.message_id)

# --- ИЗМЕНЕНО: Подсказка при вводе ---
@dp.message(Command("weight"))
async def cmd_weight(message: types.Message):
    user_id = message.from_user.id
    args = message.text.split()
    if len(args) != 2:
        # Отправляем подсказку, если команда введена без аргумента
        if len(args) == 1:
            msg = await message.answer("Пожалуйста, введите вес в формате: /weight 70.5")
        else:
            msg = await message.answer("Введите команду в формате: /weight 70.5")
        add_message_id(user_id, msg.message_id)
        return
    try:
        weight = float(args[1])
        save_weight(user_id, weight)
        msg = await message.answer(f"Вес {weight} кг сохранён.")
        add_message_id(user_id, msg.message_id)
    except ValueError:
        msg = await message.answer("Введите корректное число.")
        add_message_id(user_id, msg.message_id)

# --- Новые команды ---

# --- ИЗМЕНЕНО: Подсказка при вводе ---
@dp.message(Command("progress"))
async def cmd_progress(message: types.Message):
    user_id = message.from_user.id
    args = message.text.split()
    if len(args) < 2:
        # Отправляем подсказку, если команда введена без аргумента
        if len(args) == 1:
            msg = await message.answer("Пожалуйста, введите вес в формате: /progress 70.5")
        else:
            msg = await message.answer("Введите команду в формате:\n/progress 70.5 (вес в кг)")
        add_message_id(user_id, msg.message_id)
        return

    try:
        weight = float(args[1])
        save_weight(user_id, weight)

        # Сохраняем в progress
        cur.execute("""
            INSERT INTO progress (user_id, weight) VALUES (?, ?)
        """, (user_id, weight))
        conn.commit()

        msg = await message.answer(f"✅ Вес {weight} кг сохранён в прогресс.")
        add_message_id(user_id, msg.message_id)

        # Проверим достижения
        check_achievements(user_id)

    except ValueError:
        msg = await message.answer("Введите корректное число.")
        add_message_id(user_id, msg.message_id)

@dp.message(Command("schedule"))
async def cmd_schedule(message: types.Message):
    user_id = message.from_user.id
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="3 раза в неделю", callback_data="schedule_3")],
        [InlineKeyboardButton(text="4 раза в неделю", callback_data="schedule_4")],
        [InlineKeyboardButton(text="5 раза в неделю", callback_data="schedule_5")]
    ])
    msg = await message.answer("Сколько раз в неделю хочешь тренироваться?", reply_markup=keyboard)
    add_message_id(user_id, msg.message_id)

@dp.message(Command("report"))
async def cmd_report(message: types.Message):
    user_id = message.from_user.id
    # Пример: недельный отчёт
    now = datetime.now()
    week_ago = now - timedelta(days=7)

    # Сколько тренировок выполнено за неделю
    cur.execute("""
        SELECT COUNT(*) FROM trainings
        WHERE user_id = ? AND status = 'completed' AND date >= ?
    """, (user_id, week_ago.isoformat()))
    completed_count = cur.fetchone()[0]

    # Сколько тренировок просрочено
    cur.execute("""
        SELECT COUNT(*) FROM trainings
        WHERE user_id = ? AND status = 'missed' AND date >= ?
    """, (user_id, week_ago.isoformat()))
    missed_count = cur.fetchone()[0]

    report = f"""
📊 Недельный отчёт (последние 7 дней):
- Выполнено тренировок: {completed_count}
- Пропущено тренировок: {missed_count}
    """

    msg = await message.answer(report)
    add_message_id(user_id, msg.message_id)

@dp.message(Command("achievements"))
async def cmd_achievements(message: types.Message):
    user_id = message.from_user.id
    cur.execute("SELECT name, date_achieved FROM achievements WHERE user_id = ?", (user_id,))
    rows = cur.fetchall()

    if not rows:
        msg = await message.answer("У тебя пока нет достижений.")
    else:
        ach_list = "\n".join([f"🏆 {name} — {date.split()[0]}" for name, date in rows])
        msg = await message.answer(f"Твои достижения:\n\n{ach_list}")

    add_message_id(user_id, msg.message_id)

# --- ИЗМЕНЕНО: Формат дат ---
@dp.message(Command("profile"))
async def show_profile(message: types.Message):
    logger.info(f"Получена команда /profile от {message.from_user.id}")
    user_id = message.from_user.id
    user = get_user_profile(user_id)
    if not user:
        msg = await message.answer("Сначала пройди анкету: /start")
        add_message_id(user_id, msg.message_id)
        return

    # Форматируем дату окончания подписки
    sub_status = "Подписка не оформлена"
    cur.execute("SELECT expires_at FROM subscriptions WHERE user_id = ?", (user_id,))
    sub_row = cur.fetchone()
    if sub_row:
        try:
            expires_at_dt = datetime.fromisoformat(sub_row[0])
            sub_status = f"Подписка активна до: {expires_at_dt.strftime('%d.%m.%Y')}"
        except ValueError:
            sub_status = f"Подписка активна до: {sub_row[0]}"

    weights = get_weights(user_id)
    weights_str = "\n".join([f"{w[1].split()[0]}: {w[0]} кг" for w in weights[-5:]])

    # Получаем график
    cur.execute("SELECT schedule FROM training_schedule WHERE user_id = ?", (user_id,))
    sched_row = cur.fetchone()
    schedule_info = sched_row[0] if sched_row else "не настроен"

    # Получаем достижения
    cur.execute("SELECT name FROM achievements WHERE user_id = ?", (user_id,))
    ach_rows = cur.fetchall()
    achievements_list = ", ".join([a[0] for a in ach_rows]) if ach_rows else "нет"

    # --- ИЗМЕНЕНО: Формат даты следующей тренировки ---
    next_training_date_str = user['next_training_date']
    if next_training_date_str:
        try:
            next_dt = datetime.fromisoformat(next_training_date_str)
            formatted_next_date = next_dt.strftime('%d.%m.%Y')
        except ValueError:
            formatted_next_date = next_training_date_str # Если формат не распознан
    else:
        formatted_next_date = 'не указана'

    profile = (
        f"Имя: {user['name']}\n"
        f"Возраст: {user['age']}\n"
        f"Пол: {user['gender']}\n"
        f"Рост: {user['height']} см\n"
        f"Вес: {user['weight']} кг\n"
        f"Цель: {user['goal']}\n"
        f"Место тренировки: {user['training_location'] or 'не указано'}\n"
        f"Уровень: {user['level'] or 'не указан'}\n"
        f"Дата следующей тренировки: {formatted_next_date}\n" # <-- Используем формат
        f"Время напоминаний: {user['reminder_time']}\n"
        f"График тренировок: {schedule_info}\n"
        f"Достижения: {achievements_list}\n"
        f"Статус подписки: {sub_status}\n" # <-- Используем формат
        f"История веса:\n{weights_str if weights else 'Нет данных'}"
    )
    msg = await message.answer(profile)
    add_message_id(user_id, msg.message_id)

@dp.message(Command("weight_graph"))
async def send_weight_graph(message: types.Message):
    user_id = message.from_user.id
    weights = get_weights(user_id)

    if not weights:
        msg = await message.answer("Нет данных о весе.")
        add_message_id(user_id, msg.message_id)
        return

    dates = [w[1].split()[0] for w in weights]
    values = [w[0] for w in weights]

    plt.figure(figsize=(10, 5))
    plt.plot(dates, values, marker='o')
    plt.title("График изменения веса")
    plt.xlabel("Дата")
    plt.ylabel("Вес (кг)")
    plt.xticks(rotation=45)
    plt.tight_layout()

    img = io.BytesIO()
    plt.savefig(img, format='png')
    img.seek(0)
    plt.close()

    # Оборачиваем BytesIO в BufferedInputFile
    photo = BufferedInputFile(img.read(), filename='weight_graph.png')
    msg = await message.answer_photo(photo=photo)
    add_message_id(user_id, msg.message_id)

# --- ИЗМЕНЕНО: Подсказка и валидация ---
@dp.message(Command("set_reminder_time"))
async def cmd_set_reminder_time(message: types.Message):
    user_id = message.from_user.id
    args = message.text.split()
    if len(args) != 2:
        # Отправляем подсказку, если команда введена без аргумента
        if len(args) == 1:
            msg = await message.answer("Пожалуйста, введите время в формате: /set_reminder_time 19:00")
        else:
            msg = await message.answer("Введите команду в формате: /set_reminder_time HH:MM")
        add_message_id(user_id, msg.message_id)
        return

    time_str = args[1]
    # Проверяем формат HH:MM
    if not re.match(r'^([01]?[0-9]|2[0-3]):[0-5][0-9]$', time_str):
        msg = await message.answer("Неверный формат времени. Используйте HH:MM (например, 19:00).")
        add_message_id(user_id, msg.message_id)
        return

    # Сохраняем время в базу данных (в столбец reminder_time в таблице users)
    cur.execute("UPDATE users SET reminder_time = ? WHERE user_id = ?", (time_str, user_id))
    conn.commit()
    msg = await message.answer(f"✅ Время напоминаний установлено на {time_str}.")
    add_message_id(user_id, msg.message_id)

# --- Callback-ы ---

@dp.callback_query(lambda c: c.data.startswith("gender_"))
async def process_gender_callback(callback_query: types.CallbackQuery):
    logger.info(f"✅ Получен callback: {callback_query.data}")  # Лог
    user_id = callback_query.from_user.id
    if user_id not in user_states:
        await callback_query.answer("Сначала начни анкету: /start")
        return

    state = user_states[user_id]
    if state["step"] != "gender":
        await callback_query.answer("Это не тот этап анкеты.")
        return

    gender = "мужской" if callback_query.data == "gender_male" else "женский"
    state["data"]["gender"] = gender
    state["step"] = "height"

    # Удаляем старые сообщения
    await delete_old_messages(user_id, keep_last=0)
    msg = await callback_query.message.edit_text(f"Отлично! Теперь скажи, какой у тебя рост? (в см)")
    add_message_id(user_id, msg.message_id)

@dp.callback_query(lambda c: c.data.startswith("goal_"))
async def process_goal_callback(callback_query: types.CallbackQuery):
    logger.info(f"✅ Получен callback: {callback_query.data}")  # Лог
    user_id = callback_query.from_user.id
    if user_id not in user_states:
        await callback_query.answer("Сначала начни анкету: /start")
        return

    state = user_states[user_id]
    if state["step"] != "goal":
        await callback_query.answer("Это не тот этап анкеты.")
        return

    goal_map = {
        "goal_lose_weight": "похудеть",
        "goal_gain_muscle": "набрать массу",
        "goal_maintain": "поддерживать"
    }
    goal = goal_map[callback_query.data]
    state["data"]["goal"] = goal

    # Перейти к выбору места тренировки
    state["step"] = "training_location"
    # Удаляем старые сообщения
    await delete_old_messages(user_id, keep_last=0)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Дом (без инвентаря)", callback_data="location_home_basic")],
        [InlineKeyboardButton(text="🏋️ Дом + гантели", callback_data="location_home_weights")],
        [InlineKeyboardButton(text="🏋️‍♂️ Зал", callback_data="location_gym")],
        [InlineKeyboardButton(text="🌿 Улица", callback_data="location_outdoor")]
    ])
    msg = await callback_query.message.edit_text("Где ты тренируешься?", reply_markup=keyboard)
    add_message_id(user_id, msg.message_id)

@dp.callback_query(lambda c: c.data.startswith("location_"))
async def process_location_callback(callback_query: types.CallbackQuery):
    logger.info(f"✅ Получен callback: {callback_query.data}")  # Лог
    user_id = callback_query.from_user.id
    if user_id not in user_states:
        await callback_query.answer("Сначала начни анкету: /start")
        return

    state = user_states[user_id]
    if state["step"] != "training_location":
        await callback_query.answer("Это не тот этап анкеты.")
        return

    location_map = {
        "location_home_basic": "дом (без инвентаря)",
        "location_home_weights": "дом + гантели",
        "location_gym": "зал",
        "location_outdoor": "улица"
    }
    location = location_map[callback_query.data]
    state["data"]["training_location"] = location

    logger.info(f"✅ Сохранено место тренировки: {location}")  # Лог

    # Перейти к выбору уровня
    state["step"] = "level"
    # Удаляем старые сообщения
    await delete_old_messages(user_id, keep_last=0)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌱 Новичок", callback_data="level_beginner")],
        [InlineKeyboardButton(text="⚡ Средний", callback_data="level_intermediate")],
        [InlineKeyboardButton(text="🔥 Продвинутый", callback_data="level_advanced")]
    ])
    msg = await callback_query.message.edit_text("Какой у тебя уровень?", reply_markup=keyboard)
    add_message_id(user_id, msg.message_id)

@dp.callback_query(lambda c: c.data.startswith("level_"))
async def process_level_callback(callback_query: types.CallbackQuery):
    logger.info(f"✅ Получен callback: {callback_query.data}")  # Лог
    user_id = callback_query.from_user.id
    if user_id not in user_states:
        await callback_query.answer("Сначала начни анкету: /start")
        return

    state = user_states[user_id]
    if state["step"] != "level":
        await callback_query.answer("Это не тот этап анкеты.")
        return

    level_map = {
        "level_beginner": "новичок",
        "level_intermediate": "средний",
        "level_advanced": "продвинутый"
    }
    level = level_map[callback_query.data]
    state["data"]["level"] = level

    logger.info(f"✅ Сохранён уровень: {level}")  # Лог

    # Сохраняем профиль
    profile = state["data"]
    save_user_profile(user_id, profile)

    # Очищаем состояние
    del user_states[user_id]

    # Удаляем старые сообщения
    await delete_old_messages(user_id, keep_last=0)
    msg = await callback_query.message.edit_text(
        f"✅ Отлично, {profile['name']}! Твой профиль сохранён.\n\nТеперь ты можешь использовать:\n"
        "/training — получить тренировку\n"
        "/food — получить питание\n"
        "/subscribe — оформить подписку\n"
        "/profile — посмотреть свой профиль"
    )
    add_message_id(user_id, msg.message_id)

@dp.callback_query(lambda c: c.data == "training_completed")
async def training_completed_callback(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id

    # Сначала находим ID самой последней "pending" тренировки
    cur.execute("""
        SELECT id FROM trainings
        WHERE user_id = ? AND status = 'pending'
        ORDER BY date DESC
        LIMIT 1
    """, (user_id,))
    row = cur.fetchone()

    if row:
        training_id = row[0]
        # Обновляем статус
        cur.execute("UPDATE trainings SET status = 'completed' WHERE id = ?", (training_id,))
        conn.commit()
        await callback_query.answer("✅ Отлично! Тренировка засчитана.")
        check_achievements(user_id)  # Проверяем достижения
    else:
        await callback_query.answer("❌ Нет активной тренировки для завершения.", show_alert=True)

    await callback_query.message.edit_reply_markup(reply_markup=None)  # Убираем кнопки

@dp.callback_query(lambda c: c.data == "training_postpone")
async def training_postpone_callback(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    # Обновляем дату следующей тренировки на +1 день
    next_date = datetime.now() + timedelta(days=1)
    cur.execute("UPDATE users SET next_training_date = ? WHERE user_id = ?", (next_date.isoformat(), user_id))
    conn.commit()
    await callback_query.answer("✅ Тренировка перенесена на завтра.")
    await callback_query.message.edit_reply_markup(reply_markup=None)  # Убираем кнопки

# --- ИЗМЕНЕНО: Русские дни недели ---
@dp.callback_query(lambda c: c.data.startswith("schedule_"))
async def process_schedule_callback(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    schedule_map = {
        "schedule_3": {"days_per_week": 3, "days": ["Mon", "Wed", "Fri"]},
        "schedule_4": {"days_per_week": 4, "days": ["Mon", "Tue", "Thu", "Sat"]},
        "schedule_5": {"days_per_week": 5, "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]}
    }
    schedule_key = callback_query.data
    schedule_data = schedule_map.get(schedule_key)
    if schedule_data: # <-- Эта строка должна быть полной
        import json
        cur.execute("INSERT OR REPLACE INTO training_schedule (user_id, schedule) VALUES (?, ?)", (user_id, json.dumps(schedule_data)))
        conn.commit()
        # --- ИЗМЕНЕНО: Русские дни недели ---
        # Словарь для перевода дней недели
        day_map = {
            "Mon": "Пн",
            "Tue": "Вт",
            "Wed": "Ср",
            "Thu": "Чт",
            "Fri": "Пт",
            "Sat": "Сб",
            "Sun": "Вс"
        }
        russian_days = [day_map.get(day, day) for day in schedule_data['days']] # Переводим дни
        await callback_query.answer(f"✅ Установлен график: {schedule_data['days_per_week']} раза в неделю.")
        await callback_query.message.edit_text(f"Твой график: {schedule_data['days_per_week']} тренировки в неделю ({', '.join(russian_days)}).") # <-- Используем русские дни
    else:
        await callback_query.answer("❌ Ошибка: Неверный выбор графика.", show_alert=True)
    await callback_query.message.edit_reply_markup(reply_markup=None) # Убираем кнопки в любом случае
# --- Обработчик текста (всегда в конце!) ---

@dp.message()
async def handle_questionnaire(message: types.Message):
    user_id = message.from_user.id
    logger.info(f"Получено сообщение от {user_id}: {message.text}")  # Лог

    # Проверяем, является ли сообщение командой
    if message.text and message.text.startswith('/'):
        logger.info(f"Команда '{message.text}' — пропускаем, пусть обработчик команд сработает")  # Лог
        # НЕ вызываем await, просто выходим — пусть другие хендлеры обработают команду
        return

    # Если пользователь в анкете, обрабатываем анкету
    if user_id in user_states:
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
            # Удаляем старые сообщения
            await delete_old_messages(user_id, keep_last=0)
            msg = await message.answer(f"Отлично, {name}! Сколько тебе лет? (введите число)")
            add_message_id(user_id, msg.message_id)

        elif step == "age":
            try:
                age = int(message.text.strip())
                if age < 10 or age > 100:
                    msg = await message.answer("Пожалуйста, введи реальный возраст (от 10 до 100).")
                    add_message_id(user_id, msg.message_id)
                    return
                data["age"] = age
                state["step"] = "gender"
                # Удаляем старые сообщения
                await delete_old_messages(user_id, keep_last=0)
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Мужской", callback_data="gender_male")],
                    [InlineKeyboardButton(text="Женский", callback_data="gender_female")]
                ])
                msg = await message.answer("Какой у тебя пол?", reply_markup=keyboard)
                add_message_id(user_id, msg.message_id)
            except ValueError:
                msg = await message.answer("Пожалуйста, введи число.")
                add_message_id(user_id, msg.message_id)

        elif step == "height":
            try:
                height = int(message.text.strip())
                if height < 100 or height > 250:
                    msg = await message.answer("Пожалуйста, введи реальный рост в см (от 100 до 250).")
                    add_message_id(user_id, msg.message_id)
                    return
                data["height"] = height
                state["step"] = "weight"
                # Удаляем старые сообщения
                await delete_old_messages(user_id, keep_last=0)
                msg = await message.answer("Какой у тебя текущий вес? (в кг, например: 70.5)")
                add_message_id(user_id, msg.message_id)
            except ValueError:
                msg = await message.answer("Пожалуйста, введи число.")
                add_message_id(user_id, msg.message_id)

        elif step == "weight":
            try:
                weight = float(message.text.strip())
                if weight < 30 or weight > 300:
                    msg = await message.answer("Пожалуйста, введи реальный вес (от 30 до 300 кг).")
                    add_message_id(user_id, msg.message_id)
                    return
                data["weight"] = weight
                state["step"] = "goal"
                # Удаляем старые сообщения
                await delete_old_messages(user_id, keep_last=0)
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Похудеть", callback_data="goal_lose_weight")],
                    [InlineKeyboardButton(text="Набрать массу", callback_data="goal_gain_muscle")],
                    [InlineKeyboardButton(text="Поддерживать", callback_data="goal_maintain")]
                ])
                msg = await message.answer("Какая у тебя цель?", reply_markup=keyboard)
                add_message_id(user_id, msg.message_id)
            except ValueError:
                msg = await message.answer("Пожалуйста, введи число (можно с точкой).")
                add_message_id(user_id, msg.message_id)
    # Если пользователь не в анкете и это не команда — игнорируем

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
        return

    # --- Flask приложение для вебхука ---
    app = Flask(__name__)

    @app.route('/webhook', methods=['POST'])
    def webhook():
        if request.headers.get('content-type') == 'application/json':
            json_string = request.get_data().decode('utf-8')
            update = types.Update.model_validate_json(json_string)
            # Используем threadsafe версию, передавая сохранённый loop
            asyncio.run_coroutine_threadsafe(dp.feed_update(bot, update), loop)
            return '', 200
        else:
            logger.warning("Получен запрос на /webhook с неправильным Content-Type")
            return '', 403

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