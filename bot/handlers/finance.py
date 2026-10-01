"""«Финансы»: the admin screen and the live message in the log chat's statistics topic (services/finance.py)."""
from contextlib import suppress

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import config
from bot.emoji import back, btn, kb, pe
from bot.handlers import logchat
from bot.models import Setting, User, now
from bot.services import finance, money
from bot.ui import MSK, clean, paced, show, title

router = Router()
router.callback_query.filter(F.from_user.id.in_(config.admin_ids))


def _u(v) -> str:
    return "нет ответа" if v is None else f"{money.usdt(v)} USDT"


def render(sn: finance.Snapshot) -> str:
    wallet = {"address": f"Ваш кошелёк (автоперевод): <b>{_u(sn.wallet)}</b>",
              "xrocket": "Автоперевод идёт на баланс xRocket",
              "none": "Кошелёк для автоперевода не задан"}[sn.wallet_kind]
    free = sn.free
    lines = [
        title(pe("stats"), "Финансы Strait Pay"),
        f"обновлено {now().astimezone(MSK):%d.%m %H:%M} МСК",
        "",
        "<b>Что есть</b>",
        f"xRocket (из него платятся выводы): <b>{_u(sn.xrocket)}</b>",
        wallet,
        f"Ещё на адресах пополнения: <b>{money.usdt(sn.unswept)} USDT</b>" if sn.unswept else "",
        f"Итого: <b>{money.usdt(sn.assets)} USDT</b>",
        f"Пришло на Bybit операторов по ордерам (в «есть» не входит — переведите на xRocket): 24 ч "
        f"<b>{money.usdt(sn.bybit['24h'])}</b> · 7 д {money.usdt(sn.bybit['7d'])} · всего "
        f"{money.usdt(sn.bybit['all'])} USDT" if sn.bybit["all"] else "",
        "",
        "<b>Что должны пользователям</b>",
        f"Балансы: <b>{money.usdt(sn.users_available)} USDT</b> · в сделках: <b>{money.usdt(sn.users_frozen)} USDT</b>",
        f"Выводы в пути: <b>{money.usdt(sn.unpaid)} USDT</b> ({sn.unpaid_n})" if sn.unpaid_n else "",
        f"Итого: <b>{money.usdt(sn.liabilities)} USDT</b>",
        "",
        (f"🟢 <b>Можно забрать: {money.usdt(free)} USDT</b>" if free >= 0 else
         f"🔴 <b>Не хватает: {money.usdt(-free)} USDT</b> — активов меньше, чем денег пользователей"),
        f"Прибыль площадки: 24 ч <b>+{money.usdt(sn.profit['24h'])}</b> · 7 д +{money.usdt(sn.profit['7d'])} · "
        f"30 д +{money.usdt(sn.profit['30d'])} · всего +{money.usdt(sn.profit['all'])} USDT",
        "",
        f"Сделок за 24 ч: {sn.volume['24h'][0]} на {money.fmt(sn.volume['24h'][1])} ₽ · за 7 д: "
        f"{sn.volume['7d'][0]} на {money.fmt(sn.volume['7d'][1])} ₽",
        f"Пользователей: {sn.users} · мерчантов на смене: {sn.online}",
    ]
    if sn.queued_n:
        short = sn.queued - (sn.xrocket or 0)
        lines += ["", f"⚠️ В очереди на вывод {sn.queued_n} на {money.usdt(sn.queued)} USDT"
                      + (f" — пополните xRocket минимум на {money.usdt(short)} USDT" if short > 0 else "")]
    lines += ["", "«Можно забрать» = что есть − что должны пользователям; прибыль уже внутри. Если на кошельке "
                  "автоперевода есть и ваши личные средства, они тоже учтены."]
    return "\n".join(line for line in lines if line is not None)


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

