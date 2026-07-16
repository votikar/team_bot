import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Optional, List, Dict, Any

import requests
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import BotCommand, Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram import F
from supabase import create_client, Client

# ---------- НАСТРОЙКИ (читаем из переменных окружения) ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан в переменных окружения")

ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Не заданы SUPABASE_URL или SUPABASE_KEY")

# ---------- ПОДКЛЮЧЕНИЕ К SUPABASE ----------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ---------- КЕШ ДЛЯ КУРСОВ ----------
_cache = {"rates": {}, "last_cny": None}
CACHE_TTL = 60

# ---------- ЛОГИРОВАНИЕ ----------
logging.basicConfig(level=logging.INFO)

# ---------- ИНИЦИАЛИЗАЦИЯ БОТА ----------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ---------- ФУНКЦИИ ДЛЯ РАБОТЫ С ПОЛЬЗОВАТЕЛЯМИ (без пароля) ----------
def get_user(telegram_id: int) -> Optional[Dict]:
    """Проверяет, есть ли пользователь в БД (для статистики, не для авторизации)"""
    try:
        resp = supabase.table("users").select("*").eq("id", telegram_id).execute()
        if resp.data and len(resp.data) > 0:
            return resp.data[0]
        return None
    except Exception as e:
        logging.error(f"Supabase get_user error: {e}")
        return None

def add_user(telegram_id: int, username: str = "") -> bool:
    """Добавляет пользователя в БД (для статистики)"""
    try:
        # Проверяем, есть ли уже
        if get_user(telegram_id):
            return True
        supabase.table("users").insert({"id": telegram_id, "username": username}).execute()
        return True
    except Exception as e:
        logging.error(f"Supabase add_user error: {e}")
        return False

def remove_user(telegram_id: int) -> bool:
    """Удаляет пользователя из БД (только админ)"""
    try:
        supabase.table("users").delete().eq("id", telegram_id).execute()
        return True
    except Exception as e:
        logging.error(f"Supabase remove_user error: {e}")
        return False

def get_all_users() -> List[Dict]:
    """Возвращает всех пользователей (только админ)"""
    try:
        resp = supabase.table("users").select("*").execute()
        return resp.data
    except Exception as e:
        logging.error(f"Supabase get_all_users error: {e}")
        return []

# ---------- ФУНКЦИИ ДЛЯ АЛЕРТОВ (без изменений) ----------
def get_alerts() -> List[Dict]:
    try:
        resp = supabase.table("alerts").select("*").eq("is_active", True).execute()
        return resp.data
    except Exception as e:
        logging.error(f"Supabase get_alerts error: {e}")
        return []

def add_alert(pair: str, threshold: float, direction: str) -> bool:
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
    try:
        supabase.table("alerts").update({"is_active": False}).eq("id", alert_id).execute()
        return True
    except Exception as e:
        logging.error(f"Supabase remove_alert error: {e}")
        return False

# ---------- ПОЛУЧЕНИЕ КУРСОВ (без изменений) ----------
def get_cbr_rates() -> Dict[str, float]:
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

def get_all_rates(force=False) -> Dict[str, Any]:
    now = datetime.now()
    if not force and _cache["rates"] and _cache["rates"].get("timestamp") and (now - _cache["rates"]["timestamp"]).seconds < CACHE_TTL:
        return _cache["rates"]

    result = {}
    cbr = get_cbr_rates()
    if cbr["USD"] and cbr["USD"] > 0:
        result["RUB/USD"] = 1 / cbr["USD"]
    else:
        result["RUB/USD"] = None
    if cbr["CNY"] and cbr["CNY"] > 0:
        result["RUB/CNY"] = 1 / cbr["CNY"]
    else:
        result["RUB/CNY"] = None

    usdt_rub = get_usdt_rub_rapira()
    result["RUB/USDT"] = usdt_rub

    usdt_cny = get_usdt_cny_bybit()
    result["USDT/CNY"] = usdt_cny

    usd_cny = get_usd_cny_bybit()
    if usd_cny is None and result["RUB/USD"] and result["RUB/CNY"]:
        usd_cny = result["RUB/CNY"] / result["RUB/USD"]
    result["USD/CNY"] = usd_cny

    result["timestamp"] = now
    _cache["rates"] = result
    return result

# ---------- ФУНКЦИИ ДЛЯ КРАСИВОГО ВЫВОДА ----------
def format_rates(rates: Dict, show_title=True) -> str:
    now = datetime.now().strftime("%d.%m.%Y, %H:%M")
    lines = []
    if show_title:
        lines.append(f"📊 **Курсы на {now}**")
        lines.append("━━━━━━━━━━━━━━━━━━━━━")
    val = rates.get("RUB/USD")
    lines.append(f"🇷🇺 RUB/USD: **{val:.2f}** ₽" if val else "🇷🇺 RUB/USD: ❌")
    val = rates.get("RUB/USDT")
    lines.append(f"🇷🇺 RUB/USDT: **{val:.2f}** ₽" if val else "🇷🇺 RUB/USDT: ❌")
    val = rates.get("RUB/CNY")
    lines.append(f"🇷🇺 RUB/CNY: **{val:.2f}** ₽" if val else "🇷🇺 RUB/CNY: ❌")
    val = rates.get("USDT/CNY")
    lines.append(f"🇨🇳 USDT/CNY: **{val:.2f}** ¥" if val else "🇨🇳 USDT/CNY: ❌")
    val = rates.get("USD/CNY")
    lines.append(f"🇺🇸 USD/CNY: **{val:.2f}** ¥" if val else "🇺🇸 USD/CNY: ❌")
    return "\n".join(lines)

# ---------- КЛАВИАТУРА МЕНЮ ----------
def main_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Все курсы", callback_data="show_rates")],
        [InlineKeyboardButton(text="💱 Конвертация", callback_data="convert_menu")],
        [InlineKeyboardButton(text="❓ Помощь", callback_data="help")]
    ])

def convert_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="RUB → USD", callback_data="conv_RUB_USD"),
         InlineKeyboardButton(text="RUB → USDT", callback_data="conv_RUB_USDT")],
        [InlineKeyboardButton(text="RUB → CNY", callback_data="conv_RUB_CNY"),
         InlineKeyboardButton(text="USDT → CNY", callback_data="conv_USDT_CNY")],
        [InlineKeyboardButton(text="USD → CNY", callback_data="conv_USD_CNY")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")]
    ])

# ---------- ОБРАБОТЧИКИ КОМАНД ----------
@dp.message(Command("start"))
async def start_cmd(message: Message):
    # Добавляем пользователя в БД (для статистики)
    add_user(message.from_user.id, message.from_user.username or "")
    rates = get_all_rates(force=True)
    text = "👋 **Привет, сотрудник!**\n\n" + format_rates(rates, show_title=True) + "\n\nВыбери действие:"
    await message.answer(text, reply_markup=main_menu_keyboard(), parse_mode="Markdown")

@dp.message(Command("kurs"))
async def kurs_cmd(message: Message):
    rates = get_all_rates(force=True)
    text = format_rates(rates, show_title=True)
    await message.answer(text, parse_mode="Markdown", reply_markup=main_menu_keyboard())

@dp.message(Command("convert"))
async def convert_cmd(message: Message):
    await message.answer("Выбери направление конвертации:", reply_markup=convert_menu_keyboard())

@dp.message(Command("help"))
async def help_cmd(message: Message):
    text = (
        "📋 **Доступные действия:**\n\n"
        "• Нажми «Все курсы» для обновления.\n"
        "• Нажми «Конвертация» и выбери пару.\n"
        "• Для админов есть доп. команды:\n"
        "  /add_user, /remove_user, /list_users,\n"
        "  /add_alert, /list_alerts, /remove_alert"
    )
    await message.answer(text, parse_mode="Markdown", reply_markup=main_menu_keyboard())

# ---------- ОБРАБОТКА ИНЛАЙН-КНОПОК ----------
@dp.callback_query(F.data == "show_rates")
async def show_rates_callback(callback: CallbackQuery):
    await callback.answer()
    rates = get_all_rates(force=True)
    text = format_rates(rates, show_title=True)
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=main_menu_keyboard())

@dp.callback_query(F.data == "convert_menu")
async def convert_menu_callback(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text("Выбери направление конвертации:", reply_markup=convert_menu_keyboard())

@dp.callback_query(F.data == "help")
async def help_callback(callback: CallbackQuery):
    await callback.answer()
    text = (
        "📋 **Доступные действия:**\n\n"
        "• Нажми «Все курсы» для обновления.\n"
        "• Нажми «Конвертация» и выбери пару.\n"
        "• Для админов есть доп. команды:\n"
        "  /add_user, /remove_user, /list_users,\n"
        "  /add_alert, /list_alerts, /remove_alert"
    )
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=main_menu_keyboard())

@dp.callback_query(F.data == "main_menu")
async def main_menu_callback(callback: CallbackQuery):
    await callback.answer()
    rates = get_all_rates(force=True)
    text = "👋 **Главное меню**\n\n" + format_rates(rates, show_title=True) + "\n\nВыбери действие:"
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=main_menu_keyboard())

@dp.callback_query(F.data.startswith("conv_"))
async def convert_pair_callback(callback: CallbackQuery):
    await callback.answer()
    pair = callback.data.split("_")[1:]
    if len(pair) != 2:
        await callback.message.answer("Ошибка выбора пары.")
        return
    from_cur, to_cur = pair
    await callback.message.answer(f"💱 **Конвертация {from_cur} → {to_cur}**\nВведите сумму в {from_cur}:", parse_mode="Markdown")
    waiting_for_convert[callback.from_user.id] = {"from": from_cur, "to": to_cur}

# ---------- ОБРАБОТКА ТЕКСТОВЫХ СООБЩЕНИЙ (для ввода суммы) ----------
waiting_for_convert = {}

@dp.message(F.text)
async def handle_text(message: Message):
    user_id = message.from_user.id
    if user_id in waiting_for_convert:
        try:
            amount = float(message.text.replace(',', '.'))
            if amount <= 0:
                raise ValueError
        except:
            await message.answer("❌ Введите корректное положительное число.")
            return
        conv_data = waiting_for_convert.pop(user_id)
        from_cur = conv_data["from"]
        to_cur = conv_data["to"]
        rates = get_all_rates(force=True)
        result = None
        pair_key = f"{from_cur}/{to_cur}"
        reverse_key = f"{to_cur}/{from_cur}"
        if pair_key in rates and rates[pair_key] is not None:
            rate = rates[pair_key]
            result = amount * rate
            text = f"💱 **{amount:.2f} {from_cur} = {result:.2f} {to_cur}**\nпо курсу {rate:.4f}"
        elif reverse_key in rates and rates[reverse_key] is not None:
            rate = rates[reverse_key]
            result = amount / rate
            text = f"💱 **{amount:.2f} {from_cur} = {result:.2f} {to_cur}**\nпо курсу 1/{rate:.4f}"
        else:
            rub_amount = None
            if from_cur == "RUB":
                rub_amount = amount
            elif from_cur == "USD" and rates.get("RUB/USD"):
                rub_amount = amount * rates["RUB/USD"]
            elif from_cur == "USDT" and rates.get("RUB/USDT"):
                rub_amount = amount * rates["RUB/USDT"]
            elif from_cur == "CNY" and rates.get("RUB/CNY"):
                rub_amount = amount * rates["RUB/CNY"]
            else:
                await message.answer(f"❌ Не могу конвертировать {from_cur} → {to_cur}.")
                return
            if rub_amount is None:
                await message.answer(f"❌ Не удалось получить курс для {from_cur}.")
                return
            if to_cur == "RUB":
                result = rub_amount
            elif to_cur == "USD" and rates.get("RUB/USD"):
                result = rub_amount / rates["RUB/USD"]
            elif to_cur == "USDT" and rates.get("RUB/USDT"):
                result = rub_amount / rates["RUB/USDT"]
            elif to_cur == "CNY" and rates.get("RUB/CNY"):
                result = rub_amount / rates["RUB/CNY"]
            else:
                await message.answer(f"❌ Не могу конвертировать RUB → {to_cur}.")
                return
            if result is None:
                await message.answer(f"❌ Не удалось получить курс для {to_cur}.")
                return
            text = f"💱 **{amount:.2f} {from_cur} = {result:.2f} {to_cur}**"
        await message.answer(text, parse_mode="Markdown", reply_markup=convert_menu_keyboard())
        return

    # Если просто текст без ожидания
    await message.answer("Используйте кнопки меню или команды:\n/start, /kurs, /convert, /help")

# ---------- АДМИН-КОМАНДЫ (только для ADMIN_ID) ----------
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

@dp.message(Command("add_user"))
async def add_user_cmd(message: Message):
    if not is_admin(message.from_user.id):
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
        await message.answer(f"✅ Пользователь {user_id} добавлен в базу.")
    else:
        await message.answer("❌ Ошибка при добавлении пользователя.")

@dp.message(Command("remove_user"))
async def remove_user_cmd(message: Message):
    if not is_admin(message.from_user.id):
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
    if not is_admin(message.from_user.id):
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

@dp.message(Command("add_alert"))
async def add_alert_cmd(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Только для администратора.")
        return
    args = message.text.split()
    if len(args) != 4:
        await message.answer("❌ Пример: `/add_alert RUB/USD 85 above`\n"
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
    if not is_admin(message.from_user.id):
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
    if not is_admin(message.from_user.id):
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
        BotCommand(command="start", description="🚀 Главное меню"),
        BotCommand(command="kurs", description="📊 Все курсы"),
        BotCommand(command="convert", description="💱 Конвертация"),
        BotCommand(command="help", description="❓ Помощь")
    ])
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())