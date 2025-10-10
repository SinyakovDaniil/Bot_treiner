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
    base_url='https://openrouter.ai/api/v1/' # Важно: со слэшем в конце
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
    last_weight_entry TEXT DEFAULT NULL
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

conn.commit()

# --- Глобальные переменные ---
user_states = {}
scheduler = AsyncIOScheduler()
reminder_times = {} # {user_id: time_str}

# --- Вспомогательные функции ---
def get_user_profile(user_id):
    cur.execute("SELECT name, age, weight, height, goal FROM users WHERE id = ?", (user_id,))
    return cur.fetchone()

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
    if profile and profile[0]: # Если имя заполнено
        cur.execute("SELECT id FROM achievements WHERE user_id = ? AND title = ?", (user_id, "Первые шаги"))
        if cur.fetchone() is None:
            add_achievement(user_id, "Первые шаги", "Заполнил анкету и начал путь к здоровью!")

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
            await message.answer("Привет! 🏋️‍♂️ Я твой ИИ-тренер. Ты получил <b>бесплатную неделю подписки</b>! Для начала, давай заполним анкету. Как тебя зовут?")
            user_states[user_id] = {"step": "name"}
        else:
            await message.answer("Привет! 🏋️‍♂️ Я твой ИИ-тренер. Для начала, давай заполним анкету. Как тебя зовут?")
            user_states[user_id] = {"step": "name"}
    else:
        # Старый пользователь
        logger.info(f"Старый пользователь: {user_id} ({username})")
        await message.answer("Привет! 🏋️‍♂️ С возвращением! Используй /menu для навигации.")

@dp.message(Command("subscribe"))
async def cmd_subscribe(message: types.Message):
    if is_subscribed(message.from_user.id):
        days_left = get_trial_days_left(message.from_user.id)
        sub_end = datetime.fromisoformat(cur.execute("SELECT subscription_end_date FROM users WHERE id = ?", (message.from_user.id,)).fetchone()[0])
        await message.answer(f"У вас активна подписка до {sub_end.strftime('%d.%m.%Y')}.")
        if days_left > 0:
            await message.answer(f"Из них <b>{days_left} дней</b> — осталось от пробной недели.")
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 месяц - 499 руб", callback_data="buy_month")],
        [InlineKeyboardButton(text="6 месяцев - 2499 руб", callback_data="buy_half_year")],
        [InlineKeyboardButton(text="1 год - 4999 руб", callback_data="buy_year")],
    ])
    await message.answer("<b>Тарифы подписки:</b>\n\n"
                         "1 месяц — 499 руб\n"
                         "6 месяцев — 2499 руб (416 руб/мес)\n"
                         "1 год — 4999 руб (416 руб/мес)\n\n"
                         "<a href='https://yourdomain.com/offerta'>Оферта</a>\n\n"
                         "Выберите тариф:", reply_markup=keyboard)

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
    robokassa_url = "https://auth.robokassa.ru/Merchant/Index.aspx?" + urlencode(params)

    await bot.send_message(user_id, f"Ссылка для оплаты подписки на {months_added} месяцев ({price} руб):\n{robokassa_url}\n\nПосле оплаты подпишитесь повторно, чтобы обновить статус.")
    await callback_query.answer()

# --- Обработка оплаты Robokassa ---
@dp.message(Command("check_payment")) # Команда для проверки (временно, для теста)
async def cmd_check_payment(message: types.Message):
    # В реальности, это будет callback от Robokassa на ваш сервер
    # Этот код нужно будет адаптировать под вашу логику обработки уведомления
    user_id = message.from_user.id
    # Пример: если получено уведомление об успешной оплате
    new_sub_end_date = datetime.now() + timedelta(days=30) # Пример: 1 месяц
    cur.execute("UPDATE users SET subscription_end_date = ? WHERE id = ?", (new_sub_end_date.isoformat(), user_id))
    conn.commit()
    await message.answer("Подписка успешно обновлена!")

# --- Обработчики анкеты ---
@dp.message(lambda message: message.from_user.id in user_states)
async def process_profile(message: types.Message):
    user_id = message.from_user.id
    state = user_states[user_id]

    if state["step"] == "name":
        user_states[user_id]["name"] = message.text
        await message.answer("Сколько тебе лет?")
        user_states[user_id]["step"] = "age"

    elif state["step"] == "age":
        try:
            age = int(message.text)
            if 10 <= age <= 120:
                user_states[user_id]["age"] = age
                await message.answer("Какой у тебя вес (в кг)?")
                user_states[user_id]["step"] = "weight"
            else:
                await message.answer("Пожалуйста, введите реальный возраст (10-120).")
        except ValueError:
            await message.answer("Пожалуйста, введите число.")

    elif state["step"] == "weight":
        try:
            weight = float(message.text.replace(',', '.'))
            if 30 <= weight <= 300:
                user_states[user_id]["weight"] = weight
                await message.answer("Какой у тебя рост (в см)?")
                user_states[user_id]["step"] = "height"
            else:
                await message.answer("Пожалуйста, введите реальный вес (30-300).")
        except ValueError:
            await message.answer("Пожалуйста, введите число.")

    elif state["step"] == "height":
        try:
            height = int(message.text)
            if 100 <= height <= 250:
                user_states[user_id]["height"] = height
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Похудеть", callback_data="goal_lose_weight")],
                    [InlineKeyboardButton(text="Набрать мышечную массу", callback_data="goal_gain_muscle")],
                    [InlineKeyboardButton(text="Поддерживать форму", callback_data="goal_maintain")]
                ])
                await message.answer("Какая у тебя цель?", reply_markup=keyboard)
                user_states[user_id]["step"] = "goal_choice"
            else:
                await message.answer("Пожалуйста, введите реальный рост (100-250).")
        except ValueError:
            await message.answer("Пожалуйста, введите число.")

# --- Обработка выбора цели ---
@dp.callback_query(lambda c: c.data.startswith('goal_'))
async def process_goal_callback(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    if user_id not in user_states or user_states[user_id]["step"] != "goal_choice":
        await callback_query.answer("Сначала заполните анкету.")
        return

    goals = {
        "goal_lose_weight": "Похудеть",
        "goal_gain_muscle": "Набрать мышечную массу",
        "goal_maintain": "Поддерживать форму"
    }
    goal = goals[callback_query.data]

    # Сохраняем данные в базу
    data = user_states[user_id]
    cur.execute("UPDATE users SET name=?, age=?, weight=?, height=?, goal=? WHERE id = ?",
                (data["name"], data["age"], data["weight"], data["height"], goal, user_id))
    conn.commit()

    # Удаляем пользователя из состояний
    if user_id in user_states:
        del user_states[user_id]

    # Проверяем и выдаём достижение
    profile = get_user_profile(user_id)
    check_and_award_achievements(user_id, profile)

    await bot.send_message(user_id, f"Анкета заполнена! Имя: {data['name']}, Возраст: {data['age']}, Вес: {data['weight']} кг, Рост: {data['height']} см, Цель: {goal}. Используй /menu для навигации.")

# --- Остальные команды (требуют подписки) ---
@dp.message(Command("training"))
async def cmd_training(message: types.Message):
    user_id = message.from_user.id
    if not is_subscribed(user_id):
        await message.answer("Для доступа к тренировкам нужна подписка. /subscribe")
        return

    profile = get_user_profile(user_id)
    if not profile:
        await message.answer("Сначала заполните анкету /start")
        return

    name, age, weight, height, goal = profile
    prompt = f"""
    Ты ИИ-тренер. Создай индивидуальную тренировку на 1 день для человека:
    - Имя: {name}
    - Возраст: {age}
    - Вес: {weight} кг
    - Рост: {height} см
    - Цель: {goal}
    - Уровень подготовки: начальный (если возраст > 50 или вес > 100 кг, учитывай осторожность)
    - Доступные снаряды: только тело, стул, стена (если ничего нет).
    - Длительность: 30-45 минут.
    Верни ПЛАН ТРЕНИРОВКИ в виде нумерованного списка упражнений с описанием, количеством подходов/повторений и отдыхом между.
    """

    try:
        response = client.chat.completions.create(
            model="openchat/openchat-7b",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1000
        )
        training_plan = response.choices[0].message.content
        await message.answer(f"<b>Ваша тренировка на сегодня:</b>\n\n{training_plan}")
    except Exception as e:
        logger.error(f"Ошибка при генерации тренировки: {e}")
        await message.answer("Ошибка при создании тренировки. Попробуйте позже.")

@dp.message(Command("food"))
async def cmd_food(message: types.Message):
    user_id = message.from_user.id
    if not is_subscribed(user_id):
        await message.answer("Для доступа к питанию нужна подписка. /subscribe")
        return

    profile = get_user_profile(user_id)
    if not profile:
        await message.answer("Сначала заполните анкету /start")
        return

    name, age, weight, height, goal = profile
    prompt = f"""
    Ты ИИ-тренер. Создай примерное меню на 1 день для человека:
    - Имя: {name}
    - Возраст: {age}
    - Вес: {weight} кг
    - Рост: {height} см
    - Цель: {goal}
    - Диетические ограничения: нет
    - Предпочтения: здоровая, сбалансированная пища
    Верни ПЛАН ПИТАНИЯ в виде: Завтрак, Обед, Ужин, Перекусы (если нужно) с описанием блюд и примерной калорийностью.
    """

    try:
        response = client.chat.completions.create(
            model="openchat/openchat-7b",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1000
        )
        food_plan = response.choices[0].message.content
        await message.answer(f"<b>Ваш план питания на сегодня:</b>\n\n{food_plan}")
    except Exception as e:
        logger.error(f"Ошибка при генерации питания: {e}")
        await message.answer("Ошибка при создании плана питания. Попробуйте позже.")

@dp.message(Command("progress"))
async def cmd_progress(message: types.Message):
    user_id = message.from_user.id
    if not is_subscribed(user_id):
        await message.answer("Для доступа к прогрессу нужна подписка. /subscribe")
        return

    text = message.text.split()
    if len(text) != 2:
        await message.answer("Используйте команду: /progress <вес>")
        return

    try:
        weight = float(text[1].replace(',', '.'))
        if not (30 <= weight <= 300):
            raise ValueError("Неверный вес")
    except ValueError:
        await message.answer("Введите корректный вес (например, 70.5).")
        return

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("INSERT INTO progress (user_id, weight, date) VALUES (?, ?, ?)", (user_id, weight, now))
    conn.commit()

    # Проверяем достижения (например, "Вес в норме")
    profile = get_user_profile(user_id)
    if profile:
        height_m = profile[3] / 100
        bmi = weight / (height_m ** 2)
        if 18.5 <= bmi <= 24.9:
            cur.execute("SELECT id FROM achievements WHERE user_id = ? AND title = ?", (user_id, "Золотая середина"))
            if cur.fetchone() is None:
                add_achievement(user_id, "Золотая середина", "BMI в норме!")

    await message.answer(f"Вес {weight} кг записан. Дата: {now.split()[0]}")

@dp.message(Command("profile"))
async def cmd_profile(message: types.Message):
    user_id = message.from_user.id
    if not is_subscribed(user_id):
        await message.answer("Для доступа к профилю нужна подписка. /subscribe")
        return

    profile = get_user_profile(user_id)
    if not profile:
        await message.answer("Сначала заполните анкету /start")
        return

    name, age, weight, height, goal = profile
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

    await message.answer(f"<b>Профиль:</b>\n"
                         f"Имя: {name}\n"
                         f"Возраст: {age}\n"
                         f"Вес: {weight} кг\n"
                         f"Рост: {height} см\n"
                         f"Цель: {goal}{sub_info}")

@dp.message(Command("achievements"))
async def cmd_achievements(message: types.Message):
    user_id = message.from_user.id
    if not is_subscribed(user_id):
        await message.answer("Для доступа к достижениям нужна подписка. /subscribe")
        return

    cur.execute("SELECT title, description, unlocked_date FROM achievements WHERE user_id = ?", (user_id,))
    achs = cur.fetchall()

    if not achs:
        await message.answer("У вас пока нет достижений. Продолжайте тренироваться!")
        return

    ach_text = "<b>Ваши достижения:</b>\n\n"
    for title, desc, date in achs:
        ach_text += f"🏆 <b>{title}</b>\n<i>{desc}</i>\nДата: {date[:10]}\n\n"

    await message.answer(ach_text)

# --- Чат с ИИ ---
@dp.message(Command("chat"))
async def cmd_chat(message: types.Message):
    user_id = message.from_user.id
    if not is_subscribed(user_id):
        await message.answer("Для доступа к чату с ИИ нужна подписка. /subscribe")
        return

    profile = get_user_profile(user_id)
    if not profile:
        await message.answer("Сначала заполните анкету /start")
        return

    prompt = f"Ты ИИ-тренер. Пользователь задаёт вопрос. Учитывай его данные: Имя: {profile[0]}, Возраст: {profile[1]}, Вес: {profile[2]} кг, Рост: {profile[3]} см, Цель: {profile[4]}. Ответь на вопрос: {message.text[6:]}" # [6:] убирает "/chat "

    try:
        response = client.chat.completions.create(
            model="openchat/openchat-7b",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500
        )
        ai_response = response.choices[0].message.content
        await message.answer(ai_response)
    except Exception as e:
        logger.error(f"Ошибка при ответе ИИ: {e}")
        await message.answer("Ошибка при ответе ИИ. Попробуйте позже.")

# --- Анализ рациона ---
@dp.message(Command("analyze_food"))
async def cmd_analyze_food(message: types.Message):
    user_id = message.from_user.id
    if not is_subscribed(user_id):
        await message.answer("Для доступа к анализу питания нужна подписка. /subscribe")
        return

    food_description = message.text[14:] # Убираем "/analyze_food "
    if not food_description.strip():
        await message.answer("Используйте команду: /analyze_food <описание еды>")
        return

    profile = get_user_profile(user_id)
    prompt = f"""
    Ты ИИ-тренер. Проанализируй питание, описанное пользователем: "{food_description}".
    Учитывай его данные: Имя: {profile[0]}, Возраст: {profile[1]}, Вес: {profile[2]} кг, Рост: {profile[3]} см, Цель: {profile[4]}.
    Оцени полезность, сбалансированность, калорийность (приблизительно). Дай рекомендации.
    """

    try:
        response = client.chat.completions.create(
            model="openchat/openchat-7b",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500
        )
        analysis = response.choices[0].message.content
        await message.answer(f"<b>Анализ вашего рациона:</b>\n\n{analysis}")
    except Exception as e:
        logger.error(f"Ошибка при анализе рациона: {e}")
        await message.answer("Ошибка при анализе рациона. Попробуйте позже.")

# --- График прогресса ---
@dp.message(Command("graph"))
async def cmd_graph(message: types.Message):
    user_id = message.from_user.id
    if not is_subscribed(user_id):
        await message.answer("Для доступа к графику нужна подписка. /subscribe")
        return

    cur.execute("SELECT date, weight FROM progress WHERE user_id = ? ORDER BY date ASC", (user_id,))
    data = cur.fetchall()

    if not data:
        await message.answer("Нет данных для построения графика. Добавьте вес с помощью /progress.")
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
    await message.answer_document(input_file)

# --- Напоминания ---
@dp.message(Command("set_reminder_time"))
async def cmd_set_reminder_time(message: types.Message):
    user_id = message.from_user.id
    if not is_subscribed(user_id):
        await message.answer("Для настройки напоминаний нужна подписка. /subscribe")
        return

    text = message.text.split()
    if len(text) != 2:
        await message.answer("Используйте команду: /set_reminder_time HH:MM (например, 19:00)")
        return

    time_str = text[1]
    try:
        # Проверяем формат времени
        datetime.strptime(time_str, "%H:%M")
        reminder_times[user_id] = time_str
        # Перезапускаем задачу для этого пользователя
        scheduler.remove_job(job_id=f"remind_{user_id}", jobstore='default')
        hour, minute = map(int, time_str.split(':'))
        scheduler.add_job(remind_workout, "cron", hour=hour, minute=minute, id=f"remind_{user_id}", args=[user_id])
        await message.answer(f"Время напоминания установлено на {time_str}.")
    except ValueError:
        await message.answer("Неверный формат времени. Используйте HH:MM (например, 19:00).")

async def remind_workout(user_id):
    try:
        await bot.send_message(user_id, "Время тренировки! 💪 Не забудь про /training и /food сегодня!")
    except Exception as e:
        logger.error(f"Ошибка при отправке напоминания пользователю {user_id}: {e}")

# --- Основная функция запуска ---
async def main():
    # Устанавливаем вебхук
    await bot.set_webhook(WEBHOOK_URL)
    logger.info(f"Вебхук установлен на {WEBHOOK_URL}")

    # Запускаем планировщик
    scheduler.start()

    # Запускаем диспетчер (он будет слушать webhook)
    await dp.start_polling(bot)

# --- Flask приложение для вебхука ---
app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():
    update_data = request.get_json()
    update = types.Update(**update_data)
    # Запускаем обработку обновления в asyncio
    asyncio.create_task(dp.process_update(update))
    return 'OK', 200

# --- Запуск Flask в отдельном потоке ---
def run_flask():
    app.run(host='0.0.0.0', port=8000, debug=False, use_reloader=False)

if __name__ == "__main__":
    # Запускаем Flask в отдельном потоке
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.start()

    # Запускаем основной цикл aiogram (вебхуки)
    asyncio.run(main())