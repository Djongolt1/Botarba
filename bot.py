import io
import logging
import asyncio
from datetime import datetime, timedelta

import pandas as pd
import matplotlib.pyplot as plt
from moexalgo import Ticker
from telegram import Bot
from telegram.ext import Application, CommandHandler
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ======= НАСТРОЙКИ =======
TELEGRAM_TOKEN = "8663503148:AAEr1Vss6v1vLHf47u1M-Vp_wbhIn0PLBd8"
CHAT_ID = 1234329121               # ID чата (число)
UPDATE_INTERVAL_MINUTES = 60       # как часто слать автоматические отчёты
DAYS_BACK = 5                      # период в днях (только Мечел)

# ======= ЛОГИРОВАНИЕ =======
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ======= ФУНКЦИЯ ЗАГРУЗКИ СПРЕДА =======
def get_spread_data(ticker1, ticker2, start_date, end_date):
    """Возвращает DataFrame с колонками close1, close2 и Spread"""
    try:
        t1 = Ticker(ticker1)
        t2 = Ticker(ticker2)
        try:
            data1 = t1.candles(start=start_date, end=end_date, period='15min')
            data2 = t2.candles(start=start_date, end=end_date, period='15min')
        except Exception:
            data1 = t1.candles(start=start_date, end=end_date)
            data2 = t2.candles(start=start_date, end=end_date)

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
        combined['Spread'] = combined[f'{ticker1}_close'] - combined[f'{ticker2}_close']
        # оставляем только с 7:00 утра
        combined = combined[combined.index.hour >= 7]
        if combined.empty:
            return None
        return combined
    except Exception as e:
        logger.error(f"Ошибка загрузки {ticker1}/{ticker2}: {e}")
        return None

# ======= ГЕНЕРАЦИЯ ГРАФИКА ТОЛЬКО ПО МЕЧЕЛУ =======
def generate_mechel_plot():
    """Создаёт график спреда MTLR - MTLRP за последние DAYS_BACK дней.
       Возвращает (BytesIO, ошибка)."""
    today = datetime.now().date()
    start_date = today - timedelta(days=DAYS_BACK)
    end_date = today

    mechel = get_spread_data('MTLR', 'MTLRP', start_date, end_date)
    if mechel is None or mechel.empty:
        return None, "Не удалось загрузить данные по Мечелу."

    # Строим один график
    fig, ax = plt.subplots(figsize=(12, 6))
    x = range(len(mechel))
    y = mechel['Spread'].values
    ax.plot(x, y, color='purple', linewidth=1.8, label='Спред MTLR - MTLRP')

    # Настройка подписей оси X (чтобы не было скученности)
    step = max(1, len(mechel) // 15)
    xticks_pos = list(range(0, len(mechel), step))
    xticks_labels = [mechel.index[i].strftime("%m-%d %H:%M") for i in xticks_pos]
    ax.set_xticks(xticks_pos)
    ax.set_xticklabels(xticks_labels, rotation=45, ha='right', fontsize=8)

    ax.set_title(f'Спред Мечел (обычка – преф) за последние {DAYS_BACK} дней\n{start_date} – {today}', fontsize=14)
    ax.set_ylabel('Разница (₽)')
    ax.set_xlabel('Время (порядок свечей)')
    ax.grid(True, alpha=0.3)
    ax.legend(loc='upper left')

    # Текущее значение спреда
    last_spread = y[-1]
    last_time = mechel.index[-1]
    ax.text(1.02, 0.90,
            f'Текущий спред: {last_spread:.2f} ₽\nВремя расчёта:\n{last_time.strftime("%Y-%m-%d %H:%M")}',
            transform=ax.transAxes, verticalalignment='top', horizontalalignment='left',
            bbox=dict(boxstyle='round', facecolor='white', alpha=0.8), fontsize=9)

    # Сохраняем в буфер
    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return buf, None

# ======= АСИНХРОННАЯ ОТПРАВКА ГРАФИКА =======
async def send_spread_update(context=None):
    """Генерирует и отправляет график Мечела."""
    logger.info("Генерация отчёта по Мечелу...")
    buf, err = generate_mechel_plot()
    if err:
        logger.error(err)
        if context and hasattr(context, 'bot'):
            await context.bot.send_message(chat_id=CHAT_ID, text=f"❌ Ошибка: {err}")
        return

    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.send_photo(chat_id=CHAT_ID, photo=buf, caption=f"📊 Спред Мечел (MTLR – MTLRP) за последние {DAYS_BACK} дней")

# ======= КОМАНДЫ БОТА =======
async def start(update, context):
    await update.message.reply_text(
        "Привет! Я бот для отслеживания спреда Мечел (MTLR – MTLRP).\n"
        "Используй команду /spread, чтобы получить текущий график за последние "
        f"{DAYS_BACK} дней.\n"
        f"Автоматическая отправка происходит каждые {UPDATE_INTERVAL_MINUTES} минут."
    )

async def spread_cmd(update, context):
    await update.message.reply_text("Генерирую график спреда Мечела, подождите...")
    await send_spread_update(context)

# ======= ЗАПУСК БОТА =======
async def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("spread", spread_cmd))

    # Планировщик
    scheduler = AsyncIOScheduler()
    scheduler.add_job(send_spread_update, 'interval', minutes=UPDATE_INTERVAL_MINUTES)
    scheduler.start()

    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    logger.info("Бот запущен и слушает команды (только Мечел, период 5 дней)...")

    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановка бота...")
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        scheduler.shutdown()

if __name__ == "__main__":
    asyncio.run(main())