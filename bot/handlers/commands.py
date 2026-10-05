"""Slash commands. A command is the user's own message: it stays in the chat and the bot answers with a
new screen at the bottom; the previous screen keeps its text but loses its buttons (middlewares.Context).
A command always ends the current dialog step: it is never taken as an answer (amount, ticket text...)."""
import re
from contextlib import suppress

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeChat, BotCommandScopeDefault,
                           Message)
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.admin import admin_screen
from bot.handlers.admin_deals import deal_view
from bot.handlers.api_user import api_screen
from bot.handlers.deal import deal_screen, deals_screen
from bot.handlers.market import buy_screen
from bot.handlers.orders import merchant_screen, take_screen
from bot.handlers.seller import seller_menu
from bot.handlers.start import info_screen, main_menu
from bot.handlers.team import joined_screen, team_screen
from bot.handlers.wallet import op_screen, wallet_screen
from bot.models import Deal, Ledger, Team, User
from bot.services import admins, teams
from bot.ui import BRAND, TAGLINE, esc, ok, warn

router = Router()

# the «/» menu: what people actually use, named by what it does; other commands still work, they are just not listed
COMMANDS = [
    ("start", "Главное меню"),
    ("buy", "Купить USDT за рубли"),
    ("sell", "Продать USDT на свою карту"),
    ("wallet", "Кошелёк: пополнить и вывести"),
    ("deals", "Мои сделки"),
    ("help", "Помощь и условия"),
    ("manager", "Связаться с менеджером"),
]
ADMIN_COMMANDS = COMMANDS + [("admin", "Админ-панель"), ("find", "Поиск: ID, @ник, #сделка, карта"),
                             ("deal", "Сделка по номеру: /deal 15"), ("balance", "Балансы пользователей")]
GROUP_COMMANDS = [("help", "Инструкции Strait Pay")]


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
    with suppress(TelegramAPIError):  # groups (the community and team chats): /help with the guides
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in GROUP_COMMANDS],
                                  scope=BotCommandScopeAllGroupChats())
    for aid in admins.ids():
        await admin_menu(bot, aid, True)
    await app_menu(bot)


async def app_menu(bot: Bot) -> None:
    """The button next to the input field: «Strait Pay» opens the mini app (no app address — the «/» menu)."""
    from aiogram.types import MenuButtonCommands, MenuButtonWebApp, WebAppInfo

    from bot import ui
    url = ui.app_url()
    with suppress(TelegramAPIError):
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Strait Pay", web_app=WebAppInfo(url=url))
                                       if url else MenuButtonCommands())


async def admin_menu(bot: Bot, uid: int, on: bool) -> None:
    """The «/» menu of one admin (or back to the general one when he stops being an admin)."""
    with suppress(TelegramAPIError):  # he never opened the bot yet
        if on:
            await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in ADMIN_COMMANDS],
                                      scope=BotCommandScopeChat(chat_id=uid))
        else:
            await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=uid))


START = re.compile(r"^(?:o(\d+))?(?:_?t(\d+))?$")
ADMIN_LINK = re.compile(r"^a-([a-z]+)-(\d+)$")  # ui.alink: an object's admin card


@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(m: Message, bot: Bot, s: AsyncSession, user: User, is_admin: bool, state: FSMContext,
                    command: CommandObject | None = None):
    """/start with a deep link: o<deal> — an order request from a chat post (take it here), t<team> — the team
    leader's referral link (join his team), o<deal>_t<team> — a request posted in a team chat (both), om — order
    merchants."""
    await state.clear()
    payload = (command.args or "").strip() if command and command.command == "start" else ""
    if payload == "om":
        return await merchant_screen(bot, s, user)
    if link := ADMIN_LINK.match(payload):
        from bot.handlers.admin_cmds import open_ref
        if is_admin:
            with suppress(TelegramAPIError):
                await m.delete()  # the screen is the answer; the /start line goes
            if await open_ref(bot, s, user, link[1], int(link[2])):
                return None
            return await admin_screen(bot, s, user, note=warn("Не найдено — возможно, запись удалена."))
        return await main_menu(bot, s, user, is_admin)
    found = START.match(payload) if payload else None
    if not found or not any(found.groups()):
        return await main_menu(bot, s, user, is_admin)
    deal_id, team_id = (int(x) if x else None for x in found.groups())
    team = await s.get(Team, team_id) if team_id else None
    fresh = bool(team) and await teams.join(s, user, team)
    if deal_id:
        return await take_screen(bot, s, user, deal_id, note=ok(f"Вы в команде «{esc(team.name)}»") if fresh else "")
    if team and team.leader_id == user.id:
        return await team_screen(bot, s, user)
    if team and team.status == "approved" and (fresh or user.team_id == team.id):
        return await joined_screen(bot, s, user, team, fresh)
    await main_menu(bot, s, user, is_admin, note=warn("Ссылка команды недействительна или вы уже в другой команде"))


@router.message(Command("buy"))
async def cmd_buy(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    await buy_screen(bot, s, user, state)


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


@router.message(Command("manager"))
async def cmd_manager(m: Message, bot: Bot, user: User, state: FSMContext):
    from bot.handlers.start import manager_url
    from bot.emoji import back, btn, kb, pe
    from bot.services import settings
    from bot.ui import quote, show, title
    await state.clear()
    url = manager_url()
    nick = settings.get("manager") or settings.get("support")
    await show(bot, user, "\n".join([
        title(pe("support"), "Менеджер Strait Pay"),
        "",
        quote(f"Вопросы по работе, условиям и сделкам — @{nick}. Укажите номер сделки и ваш ID "
              f"<code>{user.id}</code>." if url else "Менеджер пока не назначен — напишите в «Помощь».")]),
        kb(btn("Написать менеджеру", url=url, icon="support", style="success") if url else None,
           back("menu", "В меню")))


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
    if d is not None and admins.is_admin(user.id) and user.id not in (d.buyer_id, d.seller_id, d.operator_id):
        return await deal_view(bot, s, user, d)  # an admin follows any deal by its number: the full card
    if d is None or user.id not in (d.buyer_id, d.seller_id, d.operator_id):
        return await main_menu(bot, s, user, admins.is_admin(user.id), note=warn("Сделка не найдена"))
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
