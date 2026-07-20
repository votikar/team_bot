import asyncio
import hashlib
import logging
import os
import re
from datetime import datetime

import requests
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import BotCommand, Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram import F
from supabase import create_client, Client

# ---------- Переменные окружения ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("SUPABASE_URL или SUPABASE_KEY не заданы")

# ---------- Supabase ----------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ---------- Кеш курсов ----------
_cache = {
    "usd_rub": None,
    "usdt_rub": None,
    "cny_rub": None,
    "usd_cny": None,
    "timestamp": None,
}
CACHE_TTL = 30

# ---------- Логирование ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- Бот ----------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ---------- Вспомогательные функции для Supabase ----------
def get_password_hash() -> str:
    try:
        resp = supabase.table("settings").select("value").eq("key", "access_password").execute()
        if resp.data and len(resp.data) > 0:
            return resp.data[0]["value"]
        default_hash = hashlib.sha256("1234".encode()).hexdigest()
        supabase.table("settings").insert({"key": "access_password", "value": default_hash}).execute()
        return default_hash
    except Exception as e:
        logger.error(f"get_password_hash error: {e}")
        return hashlib.sha256("1234".encode()).hexdigest()

def set_password_hash(new_password: str) -> bool:
    try:
        new_hash = hashlib.sha256(new_password.encode()).hexdigest()
        supabase.table("settings").update({"value": new_hash}).eq("key", "access_password").execute()
        return True
    except Exception as e:
        logger.error(f"set_password_hash error: {e}")
        return False

def get_user(telegram_id: int):
    try:
        resp = supabase.table("users").select("*").eq("id", telegram_id).execute()
        return resp.data[0] if resp.data else None
    except Exception as e:
        logger.error(f"get_user error: {e}")
        return None

def add_user(telegram_id: int, username: str = ""):
    try:
        supabase.table("users").insert({"id": telegram_id, "username": username}).execute()
        return True
    except Exception as e:
        logger.error(f"add_user error: {e}")
        return False

def remove_user(telegram_id: int):
    try:
        supabase.table("users").delete().eq("id", telegram_id).execute()
        return True
    except Exception as e:
        logger.error(f"remove_user error: {e}")
        return False

def get_all_users():
    try:
        resp = supabase.table("users").select("*").execute()
        return resp.data
    except Exception as e:
        logger.error(f"get_all_users error: {e}")
        return []

def get_today_deltas():
    today = datetime.now().strftime("%Y-%m-%d")
    # Сначала пробуем переменные окружения
    env_deltas = {
        "usd_rub": os.environ.get("DELTA_USD_RUB"),
        "usdt_rub": os.environ.get("DELTA_USDT_RUB"),
        "cny_rub": os.environ.get("DELTA_CNY_RUB"),
        "usd_cny": os.environ.get("DELTA_USD_CNY"),
    }
    if any(v is not None for v in env_deltas.values()):
        try:
            resp = supabase.table("deltas").select("*").eq("date", today).execute()
            if resp.data and len(resp.data) > 0:
                base = resp.data[0]
            else:
                base = {"usd_rub": 0.0, "usdt_rub": 0.0, "cny_rub": 0.0, "usd_cny": 0.0}
        except:
            base = {"usd_rub": 0.0, "usdt_rub": 0.0, "cny_rub": 0.0, "usd_cny": 0.0}
        return {
            "date": today,
            "usd_rub": float(env_deltas["usd_rub"]) if env_deltas["usd_rub"] is not None else base["usd_rub"],
            "usdt_rub": float(env_deltas["usdt_rub"]) if env_deltas["usdt_rub"] is not None else base["usdt_rub"],
            "cny_rub": float(env_deltas["cny_rub"]) if env_deltas["cny_rub"] is not None else base["cny_rub"],
            "usd_cny": float(env_deltas["usd_cny"]) if env_deltas["usd_cny"] is not None else base["usd_cny"],
        }

    try:
        resp = supabase.table("deltas").select("*").eq("date", today).execute()
        if resp.data and len(resp.data) > 0:
            return resp.data[0]
        default = {
            "date": today,
            "usd_rub": 0.0,
            "usdt_rub": 0.0,
            "cny_rub": 0.0,
            "usd_cny": 0.0,
        }
        supabase.table("deltas").insert(default).execute()
        return default
    except Exception as e:
        logger.error(f"get_today_deltas error: {e}")
        return {"date": today, "usd_rub": 0.0, "usdt_rub": 0.0, "cny_rub": 0.0, "usd_cny": 0.0}

def update_delta(pair: str, value: float) -> bool:
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        resp = supabase.table("deltas").select("*").eq("date", today).execute()
        if resp.data and len(resp.data) > 0:
            supabase.table("deltas").update({pair: value}).eq("date", today).execute()
        else:
            default = {
                "date": today,
                "usd_rub": 0.0,
                "usdt_rub": 0.0,
                "cny_rub": 0.0,
                "usd_cny": 0.0,
                pair: value,
            }
            supabase.table("deltas").insert(default).execute()
        return True
    except Exception as e:
        logger.error(f"update_delta error: {e}")
        return False

# ---------- Получение курсов ----------
def get_usd_rub_rate(force=False):
    now = datetime.now()
    if not force and _cache["timestamp"] and (now - _cache["timestamp"]).seconds < CACHE_TTL:
        if _cache["usd_rub"] is not None:
            return _cache["usd_rub"]
    url = "https://www.cbr-xml-daily.ru/daily_json.js"
    try:
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        rate = data["Valute"]["USD"]["Value"]
        _cache["usd_rub"] = rate
        _cache["timestamp"] = now
        logger.info(f"USD/RUB: {rate}")
        return rate
    except Exception as e:
        logger.error(f"USD/RUB error: {e}")
        return None

def get_usdt_rub_rate(force=False):
    now = datetime.now()
    if not force and _cache["timestamp"] and (now - _cache["timestamp"]).seconds < CACHE_TTL:
        if _cache["usdt_rub"] is not None:
            return _cache["usdt_rub"]
    url = "https://api.rapira.net/open/market/rates"
    try:
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("data", []):
            if item.get("symbol") == "USDT/RUB":
                rate = float(item.get("askPrice", 0))
                _cache["usdt_rub"] = rate
                _cache["timestamp"] = now
                logger.info(f"USDT/RUB: {rate}")
                return rate
        return None
    except Exception as e:
        logger.error(f"USDT/RUB error: {e}")
        return None

def get_cny_rub_rate(force=False):
    now = datetime.now()
    if not force and _cache["timestamp"] and (now - _cache["timestamp"]).seconds < CACHE_TTL:
        if _cache["cny_rub"] is not None:
            return _cache["cny_rub"]
    url = "https://www.cbr-xml-daily.ru/daily_json.js"
    try:
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        rate = data["Valute"]["CNY"]["Value"]
        _cache["cny_rub"] = rate
        _cache["timestamp"] = now
        logger.info(f"CNY/RUB: {rate}")
        return rate
    except Exception as e:
        logger.error(f"CNY/RUB error: {e}")
        return None

def get_usd_cny_rate(force=False):
    now = datetime.now()
    if not force and _cache["timestamp"] and (now - _cache["timestamp"]).seconds < CACHE_TTL:
        if _cache["usd_cny"] is not None:
            return _cache["usd_cny"]
    try:
        url = "https://api.bybit.com/v5/market/tickers?category=spot&symbol=USDCNY"
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("retCode") == 0:
                rate = float(data["result"]["list"][0]["lastPrice"])
                _cache["usd_cny"] = rate
                _cache["timestamp"] = now
                logger.info(f"USD/CNY from Bybit: {rate}")
                return rate
    except Exception as e:
        logger.warning(f"Bybit USD/CNY failed: {e}")
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=usd&vs_currencies=cny"
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            rate = float(resp.json()["usd"]["cny"])
            _cache["usd_cny"] = rate
            _cache["timestamp"] = now
            logger.info(f"USD/CNY from CoinGecko: {rate}")
            return rate
    except Exception as e:
        logger.warning(f"CoinGecko USD/CNY failed: {e}")
    return None

# ---------- Формирование текста курсов ----------
def format_course_text():
    usd_rub = get_usd_rub_rate()
    usdt_rub = get_usdt_rub_rate()
    cny_rub = get_cny_rub_rate()
    usd_cny = get_usd_cny_rate()
    deltas = get_today_deltas()
    today = datetime.now().strftime("%d.%m.%Y")

    if usd_rub is None:
        return "❌ Не удалось получить курсы. Попробуйте позже."

    text = f"💰 **Курсы на {today}**\n\n"
    text += f"🇺🇸 USD/RUB: **{usd_rub:.2f}** ₽\n"
    text += f"🪙 USDT/RUB: **{usdt_rub:.2f}** ₽\n" if usdt_rub is not None else "🪙 USDT/RUB: ❌\n"
    text += f"🇨🇳 CNY/RUB: **{cny_rub:.2f}** ₽\n" if cny_rub is not None else "🇨🇳 CNY/RUB: ❌\n"
    text += f"🇺🇸 USD/CNY: **{usd_cny:.2f}** ¥\n" if usd_cny is not None else "🇺🇸 USD/CNY: ❌\n"

    text += f"\n📌 **Дельта на сегодня ({today}):**\n"
    text += f"USD/RUB: **{deltas['usd_rub']:.2f}** ₽\n"
    text += f"USDT/RUB: **{deltas['usdt_rub']:.2f}** ₽\n"
    text += f"CNY/RUB: **{deltas['cny_rub']:.2f}** ₽\n"
    text += f"USD/CNY: **{deltas['usd_cny']:.2f}** ¥\n"

    text += "\n📡 **Источники:** USD/RUB — ЦБ РФ, USDT/RUB — Rapira, CNY/RUB — ЦБ РФ, USD/CNY — Bybit/CoinGecko"
    return text

# ---------- Конвертация ----------
def convert_generic(amount, rate, delta, is_buy):
    effective_rate = rate + delta if is_buy else rate - delta
    if is_buy:
        return amount / effective_rate, effective_rate
    else:
        return amount * effective_rate, effective_rate

# ---------- Клавиатуры ----------
def main_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить курс", callback_data="refresh")],
        [InlineKeyboardButton(text="💱 Конвертировать", callback_data="convert")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
    ])

def convert_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="RUB → USD", callback_data="conv_RUB_USD"),
         InlineKeyboardButton(text="USD → RUB", callback_data="conv_USD_RUB")],
        [InlineKeyboardButton(text="RUB → USDT", callback_data="conv_RUB_USDT"),
         InlineKeyboardButton(text="USDT → RUB", callback_data="conv_USDT_RUB")],
        [InlineKeyboardButton(text="RUB → CNY", callback_data="conv_RUB_CNY"),
         InlineKeyboardButton(text="CNY → RUB", callback_data="conv_CNY_RUB")],
        [InlineKeyboardButton(text="USD → CNY", callback_data="conv_USD_CNY"),
         InlineKeyboardButton(text="CNY → USD", callback_data="conv_CNY_USD")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_course")]
    ])

# ---------- Обработчики команд ----------
waiting_for = {}

@dp.message(Command("start"))
async def start_cmd(message: Message):
    user_id = message.from_user.id
    user = get_user(user_id)
    if not user:
        await message.answer("🔐 Введите пароль для доступа к боту:")
        waiting_for[user_id] = "waiting_password"
        return
    await message.answer(
        f"🏦 Добро пожаловать, сотрудник!\n\n{format_course_text()}",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown"
    )

@dp.message(Command("course"))
async def course_cmd(message: Message):
    user_id = message.from_user.id
    if not get_user(user_id):
        await message.answer("⛔ Доступ запрещён. Используйте /start для авторизации.")
        return
    await message.answer(
        format_course_text(),
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )

@dp.message(Command("convert"))
async def convert_cmd(message: Message):
    user_id = message.from_user.id
    if not get_user(user_id):
        await message.answer("⛔ Доступ запрещён. Используйте /start для авторизации.")
        return
    await message.answer("Выберите направление конвертации:", reply_markup=convert_menu_keyboard())

@dp.message(Command("help"))
async def help_cmd(message: Message):
    user_id = message.from_user.id
    if not get_user(user_id):
        await message.answer("⛔ Доступ запрещён. Используйте /start для авторизации.")
        return
    await message.answer(
        "📋 **Доступные команды:**\n"
        "/start – Главное меню\n"
        "/course – Показать курсы и дельты\n"
        "/convert – Открыть меню конвертации\n"
        "/help – Эта справка\n\n"
        "💡 При конвертации можно указать индивидуальную дельту:\n"
        "Введите сумму и дельту через пробел, например:\n"
        "`1000000 1.50`"
    )

# ---------- Админ-команды ----------
@dp.message(Command("set_delta_USD_RUB"))
async def set_delta_usd_rub(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только для администратора.")
        return
    args = message.text.split()
    if len(args) != 2:
        await message.answer("❌ Пример: `/set_delta_USD_RUB 0.10`")
        return
    try:
        val = float(args[1].replace(',', '.'))
        if update_delta("usd_rub", val):
            await message.answer(f"✅ Дельта USD/RUB установлена: {val:.2f}")
        else:
            await message.answer("❌ Ошибка при сохранении дельты.")
    except:
        await message.answer("❌ Введите корректное число.")

@dp.message(Command("set_delta_USDT_RUB"))
async def set_delta_usdt_rub(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только для администратора.")
        return
    args = message.text.split()
    if len(args) != 2:
        await message.answer("❌ Пример: `/set_delta_USDT_RUB 0.35`")
        return
    try:
        val = float(args[1].replace(',', '.'))
        if update_delta("usdt_rub", val):
            await message.answer(f"✅ Дельта USDT/RUB установлена: {val:.2f}")
        else:
            await message.answer("❌ Ошибка при сохранении дельты.")
    except:
        await message.answer("❌ Введите корректное число.")

@dp.message(Command("set_delta_CNY_RUB"))
async def set_delta_cny_rub(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только для администратора.")
        return
    args = message.text.split()
    if len(args) != 2:
        await message.answer("❌ Пример: `/set_delta_CNY_RUB 0.00`")
        return
    try:
        val = float(args[1].replace(',', '.'))
        if update_delta("cny_rub", val):
            await message.answer(f"✅ Дельта CNY/RUB установлена: {val:.2f}")
        else:
            await message.answer("❌ Ошибка при сохранении дельты.")
    except:
        await message.answer("❌ Введите корректное число.")

@dp.message(Command("set_delta_USD_CNY"))
async def set_delta_usd_cny(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только для администратора.")
        return
    args = message.text.split()
    if len(args) != 2:
        await message.answer("❌ Пример: `/set_delta_USD_CNY 0.05`")
        return
    try:
        val = float(args[1].replace(',', '.'))
        if update_delta("usd_cny", val):
            await message.answer(f"✅ Дельта USD/CNY установлена: {val:.2f}")
        else:
            await message.answer("❌ Ошибка при сохранении дельты.")
    except:
        await message.answer("❌ Введите корректное число.")

@dp.message(Command("show_deltas"))
async def show_deltas(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только для администратора.")
        return
    deltas = get_today_deltas()
    today = datetime.now().strftime("%d.%m.%Y")
    text = f"📊 **Дельта на {today}**\n\n"
    text += f"USD/RUB: {deltas['usd_rub']:.2f} ₽\n"
    text += f"USDT/RUB: {deltas['usdt_rub']:.2f} ₽\n"
    text += f"CNY/RUB: {deltas['cny_rub']:.2f} ₽\n"
    text += f"USD/CNY: {deltas['usd_cny']:.2f} ¥"
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("set_password"))
async def set_password(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только для администратора.")
        return
    args = message.text.split()
    if len(args) != 2:
        await message.answer("❌ Пример: `/set_password 5678`")
        return
    new_pass = args[1].strip()
    if len(new_pass) < 4:
        await message.answer("❌ Пароль должен быть не менее 4 символов.")
        return
    if set_password_hash(new_pass):
        await message.answer(f"✅ Пароль изменён на `{new_pass}`")
        try:
            supabase.table("users").delete().neq("id", 0).execute()
            await message.answer("⚠️ Все пользователи были удалены. Теперь они должны заново ввести пароль.")
        except Exception as e:
            logger.error(f"Ошибка при удалении пользователей: {e}")
    else:
        await message.answer("❌ Ошибка при смене пароля.")

@dp.message(Command("add_user"))
async def add_user(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только для администратора.")
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Пример: `/add_user 123456789`")
        return
    try:
        new_id = int(args[1])
        if add_user(new_id):
            await message.answer(f"✅ Пользователь {new_id} добавлен.")
        else:
            await message.answer("❌ Ошибка при добавлении пользователя.")
    except:
        await message.answer("❌ Укажите числовой ID.")

@dp.message(Command("remove_user"))
async def remove_user(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только для администратора.")
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Пример: `/remove_user 123456789`")
        return
    try:
        new_id = int(args[1])
        if remove_user(new_id):
            await message.answer(f"✅ Пользователь {new_id} удалён.")
        else:
            await message.answer("❌ Ошибка при удалении пользователя.")
    except:
        await message.answer("❌ Укажите числовой ID.")

@dp.message(Command("list_users"))
async def list_users(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только для администратора.")
        return
    users = get_all_users()
    if not users:
        await message.answer("Список пользователей пуст.")
        return
    text = "👥 **Пользователи:**\n"
    for u in users:
        text += f"ID: {u['id']}, Username: {u.get('username', '—')}\n"
    await message.answer(text, parse_mode="Markdown")

# ---------- Обработка текста (новая логика пароля) ----------
@dp.message(F.text)
async def handle_text(message: Message):
    user_id = message.from_user.id
    text = message.text.strip()

    # ---- 1. Если пользователь НЕ авторизован, любой ввод проверяем как пароль ----
    if not get_user(user_id):
        stored_hash = get_password_hash()
        if hashlib.sha256(text.encode()).hexdigest() == stored_hash:
            # Пароль верный
            add_user(user_id, message.from_user.username or "")
            await message.answer(
                f"✅ Доступ разрешён!\n\n{format_course_text()}",
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard()
            )
            if user_id in waiting_for:
                del waiting_for[user_id]
            return
        else:
            # Неверный пароль – даём подсказку
            await message.answer("❌ Неверный пароль. Попробуйте ещё раз или введите /start для начала.")
            return

    # ---- 2. Если пользователь авторизован ----
    # Если он ожидал пароль (маловероятно, но на всякий случай)
    if user_id in waiting_for and waiting_for[user_id] == "waiting_password":
        del waiting_for[user_id]

    # Проверяем, ожидаем ли мы ввод для конвертации
    if user_id not in waiting_for:
        await message.answer("Сначала выберите направление конвертации через /convert.")
        return

    conv_type = waiting_for.get(user_id)
    if not conv_type or not conv_type.startswith("conv_"):
        await message.answer("Сначала выберите направление конвертации через /convert.")
        return

    # Парсим "сумма" или "сумма дельта"
    parts = text.split()
    if len(parts) == 2:
        try:
            amount = float(parts[0].replace(',', '.'))
            custom_delta = float(parts[1].replace(',', '.'))
            if amount <= 0:
                raise ValueError
        except:
            await message.answer("❌ Введите корректные числа: сумма и дельта, например `1000000 1.50`")
            return
        use_custom_delta = True
    elif len(parts) == 1:
        try:
            amount = float(parts[0].replace(',', '.'))
            if amount <= 0:
                raise ValueError
        except:
            await message.answer("❌ Введите положительное число.")
            return
        custom_delta = None
        use_custom_delta = False
    else:
        await message.answer("❌ Введите сумму, либо сумму и дельту через пробел, например `1000000 1.50`")
        return

    # Убираем ожидание
    waiting_for.pop(user_id, None)

    # Определяем направление
    from_cur, to_cur = conv_type.split('_')[1], conv_type.split('_')[2]
    is_buy = from_cur == "RUB"  # покупка (RUB → X) или продажа (X → RUB)

    # Получаем курс и дельту
    if from_cur == "USD" or to_cur == "USD":
        rate = get_usd_rub_rate()
        delta_key = "usd_rub"
    elif from_cur == "USDT" or to_cur == "USDT":
        rate = get_usdt_rub_rate()
        delta_key = "usdt_rub"
    elif from_cur == "CNY" or to_cur == "CNY":
        rate = get_cny_rub_rate()
        delta_key = "cny_rub"
    else:
        await message.answer("❌ Неизвестная валюта.")
        return

    if rate is None:
        await message.answer("❌ Не удалось получить курс. Попробуйте позже.")
        return

    deltas = get_today_deltas()
    standard_delta = deltas.get(delta_key, 0.0)
    delta_used = custom_delta if use_custom_delta else standard_delta

    # Анимация
    loading_msg = await message.answer("⏳ Конвертирую...")

    # Расчёт
    if is_buy:
        result = amount / (rate + delta_used)
        result_without = amount / rate
        result_with = amount / (rate + standard_delta)
    else:
        result = amount * (rate - delta_used)
        result_without = amount * rate
        result_with = amount * (rate - standard_delta)

    if result is None:
        await loading_msg.edit_text("❌ Не удалось выполнить конвертацию.")
        return

    # Формируем ответ
    result_text = f"💱 **Результат конвертации {amount:.2f} {from_cur}**\n\n"
    if use_custom_delta:
        result_text += f"🔹 **Без дельты:** {result_without:.4f} {to_cur}\n"
        result_text += f"🔸 **С вашей дельтой ({delta_used:.2f}):** {result:.4f} {to_cur}\n"
        result_text += f"📌 Стандартная дельта на сегодня: {standard_delta:.2f}\n"
        profit_abs = result_without - result if is_buy else result - result_without
        profit_percent = (profit_abs / result_without * 100) if result_without != 0 else 0
        result_text += f"💰 Прибыль от вашей дельты: {profit_abs:.4f} {to_cur} ({profit_percent:.2f}%)"
    else:
        result_text += f"🔹 **Без дельты:** {result_without:.4f} {to_cur}\n"
        result_text += f"🔸 **С дельтой ({standard_delta:.2f}):** {result_with:.4f} {to_cur}\n"
        profit_abs = result_without - result_with if is_buy else result_with - result_without
        profit_percent = (profit_abs / result_without * 100) if result_without != 0 else 0
        result_text += f"💰 Прибыль: {profit_abs:.4f} {to_cur} ({profit_percent:.2f}%)"

    await loading_msg.edit_text(result_text, parse_mode="Markdown")
    await message.answer("🏠 Вернуться в главное меню:", reply_markup=main_menu_keyboard())

# ---------- Коллбэки ----------
@dp.callback_query(F.data == "refresh")
async def refresh_cb(callback: CallbackQuery):
    await callback.answer("Обновляю...")
    get_usd_rub_rate(force=True)
    get_usdt_rub_rate(force=True)
    get_cny_rub_rate(force=True)
    get_usd_cny_rate(force=True)
    await callback.message.answer(
        format_course_text(),
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )

@dp.callback_query(F.data == "back_to_course")
async def back_cb(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        format_course_text(),
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )

@dp.callback_query(F.data == "main_menu")
async def main_menu_cb(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        f"🏦 Главное меню\n\n{format_course_text()}",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )

@dp.callback_query(F.data == "convert")
async def convert_cb(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("Выберите направление конвертации:", reply_markup=convert_menu_keyboard())

@dp.callback_query(F.data.startswith("conv_"))
async def conv_choice_cb(callback: CallbackQuery):
    await callback.answer()
    pair = callback.data.split("_")[1:]
    if len(pair) != 2:
        await callback.message.answer("Ошибка.")
        return
    from_cur, to_cur = pair
    conv_key = f"conv_{from_cur}_{to_cur}"
    waiting_for[callback.from_user.id] = conv_key
    await callback.message.answer(
        f"💱 Введите сумму в {from_cur}:\n"
        "Можно указать дельту через пробел, например:\n"
        "`1000000 1.50`"
    )

# ---------- Запуск ----------
async def main():
    await bot.set_my_commands([
        BotCommand(command="start", description="🏦 Главное меню"),
        BotCommand(command="course", description="💰 Курсы и дельты"),
        BotCommand(command="convert", description="💱 Конвертация валют"),
        BotCommand(command="help", description="❓ Помощь")
    ])
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())