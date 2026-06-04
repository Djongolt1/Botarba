#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import subprocess
import sys
import io
import logging
import asyncio
import os
from datetime import datetime, timedelta

# ===== АВТОУСТАНОВКА НЕДОСТАЮЩИХ БИБЛИОТЕК =====
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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
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
TELEGRAM_TOKEN = "8885452009:AAFBmG8idkXGSs_TBA0n-_9GkiR1WmA1-_4"
CHAT_ID = 1234329121
UPDATE_INTERVAL_MINUTES = 60
DAYS_BACK = 6   # максимум 6 дней

PAIRS = {
    "Мечел": ("MTLR", "MTLRP"),
    "Татнефть": ("TATN", "TATNP"),
    "Ростелеком": ("RTKM", "RTKMP"),
    "Сбер": ("SBER", "SBERP"),
}
DEFAULT_PAIR = "Мечел"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ======= ЗАГРУЗКА ДАННЫХ =======
def get_spread_data(ticker1, ticker2, start_date, end_date):
    try:
        t1 = Ticker(ticker1)
        t2 = Ticker(ticker2)
        # Пробуем 15-минутки
        intraday = True
        try:
            data1 = t1.candles(start=start_date, end=end_date, period='15min')
            data2 = t2.candles(start=start_date, end=end_date, period='15min')
            logger.info(f"Загружены 15min свечи для {ticker1}/{ticker2}")
        except Exception:
            logger.warning(f"15min не доступны, берём дневные свечи для {ticker1}/{ticker2}")
            data1 = t1.candles(start=start_date, end=end_date)
            data2 = t2.candles(start=start_date, end=end_date)
            intraday = False

        df1 = pd.DataFrame(data1).set_index('begin')
        df2 = pd.DataFrame(data2).set_index('begin')
        df1.index = pd.to_datetime(df1.index)
        df2.index = pd.to_datetime(df2.index)

        if df1.empty or df2.empty:
            return None

        combined = pd.DataFrame({
            f'{ticker1}_close': df1['close'],
            f'{ticker2}_close': df2['close']
        }).dropna()

        # Фильтр по часу только для внутридневных (отсекаем ночь)
        if intraday:
            combined = combined[combined.index.hour >= 7]

        if combined.empty:
            return None

        combined['Spread'] = combined[f'{ticker1}_close'] - combined[f'{ticker2}_close']
        return combined
    except Exception as e:
        logger.error(f"Ошибка в get_spread_data: {e}")
        return None

# ======= ГЕНЕРАЦИЯ ГРАФИКА =======
def generate_spread_plot(pair_name):
    if pair_name not in PAIRS:
        return None, f"Пара '{pair_name}' не найдена."

    ticker1, ticker2 = PAIRS[pair_name]
    end_date = datetime.now().date() + timedelta(days=1)
    start_date = end_date - timedelta(days=DAYS_BACK)

    data = get_spread_data(ticker1, ticker2, start_date, end_date)
    if data is None or data.empty:
        return None, f"Нет данных за последние {DAYS_BACK} дней по паре {pair_name}.\nПроверьте, торгуются ли {ticker1} и {ticker2}."

    fig, ax = plt.subplots(figsize=(12, 6))
    x = range(len(data))
    y = data['Spread'].values
    ax.plot(x, y, linewidth=1.8, label=f'Спред {ticker1} – {ticker2}')

    step = max(1, len(data) // 15)
    xticks_pos = list(range(0, len(data), step))
    xticks_labels = [data.index[i].strftime("%m-%d %H:%M") for i in xticks_pos]
    ax.set_xticks(xticks_pos)
    ax.set_xticklabels(xticks_labels, rotation=45, ha='right', fontsize=8)

    ax.set_title(f'{pair_name} — спред ({ticker1} – {ticker2})\n{start_date.date()} – {end_date.date() - timedelta(days=1)}', fontsize=14)
    ax.set_ylabel('Разница (₽)')
    ax.set_xlabel('Время')
    ax.grid(True, alpha=0.3)
    ax.legend()

    last_spread = y[-1]
    last_time = data.index[-1]
    ax.text(1.02, 0.90,
            f'Текущий спред: {last_spread:.2f} ₽\n{last_time.strftime("%Y-%m-%d %H:%M")}',
            transform=ax.transAxes, verticalalignment='top', horizontalalignment='left',
            bbox=dict(boxstyle='round', facecolor='white', alpha=0.8), fontsize=9)

    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return buf, None

# ======= ОТПРАВКА ГРАФИКА =======
async def send_spread_update(chat_id, pair_name, bot):
    try:
        buf, err = generate_spread_plot(pair_name)
        if err:
            await bot.send_message(chat_id=chat_id, text=f"❌ {err}")
            return
        await bot.send_photo(
            chat_id=chat_id,
            photo=buf,
            caption=f"📊 Спред {pair_name} ({PAIRS[pair_name][0]} – {PAIRS[pair_name][1]}) за {DAYS_BACK} дней"
        )
    except Exception as e:
        logger.exception(f"Ошибка при отправке {pair_name}")
        await bot.send_message(chat_id=chat_id, text=f"❌ Критическая ошибка для {pair_name}: {e}")

# ======= АВТООТПРАВКА =======
async def auto_send_all(context: ContextTypes.DEFAULT_TYPE):
    for name in PAIRS:
        await send_spread_update(CHAT_ID, name, context.bot)
        await asyncio.sleep(3)

# ======= КОМАНДЫ =======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(name, callback_data=f"pair_{name}")] for name in PAIRS]
    await update.message.reply_text(
        f"📈 Бот спредов российских акций.\n"
        f"Автоотправка всех пар каждые {UPDATE_INTERVAL_MINUTES} мин.\n\n"
        f"Доступные пары:\n" + "\n".join(f"• {n} ({t1}/{t2})" for n, (t1, t2) in PAIRS.items()),
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def pairs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(name, callback_data=f"pair_{name}")] for name in PAIRS]
    await update.message.reply_text(
        "Выберите пару:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def spread_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🔄 Загружаю {DEFAULT_PAIR}...")
    await send_spread_update(update.effective_chat.id, DEFAULT_PAIR, context.bot)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pair_name = query.data.replace("pair_", "")
    if pair_name not in PAIRS:
        await query.edit_message_text("❌ Неизвестная пара")
        return
    await query.edit_message_text(f"⏳ Генерирую график для {pair_name}...")
    await send_spread_update(update.effective_chat.id, pair_name, context.bot)

# ======= ЗАПУСК =======
async def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    async with app:
        await app.bot.delete_webhook()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pairs", pairs_command))
    app.add_handler(CommandHandler("spread", spread_cmd))
    app.add_handler(CallbackQueryHandler(button_callback, pattern="^pair_"))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(auto_send_all, 'interval', minutes=UPDATE_INTERVAL_MINUTES, args=[app.bot])
    scheduler.start()

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    logger.info(f"✅ Бот запущен. Пары: {list(PAIRS.keys())}")
    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановка...")
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        scheduler.shutdown()
        if os.name == "nt":
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
        os.remove(LOCK_FILE)

if __name__ == "__main__":
    asyncio.run(main())
