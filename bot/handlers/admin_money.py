"""The admins' hand tools next to /balance (handlers.admin_balance): an operator's debt up and down with a reason,
a static card's limits and its flow set by hand."""
import re
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.emoji import back, kb, pe
from bot.models import Card, Operator, User
from bot.services import audit, deals, events, money, operators
from bot.services.admins import IsAdmin
from bot.ui import esc, notify, ok, quote, show, title, warn

router = Router()
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class Desk(StatesGroup):
    debt = State()
    card = State()


# ---------- an operator's debt, both ways ----------

@router.callback_query(F.data.regexp(r"^amd:(\d+):([+-])$"))
async def cb_debt(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    _, uid, sign = c.data.split(":")
    op = await s.get(Operator, int(uid))
    await state.set_state(Desk.debt)
    await state.set_data({"uid": int(uid), "sign": sign})
    await show(bot, user, "\n".join([
        title(pe("shop"), ("Добавить долг" if sign == "+" else "Уменьшить долг") + f" · {uid}"),
        quote(f"• Долг сейчас: <b>{money.usdt(op.debt if op else Decimal(0))} USDT</b>"),
        "Добавить — оператор получил USDT мимо бота; уменьшить — вернул их мимо бота. Отправьте: "
        "<code>сумма причина</code>, например <code>50 вернул на горячий кошелёк</code>.",
    ]), kb(back(f"aop:{uid}", "Отмена")), c)


@router.message(Desk.debt, F.text)
async def msg_debt(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    data = await state.get_data()
    found = re.match(r"^\s*(\d+(?:[.,]\d{1,6})?)\s+(.{3,200})$", m.text or "")
    if not found or Decimal(found.group(1).replace(",", ".")) <= 0:
        return await show(bot, user, warn("Сумма и причина: «50 вернул на горячий кошелёк»"), kb(back(f"aop:{data['uid']}",
                                                                                            "Отмена")))
    await state.clear()
    v, why = Decimal(found.group(1).replace(",", ".")), " ".join(found.group(2).split())
    uid = data["uid"]
    if data["sign"] == "+":
        op = await operators.accrue(s, uid, v, f"manual:{user.id}")
        text = f"+{money.usdt(v)} USDT"
    else:
        paid, _ = await operators.repay(s, uid, v, f"вручную, администратор {user.id}: {why}")
        op = await s.get(Operator, uid)
        text = f"−{money.usdt(paid)} USDT"
    audit.log(s, user.id, "operator_debt", f"op:{uid}", f"{text}: {why}")
    events.add(s, f"op:{uid}", "debt_manual", f"Долг {text} вручную ({user.name}): {why} · всего "
                                              f"{money.usdt(op.debt)} USDT", uid, alert=True)
    await s.commit()
    await notify(bot, uid, f"{pe('shop')} Администрация изменила ваш долг оператора: {text}. Причина: {esc(why)}. "
                           f"Долг сейчас: <b>{money.usdt(op.debt)} USDT</b>.")
    from bot.handlers.admin_people import operator_card
    await operator_card(bot, s, user, uid, note=ok(f"Долг {text}, теперь {money.usdt(op.debt)} USDT"))


# ---------- a static card set by hand ----------

FIELDS = {"min": "минимум одной сделки, ₽", "max": "максимум одной сделки, ₽", "daily": "лимит в день, ₽ (0 — без)"}


@router.callback_query(F.data.regexp(r"^amc:(min|max|daily):(\d+)$"))
async def cb_card_field(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    _, field, cid = c.data.split(":")
    cd = await s.get(Card, int(cid))
    if not cd or cd.is_deleted:
        return await c.answer("Карта удалена", show_alert=True)
    await state.set_state(Desk.card)
    await state.set_data({"cid": cd.id, "field": field})
    await show(bot, user, f"{title(pe('card'), f'Карта #{cd.id}')}\n\nОтправьте {FIELDS[field]}. Сейчас: "
                          f"{money.fmt(cd.min_rub)} – {money.fmt(cd.max_rub)} ₽"
                          + (f", в день {money.fmt(cd.daily_limit_rub)} ₽" if cd.daily_limit_rub else ""),
               kb(back(f"acv:{cd.id}", "Отмена")), c)


@router.message(Desk.card, F.text)
async def msg_card_field(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    from bot.handlers.admin import admin_card_screen
    from bot.handlers.seller import check_field, set_field
    data = await state.get_data()
    cd = await s.get(Card, data["cid"])
    owner = await s.get(User, cd.user_id)
    value, err = await check_field(s, owner, cd, data["field"], m.text or "")
    if err:
        return await show(bot, user, warn(err), kb(back(f"acv:{cd.id}", "Отмена")))
    await state.clear()
    set_field(cd, data["field"], value)
    audit.log(s, user.id, "card_limits", f"card:{cd.id}", f"{data['field']} = {value}")
    events.add(s, f"card:{cd.id}", "admin_limits", f"Администратор {user.name} изменил {FIELDS[data['field']]}: "
               f"{money.fmt(value) if value else 'без лимита'}", cd.user_id, notice=True)
    await s.commit()
    await notify(bot, cd.user_id, f"{pe('card')} Администрация изменила вашу карту {esc(cd.bank)}: {FIELDS[data['field']]} "
                                  f"— {money.fmt(value) if value else 'без лимита'}.")
    await admin_card_screen(bot, s, user, cd, note=ok("Сохранено"))


@router.callback_query(F.data.regexp(r"^amc:on:(\d+)$"))
async def cb_card_on(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    from bot.handlers.admin import admin_card_screen
    cd = await s.get(Card, int(c.data.split(":")[2]))
    owner = await s.get(User, cd.user_id) if cd else None
    if not cd or cd.is_deleted or cd.is_banned:
        return await c.answer("Карту нельзя включить: удалена или заблокирована", show_alert=True)
    if problem := deals.flow_problem(cd, owner):
        return await c.answer(f"Нельзя поставить в поток: {problem}", show_alert=True)
    cd.is_active = True
    audit.log(s, user.id, "card_on", f"card:{cd.id}")
    events.add(s, f"card:{cd.id}", "card_on", f"Включена администратором ({user.name})", cd.user_id, notice=True)
    await admin_card_screen(bot, s, user, cd, c, ok("Карта в потоке" + ("" if owner.is_online
                                                                         else " — владелец не на смене")))
