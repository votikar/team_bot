import os
import threading
from flask import Flask
import asyncio
import logging
from bot import bot, dp
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

@app.route('/')
def home():
    return "Team Bot is running!"

@app.route('/health')
def health():
    return "OK"

# Импортируем функции для рассылки из bot
from bot import send_daily_update, send_evening_update

def schedule_jobs():
    scheduler = BackgroundScheduler(timezone=pytz.timezone('Europe/Moscow'))
    # Утром в 9:00
    scheduler.add_job(
        func=lambda: asyncio.run(send_daily_update()),
        trigger=CronTrigger(hour=9, minute=0),
        id='morning_update'
    )
    # Вечером в 18:00
    scheduler.add_job(
        func=lambda: asyncio.run(send_evening_update()),
        trigger=CronTrigger(hour=18, minute=0),
        id='evening_update'
    )
    scheduler.start()
    logging.info("Scheduler started")

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

async def run_bot():
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    # Запускаем планировщик в фоне
    schedule_jobs()
    # Запускаем Flask в потоке
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    # Запускаем бота в основном потоке
    asyncio.run(run_bot())