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
DAYS_BACK = 7
CANDLE_INTERVAL = 60                    # Часовые свечи – стабильно работают за 7 дней

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

# ======= НАСТРОЙКА HTTP-СЕССИИ =======
def get_http_session():
    session = requests.Session()
    retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

session = get_http_session()

# ======= ПОЛУЧЕНИЕ ТЕКУЩЕЙ ЦЕНЫ =======
def get_last_price(ticker):
    for board in ['TQBR', None]:
        if board:
            url = f'https://iss.moex.com/iss/engines/stock/markets/shares/boards/{board}/securities/{ticker}.json?iss.only=marketdata&marketdata.columns=LAST'
        else:
            url = f'https://iss.moex.com/iss/engines/stock/markets/shares/securities/{ticker}.json?iss.only=marketdata&marketdata.columns=LAST'
        try:
            resp = session.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            rows = data.get('marketdata', {}).get('data', [])
            if rows and len(rows[0]) > 0:
                val = rows[0][0]
                if val is not None:
                    return float(val)
        except Exception as e:
            logger.warning(f"Не удалось получить последнюю цену для {ticker} (доска {board}): {e}")
            continue
    return None

def get_current_spread(pair_name):
    if pair_name not in PAIRS:
        return None
    t1, t2 = PAIRS[pair_name]
    p1 = get_last_price(t1)
    p2 = get_last_price(t2)
    if p1 is None or p2 is None:
        return None
    return p1, p2, p1 - p2

# ======= ФУНКЦИЯ ЗАГРУЗКИ СВЕЧЕЙ =======
def fetch_candles(ticker, start_date, end_date, interval=CANDLE_INTERVAL):
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
            resp = session.get(url, params=params, timeout=60)
            resp.raise_for_status()
            data = resp.json()

            # Ищем блок candles
            candles_block = None
            if isinstance(data, dict):
                candles_block = data.get('candles')
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and 'candles' in item:
                        candles_block = item['candles']
                        break

            if candles_block is None:
                continue

            # Извлекаем metadata и data
            metadata = None
            rows = None
            cols = None

            if isinstance(candles_block, dict):
                metadata = candles_block.get('metadata')
                rows = candles_block.get('data')
                if metadata is None:
                    cols = candles_block.get('columns')
                    rows = candles_block.get('data')
            elif isinstance(candles_block, list):
                if len(candles_block) >= 2:
                    first = candles_block[0]
                    second = candles_block[1]
                    if isinstance(first, dict) and 'metadata' in first:
                        metadata = first['metadata']
                        rows = second if isinstance(second, list) else None
                    elif isinstance(first, dict) and 'columns' in first:
                        cols = first['columns']
                        rows = second if isinstance(second, list) else None
                if metadata is None and cols is None:
                    for part in candles_block:
                        if isinstance(part, dict):
                            if 'metadata' in part:
                                metadata = part['metadata']
                            elif 'columns' in part:
                                cols = part['columns']
                            elif 'data' in part:
                                rows = part['data']
                            if (metadata or cols) and rows is not None:
                                break

            if (metadata is None and cols is None) or rows is None:
                continue

            if not isinstance(rows, list) or len(rows) == 0:
                return None

            columns = list(metadata.keys()) if metadata else cols
            df = pd.DataFrame(rows, columns=columns)
            if 'begin' not in df.columns:
                continue

            df['begin'] = pd.to_datetime(df['begin'])
            df.set_index('begin', inplace=True)
            return df[['close']]

        except Exception as e:
            logger.warning(f"Ошибка при запросе {ticker} (доска {board}): {e}")
            continue

    return None

def get_spread_data(ticker1, ticker2, start_date, end_date):
    try:
        df1 = fetch_candles(ticker1, start_date, end_date)
        df2 = fetch_candles(ticker2, start_date, end_date)

        if df1 is None or df2 is None or df1.empty or df2.empty:
            return None

        combined = pd.merge(df1, df2, left_index=True, right_index=True, suffixes=(f'_{ticker1}', f'_{ticker2}'))
        combined.dropna(inplace=True)

        if combined.empty:
            return None

        combined['Spread'] = combined[f'close_{ticker1}'] - combined[f'close_{ticker2}']
        return combined

    except Exception as e:
        logger.error(f"Ошибка в get_spread_data: {e}")
        return None

# ======= ГЕНЕРАЦИЯ ГРАФИКА С ТЕКУЩЕЙ ТОЧКОЙ =======
def generate_spread_plot(pair_name, current_spread):
    if pair_name not in PAIRS:
        return None, f"Пара '{pair_name}' не найдена."

    ticker1, ticker2 = PAIRS[pair_name]
    now = datetime.now()
    end_date = now.date() + timedelta(days=1)
    start_date = end_date - timedelta(days=DAYS_BACK)

    data = get_spread_data(ticker1, ticker2, start_date, end_date)
    if data is None or data.empty:
        if current_spread:
            return None, f"Нет исторических свечей за последние {DAYS_BACK} дней, но текущий спред: {current_spread[2]:.2f} ₽"
        else:
            return None, f"Нет данных за последние {DAYS_BACK} дней по паре {pair_name}."

    fig, ax = plt.subplots(figsize=(12, 6))
    x = list(range(len(data)))
    y = data['Spread'].values
    ax.plot(x, y, linewidth=1.8, label=f'Спред {ticker1} – {ticker2}')

    # Добавляем текущую точку, если есть
    if current_spread:
        new_x = len(data)
        new_y = current_spread[2]
        ax.scatter(new_x, new_y, color='red', s=120, zorder=5, label='Текущий спред')
        ax.axvline(x=new_x, color='gray', linestyle='--', alpha=0.5, linewidth=1)

    # Настройка подписей оси X
    step = max(1, len(data) // 15)
    xticks_pos = list(range(0, len(data), step))
    xticks_labels = [data.index[i].strftime("%m-%d %H:%M") for i in xticks_pos]
    if current_spread:
        xticks_pos.append(len(data))
        xticks_labels.append('Сейчас')
    ax.set_xticks(xticks_pos)
    ax.set_xticklabels(xticks_labels, rotation=45, ha='right', fontsize=8)

    title_start = start_date.isoformat()
    title_end = (end_date - timedelta(days=1)).isoformat()
    ax.set_title(f'{pair_name} — спред ({ticker1} – {ticker2})\n{title_start} – {title_end} (часовые свечи)', fontsize=14)
    ax.set_ylabel('Разница (₽)')
    ax.set_xlabel('Время')
    ax.grid(True, alpha=0.3)
    ax.legend(loc='upper left')

    # Подписи на графике
    last_spread = y[-1]
    last_time = data.index[-1]
    ax.text(1.02, 0.85,
            f'Последний спред (свечи): {last_spread:.2f} ₽\n{last_time.strftime("%Y-%m-%d %H:%M")}',
            transform=ax.transAxes, verticalalignment='top', horizontalalignment='left',
            bbox=dict(boxstyle='round', facecolor='white', alpha=0.8), fontsize=9)
    if current_spread:
        ax.text(1.02, 0.70,
                f'Текущий спред: {current_spread[2]:.2f} ₽\n{datetime.now().strftime("%Y-%m-%d %H:%M")}',
                transform=ax.transAxes, verticalalignment='top', horizontalalignment='left',
                bbox=dict(boxstyle='round', facecolor='#ffcccc', alpha=0.8), fontsize=10, weight='bold')

    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight', dpi=100)
    plt.close(fig)
    buf.seek(0)
    return buf, None

# ======= ОТПРАВКА ГРАФИКА =======
async def send_spread_update(chat_id, pair_name, bot):
    try:
        logger.info(f"Генерация графика для {pair_name}")
        current = get_current_spread(pair_name)
        buf, err = generate_spread_plot(pair_name, current)
        if err:
            await bot.send_message(chat_id=chat_id, text=f"❌ {err}")
            return

        caption = f"📊 Спред {pair_name} ({PAIRS[pair_name][0]} – {PAIRS[pair_name][1]}) за последние {DAYS_BACK} дней (часовые свечи)"
        if current:
            p1, p2, spread = current
            caption += f"\n\n🟢 Текущий спред: **{spread:.2f} ₽**\n{PAIRS[pair_name][0]}: {p1:.2f}  |  {PAIRS[pair_name][1]}: {p2:.2f}"
        else:
            caption += "\n\n⚠️ Не удалось получить текущие цены."

        await bot.send_photo(chat_id=chat_id, photo=buf, caption=caption)
        logger.info(f"График для {pair_name} отправлен")
    except Exception as e:
        logger.exception(f"Ошибка при отправке {pair_name}")
        await bot.send_message(chat_id=chat_id, text=f"❌ Критическая ошибка: {e}")

# ======= АВТООТПРАВКА =======
async def auto_send_all(bot):
    logger.info("Запуск автоматической отправки всех пар")
    for name in PAIRS:
        await send_spread_update(CHAT_ID, name, bot)
        await asyncio.sleep(3)
    logger.info("Автоотправка завершена")

# ======= КОМАНДЫ =======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"Команда /start от {user.id}")
    keyboard = [[InlineKeyboardButton(name, callback_data=f"pair_{name}")] for name in PAIRS]
    await update.message.reply_text(
        f"📈 Бот спредов российских акций.\n"
        f"Автоотправка каждые {UPDATE_INTERVAL_MINUTES} мин.\n"
        f"Используются часовые свечи (история 7 дней) + актуальный спред.\n"
        f"На графике красная точка – текущее значение.\n\n"
        f"Доступные пары:\n" + "\n".join(f"• {n} ({t1}/{t2})" for n, (t1, t2) in PAIRS.items()),
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    await update.message.reply_text("Кнопки управления:", reply_markup=MAIN_KEYBOARD)

async def pairs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(name, callback_data=f"pair_{name}")] for name in PAIRS]
    await update.message.reply_text("Выберите пару:", reply_markup=InlineKeyboardMarkup(keyboard))
    await update.message.reply_text("Кнопки управления:", reply_markup=MAIN_KEYBOARD)

async def spread_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🔄 Загружаю {DEFAULT_PAIR}...")
    await send_spread_update(update.effective_chat.id, DEFAULT_PAIR, context.bot)
    await update.message.reply_text("Кнопки управления:", reply_markup=MAIN_KEYBOARD)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except BadRequest as e:
        if "Query is too old" in str(e) or "query id is invalid" in str(e):
            logger.warning(f"Устаревший колбэк: {e}")
            return
        else:
            raise

    pair_name = query.data.replace("pair_", "")
    if pair_name not in PAIRS:
        await query.edit_message_text("❌ Неизвестная пара")
        return

    await query.edit_message_text(f"⏳ Генерирую график для {pair_name}...")
    await send_spread_update(update.effective_chat.id, pair_name, context.bot)
    await context.bot.send_message(chat_id=update.effective_chat.id, text="Кнопки управления:", reply_markup=MAIN_KEYBOARD)

async def handle_main_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "🏠 Старт":
        await start(update, context)
    elif text == "📊 Пары":
        await pairs_command(update, context)
    else:
        await update.message.reply_text("Используйте кнопки ниже:", reply_markup=MAIN_KEYBOARD)

# ======= ЗАПУСК =======
async def main():
    request = HTTPXRequest(read_timeout=30, write_timeout=30, connect_timeout=30, pool_timeout=30)
    application = Application.builder().token(TELEGRAM_TOKEN).request(request).build()

    await application.bot.delete_webhook()
    logger.info("Вебхук удалён, поллинг.")

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

    logger.info(f"✅ Бот запущен. Пары: {list(PAIRS.keys())}")

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
        logger.info("Бот остановлен.")

    if os.name == "nt":
        msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    lock_fd.close()
    os.remove(LOCK_FILE)

if __name__ == "__main__":
    asyncio.run(main())