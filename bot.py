import asyncio
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
import hashlib
import json

import requests
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import BotCommand, Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram import F
from supabase import create_client, Client

# ---------- НАСТРОЙКИ ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВАШ_ТОКЕН")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))  # ваш Telegram ID
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Не заданы SUPABASE_URL или SUPABASE_KEY в переменных окружения")

# ---------- ПОДКЛЮЧЕНИЕ К SUPABASE ----------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ---------- КЕШ ДЛЯ КУРСОВ (60 секунд) ----------
_cache = {
    "rates": {},          # {"pair": {"rate": float, "timestamp": datetime}}
    "last_cny": None,
}
CACHE_TTL = 60

# ---------- ЛОГИРОВАНИЕ ----------
logging.basicConfig(level=logging.INFO)

# ---------- ИНИЦИАЛИЗАЦИЯ БОТА ----------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ SUPABASE ----------
def get_user(telegram_id: int) -> Optional[Dict]:
    """Проверяет, авторизован ли пользователь"""
    try:
        resp = supabase.table("users").select("*").eq("id", telegram_id).execute()
        if resp.data and len(resp.data) > 0:
            return resp.data[0]
        return None
    except Exception as e:
        logging.error(f"Supabase get_user error: {e}")
        return None

def add_user(telegram_id: int, username: str = "") -> bool:
    """Добавляет пользователя в таблицу users"""
    try:
        supabase.table("users").insert({"id": telegram_id, "username": username}).execute()
        return True
    except Exception as e:
        logging.error(f"Supabase add_user error: {e}")
        return False

def remove_user(telegram_id: int) -> bool:
    """Удаляет пользователя"""
    try:
        supabase.table("users").delete().eq("id", telegram_id).execute()
        return True
    except Exception as e:
        logging.error(f"Supabase remove_user error: {e}")
        return False

def get_all_users() -> List[Dict]:
    """Возвращает всех активных пользователей"""
    try:
        resp = supabase.table("users").select("*").eq("is_active", True).execute()
        return resp.data
    except Exception as e:
        logging.error(f"Supabase get_all_users error: {e}")
        return []

def get_password_hash() -> str:
    """Возвращает хеш пароля из таблицы settings"""
    try:
        resp = supabase.table("settings").select("value").eq("key", "access_password").execute()
        if resp.data and len(resp.data) > 0:
            return resp.data[0]["value"]
        # Если пароля нет, создаём дефолтный "1234" в хеше
        default_hash = hashlib.sha256("1234".encode()).hexdigest()
        supabase.table("settings").insert({"key": "access_password", "value": default_hash}).execute()
        return default_hash
    except Exception as e:
        logging.error(f"Supabase get_password_hash error: {e}")
        return hashlib.sha256("1234".encode()).hexdigest()

def set_password_hash(new_password: str) -> bool:
    """Обновляет хеш пароля"""
    try:
        new_hash = hashlib.sha256(new_password.encode()).hexdigest()
        supabase.table("settings").update({"value": new_hash}).eq("key", "access_password").execute()
        return True
    except Exception as e:
        logging.error(f"Supabase set_password_hash error: {e}")
        return False

def get_alerts() -> List[Dict]:
    """Возвращает все активные алерты"""
    try:
        resp = supabase.table("alerts").select("*").eq("is_active", True).execute()
        return resp.data
    except Exception as e:
        logging.error(f"Supabase get_alerts error: {e}")
        return []

def add_alert(pair: str, threshold: float, direction: str) -> bool:
    """Создаёт новый алерт"""
    try:
        supabase.table("alerts").insert({
            "pair": pair,
            "threshold": threshold,
            "direction": direction
        }).execute()
        return True
    except Exception as e:
        logging.error(f"Supabase add_alert error: {e}")
        return False

def remove_alert(alert_id: int) -> bool:
    """Удаляет алерт (мягкое удаление, ставим is_active=false)"""
    try:
        supabase.table("alerts").update({"is_active": False}).eq("id", alert_id).execute()
        return True
    except Exception as e:
        logging.error(f"Supabase remove_alert error: {e}")
        return False

# ---------- ПОЛУЧЕНИЕ КУРСОВ ----------
def get_cbr_rates() -> Dict[str, float]:
    """Получает курсы USD и CNY от ЦБ РФ"""
    url = "https://www.cbr-xml-daily.ru/daily_json.js"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return {
            "USD": data["Valute"]["USD"]["Value"],
            "CNY": data["Valute"]["CNY"]["Value"]
        }
    except Exception as e:
        logging.error(f"ЦБ РФ error: {e}")
        return {"USD": None, "CNY": None}

def get_usdt_rub_rapira() -> Optional[float]:
    """USDT/RUB с Rapira (askPrice)"""
    url = "https://api.rapira.net/open/market/rates"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("data", []):
            if item.get("symbol") == "USDT/RUB":
                return float(item.get("askPrice", 0))
        return None
    except Exception as e:
        logging.error(f"Rapira USDT/RUB error: {e}")
        return None

def get_usdt_cny_bybit() -> Optional[float]:
    """USDT/CNY с Bybit"""
    url = "https://api.bybit.com/v5/market/tickers?category=spot&symbol=USDTCNY"
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("retCode") == 0:
                ticker = data["result"]["list"][0]
                return float(ticker["lastPrice"])
        return None
    except Exception as e:
        logging.error(f"Bybit USDT/CNY error: {e}")
        return None

def get_usd_cny_bybit() -> Optional[float]:
    """USD/CNY с Bybit"""
    url = "https://api.bybit.com/v5/market/tickers?category=spot&symbol=USDCNY"
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("retCode") == 0:
                ticker = data["result"]["list"][0]
                return float(ticker["lastPrice"])
        return None
    except Exception as e:
        logging.error(f"Bybit USD/CNY error: {e}")
        return None

# ---------- ПОЛУЧЕНИЕ ВСЕХ КУРСОВ ОДНИМ ЗАПРОСОМ (С КЕШИРОВАНИЕМ) ----------
def get_all_rates(force=False) -> Dict[str, Any]:
    """Возвращает словарь со всеми курсами (RUB/USD, RUB/CNY, RUB/USDT, USDT/CNY, USD/CNY)"""
    now = datetime.now()
    # Проверяем кеш
    if not force and _cache["rates"] and _cache["rates"].get("timestamp") and (now - _cache["rates"]["timestamp"]).seconds < CACHE_TTL:
        return _cache["rates"]

    result = {}
    # 1. ЦБ РФ: USD/RUB и CNY/RUB (нам нужно RUB/USD = 1 / USD/RUB, RUB/CNY = 1 / CNY/RUB)
    cbr = get_cbr_rates()
    if cbr["USD"] and cbr["USD"] > 0:
        result["RUB/USD"] = 1 / cbr["USD"]
    else:
        result["RUB/USD"] = None
    if cbr["CNY"] and cbr["CNY"] > 0:
        result["RUB/CNY"] = 1 / cbr["CNY"]
    else:
        result["RUB/CNY"] = None

    # 2. RUB/USDT – Rapira
    usdt_rub = get_usdt_rub_rapira()
    result["RUB/USDT"] = usdt_rub

    # 3. USDT/CNY – Bybit
    usdt_cny = get_usdt_cny_bybit()
    result["USDT/CNY"] = usdt_cny

    # 4. USD/CNY – Bybit
    usd_cny = get_usd_cny_bybit()
    # если Bybit не дал, пробуем вычислить кросс: (RUB/CNY) / (RUB/USD)
    if usd_cny is None and result["RUB/USD"] and result["RUB/CNY"]:
        usd_cny = result["RUB/CNY"] / result["RUB/USD"]
    result["USD/CNY"] = usd_cny

    result["timestamp"] = now
    _cache["rates"] = result
    return result

# ---------- ФУНКЦИИ ДЛЯ АВТОРАССЫЛКИ ----------
async def send_to_all_users(text: str):
    """Отправляет сообщение всем активным пользователям"""
    users = get_all_users()
    for user in users:
        try:
            await bot.send_message(user["id"], text, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Не удалось отправить пользователю {user['id']}: {e}")

async def send_daily_update():
    """Утренняя рассылка (курсы на текущий момент)"""
    rates = get_all_rates(force=True)
    now = datetime.now().strftime("%d.%m.%Y, %H:%M")
    text = f"🌅 **Доброе утро!**\n\n"
    text += f"📈 **Курсы на {now}**\n"
    text += f"🇷🇺 RUB/USD: **{rates['RUB/USD']:.2f}** ₽\n" if rates['RUB/USD'] else "🇷🇺 RUB/USD: ❌\n"
    text += f"🇷🇺 RUB/USDT: **{rates['RUB/USDT']:.2f}** ₽\n" if rates['RUB/USDT'] else "🇷🇺 RUB/USDT: ❌\n"
    text += f"🇷🇺 RUB/CNY: **{rates['RUB/CNY']:.2f}** ₽\n" if rates['RUB/CNY'] else "🇷🇺 RUB/CNY: ❌\n"
    text += f"🇨🇳 USDT/CNY: **{rates['USDT/CNY']:.2f}** ¥\n" if rates['USDT/CNY'] else "🇨🇳 USDT/CNY: ❌\n"
    text += f"🇺🇸 USD/CNY: **{rates['USD/CNY']:.2f}** ¥\n" if rates['USD/CNY'] else "🇺🇸 USD/CNY: ❌"
    await send_to_all_users(text)

async def send_evening_update():
    """Вечерняя рассылка с изменением за день"""
    # Получаем текущие курсы
    rates = get_all_rates(force=True)
    now = datetime.now().strftime("%d.%m.%Y, %H:%M")
    # Чтобы узнать изменение за день, нужно сохранить утренние курсы.
    # Для простоты сохраним в кеш при утренней рассылке, а здесь сравним.
    # Реализуем через глобальную переменную (или можно хранить в Supabase).
    # Я сделаю простое решение: сохраняем в файл или в переменную.
    # Здесь я добавлю временное решение: будем хранить утренние курсы в файле, если хотите — можно переделать на Supabase.
    # Пока оставим простой вариант: утренние курсы не храним, а показываем только текущие без изменений.
    # Если нужна динамика, можно хранить в Supabase, но для MVP сойдёт.
    text = f"🌆 **Вечерний отчёт**\n\n"
    text += f"📈 **Курсы на {now}**\n"
    text += f"🇷🇺 RUB/USD: **{rates['RUB/USD']:.2f}** ₽\n" if rates['RUB/USD'] else "🇷🇺 RUB/USD: ❌\n"
    text += f"🇷🇺 RUB/USDT: **{rates['RUB/USDT']:.2f}** ₽\n" if rates['RUB/USDT'] else "🇷🇺 RUB/USDT: ❌\n"
    text += f"🇷🇺 RUB/CNY: **{rates['RUB/CNY']:.2f}** ₽\n" if rates['RUB/CNY'] else "🇷🇺 RUB/CNY: ❌\n"
    text += f"🇨🇳 USDT/CNY: **{rates['USDT/CNY']:.2f}** ¥\n" if rates['USDT/CNY'] else "🇨🇳 USDT/CNY: ❌\n"
    text += f"🇺🇸 USD/CNY: **{rates['USD/CNY']:.2f}** ¥\n" if rates['USD/CNY'] else "🇺🇸 USD/CNY: ❌"
    await send_to_all_users(text)

# ---------- ПРОВЕРКА АЛЕРТОВ ----------
async def check_alerts():
    """Проверяет все активные алерты и отправляет уведомления админу, если достигнуты"""
    alerts = get_alerts()
    if not alerts:
        return
    rates = get_all_rates(force=True)
    for alert in alerts:
        pair = alert["pair"]
        threshold = alert["threshold"]
        direction = alert["direction"]
        rate = rates.get(pair)
        if rate is None:
            continue
        triggered = False
        if direction == "above" and rate >= threshold:
            triggered = True
        elif direction == "below" and rate <= threshold:
            triggered = True
        if triggered:
            msg = f"🔔 **Алерт!**\nПара: {pair}\nТекущий курс: {rate:.2f}\nПорог: {threshold:.2f} ({direction})"
            await bot.send_message(ADMIN_ID, msg)
            # После срабатывания деактивируем алерт, чтобы не спамить
            await remove_alert(alert["id"])

# ---------- КОМАНДЫ БОТА ----------
@dp.message(Command("start"))
async def start_cmd(message: Message):
    user_id = message.from_user.id
    # Проверяем, авторизован ли пользователь
    user = get_user(user_id)
    if user:
        # Уже авторизован
        await message.answer(
            "👋 Привет! Ты уже авторизован.\n"
            "Используй команды:\n"
            "/kurs – показать все курсы\n"
            "/convert <сумма> <из_валюты> <в_валюту> – конвертация\n"
            "/help – справка"
        )
        return
    # Не авторизован, запрашиваем пароль
    await message.answer("🔐 **Введите 4-значный пароль для доступа к боту:**", parse_mode="Markdown")
    # Устанавливаем состояние ожидания пароля
    waiting_for_password[user_id] = True

# Словарь для ожидания пароля
waiting_for_password = {}

@dp.message(F.text)
async def handle_text(message: Message):
    user_id = message.from_user.id
    if user_id in waiting_for_password:
        # Проверяем пароль
        entered = message.text.strip()
        stored_hash = get_password_hash()
        if hashlib.sha256(entered.encode()).hexdigest() == stored_hash:
            # Пароль верный
            del waiting_for_password[user_id]
            add_user(user_id, message.from_user.username or "")
            await message.answer("✅ **Доступ разрешён!**\n\n"
                                 "Теперь ты можешь использовать команды:\n"
                                 "/kurs – показать все курсы\n"
                                 "/convert <сумма> <из_валюты> <в_валюту> – конвертация\n"
                                 "/help – справка", parse_mode="Markdown")
        else:
            await message.answer("❌ **Неверный пароль. Попробуйте ещё раз.**", parse_mode="Markdown")
        return

    # Остальные текстовые сообщения игнорируем (или можно дать подсказку)
    await message.answer("Используйте команды из меню или /help")

@dp.message(Command("kurs"))
async def kurs_cmd(message: Message):
    if not get_user(message.from_user.id):
        await message.answer("⛔ Доступ запрещён. Используйте /start для авторизации.")
        return
    rates = get_all_rates(force=True)
    now = datetime.now().strftime("%d.%m.%Y, %H:%M")
    text = f"📈 **Курсы на {now}**\n\n"
    text += f"🇷🇺 RUB/USD: **{rates['RUB/USD']:.2f}** ₽\n" if rates['RUB/USD'] else "🇷🇺 RUB/USD: ❌\n"
    text += f"🇷🇺 RUB/USDT: **{rates['RUB/USDT']:.2f}** ₽\n" if rates['RUB/USDT'] else "🇷🇺 RUB/USDT: ❌\n"
    text += f"🇷🇺 RUB/CNY: **{rates['RUB/CNY']:.2f}** ₽\n" if rates['RUB/CNY'] else "🇷🇺 RUB/CNY: ❌\n"
    text += f"🇨🇳 USDT/CNY: **{rates['USDT/CNY']:.2f}** ¥\n" if rates['USDT/CNY'] else "🇨🇳 USDT/CNY: ❌\n"
    text += f"🇺🇸 USD/CNY: **{rates['USD/CNY']:.2f}** ¥\n" if rates['USD/CNY'] else "🇺🇸 USD/CNY: ❌"
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("convert"))
async def convert_cmd(message: Message):
    if not get_user(message.from_user.id):
        await message.answer("⛔ Доступ запрещён. Используйте /start для авторизации.")
        return
    args = message.text.split()
    if len(args) != 4:
        await message.answer("❌ Используйте: `/convert <сумма> <из_валюты> <в_валюту>`\n"
                             "Пример: `/convert 1000 RUB USD`\n"
                             "Поддерживаемые валюты: RUB, USD, USDT, CNY", parse_mode="Markdown")
        return
    try:
        amount = float(args[1].replace(',', '.'))
        from_cur = args[2].upper()
        to_cur = args[3].upper()
        if amount <= 0:
            raise ValueError
    except:
        await message.answer("❌ Введите корректное положительное число.")
        return

    # Получаем все курсы
    rates = get_all_rates(force=True)
    # Строим кросс-курс: сначала получить значение из_валюты в RUB, затем перевести в целевую
    # Для простоты будем использовать прямые пары, где возможно.
    # Сделаем маппинг: RUB -> RUB (1), USD -> RUB (через RUB/USD), USDT -> RUB (через RUB/USDT), CNY -> RUB (через RUB/CNY)
    # Затем из RUB в целевую.
    rub_from = None
    if from_cur == "RUB":
        rub_from = amount
    elif from_cur == "USD" and rates["RUB/USD"]:
        rub_from = amount * rates["RUB/USD"]
    elif from_cur == "USDT" and rates["RUB/USDT"]:
        rub_from = amount * rates["RUB/USDT"]
    elif from_cur == "CNY" and rates["RUB/CNY"]:
        rub_from = amount * rates["RUB/CNY"]
    else:
        await message.answer(f"❌ Не могу конвертировать {from_cur} в RUB. Проверьте курс.")
        return

    if rub_from is None:
        await message.answer(f"❌ Не удалось получить курс для {from_cur}.")
        return

    # Теперь из RUB в целевую
    result = None
    if to_cur == "RUB":
        result = rub_from
    elif to_cur == "USD" and rates["RUB/USD"]:
        result = rub_from / rates["RUB/USD"]
    elif to_cur == "USDT" and rates["RUB/USDT"]:
        result = rub_from / rates["RUB/USDT"]
    elif to_cur == "CNY" and rates["RUB/CNY"]:
        result = rub_from / rates["RUB/CNY"]
    else:
        await message.answer(f"❌ Не могу конвертировать RUB в {to_cur}. Проверьте курс.")
        return

    if result is None:
        await message.answer(f"❌ Не удалось получить курс для {to_cur}.")
        return

    await message.answer(f"💱 **{amount:.2f} {from_cur} = {result:.2f} {to_cur}**\n"
                         f"🕐 {datetime.now().strftime('%d.%m.%Y, %H:%M')}",
                         parse_mode="Markdown")

@dp.message(Command("help"))
async def help_cmd(message: Message):
    if not get_user(message.from_user.id):
        await message.answer("⛔ Доступ запрещён. Используйте /start для авторизации.")
        return
    await message.answer(
        "📋 **Доступные команды:**\n\n"
        "/kurs – показать все курсы\n"
        "/convert <сумма> <из_валюты> <в_валюту> – конвертация\n"
        "/help – эта справка\n\n"
        "Поддерживаемые валюты: RUB, USD, USDT, CNY"
    )

# ---------- АДМИН-КОМАНДЫ ----------
@dp.message(Command("set_password"))
async def set_password_cmd(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только для администратора.")
        return
    args = message.text.split()
    if len(args) != 2:
        await message.answer("❌ Пример: `/set_password 5678`", parse_mode="Markdown")
        return
    new_pass = args[1].strip()
    if len(new_pass) != 4 or not new_pass.isdigit():
        await message.answer("❌ Пароль должен быть ровно 4 цифры.")
        return
    if set_password_hash(new_pass):
        await message.answer(f"✅ Пароль изменён на `{new_pass}`")
    else:
        await message.answer("❌ Ошибка при смене пароля.")

@dp.message(Command("add_user"))
async def add_user_cmd(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только для администратора.")
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Пример: `/add_user 123456789`", parse_mode="Markdown")
        return
    try:
        user_id = int(args[1])
    except:
        await message.answer("❌ Укажите числовой ID.")
        return
    if add_user(user_id):
        await message.answer(f"✅ Пользователь {user_id} добавлен.")
    else:
        await message.answer("❌ Ошибка при добавлении пользователя.")

@dp.message(Command("remove_user"))
async def remove_user_cmd(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только для администратора.")
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Пример: `/remove_user 123456789`", parse_mode="Markdown")
        return
    try:
        user_id = int(args[1])
    except:
        await message.answer("❌ Укажите числовой ID.")
        return
    if remove_user(user_id):
        await message.answer(f"✅ Пользователь {user_id} удалён.")
    else:
        await message.answer("❌ Ошибка при удалении пользователя.")

@dp.message(Command("list_users"))
async def list_users_cmd(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только для администратора.")
        return
    users = get_all_users()
    if not users:
        await message.answer("Список пользователей пуст.")
        return
    text = "👥 **Авторизованные пользователи:**\n"
    for u in users:
        text += f"ID: {u['id']}, Username: {u.get('username', '—')}\n"
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("add_alert"))
async def add_alert_cmd(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только для администратора.")
        return
    args = message.text.split()
    if len(args) != 4:
        await message.answer("❌ Пример: `/add_alert RUB/USD 80 above`\n"
                             "Доступные пары: RUB/USD, RUB/USDT, RUB/CNY, USDT/CNY, USD/CNY\n"
                             "Направление: above или below", parse_mode="Markdown")
        return
    pair = args[1]
    try:
        threshold = float(args[2].replace(',', '.'))
    except:
        await message.answer("❌ Неверное значение порога.")
        return
    direction = args[3].lower()
    if direction not in ("above", "below"):
        await message.answer("❌ Направление должно быть 'above' или 'below'.")
        return
    if add_alert(pair, threshold, direction):
        await message.answer(f"✅ Алерт для {pair} установлен: {direction} {threshold:.2f}")
    else:
        await message.answer("❌ Ошибка при создании алерта.")

@dp.message(Command("list_alerts"))
async def list_alerts_cmd(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только для администратора.")
        return
    alerts = get_alerts()
    if not alerts:
        await message.answer("Нет активных алертов.")
        return
    text = "🔔 **Активные алерты:**\n"
    for a in alerts:
        text += f"ID: {a['id']}, {a['pair']} {a['direction']} {a['threshold']:.2f}\n"
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("remove_alert"))
async def remove_alert_cmd(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только для администратора.")
        return
    args = message.text.split()
    if len(args) != 2:
        await message.answer("❌ Пример: `/remove_alert 1`", parse_mode="Markdown")
        return
    try:
        alert_id = int(args[1])
    except:
        await message.answer("❌ Укажите числовой ID.")
        return
    if remove_alert(alert_id):
        await message.answer(f"✅ Алерт {alert_id} удалён.")
    else:
        await message.answer("❌ Ошибка при удалении алерта.")

# ---------- ЗАПУСК ----------
async def main():
    await bot.set_my_commands([
        BotCommand(command="kurs", description="📈 Все курсы"),
        BotCommand(command="convert", description="💱 Конвертировать валюты"),
        BotCommand(command="help", description="❓ Справка")
    ])
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())