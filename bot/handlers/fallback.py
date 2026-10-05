"""Last router: nothing else matched. Never swallow input silently. Only private chats reach here."""
from aiogram import Bot, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.deal import process_receipt
from bot.handlers.start import main_menu
from bot.models import User
from bot.services import deals
from bot.ui import warn

router = Router()


@router.message()
async def any_message(m: Message, bot: Bot, s: AsyncSession, user: User, is_admin: bool, state: FSMContext):
    await state.set_state(None)
    waiting = [d for d in await deals.open_deals_of(s, user.id) if d.status == "waiting_payment"]
    d = waiting[0] if len(waiting) == 1 else None  # with several unpaid deals the receipt must be attached to one
    if (m.document or m.photo) and d:
        # e.g. input state was lost on restart: a PDF during an unpaid deal is its receipt
        return await process_receipt(bot, s, user, d, m, state)
    await main_menu(bot, s, user, is_admin, note=warn(
        "Сообщение не распознано: сейчас бот не ждёт ввода. Выберите действие кнопками ниже — "
        "если вы что-то заполняли, начните этот шаг заново."))


@router.callback_query()
async def any_callback(c: CallbackQuery):
    await c.answer("Кнопка устарела. Откройте меню: /start", show_alert=True)
