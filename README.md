# Номера96 Telegram-бот

## Railway variables

Обязательная переменная:

```text
BOT_TOKEN=новый токен от BotFather
```

Дополнительные:

```text
CALL_TO_ACTION=Понравился номер? Пиши в Direct
OWNER_ID=
```

Сначала можно оставить `OWNER_ID` пустым. После запуска сразу откройте бота,
нажмите Start и отправьте `/myid`. Полученное число добавьте в Railway как
`OWNER_ID`, затем выполните Redeploy.

## Запуск

Railway автоматически обнаружит Dockerfile и запустит:

```text
python bot.py
```
