"""Dispatcher wiring shared by the bot process and the tests."""
import logging
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.strategy import FSMStrategy
from aiogram.types import ErrorEvent

from bot.emoji import btn, kb, pe
from bot.handlers import (admin, admin_api, admin_balance, admin_bsc, admin_chat, admin_cmds, admin_deals, admin_money,
                          admin_ops, admin_orders, admin_people,
                          api_user, channel, commands, community, deal, fallback, finance, inline, logchat, market, operator, orders, relay,
                          seller, signup, start, team, wallet)
from bot.middlewares import Context
from bot.storage import DbStorage, UserIsolation
from bot.ui import notify

log = logging.getLogger(__name__)
# admin_cmds.router first: the admin chat's commands; signup.router: a user who is not let in yet reaches nothing else;
# community.gate: the entry points of a user who has not joined the chat / channel yet;
# team.router early: it ends any other command in a group (not the admin chat); fallback must stay last
ROUTERS = (logchat.router, admin_cmds.router, admin_bsc.router, admin_balance.router, admin_money.router, signup.router, community.gate, community.router, team.router,
           admin_chat.events_router, signup.admin_router,
           inline.router, finance.router, admin_cmds.tail_router, commands.router, start.router, admin.router,
           admin_deals.router, admin_ops.router, channel.router,
           admin_chat.router, admin_api.router, admin_orders.router, admin_people.router, relay.router, market.router,
           seller.router, deal.router, orders.router, operator.router, wallet.router, api_user.router,
           fallback.router)


async def on_error(event: ErrorEvent, bot: Bot, state=None) -> None:
    """Any failure still gets an answer: a callback spinner never hangs and a broken dialog step is reset."""
    log.error("update %s failed", event.update.update_id, exc_info=event.exception)
    if state is not None:
        with suppress(Exception):
            await state.clear()
    upd = event.update
    if upd.callback_query:
        with suppress(TelegramAPIError):
            await upd.callback_query.answer("Не получилось выполнить действие. Попробуйте ещё раз или откройте /start",
                                            show_alert=True)
    elif upd.message:  # groups never reach handlers (middlewares.Context)
        await notify(bot, upd.message.chat.id, f"{pe('warn')} Не получилось обработать сообщение. "
                                               "Откройте меню и повторите шаг.", kb(btn("В меню", "menu", "shop")))


def build_dispatcher() -> Dispatcher:
    """GLOBAL_USER: the whole dialog lives in the private chat, so state is per user; for private chats
    the storage key is the same as with the default strategy."""
    dp = Dispatcher(storage=DbStorage(), events_isolation=UserIsolation(), fsm_strategy=FSMStrategy.GLOBAL_USER)
    dp.update.outer_middleware(Context())
    dp.errors.register(on_error)
    for r in ROUTERS:
        r._parent_router = None  # routers are module singletons: allow a new dispatcher (tests restart the bot)
    dp.include_routers(*ROUTERS)
    return dp
