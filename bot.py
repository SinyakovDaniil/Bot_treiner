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
import hashlib  # <--- Добавь эту строку
from urllib.parse import urlencode

API_TOKEN = '8222634489:AAEJMtFGVZGY6MDotsWUn0_5UmlVaK8F06E'  # Замени на токен от @BotFather
OPENROUTER_API_KEY = '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'  # Замени на API-ключ от OpenRouter

# ИСПРАВЛЕНО: base_url и убраны лишние пробелы
client = OpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url='https://openrouter.ai/api/v1/'  # Правильный URL API
)

MODEL = "microsoft/wizardlm-2-8x22b"  # Используем конкретную модель

bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# Подключение к SQLite
conn = sqlite3.connect('trainer_bot.db')
cur = conn.cursor()

# Создание таблиц
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
    training_location TEXT,  -- Новое поле
    level TEXT,              -- Новое поле
    last_training_date TIMESTAMP,
    next_training_date TIMESTAMP,
    reminder_time TEXT DEFAULT '08:00',  -- Новое поле
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

# Словарь для хранения ID сообщений от бота
bot_messages = {}  # {user_id: [message_id, ...]}

# Словарь для хранения состояния анкеты пользователей
user_states = {}  # {user_id: {"step": "name", "data": {...}}}

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

def add_message_id(user_id, msg_id):
    if user_id not in bot_messages:
        bot_messages[user_id] = []
    bot_messages[user_id].append(msg_id)

async def delete_old_messages(user_id, keep_last=3):
    if user_id in bot_messages and len(bot_messages[user_id]) > keep_last:
        to_delete = bot_messages[user_id][:-keep_last]
        for msg_id in to_delete:
            try:
                await bot.delete_message(chat_id=user_id, message_id=msg_id)
            except Exception:
                pass  # Сообщение уже удалено или не может быть удалено
        bot_messages[user_id] = bot_messages[user_id][-keep_last:]

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
    user_states[user_id] = {"step": "name", "data": {}}
    # Удаляем старые сообщения
    await delete_old_messages(user_id, keep_last=0)
    msg = await message.answer(
        "Привет! Я твой персональный тренер 💪\n\nКак тебя зовут?"
    )
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

# --- Robokassa оплата ---
# Настрой Robokassa (тестовые данные)
MERCHANT_LOGIN = 'instructorII'  # Заменить
MERCHANT_PASS1 = 'rfl55jo7ELyUoCD1Edt7'  # Заменить
MERCHANT_PASS2 = 'oAoCHt4yBa1lg5w1HD0k'  # Заменить
ROBOKASSA_URL = 'https://auth.robokassa.ru/Merchant/Index.aspx'

@dp.message(Command("subscribe"))
async def cmd_subscribe(message: types.Message):
    user_id = message.from_user.id

    # Параметры для Robokassa
    out_sum = 149.00  # Цена подписки
    inv_id = user_id  # Используем user_id как ID заказа
    desc = f'Подписка для пользователя {user_id} на 1 месяц'

    # Формируем подпись
    signature = f"{MERCHANT_LOGIN}:{out_sum}:{inv_id}:{MERCHANT_PASS1}:Shp_userId={user_id}"
    sign_hash = hashlib.md5(signature.encode()).hexdigest()

    # Формируем URL
    params = {
        'MerchantLogin': MERCHANT_LOGIN,
        'OutSum': out_sum,
        'InvId': inv_id,
        'Desc': desc,
        'SignatureValue': sign_hash,
        'Shp_userId': user_id,
        'Encoding': 'utf-8',
        'Culture': 'ru'
    }

    payment_url = f"{ROBOKASSA_URL}?{urlencode(params)}"

    # Отправляем сообщение с офертом и ссылкой
    msg = await message.answer(
        f"Оформить подписку на 1 месяц можно по ссылке:\n\n{payment_url}\n\n"
        f"При оплате вы соглашаетесь с условиями публичной оферты: https://docs.google.com/document/d/14NrOTKOJ2Dcd5-guVZGU7fRj9gj-wS1X/edit?usp=drive_link&ouid=111319375229341079989&rtpof=true&sd=true"
    )
    add_message_id(user_id, msg.message_id)

@dp.message(Command("training"))
async def send_training(message: types.Message):
    print("❌ НАЧАЛО ОБРАБОТЧИКА /training")
    user_id = message.from_user.id
    user = get_user_profile(user_id)
    print(f"Профиль пользователя: {user}")

    if not user:
        print("Профиль не найден")
        msg = await message.answer("Сначала пройди анкету: /start")
        add_message_id(user_id, msg.message_id)
        return

    print(f"Подписка: {is_subscribed(user_id)}")
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
        print("Отправляем запрос к API...")
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
"""},  # Улучшенный промт
                {"role": "user", "content": "Создай тренировку."}
            ],
            max_tokens=1000,  # Увеличено
            temperature=0.7
        )
        training = completion.choices[0].message.content
        print("Ответ от API получен")

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
        print(f"❌ Ошибка при генерации тренировки: {e}")
        msg = await message.answer("❌ Ошибка при генерации тренировки. Попробуй позже.")
        add_message_id(user_id, msg.message_id)


@dp.message(Command("food"))
async def send_food(message: types.Message):
    print("❌ НАЧАЛО ОБРАБОТЧИКА /food")
    user_id = message.from_user.id
    user = get_user_profile(user_id)
    print(f"Профиль пользователя: {user}")

    if not user:
        msg = await message.answer("Сначала пройди анкету: /start")
        add_message_id(user_id, msg.message_id)
        return

    if not is_subscribed(user_id):
        msg = await message.answer("🔒 Эта функция доступна только по подписке. Используй /subscribe, чтобы оформить.")
        add_message_id(user_id, msg.message_id)
        return

    try:
        print("Отправляем запрос к API (питание)...")
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
"""},  # Улучшенный промт
                {"role": "user", "content": "Создай питание."}
            ],
            max_tokens=1000,  # Увеличено
            temperature=0.7
        )
        food = completion.choices[0].message.content
        print("Ответ от API (питание) получен")
        msg = await message.answer(f"Твоё питание на сегодня:\n\n{food}")
        add_message_id(user_id, msg.message_id)
        await delete_old_messages(user_id)
    except Exception as e:
        print(f"❌ Ошибка при генерации питания: {e}")
        msg = await message.answer("❌ Ошибка при генерации питания. Попробуй позже.")
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
        [InlineKeyboardButton(text="5 раз в неделю", callback_data="schedule_5")]
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

@dp.message(Command("profile"))
async def show_profile(message: types.Message):
    print("❌ НАЧАЛО ОБРАБОТЧИКА /profile")
    user_id = message.from_user.id
    user = get_user_profile(user_id)
    print(f"Профиль пользователя: {user}")

    if not user:
        msg = await message.answer("Сначала пройди анкету: /start")
        add_message_id(user_id, msg.message_id)
        return

    sub_status = "Подписка активна" if is_subscribed(user_id) else "Подписка не оформлена"
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

    # Оборачиваем BytesIO в BufferedInputFile
    photo = BufferedInputFile(img.read(), filename='weight_graph.png')
    msg = await message.answer_photo(photo=photo)
    add_message_id(user_id, msg.message_id)

# --- Новые функции ---

@dp.message(Command("chat"))
async def cmd_chat(message: types.Message):
    user_id = message.from_user.id
    user = get_user_profile(user_id)

    if not user:
        msg = await message.answer("Сначала пройди анкету: /start")
        add_message_id(user_id, msg.message_id)
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        msg = await message.answer("Напиши вопрос тренеру после команды: /chat Почему болят мышцы после тренировки?")
        add_message_id(user_id, msg.message_id)
        return

    question = args[1]

    try:
        completion = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": f"""
Ты — персональный фитнес-тренер. Отвечай на вопросы пользователя, учитывая его профиль:

- Имя: {user['name']}
- Пол: {user['gender']}
- Возраст: {user['age']} лет
- Рост: {user['height']} см
- Вес: {user['weight']} кг
- Цель: {user['goal']}
- Место тренировки: {user['training_location'] or 'не указано'}
- Уровень: {user['level'] or 'не указан'}

Пиши на **русском языке**, **дружелюбно** и **по делу**.
"""},  # Контекст
                {"role": "user", "content": question}
            ],
            max_tokens=500,
            temperature=0.7
        )
        answer = completion.choices[0].message.content
        msg = await message.answer(f"🧠 Ответ тренера:\n\n{answer}")
        add_message_id(user_id, msg.message_id)

    except Exception as e:
        msg = await message.answer(f"Ошибка при генерации ответа: {str(e)}")
        add_message_id(user_id, msg.message_id)

@dp.message(Command("analyze_food"))
async def cmd_analyze_food(message: types.Message):
    user_id = message.from_user.id
    user = get_user_profile(user_id)

    if not user:
        msg = await message.answer("Сначала пройди анкету: /start")
        add_message_id(user_id, msg.message_id)
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        msg = await message.answer("Напиши, что ел сегодня: /analyze_food яйца, бекон, тост, кофе")
        add_message_id(user_id, msg.message_id)
        return

    food = args[1]

    try:
        completion = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": f"""
Ты — персональный диетолог. Проанализируй рацион пользователя, учитывая его профиль:

- Имя: {user['name']}
- Пол: {user['gender']}
- Возраст: {user['age']} лет
- Рост: {user['height']} см
- Вес: {user['weight']} кг
- Цель: {user['goal']}
- Место тренировки: {user['training_location'] or 'не указано'}
- Уровень: {user['level'] or 'не указан'}

Рацион: {food}

Проанализируй:
- Сколько калорий (примерно).
- Баланс БЖУ (белки, жиры, углеводы).
- Подходит ли под цель.
- Рекомендации.

Пиши на **русском языке**, **дружелюбно** и **по делу**.
"""},  # Контекст
                {"role": "user", "content": f"Проанализируй этот рацион: {food}"}
            ],
            max_tokens=500,
            temperature=0.7
        )
        analysis = completion.choices[0].message.content
        msg = await message.answer(f"🥗 Анализ рациона:\n\n{analysis}")
        add_message_id(user_id, msg.message_id)

    except Exception as e:
        msg = await message.answer(f"Ошибка при анализе рациона: {str(e)}")
        add_message_id(user_id, msg.message_id)

@dp.message(Command("export_progress"))
async def cmd_export_progress(message: types.Message):
    user_id = message.from_user.id
    weights = get_weights(user_id)

    if not weights:
        msg = await message.answer("Нет данных для экспорта.")
        add_message_id(user_id, msg.message_id)
        return

    # Формат: дата, вес
    data_str = "Дата\tВес (кг)\n"
    data_str += "\n".join([f"{w[1].split()[0]}\t{w[0]}" for w in weights])

    # Отправляем как текстовое сообщение
    msg = await message.answer(f"📋 Прогресс (скопируй и вставь в Excel):\n\n```\n{data_str}\n```", parse_mode="Markdown")
    add_message_id(user_id, msg.message_id)

@dp.message(Command("set_reminder_time"))
async def cmd_set_reminder_time(message: types.Message):
    user_id = message.from_user.id
    args = message.text.split()
    if len(args) != 2:
        msg = await message.answer("Используй формат: /set_reminder_time 19:00")
        add_message_id(user_id, msg.message_id)
        return

    time_str = args[1]
    # Проверим формат времени (простая проверка)
    try:
        hour, minute = map(int, time_str.split(':'))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError:
        msg = await message.answer("Неправильный формат времени. Используй HH:MM (например, 19:00).")
        add_message_id(user_id, msg.message_id)
        return

    cur.execute("UPDATE users SET reminder_time = ? WHERE user_id = ?", (time_str, user_id))
    conn.commit()

    msg = await message.answer(f"✅ Время напоминаний установлено на {time_str}.")
    add_message_id(user_id, msg.message_id)

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    help_text = """
📖 Справочник по командам:

/start — начать анкету
/training — получить тренировку
/food — получить питание
/progress — ввести вес (например, /progress 70.5)
/schedule — настроить график тренировок
/report — получить недельный отчёт
/achievements — посмотреть достижения
/profile — посмотреть свой профиль
/weight_graph — график изменения веса
/chat — задать вопрос тренеру
/analyze_food — анализ рациона (например, /analyze_food яйца, бекон)
/export_progress — экспорт прогресса (для Excel)
/set_reminder_time — установить время напоминаний (например, /set_reminder_time 19:00)
/subscribe — оформить подписку
/help — этот справочник
    """
    msg = await message.answer(help_text)
    add_message_id(message.from_user.id, msg.message_id)

# --- Callback-ы ---

@dp.callback_query(lambda c: c.data.startswith("gender_"))
async def process_gender_callback(callback_query: types.CallbackQuery):
    print(f"✅ Получен callback: {callback_query.data}")  # Лог
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
    print(f"✅ Получен callback: {callback_query.data}")  # Лог
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
    print(f"✅ Получен callback: {callback_query.data}")  # Лог
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

    print(f"✅ Сохранено место тренировки: {location}")  # Лог

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
    print(f"✅ Получен callback: {callback_query.data}")  # Лог
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

    print(f"✅ Сохранён уровень: {level}")  # Лог

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
    print(f"Получено сообщение от {user_id}: {message.text}")  # Лог

    # Проверяем, является ли сообщение командой
    if message.text and message.text.startswith('/'):
        print(f"Команда '{message.text}' — пропускаем, пусть обработчик команд сработает")  # Лог
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

async def send_daily_reminder():
    now = datetime.now()
    # Проверяем, есть ли просроченные тренировки (не выполненные)
    cur.execute("""
        SELECT user_id, content FROM trainings
        WHERE status = 'pending' AND date < ?
    """, (now - timedelta(days=1),))  # Просрочено более 1 дня
    overdue_trainings = cur.fetchall()

    for user_id, content in overdue_trainings:
        try:
            await bot.send_message(user_id, f"⚠️ Напоминаю: ты не завершил(а) эту тренировку:\n\n{content}")
        except Exception as e:
            print(f"Ошибка при отправке напоминания пользователю {user_id}: {e}")

    # Обновляем статус на 'missed'
    cur.execute("""
        UPDATE trainings SET status = 'missed' WHERE status = 'pending' AND date < ?
    """, (now - timedelta(days=1),))
    conn.commit()

    # Проверяем, кому пора отправить новую тренировку
    cur.execute("""
        SELECT user_id FROM users
        WHERE next_training_date IS NOT NULL AND next_training_date <= ?
    """, (now,))
    users_for_training = cur.fetchall()

    for user in users_for_training:
        user_id = user[0]
        if is_subscribed(user_id):
            try:
                await bot.send_message(user_id, "Время новой тренировки!")
                # Вызываем команду /training
                fake_message = types.Message()
                fake_message.from_user = types.User(id=user_id, is_bot=False, first_name="User", username=None)
                fake_message.chat = types.Chat(id=user_id, type="private")
                await send_training(fake_message)
            except Exception as e:
                print(f"Ошибка при отправке тренировки пользователю {user_id}: {e}")

async def main():
    print("🤖 Бот начинает запуск...")
    try:
        # Проверим, можно ли создать клиента
        print("🔧 Инициализация OpenAI клиента...")
        # Убрана переинициализация client
        print("✅ Клиент OpenAI создан")
    except Exception as e:
        print(f"❌ Ошибка при создании клиента: {e}")
        return

    scheduler = AsyncIOScheduler()
    scheduler.add_job(send_daily_reminder, CronTrigger(hour=8, minute=0))  # Напоминание в 8:00 утра
    scheduler.start()
    print("⏰ Планировщик запущен")

    print("📡 Запуск polling...")
    await dp.start_polling(bot)
    print("✅ Polling завершён")


if __name__ == '__main__':
    asyncio.run(main())