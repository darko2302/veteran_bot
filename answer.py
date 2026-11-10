import asyncio
import logging
import os
import sys
from html import escape
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyParameters,
    ReplyKeyboardMarkup,
    KeyboardButton,
)

import db

# ---------------------------------------------------------------------------
# Конфігурація
# ---------------------------------------------------------------------------

def load_bot_token() -> str | None:
    token = os.getenv("BOT_TOKEN")
    if token:
        return token
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        key, separator, value = line.partition("=")
        if not separator or key.strip() != "BOT_TOKEN":
            continue
        token = value.strip()
        if len(token) >= 2 and token[0] == token[-1] and token[0] in {"'", '"'}:
            token = token[1:-1]
        if token:
            os.environ["BOT_TOKEN"] = token
            return token
    return None

TOKEN = load_bot_token()
ADMIN_GROUP_CHAT_ID: int | None = -1004300032591
BOT_LOCK_PATH = Path(__file__).with_name(".bot_instance.lock")
TEMP_MESSAGE_TTL_SECONDS = 10
GROUP_CARD_MAX_LENGTH = 3900
DELETION_TASKS: set[asyncio.Task[None]] = set()

CATEGORIES: dict[str, dict] = {
    "payments": {
        "name": "Соціальний захист, пільги",
        "operator_chat_id": -1004405097726,
        "admin_topic_id": 45,
    },
    "medicine": {
        "name": "Медицина, протезування",
        "operator_chat_id": -1004332553820,
        "admin_topic_id": 42,
    },
    "psychological_support": {
        "name": "Психологічна підтримка",
        "operator_chat_id": -1004436526266,
        "admin_topic_id": 39,
    },
    "work_education_business": {
        "name": "Робота, навчання, підприємництво",
        "operator_chat_id": -1004442860950,
        "admin_topic_id": 21,
    },
    "legal_aid": {
        "name": "Правова допомога",
        "operator_chat_id": -1003894735807,
        "admin_topic_id": 23,
    },
    "families": {
        "name": "Питання родин загиблих",
        "operator_chat_id": -1003901287143,
        "admin_topic_id": 25,
    },
    "other": {
        "name": "Інше",
        "operator_chat_id": -1004445988677,
        "admin_topic_id": 27,
    },
}

class AppealState(StatesGroup):
    waiting_for_text = State()
    reviewing = State()

class OperatorReplyState(StatesGroup):
    waiting_for_reply = State()

class ConfigurationError(Exception):
    pass

router = Router()

# ---------------------------------------------------------------------------
# Утиліти FSM / тимчасові повідомлення
# ---------------------------------------------------------------------------

async def delete_message_safe(bot: Bot, chat_id: int, message_id: int | None) -> None:
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramAPIError:
        pass

async def delete_message_later(bot: Bot, chat_id: int, message_id: int, delay: int = TEMP_MESSAGE_TTL_SECONDS) -> None:
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramAPIError:
        pass

def schedule_message_deletion(bot: Bot, chat_id: int, message_id: int, delay: int = TEMP_MESSAGE_TTL_SECONDS) -> None:
    task = asyncio.create_task(
        delete_message_later(bot, chat_id, message_id, delay),
        name=f"delete-temporary-message-{chat_id}-{message_id}",
    )
    DELETION_TASKS.add(task)
    task.add_done_callback(DELETION_TASKS.discard)

async def reply_temporarily(message: Message, text: str, delay: int = TEMP_MESSAGE_TTL_SECONDS) -> Message:
    sent_message = await message.reply(text)
    schedule_message_deletion(message.bot, message.chat.id, sent_message.message_id, delay=delay)
    return sent_message

async def answer_temporarily(message: Message, text: str, delay: int = TEMP_MESSAGE_TTL_SECONDS, **kwargs) -> Message:
    sent_message = await message.answer(text, **kwargs)
    schedule_message_deletion(message.bot, message.chat.id, sent_message.message_id, delay=delay)
    return sent_message

# ---------------------------------------------------------------------------
# Валідація маршрутизації
# ---------------------------------------------------------------------------

def get_destinations(category_key: str) -> list[tuple[int, int | None]]:
    category = CATEGORIES.get(category_key)
    if category is None:
        raise ConfigurationError(f"Невідома категорія звернення: {category_key!r}.")

    problems: list[str] = []
    admin_id = ADMIN_GROUP_CHAT_ID
    if not isinstance(admin_id, int) or admin_id >= 0:
        problems.append(f"ADMIN_GROUP_CHAT_ID={admin_id!r}")

    operator_id = category.get("operator_chat_id")
    if not isinstance(operator_id, int) or operator_id >= 0:
        problems.append(f"{category_key}.operator_chat_id={operator_id!r}")

    topic_id = category.get("admin_topic_id")
    if topic_id is not None and (not isinstance(topic_id, int) or topic_id <= 0):
        problems.append(f"{category_key}.admin_topic_id={topic_id!r}")

    if problems:
        raise ConfigurationError(f"Некоректні ID для категорії {category_key!r}: " + ", ".join(problems))

    return [(operator_id, None), (admin_id, topic_id)]

def validate_all_categories() -> list[str]:
    errors: list[str] = []
    for key in CATEGORIES:
        try:
            get_destinations(key)
        except ConfigurationError as exc:
            errors.append(str(exc))
    return errors

# ---------------------------------------------------------------------------
# Клавіатури
# ---------------------------------------------------------------------------

def get_main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✍️ Створити звернення"), KeyboardButton(text="📂 Переглянути звернення")]
        ],
        resize_keyboard=True,
        input_field_placeholder="Оберіть дію..."
    )

def get_user_reply_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Відповісти", callback_data="reply")],
        [InlineKeyboardButton(text="✍️ Створити нове звернення", callback_data="create_appeal")]
    ])

def get_operator_case_keyboard(appeal_id: int, blocked: bool) -> InlineKeyboardMarkup:
    block_text = "✅ Розблокувати користувача" if blocked else "🚫 Заблокувати користувача"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Відповісти", callback_data="reply")],
        [InlineKeyboardButton(text=block_text, callback_data=f"block_{appeal_id}")]
    ])

def get_categories_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    for i, (key, cat) in enumerate(CATEGORIES.items(), start=1):
        buttons.append([InlineKeyboardButton(text=f"{i}. {cat['name']}", callback_data=f"cat_{key}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_review_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Змінити категорію", callback_data="review_edit_category")],
        [InlineKeyboardButton(text="✏️ Змінити текст", callback_data="review_edit_text")],
        [InlineKeyboardButton(text="✅ Надіслати звернення", callback_data="review_submit")],
    ])

# ---------------------------------------------------------------------------
# Рендер картки
# ---------------------------------------------------------------------------

def _format_entry(entry: dict, content: str | None = None) -> str:
    label = f"{entry['sender_role']} — {entry['sender_label']}"
    body = entry["content"] if content is None else content
    return f"\n\n<b>{escape(label)}:</b>\n{escape(body)}"

def render_group_card(base_text: str, entries: list[dict]) -> str:
    text = base_text + "".join(_format_entry(entry) for entry in entries)
    if len(text) <= GROUP_CARD_MAX_LENGTH:
        return text

    marker = "\n\n<i>Попередні повідомлення залишилися в історії звернення.</i>"
    visible_entries: list[dict] = []
    for entry in reversed(entries):
        candidate = [entry, *visible_entries]
        candidate_text = base_text + marker + "".join(_format_entry(item) for item in candidate)
        if len(candidate_text) > GROUP_CARD_MAX_LENGTH:
            break
        visible_entries.insert(0, entry)

    if not visible_entries and entries:
        latest = entries[-1]
        available = max(0, GROUP_CARD_MAX_LENGTH - len(base_text + marker + _format_entry(latest, "")))
        latest_text = _format_entry(latest, latest["content"][:available] + "…")
        return base_text + marker + latest_text

    return base_text + marker + "".join(_format_entry(entry) for entry in visible_entries)

async def render_private_card(appeal_id: int) -> str:
    appeal = await db.get_appeal_by_id(appeal_id)
    if not appeal:
        return ""
    
    entries = await db.get_appeal_messages(appeal_id)
    
    base_text = (
        f"<b>Ось ваша форма звернення:</b>\n\n"
        f"📂 <b>Категорія:</b> {escape(CATEGORIES[appeal['category_key']]['name'])}\n"
    )
    
    text = base_text + "".join(_format_entry(entry) for entry in entries)
    if len(text) <= GROUP_CARD_MAX_LENGTH:
        return text
        
    marker = "\n\n<i>Попередні повідомлення залишилися в історії.</i>"
    visible_entries = []
    for entry in reversed(entries):
        candidate = [entry, *visible_entries]
        candidate_text = base_text + marker + "".join(_format_entry(item) for item in candidate)
        if len(candidate_text) > GROUP_CARD_MAX_LENGTH:
            break
        visible_entries.insert(0, entry)
    
    if not visible_entries and entries:
        latest = entries[-1]
        available = max(0, GROUP_CARD_MAX_LENGTH - len(base_text + marker + _format_entry(latest, "")))
        latest_text = _format_entry(latest, latest["content"][:available] + "…")
        return base_text + marker + latest_text

    return base_text + marker + "".join(_format_entry(entry) for entry in visible_entries)

# ---------------------------------------------------------------------------
# Відправка в групи
# ---------------------------------------------------------------------------

async def append_message_to_group_cards(bot: Bot, appeal_id: int, sender_role: str, sender_id: int, sender_label: str, content: str) -> set[int]:
    entries, cards, appeal = await db.append_message_to_group_cards_db(appeal_id, sender_role, sender_id, sender_label, content)

    if appeal is None:
        raise LookupError(f"Звернення #{appeal_id} не знайдено.")
    if not cards:
        return set()

    blocked = await db.is_user_blocked(appeal["user_id"])
    updated_chats: set[int] = set()
    
    for card in cards:
        try:
            await bot.edit_message_text(
                chat_id=card["chat_id"],
                message_id=card["message_id"],
                text=render_group_card(card["base_text"], entries),
                reply_markup=get_operator_case_keyboard(appeal_id, blocked),
            )
            updated_chats.add(card["chat_id"])
        except TelegramAPIError as e:
            if "message is not modified" not in str(e).lower():
                logging.exception("Не вдалося оновити картку #%s у групі %s", appeal_id, card["chat_id"])
    return updated_chats

async def update_private_card(bot: Bot, appeal_id: int, user_id: int, conversation_message_id: int):
    text = await render_private_card(appeal_id)
    try:
        await bot.edit_message_text(
            chat_id=user_id,
            message_id=conversation_message_id,
            text=text,
            reply_markup=get_user_reply_keyboard()
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            logging.error(f"Не вдалося оновити картку в ЛС для #{appeal_id}: {e}")

async def safe_send_message(bot: Bot, chat_id: int, thread_id: int | None, **kwargs) -> Message:
    try:
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id
        return await bot.send_message(chat_id=chat_id, **kwargs)
    except TelegramBadRequest as e:
        if "message thread not found" in str(e).lower():
            kwargs.pop("message_thread_id", None)
            return await bot.send_message(chat_id=chat_id, **kwargs)
        raise

# ---------------------------------------------------------------------------
# Форма звернення
# ---------------------------------------------------------------------------

def format_appeal_preview(data: dict, submitted: bool = False) -> str:
    title = "<b>Ось ваша форма звернення:</b>\n\n" if submitted else "<b>Перевірте звернення перед надсиланням:</b>\n\n"
    text = title + f"📂 <b>Категорія:</b> {escape(CATEGORIES[data['category_key']]['name'])}\n\n" f"💬 <b>Текст:</b>\n{escape(data['appeal_text'])}"
    if submitted:
        text += "\n\n✅ <b>Звернення надіслано.</b> Відповіді будуть у цій гілці чату."
    return text

async def show_appeal_preview(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    preview_message_id = data.get("preview_message_id")
    prompt_id = data.get("last_form_prompt_id")
    
    if prompt_id:
        await delete_message_safe(message.bot, message.chat.id, prompt_id)
        await state.update_data(last_form_prompt_id=None)

    preview_text = format_appeal_preview(data)
    if preview_message_id is None:
        preview = await message.answer(preview_text, reply_markup=get_review_keyboard())
        await state.update_data(preview_message_id=preview.message_id)
    else:
        try:
            await message.bot.edit_message_text(chat_id=message.chat.id, message_id=preview_message_id, text=preview_text, reply_markup=get_review_keyboard())
        except TelegramBadRequest:
            pass
    await state.set_state(AppealState.reviewing)

def get_user_label(user) -> str:
    if user is None:
        return "Невідомий"
    return f"{user.full_name} (@{user.username})" if user.username else user.full_name

# ---------------------------------------------------------------------------
# Хендлери (Користувач)
# ---------------------------------------------------------------------------

@router.message(F.chat.type == "private", F.text == "/start")
async def cmd_start(message: Message, state: FSMContext) -> None:
    if message.from_user is not None and await db.is_user_blocked(message.from_user.id):
        await message.answer("Доступ до спілкування з ботом тимчасово обмежено.")
        return
    await state.clear()
    await message.answer(
        "🇺🇦 Вітаю! Це офіційний бот для звернень ветеранів.\n"
        "Скористайтеся кнопками меню внизу екрану.", 
        reply_markup=get_main_menu_keyboard()
    )

@router.message(F.chat.type == "private", F.text == "✍️ Створити звернення")
async def process_menu_create(message: Message, state: FSMContext) -> None:
    if await db.is_user_blocked(message.from_user.id):
        return
    await state.clear()
    await message.answer("Оберіть категорію вашого питання:", reply_markup=get_categories_keyboard())

@router.message(F.chat.type == "private", F.text == "📂 Переглянути звернення")
async def process_menu_list(message: Message, state: FSMContext) -> None:
    if await db.is_user_blocked(message.from_user.id):
        return
    
    user_id = message.from_user.id
    appeals = await db.get_all_user_appeals(user_id)
    
    if not appeals:
        await message.answer("У вас немає звернень.")
        return
        
    text = "<b>📂 Ваші звернення:</b>\n\n"
    for a in appeals:
        status_emoji = "🟢" if a["status"] == "open" else "🔴"
        text += f"{status_emoji} <b>#{a['id']}</b> - {escape(CATEGORIES[a['category_key']]['name'])}\n"
        
    text += "\nЩоб продовжити діалог, натисніть «Відповісти» на картці звернення в історії."
    await message.answer(text)

@router.callback_query(F.data == "create_appeal")
async def process_create_appeal(callback: CallbackQuery, state: FSMContext) -> None:
    if await db.is_user_blocked(callback.from_user.id):
        await callback.answer("Доступ до бота тимчасово обмежено.", show_alert=True)
        return
    await state.clear()
    if callback.message:
        await callback.message.edit_text(
            "Оберіть категорію вашого питання:",
            reply_markup=get_categories_keyboard()
        )
    await callback.answer()

@router.callback_query(F.data.startswith("cat_"))
async def process_category(callback: CallbackQuery, state: FSMContext) -> None:
    if await db.is_user_blocked(callback.from_user.id):
        await callback.answer("Доступ до бота тимчасово обмежено.", show_alert=True)
        return

    category_key = callback.data.removeprefix("cat_")
    if category_key not in CATEGORIES:
        await callback.answer("Невідома категорія.", show_alert=True)
        return

    data = await state.get_data()
    if data.get("editing_category"):
        await state.update_data(category_key=category_key, editing_category=False)
        if callback.message is not None:
            await callback.message.edit_text(format_appeal_preview({**data, "category_key": category_key}), reply_markup=get_review_keyboard())
        await state.set_state(AppealState.reviewing)
    else:
        await state.update_data(category_key=category_key, category_name=CATEGORIES[category_key]["name"])
        if callback.message is not None:
            msg = await callback.message.edit_text(f"Ви обрали категорію: <b>{escape(CATEGORIES[category_key]['name'])}</b>\n\nТепер надішліть текст звернення:")
            await state.update_data(last_form_prompt_id=msg.message_id)
        await state.set_state(AppealState.waiting_for_text)
    await callback.answer()

@router.callback_query(F.data == "review_edit_category")
async def edit_appeal_category(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer("Не вдалося відкрити категорії.", show_alert=True)
        return
    await state.update_data(editing_category=True)
    await callback.message.edit_text("Оберіть іншу категорію:", reply_markup=get_categories_keyboard())
    await callback.answer()

@router.callback_query(F.data == "review_edit_text")
async def edit_appeal_text(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        return
    await state.update_data(editing_field="appeal_text")
    await callback.message.edit_reply_markup(reply_markup=None)
    prompt = await callback.message.answer("Надішліть новий текст звернення:")
    await state.update_data(last_form_prompt_id=prompt.message_id)
    await state.set_state(AppealState.waiting_for_text)
    await callback.answer()

@router.callback_query(F.data == "review_submit")
async def submit_appeal(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        return
        
    # --- ЗАЩИТА ОТ СПАМА ---
    current_state = await state.get_state()
    if current_state != AppealState.reviewing.state:
        await callback.answer("Звернення вже надсилається або обробляється.", show_alert=True)
        return
    
    # Мгновенно сбрасываем состояние, чтобы избежать дублирования
    await state.set_state(None)

    data = await state.get_data()
    user = callback.from_user
    if user is None or await db.is_user_blocked(user.id):
        await callback.answer("Доступ обмежено.", show_alert=True)
        return

    missing = [field for field in ("preview_message_id", "category_key", "appeal_text") if not data.get(field)]
    if data.get("preview_message_id") != callback.message.message_id:
        missing.append("preview_message_id")
    if missing:
        await callback.answer(f"Дані не знайдено.", show_alert=True)
        return

    category_key = data["category_key"]
    if category_key not in CATEGORIES:
        await callback.answer("Категорія застаріла.", show_alert=True)
        return

    try:
        destinations = get_destinations(category_key)
    except ConfigurationError:
        await callback.answer("❌ Адміністратор ще не налаштував ID груп.", show_alert=True)
        return

    user_label = get_user_label(user)
    appeal_id = await db.create_appeal(user.id, category_key, callback.message.message_id)
    await db.register_private_message(user.id, callback.message.message_id, appeal_id)
    
    # 1. Сначала записываем сообщение в историю базы, чтобы оно было в карточке ЛС
    await append_message_to_group_cards(callback.bot, appeal_id, "Користувач", user.id, user_label, data["appeal_text"])
    
    # 2. Формируем шапку БЕЗ текста пользователя (чтобы не дублировать в группе)
    header = (
        f"💬 <b>Повідомлення до звернення #{appeal_id}</b>\n"
        f"📂 <b>Категорія:</b> {escape(CATEGORIES[category_key]['name'])}\n"
        f"👤 <b>Від кого:</b> {escape(user_label)}\n"
        f"🆔 <b>ID користувача:</b> {user.id}\n"
    )
    
    blocked = await db.is_user_blocked(user.id)
    errors = []
    for chat_id, thread_id in destinations:
        try:
            reply_markup = get_operator_case_keyboard(appeal_id, blocked)
            header_message = await safe_send_message(
                callback.bot, chat_id, thread_id,
                text=header,
                reply_markup=reply_markup,
            )
            await db.register_group_message(chat_id, header_message.message_id, appeal_id, user.id, header_message.message_id)
            await db.register_group_card(chat_id, appeal_id, header_message.message_id, header)
        except TelegramAPIError:
            errors.append(str(chat_id))
    
    await update_private_card(callback.bot, appeal_id, user.id, callback.message.message_id)

    cleanup_message_ids = {*data.get("form_cleanup_message_ids", []), *data.get("form_input_message_ids", [])}
    cleanup_message_ids.discard(callback.message.message_id)
    for message_id in cleanup_message_ids:
        await delete_message_safe(callback.bot, user.id, message_id)
        
    await state.clear()
    await callback.answer("Звернення надіслано.")

@router.callback_query(F.data == "reply")
async def process_reply_button(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        return
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id

    if callback.message.chat.type == "private":
        if await db.is_user_blocked(user_id):
            await callback.answer("Доступ обмежено.", show_alert=True)
            return
        mapping = await db.get_private_message(user_id, callback.message.message_id)
        if mapping is None or mapping["status"] != "open":
            await callback.answer("Звернення закрито.", show_alert=True)
            return
        
        prompt = await callback.message.answer(
            "↩️",
            reply_parameters=ReplyParameters(message_id=callback.message.message_id),
            reply_markup=ForceReply(selective=True),
        )
        await state.update_data(last_reply_prompt_id=prompt.message_id)
        schedule_message_deletion(callback.bot, callback.message.chat.id, prompt.message_id, delay=30)
        await db.register_private_message(user_id, prompt.message_id, mapping["appeal_id"])
    else:
        mapping = await db.get_group_message(chat_id, callback.message.message_id)
        if mapping is None or mapping["status"] != "open":
            await callback.answer("Звернення закрито.", show_alert=True)
            return
        prompt = await callback.message.answer(
            "↩️",
            reply_parameters=ReplyParameters(message_id=callback.message.message_id),
            reply_markup=ForceReply(selective=True),
        )
        await state.update_data(last_reply_prompt_id=prompt.message_id)
        schedule_message_deletion(callback.bot, callback.message.chat.id, prompt.message_id, delay=30)
        await db.register_group_message(chat_id, prompt.message_id, mapping["appeal_id"], mapping["user_id"], mapping["source_message_id"])
        await state.set_state(OperatorReplyState.waiting_for_reply)
        await state.update_data(reply_appeal_id=mapping["appeal_id"])
    await callback.answer()

@router.message(AppealState.waiting_for_text)
async def process_new_appeal(message: Message, state: FSMContext) -> None:
    if message.from_user is None or await db.is_user_blocked(message.from_user.id):
        return
    if message.text is None or not message.text.strip():
        await answer_temporarily(message, "Надішліть текст звернення текстом.")
        return
    
    appeal_text = message.text.strip()
    if len(escape(appeal_text)) > 3300:
        await answer_temporarily(message, "Текст занадто довгий.")
        return
        
    data = await state.get_data()
    input_message_ids = list(data.get("form_input_message_ids", []))
    if message.message_id not in input_message_ids:
        input_message_ids.append(message.message_id)
    await state.update_data(appeal_text=appeal_text, text_message_id=message.message_id, form_input_message_ids=input_message_ids, editing_field=None)
    
    await delete_message_safe(message.bot, message.chat.id, message.message_id)
    prompt_id = data.get("last_form_prompt_id")
    await delete_message_safe(message.bot, message.chat.id, prompt_id)

    await show_appeal_preview(message, state)

async def deliver_followup(bot: Bot, user_id: int, source_message_id: int, appeal: dict, content: str, sender_label: str) -> list[str]:
    category_key = appeal["category_key"]
    await db.register_private_message(user_id, source_message_id, appeal["id"])
    await append_message_to_group_cards(bot, appeal["id"], "Користувач", user_id, sender_label, content)
    
    destinations = get_destinations(category_key)
    
    for chat_id, thread_id in destinations:
        old_msg_id = await db.get_active_user_group_msg(appeal["id"], chat_id)
        if old_msg_id:
            await delete_message_safe(bot, chat_id, old_msg_id)
        try:
            new_group_msg = await safe_send_message(
                bot, chat_id, thread_id,
                text=f"<b>💬 Повідомлення від користувача:</b>\n\n{escape(content)}",
            )
            await db.set_active_user_group_msg(appeal["id"], chat_id, new_group_msg.message_id)
        except TelegramAPIError:
            pass
            
    return []

@router.message(F.chat.type == "private")
async def process_user_followup(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    if await db.is_user_blocked(message.from_user.id):
        await message.delete()
        return
    if message.text is not None and message.text.startswith("/"):
        await answer_temporarily(message, "Щоб створити звернення, натисніть меню.")
        return
    user_id = message.from_user.id
    appeal = None
    if message.reply_to_message is not None:
        appeal = await db.get_private_message(user_id, message.reply_to_message.message_id)
        if appeal is not None and appeal["status"] != "open":
            await answer_temporarily(message, "Звернення вже закрито.")
            return
    open_appeals = await db.get_open_appeals(user_id)
    if appeal is None:
        if len(open_appeals) == 1:
            appeal = open_appeals[0]
        elif len(open_appeals) > 1:
            await delete_message_safe(message.bot, message.chat.id, message.message_id)
            await answer_temporarily(message, "У вас кілька звернень. Натисніть «Відповісти» під потрібним.", delay=3)
            return
        else:
            await delete_message_safe(message.bot, message.chat.id, message.message_id)
            await answer_temporarily(message, "У вас немає активних звернень.", delay=3)
            return

    data = await state.get_data()
    prompt_id = data.get("last_reply_prompt_id")
    if prompt_id:
        await delete_message_safe(message.bot, message.chat.id, prompt_id)
        await state.update_data(last_reply_prompt_id=None)

    success_msg = await message.answer("✅")
    schedule_message_deletion(message.bot, message.chat.id, success_msg.message_id, delay=2)
    
    try:
        await message.delete()
    except Exception:
        pass

    try:
        await deliver_followup(message.bot, user_id=user_id, source_message_id=message.message_id, appeal=appeal, content=message.text or message.caption or "Вкладення", sender_label=get_user_label(message.from_user))
        await update_private_card(message.bot, appeal["id"], user_id, appeal["conversation_message_id"])
    except Exception as e:
        logging.exception("Помилка")
        await answer_temporarily(message, "❌ Помилка при відправці.")

# ---------------------------------------------------------------------------
# Хендлери (Оператор / Групи)
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("block_"))
async def process_block_user(callback: CallbackQuery) -> None:
    if callback.message is None:
        return
    try:
        appeal_id = int(callback.data.removeprefix("block_"))
    except ValueError:
        return
    mapping = await db.get_group_message(callback.message.chat.id, callback.message.message_id)
    if mapping is None or mapping["appeal_id"] != appeal_id:
        await callback.answer("Не знайдено.", show_alert=True)
        return

    blocked = not await db.is_user_blocked(mapping["user_id"])
    await db.set_user_blocked(mapping["user_id"], blocked)
    
    await append_message_to_group_cards(callback.bot, appeal_id, "Система", 0, "Система", "Заблоковано" if blocked else "Розблоковано")
    
    try:
        notification = await callback.bot.send_message(
            chat_id=mapping["user_id"],
            text=("🚫 Вас заблоковано." if blocked else "✅ Вас розблоковано."),
            reply_parameters=(ReplyParameters(message_id=mapping["conversation_message_id"]) if mapping["conversation_message_id"] else None),
        )
        schedule_message_deletion(callback.bot, mapping["user_id"], notification.message_id)
    except TelegramAPIError:
        pass
    await callback.answer("Заблоковано" if blocked else "Розблоковано")

@router.message(OperatorReplyState.waiting_for_reply, F.chat.type.in_({"group", "supergroup"}))
async def process_operator_text_reply(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    appeal_id = data.get("reply_appeal_id")
    prompt_id = data.get("last_reply_prompt_id")
    
    if not appeal_id:
        await state.clear()
        return
        
    appeal = await db.get_appeal_by_id(appeal_id)
        
    if not appeal or appeal["status"] != "open":
        await reply_temporarily(message, "Звернення закрито.")
        await state.clear()
        return

    if prompt_id:
        await delete_message_safe(message.bot, message.chat.id, prompt_id)

    reply_text = message.text or message.caption or "Вкладення"

    old_user_msg_id = await db.get_active_operator_user_msg(appeal["id"])
    if old_user_msg_id:
        await delete_message_safe(message.bot, appeal["user_id"], old_user_msg_id)
    try:
        new_user_msg = await message.bot.send_message(
            chat_id=appeal["user_id"],
            text=f"<b>💬 Відповідь оператора:</b>\n\n{escape(reply_text)}",
        )
        await db.set_active_operator_user_msg(appeal["id"], new_user_msg.message_id)
    except TelegramAPIError:
        pass

    sender_label = get_user_label(message.from_user) if message.from_user else "Оператор"
    sender_id = message.from_user.id if message.from_user else 0
    
    await append_message_to_group_cards(message.bot, appeal["id"], "Оператор", sender_id, sender_label, reply_text)
    await update_private_card(message.bot, appeal["id"], appeal["user_id"], appeal["conversation_message_id"])
    
    await reply_temporarily(message, "✅", delay=2)
    schedule_message_deletion(message.bot, message.chat.id, message.message_id, delay=2)
    await state.clear()

@router.message(F.chat.type.in_({"group", "supergroup"}), F.reply_to_message)
async def process_admin_reply(message: Message) -> None:
    original = message.reply_to_message
    if original is None:
        return
    mapping = await db.get_group_message(message.chat.id, original.message_id)
    if mapping is None:
        return
    if mapping["status"] != "open" or await db.is_user_blocked(mapping["user_id"]):
        return

    reply_text = message.text or message.caption or "Вкладення"

    old_user_msg_id = await db.get_active_operator_user_msg(mapping["appeal_id"])
    if old_user_msg_id:
        await delete_message_safe(message.bot, mapping["user_id"], old_user_msg_id)

    try:
        new_user_msg = await message.bot.send_message(
            chat_id=mapping["user_id"],
            text=f"<b>💬 Відповідь оператора:</b>\n\n{escape(reply_text)}",
        )
        await db.set_active_operator_user_msg(mapping["appeal_id"], new_user_msg.message_id)
    except TelegramAPIError:
        pass

    sender_label = get_user_label(message.from_user) if message.from_user else "Оператор"
    sender_id = message.from_user.id if message.from_user else 0
    
    await append_message_to_group_cards(message.bot, mapping["appeal_id"], "Оператор", sender_id, sender_label, reply_text)
    await update_private_card(message.bot, mapping["appeal_id"], mapping["user_id"], mapping["conversation_message_id"])

    schedule_message_deletion(message.bot, message.chat.id, message.message_id, delay=2)

# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------

def acquire_instance_lock():
    if sys.platform == "win32":
        import msvcrt
        lock_file = BOT_LOCK_PATH.open("a+b")
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            lock_file.close()
            raise RuntimeError("Бот вже запущено.") from error
        return lock_file, "msvcrt"
    else:
        import fcntl
        lock_file = BOT_LOCK_PATH.open("a+b")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            lock_file.close()
            raise RuntimeError("Бот вже запущено.") from error
        return lock_file, "fcntl"

def release_instance_lock(lock_file, kind: str) -> None:
    if kind == "msvcrt":
        import msvcrt
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    lock_file.close()

async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    if not TOKEN:
        raise RuntimeError("Задайте BOT_TOKEN.")
    
    validate_all_categories()

    instance_lock, lock_kind = acquire_instance_lock()
    try:
        await db.init_db()  # Ініціалізація PostgreSQL
        bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
        dispatcher = Dispatcher()
        dispatcher.include_router(router)
        logging.info("Бот запущено...")
        await dispatcher.start_polling(bot)
    finally:
        await db.close_db() # Закриття пулу
        release_instance_lock(instance_lock, lock_kind)

if __name__ == "__main__":
    asyncio.run(main())