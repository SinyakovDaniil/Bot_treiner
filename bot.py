# bot.py
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
import re

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
client = OpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url='https://openrouter.ai/api/v1/'
)

MODEL = "qwen/qwen2.5-vl-72b-instruct:free"

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
    trial_granted BOOLEAN DEFAULT 0
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
    status TEXT DEFAULT 'pending',
    FOREIGN KEY (user_id) REFERENCES users (user_id)
);
""")

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
    schedule TEXT,
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
user_states = {}
scheduler = AsyncIOScheduler()
reminder_times = {}
loop = None

# --- Вспомогательные функции ---
def is_admin(user_id):
    return user_id in ADMIN_IDS

def get_user_count():
    cur.execute("SELECT COUNT(*) FROM users")
    return cur.fetchone()[0]

def get_subscribed_users():
    cur.execute("SELECT user_id FROM subscriptions WHERE expires_at > ?", (datetime.now().isoformat(),))
    return [row[0] for row in cur.fetchall()]

def get_users_list():
    cur.execute("""
        SELECT u.user_id, u.name, u.created_at, s.expires_at
        FROM users u
        LEFT JOIN subscriptions s ON u.user_id = s.user_id
        ORDER BY u.created_at DESC
    """)
    raw_users = cur.fetchall()
    processed_users = []
    now = datetime.now()
    for user in raw_users:
        user_id, name, created_at, expires_at = user
        sub_status = "Нет подписки"
        if expires_at:
            expires_dt = datetime.fromisoformat(expires_at)
            if expires_dt > now:
                sub_status = f"Активна до: {expires_dt.strftime('%Y-%m-%d')}"
            else:
                sub_status = f"Просрочена (до: {expires_dt.strftime('%Y-%m-%d')})"
        processed_users.append((user_id, name, created_at, sub_status))
    return processed_users

def get_user_by_id(user_id):
    cur.execute("SELECT user_id, name FROM users WHERE user_id = ?", (user_id,))
    return cur.fetchone()

def delete_user_from_db(user_id):
    cur.execute("DELETE FROM weights WHERE user_id = ?", (user_id,))
    cur.execute("DELETE FROM trainings WHERE user_id = ?", (user_id,))
    cur.execute("DELETE FROM progress WHERE user_id = ?", (user_id,))
    cur.execute("DELETE FROM achievements WHERE user_id = ?", (user_id,))
    cur.execute("DELETE FROM training_schedule WHERE user_id = ?", (user_id,))
    cur.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
    cur.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
    conn.commit()
    logger.info(f"Пользователь {user_id} удалён из базы данных.")

def save_user_profile(user_id, profile):
    cur.execute("""
        INSERT OR REPLACE INTO users (user_id, name, age, gender, height, weight, goal, training_location, level)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (user_id, profile['name'], profile['age'], profile['gender'], profile['height'], profile['weight'], profile['goal'], profile.get('training_location', ''), profile.get('level', '')))
    conn.commit()

def save_weight(user_id, weight):
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

def grant_subscription(user_id, days=7):
    expires_at = datetime.now() + timedelta(days=days)
    cur.execute("""
        INSERT OR REPLACE INTO subscriptions (user_id, expires_at)
        VALUES (?, ?)
    """, (user_id, expires_at.isoformat()))
    conn.commit()

def revoke_subscription(user_id):
    cur.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
    conn.commit()

def has_trial_granted(user_id):
    cur.execute("SELECT trial_granted FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if row:
        return bool(row[0])
    return False

def mark_trial_granted(user_id):
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
                    pass

def check_achievements(user_id):
    cur.execute("SELECT COUNT(*) FROM trainings WHERE user_id = ? AND status = 'completed'", (user_id,))
    completed_count = cur.fetchone()[0]
    if completed_count == 1:
        cur.execute("INSERT OR IGNORE INTO achievements (user_id, name) VALUES (?, ?)", (user_id, "Первая тренировка"))
        conn.commit()

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

# --- Команды ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    user_exists = cur.fetchone()

    if not user_exists:
        logger.info(f"Новый пользователь: {user_id}")
        # Создаём запись, но НЕ выдаём пробник
        cur.execute("""
            INSERT INTO users (user_id, created_at, trial_granted)
            VALUES (?, datetime('now'), 0) -- trial_granted = 0 при создании
        """, (user_id,))
        conn.commit()
        msg = await message.answer("🎉 Привет! Начни анкету: Как тебя зовут?")
    else:
        logger.info(f"Повторный запуск от: {user_id}")
        msg = await message.answer("Привет снова! Ты уже проходил анкету. Используй команды.")

    user_states[user_id] = {"step": "name", "data": {}, "messages": []}
    await delete_old_messages(user_id, keep_last=0)
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

# --- ЮMoney оплата ---
@dp.message(Command("subscribe"))
async def cmd_subscribe(message: types.Message):
    logger.info(f"[DEBUG] provider_token = '{provider_token}'")
    user_id = message.from_user.id

    # Параметры для Telegram Payments (интеграция с ЮMoney)
    # Используем YOOMONEY_SHOP_ID как provider_token для теста
    # В реальности, для Telegram Payments нужен специальный provider_token от Telegram
    # Но если вы используете ЮMoney, скорее всего, это будет ваш YOOMONEY_SHOP_ID
    # или специальный токен, полученный через BotFather
    
    # Для тестирования можно использовать тестовый provider_token от Telegram
    # provider_token = "123456789:TEST:..." 
    # Но если вы настроили через BotFather, используйте свой
    
    # ВАЖНО: Убедитесь, что provider_token корректен
    # Он обычно выглядит как "123456789:AA..." или "123456789:TEST:..."
    
    # Пример с использованием YOOMONEY_SHOP_ID как заглушки (это НЕВЕРНО для продакшена)
    # provider_token = YOOMONEY_SHOP_ID # <-- НЕПРАВИЛЬНО
    
    # Правильный способ: получить provider_token через BotFather
    # Для теста используем тестовый токен (замените на реальный для продакшена)
    provider_token = "390540012:LIVE:80188" # <-- ЗАМЕНИТЕ НА РЕАЛЬНЫЙ provider_token от Telegram/YooMoney
    
    if not provider_token or provider_token.startswith("390540012:LIVE:80188"):
        msg = await message.answer("❌ Оплата временно недоступна. Свяжитесь с администратором.")
        add_message_id(user_id, msg.message_id)
        return

    prices = [
        LabeledPrice(label="1 месяц", amount=14900), # 149.00 руб
    ]

    # Отправляем счет
    try:
        await bot.send_invoice(
            chat_id=user_id,
            title="Подписка на 1 месяц",
            description="Доступ к тренировкам и питанию на 30 дней",
            payload="subscription_1_month", # Уникальный идентификатор заказа
            provider_token=provider_token, # Токен от ЮMoney/Telegram Payments
            currency="RUB",
            prices=prices,
            start_parameter="subscribe_monthly"
        )
        logger.info(f"Счет отправлен пользователю {user_id}")
        
        # Проверяем, выдавался ли тестовый период
        if not has_trial_granted(user_id):
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🎁 Тестовый период (7 дней)", callback_data="trial_7")]
            ])
            msg = await message.answer("Или попробуй бесплатно на 7 дней!", reply_markup=keyboard)
            add_message_id(user_id, msg.message_id)
            
    except Exception as e:
        logger.error(f"Ошибка при отправке счета: {e}")
        msg = await message.answer("❌ Ошибка при создании счета. Попробуйте позже.")
        add_message_id(user_id, msg.message_id)

# Обработчик pre_checkout_query для Telegram Payments
@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: types.PreCheckoutQuery):
    # Всегда отвечаем OK
    await pre_checkout_query.answer(ok=True)
    logger.info(f"Pre-checkout query обработан для {pre_checkout_query.from_user.id}")

# Обработчик успешной оплаты
@dp.message(lambda m: m.content_type == 'successful_payment')
async def process_successful_payment(message: types.Message):
    user_id = message.from_user.id
    payment_info = message.successful_payment
    
    logger.info(f"Успешная оплата от {user_id}: {payment_info.invoice_payload}")
    
    # Проверяем, за что была оплата
    if payment_info.invoice_payload == "subscription_1_month":
        add_subscription(user_id, months=1)
        msg = await message.answer("✅ Спасибо за покупку! Подписка на 1 месяц активирована.")
        add_message_id(user_id, msg.message_id)
    elif payment_info.invoice_payload == "trial_7_days":
        grant_subscription(user_id, days=7)
        mark_trial_granted(user_id)
        msg = await message.answer("✅ Тестовый период на 7 дней активирован!")
        add_message_id(user_id, msg.message_id)
    else:
        logger.warning(f"Неизвестный payload: {payment_info.invoice_payload}")
        msg = await message.answer("✅ Оплата прошла успешно!")
        add_message_id(user_id, msg.message_id)

@dp.callback_query(lambda c: c.data == "trial_7")
async def process_trial_callback(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    
    if has_trial_granted(user_id):
        await callback_query.answer("❌ Тестовый период уже был выдан!", show_alert=True)
        return
        
    grant_subscription(user_id, days=7)
    mark_trial_granted(user_id)
    await callback_query.message.edit_text("✅ Тестовый период на 7 дней активирован!")
    await callback_query.answer()

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

    cur.execute("""
        SELECT status FROM trainings
        WHERE user_id = ? ORDER BY date DESC LIMIT 5
    """, (user_id,))
    recent_trainings = cur.fetchall()
    recent_statuses = [t[0] for t in recent_trainings]
    completed_count = recent_statuses.count('completed')
    if completed_count < 3:
        difficulty = "лёгкие и простые упражнения"
    else:
        difficulty = "средние или сложные упражнения"

    try:
        completion = client.chat.completions.create(
            model=MODEL,
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
            max_tokens=3000,
            temperature=0.7
        )
        training = completion.choices[0].message.content

        cur.execute("INSERT INTO trainings (user_id, content) VALUES (?, ?)", (user_id, training))
        conn.commit()

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Выполнил", callback_data="training_completed")],
            [InlineKeyboardButton(text=" сделаю позже", callback_data="training_postpone")]
        ])
        msg = await message.answer(f"Твоя тренировка на сегодня:\n\n{training}", reply_markup=keyboard)
        add_message_id(user_id, msg.message_id)
        await delete_old_messages(user_id)

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
            model=MODEL,
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
            max_tokens=3000,
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

@dp.message(Command("weight"))
async def cmd_weight(message: types.Message):
    user_id = message.from_user.id
    args = message.text.split()
    if len(args) != 2:
        msg = await message.answer("Введите команду в формате: /weight 70")
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

@dp.message(Command("progress"))
async def cmd_progress(message: types.Message):
    user_id = message.from_user.id
    args = message.text.split()
    if len(args) < 2:
        msg = await message.answer("Введите команду в формате:\n/progress 70.5 (вес в кг)")
        add_message_id(user_id, msg.message_id)
        return

    try:
        weight = float(args[1])
        save_weight(user_id, weight)

        cur.execute("""
            INSERT INTO progress (user_id, weight) VALUES (?, ?)
        """, (user_id, weight))
        conn.commit()

        msg = await message.answer(f"✅ Вес {weight} кг сохранён в прогресс.")
        add_message_id(user_id, msg.message_id)

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
    now = datetime.now()
    week_ago = now - timedelta(days=7)

    cur.execute("""
        SELECT COUNT(*) FROM trainings
        WHERE user_id = ? AND status = 'completed' AND date >= ?
    """, (user_id, week_ago.isoformat()))
    completed_count = cur.fetchone()[0]

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

@dp.message(Command("profile"))
async def show_profile(message: types.Message):
    logger.info(f"Получена команда /profile от {message.from_user.id}")
    user_id = message.from_user.id
    user = get_user_profile(user_id)
    if not user:
        msg = await message.answer("Сначала пройди анкету: /start")
        add_message_id(user_id, msg.message_id)
        return

    sub_status = "Подписка активна" if is_subscribed(user_id) else "Подписка не оформлена"
    weights = get_weights(user_id)
    weights_str = "\n".join([f"{w[1].split()[0]}: {w[0]} кг" for w in weights[-5:]])

    cur.execute("SELECT schedule FROM training_schedule WHERE user_id = ?", (user_id,))
    sched_row = cur.fetchone()
    schedule_info = sched_row[0] if sched_row else "не настроен"

    cur.execute("SELECT name FROM achievements WHERE user_id = ?", (user_id,))
    ach_rows = cur.fetchall()
    achievements_list = ", ".join([a[0] for a in ach_rows]) if ach_rows else "нет"

    profile = (
        f"Имя: {user['name']}\n"
        f"Возраст: {user['age']}\n"
        f"Пол: {user['gender']}\n"
        f"Рост: {user['height']} см\n"
        f"Вес: {user['weight']} кг\n"
        f"Цель: {user['goal']}\n"
        f"Место тренировки: {user['training_location'] or 'не указано'}\n"
        f"Уровень: {user['level'] or 'не указан'}\n"
        f"Дата следующей тренировки: {user['next_training_date'] or 'не указана'}\n"
        f"Время напоминаний: {user['reminder_time']}\n"
        f"График тренировок: {schedule_info}\n"
        f"Достижения: {achievements_list}\n"
        f"Статус подписки: {sub_status}\n"
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

    photo = BufferedInputFile(img.read(), filename='weight_graph.png')
    msg = await message.answer_photo(photo=photo)
    add_message_id(user_id, msg.message_id)

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        msg = await message.answer("❌ У вас нет прав администратора.")
        add_message_id(user_id, msg.message_id)
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Количество пользователей", callback_data="admin_user_count")],
        [InlineKeyboardButton(text="📋 Подписчики", callback_data="admin_subscribed")],
        [InlineKeyboardButton(text="✅ Выдать подписку", callback_data="admin_grant_sub")],
        [InlineKeyboardButton(text="❌ Отозвать подписку", callback_data="admin_revoke_sub")],
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
    ])
    msg = await message.answer("🔐 Панель администратора:", reply_markup=keyboard)
    add_message_id(user_id, msg.message_id)

@dp.callback_query(lambda c: c.data.startswith("admin_"))
async def admin_callback_handler(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    if not is_admin(user_id):
        await callback_query.answer("❌ У вас нет прав.")
        return

    action = callback_query.data

    if action == "admin_user_count":
        count = get_user_count()
        await callback_query.answer(f"Всего пользователей: {count}", show_alert=True)

    elif action == "admin_subscribed":
        subs = get_subscribed_users()
        await callback_query.answer(f"Количество подписчиков: {len(subs)}", show_alert=True)

    elif action == "admin_grant_sub":
        await callback_query.answer("Функция 'Выдать подписку' требует доработки для ввода ID пользователя и дней.", show_alert=True)

    elif action == "admin_revoke_sub":
        await callback_query.answer("Функция 'Отозвать подписку' требует доработки для ввода ID пользователя.", show_alert=True)

    elif action == "admin_users":
        await callback_query.answer("Функция 'Пользователи' доступна в веб-админке.", show_alert=True)

    elif action == "admin_broadcast":
        await callback_query.answer("Функция 'Рассылка' доступна в веб-админке.", show_alert=True)

    await callback_query.message.edit_reply_markup(reply_markup=None)

# --- Callback-ы ---

@dp.callback_query(lambda c: c.data.startswith("gender_"))
async def process_gender_callback(callback_query: types.CallbackQuery):
    logger.info(f"✅ Получен callback: {callback_query.data}")
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

    await delete_old_messages(user_id, keep_last=0)
    msg = await callback_query.message.edit_text(f"Отлично! Теперь скажи, какой у тебя рост? (в см)")
    add_message_id(user_id, msg.message_id)

@dp.callback_query(lambda c: c.data.startswith("goal_"))
async def process_goal_callback(callback_query: types.CallbackQuery):
    logger.info(f"✅ Получен callback: {callback_query.data}")
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

    state["step"] = "training_location"
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
    logger.info(f"✅ Получен callback: {callback_query.data}")
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

    logger.info(f"✅ Сохранено место тренировки: {location}")

    state["step"] = "level"
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
    logger.info(f"✅ Получен callback: {callback_query.data}")
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

    logger.info(f"✅ Сохранён уровень: {level}")

    profile = state["data"]
    save_user_profile(user_id, profile)

    del user_states[user_id]

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

    cur.execute("""
        SELECT id FROM trainings
        WHERE user_id = ? AND status = 'pending'
        ORDER BY date DESC
        LIMIT 1
    """, (user_id,))
    row = cur.fetchone()

    if row:
        training_id = row[0]
        cur.execute("UPDATE trainings SET status = 'completed' WHERE id = ?", (training_id,))
        conn.commit()
        await callback_query.answer("✅ Отлично! Тренировка засчитана.")
        check_achievements(user_id)
    else:
        await callback_query.answer("❌ Нет активной тренировки для завершения.", show_alert=True)

    await callback_query.message.edit_reply_markup(reply_markup=None)

@dp.callback_query(lambda c: c.data == "training_postpone")
async def training_postpone_callback(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    next_date = datetime.now() + timedelta(days=1)
    cur.execute("UPDATE users SET next_training_date = ? WHERE user_id = ?", (next_date.isoformat(), user_id))
    conn.commit()
    await callback_query.answer("✅ Тренировка перенесена на завтра.")
    await callback_query.message.edit_reply_markup(reply_markup=None)

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
    if schedule_data:
        import json
        cur.execute("INSERT OR REPLACE INTO training_schedule (user_id, schedule) VALUES (?, ?)", (user_id, json.dumps(schedule_data)))
        conn.commit()
        await callback_query.answer(f"✅ Установлен график: {schedule_data['days_per_week']} раза в неделю.")
        await callback_query.message.edit_text(f"Твой график: {schedule_data['days_per_week']} тренировки в неделю ({', '.join(schedule_data['days'])}).")

# --- Обработчик текста (всегда в конце!) ---

@dp.message()
async def handle_questionnaire(message: types.Message):
    user_id = message.from_user.id
    logger.info(f"Получено сообщение от {user_id}: {message.text}")

    if message.text and message.text.startswith('/'):
        logger.info(f"Команда '{message.text}' — пропускаем")
        return

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

# --- Основная функция запуска ---
async def main():
    global loop
    loop = asyncio.get_running_loop()
    scheduler.start()
    logger.info("⏰ Планировщик запущен")

    try:
        await bot.set_webhook(WEBHOOK_URL)
        logger.info(f"📡 Вебхук установлен на {WEBHOOK_URL}")
    except Exception as e:
        logger.error(f"❌ Ошибка при установке вебхука: {e}")
        return

    # --- Flask приложение для вебхука (порт 8000) ---
    webhook_app = Flask(__name__)

    @webhook_app.route('/webhook', methods=['POST'])
    def webhook():
        content_type = request.headers.get('Content-Type', '').lower()
        if content_type != 'application/json':
            logger.warning("Получен запрос на /webhook с неправильным Content-Type")
            return '', 403

        json_string = request.get_data().decode('utf-8')
        try:
            update = types.Update.model_validate_json(json_string)
        except Exception as e:
            logger.error(f"Ошибка при десериализации JSON: {e}")
            return '', 400

        try:
            future = asyncio.run_coroutine_threadsafe(dp.feed_update(bot, update), loop)
        except Exception as e:
            logger.error(f"Ошибка при передаче апдейта в aiogram: {e}")
            return '', 500

        return '', 200

    # --- Flask приложение для веб-админки (порт 8001) ---
    admin_app = Flask(__name__)
    admin_app.secret_key = 'your_secret_key_here' # Замените на случайный ключ

    # --- Вспомогательные функции админки ---
    def admin_required(f):
        def decorated_function(*args, **kwargs):
            if not session.get('authenticated'):
                return redirect(url_for('admin_login'))
            return f(*args, **kwargs)
        decorated_function.__name__ = f.__name__
        return decorated_function

    @admin_app.route('/admin/login', methods=['GET', 'POST'])
    def admin_login():
        if request.method == 'POST':
            password = request.form.get('password')
            if password == ADMIN_PASSWORD:
                session['authenticated'] = True
                return redirect(url_for('admin_index'))
            else:
                return "❌ Неверный пароль", 403
        return render_template('admin_login.html')

    @admin_app.route('/admin')
    @admin_required
    def admin_index():
        user_count = get_user_count()
        sub_count = len(get_subscribed_users())
        return render_template('admin.html', authenticated=True, user_count=user_count, sub_count=sub_count)

    @admin_app.route('/admin/users')
    @admin_required
    def admin_users():
        users = get_users_list()
        return render_template('admin_users.html', users=users)

    @admin_app.route('/admin/grant', methods=['GET', 'POST'])
    @admin_required
    def admin_grant():
        if request.method == 'POST':
            user_id_str = request.form.get('user_id')
            days_str = request.form.get('days')
            try:
                user_id = int(user_id_str)
                days = int(days_str)
                if days <= 0:
                    return "❌ Количество дней должно быть положительным.", 400
                grant_subscription(user_id, days=days)
                logger.info(f"Администратор выдал подписку на {days} дней пользователю {user_id}")
                return redirect(url_for('admin_grant'))
            except ValueError:
                return "❌ Неверный формат ID пользователя или дней.", 400
        return render_template('admin_grant.html')

    @admin_app.route('/admin/revoke', methods=['GET', 'POST'])
    @admin_required
    def admin_revoke():
        if request.method == 'POST':
            user_id_str = request.form.get('user_id')
            try:
                user_id = int(user_id_str)
                revoke_subscription(user_id)
                logger.info(f"Администратор отозвал подписку у пользователя {user_id}")
                return redirect(url_for('admin_revoke'))
            except ValueError:
                return "❌ Неверный формат ID пользователя.", 400
        return render_template('admin_revoke.html')

    @admin_app.route('/admin/broadcast', methods=['GET', 'POST'])
    @admin_required
    def admin_broadcast():
        if request.method == 'POST':
            message_text = request.form.get('message')
            if not message_text:
                return "❌ Сообщение не может быть пустым.", 400

            cur.execute("SELECT user_id FROM users")
            user_ids = [row[0] for row in cur.fetchall()]
            sent_count = 0
            failed_count = 0

            for user_id in user_ids:
                try:
                    logger.info(f"Broadcast: сообщение для {user_id} готово к отправке.")
                    sent_count += 1
                except Exception as e:
                    logger.error(f"Ошибка при отправке сообщения {user_id}: {e}")
                    failed_count += 1

            logger.info(f"Рассылка завершена. Успешно: {sent_count}, Ошибок: {failed_count}")
            return redirect(url_for('admin_broadcast'))
        return render_template('admin_broadcast.html')

    @admin_app.route('/admin/delete_user_confirm/<int:user_id>')
    @admin_required
    def admin_delete_user_confirm(user_id):
        cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        if not cur.fetchone():
            return "❌ Пользователь с таким ID не найден.", 404
        delete_user_from_db(user_id)
        logger.info(f"Администратор удалил пользователя {user_id}")
        return redirect(url_for('admin_users'))

    # --- Запуск Flask-серверов в отдельных потоках ---
    def run_webhook():
        from waitress import serve
        logger.info("🌐 Flask (Waitress) вебхука запускается на 0.0.0.0:8000...")
        serve(webhook_app, host='0.0.0.0', port=8000)

    def run_admin():
        from waitress import serve
        logger.info("🌐 Flask (Waitress) админки запускается на 0.0.0.0:8001...")
        serve(admin_app, host='0.0.0.0', port=8001)

    webhook_thread = threading.Thread(target=run_webhook)
    admin_thread = threading.Thread(target=run_admin)

    webhook_thread.daemon = True
    admin_thread.daemon = True

    webhook_thread.start()
    admin_thread.start()

    logger.info("🧵 Потоки Flask запущены")

    logger.info("🤖 Бот запущен и ожидает сообщений...")

    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        logger.info("🛑 Бот остановлен пользователем")

if __name__ == "__main__":
    try:
        import waitress
    except ImportError:
        logger.critical("❌ Модуль 'waitress' не найден. Установите его: pip install waitress")
        exit(1)

    try:
        asyncio.run(main())
    except Exception as e:
        logger.critical(f"💥 Критическая ошибка в основном цикле: {e}", exc_info=True)
