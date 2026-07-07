# Запуск через GitHub Actions

Этот режим подходит, если у вас нет сервера. GitHub будет запускать бота каждый день в 09:00 по Новосибирску, бот отправит отчет и завершит работу.

В этом режиме команды `/start`, `/now` и `/stop` не работают постоянно, потому что бот не запущен 24/7.

## 1. Получите chat id

1. Напишите любое сообщение своему Telegram-боту.
2. Откройте в браузере:

```text
https://api.telegram.org/botВАШ_ТОКЕН/getUpdates
```

3. Найдите число в `message.chat.id`.

## 2. Добавьте секреты GitHub

В репозитории откройте:

```text
Settings -> Secrets and variables -> Actions -> New repository secret
```

Добавьте два секрета:

```text
TELEGRAM_BOT_TOKEN=ваш_токен_от_BotFather
TELEGRAM_CHAT_IDS=ваш_chat_id
```

Если нужно отправлять в несколько чатов, укажите через запятую:

```text
TELEGRAM_CHAT_IDS=123456789,987654321
```

## 3. Проверьте вручную

Откройте:

```text
Actions -> Daily Currency Report -> Run workflow
```

Если все настроено верно, бот пришлет отчет в Telegram.

Дальше GitHub будет запускать workflow каждый день в 02:00 UTC, что соответствует 09:00 в Новосибирске.
