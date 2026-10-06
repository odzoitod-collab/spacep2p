"""Operator's cabinet («Оператор» in the menu): his debt for accepted Bybit orders and how to repay it (from his
balance, topped up with USDT BEP-20 like any user's), orders in work, free orders waiting for an operator, results.
The debt itself is kept in services/operators.py."""
from datetime import timedelta
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.emoji import back, btn, kb, pe
from bot.models import Deal, Operator, User, now
from bot.services import deals, events, money, operators
from bot.ui import app_btn, at, esc, ok, quote, section, show, title, warn

router = Router()
WORK = {"assigned": "мерчант пересоздаёт ордер", "checking": "выдать реквизиты", "waiting_payment": "ждём оплату",
        "paid": "проверьте оплату", "dispute": "спор"}
# the cabinet's groups, the operator's next step first
GROUPS = (("paid", "bell", "Проверьте оплату"), ("checking", "key", "Выдайте реквизиты"),
          ("waiting_payment", "clock", "Ждём перевод покупателя"), ("assigned", "refresh", "Мерчант пересоздаёт ордер"),
          ("dispute", "flag", "Спор"))


async def allowed(s: AsyncSession, uid: int) -> bool:
    """An operator, or a former one who still owes."""
    if await operators.is_operator(s, uid):
        return True
    op = await s.get(Operator, uid)
    return op is not None and op.debt > 0


async def _results(s: AsyncSession, uid: int, since) -> tuple[int, Decimal]:
    n, usdt = (await s.execute(select(func.count(Deal.id), func.coalesce(func.sum(Deal.seller_debit), 0)).where(
        Deal.operator_id == uid, Deal.via_bybit, Deal.status == "completed",
        *([Deal.closed_at >= since] if since else [])))).one()
    return n, Decimal(usdt)


async def operator_screen(bot: Bot, s: AsyncSession, user: User, src=None, note: str = ""):
    op = await s.get(Operator, user.id, populate_existing=True)
    debt = op.debt if op else Decimal(0)
    working = (await s.scalars(select(Deal).where(Deal.operator_id == user.id, Deal.via_bybit,
                                                  Deal.status.in_(tuple(WORK))).order_by(Deal.id))).all()
    free = (await s.scalars(select(Deal).where(Deal.status == "checking", Deal.operator_id.is_(None),
                                               Deal.seller_id != user.id, Deal.buyer_id != user.id)
                            .order_by(Deal.id).limit(5))).all()
    today, week, total = [await _results(s, user.id, since) for since in
                          (deals.day_start(), now() - timedelta(days=7), None)]
    active = await operators.is_operator(s, user.id)
    by = {st: [d for d in working if d.status == st] for st, _, _ in GROUPS}
    await show(bot, user, "\n".join(x for x in [
        title(pe("shop"), "Кабинет оператора"),
        None if active else f"{pe('pause')} Вы больше не оператор — погасите остаток долга.",
        "",
        section("fire", "Ваши сделки"),
        quote(*[f"• {label}: <b>{len(by[st])}</b>" for st, _, label in GROUPS if by[st]])
        if working else "<i>Принятых ордеров нет.</i>",
        f"{pe('bell')} Свободных ордеров ждут оператора: <b>{len(free)}</b>" if free else None,
        "",
        section("dollar", "Долг перед площадкой"),
        quote(f"• Принято по ордерам и не погашено: <b>{money.usdt(debt)} USDT</b>",
              f"• Ваш баланс в боте: {money.usdt(user.balance)} USDT",
              "• Гасится с баланса; не хватает — пополните кошелёк USDT в сети BEP-20 (BSC)")
        if debt else f"{pe('ok')} Долга нет.",
        "",
        section("stats", "Результаты"),
        quote(f"• Сегодня: <b>{today[0]}</b> ордеров · {money.usdt(today[1])} USDT",
              f"• 7 дней: {week[0]} · {money.usdt(week[1])} USDT · всего {total[0]}"),
    ] if x is not None) + note, kb(
        *[btn(f"#{d.id} · {money.fmt(d.amount_rub)} ₽ · {WORK[d.status]}", f"dl:{d.id}" if st != "checking"
              else f"orq:give:{d.id}", icon, style="danger" if st == "paid" else "primary" if st == "checking" else None)
          for st, icon, _ in GROUPS for d in by[st]],
        *([btn(f"Принять ордер #{d.id} · {money.fmt(d.amount_rub)} ₽", f"opq:go:{d.id}", "bell", style="success")
           for d in free] if active else []),
        btn(f"Погасить с баланса · {money.usdt(min(debt, user.balance))} USDT", "op:bal", "dollar", style="success")
        if debt and user.balance > 0 else None,
        btn("Пополнить баланс (USDT BEP-20)", "w:in", "plus") if debt and user.balance < debt else None,
        [btn("Обновить", "op", "refresh"), btn("История долга", "op:h", "list")],
        app_btn("Кабинет в приложении", "operator", "shop", style="primary", wide=True),
        back("menu", "В меню"),
    ), src)


@router.callback_query(F.data == "op")
async def cb_operator(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    if not await allowed(s, user.id):
        return await c.answer("Кабинет оператора доступен только операторам", show_alert=True)
    await operator_screen(bot, s, user, c)


@router.callback_query(F.data == "op:bal")
async def cb_from_balance(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    op = await s.get(Operator, user.id)
    amount = min(op.debt, user.balance) if op else Decimal(0)
    if amount <= 0:
        return await operator_screen(bot, s, user, c, warn("Нечем гасить: нет долга или баланса"))
    await show(bot, user, "\n".join([
        title(pe("dollar"), "Погасить долг с баланса"),
        quote(f"• Спишется с баланса: <b>{money.usdt(amount)} USDT</b>",
              f"• Долг: {money.usdt(op.debt)} → <b>{money.usdt(op.debt - amount)} USDT</b>"),
    ]), kb(btn("Погасить", "op:bal2", "ok", style="success"), back("op", "Отмена")), c)


@router.callback_query(F.data == "op:bal2")
async def cb_from_balance2(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    u = await money.lock(s, user.id)  # user row first, like deal completion does
    op = await operators.row(s, user.id, lock=True)
    amount = min(op.debt, u.balance)
    if amount <= 0:
        return await operator_screen(bot, s, user, c, warn("Нечем гасить: нет долга или баланса"))
    await money.add(s, user.id, -amount, "debt_repay", f"op:{user.id}")
    await operators.repay(s, user.id, amount, "с баланса")
    await s.commit()
    await operator_screen(bot, s, user, c, ok(f"Погашено {money.usdt(amount)} USDT с баланса"))


@router.callback_query(F.data == "op:h")
async def cb_history(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    rows = list(reversed(await events.history(s, f"op:{user.id}", limit=15)))
    await show(bot, user, "\n".join([
        title(pe("list"), "История долга"),
        "Новые сверху.",
        quote(*[f"• {at(e.created_at, 'dt')} · {esc(e.text[:150])}" for e in rows]) if rows else "Пока пусто.",
    ]), kb(back("op", "Кабинет оператора")), c)
