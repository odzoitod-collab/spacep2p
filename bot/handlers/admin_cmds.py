"""Admin commands — in the private chat and in the admin chat, where a command's screen opens right in that chat
(ui.place) and nothing goes to the private chat — and the admin links /start a-<kind>-<id> (ui.alink): a name or a
number in a log or an admin screen opens that object's card in the bot.

In the admin chat only admins are answered: anyone else's commands get silence (middlewares.Context)."""
from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import ui
from bot.emoji import back, kb, pe
from bot.models import (Adjustment, ApiApplication, ApiClient, Card, Deal, Deposit, OrderMerchant, Signup, Team,
                        Ticket, User, Withdrawal)
from bot.services import admins
from bot.services.admins import IsAdmin
from bot.ui import card, cf, esc, show, ulink

router = Router()
router.message.filter(IsAdmin())
tail_router = Router()  # after every other admin command: an unknown command in the admin chat gets the help
tail_router.message.filter(IsAdmin())


def in_admin_chat(_) -> bool:
    return ui.place.get() is not None


HELP = [
    ("/admin", "админ-панель здесь же, в чате"),
    ("/deal 15", "сделка по номеру: вся информация и решения"),
    ("/user ID или @ник", "карточка пользователя: баланс, сделки, роли, бан"),
    ("/balance", "все балансы: правка по одному кнопками ±, начислить / списать / обнулить всем; /balance ID или @ник — сразу карточка"),
    ("/find запрос", "поиск: ID, @ник, #сделка, в12 — вывод, п12 — пополнение, номер карты или телефона"),
    ("/teams", "все команды; /team ID — карточка команды, ссылка в её чат"),
    ("/signups", "заявки на вход, ждущие решения"),
    ("/admins", "администраторы; /addadmin ID — назначить, /deladmin ID — снять (владельцы)"),
    ("/setts", "настроить этот чат: темы, закрепы со справкой"),
]


def help_text() -> str:
    return "\n".join([
        f"{pe('info')} <b>Команды админ-чата</b>",
        "",
        *[f"<blockquote><b>{esc(c)}</b>\n{d}</blockquote>" for c, d in HELP],
        "<blockquote><i>Команды и кнопки здесь работают только для администраторов; экраны открываются прямо в "
        "чате. Имена и номера в логах — ссылки: откроют карточку в личке с ботом.</i></blockquote>",
    ])


def usage(cmd: str, hint: str) -> str:
    return f"{pe('info')} Как пользоваться командой:\n\n<blockquote><code>{esc(cmd)}</code></blockquote>\n" \
           f"<blockquote>{hint}</blockquote>"


def failure(text: str, *fields: str) -> str:
    return "\n".join([f"{pe('warn')} {text}", "", card(*fields)])


def success(text: str, *fields: str) -> str:
    return "\n".join([f"{pe('ok')} {text}", "", card(*fields)])


async def open_ref(bot: Bot, s: AsyncSession, admin: User, kind: str, oid: int, src=None) -> bool:
    """The admin card of an object by its log kind and id. False if there is no such object."""
    from bot.handlers.admin import adjustment_screen, admin_card_screen, user_screen
    from bot.handlers.admin_api import app_screen, client_screen
    from bot.handlers.admin_deals import deal_view
    from bot.handlers.admin_ops import deposit_screen, ticket_screen, withdrawal_screen
    from bot.handlers.admin_orders import merchant_card
    from bot.handlers.admin_people import members_screen, operator_card, team_card
    from bot.handlers.signup import signup_card
    screens = {"deal": (Deal, deal_view), "user": (User, user_screen), "team": (Team, team_card),
               "teamm": (Team, members_screen), "wd": (Withdrawal, withdrawal_screen),
               "dep": (Deposit, deposit_screen), "ticket": (Ticket, ticket_screen), "card": (Card, admin_card_screen),
               "adj": (Adjustment, adjustment_screen), "signup": (Signup, signup_card),
               "om": (OrderMerchant, merchant_card), "apa": (ApiApplication, app_screen),
               "apc": (ApiClient, client_screen)}
    if kind == "op":
        if await s.get(User, oid) is None:
            return False
        await operator_card(bot, s, admin, oid, src)
        return True
    if kind not in screens:
        return False
    model, screen = screens[kind]
    obj = await s.get(model, oid, populate_existing=True)
    if obj is None:
        return False
    await screen(bot, s, admin, obj, src)
    return True


@router.message(Command("admin"), in_admin_chat)
async def cmd_admin(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    from bot.handlers.admin import admin_screen
    await state.clear()
    await admin_screen(bot, s, user)


@router.message(Command("help"), in_admin_chat)
async def cmd_help(m: Message, bot: Bot, user: User):
    await show(bot, user, help_text())


@router.message(Command("deal"), in_admin_chat)
async def cmd_deal(m: Message, bot: Bot, s: AsyncSession, user: User, command: CommandObject, state: FSMContext):
    await state.clear()
    arg = (command.args or "").strip().lstrip("#")
    if not arg.isdigit():
        return await show(bot, user, usage("/deal 15", "Номер сделки — из лога или из карточки пользователя."))
    if not await open_ref(bot, s, user, "deal", int(arg)):
        await show(bot, user, failure("Сделка не найдена.", cf("Номер", f"<code>#{esc(arg)}</code>")))


@router.message(Command("user"))
async def cmd_user(m: Message, bot: Bot, s: AsyncSession, user: User, command: CommandObject, state: FSMContext):
    from bot.handlers.admin import user_screen
    from bot.handlers.admin_people import find_user
    await state.clear()
    if not (command.args or "").strip():
        return await show(bot, user, usage("/user 123456789\n/user @username", "ID или @username пользователя бота."))
    u = await find_user(s, command.args)
    if u is None:
        return await show(bot, user, failure("Пользователь не найден.", cf("Искали", f"<code>{esc(command.args[:40])}</code>")))
    await user_screen(bot, s, user, u)


@router.message(Command("find"))
async def cmd_find(m: Message, bot: Bot, s: AsyncSession, user: User, command: CommandObject, state: FSMContext):
    from bot.handlers.admin import find
    await state.clear()
    q = (command.args or "").strip()
    if not q:
        return await show(bot, user, usage("/find 123456789\n/find @username\n/find #15\n/find в12\n/find 2200…",
                                           "ID или @ник — пользователь, #N — сделка, в12 / п12 — вывод / пополнение "
                                           "(откроется владелец), номер карты или телефон полностью — карта."))
    if not await find(bot, s, user, q):
        await show(bot, user, failure("Ничего не найдено.", cf("Запрос", f"<code>{esc(q[:40])}</code>")))


@router.message(Command("teams"))
async def cmd_teams(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    from bot.handlers.admin_people import teams_screen
    await state.clear()
    await teams_screen(bot, s, user)


@router.message(Command("team"), in_admin_chat)
async def cmd_team(m: Message, bot: Bot, s: AsyncSession, user: User, command: CommandObject, state: FSMContext):
    """In the admin chat /team never connects the chat to a team: it opens a team (or the list)."""
    from bot.handlers.admin_people import teams_screen
    await state.clear()
    arg = (command.args or "").strip().lstrip("#")
    if not arg:
        return await teams_screen(bot, s, user)
    if not arg.isdigit() or not await open_ref(bot, s, user, "team", int(arg)):
        await show(bot, user, failure("Команда не найдена.", cf("Номер", f"<code>{esc(arg[:20])}</code>")))


@router.message(Command("admins"))
async def cmd_admins(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    from bot.handlers.admin_people import admins_screen
    await state.clear()
    await admins_screen(bot, s, user)


@router.message(Command("addadmin", "deladmin"))
async def cmd_set_admin(m: Message, bot: Bot, s: AsyncSession, user: User, command: CommandObject,
                        state: FSMContext):
    from bot.handlers.admin_people import find_user, set_admin
    await state.clear()
    on = command.command == "addadmin"
    if not admins.is_owner(user.id):
        return await show(bot, user, failure("Назначать и снимать админов могут только владельцы (ADMIN_IDS)."))
    if not (command.args or "").strip():
        return await show(bot, user, usage(f"/{command.command} 123456789\n/{command.command} @username",
                                           "Человек должен хотя бы раз запустить бота." if on else
                                           "Владельцев из .env снять нельзя."))
    u = await find_user(s, command.args)
    if u is None:
        return await show(bot, user, failure("Пользователь не найден — пусть запустит бота.",
                                             cf("Искали", f"<code>{esc(command.args[:40])}</code>")))
    if not await set_admin(bot, s, user, u, on):
        why = (f"Уже {admins.role(u.id)}." if on else
               "Это владелец — права из .env не снимаются." if admins.is_owner(u.id) else "Он не администратор.")
        return await show(bot, user, failure(why, cf("Кто", ulink(u), icon="profile")))
    await show(bot, user, success("Администратор назначен." if on else "Права администратора сняты.",
                                  cf("Кто", ulink(u), icon="profile"),
                                  cf("Что дальше", "получил сообщение с кнопкой админ-панели и ссылкой в этот чат"
                                     if on else "удалён из админ-чата, меню команд обычное")),
               kb(back("aadm", "Администраторы")))


@tail_router.message(in_admin_chat, F.text.startswith("/"))
async def cmd_unknown(m: Message, bot: Bot, user: User):
    await show(bot, user, help_text())
