import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import requests
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import BotCommand, Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram import F

BOT_TOKEN = "8221747840:AAHWUVECN07_ldY8aLutcr9qLnsKtRt45Uc"
ADMIN_ID = 8891085561

# ---------- Чтение дельт из переменных окружения (с запасными значениями) ----------
def get_delta_from_env(key, default):
    try:
        val = os.environ.get(key)
        if val is not None:
            return float(val)
    except:
        pass
    return default

deltas = {
    "delta_rub_to_usdt": get_delta_from_env("DELTA_RUB_USDT", 0.30),
    "delta_usdt_to_cny": get_delta_from_env("DELTA_USDT_CNY", 0.10),
    "delta_usdt_to_rub": get_delta_from_env("DELTA_USDT_RUB", 0.20),
}

# ---------- Кеш для курсов ----------
_cache = {
    "usdt_rub": None,
    "usdt_cny": None,
    "timestamp": None,
    "last_successful_cny": None
}
CACHE_TTL = 60

# ---------- Отправка ошибки админу ----------
async def notify_admin_error(error_text):
    try:
        bot = Bot(token=BOT_TOKEN)
        await bot.send_message(ADMIN_ID, f"⚠️ **Ошибка бота:**\n{error_text}")
        await bot.session.close()
    except:
        pass

# ---------- Получение курсов ----------
def get_usdt_rub_rate(force=False):
    now = datetime.now()
    if not force and _cache["timestamp"] is not None and (now - _cache["timestamp"]).seconds < CACHE_TTL:
        if _cache["usdt_rub"] is not None:
            return _cache["usdt_rub"]

    url = "https://api.rapira.net/open/market/rates"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("data", []):
            if item.get("symbol") == "USDT/RUB":
                rate = float(item.get("askPrice", 0))
                _cache["usdt_rub"] = rate
                _cache["timestamp"] = now
                return rate
        return None
    except Exception as e:
        logging.error(f"Rapira error: {e}")
        asyncio.create_task(notify_admin_error(f"Rapira не отвечает: {e}"))
        return None

def get_usdt_cny_rate(force=False):
    now = datetime.now()
    if not force and _cache["timestamp"] is not None and (now - _cache["timestamp"]).seconds < CACHE_TTL:
        if _cache["usdt_cny"] is not None:
            return _cache["usdt_cny"]

    # 1. Пробуем Bybit
    try:
        url = "https://api.bybit.com/v5/market/tickers?category=spot&symbol=USDTCNY"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("retCode") == 0:
                ticker = data["result"]["list"][0]
                rate = float(ticker["lastPrice"])
                _cache["usdt_cny"] = rate
                _cache["last_successful_cny"] = rate
                _cache["timestamp"] = now
                logging.info(f"CNY rate from Bybit: {rate}")
                return rate
    except Exception as e:
        logging.warning(f"Bybit USDT/CNY failed: {e}")

    # 2. Пробуем CoinGecko (резерв)
    url = "https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=cny"
    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code == 429:
                logging.warning(f"CoinGecko rate limit, attempt {attempt+1}/3, waiting 3s")
                time.sleep(3)
                continue
            resp.raise_for_status()
            data = resp.json()
            rate = float(data["tether"]["cny"])
            _cache["usdt_cny"] = rate
            _cache["last_successful_cny"] = rate
            _cache["timestamp"] = now
            logging.info(f"CNY rate from CoinGecko: {rate}")
            return rate
        except Exception as e:
            logging.warning(f"CoinGecko attempt {attempt+1} failed: {e}")
            time.sleep(2)

    # 3. Если всё упало, возвращаем последний успешный курс
    if _cache["last_successful_cny"] is not None:
        logging.warning("Using cached CNY rate")
        return _cache["last_successful_cny"]

    # 4. Ничего не получилось
    asyncio.create_task(notify_admin_error("Не удалось получить курс CNY ни из одного источника"))
    return None

# ---------- Функции конвертации ----------
def convert_rub_to_usdt_cny(amount_rub):
    usdt_rate = get_usdt_rub_rate()
    cny_rate = get_usdt_cny_rate()
    if usdt_rate is None or cny_rate is None:
        return None
    usdt_rate_with_delta = usdt_rate + deltas["delta_rub_to_usdt"]
    cny_rate_with_delta = cny_rate + deltas["delta_usdt_to_cny"]
    usdt = amount_rub / usdt_rate_with_delta
    cny = usdt * cny_rate_with_delta
    return {
        "amount_rub": amount_rub,
        "usdt": usdt,
        "cny": cny,
    }

def convert_usdt_to_rub(amount_usdt):
    usdt_rate = get_usdt_rub_rate()
    if usdt_rate is None:
        return None
    rate_with_delta = usdt_rate - deltas["delta_usdt_to_rub"]
    if rate_with_delta < 0:
        rate_with_delta = 0
    rub = amount_usdt * rate_with_delta
    return {
        "amount_usdt": amount_usdt,
        "rub": rub,
    }

# ---------- Инициализация бота ----------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

waiting_for_rub = {}
waiting_for_usdt = {}

async def set_default_commands():
    await bot.set_my_commands([
        BotCommand(command="course", description="📈 Текущий курс USDT/RUB и USDT/CNY"),
        BotCommand(command="convert_rub", description="💱 Конвертировать рубли → USDT/CNY"),
        BotCommand(command="convert_usdt", description="💰 Конвертировать USDT → рубли"),
        BotCommand(command="show_deltas", description="🔧 Показать текущие дельты (админ)"),
        BotCommand(command="help", description="❓ Справка")
    ])

# ---------- Обработчики команд ----------
@dp.message(Command("start"))
async def start_cmd(message: Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📈 Курс", callback_data="course")],
        [InlineKeyboardButton(text="💱 Рубли → USDT/CNY", callback_data="convert_rub")],
        [InlineKeyboardButton(text="💰 USDT → рубли", callback_data="convert_usdt")],
        [InlineKeyboardButton(text="❓ Помощь", callback_data="help")]
    ])
    # При старте сразу показываем курс
    rub_rate = get_usdt_rub_rate(force=True)
    cny_rate = get_usdt_cny_rate(force=True)
    now = datetime.now().strftime("%d.%m.%Y, %H:%M")
    course_text = ""
    if rub_rate is not None:
        course_text = f"\n📈 **Курс на {now}**\n🇺🇸 USDT/RUB: **{rub_rate:.2f}** ₽"
        if cny_rate is not None:
            course_text += f"\n🇨🇳 USDT/CNY: **{cny_rate:.2f}** ¥"
        else:
            course_text += "\n🇨🇳 USDT/CNY: ❌"
    else:
        course_text = "\n❌ Не удалось получить курс"

    await message.answer(
        f"👋 Привет! Я бот для конвертации криптовалют.\n\n"
        f"Используй кнопки ниже или команды из меню.{course_text}",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "course")
async def course_callback(callback: CallbackQuery):
    await callback.answer()
    await course_cmd(callback.message)

@dp.callback_query(F.data == "convert_rub")
async def convert_rub_callback(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("Введите сумму в рублях (например, 10000):")
    waiting_for_rub[callback.from_user.id] = True

@dp.callback_query(F.data == "convert_usdt")
async def convert_usdt_callback(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("Введите сумму в USDT (например, 500):")
    waiting_for_usdt[callback.from_user.id] = True

@dp.callback_query(F.data == "help")
async def help_callback(callback: CallbackQuery):
    await callback.answer()
    await help_cmd(callback.message)

@dp.message(Command("course"))
async def course_cmd(message: Message):
    rub_rate = get_usdt_rub_rate(force=True)
    cny_rate = get_usdt_cny_rate(force=True)
    now = datetime.now().strftime("%d.%m.%Y, %H:%M")
    if rub_rate is None:
        await message.answer("❌ Не удалось получить курс USDT/RUB. Попробуйте позже.")
        return
    response = f"📈 **Курсы на {now}**\n\n"
    response += f"🇺🇸 USDT/RUB: **{rub_rate:.2f}** ₽\n"
    if cny_rate is not None:
        response += f"🇨🇳 USDT/CNY: **{cny_rate:.2f}** ¥"
    else:
        response += f"🇨🇳 USDT/CNY: ❌ не удалось получить"
    await message.answer(response, parse_mode="Markdown")

@dp.message(Command("convert_rub"))
async def convert_rub_cmd(message: Message):
    args = message.text.split()
    if len(args) > 1:
        try:
            amount = float(args[1].replace(',', '.'))
            if amount <= 0:
                raise ValueError
            await process_rub_conversion(message, amount)
        except:
            await message.answer("❌ Введите корректное положительное число.")
    else:
        await message.answer("Введите сумму в рублях (например, 10000):")
        waiting_for_rub[message.from_user.id] = True

@dp.message(Command("convert_usdt"))
async def convert_usdt_cmd(message: Message):
    args = message.text.split()
    if len(args) > 1:
        try:
            amount = float(args[1].replace(',', '.'))
            if amount <= 0:
                raise ValueError
            await process_usdt_conversion(message, amount)
        except:
            await message.answer("❌ Введите корректное положительное число.")
    else:
        await message.answer("Введите сумму в USDT (например, 500):")
        waiting_for_usdt[message.from_user.id] = True

@dp.message(Command("show_deltas"))
async def show_deltas(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только для администратора.")
        return
    text = (
        f"🔧 **Текущие дельты:**\n\n"
        f"RUB → USDT: **{deltas['delta_rub_to_usdt']:.2f}** ₽\n"
        f"USDT → CNY: **{deltas['delta_usdt_to_cny']:.2f}** ¥\n"
        f"USDT → RUB: **{deltas['delta_usdt_to_rub']:.2f}** ₽"
    )
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        "📋 **Доступные команды:**\n\n"
        "/course – текущий курс USDT/RUB и USDT/CNY\n"
        "/convert_rub [сумма] – конвертировать рубли в USDT и CNY\n"
        "/convert_usdt [сумма] – конвертировать USDT в рубли\n"
        "/show_deltas – показать текущие дельты (админ)\n"
        "/help – эта справка\n\n"
        "💡 Примеры:\n"
        "/convert_rub 10000\n"
        "/convert_usdt 500"
    )

# ---------- Админ-команды для изменения дельт (они теперь тоже обновляют переменные окружения? Но мы не можем менять их через код, поэтому оставим как есть, но они будут менять значение в памяти, а при перезапуске вернутся к значениям из окружения. Чтобы изменения сохранялись, нужно либо хранить в базе, либо обновлять переменные через API Render. Для простоты пока оставим так: команды изменяют дельты в памяти, но при перезапуске они сбросятся к значениям из окружения. Если хотите постоянного изменения, лучше использовать файл, но тогда проблема сброса. Пока предлагаю так: пользователь меняет дельты командами, они действуют до перезапуска, а после перезапуска берутся из окружения. ----------
@dp.message(Command("set_delta_rub_usdt"))
async def set_delta_rub_usdt(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только для администратора.")
        return
    args = message.text.split()
    if len(args) != 2:
        await message.answer("❌ Пример: `/set_delta_rub_usdt 0.30`", parse_mode="Markdown")
        return
    try:
        val = float(args[1].replace(',', '.'))
        deltas["delta_rub_to_usdt"] = val
        await message.answer(f"✅ Дельта RUB→USDT установлена на **{val:.2f}** ₽ (до перезапуска)")
    except:
        await message.answer("❌ Введите корректное число.")

@dp.message(Command("set_delta_usdt_cny"))
async def set_delta_usdt_cny(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только для администратора.")
        return
    args = message.text.split()
    if len(args) != 2:
        await message.answer("❌ Пример: `/set_delta_usdt_cny 0.10`", parse_mode="Markdown")
        return
    try:
        val = float(args[1].replace(',', '.'))
        deltas["delta_usdt_to_cny"] = val
        await message.answer(f"✅ Дельта USDT→CNY установлена на **{val:.2f}** ¥ (до перезапуска)")
    except:
        await message.answer("❌ Введите корректное число.")

@dp.message(Command("set_delta_usdt_rub"))
async def set_delta_usdt_rub(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только для администратора.")
        return
    args = message.text.split()
    if len(args) != 2:
        await message.answer("❌ Пример: `/set_delta_usdt_rub 0.20`", parse_mode="Markdown")
        return
    try:
        val = float(args[1].replace(',', '.'))
        deltas["delta_usdt_to_rub"] = val
        await message.answer(f"✅ Дельта USDT→RUB установлена на **{val:.2f}** ₽ (до перезапуска)")
    except:
        await message.answer("❌ Введите корректное число.")

# ---------- Обработка текстовых сообщений ----------
@dp.message(F.text)
async def handle_text(message: Message):
    user_id = message.from_user.id
    text = message.text.strip()

    if waiting_for_rub.get(user_id, False):
        try:
            amount = float(text.replace(',', '.'))
            if amount <= 0:
                raise ValueError
            del waiting_for_rub[user_id]
            await process_rub_conversion(message, amount)
        except:
            await message.answer("❌ Введите корректное положительное число.")
        return

    if waiting_for_usdt.get(user_id, False):
        try:
            amount = float(text.replace(',', '.'))
            if amount <= 0:
                raise ValueError
            del waiting_for_usdt[user_id]
            await process_usdt_conversion(message, amount)
        except:
            await message.answer("❌ Введите корректное положительное число.")
        return

    await message.answer("Используйте команды из меню или отправьте /help")

# ---------- Конвертация с улучшенной анимацией ----------
async def process_rub_conversion(message: Message, amount_rub):
    loading = await message.answer("⏳ Конвертирую...")
    await asyncio.sleep(0.3)
    await loading.edit_text("🔄 Считаю...")
    result = convert_rub_to_usdt_cny(amount_rub)
    if result is None:
        await loading.edit_text("❌ Не удалось получить курс. Попробуйте позже.")
        return
    await asyncio.sleep(0.3)
    now = datetime.now().strftime("%d.%m.%Y, %H:%M")
    await loading.edit_text(
        f"💱 **Конвертация {result['amount_rub']:,.0f} ₽**\n"
        f"🕐 {now}\n\n"
        f"🪙 Получите: **{result['usdt']:,.2f} USDT**\n"
        f"🇨🇳 В юанях: **{result['cny']:,.2f} CNY**\n\n"
        f"✅ Готово!",
        parse_mode="Markdown"
    )

async def process_usdt_conversion(message: Message, amount_usdt):
    loading = await message.answer("⏳ Конвертирую...")
    await asyncio.sleep(0.3)
    await loading.edit_text("🔄 Считаю...")
    result = convert_usdt_to_rub(amount_usdt)
    if result is None:
        await loading.edit_text("❌ Не удалось получить курс. Попробуйте позже.")
        return
    await asyncio.sleep(0.3)
    now = datetime.now().strftime("%d.%m.%Y, %H:%M")
    await loading.edit_text(
        f"💰 **Конвертация {result['amount_usdt']:,.2f} USDT**\n"
        f"🕐 {now}\n\n"
        f"🇷🇺 Получите: **{result['rub']:,.2f} ₽**\n\n"
        f"✅ Готово!",
        parse_mode="Markdown"
    )

# ---------- Запуск ----------
async def main():
    await set_default_commands()
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())