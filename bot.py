# -*- coding: utf-8 -*-
"""
vk_Api.py

Единый файл VK-бота: система очереди администраторов на мероприятия.

Реализованные команды (и их алиасы):
    /stats   (/стата)   — статистика администратора
    /event   (/м)       — занять очередь на мероприятие (нужно фото ИЛИ слово "zmp")
    /over    (/o)       — завершить мероприятие (за себя — все, за другого — только админы)
    /change  (/передать)— передать мероприятие другому администратору (через @ или через запятую)
    /line    (/очередь) — показать очередь (ники кликабельны!)
    /addcd               — установить общее КД мероприятия
    /cd      (/кд)      — показать текущее КД
    /name    (/имя)     — закрепить своё имя за аккаунтом ВК
    /clearname (/очиститьимя) — [admin] очистить закреплённое имя (своё, чужое или ВСЕ через /clearname all)
    /clearline           — [admin] очистить очередь
    /nlist               — [admin] список администраторов (с никами / без ников)
    /addadmin            — [owner] добавить администратора
    /word    (/слово)   — инструкция по использованию бота
    /help                — список команд + синяя callback-кнопка
                           "Альтернативные команды"
"""

import os
import re
import json
import logging
import threading
import time
from datetime import datetime

import vk_api
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
from vk_api.keyboard import VkKeyboard, VkKeyboardColor
from vk_api.utils import get_random_id


# =========================================================================
#                              НАСТРОЙКИ
# =========================================================================

TOKEN = os.environ.get("VK_GROUP_TOKEN", "vk1.a.0kfvpMXiRolgHAPgcHepAufH6BIoJJLe-0P43SKlj8jepaYk0xvTAD2J0EckuZT6zuJfLUK5Ibijixxz-pcs5xQiy-AL9h0L8PbWlG_99ziYCeFm5f9xUEePk-3D4UqsMh9TaapXykbmzDDmLCS8ZTbW62FUOYwABIjYGSa2rzPvqneUkvXsg8mK-VA-gG6JnLicO7g9gFRiarLZaiLVHw")
GROUP_ID = os.environ.get("VK_GROUP_ID", "240845732")
BOT_START_TIME = time.time()

STORAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "queue_storage.json")
ERROR_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_errors.log")
EVENT_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "event_logs.log")

logging.basicConfig(
    filename=ERROR_LOG_FILE,
    level=logging.ERROR,
    format="%(asctime)s %(levelname)s %(message)s",
    encoding="utf-8",
)
logger = logging.getLogger(__name__)
event_logger = logging.getLogger("event_logger")
event_logger.setLevel(logging.INFO)
event_logger.propagate = False
event_log_handler = logging.FileHandler(EVENT_LOG_FILE, encoding="utf-8")
event_log_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%d.%m.%Y %H:%M:%S"))
event_logger.addHandler(event_log_handler)

OWNER_IDS = {877246890}  # <-- ВАШИ ID

ITEMS_PER_PAGE = 10


# =========================================================================
#                         ХРАНИЛИЩЕ ДАННЫХ (JSON)
# =========================================================================

_storage_lock = threading.Lock()


def _default_state():
    return {
        "queue": [],
        "queue_times": {},
        "stats": {},
        "cd": None,
        "help_alt": {},
        "admins": [],
    }


def _load_state():
    if not os.path.exists(STORAGE_FILE):
        return _default_state()
    try:
        with open(STORAGE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        state = _default_state()
        state.update(data)
        return state
    except (json.JSONDecodeError, OSError):
        return _default_state()


def _save_state(state):
    tmp_path = STORAGE_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, STORAGE_FILE)


class Storage:

    def __init__(self):
        self._lock = _storage_lock
        with self._lock:
            if not os.path.exists(STORAGE_FILE):
                _save_state(_default_state())

    def get_queue(self):
        with self._lock:
            return list(_load_state()["queue"])

    def is_in_queue(self, name):
        with self._lock:
            state = _load_state()
            return _normalize(name) in [_normalize(n) for n in state["queue"]]

    def add_to_queue(self, name):
        with self._lock:
            state = _load_state()
            normalized_existing = [_normalize(n) for n in state["queue"]]
            if _normalize(name) in normalized_existing:
                return False
            state["queue"].append(name)
            state.setdefault("queue_times", {})[name] = time.time()
            _save_state(state)
            return True

    def remove_from_queue(self, name):
        with self._lock:
            state = _load_state()
            idx = _find_index(state["queue"], name)
            if idx is None:
                return False
            removed_name = state["queue"].pop(idx)
            state.setdefault("queue_times", {}).pop(removed_name, None)
            _save_state(state)
            return True

    def replace_in_queue(self, old_name, new_name):
        with self._lock:
            state = _load_state()
            idx = _find_index(state["queue"], old_name)
            if idx is None:
                return "not_found"
            state["queue"][idx] = new_name
            queue_times = state.setdefault("queue_times", {})
            queue_times[new_name] = queue_times.pop(old_name, time.time())
            _save_state(state)
            return "ok"

    def clear_queue(self):
        with self._lock:
            state = _load_state()
            state["queue"] = []
            state["queue_times"] = {}
            _save_state(state)

    def get_queue_times(self):
        with self._lock:
            return dict(_load_state().get("queue_times", {}))

    def get_stats(self, name):
        with self._lock:
            state = _load_state()
            return _get_stat_value(state["stats"], name)

    def increment_stats(self, name):
        with self._lock:
            state = _load_state()
            key = _find_stats_key(state["stats"], name)
            if key is None:
                key = name
                state["stats"][key] = 0
            state["stats"][key] += 1
            _save_state(state)
            return state["stats"][key]

    def get_cd(self):
        with self._lock:
            return _load_state()["cd"]

    def set_cd(self, value):
        with self._lock:
            state = _load_state()
            state["cd"] = value
            _save_state(state)

    def get_admins(self):
        with self._lock:
            return list(_load_state().get("admins", []))

    def is_admin(self, user_id):
        with self._lock:
            return str(user_id) in _load_state().get("admins", [])

    def add_admin(self, user_id):
        with self._lock:
            state = _load_state()
            admins = state.get("admins", [])
            uid = str(user_id)
            if uid in admins:
                return False
            admins.append(uid)
            state["admins"] = admins
            _save_state(state)
            return True

    def remove_admin(self, user_id):
        with self._lock:
            state = _load_state()
            uid = str(user_id)
            admins = state.get("admins", [])
            if uid not in admins:
                return False
            admins.remove(uid)
            state["admins"] = admins
            _save_state(state)
            return True


def _normalize(name):
    return name.strip().lower()


def _find_index(names_list, name):
    target = _normalize(name)
    for i, n in enumerate(names_list):
        if _normalize(n) == target:
            return i
    return None


def _find_stats_key(stats_dict, name):
    target = _normalize(name)
    for key in stats_dict:
        if _normalize(key) == target:
            return key
    return None


def _get_stat_value(stats_dict, name):
    key = _find_stats_key(stats_dict, name)
    if key is None:
        return 0
    return stats_dict[key]


storage = Storage()


# =========================================================================
#                          РАЗБОР КОМАНД / ТЕКСТА
# =========================================================================

ALIASES = {
    "/стата": "/stats",
    "/м": "/event",
    "/o": "/over",
    "/о": "/over",
    "/передать": "/change",
    "/очередь": "/line",
    "/кд": "/cd",
    "/имя": "/name",
    "/очиститьимя": "/clearname",
    "/инструктаж": "/word",
}

CANONICAL_COMMANDS = {
    "/stats", "/event", "/over", "/change", "/line",
    "/addcd", "/cd", "/help", "/name", "/clearname", "/word",
    "/clearline", "/nlist", "/addadmin", "/apanel",
    "/deladmin",
}


def parse_command(raw_text):
    if not raw_text:
        return None, None

    text = raw_text.strip()
    if not text.startswith("/"):
        return None, None

    parts = text.split(None, 1)
    cmd_token = parts[0].lower()
    argument = parts[1].strip() if len(parts) > 1 else ""

    canonical = ALIASES.get(cmd_token, cmd_token)
    if canonical not in CANONICAL_COMMANDS:
        return None, None

    return canonical, argument


def contains_zmp(text):
    if not text:
        return False
    return re.search(r"\bzmp\b", text, flags=re.IGNORECASE) is not None


def has_photo_attachment(vk_message):
    attachments = vk_message.get("attachments", []) or []
    for att in attachments:
        if att.get("type") == "photo":
            return True
    for fwd in vk_message.get("fwd_messages", []) or []:
        for att in fwd.get("attachments", []) or []:
            if att.get("type") == "photo":
                return True
    reply = vk_message.get("reply_message")
    if reply:
        for att in reply.get("attachments", []) or []:
            if att.get("type") == "photo":
                return True
    return False


def get_photo_attachment(vk_message):
    for attachment in vk_message.get("attachments", []) or []:
        if attachment.get("type") != "photo":
            continue
        photo = attachment.get("photo", {})
        owner_id = photo.get("owner_id")
        photo_id = photo.get("id")
        if owner_id is None or photo_id is None:
            continue
        access_key = photo.get("access_key")
        suffix = f"_{access_key}" if access_key else ""
        return f"photo{owner_id}_{photo_id}{suffix}"
    return None


# =========================================================================
#                          ТЕКСТЫ ОТВЕТОВ
# =========================================================================

MSG_EVENT_SUCCESS = "Вы успешно заняли очередь на мероприятие! ✅"
MSG_EVENT_NEED_NAME = "У вас не закреплён ник."
MSG_COMMAND_NEED_NAME = "Сначала закрепите ник через /name Имя. Без закреплённого ника действия недоступны."
MSG_EVENT_NEED_PROOF = "Для занятия мероприятия нужно приложить фото или указать слово «zmp»."
MSG_EVENT_ALREADY_IN_QUEUE = "Этот администратор уже находится в очереди."

MSG_LINE_HEADER = "Очередь на мероприятие:"
MSG_LINE_EMPTY = "Очередь на мероприятие пуста."

MSG_OVER_SUCCESS = "Вы успешно завершили мероприятие! 📉"
MSG_OVER_NOT_IN_QUEUE = "Вы не находитесь в очереди на мероприятие."
MSG_OVER_CANT_FOR_OTHER = "❌ Вы не можете завершить мероприятие за другого администратора. Используйте /over без аргумента, чтобы завершить за себя."

MSG_CHANGE_BAD_FORMAT = (
    "Укажите правильно: либо через упоминание (@пользователь), "
    "либо через запятую: /change @user"
)
MSG_CHANGE_NOT_FOUND = "Вас нет в очереди."
MSG_CHANGE_TARGET_NO_NICK = "У упомянутого пользователя не закреплён ник. Он должен сначала использовать /name."
MSG_CHANGE_SELF = "Нельзя передать мероприятие самому себе!"

MSG_ADDCD_SUCCESS = "Вы успешно установили кд!"
MSG_ADDCD_BAD_FORMAT = "Укажите корректное число, например: /addcd 43"

MSG_CD_NOT_SET = "Кд на мероприятие ещё не установлен."

MSG_STATS_HEADER = "Информация о администраторе:"

MSG_NAME_NEED_ARG = "Укажите имя, например: /name ваше имя"
MSG_NAME_SUCCESS = "Имя «{name}» закреплено за вашим аккаунтом ВК!"

MSG_CLEARNAME_CLEARED = "Очищено {count} никнеймов."
MSG_CLEARNAME_NOTHING = "Ничего не найдено для очистки."

MSG_CLEARLINE_SUCCESS = "Очередь успешно очищена! ✅"

MSG_ADDADMIN_SUCCESS = "Пользователь {uid} добавлен в список администраторов! ✅"
MSG_ADDADMIN_EXISTS = "Пользователь {uid} уже является администратором."
MSG_ADDADMIN_NEED_ARG = "Укажите ID пользователя, например: /addadmin 123456"
MSG_ADDADMIN_OWNER_ONLY = "Только владелец бота может использовать эту команду."
MSG_DELADMIN_SUCCESS = "Пользователь {uid} удалён из списка администраторов."
MSG_DELADMIN_NOT_FOUND = "Пользователь {uid} не найден среди администраторов."
MSG_DELADMIN_OWNER = "Владельца бота нельзя удалить из списка администраторов."

HELP_MAIN_TEXT = (
    "Команды пользователей:\n"
    "/stats – статистика администратора\n"
    "/event – занять мероприятие (сначала нужно закрепить ник через /name)\n"
    "/over – закончить мероприятие (за себя)\n"
    "/change – передать мероприятие @user\n"
    "/line – очередь\n"
    "/addcd – написать кд мероприятие\n"
    "/cd – посмотреть кд мероприятия\n"
    "/name – закрепить своё имя за аккаунтом\n"
    "/word – инструкция по использованию бота"
)

HELP_ALT_TEXT = (
    "Альтернативные команды:\n"
    "/стата – /stats\n"
    "/м – /event\n"
    "/o – /over\n"
    "/передать – /change\n"
    "/очередь – /line\n"
    "/кд – /cd\n"
    "/имя – /name\n"
    "/слово – /word"
)

HELP_BUTTON_LABEL = "Альтернативные команды"
HELP_BUTTON_PAYLOAD = {"action": "show_alt_commands"}

ADMIN_PANEL_TEXT = "Админ-панель\nВыберите действие:"

WORD_TEXT = (
    "📖 Инструкция по боту:\n\n"
    "📌 Занять мероприятие:\n"
    "Сначала закрепите ник: /name Имя (делается один раз).\n"
    "Затем: /event — нужно приложить фото ИЛИ написать слово «zmp» в сообщении.\n"
    "Без закреплённого ника команда /event не сработает.\n\n"
    "📌 Завершить мероприятие:\n"
    "/over — завершает на ваше закреплённое имя.\n\n"
    "📌 Передать мероприятие другому:\n"
    "/change @пользователь — передать ваше место в очереди пользователю.\n\n"
    "📌 Посмотреть очередь:\n"
    "/line\n\n"
    "📌 Установить общее КД:\n"
    "/addcd Число — например /addcd 43\n\n"
    "📌 Посмотреть текущее КД:\n"
    "/cd\n\n"
    "📌 Закрепить своё имя за аккаунтом:\n"
    "/name Имя\n\n"
    "📌 Статистика администратора:\n"
    "/stats — своя (если имя закреплено)\n\n"
    "📌 ВАЖНО!\n"
    "Если вы не закрепите никнейм через команду /name, команды не будут работать.\n\n"
    "Полный список команд и алиасов: /help"
)


def format_stats_message(count):
    return f"{MSG_STATS_HEADER}\n\nсколько всего сделано мероприятий: {count}"


def format_line_message(queue):
    if not queue:
        return MSG_LINE_EMPTY
    
    last_names = _load_last_names()
    queue_times = storage.get_queue_times()
    nick_to_id = {}
    for uid, nick in last_names.items():
        if nick not in nick_to_id:
            nick_to_id[nick] = uid

    formatted_queue = []
    for position, item in enumerate(queue, 1):
        uid = nick_to_id.get(item)
        display_name = f"[id{uid}|{item}]" if uid else item
        joined_at = queue_times.get(item)
        time_text = time.strftime("%d.%m.%Y %H:%M", time.localtime(joined_at)) if joined_at else ""
        time_suffix = f" — занято: {time_text}" if time_text else ""
        formatted_queue.append(f"{position}. {display_name}{time_suffix}")
            
    return MSG_LINE_HEADER + "\n" + "\n".join(formatted_queue)


def format_cd_message(cd_value):
    if cd_value is None:
        return MSG_CD_NOT_SET
    return f"Кд на мероприятие: {cd_value}"


# =========================================================================
#                         КЛАВИАТУРА ДЛЯ /help
# =========================================================================

def build_help_keyboard():
    keyboard = VkKeyboard(inline=True)
    keyboard.add_callback_button(
        HELP_BUTTON_LABEL,
        color=VkKeyboardColor.PRIMARY,
        payload=HELP_BUTTON_PAYLOAD,
    )
    return keyboard.get_keyboard()


def build_admin_panel_keyboard():
    keyboard = VkKeyboard(inline=True)
    keyboard.add_callback_button(
        "Список админов",
        color=VkKeyboardColor.SECONDARY,
        payload={"action": "apanel_admins"},
    )
    keyboard.add_callback_button(
        "Управление админами",
        color=VkKeyboardColor.SECONDARY,
        payload={"action": "apanel_admin_manage"},
    )
    keyboard.add_line()
    keyboard.add_callback_button(
        "Статус бота",
        color=VkKeyboardColor.SECONDARY,
        payload={"action": "apanel_status"},
    )
    keyboard.add_callback_button(
        "Логи ошибок",
        color=VkKeyboardColor.SECONDARY,
        payload={"action": "apanel_logs"},
    )
    keyboard.add_callback_button(
        "Логи МП",
        color=VkKeyboardColor.SECONDARY,
        payload={"action": "apanel_event_logs"},
    )
    keyboard.add_line()
    keyboard.add_callback_button(
        "Очистить очередь",
        color=VkKeyboardColor.NEGATIVE,
        payload={"action": "apanel_clearline"},
    )
    return keyboard.get_keyboard()


def build_admin_panel_back_keyboard():
    keyboard = VkKeyboard(inline=True)
    keyboard.add_callback_button(
        "Назад",
        color=VkKeyboardColor.SECONDARY,
        payload={"action": "apanel_main"},
    )
    return keyboard.get_keyboard()


def get_error_log_message():
    try:
        with open(ERROR_LOG_FILE, "r", encoding="utf-8") as log_file:
            lines = log_file.readlines()[-20:]
    except OSError:
        lines = []

    if not lines:
        return "Лог ошибок пуст."

    log_text = "".join(lines).strip()
    if len(log_text) > 3500:
        log_text = log_text[-3500:]
        log_text = "...\n" + log_text
    return "Последние ошибки:\n\n" + log_text


def get_event_log_message():
    try:
        with open(EVENT_LOG_FILE, "r", encoding="utf-8") as log_file:
            lines = log_file.readlines()[-50:]
    except OSError:
        lines = []

    if not lines:
        return "Журнал занятий пока пуст."

    log_text = "".join(lines).strip()
    if len(log_text) > 3500:
        log_text = log_text[-3500:]
        log_text = "...\n" + log_text
    return "Последние занятия мероприятий:\n\n" + log_text


def format_bot_status_message():
    uptime_seconds = max(0, int(time.time() - BOT_START_TIME))
    days, remainder = divmod(uptime_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_parts = []
    if days:
        uptime_parts.append(f"{days} дн.")
    if hours or days:
        uptime_parts.append(f"{hours} ч.")
    if minutes or hours or days:
        uptime_parts.append(f"{minutes} мин.")
    uptime_parts.append(f"{seconds} сек.")

    admin_ids = set(storage.get_admins()) | {str(uid) for uid in OWNER_IDS}
    storage_status = "доступно" if os.path.exists(STORAGE_FILE) else "не найдено"
    return (
        "Статус бота:\n"
        f"Время работы: {' '.join(uptime_parts)}\n"
        f"Администраторов: {len(admin_ids)}\n"
        f"В очереди: {len(storage.get_queue())}\n"
        f"Хранилище: {storage_status}\n"
        f"Текущее время: {datetime.now().astimezone().strftime('%d.%m.%Y %H:%M:%S %z')}"
    )


# =========================================================================
#                          ОБРАБОТЧИКИ КОМАНД
# =========================================================================

def strip_zmp_token(text):
    cleaned = re.sub(r"\bzmp\b", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", cleaned).strip()


def handle_event(user_id, argument, vk_message):
    has_zmp = contains_zmp(vk_message.get("text", ""))
    name = get_last_name(user_id) or ""
    if not name:
        return MSG_EVENT_NEED_NAME, None

    if not (has_photo_attachment(vk_message) or has_zmp):
        return MSG_EVENT_NEED_PROOF, None

    added = storage.add_to_queue(name)
    if not added:
        return MSG_EVENT_ALREADY_IN_QUEUE, None

    event_logger.info("Администратор [id%s|%s] занял мероприятие", user_id, name)
    return MSG_EVENT_SUCCESS, name


def handle_line(user_name, argument):
    queue = storage.get_queue()
    return format_line_message(queue)


def handle_change(user_id, argument):
    mention_match = re.search(r'\[id(\d+)\|', argument)
    if mention_match:
        target_uid = mention_match.group(1)
        target_name = get_last_name(target_uid)
        current_name = get_last_name(user_id)

        if not current_name:
            return MSG_EVENT_NEED_NAME
        if not target_name:
            return MSG_CHANGE_TARGET_NO_NICK
        if str(user_id) == target_uid:
            return MSG_CHANGE_SELF

        result = storage.replace_in_queue(current_name, target_name)
        if result == "not_found":
            return MSG_CHANGE_NOT_FOUND

        return f"Вы успешно передали мероприятие [id{target_uid}|{target_name}]!"

    if "," not in argument:
        return MSG_CHANGE_BAD_FORMAT

    left, right = argument.split(",", 1)
    old_name = left.strip()
    new_name = right.strip()

    if not old_name or not new_name:
        return MSG_CHANGE_BAD_FORMAT

    result = storage.replace_in_queue(old_name, new_name)
    if result == "not_found":
        return MSG_CHANGE_NOT_FOUND

    return f"Вы успешно передали мероприятие {new_name.lower()}!"


def handle_addcd(user_name, argument):
    value = argument.strip()
    if not value or not re.fullmatch(r"-?\d+", value):
        return MSG_ADDCD_BAD_FORMAT

    storage.set_cd(int(value))
    return MSG_ADDCD_SUCCESS


def handle_cd(user_name, argument):
    cd_value = storage.get_cd()
    return format_cd_message(cd_value)


def handle_help():
    return HELP_MAIN_TEXT, build_help_keyboard()


def handle_name(user_id, argument):
    name = argument.strip()
    if not name:
        return MSG_NAME_NEED_ARG
    remember_last_name(user_id, name)
    return MSG_NAME_SUCCESS.format(name=name)


def handle_word():
    return WORD_TEXT


def handle_clearline():
    storage.clear_queue()
    return MSG_CLEARLINE_SUCCESS


def handle_addadmin(user_id, argument):
    if user_id not in OWNER_IDS:
        return MSG_ADDADMIN_OWNER_ONLY

    uid = argument.strip()
    if not uid or not uid.isdigit():
        return MSG_ADDADMIN_NEED_ARG

    if storage.add_admin(uid):
        return MSG_ADDADMIN_SUCCESS.format(uid=uid)
    return MSG_ADDADMIN_EXISTS.format(uid=uid)


def handle_deladmin(user_id, argument):
    if user_id not in OWNER_IDS:
        return MSG_ADDADMIN_OWNER_ONLY

    uid = argument.strip()
    if not uid or not uid.isdigit():
        return "Укажите ID пользователя, например: /deladmin 123456"
    if int(uid) in OWNER_IDS:
        return MSG_DELADMIN_OWNER
    if storage.remove_admin(uid):
        return MSG_DELADMIN_SUCCESS.format(uid=uid)
    return MSG_DELADMIN_NOT_FOUND.format(uid=uid)


# ---- /nlist ----

def _get_vk_names(vk, user_ids):
    if not user_ids:
        return {}
    try:
        users = vk.users.get(user_ids=user_ids, fields="first_name,last_name")
        return {str(u['id']): f"{u['first_name']} {u['last_name']}" for u in users}
    except Exception:
        return {uid: uid for uid in user_ids}


def _get_nlist_data(vk, mode):
    json_admins = set(storage.get_admins())
    owner_ids_str = {str(uid) for uid in OWNER_IDS}
    all_admin_ids = list(json_admins | owner_ids_str)

    vk_names = _get_vk_names(vk, all_admin_ids)
    last_names = _load_last_names()

    if mode == "with_nicks":
        items = [(uid, vk_names.get(uid, uid), last_names.get(uid)) for uid in all_admin_ids if uid in last_names]
        header = "Список администраторов (с никами):"
    else:
        items = [(uid, vk_names.get(uid, uid)) for uid in all_admin_ids if uid not in last_names]
        header = "Пользователи без ников:"
    return items, header


def _build_nlist_message(vk, page, mode):
    items, header = _get_nlist_data(vk, mode)
    total_pages = max(1, (len(items) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    if page >= total_pages:
        page = 0

    start = page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    page_items = items[start:end]

    if not items:
        return "Список пуст.", 0

    text = f"{header}\nСтраница {page+1}/{total_pages}\n\n"
    if mode == "with_nicks":
        text += "\n".join([f"[id{uid}|{vk_name}] - {nick}" for uid, vk_name, nick in page_items])
    else:
        text += "\n".join([f"[id{uid}|{vk_name}]" for uid, vk_name in page_items])

    return text, total_pages


def _build_nlist_keyboard(page, total_pages, mode):
    keyboard = VkKeyboard(inline=True)

    toggle_label = "Без ников" if mode == "with_nicks" else "С никаим"
    toggle_mode = "without_nicks" if mode == "with_nicks" else "with_nicks"
    keyboard.add_callback_button(
        toggle_label,
        color=VkKeyboardColor.PRIMARY,
        payload={"action": "nlist_toggle", "mode": toggle_mode}
    )

    has_prev = page > 0
    has_next = page < total_pages - 1

    if has_prev or has_next:
        keyboard.add_line()
        if has_prev:
            keyboard.add_callback_button(
                "⬅️ Назад",
                color=VkKeyboardColor.SECONDARY,
                payload={"action": "nlist_page", "page": page-1, "mode": mode}
            )
        if has_next:
            keyboard.add_callback_button(
                "Вперёд ➡️",
                color=VkKeyboardColor.SECONDARY,
                payload={"action": "nlist_page", "page": page+1, "mode": mode}
            )

    return keyboard.get_keyboard()


def handle_nlist(vk, user_id, page=0, mode="with_nicks"):
    text, total_pages = _build_nlist_message(vk, page, mode)
    if total_pages == 0:
        return text, None
    keyboard = _build_nlist_keyboard(page, total_pages, mode)
    return text, keyboard


# =========================================================================
#                    СПЕЦИАЛЬНАЯ ОБРАБОТКА /event и /over
# =========================================================================

_LAST_NAME_LOCK = threading.Lock()


def _last_name_file():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_names.json")


def _load_last_names():
    path = _last_name_file()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_last_names(data):
    path = _last_name_file()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def remember_last_name(user_id, name):
    with _LAST_NAME_LOCK:
        data = _load_last_names()
        data[str(user_id)] = name
        _save_last_names(data)


def get_last_name(user_id):
    with _LAST_NAME_LOCK:
        data = _load_last_names()
        return data.get(str(user_id))


def clear_last_name(identifier):
    with _LAST_NAME_LOCK:
        data = _load_last_names()
        removed = 0
        
        if identifier.lower() in ("all", "все"):
            removed = len(data)
            data = {}
        elif identifier.isdigit():
            if identifier in data:
                del data[identifier]
                removed = 1
        else:
            target = _normalize(identifier)
            for uid in list(data.keys()):
                if _normalize(data[uid]) == target:
                    del data[uid]
                    removed += 1
                    
        if removed:
            _save_last_names(data)
        return removed


def handle_clearname(user_id, argument):
    arg = argument.strip()
    identifier = arg if arg else str(user_id)
    removed = clear_last_name(identifier)
    if removed:
        return MSG_CLEARNAME_CLEARED.format(count=removed)
    return MSG_CLEARNAME_NOTHING


def handle_over_smart(user_id, argument):
    name = argument.strip()
    
    if name and not is_admin(user_id):
        return MSG_OVER_CANT_FOR_OTHER

    if not name:
        name = get_last_name(user_id) or ""
    
    if not name:
        return "Укажите своё имя: /over Имя"

    removed = storage.remove_from_queue(name)
    if not removed:
        return MSG_OVER_NOT_IN_QUEUE

    storage.increment_stats(name)
    return MSG_OVER_SUCCESS


def handle_stats_smart(user_id, argument):
    name = argument.strip()
    if not name:
        name = get_last_name(user_id) or ""
    if not name:
        return "Укажите имя администратора: /stats Имя"
    count = storage.get_stats(name)
    return format_stats_message(count)


# =========================================================================
#                              ОСНОВНОЙ БОТ
# =========================================================================

def is_admin(user_id):
    return storage.is_admin(user_id) or user_id in OWNER_IDS


def is_owner(user_id):
    return user_id in OWNER_IDS


ADMIN_ONLY_COMMANDS = {
    "/event", "/change", "/addcd", "/clearname",
    "/clearline", "/nlist", "/addadmin", "/deladmin", "/apanel"
}

COMMANDS_WITHOUT_NAME = {"/name", "/help", "/word", "/apanel", "/deladmin"}


def dispatch_message(vk, vk_message, event_api=None):
    user_id = vk_message.get("from_id")
    peer_id = vk_message.get("peer_id")
    text = vk_message.get("text", "")

    command, argument = parse_command(text)
    if command is None:
        return

    if command in ADMIN_ONLY_COMMANDS and not is_admin(user_id):
        vk.messages.send(
            peer_id=peer_id,
            message="У вас недостаточно прав для использования этой команды.",
            random_id=get_random_id(),
        )
        return

    if command == "/apanel":
        if peer_id >= 2000000000:
            vk.messages.send(
                peer_id=peer_id,
                message="Админ-панель доступна только в личных сообщениях боту.",
                random_id=get_random_id(),
            )
            return
        vk.messages.send(
            peer_id=peer_id,
            message=ADMIN_PANEL_TEXT,
            random_id=get_random_id(),
            keyboard=build_admin_panel_keyboard(),
        )
        return

    if command not in COMMANDS_WITHOUT_NAME and not get_last_name(user_id):
        vk.messages.send(
            peer_id=peer_id,
            message=MSG_COMMAND_NEED_NAME,
            random_id=get_random_id(),
        )
        return

    if command == "/stats":
        reply = handle_stats_smart(user_id, argument)
        vk.messages.send(peer_id=peer_id, message=reply, random_id=get_random_id())
        return

    if command == "/event":
        reply, resolved_name = handle_event(user_id, argument, vk_message)
        event_success = reply == MSG_EVENT_SUCCESS and resolved_name
        if event_success:
            remember_last_name(user_id, resolved_name)
            reply = f"[id{user_id}|{resolved_name}] вы успешно заняли мероприятие! ✅"
        attachment = get_photo_attachment(vk_message) if event_success else None
        send_kwargs = {
            "peer_id": peer_id,
            "message": reply,
            "random_id": get_random_id(),
        }
        if attachment:
            send_kwargs["attachment"] = attachment
        vk.messages.send(**send_kwargs)
        conversation_message_id = vk_message.get("conversation_message_id")
        if conversation_message_id is not None and peer_id >= 2000000000:
            try:
                vk.messages.delete(
                    conversation_message_ids=conversation_message_id,
                    peer_id=peer_id,
                    delete_for_all=1,
                )
            except Exception:
                logger.exception("Не удалось удалить сообщение с командой /event")
        elif vk_message.get("id") is not None:
            try:
                vk.messages.delete(
                    message_ids=vk_message["id"],
                    delete_for_all=1,
                )
            except Exception:
                logger.exception("Не удалось удалить сообщение с командой /event")
        return

    if command == "/line":
        reply = handle_line(user_id, argument)
        vk.messages.send(peer_id=peer_id, message=reply, random_id=get_random_id())
        return

    if command == "/over":
        reply = handle_over_smart(user_id, argument)
        vk.messages.send(peer_id=peer_id, message=reply, random_id=get_random_id())
        return

    if command == "/change":
        reply = handle_change(user_id, argument)
        vk.messages.send(peer_id=peer_id, message=reply, random_id=get_random_id())
        return

    if command == "/addcd":
        reply = handle_addcd(user_id, argument)
        vk.messages.send(peer_id=peer_id, message=reply, random_id=get_random_id())
        return

    if command == "/cd":
        reply = handle_cd(user_id, argument)
        vk.messages.send(peer_id=peer_id, message=reply, random_id=get_random_id())
        return

    if command == "/name":
        reply = handle_name(user_id, argument)
        vk.messages.send(peer_id=peer_id, message=reply, random_id=get_random_id())
        return

    if command == "/clearname":
        reply = handle_clearname(user_id, argument)
        vk.messages.send(peer_id=peer_id, message=reply, random_id=get_random_id())
        return

    if command == "/clearline":
        keyboard = VkKeyboard(inline=True)
        keyboard.add_callback_button(
            "Подтвердить",
            color=VkKeyboardColor.NEGATIVE,
            payload={"action": "clearline_confirm"},
        )
        keyboard.add_callback_button(
            "Отмена",
            color=VkKeyboardColor.SECONDARY,
            payload={"action": "clearline_cancel"},
        )
        vk.messages.send(
            peer_id=peer_id,
            message="Очистить всю очередь?",
            random_id=get_random_id(),
            keyboard=keyboard.get_keyboard(),
        )
        return

    if command == "/addadmin":
        reply = handle_addadmin(user_id, argument)
        vk.messages.send(peer_id=peer_id, message=reply, random_id=get_random_id())
        return

    if command == "/deladmin":
        reply = handle_deladmin(user_id, argument)
        vk.messages.send(peer_id=peer_id, message=reply, random_id=get_random_id())
        return

    if command == "/nlist":
        reply, keyboard = handle_nlist(vk, user_id, 0, "with_nicks")
        vk.messages.send(
            peer_id=peer_id,
            message=reply,
            random_id=get_random_id(),
            keyboard=keyboard,
        )
        return

    if command == "/word":
        reply = handle_word()
        vk.messages.send(peer_id=peer_id, message=reply, random_id=get_random_id())
        return

    if command == "/help":
        text_reply, keyboard = handle_help()
        vk.messages.send(
            peer_id=peer_id,
            message=text_reply,
            random_id=get_random_id(),
            keyboard=keyboard,
        )
        return


def dispatch_message_event(vk, event, group_id):
    payload = event.object.get("payload") or {}
    action = payload.get("action")

    panel_actions = {
        "apanel_main", "apanel_line", "apanel_stats",
        "apanel_admins", "apanel_cd", "apanel_logs", "apanel_event_logs",
        "apanel_admin_manage", "apanel_status", "apanel_clearline",
    }
    if action in panel_actions:
        peer_id = event.object["peer_id"]
        conversation_message_id = event.object.get("conversation_message_id")
        user_id = event.object["user_id"]

        if peer_id >= 2000000000 or not is_admin(user_id):
            vk.messages.sendMessageEventAnswer(
                event_id=event.object["event_id"],
                user_id=user_id,
                peer_id=peer_id,
                event_data=json.dumps({
                    "type": "show_snackbar",
                    "text": "Панель доступна только администраторам в ЛС.",
                }),
            )
            return

        if action == "apanel_main":
            panel_text = ADMIN_PANEL_TEXT
            panel_keyboard = build_admin_panel_keyboard()
        elif action == "apanel_line":
            panel_text = handle_line(user_id, "")
            panel_keyboard = build_admin_panel_back_keyboard()
        elif action == "apanel_stats":
            panel_text = handle_stats_smart(user_id, "")
            panel_keyboard = build_admin_panel_back_keyboard()
        elif action == "apanel_admins":
            panel_text, _ = _build_nlist_message(vk, 0, "with_nicks")
            panel_keyboard = build_admin_panel_back_keyboard()
        elif action == "apanel_admin_manage":
            admin_text, _ = _build_nlist_message(vk, 0, "with_nicks")
            panel_text = (
                "Управление администраторами:\n\n"
                f"{admin_text}\n\n"
                "Добавить: /addadmin ID\n"
                "Удалить: /deladmin ID\n"
                "Изменять список может только владелец."
            )
            panel_keyboard = build_admin_panel_back_keyboard()
        elif action == "apanel_cd":
            panel_text = format_cd_message(storage.get_cd())
            panel_keyboard = build_admin_panel_back_keyboard()
        elif action == "apanel_status":
            panel_text = format_bot_status_message()
            panel_keyboard = build_admin_panel_back_keyboard()
        elif action == "apanel_logs":
            panel_text = get_error_log_message()
            panel_keyboard = build_admin_panel_back_keyboard()
        elif action == "apanel_event_logs":
            panel_text = get_event_log_message()
            panel_keyboard = build_admin_panel_back_keyboard()
        else:
            confirm_keyboard = VkKeyboard(inline=True)
            confirm_keyboard.add_callback_button(
                "Подтвердить",
                color=VkKeyboardColor.NEGATIVE,
                payload={"action": "clearline_confirm"},
            )
            confirm_keyboard.add_callback_button(
                "Отмена",
                color=VkKeyboardColor.SECONDARY,
                payload={"action": "clearline_cancel"},
            )
            panel_text = "Очистить всю очередь?"
            panel_keyboard = confirm_keyboard.get_keyboard()

        vk.messages.edit(
            peer_id=peer_id,
            conversation_message_id=conversation_message_id,
            message=panel_text,
            keyboard=panel_keyboard,
        )
        vk.messages.sendMessageEventAnswer(
            event_id=event.object["event_id"],
            user_id=user_id,
            peer_id=peer_id,
        )
        return

    if action in ("clearline_confirm", "clearline_cancel"):
        peer_id = event.object["peer_id"]
        conversation_message_id = event.object["conversation_message_id"]
        user_id = event.object["user_id"]

        if action == "clearline_confirm" and is_admin(user_id):
            reply = handle_clearline()
        elif action == "clearline_cancel":
            reply = "Очистка очереди отменена."
        else:
            reply = "Недостаточно прав для очистки очереди."

        vk.messages.edit(
            peer_id=peer_id,
            conversation_message_id=conversation_message_id,
            message=reply,
            keyboard=VkKeyboard.get_empty_keyboard(),
        )
        vk.messages.sendMessageEventAnswer(
            event_id=event.object["event_id"],
            user_id=user_id,
            peer_id=peer_id,
        )
        return

    if action == "show_alt_commands":
        peer_id = event.object["peer_id"]
        conversation_message_id = event.object["conversation_message_id"]

        vk.messages.edit(
            peer_id=peer_id,
            conversation_message_id=conversation_message_id,
            message=HELP_ALT_TEXT,
            keyboard=VkKeyboard.get_empty_keyboard(),
        )
        vk.messages.sendMessageEventAnswer(
            event_id=event.object["event_id"],
            user_id=event.object["user_id"],
            peer_id=event.object["peer_id"],
        )
        return

    if action in ("nlist_page", "nlist_toggle"):
        peer_id = event.object["peer_id"]
        conversation_message_id = event.object["conversation_message_id"]

        if action == "nlist_page":
            page = payload.get("page", 0)
            mode = payload.get("mode", "with_nicks")
        else:
            page = 0
            mode = payload.get("mode", "with_nicks")

        text, total_pages = _build_nlist_message(vk, page, mode)
        if total_pages == 0:
            keyboard = None
        else:
            keyboard = _build_nlist_keyboard(page, total_pages, mode)

        vk.messages.edit(
            peer_id=peer_id,
            conversation_message_id=conversation_message_id,
            message=text,
            keyboard=keyboard,
        )

        vk.messages.sendMessageEventAnswer(
            event_id=event.object["event_id"],
            user_id=event.object["user_id"],
            peer_id=event.object["peer_id"],
        )
        return

    vk.messages.sendMessageEventAnswer(
        event_id=event.object["event_id"],
        user_id=event.object["user_id"],
        peer_id=event.object["peer_id"],
    )


def main():
    vk_session = vk_api.VkApi(token=TOKEN)
    vk = vk_session.get_api()
    longpoll = VkBotLongPoll(vk_session, GROUP_ID)

    print("Бот запущен и слушает события...")

    for event in longpoll.listen():
        try:
            if event.type == VkBotEventType.MESSAGE_NEW:
                dispatch_message(vk, event.object.message)
            elif event.type == VkBotEventType.MESSAGE_EVENT:
                dispatch_message_event(vk, event, GROUP_ID)
        except Exception as exc:
            logger.exception("Ошибка при обработке события: %s", exc)


if __name__ == "__main__":
    main()
