#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import subprocess
import sys
import io
import logging
import asyncio
import os
from datetime import datetime, timedelta

# ===== АВТОУСТАНОВКА БИБЛИОТЕК =====
required_packages = [
    "matplotlib",
    "moexalgo",
    "pandas",
    "python-telegram-bot[job-queue]",
    "apscheduler"
]

for package in required_packages:
    try:
        if package.startswith("python-telegram-bot"):
            import telegram
        elif package == "moexalgo":
            import moexalgo
        elif package == "pandas":
            import pandas
        elif package == "matplotlib":
            import matplotlib
        elif package == "apscheduler":
            import apscheduler
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", package.split("[")[0]])

import pandas as pd
import matplotlib.pyplot as plt
from moexalgo import Ticker
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ======= ПРОВЕРКА БЛОКИРОВКИ =======
LOCK_FILE = "bot.lock"
try:
    import fcntl
    lock_fd = open(LOCK_FILE, "w")
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except (ImportError, OSError):
    try:
        if os.name == "nt":
            import msvcrt
            lock_fd = open(LOCK_FILE, "w")
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            raise
    except Exception:
        print("❌ Бот уже запущен! Удалите bot.lock и завершите старый процесс.")
        sys.exit(1)

# ======= НАСТРОЙКИ =======
TELEGRAM_TOKEN = "8885452009:AAGxNl0iUCT2Q58jXm0DZ_h-ZDevQcYtqpw"
CHAT_ID = 1234329121
UPDATE_INTERVAL_MINUTES = 60
DAYS_BACK = 6

PAIRS = {
    "Мечел": ("MTLR", "MTLRP"),
    "Татнефть": ("TATN", "TATNP"),
}
DEFAULT_PAIR = "Мечел"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🏠 Старт"), KeyboardButton("📊 Пары")],
    ],
    resize_keyboard=True,
    one_time_keyboard=False
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ======= ФУНКЦИИ (без изменений) =======
def get_spread_data(ticker1, ticker2, start_date, end_date):
    ...  # (оставьте как было)

def generate_spread_plot(pair_name):
    ...  # (оставьте как было)

async def send_spread_update(chat_id, pair_name, bot):
    ...  # (оставьте как было)

async def auto_send_all(bot):
    ...  # (оставьте как было)

# ======= КОМАНДЫ =======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"Команда /start от пользователя {user.id}")
    keyboard = [[InlineKeyboardButton(name, callback_data=f"pair_{name}")] for name in PAIRS]
    await update.message.reply_text(
        f"📈 Бот спредов российских акций.\n"
        f"Автоотправка всех пар каждые {UPDATE_INTERVAL_MINUTES} мин.\n\n"
        f"Доступные пары:\n" + "\n".join(f"• {n} ({t1}/{t2})" for n, (t1, t2) in PAIRS.items()),
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    await update.message.reply_text("Кнопки управления всегда под рукой 👇", reply_markup=MAIN_KEYBOARD)

async def pairs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ...  # аналогично

async def spread_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ...  # аналогично

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ...  # аналогично

async def handle_main_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ...  # аналогично

# ======= ЗАПУСК С УЛУЧШЕННЫМ ПОЛЛИНГОМ =======
async def main():
    request = HTTPXRequest(read_timeout=30, write_timeout=30, connect_timeout=30, pool_timeout=30)
    application = Application.builder().token(TELEGRAM_TOKEN).request(request).build()

    await application.bot.delete_webhook()
    logger.info("Вебхук удалён.")

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("pairs", pairs_command))
    application.add_handler(CommandHandler("spread", spread_cmd))
    application.add_handler(CallbackQueryHandler(button_callback, pattern="^pair_"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_main_keyboard))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(auto_send_all, 'interval', minutes=UPDATE_INTERVAL_MINUTES, args=[application.bot])
    scheduler.start()
    logger.info(f"Планировщик запущен, интервал {UPDATE_INTERVAL_MINUTES} мин.")

    await application.initialize()
    await application.start()
    # ГЛАВНОЕ ИЗМЕНЕНИЕ: drop_pending_updates=True
    await application.updater.start_polling(
        bootstrap_retries=5,
        drop_pending_updates=True,
        allowed_updates=['message', 'callback_query', 'my_chat_member', 'chat_member']
    )

    logger.info(f"✅ Бот запущен. Пары: {list(PAIRS.keys())}.")

    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановка...")
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        scheduler.shutdown()
        # снимаем блокировку
        if os.name == "nt":
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
        os.remove(LOCK_FILE)

if __name__ == "__main__":
    asyncio.run(main())