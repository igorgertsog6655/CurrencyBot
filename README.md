# Telegram Currency Bot

Бот ежедневно в 07:00 по новосибирскому времени отправляет:

- курс доллара и евро к рублю по ЦБ РФ;
- текущие Forex-котировки USD/RUB и EUR/RUB;
- текущий курс BTC/USD;
- графики с модельным прогнозом USD/RUB, EUR/RUB и BTC/USD на 7 дней вперед.

## Источники

- ЦБ РФ: официальный XML `https://www.cbr.ru/scripts/XML_daily.asp`
- Forex и BTC: открытые котировки Yahoo Finance для `RUB=X`, `EURRUB=X` и `BTC-USD`

Прогноз не является финансовой рекомендацией. Это простая модельная экстраполяция по недавней истории Forex.

## Запуск через GitHub Actions

Если у вас нет сервера или компьютера для постоянного запуска, используйте инструкцию [GITHUB_ACTIONS.md](GITHUB_ACTIONS.md).

## Локальный запуск

1. Создайте бота через `@BotFather` и получите токен.
2. Скопируйте `.env.example` в `.env`.
3. Укажите `TELEGRAM_BOT_TOKEN`.
4. Установите зависимости:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

5. Запустите:

```powershell
python src\bot.py
```

6. Откройте своего бота в Telegram и нажмите **Start** или отправьте команду `/start`.
