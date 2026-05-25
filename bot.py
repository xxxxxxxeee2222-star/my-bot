import html
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
STATE_PATH = DATA_DIR / "state.json"
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
    except OSError as exc:
        print(f"Cannot read {path}: {exc}")
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")
    temp_path.replace(path)


def normalize_admin_ids(config):
    admin_ids = config.get("admin_ids", [])
    if isinstance(admin_ids, (str, int)):
        admin_ids = [admin_ids]
    return {str(admin_id).strip() for admin_id in admin_ids if str(admin_id).strip()}


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
        raise RuntimeError("В config.json не заполнены обязательные поля: " + ", ".join(missing))

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
    config.setdefault("admin_ids", [])
    return config


def normalize_storage(storage):
    if not isinstance(storage, dict):
        return {}

    normalized = {}
    for telegram_id, nicks in storage.items():
        if not isinstance(nicks, list):
            continue

        clean_nicks = []
        seen = set()
        for nick in nicks:
            nick = str(nick).strip()
            nick_key = nick.lower()
            if not nick or nick_key in seen:
                continue
            clean_nicks.append(nick)
            seen.add(nick_key)

        if clean_nicks:
            normalized[str(telegram_id)] = clean_nicks

    return normalized


def load_storage():
    storage = load_json(STORAGE_PATH, None)
    old_storage_path = BASE_DIR / "users.json"
    if storage is None and STORAGE_PATH != old_storage_path and old_storage_path.exists():
        shutil.copyfile(old_storage_path, STORAGE_PATH)
        storage = load_json(STORAGE_PATH, {})

    storage = normalize_storage(storage or {})
    save_json(STORAGE_PATH, storage)
    return storage


def load_state():
    state = load_json(STATE_PATH, {})
    return state if isinstance(state, dict) else {}


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


def build_channel_keyboard(config):
    buttons = []
    for channel in required_channels(config):
        if channel["url"]:
            buttons.append([{"text": channel_button_text(channel), "url": channel["url"]}])
    return {"inline_keyboard": buttons} if buttons else None


def send_message(token, chat_id, text, reply_markup=None):
    params = {
        "chat_id": str(chat_id),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    if reply_markup:
        params["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    telegram_request(token, "sendMessage", params)


def set_bot_commands(config):
    token = config["telegram_bot_token"]
    public_commands = [
        {"command": "request", "description": "добавить ник"},
        {"command": "my_nicks", "description": "мои ники"},
        {"command": "status", "description": "статус"},
        {"command": "help", "description": "помощь"},
    ]
    telegram_request(token, "setMyCommands", {"commands": json.dumps(public_commands, ensure_ascii=False)})

    admin_commands = public_commands + [
        {"command": "admin", "description": "админ-панель"},
        {"command": "add_nick", "description": "добавить ник игроку"},
        {"command": "del_nick", "description": "удалить ник из базы"},
        {"command": "find_nick", "description": "найти ник"},
        {"command": "all_nicks", "description": "все ники"},
    ]
    for admin_id in normalize_admin_ids(config):
        telegram_request(
            token,
            "setMyCommands",
            {
                "commands": json.dumps(admin_commands, ensure_ascii=False),
                "scope": json.dumps({"type": "chat", "chat_id": int(admin_id)}),
            },
        )


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


def channel_button_text(channel):
    label = channel["chat_id"].lstrip("@")
    return label or "Открыть канал"


def channel_label(channel):
    if channel["url"]:
        return f'<a href="{html.escape(channel["url"])}">{html.escape(channel_button_text(channel))}</a>'
    return html.escape(channel["chat_id"])


def format_required_channels(config):
    channels = required_channels(config)
    if not channels:
        return "Каналы не настроены."
    return "\n".join(f"• {channel_label(channel)}" for channel in channels)


def storage_stats(storage):
    accounts = len(storage)
    nick_count = sum(len(nicks) for nicks in storage.values())
    return accounts, nick_count


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


def build_subscription_required_message(config, missing):
    lines = [
        "<b>Подписка не найдена</b>",
        "",
        "Перед добавлением ника подпишись на все каналы ниже, потом снова отправь:",
        "<code>/request ТВОЙ_НИК</code>",
        "",
        "<b>Нужно подписаться:</b>",
    ]
    lines.extend(f"• {channel_label(channel)}" for channel in missing)
    return "\n".join(lines)


def build_start_text(config, storage=None):
    lines = [
        "<b>MiridesSMP whitelist</b>",
        "",
        "Бот добавляет Minecraft-ник в whitelist сервера.",
        "",
        "<b>Как войти:</b>",
        "1. Подпишись на каналы ниже.",
        "2. Отправь команду <code>/request ТВОЙ_НИК</code>.",
        f"3. Лимит: <b>{html.escape(str(config['max_nicks_per_account']))}</b> ника на один Telegram.",
        "",
        "<b>Каналы:</b>",
        format_required_channels(config),
        "",
        "<b>Команды:</b>",
        "<code>/request ник</code> - добавить ник",
        "<code>/my_nicks</code> - мои ники",
        "<code>/status</code> - статус бота",
        "<code>/help</code> - помощь",
    ]
    return "\n".join(lines)


def build_status_text(config, storage, telegram_id):
    own_nicks = storage.get(telegram_id, [])
    servers_status = "работают" if config.get("hub_bridge_url") else "работает"
    nicks_text = "нет" if not own_nicks else "\n".join(f"• {html.escape(nick)}" for nick in own_nicks)
    return "\n".join(
        [
            "<b>Статус</b>",
            "",
            f"Сервера: <b>{servers_status}</b>",
            f"Твои ники: <b>{len(own_nicks)}</b> / <b>{html.escape(str(config['max_nicks_per_account']))}</b>",
            "",
            nicks_text,
        ]
    )


def build_all_nicks_text(storage):
    accounts, nick_count = storage_stats(storage)
    lines = [f"<b>Ники в базе:</b> {nick_count} / Telegram: {accounts}", ""]
    for telegram_id in sorted(storage.keys(), key=lambda value: (0, int(value)) if value.isdigit() else (1, value)):
        nicks = ", ".join(html.escape(nick) for nick in storage[telegram_id])
        lines.append(f"<code>{html.escape(telegram_id)}</code>: {nicks}")
    return "\n".join(lines)


def split_message(text, limit=3900):
    chunks = []
    current = ""
    for line in text.splitlines():
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            if current:
                chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def validate_nickname(nickname):
    if not NICKNAME_PATTERN.fullmatch(nickname):
        return (
            False,
            "\n".join(
                [
                    "<b>Ник не подходит</b>",
                    "",
                    "Нужен обычный Minecraft-ник:",
                    "• 3-16 символов",
                    "• английские буквы, цифры и _",
                    "",
                    "Пример: <code>/request Steve_123</code>",
                ]
            ),
        )

    return True, ""


def is_admin(config, telegram_id):
    return str(telegram_id) in normalize_admin_ids(config)


def require_admin(config, chat_id, telegram_id):
    if is_admin(config, telegram_id):
        return True
    send_message(config["telegram_bot_token"], chat_id, "Эта команда доступна только админам.")
    return False


def bridge_error_text(place, exc):
    if isinstance(exc, urllib.error.HTTPError):
        return f"{place}: HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return f"{place}: {html.escape(str(exc.reason))}"
    return f"{place}: {html.escape(str(exc))}"


def handle_request(config, storage, chat_id, telegram_id, nickname):
    token = config["telegram_bot_token"]
    nickname = nickname.strip()
    escaped_nickname = html.escape(nickname)

    is_valid, reason = validate_nickname(nickname)
    if not is_valid:
        send_message(token, chat_id, reason)
        return storage

    try:
        missing = missing_subscriptions(config, telegram_id)
        if missing:
            send_message(token, chat_id, build_subscription_required_message(config, missing), build_channel_keyboard(config))
            return storage
    except Exception as exc:
        send_message(
            token,
            chat_id,
            "\n".join(
                [
                    "<b>Не удалось проверить подписку</b>",
                    "",
                    "Проверь, что бот добавлен в каналы/группы и видит участников.",
                    "",
                    f"<code>{bridge_error_text('Telegram', exc)}</code>",
                ]
            ),
        )
        return storage

    current_nicks = storage.get(telegram_id, [])
    current_nicks_lower = {nick.lower() for nick in current_nicks}

    if nickname.lower() in current_nicks_lower:
        send_message(token, chat_id, f"<b>Этот ник уже привязан</b>\n\n<code>{escaped_nickname}</code>")
        return storage

    if len(current_nicks) >= int(config["max_nicks_per_account"]):
        send_message(
            token,
            chat_id,
            "\n".join(
                [
                    "<b>Лимит ников достигнут</b>",
                    "",
                    f"Можно добавить только {html.escape(str(config['max_nicks_per_account']))} ника на один Telegram.",
                    "Твои ники:",
                    "• " + "\n• ".join(html.escape(nick) for nick in current_nicks),
                ]
            ),
        )
        return storage

    try:
        result = request_whitelist(config, telegram_id, nickname)
    except Exception as exc:
        send_message(
            token,
            chat_id,
            f"<b>Основной сервер не ответил</b>\n\n<code>{bridge_error_text('whitelist', exc)}</code>",
        )
        return storage

    if not result.get("ok"):
        error = html.escape(str(result.get("error", "unknown_error")))
        send_message(token, chat_id, f"<b>Основной сервер отклонил ник</b>\n\n<code>{error}</code>")
        return storage

    try:
        hub_result = request_hub_access(config, telegram_id, nickname)
    except Exception as exc:
        send_message(
            token,
            chat_id,
            "\n".join(
                [
                    "<b>Ник добавлен, но один сервер временно не ответил</b>",
                    "",
                    f"Ник: <code>{escaped_nickname}</code>",
                    f"<code>{bridge_error_text('server', exc)}</code>",
                ]
            ),
        )
        return storage

    if not hub_result.get("ok"):
        error = html.escape(str(hub_result.get("error", "unknown_error")))
        send_message(
            token,
            chat_id,
            f"<b>Ник добавлен, но один сервер отклонил запрос</b>\n\n<code>{error}</code>",
        )
        return storage

    current_nicks.append(nickname)
    storage[telegram_id] = current_nicks
    save_json(STORAGE_PATH, storage)
    send_message(
        token,
        chat_id,
        "\n".join(
            [
                "<b>Готово</b>",
                "",
                f"Ник <code>{escaped_nickname}</code> добавлен в whitelist.",
                "Можно заходить на сервер.",
            ]
        ),
    )
    return storage


def handle_my_nicks(config, storage, chat_id, telegram_id):
    token = config["telegram_bot_token"]
    nicks = storage.get(telegram_id, [])
    if not nicks:
        send_message(
            token,
            chat_id,
            "У тебя пока нет привязанных ников.\n\nДобавь первый:\n<code>/request ТВОЙ_НИК</code>",
        )
        return

    max_nicks = html.escape(str(config["max_nicks_per_account"]))
    text = "\n".join(
        [
            "<b>Твои ники</b>",
            f"{len(nicks)} / {max_nicks}",
            "",
            "• " + "\n• ".join(html.escape(nick) for nick in nicks),
        ]
    )
    send_message(token, chat_id, text)


def handle_all_nicks(config, storage, chat_id, telegram_id):
    token = config["telegram_bot_token"]
    admin_ids = normalize_admin_ids(config)
    if admin_ids and telegram_id not in admin_ids:
        send_message(token, chat_id, "Эта команда доступна только админам.")
        return

    for chunk in split_message(build_all_nicks_text(storage)):
        send_message(token, chat_id, chunk)


def handle_admin_help(config, storage, chat_id, telegram_id):
    if not require_admin(config, chat_id, telegram_id):
        return

    accounts, nick_count = storage_stats(storage)
    send_message(
        config["telegram_bot_token"],
        chat_id,
        "\n".join(
            [
                "<b>Админ-команды</b>",
                "",
                f"В базе: <b>{nick_count}</b> ников / <b>{accounts}</b> Telegram",
                "",
                "<code>/add_nick TELEGRAM_ID НИК</code> - добавить ник игроку без проверки подписки и лимита",
                "<code>/del_nick TELEGRAM_ID НИК</code> - удалить ник из базы бота",
                "<code>/find_nick НИК</code> - найти владельца ника",
                "<code>/all_nicks</code> - показать все ники",
            ]
        ),
    )


def handle_admin_add_nick(config, storage, chat_id, telegram_id, args):
    token = config["telegram_bot_token"]
    if not require_admin(config, chat_id, telegram_id):
        return storage

    parts = args.split(maxsplit=1)
    if len(parts) != 2:
        send_message(token, chat_id, "Формат:\n<code>/add_nick TELEGRAM_ID НИК</code>")
        return storage

    target_id, nickname = parts[0].strip(), parts[1].strip()
    if not target_id.isdigit():
        send_message(token, chat_id, "Telegram ID должен быть числом.")
        return storage

    is_valid, reason = validate_nickname(nickname)
    if not is_valid:
        send_message(token, chat_id, reason)
        return storage

    current_nicks = storage.get(target_id, [])
    if nickname.lower() in {nick.lower() for nick in current_nicks}:
        send_message(token, chat_id, f"Ник уже есть у <code>{html.escape(target_id)}</code>.")
        return storage

    try:
        result = request_whitelist(config, target_id, nickname)
        if not result.get("ok"):
            error = html.escape(str(result.get("error", "unknown_error")))
            send_message(token, chat_id, f"Основной сервер отклонил ник:\n<code>{error}</code>")
            return storage

        hub_result = request_hub_access(config, target_id, nickname)
        if not hub_result.get("ok"):
            error = html.escape(str(hub_result.get("error", "unknown_error")))
            send_message(token, chat_id, f"Один сервер отклонил ник:\n<code>{error}</code>")
            return storage
    except Exception as exc:
        send_message(token, chat_id, f"Сервер не ответил:\n<code>{bridge_error_text('server', exc)}</code>")
        return storage

    current_nicks.append(nickname)
    storage[target_id] = current_nicks
    save_json(STORAGE_PATH, storage)
    send_message(token, chat_id, f"Готово. <code>{html.escape(nickname)}</code> добавлен для <code>{html.escape(target_id)}</code>.")
    return storage


def handle_admin_del_nick(config, storage, chat_id, telegram_id, args):
    token = config["telegram_bot_token"]
    if not require_admin(config, chat_id, telegram_id):
        return storage

    parts = args.split(maxsplit=1)
    if len(parts) != 2:
        send_message(token, chat_id, "Формат:\n<code>/del_nick TELEGRAM_ID НИК</code>")
        return storage

    target_id, nickname = parts[0].strip(), parts[1].strip()
    current_nicks = storage.get(target_id, [])
    new_nicks = [nick for nick in current_nicks if nick.lower() != nickname.lower()]
    if len(new_nicks) == len(current_nicks):
        send_message(token, chat_id, "Такого ника у этого Telegram ID нет.")
        return storage

    if new_nicks:
        storage[target_id] = new_nicks
    else:
        storage.pop(target_id, None)
    save_json(STORAGE_PATH, storage)
    send_message(token, chat_id, f"Удалил <code>{html.escape(nickname)}</code> из базы бота.")
    return storage


def handle_admin_find_nick(config, storage, chat_id, telegram_id, args):
    token = config["telegram_bot_token"]
    if not require_admin(config, chat_id, telegram_id):
        return

    needle = args.strip().lower()
    if not needle:
        send_message(token, chat_id, "Формат:\n<code>/find_nick НИК</code>")
        return

    matches = []
    for target_id, nicks in storage.items():
        for nick in nicks:
            if needle in nick.lower():
                matches.append(f"<code>{html.escape(target_id)}</code>: {html.escape(nick)}")

    if not matches:
        send_message(token, chat_id, "Ничего не найдено.")
        return

    for chunk in split_message("<b>Найдено:</b>\n\n" + "\n".join(matches)):
        send_message(token, chat_id, chunk)


def process_message(config, storage, message):
    token = config["telegram_bot_token"]
    chat_id = message["chat"]["id"]
    telegram_id = str(message["from"]["id"])
    text = message.get("text", "").strip()

    if not text:
        return storage

    command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()

    if command in {"/start", "/help"}:
        send_message(token, chat_id, build_start_text(config, storage), build_channel_keyboard(config))
        return storage

    if command == "/status":
        send_message(token, chat_id, build_status_text(config, storage, telegram_id))
        return storage

    if command == "/my_nicks":
        handle_my_nicks(config, storage, chat_id, telegram_id)
        return storage

    if command == "/all_nicks":
        handle_all_nicks(config, storage, chat_id, telegram_id)
        return storage

    if command == "/admin":
        handle_admin_help(config, storage, chat_id, telegram_id)
        return storage

    if command == "/add_nick":
        args = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""
        return handle_admin_add_nick(config, storage, chat_id, telegram_id, args)

    if command == "/del_nick":
        args = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""
        return handle_admin_del_nick(config, storage, chat_id, telegram_id, args)

    if command == "/find_nick":
        args = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""
        handle_admin_find_nick(config, storage, chat_id, telegram_id, args)
        return storage

    if command == "/request":
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            send_message(token, chat_id, "Формат команды:\n<code>/request ТВОЙ_НИК</code>")
            return storage
        return handle_request(config, storage, chat_id, telegram_id, parts[1])

    send_message(
        token,
        chat_id,
        "\n".join(
            [
                "<b>Команда не распознана</b>",
                "",
                "Используй:",
                "<code>/request ник</code>",
                "<code>/my_nicks</code>",
                "<code>/status</code>",
                "<code>/help</code>",
            ]
        ),
    )
    return storage


def ensure_runtime_files():
    STORAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    old_storage_path = BASE_DIR / "users.json"
    if STORAGE_PATH != old_storage_path and not STORAGE_PATH.exists() and old_storage_path.exists():
        shutil.copyfile(old_storage_path, STORAGE_PATH)
    if not STORAGE_PATH.exists():
        save_json(STORAGE_PATH, {})
    if not STATE_PATH.exists():
        save_json(STATE_PATH, {})


def main():
    ensure_runtime_files()
    config = load_config()
    storage = load_storage()
    state = load_state()
    token = config["telegram_bot_token"]
    offset = int(state.get("offset", 0) or 0)

    try:
        set_bot_commands(config)
    except Exception as exc:
        print(f"Cannot update bot commands: {exc}")
    print(f"Bot started. DATA_DIR={DATA_DIR}. Users={len(storage)}. Offset={offset}")

    while True:
        try:
            updates = telegram_request(
                token,
                "getUpdates",
                {
                    "timeout": int(config["poll_timeout_seconds"]),
                    "offset": offset,
                    "allowed_updates": json.dumps(["message"]),
                },
            )
            for update in updates:
                offset = update["update_id"] + 1
                state["offset"] = offset
                save_json(STATE_PATH, state)
                message = update.get("message")
                if message:
                    storage = process_message(config, storage, message)
        except Exception as exc:
            print(f"Bot loop error: {exc}")
            time.sleep(5)


if __name__ == "__main__":
    main()
