from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
from dotenv import load_dotenv
from telegram import Bot

CBR_URL = "https://www.cbr.ru/scripts/XML_daily.asp"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 currency-bot/1.0"}
MARKET_SYMBOLS = {"USD/RUB": "RUB=X", "EUR/RUB": "EURRUB=X", "BTC/RUB": "BTC-RUB"}


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


def parse_chat_ids(raw: str | None) -> list[int]:
    return [] if not raw else [int(item.strip()) for item in raw.split(",") if item.strip()]


def format_rub(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


async def fetch_cbr_rates(client: httpx.AsyncClient) -> list[Rate]:
    response = await client.get(CBR_URL, params={"date_req": datetime.now().strftime("%d/%m/%Y")})
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


async def fetch_yahoo_history(
    client: httpx.AsyncClient,
    symbol: str,
    range_: str = "6mo",
    interval: str = "1d",
) -> list[tuple[datetime, float]]:
    response = await client.get(
        YAHOO_CHART_URL.format(symbol=symbol),
        params={"range": range_, "interval": interval},
        headers=YAHOO_HEADERS,
    )
    response.raise_for_status()
    result = response.json()["chart"]["result"][0]
    points = [
        (datetime.fromtimestamp(timestamp), float(close))
        for timestamp, close in zip(result["timestamp"], result["indicators"]["quote"][0]["close"])
        if close is not None
    ]
    if not points:
        raise RuntimeError(f"Не удалось получить историю Yahoo Finance для {symbol}")
    return points


async def fetch_market_rates(client: httpx.AsyncClient) -> list[Rate]:
    rates: list[Rate] = []
    for pair, symbol in MARKET_SYMBOLS.items():
        date, value = (await fetch_yahoo_history(client, symbol, range_="5d"))[-1]
        rates.append(Rate("Crypto" if pair == "BTC/RUB" else "Forex", pair, value, date))
    return rates


def forecast_rate(history: list[tuple[datetime, float]], days: int = 7) -> list[ForecastPoint]:
    recent = history[-60:] if len(history) >= 60 else history
    x = np.arange(len(recent), dtype=float)
    y = np.log([value for _, value in recent])
    slope, intercept = np.polyfit(x, y, deg=1)
    start = recent[-1][0]
    return [
        ForecastPoint(start + timedelta(days=offset), float(np.exp(intercept + slope * (len(recent) - 1 + offset))))
        for offset in range(1, days + 1)
    ]


def build_chart(
    history: list[tuple[datetime, float]],
    forecast: list[ForecastPoint],
    output_dir: Path,
    pair: str,
    file_stem: str,
    source: str,
) -> Path:
    chart_path = output_dir / f"{file_stem}_forecast.png"
    visible_history = history[-45:]
    hist_dates = [date for date, _ in visible_history]
    hist_values = [value for _, value in visible_history]
    forecast_dates = [point.date for point in forecast]
    forecast_values = [point.value for point in forecast]

    plt.style.use("seaborn-v0_8-whitegrid")
    fig, ax = plt.subplots(figsize=(10, 5.4), dpi=160)
    ax.plot(hist_dates, hist_values, label=f"{pair} {source}", color="#1f77b4", linewidth=2.4)
    ax.plot(
        [hist_dates[-1], *forecast_dates],
        [hist_values[-1], *forecast_values],
        label="Прогноз на 7 дней",
        color="#d62728",
        linewidth=2.4,
        linestyle="--",
        marker="o",
        markersize=4,
    )
    ax.set_title(f"{pair}: {source} и модельный прогноз", fontsize=15, pad=14)
    ax.set_ylabel("Рублей")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    ax.legend(loc="best")
    ax.margins(x=0.02)
    fig.autofmt_xdate(rotation=0)
    fig.tight_layout()
    fig.savefig(chart_path, bbox_inches="tight")
    plt.close(fig)
    return chart_path


def build_message(cbr_rates: list[Rate], market_rates: list[Rate], forecasts: dict[str, list[ForecastPoint]]) -> str:
    cbr = {rate.pair: rate for rate in cbr_rates}
    market = {rate.pair: rate for rate in market_rates}
    generated_at = datetime.now(ZoneInfo(os.getenv("TIMEZONE", "Asia/Novosibirsk")))
    return "\n".join(
        [
            f"Курсы валют на {generated_at:%d.%m.%Y %H:%M}",
            "",
            "ЦБ РФ:",
            f"USD/RUB: {format_rub(cbr['USD/RUB'].value)}",
            f"EUR/RUB: {format_rub(cbr['EUR/RUB'].value)}",
            "",
            "Forex:",
            f"USD/RUB: {format_rub(market['USD/RUB'].value)}",
            f"EUR/RUB: {format_rub(market['EUR/RUB'].value)}",
            "",
            "Crypto:",
            f"BTC/RUB: {format_rub(market['BTC/RUB'].value)}",
            "",
            "Прогноз через 7 дней:",
            f"USD/RUB: {format_rub(forecasts['USD/RUB'][-1].value)}",
            f"EUR/RUB: {format_rub(forecasts['EUR/RUB'][-1].value)}",
            f"BTC/RUB: {format_rub(forecasts['BTC/RUB'][-1].value)}",
            "Прогноз модельный, не финансовая рекомендация.",
        ]
    )


async def build_report() -> tuple[str, list[Path]]:
    with tempfile.TemporaryDirectory() as tmp_dir:
        async with httpx.AsyncClient(timeout=env_int("HTTP_TIMEOUT_SECONDS", 20)) as client:
            cbr_rates = await fetch_cbr_rates(client)
            market_rates = await fetch_market_rates(client)
            histories = {pair: await fetch_yahoo_history(client, symbol) for pair, symbol in MARKET_SYMBOLS.items()}

        forecasts = {pair: forecast_rate(history) for pair, history in histories.items()}
        chart_specs = [("USD/RUB", "usd_rub", "Forex"), ("EUR/RUB", "eur_rub", "Forex"), ("BTC/RUB", "btc_rub", "Crypto")]
        charts = [
            build_chart(histories[pair], forecasts[pair], Path(tmp_dir), pair, file_stem, source)
            for pair, file_stem, source in chart_specs
        ]
        persistent_charts: list[Path] = []
        for chart in charts:
            persistent_chart = Path.cwd() / chart.name
            persistent_chart.write_bytes(chart.read_bytes())
            persistent_charts.append(persistent_chart)

        return build_message(cbr_rates, market_rates, forecasts), persistent_charts


async def send_report(token: str, chat_id: int) -> None:
    bot = Bot(token=token)
    message, charts = await build_report()
    await bot.send_message(chat_id=chat_id, text=message)
    for chart_path in charts:
        with chart_path.open("rb") as chart:
            await bot.send_photo(chat_id=chat_id, photo=chart)


async def main_async() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_ids = parse_chat_ids(os.getenv("TELEGRAM_CHAT_IDS"))
    if not token:
        raise RuntimeError("Заполните TELEGRAM_BOT_TOKEN")
    if not chat_ids:
        raise RuntimeError("Заполните TELEGRAM_CHAT_IDS")
    for chat_id in chat_ids:
        await send_report(token, chat_id)


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
