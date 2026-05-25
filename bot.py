import json
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR)).resolve()
CONFIG_PATH = BASE_DIR / "config.json"
STORAGE_PATH = DATA_DIR / "users.json"
BANNED_WORDS_PATH = BASE_DIR / "banned_words.json"
NICKNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{3,16}$")
ALLOWED_MEMBER_STATUSES = {"creator", "administrator", "member"}


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        broken_path = path.with_name(f"{path.name}.broken-{int(time.time())}")
        shutil.move(str(path), str(broken_path))
        print(f"Broken JSON in {path}: {exc}. Moved to {broken_path}")
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")
    temp_path.replace(path)


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
    if not config.get("required_channels"):
        config["required_channels"] = [
            {
                "chat_id": config["required_channel"],
                "url": config.get("required_channel_url", ""),
            }
        ]
    config.setdefault("hub_bridge_url", "")
    config.setdefault("hub_bridge_token", config.get("bridge_token", ""))
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


def request_bridge(bridge_url, bridge_token, telegram_id, nickname):
    payload = urllib.parse.urlencode(
        {
            "token": bridge_token,
            "telegram_id": str(telegram_id),
            "nickname": nickname,
        }
    ).encode("utf-8")
    request = urllib.request.Request(bridge_url, data=payload, method="POST")
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def request_whitelist(config, telegram_id, nickname):
    return request_bridge(config["bridge_url"], config["bridge_token"], telegram_id, nickname)


def request_hub_access(config, telegram_id, nickname):
    hub_bridge_url = config.get("hub_bridge_url", "")
    if not hub_bridge_url:
        return {"ok": True, "skipped": True}

    hub_bridge_token = config.get("hub_bridge_token") or config["bridge_token"]
    return request_bridge(hub_bridge_url, hub_bridge_token, telegram_id, nickname)


def required_channels(config):
    channels = []
    for channel in config.get("required_channels", []):
        if isinstance(channel, dict):
            chat_id = str(channel.get("chat_id", "")).strip()
            url = str(channel.get("url", "")).strip()
        else:
            chat_id = str(channel).strip()
            url = ""

        if chat_id:
            channels.append({"chat_id": chat_id, "url": url})
    return channels


def channel_label(channel):
    return channel["url"] or channel["chat_id"]


def format_required_channels(config):
    return "\n".join(f"- {channel_label(channel)}" for channel in required_channels(config))


def missing_subscriptions(config, telegram_id):
    missing = []
    for channel in required_channels(config):
        result = telegram_request(
            config["telegram_bot_token"],
            "getChatMember",
            {
                "chat_id": channel["chat_id"],
                "user_id": str(telegram_id),
            },
        )
        status = result.get("status", "")
        if status not in ALLOWED_MEMBER_STATUSES:
            missing.append(channel)
    return missing


def is_subscribed(config, telegram_id):
    return not missing_subscriptions(config, telegram_id)


def build_subscription_required_message(config, missing):
    lines = [
        "Сначала подпишись на все группы/каналы, а потом повтори команду /request.",
        "",
        "Нужно подписаться:",
    ]
    lines.extend(f"- {channel_label(channel)}" for channel in missing)
    return "\n".join(lines)


def is_subscribed_old(config, telegram_id):
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
    channels_text = format_required_channels(config)
    if channels_text:
        lines.insert(4, "Ссылки:\n" + channels_text)
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
        missing = missing_subscriptions(config, telegram_id)
        if missing:
            send_message(token, chat_id, build_subscription_required_message(config, missing))
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

    try:
        hub_result = request_hub_access(config, telegram_id, nickname)
    except Exception as exc:
        send_message(token, chat_id, f"РќРёРє РґРѕР±Р°РІР»РµРЅ РІ whitelist, РЅРѕ hub РґРѕСЃС‚СѓРї РЅРµ РІС‹РґР°РЅ: {exc}")
        return storage

    if not hub_result.get("ok"):
        error = hub_result.get("error", "unknown_error")
        send_message(token, chat_id, f"РќРёРє РґРѕР±Р°РІР»РµРЅ РІ whitelist, РЅРѕ hub РґРѕСЃС‚СѓРї РЅРµ РІС‹РґР°РЅ: {error}")
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
    STORAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    old_storage_path = BASE_DIR / "users.json"
    if STORAGE_PATH != old_storage_path and not STORAGE_PATH.exists() and old_storage_path.exists():
        shutil.copyfile(old_storage_path, STORAGE_PATH)
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
