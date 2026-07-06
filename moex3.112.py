#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import subprocess
import sys
import io
import logging
import asyncio
import os
from datetime import datetime, timedelta

# ===== АВТОУСТАНОВКА (на случай, если пакетов нет) =====
required_packages = [
    "requests",
    "pandas",
    "matplotlib",
    "python-telegram-bot[job-queue]",
    "apscheduler"
]

for package in required_packages:
    try:
        if package.startswith("python-telegram-bot"):
            import telegram
        elif package == "pandas":
            import pandas
        elif package == "matplotlib":
            import matplotlib
        elif package == "requests":
            import requests
        elif package == "apscheduler":
            import apscheduler
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", package.split("[")[0]])

# Теперь импортируем
import requests
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ======= НАСТРОЙКИ =======
TELEGRAM_TOKEN = "8885452009:AAGxNl0iUCT2Q58jXm0DZ_h-ZDevQcYtqpw"  # замените при необходимости
CHAT_ID = 1234329121
UPDATE_INTERVAL_MINUTES = 60
DAYS_BACK = 7

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

# ======= ФУНКЦИИ (сокращённо, но рабочие) =======
def fetch_candles(ticker, start_date, end_date, interval='15'):
    url = f"https://iss.moex.com/iss/engines/stock/markets/shares/boards/tqbr/securities/{ticker}/candles.json"
    params = {'from': start_date.strftime('%Y-%m-%d'), 'till': end_date.strftime('%Y-%m-%d'), 'interval': interval, 'limit': 10000}
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        candles = data.get('candles', {})
        if not candles or 'data' not in candles or not candles['data']:
            return None
        df = pd.DataFrame(candles['data'], columns=candles['columns'])
        df['begin'] = pd.to_datetime(df['begin'])
        df.set_index('begin', inplace=True)
        logger.info(f"Загружено {len(df)} свечей для {ticker}")
        return df
    except Exception as e:
        logger.error(f"Ошибка запроса для {ticker}: {e}")
        return None

def get_spread_data(ticker1, ticker2, start_date, end_date):
    df1 = fetch_candles(ticker1, start_date, end_date, '15')
    df2 = fetch_candles(ticker2, start_date, end_date, '15')
    if df1 is None or df2 is None or df1.empty or df2.empty:
        logger.warning("15-минутки не загружены, пробуем дневные")
        df1 = fetch_candles(ticker1, start_date, end_date, '1')
        df2 = fetch_candles(ticker2, start_date, end_date, '1')
        if df1 is None or df2 is None or df1.empty or df2.empty:
            return None
    combined = pd.DataFrame({f'{ticker1}_close': df1['close'], f'{ticker2}_close': df2['close']}).dropna()
    if combined.empty:
        return None
    combined = combined[combined.index.hour >= 7]
    if combined.empty:
        return None
    combined['Spread'] = combined[f'{ticker1}_close'] - combined[f'{ticker2}_close']
    logger.info(f"Спред построен, точек: {len(combined)}")
    return combined

def generate_spread_plot(pair_name):
    if pair_name not in PAIRS:
        return None, f"Пара '{pair_name}' не найдена."
    ticker1, ticker2 = PAIRS[pair_name]
    now = datetime.now()
    end_date = now.date() + timedelta(days=1)
    start_date = end_date - timedelta(days=DAYS_BACK)
    data = get_spread_data(ticker1, ticker2, start_date, end_date)
    if data is None or data.empty:
        return None, f"Нет данных за последние {DAYS_BACK} дней по паре {pair_name}."
    fig, ax = plt.subplots(figsize=(12, 6))
    x = range(len(data))
    y = data['Spread'].values
    ax.plot(x, y, linewidth=1.8, label=f'Спред {ticker1} – {ticker2}')
    step = max(1, len(data) // 15)
    xticks_pos = list(range(0, len(data), step))
    xticks_labels = [data.index[i].strftime("%m-%d %H:%M") for i in xticks_pos]
    ax.set_xticks(xticks_pos)
    ax.set_xticklabels(xticks_labels, rotation=45, ha='right', fontsize=8)
    ax.set_title(f'{pair_name} — спред ({ticker1} – {ticker2})', fontsize=14)
    ax.set_ylabel('Разница (₽)')
    ax.set_xlabel('Время')
    ax.grid(True, alpha=0.3)
    ax.legend()
    last_spread = y[-1]
    last_time = data.index[-1]
    ax.text(1.02, 0.90, f'Текущий спред: {last_spread:.2f} ₽\n{last_time.strftime("%Y-%m-%d %H:%M")}',
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
        logger.info(f"Генерация графика для {pair_name}")
        buf, err = generate_spread_plot(pair_name)
        if err:
            await bot.send_message(chat_id=chat_id, text=f"❌ {err}")
            return
        await bot.send_photo(chat_id=chat_id, photo=buf,
                             caption=f"📊 Спред {pair_name} ({PAIRS[pair_name][0]} – {PAIRS[pair_name][1]}) за последние {DAYS_BACK} дней")
        logger.info(f"✅ График для {pair_name} отправлен")
    except Exception as e:
        logger.exception(f"❌ Ошибка при отправке {pair_name}")
        await bot.send_message(chat_id=chat_id, text=f"❌ Ошибка: {e}")

# ======= АВТООТПРАВКА =======
async def auto_send_all(bot):
    for name in PAIRS:
        await send_spread_update(CHAT_ID, name, bot)
        await asyncio.sleep(3)

# ======= КОМАНДЫ (упрощённо) =======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(name, callback_data=f"pair_{name}")] for name in PAIRS]
    await update.message.reply_text(f"📈 Бот спредов. Пары: " + ", ".join(PAIRS.keys()),
                                    reply_markup=InlineKeyboardMarkup(keyboard))
    await update.message.reply_text("Кнопки управления 👇", reply_markup=MAIN_KEYBOARD)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pair_name = query.data.replace("pair_", "")
    await query.edit_message_text(f"⏳ Генерирую {pair_name}...")
    await send_spread_update(update.effective_chat.id, pair_name, context.bot)
    await context.bot.send_message(chat_id=update.effective_chat.id, text="Готово", reply_markup=MAIN_KEYBOARD)

# ======= ЗАПУСК =======
async def main():
    request = HTTPXRequest(read_timeout=30, write_timeout=30, connect_timeout=30, pool_timeout=30)
    application = Application.builder().token(TELEGRAM_TOKEN).request(request).build()
    await application.bot.delete_webhook()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_callback, pattern="^pair_"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, start))  # упрощённо

    scheduler = AsyncIOScheduler()
    scheduler.add_job(auto_send_all, 'interval', minutes=UPDATE_INTERVAL_MINUTES, args=[application.bot])
    scheduler.start()

    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True, allowed_updates=['message', 'callback_query'])
    logger.info("✅ Бот запущен")
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        scheduler.shutdown()

if __name__ == "__main__":
    asyncio.run(main())