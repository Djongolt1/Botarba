#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import subprocess
import sys
import io
import logging
import asyncio
import os
from datetime import datetime, timedelta
import json
import time

# ===== АВТОУСТАНОВКА НЕДОСТАЮЩИХ БИБЛИОТЕК =====
required_packages = [
    "matplotlib",
    "pandas",
    "python-telegram-bot[job-queue]",
    "apscheduler",
    "requests"
]

for package in required_packages:
    try:
        if package.startswith("python-telegram-bot"):
            import telegram
        elif package == "pandas":
            import pandas
        elif package == "matplotlib":
            import matplotlib
        elif package == "apscheduler":
            import apscheduler
        elif package == "requests":
            import requests
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", package.split("[")[0]])

import pandas as pd
import matplotlib.pyplot as plt
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest
from telegram.error import BadRequest
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
TELEGRAM_TOKEN = "8885452009:AAGQ2c2qTZkqrLm_E66zehioGkG53D5lrls"
CHAT_ID = 1234329121
UPDATE_INTERVAL_MINUTES = 60
# Для 15-минутных свечей достаточно 2-х дней, чтобы не перегружать запрос
DAYS_BACK = 2               
# 15 минут
CANDLE_INTERVAL = 15        

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

# ======= НАСТРОЙКА HTTP-СЕССИИ С ПОВТОРАМИ =======
def get_http_session():
    session = requests.Session()
    # Увеличиваем количество попыток и уменьшаем таймаут для быстрого переключения
    retries = Retry(total=5, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

session = get_http_session()

# ======= УНИВЕРСАЛЬНАЯ ФУНКЦИЯ ЗАГРУЗКИ СВЕЧЕЙ =======
def fetch_candles(ticker, start_date, end_date, interval=CANDLE_INTERVAL):
    """
    Загружает свечи для тикера через MOEX ISS API с заданным интервалом (в минутах).
    """
    params = {
        'from': start_date.strftime('%Y-%m-%d'),
        'till': end_date.strftime('%Y-%m-%d'),
        'interval': interval,
        'iss.json': 'extended',
    }
    boards = ['TQBR', None]

    for board in boards:
        if board:
            url = f'https://iss.moex.com/iss/engines/stock/markets/shares/boards/{board}/securities/{ticker}/candles.json'
        else:
            url = f'https://iss.moex.com/iss/engines/stock/markets/shares/securities/{ticker}/candles.json'

        try:
            # Уменьшаем таймаут до 30 секунд, но увеличиваем число попыток
            resp = session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            # Извлекаем блок candles
            candles_block = None
            if isinstance(data, dict) and 'candles' in data:
                candles_block = data['candles']
            elif isinstance(data, list) and len(data) >= 2:
                for item in data:
                    if isinstance(item, dict) and 'candles' in item:
                        candles_block = item['candles']
                        break

            if candles_block is None:
                logger.warning(f"Не найден блок 'candles' для {ticker} (доска {board})")
                continue

            # --- Попытка распарсить в формате словаря с columns/data ---
            if isinstance(candles_block, dict):
                columns = candles_block.get('columns')
                rows = candles_block.get('data')
                if columns and rows:
                    df = pd.DataFrame(rows, columns=columns)
                    df['begin'] = pd.to_datetime(df['begin'])
                    df.set_index('begin', inplace=True)
                    return df[['close']]

            # --- Попытка распарсить в формате списка с объектами (без columns) ---
            if isinstance(candles_block, list) and len(candles_block) >= 2:
                for item in candles_block:
                    if isinstance(item, list) and len(item) > 0 and isinstance(item[0], dict):
                        rows = item
                        if 'begin' in rows[0]:
                            df = pd.DataFrame(rows)
                            df['begin'] = pd.to_datetime(df['begin'])
                            df.set_index('begin', inplace=True)
                            return df[['close']]
                        else:
                            logger.warning(f"Нет ключа 'begin' в данных для {ticker} (доска {board})")
                            break

            logger.warning(f"Не удалось распарсить ответ для {ticker} (доска {board})")

        except Exception as e:
            logger.warning(f"Ошибка при запросе {ticker} (доска {board}): {e}")
            continue

    return None

def get_spread_data(ticker1, ticker2, start_date, end_date):
    """Загружает данные для двух тикеров и вычисляет спред."""
    try:
        df1 = fetch_candles(ticker1, start_date, end_date)
        df2 = fetch_candles(ticker2, start_date, end_date)

        if df1 is None or df2 is None or df1.empty or df2.empty:
            logger.warning(f"Не удалось получить данные для {ticker1} или {ticker2}")
            return None

        combined = pd.merge(df1, df2, left_index=True, right_index=True, suffixes=(f'_{ticker1}', f'_{ticker2}'))
        combined.dropna(inplace=True)

        if combined.empty:
            return None

        combined['Spread'] = combined[f'close_{ticker1}'] - combined[f'close_{ticker2}']
        logger.info(f"Загружено {len(combined)} записей спреда для {ticker1}/{ticker2}")
        return combined

    except Exception as e:
        logger.error(f"Ошибка в get_spread_data: {e}")
        return None

# ======= ГЕНЕРАЦИЯ ГРАФИКА =======
def generate_spread_plot(pair_name):
    if pair_name not in PAIRS:
        return None, f"Пара '{pair_name}' не найдена."

    ticker1, ticker2 = PAIRS[pair_name]
    now = datetime.now()
    end_date = now.date() + timedelta(days=1)
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

    title_start = start_date.isoformat()
    title_end = (end_date - timedelta(days=1)).isoformat()
    ax.set_title(f'{pair_name} — спред ({ticker1} – {ticker2})\n{title_start} – {title_end} (интервал {CANDLE_INTERVAL} мин)', fontsize=14)
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
        logger.info(f"Генерация графика для {pair_name} для чата {chat_id}")
        buf, err = generate_spread_plot(pair_name)
        if err:
            await bot.send_message(chat_id=chat_id, text=f"❌ {err}")
            return
        await bot.send_photo(
            chat_id=chat_id,
            photo=buf,
            caption=f"📊 Спред {pair_name} ({PAIRS[pair_name][0]} – {PAIRS[pair_name][1]}) за последние {DAYS_BACK} дней (15-минутные свечи)"
        )
        logger.info(f"График для {pair_name} отправлен")
    except Exception as e:
        logger.exception(f"Ошибка при отправке {pair_name}")
        await bot.send_message(chat_id=chat_id, text=f"❌ Критическая ошибка для {pair_name}: {e}")

# ======= АВТООТПРАВКА ВСЕХ ПАР =======
async def auto_send_all(bot):
    logger.info("Запуск автоматической отправки всех пар")
    for name in PAIRS:
        await send_spread_update(CHAT_ID, name, bot)
        await asyncio.sleep(3)
    logger.info("Автоотправка всех пар завершена")

# ======= КОМАНДЫ И ОБРАБОТЧИКИ =======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"Команда /start от пользователя {user.id} (@{user.username})")
    keyboard = [[InlineKeyboardButton(name, callback_data=f"pair_{name}")] for name in PAIRS]
    await update.message.reply_text(
        f"📈 Бот спредов российских акций.\n"
        f"Автоотправка всех пар каждые {UPDATE_INTERVAL_MINUTES} мин.\n"
        f"Используемый интервал свечей: {CANDLE_INTERVAL} мин.\n\n"
        f"Доступные пары:\n" + "\n".join(f"• {n} ({t1}/{t2})" for n, (t1, t2) in PAIRS.items()),
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    await update.message.reply_text("Кнопки управления всегда под рукой 👇", reply_markup=MAIN_KEYBOARD)

async def pairs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"Команда /pairs от пользователя {user.id}")
    keyboard = [[InlineKeyboardButton(name, callback_data=f"pair_{name}")] for name in PAIRS]
    await update.message.reply_text("Выберите пару:", reply_markup=InlineKeyboardMarkup(keyboard))
    await update.message.reply_text("Кнопки управления всегда под рукой 👇", reply_markup=MAIN_KEYBOARD)

async def spread_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"Команда /spread от пользователя {user.id}, пара по умолчанию: {DEFAULT_PAIR}")
    await update.message.reply_text(f"🔄 Загружаю {DEFAULT_PAIR}...")
    await send_spread_update(update.effective_chat.id, DEFAULT_PAIR, context.bot)
    await update.message.reply_text("Кнопки управления всегда под рукой 👇", reply_markup=MAIN_KEYBOARD)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except BadRequest as e:
        if "Query is too old" in str(e) or "query id is invalid" in str(e):
            logger.warning(f"Пропущен устаревший колбэк: {e}")
            return
        else:
            raise

    pair_name = query.data.replace("pair_", "")
    user = update.effective_user
    logger.info(f"Callback от пользователя {user.id}: выбрана пара '{pair_name}'")

    if pair_name not in PAIRS:
        logger.warning(f"Неизвестная пара в callback: {pair_name}")
        await query.edit_message_text("❌ Неизвестная пара")
        return

    await query.edit_message_text(f"⏳ Генерирую график для {pair_name}...")
    await send_spread_update(update.effective_chat.id, pair_name, context.bot)
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Кнопки управления всегда под рукой 👇",
        reply_markup=MAIN_KEYBOARD
    )

async def handle_main_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user = update.effective_user
    logger.info(f"Нажата Reply-кнопка: '{text}' от пользователя {user.id}")
    if text == "🏠 Старт":
        await start(update, context)
    elif text == "📊 Пары":
        await pairs_command(update, context)
    else:
        await update.message.reply_text("Используйте кнопки ниже:", reply_markup=MAIN_KEYBOARD)

# ======= ЗАПУСК =======
async def main():
    request = HTTPXRequest(
        read_timeout=30,
        write_timeout=30,
        connect_timeout=30,
        pool_timeout=30
    )

    application = Application.builder() \
        .token(TELEGRAM_TOKEN) \
        .request(request) \
        .build()

    await application.bot.delete_webhook()
    logger.info("Вебхук удалён, теперь бот работает через поллинг.")

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
    await application.updater.start_polling(
        bootstrap_retries=5,
        drop_pending_updates=True,
        allowed_updates=['message', 'callback_query', 'my_chat_member', 'chat_member']
    )

    logger.info(f"✅ Бот успешно запущен. Пары: {list(PAIRS.keys())}. Автоотправка каждые {UPDATE_INTERVAL_MINUTES} мин.")

    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановка бота...")
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        scheduler.shutdown()
        logger.info("Бот остановлен.")

    if os.name == "nt":
        msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    lock_fd.close()
    os.remove(LOCK_FILE)

if __name__ == "__main__":
    asyncio.run(main())