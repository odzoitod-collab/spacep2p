"""Slash commands. A command is the user's own message: it stays in the chat and the bot answers with a
new screen at the bottom; the previous screen keeps its text but loses its buttons (middlewares.Context).
A command always ends the current dialog step: it is never taken as an answer (amount, ticket text...)."""
from contextlib import suppress

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import config
from bot.handlers.admin import admin_screen
from bot.handlers.api_user import api_screen
from bot.handlers.deal import deal_screen, deals_screen
from bot.handlers.market import buy_list
from bot.handlers.seller import seller_menu
from bot.handlers.start import info_screen, main_menu
from bot.handlers.wallet import op_screen, wallet_screen
from bot.models import Deal, Ledger, User
from bot.ui import BRAND, TAGLINE, warn

router = Router()

COMMANDS = [
    ("start", "Strait Pay: главное меню"),
    ("buy", "Купить USDT"),
    ("sell", "Продать USDT: мои карты"),
    ("wallet", "Кошелёк: пополнить и вывести"),
    ("deals", "Мои сделки"),
    ("help", "Как это работает и условия"),
    ("support", "Написать оператору"),
    ("api", "API для сервисов"),
]
ADMIN_COMMANDS = COMMANDS + [("admin", "Админ-панель")]


DESCRIPTION = (f"{BRAND} — {TAGLINE}.\n\n"
               "• Покупайте USDT за рубли у проверенных продавцов: деньги продавца заморожены до конца сделки.\n"
               "• Продавайте USDT на свою карту или СБП и зарабатывайте процент с каждой сделки.\n"
               "• Пополнение и вывод через xRocket: счёт, адрес в любой сети, чек.\n"
               "• API для сервисов: приём рублей с зачислением в USDT.\n\nНажмите «Старт».")
SHORT = f"{BRAND}: {TAGLINE}. Покупка и продажа USDT за рубли и API для сервисов."


async def setup_commands(bot: Bot) -> None:
    """Command menu in Telegram's "/" button (admins also see /admin) and the bot's profile texts."""
    with suppress(TelegramAPIError):  # profile texts are cosmetic: never block the start
        await bot.set_my_description(DESCRIPTION[:512])
        await bot.set_my_short_description(SHORT[:120])
    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS],
                              scope=BotCommandScopeDefault())
    for aid in config.admin_ids:
        with suppress(TelegramAPIError):  # admin never opened the bot yet
            await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in ADMIN_COMMANDS],
                                      scope=BotCommandScopeChat(chat_id=aid))


@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(m: Message, bot: Bot, s: AsyncSession, user: User, is_admin: bool, state: FSMContext):
    await state.clear()
    await main_menu(bot, s, user, is_admin)


@router.message(Command("buy"))
async def cmd_buy(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    await buy_list(bot, s, user, state, 0)


@router.message(Command("sell"))
async def cmd_sell(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    await seller_menu(bot, s, user)


@router.message(Command("wallet"))
async def cmd_wallet(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    await wallet_screen(bot, s, user)


@router.message(Command("deals"))
async def cmd_deals(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    await deals_screen(bot, s, user)


@router.message(Command("help"))
async def cmd_help(m: Message, bot: Bot, user: User, state: FSMContext):
    await state.clear()
    await info_screen(bot, user)


@router.message(Command("support"))
async def cmd_support(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    await info_screen(bot, user)


@router.message(Command("api"))
async def cmd_api(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    await api_screen(bot, s, user)


@router.message(Command("deal"))
async def cmd_deal(m: Message, bot: Bot, s: AsyncSession, user: User, command: CommandObject, state: FSMContext):
    """/deal N — sent by picking a deal in the inline search: the current screen becomes the deal."""
    await state.set_state(None)
    arg = (command.args or "").strip().lstrip("#")
    d = await s.get(Deal, int(arg)) if arg.isdigit() else None
    if d is None or user.id not in (d.buyer_id, d.seller_id):
        return await main_menu(bot, s, user, user.id in config.admin_ids, note=warn("Сделка не найдена"))
    await deal_screen(bot, s, user, d)


@router.message(Command("op"))
async def cmd_op(m: Message, bot: Bot, s: AsyncSession, user: User, command: CommandObject, state: FSMContext):
    """/op N — sent by picking an operation in the inline search."""
    await state.set_state(None)
    arg = (command.args or "").strip()
    r = await s.get(Ledger, int(arg)) if arg.isdigit() else None
    if r is None or r.user_id != user.id:
        return await wallet_screen(bot, s, user, note=warn("Операция не найдена"))
    await op_screen(bot, s, user, r)


@router.message(Command("admin"))
async def cmd_admin(m: Message, bot: Bot, s: AsyncSession, user: User, is_admin: bool, state: FSMContext):
    await state.clear()
    if is_admin:
        return await admin_screen(bot, s, user)
    await main_menu(bot, s, user, is_admin)


@router.message(F.text.startswith("/"))
async def cmd_unknown(m: Message, bot: Bot, s: AsyncSession, user: User, is_admin: bool, state: FSMContext):
    await state.clear()
    await main_menu(bot, s, user, is_admin, note=warn(
        "Неизвестная команда. Доступны: " + " ".join(f"/{c}" for c, _ in (ADMIN_COMMANDS if is_admin else COMMANDS))))
