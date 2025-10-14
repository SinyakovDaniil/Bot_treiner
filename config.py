import os
from dotenv import load_dotenv

# Загружаем переменные из key.env
load_dotenv(dotenv_path='key.env') # Указываем имя файла

# --- Телеграм ---
API_TOKEN = os.getenv("API_TOKEN")
if not API_TOKEN:
    raise ValueError("API_TOKEN не найден в key.env файле!")

# --- OpenRouter ---
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
if not OPENROUTER_API_KEY:
    raise ValueError("OPENROUTER_API_KEY не найден в key.env файле!")

# --- ЮMoney (вместо Robokassa) ---
# Идентификатор магазина (Shop ID) из личного кабинета ЮMoney/ЮKassa
YOOMONEY_SHOP_ID = 'your_yoomoney_shop_id'  # <-- Заменить
# Секретный ключ для проверки платежей (Notification secret)
YOOMONEY_SECRET_KEY = 'your_yoomoney_secret_key'  # <-- Заменить

# --- Вебхуки ---
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
if not WEBHOOK_URL:
    raise ValueError("WEBHOOK_URL не найден в key.env файле!")

# --- Админка ---
ADMIN_PASSWORD = os.getenv("SECRET_KEY") # Используем SECRET_KEY как пароль для веб-админки
if not ADMIN_PASSWORD:
    raise ValueError("SECRET_KEY для админки не найден в key.env файле!")

# НОВОЕ: Загрузка списка ID администраторов
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS")
if not ADMIN_IDS_RAW:
    raise ValueError("ADMIN_IDS не найден в key.env файле!")
try:
    # Преобразуем строку "123,456,789" в список [123, 456, 789]
    ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_RAW.split(',')]
except ValueError:
    raise ValueError("ADMIN_IDS должен быть строкой с ID, разделёнными запятой, например: 123,456,789")
