"""«Финансы»: the admin screen and the live message in the log chat's statistics topic (services/finance.py)."""
import re
from contextlib import suppress

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.admins import IsAdmin
from bot.emoji import back, btn, kb, pe
from bot.handlers import logchat
from bot.models import Setting, User, now
from bot.services import finance, money
from bot.ui import MSK, clean, field, mark, paced, quote, section, show, title

router = Router()
router.callback_query.filter(IsAdmin())


def _u(v) -> str:
    return "нет ответа" if v is None else f"{money.usdt(v)} USDT"


def render(sn: finance.Snapshot) -> str:
    free = sn.free
    lines = [
        title(pe("stats"), "Финансы Strait Pay") + f" · {now().astimezone(MSK):%d.%m %H:%M} МСК",
        "",
        section("wallet", "Что есть"),
        field("Горячий кошелёк TON — отсюда выводы", f"<b>{_u(sn.hot)}</b>"
              + (f" · газ {money.fmt(sn.hot_ton, 4)} TON" if sn.hot_ton is not None else "")),
        field("На адресах пополнения, ещё не собрано", f"<b>{money.usdt(sn.unswept)} USDT</b>") if sn.unswept else None,
        field("Долг операторов за Bybit-ордера", f"<b>{money.usdt(sn.op_debt)} USDT</b> — вернут на адреса долга "
              "(в «есть» не входит, пока не погашен)") if sn.op_debt else None,
        field("Пришло через Bybit-ордера", f"24 ч {money.usdt(sn.bybit['24h'])} · 7 д {money.usdt(sn.bybit['7d'])} · "
              f"всего {money.usdt(sn.bybit['all'])} USDT") if sn.bybit["all"] else None,
        "",
        section("people", "Что должны пользователям"),
        field("Балансы", f"<b>{money.usdt(sn.users_available)} USDT</b>"),
        field("В сделках", f"<b>{money.usdt(sn.users_frozen)} USDT</b>"),
        field("Командные балансы тимлидов", f"<b>{money.usdt(sn.users_team)} USDT</b>") if sn.users_team else None,
        field("Выводы в пути", f"<b>{money.usdt(sn.unpaid)} USDT</b> ({sn.unpaid_n})") if sn.unpaid_n else None,
        field("Итого", f"<b>{money.usdt(sn.liabilities)} USDT</b>"),
        "",
        (f"{mark('🟢')} <b>Можно забрать: {money.usdt(free)} USDT</b>" if free >= 0 else
         f"{mark('🔴')} <b>Не хватает: {money.usdt(-free)} USDT</b> — активов меньше, чем денег пользователей"),
        field("С учётом долга операторов", f"{money.usdt(free + sn.op_debt)} USDT") if sn.op_debt else None,
        "",
        section("up", "Прибыль площадки"),
        field("24 ч", f"<b>+{money.usdt(sn.profit['24h'])}</b> · 7 д +{money.usdt(sn.profit['7d'])} · "
              f"30 д +{money.usdt(sn.profit['30d'])} · всего +{money.usdt(sn.profit['all'])} USDT"),
        field("Выплачено тимлидам (уже вычтено)", f"{money.usdt(sn.team_paid)} USDT") if sn.team_paid else None,
        "",
        section("swap", "Оборот"),
        field("Сделок за 24 ч", f"{sn.volume['24h'][0]} на {money.fmt(sn.volume['24h'][1])} ₽"),
        field("За 7 д", f"{sn.volume['7d'][0]} на {money.fmt(sn.volume['7d'][1])} ₽"),
        field("Пользователей", f"{sn.users} · мерчантов на смене: {sn.online}"),
    ]
    if sn.queued_n:
        short = sn.queued - (sn.hot or 0)
        lines += ["", f"{pe('warn')} <b>В очереди на вывод {sn.queued_n} на {money.usdt(sn.queued)} USDT</b>"
                      + (f" — пополните горячий кошелёк минимум на {money.usdt(short)} USDT" if short > 0 else "")]
    lines += ["", quote("«Можно забрать» = что есть − что должны пользователям; прибыль уже внутри.")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line for line in lines if line is not None))


@router.callback_query(F.data == "afin")
async def cb_finance(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    await show(bot, user, render(await finance.snapshot(s)), kb(
        btn("Обновить", "afin", style="primary"), btn("Комиссии", "acm"), back("a", "Админ-панель")), c)


async def publish(bot: Bot, s: AsyncSession) -> None:
    """Keep one live statistics message in every log chat (its own topic in a forum): edit, or post once."""
    text = clean(render(await finance.snapshot(s)))
    for chat in logchat.targets():
        key = f"stats_msg:{chat}"
        row = await s.get(Setting, key)
        if row:
            try:
                await paced(lambda: bot.edit_message_text(text=text, chat_id=chat, message_id=int(row.value)))
                continue
            except TelegramBadRequest as e:
                if "not modified" in str(e):
                    continue
                # deleted by an admin: post it again
        with suppress(TelegramAPIError):
            m = await logchat.post(bot, s, chat, "stats", text, None, silent=True)
            await s.merge(Setting(key=key, value=str(m.message_id)))
    await s.commit()

