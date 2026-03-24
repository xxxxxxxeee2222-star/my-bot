import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STORAGE_PATH = BASE_DIR / "users.json"
BANNED_WORDS_PATH = BASE_DIR / "banned_words.json"
NICKNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{3,16}$")
ALLOWED_MEMBER_STATUSES = {"creator", "administrator", "member"}


def load_json(path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_json(path, data):
    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)


def load_config():
    config = load_json(CONFIG_PATH, {})
    required_keys = [
        "telegram_bot_token",
        "bridge_url",
        "bridge_token",
        "required_channel",
    ]
    missing = [key for key in required_keys if not config.get(key)]
    if missing:
        raise RuntimeError(
            "В config.json не заполнены обязательные поля: " + ", ".join(missing)
        )
    config.setdefault("poll_timeout_seconds", 30)
    config.setdefault("max_nicks_per_account", 3)
    config.setdefault("required_channel_url", "")
    return config


def load_storage():
    storage = load_json(STORAGE_PATH, {})
    return storage if isinstance(storage, dict) else {}


def load_banned_words():
    words = load_json(BANNED_WORDS_PATH, [])
    normalized = []
    for word in words:
        word = str(word).strip().lower()
        if word and word not in normalized:
            normalized.append(word)
    return normalized


def telegram_request(token, method, params=None):
    params = params or {}
    data = urllib.parse.urlencode(params).encode("utf-8")
    url = f"https://api.telegram.org/bot{token}/{method}"
    request = urllib.request.Request(url, data=data)

    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))

    if not payload.get("ok"):
        raise RuntimeError(payload.get("description", f"Telegram API error in {method}"))
    return payload["result"]


def send_message(token, chat_id, text):
    telegram_request(token, "sendMessage", {"chat_id": str(chat_id), "text": text})


def request_whitelist(config, telegram_id, nickname):
    payload = urllib.parse.urlencode(
        {
            "token": config["bridge_token"],
            "telegram_id": str(telegram_id),
            "nickname": nickname,
        }
    ).encode("utf-8")
    request = urllib.request.Request(config["bridge_url"], data=payload, method="POST")
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def is_subscribed(config, telegram_id):
    result = telegram_request(
        config["telegram_bot_token"],
        "getChatMember",
        {
            "chat_id": config["required_channel"],
            "user_id": str(telegram_id),
        },
    )
    status = result.get("status", "")
    return status in ALLOWED_MEMBER_STATUSES


def build_start_text(config):
    lines = [
        "Привет.",
        "",
        "Этот бот добавляет ники в whitelist.",
        "",
        f"1. Подпишись на группу/канал {config['required_channel']}",
        "2. Отправь команду: /request ТВОЙ_НИК",
        f"3. На один Telegram можно добавить максимум {config['max_nicks_per_account']} ника(ов)",
        "",
        "Команды:",
        "/request ник",
        "/my_nicks",
        "/help",
    ]
    if config.get("required_channel_url"):
        lines.insert(4, f"Ссылка: {config['required_channel_url']}")
    return "\n".join(lines)


def find_banned_fragment(nickname, banned_words):
    lowered = nickname.lower()
    for word in banned_words:
        if word in lowered:
            return word
    return None


def validate_nickname(nickname, banned_words):
    if not NICKNAME_PATTERN.fullmatch(nickname):
        return (
            False,
            "Ник должен быть как Minecraft-ник: только английские буквы, цифры и _, длина от 3 до 16 символов.",
        )

    banned_fragment = find_banned_fragment(nickname, banned_words)
    if banned_fragment:
        return (
            False,
            f"Этот ник нельзя использовать: найдено запрещённое слово `{banned_fragment}`.",
        )

    return True, ""


def handle_request(config, storage, banned_words, chat_id, telegram_id, nickname):
    token = config["telegram_bot_token"]
    nickname = nickname.strip()

    is_valid, reason = validate_nickname(nickname, banned_words)
    if not is_valid:
        send_message(token, chat_id, reason)
        return storage

    try:
        if not is_subscribed(config, telegram_id):
            message = (
                f"Сначала подпишись на {config['required_channel']}, "
                "а потом повтори команду /request."
            )
            if config.get("required_channel_url"):
                message += f"\nСсылка: {config['required_channel_url']}"
            send_message(token, chat_id, message)
            return storage
    except urllib.error.HTTPError as exc:
        send_message(
            token,
            chat_id,
            "Не удалось проверить подписку. Убедись, что бот добавлен в канал/группу и имеет доступ к участникам. "
            f"Код ошибки: {exc.code}",
        )
        return storage
    except Exception as exc:
        send_message(token, chat_id, f"Ошибка проверки подписки: {exc}")
        return storage

    current_nicks = storage.get(telegram_id, [])
    current_nicks_lower = {nick.lower() for nick in current_nicks}

    if nickname.lower() in current_nicks_lower:
        send_message(token, chat_id, f"Ник {nickname} уже привязан к этому Telegram.")
        return storage

    if len(current_nicks) >= int(config["max_nicks_per_account"]):
        send_message(
            token,
            chat_id,
            f"Лимит достигнут. Можно добавить только {config['max_nicks_per_account']} ника(ов) на один Telegram.",
        )
        return storage

    try:
        result = request_whitelist(config, telegram_id, nickname)
    except Exception as exc:
        send_message(token, chat_id, f"Ошибка связи с сервером whitelist: {exc}")
        return storage

    if not result.get("ok"):
        error = result.get("error", "unknown_error")
        if error == "invalid_nick":
            send_message(
                token,
                chat_id,
                "Сервер отклонил ник. Разрешены только Minecraft-ники: 3-16 символов, буквы, цифры и _.",
            )
        else:
            send_message(token, chat_id, f"Игрок не добавлен в whitelist: {error}")
        return storage

    current_nicks.append(nickname)
    storage[telegram_id] = current_nicks
    save_json(STORAGE_PATH, storage)
    send_message(token, chat_id, f"Готово. Ник {nickname} добавлен в whitelist.")
    return storage


def process_message(config, storage, banned_words, message):
    token = config["telegram_bot_token"]
    chat_id = message["chat"]["id"]
    telegram_id = str(message["from"]["id"])
    text = message.get("text", "").strip()

    if not text:
        return storage

    if text in {"/start", "/help"}:
        send_message(token, chat_id, build_start_text(config))
        return storage

    if text == "/my_nicks":
        nicks = storage.get(telegram_id, [])
        if not nicks:
            send_message(token, chat_id, "У тебя пока нет привязанных ников.")
        else:
            send_message(token, chat_id, "Твои ники:\n- " + "\n- ".join(nicks))
        return storage

    if text.startswith("/request "):
        nickname = text.split(" ", 1)[1]
        return handle_request(config, storage, banned_words, chat_id, telegram_id, nickname)

    send_message(
        token,
        chat_id,
        "Использование:\n/request ТВОЙ_НИК\n/my_nicks\n/help",
    )
    return storage


def ensure_runtime_files():
    if not STORAGE_PATH.exists():
        save_json(STORAGE_PATH, {})


def main():
    ensure_runtime_files()
    config = load_config()
    storage = load_storage()
    banned_words = load_banned_words()
    token = config["telegram_bot_token"]
    offset = 0

    while True:
        try:
            updates = telegram_request(
                token,
                "getUpdates",
                {
                    "timeout": int(config["poll_timeout_seconds"]),
                    "offset": offset,
                },
            )
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message")
                if message:
                    storage = process_message(config, storage, banned_words, message)
        except Exception as exc:
            print(f"Bot loop error: {exc}")
            time.sleep(5)


if __name__ == "__main__":
    main()
