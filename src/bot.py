from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

import httpx
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes

CBR_URL = "https://www.cbr.ru/scripts/XML_daily.asp"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 currency-bot/1.0"}

@dataclass(frozen=True)
class Rate:
    source: str
    pair: str
    value: float
    date: datetime

@dataclass(frozen=True)
class ForecastPoint:
    date: datetime
    value: float


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw else default


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_chat_ids(raw: str | None) -> list[int]:
    if not raw:
        return []
    return [int(item.strip()) for item in raw.split(",") if item.strip()]


def format_rub(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def subscribers_path() -> Path:
    return Path(os.getenv("SUBSCRIBERS_FILE", "subscribers.json"))


def load_subscribers() -> list[int]:
    path = subscribers_path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning("Файл подписчиков поврежден: %s", path)
        return []
    return sorted({int(chat_id) for chat_id in payload.get("chat_ids", [])})


def save_subscribers(chat_ids: Iterable[int]) -> None:
    payload = {"chat_ids": sorted({int(chat_id) for chat_id in chat_ids})}
    subscribers_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def add_subscriber(chat_id: int) -> bool:
    chat_ids = set(load_subscribers())
    before = len(chat_ids)
    chat_ids.add(chat_id)
    save_subscribers(chat_ids)
    return len(chat_ids) > before


def remove_subscriber(chat_id: int) -> bool:
    chat_ids = set(load_subscribers())
    if chat_id not in chat_ids:
        return False
    chat_ids.remove(chat_id)
    save_subscribers(chat_ids)
    return True


async def fetch_cbr_rates(client: httpx.AsyncClient) -> list[Rate]:
    params = {"date_req": datetime.now().strftime("%d/%m/%Y")}
    response = await client.get(CBR_URL, params=params)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    date = datetime.strptime(root.attrib["Date"], "%d.%m.%Y")
    wanted = {"USD": "USD/RUB", "EUR": "EUR/RUB"}
    rates: list[Rate] = []
    for valute in root.findall("Valute"):
        char_code = valute.findtext("CharCode")
        if char_code not in wanted:
            continue
        nominal = int(valute.findtext("Nominal") or "1")
        value = float((valute.findtext("Value") or "0").replace(",", "."))
        rates.append(Rate("ЦБ РФ", wanted[char_code], value / nominal, date))
    if len(rates) != 2:
        raise RuntimeError("Не удалось получить USD и EUR из ответа ЦБ РФ")
    return rates


async def fetch_yahoo_history(client: httpx.AsyncClient, symbol: str, range_: str = "6mo", interval: str = "1d") -> list[tuple[datetime, float]]:
    response = await client.get(YAHOO_CHART_URL.format(symbol=symbol), params={"range": range_, "interval": interval}, headers=YAHOO_HEADERS)
    response.raise_for_status()
    result = response.json()["chart"]["result"][0]
    timestamps = result["timestamp"]
    closes = result["indicators"]["quote"][0]["close"]
    points = [(datetime.fromtimestamp(ts), float(close)) for ts, close in zip(timestamps, closes) if close is not None]
    if not points:
        raise RuntimeError(f"Не удалось получить историю Yahoo Finance для {symbol}")
    return points


async def fetch_forex_rates(client: httpx.AsyncClient) -> list[Rate]:
    symbols = {"RUB=X": "USD/RUB", "EURRUB=X": "EUR/RUB"}
    rates: list[Rate] = []
    for symbol, pair in symbols.items():
        date, value = (await fetch_yahoo_history(client, symbol, range_="5d"))[-1]
        rates.append(Rate("Forex", pair, value, date))
    return rates


def forecast_usd(history: list[tuple[datetime, float]], days: int = 7) -> list[ForecastPoint]:
    recent = history[-60:] if len(history) >= 60 else history
    x = np.arange(len(recent), dtype=float)
    y = np.log([value for _, value in recent])
    slope, intercept = np.polyfit(x, y, deg=1)
    start = recent[-1][0]
    return [ForecastPoint(start + timedelta(days=offset), float(np.exp(intercept + slope * (len(recent) - 1 + offset)))) for offset in range(1, days + 1)]


def build_forecast_chart(history: list[tuple[datetime, float]], forecast: list[ForecastPoint], output_dir: Path) -> Path:
    chart_path = output_dir / "usd_rub_forecast.png"
    visible_history = history[-45:]
    hist_dates = [date for date, _ in visible_history]
    hist_values = [value for _, value in visible_history]
    forecast_dates = [point.date for point in forecast]
    forecast_values = [point.value for point in forecast]
    plt.style.use("seaborn-v0_8-whitegrid")
    fig, ax = plt.subplots(figsize=(10, 5.4), dpi=160)
    ax.plot(hist_dates, hist_values, label="USD/RUB Forex", color="#1f77b4", linewidth=2.4)
    ax.plot([hist_dates[-1], *forecast_dates], [hist_values[-1], *forecast_values], label="Прогноз на 7 дней", color="#d62728", linewidth=2.4, linestyle="--", marker="o", markersize=4)
    ax.set_title("USD/RUB: Forex и модельный прогноз", fontsize=15, pad=14)
    ax.set_ylabel("Рублей за 1 USD")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    ax.legend(loc="best")
    ax.margins(x=0.02)
    fig.autofmt_xdate(rotation=0)
    fig.tight_layout()
    fig.savefig(chart_path, bbox_inches="tight")
    plt.close(fig)
    return chart_path


def build_message(cbr_rates: Iterable[Rate], forex_rates: Iterable[Rate], forecast: list[ForecastPoint]) -> str:
    cbr = {rate.pair: rate for rate in cbr_rates}
    forex = {rate.pair: rate for rate in forex_rates}
    generated_at = datetime.now(ZoneInfo(os.getenv("TIMEZONE", "Asia/Novosibirsk")))
    return "\n".join([
        f"Курсы валют на {generated_at:%d.%m.%Y %H:%M}",
        "",
        "ЦБ РФ:",
        f"USD/RUB: {format_rub(cbr['USD/RUB'].value)}",
        f"EUR/RUB: {format_rub(cbr['EUR/RUB'].value)}",
        "",
        "Forex:",
        f"USD/RUB: {format_rub(forex['USD/RUB'].value)}",
        f"EUR/RUB: {format_rub(forex['EUR/RUB'].value)}",
        "",
        f"Прогноз USD/RUB через 7 дней: {format_rub(forecast[-1].value)}",
        "Прогноз модельный, не финансовая рекомендация.",
    ])


async def build_report() -> tuple[str, Path]:
    with tempfile.TemporaryDirectory() as tmp_dir:
        async with httpx.AsyncClient(timeout=env_int("HTTP_TIMEOUT_SECONDS", 20)) as client:
            cbr_rates = await fetch_cbr_rates(client)
            forex_rates = await fetch_forex_rates(client)
            usd_history = await fetch_yahoo_history(client, "RUB=X")
        forecast = forecast_usd(usd_history)
        chart_path = build_forecast_chart(usd_history, forecast, Path(tmp_dir))
        persistent_chart_path = Path.cwd() / "last_usd_rub_forecast.png"
        persistent_chart_path.write_bytes(chart_path.read_bytes())
        return build_message(cbr_rates, forex_rates, forecast), persistent_chart_path


async def send_report_to_chat(application: Application, chat_id: int) -> None:
    message, chart_path = await build_report()
    await application.bot.send_message(chat_id=chat_id, text=message)
    with chart_path.open("rb") as chart:
        await application.bot.send_photo(chat_id=chat_id, photo=chart)


async def send_report_with_bot(bot: Bot, chat_id: int) -> None:
    message, chart_path = await build_report()
    await bot.send_message(chat_id=chat_id, text=message)
    with chart_path.open("rb") as chart:
        await bot.send_photo(chat_id=chat_id, photo=chart)


async def send_scheduled_report(application: Application) -> None:
    chat_ids = load_subscribers()
    if not chat_ids:
        logging.warning("Список подписчиков пустой, ежедневный отчет не отправлен")
        return
    for chat_id in chat_ids:
        try:
            await send_report_to_chat(application, chat_id)
        except Exception:
            logging.exception("Не удалось отправить отчет в chat_id=%s", chat_id)


async def send_once(token: str) -> None:
    chat_ids = parse_chat_ids(os.getenv("TELEGRAM_CHAT_IDS"))
    if not chat_ids:
        raise RuntimeError("Для RUN_ONCE=true заполните TELEGRAM_CHAT_IDS")
    bot = Bot(token=token)
    for chat_id in chat_ids:
        await send_report_with_bot(bot, chat_id)


async def now_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return
    await update.effective_chat.send_message("Готовлю свежий отчет...")
    try:
        message, chart_path = await build_report()
        await update.effective_chat.send_message(message)
        with chart_path.open("rb") as chart:
            await update.effective_chat.send_photo(photo=chart)
    except Exception as exc:
        logging.exception("Ошибка команды /now")
        await update.effective_chat.send_message(f"Не удалось собрать отчет: {exc}")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return
    added = add_subscriber(update.effective_chat.id)
    prefix = "Готово, я подключил этот чат к ежедневной рассылке." if added else "Этот чат уже подключен к ежедневной рассылке."
    await update.effective_chat.send_message(f"{prefix}\n\nКаждый день в 09:00 по Новосибирску я буду отправлять курсы USD/RUB и EUR/RUB по ЦБ и Forex, а также график прогноза USD/RUB.\n\n/now — показать отчет сейчас\n/stop — отключить ежедневную рассылку")


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return
    removed = remove_subscriber(update.effective_chat.id)
    await update.effective_chat.send_message("Готово, ежедневная рассылка отключена для этого чата." if removed else "Этот чат не был подключен к рассылке.")


async def subscribers_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat:
        await update.effective_chat.send_message(f"Сейчас подключено чатов: {len(load_subscribers())}")


async def post_init(application: Application) -> None:
    scheduler = AsyncIOScheduler(timezone=ZoneInfo(os.getenv("TIMEZONE", "Asia/Novosibirsk")))
    scheduler.add_job(send_scheduled_report, "cron", hour=env_int("SEND_HOUR", 9), minute=env_int("SEND_MINUTE", 0), args=[application], id="daily_currency_report", replace_existing=True)
    scheduler.start()
    application.bot_data["scheduler"] = scheduler


async def post_shutdown(application: Application) -> None:
    scheduler = application.bot_data.get("scheduler")
    if scheduler:
        scheduler.shutdown(wait=False)


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Заполните TELEGRAM_BOT_TOKEN")
    if env_bool("RUN_ONCE"):
        asyncio.run(send_once(token))
        return
    application = Application.builder().token(token).post_init(post_init).post_shutdown(post_shutdown).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("now", now_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("subscribers", subscribers_command))
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
