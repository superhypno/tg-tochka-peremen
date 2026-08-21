"""Telegram-бот с тестом для воронки консультации/диагностики."""

from __future__ import annotations

import asyncio
import html
import logging
import os
from dataclasses import dataclass

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

from database import Database
from questions import QUESTIONS, RESULTS, RESULT_ORDER


@dataclass(frozen=True)
class Config:
    bot_token: str
    channel_url: str
    diagnostic_url: str
    database_path: str
    admin_group_id: int | str | None


def load_config() -> Config:
    load_dotenv()
    required = ("BOT_TOKEN", "CHANNEL_URL", "DIAGNOSTIC_URL")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"В .env не заполнены: {', '.join(missing)}")
    raw_admin_group_id = os.getenv("ADMIN_GROUP_ID", "").strip()
    admin_group_id: int | str | None = None
    if raw_admin_group_id:
        admin_group_id = int(raw_admin_group_id) if raw_admin_group_id.lstrip("-").isdigit() else raw_admin_group_id
    return Config(
        bot_token=os.environ["BOT_TOKEN"],
        channel_url=os.environ["CHANNEL_URL"],
        diagnostic_url=os.environ["DIAGNOSTIC_URL"],
        database_path=os.getenv("DATABASE_PATH", "bot.sqlite3"),
        admin_group_id=admin_group_id,
    )


config = load_config()
db = Database(config.database_path)
router = Router()


class TestState(StatesGroup):
    answering = State()


def welcome_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Пройти тест →", callback_data="start_test")]])


def question_keyboard(index: int) -> InlineKeyboardMarkup:
    question = QUESTIONS[index]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=("А", "Б", "В", "Г", "Д")[position],
                    callback_data=f"answer:{index}:{option.key}",
                )
                for position, option in enumerate(question.options)
            ]
        ]
    )


def question_text(index: int) -> str:
    """Полный текст вариантов остаётся в сообщении, а не обрезается в кнопках."""
    question = QUESTIONS[index]
    options = "\n".join(option.text for option in question.options)
    return f"{question.text}\n\n{options}\n\nВыберите вариант кнопкой ниже:"


def result_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Записаться на бесплатную диагностику", url=config.diagnostic_url)],
            [InlineKeyboardButton(text="Подписаться на канал", url=config.channel_url)],
            [InlineKeyboardButton(text="Пройти тест ещё раз", callback_data="retake")],
        ]
    )


async def present_question(target: Message | CallbackQuery, index: int) -> None:
    text = question_text(index)
    markup = question_keyboard(index)
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


async def begin_test(callback: CallbackQuery, state: FSMContext) -> None:
    session_id = db.start_session(callback.from_user.id, (await state.get_data()).get("source"))
    db.log_event(callback.from_user.id, "test_started", {"session_id": session_id})
    await state.set_state(TestState.answering)
    await state.update_data(session_id=session_id, question_index=0)
    await callback.answer()
    await present_question(callback, 0)


async def send_result_to_admin_group(user, source: str | None, result_type: str, selected: list[str], bot: Bot) -> None:
    """Отправляет краткую заявку в закрытую группу, если она настроена."""
    if config.admin_group_id is None:
        return
    result = RESULTS[result_type]
    selected_answers = []
    for number, (question, selected_type) in enumerate(zip(QUESTIONS, selected), start=1):
        option = next(item for item in question.options if item.result_type == selected_type)
        selected_answers.append(f"{number}. {option.text}")
    answers = "\n".join(selected_answers)
    name = html.escape(" ".join(part for part in (user.first_name, user.last_name) if part) or "Без имени")
    username = html.escape(f"@{user.username}" if user.username else "не указан")
    text = (
        "<b>Новый завершённый тест</b>\n\n"
        f"<b>Пользователь:</b> {name}\n"
        f"<b>Username:</b> {username}\n"
        f"<b>Telegram ID:</b> <code>{user.id}</code>\n"
        f"<b>Источник:</b> {html.escape(source or 'неизвестен')}\n"
        f"<b>Результат:</b> {result['title']}\n"
        f"<b>Описание:</b> {result['body']}\n\n"
        f"<b>Ответы:</b>\n{answers}"
    )
    try:
        await bot.send_message(config.admin_group_id, text)
    except Exception:
        logging.exception("Не удалось отправить результат в группу")


@router.message(Command("start"))
async def start(message: Message, state: FSMContext, command: CommandObject) -> None:
    source = (command.args or "direct").strip()[:64]
    db.upsert_user(message.from_user, source)
    db.log_event(message.from_user.id, "start", {"source": source})
    await state.clear()
    await state.update_data(source=source)
    await message.answer(
        "<b>Тест «Что на самом деле вас тормозит?»</b>\n\n"
        "7 коротких вопросов, около 3 минут. В конце вы увидите ведущий паттерн, "
        "который может мешать двигаться вперёд, и направление для самостоятельного исследования.\n\n"
        "Это не медицинская или психологическая диагностика.",
        reply_markup=welcome_keyboard(),
    )


@router.callback_query(F.data == "start_test")
async def start_test(callback: CallbackQuery, state: FSMContext) -> None:
    await begin_test(callback, state)


@router.callback_query(F.data.startswith("answer:"), TestState.answering)
async def answer_question(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        _, raw_index, option_key = callback.data.split(":")
        index = int(raw_index)
    except (AttributeError, ValueError):
        await callback.answer("Не удалось распознать ответ. Начните тест заново.", show_alert=True)
        return

    if data.get("question_index") != index or not data.get("session_id"):
        await callback.answer("Этот вопрос уже обработан.", show_alert=True)
        return
    question = QUESTIONS[index]
    option = next((item for item in question.options if item.key == option_key), None)
    if option is None:
        await callback.answer("Такого варианта нет.", show_alert=True)
        return

    session_id = data["session_id"]
    db.save_answer(session_id, index, question.key, option.key, option.text, option.result_type)
    db.log_event(callback.from_user.id, "answer_saved", {"session_id": session_id, "question": index + 1})
    next_index = index + 1
    await callback.answer()

    if next_index < len(QUESTIONS):
        await state.update_data(
            question_index=next_index,
            selected_types=data.get("selected_types", []) + [option.result_type],
        )
        await present_question(callback, next_index)
        return

    # При равенстве баллов используется фиксированный порядок RESULT_ORDER.
    scores = {result_type: 0 for result_type in RESULT_ORDER}
    # Все семь ответов в этой сессии известны из FSM: сохраняем их типы по мере кликов.
    selected = data.get("selected_types", []) + [option.result_type]
    for result_type in selected:
        scores[result_type] += 1
    result_type = max(RESULT_ORDER, key=lambda item: scores[item])
    db.finish_session(session_id, result_type)
    db.log_event(callback.from_user.id, "test_completed", {"session_id": session_id, "result_type": result_type, "scores": scores})
    await send_result_to_admin_group(
        callback.from_user,
        data.get("source"),
        result_type,
        selected,
        callback.bot,
    )
    await state.clear()
    result = RESULTS[result_type]
    await callback.message.edit_text(
        f"<b>Ваш основной паттерн: {result['title']}</b>\n\n{result['body']}\n\n"
        "Если хотите разобраться в своей ситуации бережно и предметно, запишитесь на бесплатную диагностику. "
        "А в канале — больше материалов и практик для саморефлексии.",
        reply_markup=result_keyboard(),
    )


@router.callback_query(F.data == "retake")
async def retake(callback: CallbackQuery, state: FSMContext) -> None:
    db.log_event(callback.from_user.id, "retake_requested")
    await state.update_data(source="retake")
    await begin_test(callback, state)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    db.initialize()
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(router)
    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
