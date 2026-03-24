# StandaloneWhitelistBot

Отдельные файлы Telegram-бота для whitelist.

Что умеет:
- проверяет подписку на канал или группу;
- разрешает максимум 3 ника на один Telegram-аккаунт;
- отклоняет ники с запрещёнными словами из `banned_words.json`;
- отправляет валидный ник на ваш bridge-сервер whitelist.

Как запустить:
1. Скопируйте `config.example.json` в `config.json`.
2. Заполните токен бота, `bridge_url`, `bridge_token` и канал.
3. Убедитесь, что бот добавлен в канал или группу, где нужно проверять подписку.
4. Запустите:

```powershell
python bot.py
```

Команды:
- `/start`
- `/help`
- `/request НИК`
- `/my_nicks`
