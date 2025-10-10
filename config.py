import os
from dotenv import load_dotenv

# Загружаем переменные из .env
load_dotenv()

# --- Телеграм ---
API_TOKEN = os.getenv("API_TOKEN")
if not API_TOKEN:
    raise ValueError("API_TOKEN не найден в .env файле!")

# --- OpenRouter ---
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
if not OPENROUTER_API_KEY:
    raise ValueError("OPENROUTER_API_KEY не найден в .env файле!")

# --- Robokassa ---
ROBOKASSA_LOGIN = os.getenv("ROBOKASSA_LOGIN")
ROBOKASSA_PASS1 = os.getenv("ROBOKASSA_PASS1")
ROBOKASSA_PASS2 = os.getenv("ROBOKASSA_PASS2")
if not ROBOKASSA_LOGIN or not ROBOKASSA_PASS1 or not ROBOKASSA_PASS2:
    raise ValueError("Данные Robokassa не найдены в .env файле!")

# --- Вебхуки ---
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
if not WEBHOOK_URL:
    raise ValueError("WEBHOOK_URL не найден в .env файле!")

# --- Админка ---
ADMIN_PASSWORD = os.getenv("SECRET_KEY")
if not ADMIN_PASSWORD:
    raise ValueError("SECRET_KEY для админки не найден в .env файле!")