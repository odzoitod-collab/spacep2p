"""Operator's cabinet («Оператор» in the menu): his debt for accepted Bybit orders and how to repay it (an xRocket
invoice for the debt or from his balance), orders in work, free orders waiting for an operator, results.
The debt itself is kept in services/operators.py."""
from datetime import timedelta
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.emoji import back, btn, kb, pe
from bot.models import Deal, Deposit, Operator, User, now
from bot.services import deals, events, money, operators, xrocket
from bot.ui import at, esc, ok, quote, show, title, warn

router = Router()
WORK = {"checking": "выдать реквизиты", "waiting_payment": "ждём оплату", "paid": "проверьте оплату",
        "dispute": "спор"}


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
                                               Deal.seller_id != user.id).order_by(Deal.id).limit(5))).all()
    today, week, total = [await _results(s, user.id, since) for since in
                          (deals.day_start(), now() - timedelta(days=7), None)]
    invoice = await s.scalar(select(Deposit).where(Deposit.user_id == user.id, Deposit.purpose == "debt",
                                                   Deposit.status == "active").order_by(Deposit.id.desc()).limit(1))
    active = await operators.is_operator(s, user.id)
    await show(bot, user, "\n".join([
        title(pe("shop"), "Кабинет оператора"),
        "" if active else f"{pe('pause')} Вы больше не оператор — погасите остаток долга.",
        "<b>Долг перед площадкой</b>",
        quote(f"• Принято по Bybit-ордерам и не погашено: <b>{money.usdt(debt)} USDT</b>",
              f"• Ваш баланс в боте: {money.usdt(user.balance)} USDT",
              f"• Счёт на погашение #{invoice.id}: {money.usdt(invoice.amount)} USDT ждёт оплаты" if invoice else ""),
        "Долг растёт, когда вы подтверждаете оплату по ордеру: USDT ордера пришли вам на Bybit. Погасите его счётом "
        "xRocket или с баланса." if debt else f"{pe('ok')} Долга нет.",
        "<b>Результаты</b>",
        quote(f"• Сегодня: <b>{today[0]}</b> ордеров · {money.usdt(today[1])} USDT",
              f"• 7 дней: {week[0]} · {money.usdt(week[1])} USDT",
              f"• Всего: {total[0]} · {money.usdt(total[1])} USDT"),
        f"{pe('bell')} Свободных ордеров ждут оператора: <b>{len(free)}</b>" if free else "",
    ]) + note, kb(
        btn(f"Погасить через xRocket · {money.usdt(debt)} USDT", "op:pay", "wallet", style="success") if debt else None,
        btn(f"Погасить с баланса · {money.usdt(min(debt, user.balance))} USDT", "op:bal", "dollar")
        if debt and user.balance > 0 else None,
        btn(f"Оплатить счёт #{invoice.id}", url=invoice.link, icon="wallet") if invoice and invoice.link else None,
        *[btn(f"#{d.id} · {money.usdt(d.seller_debit)} USDT · {WORK[d.status]}", f"dl:{d.id}", "fire",
              style="primary" if d.status in ("checking", "paid") else None) for d in working],
        *([btn(f"Принять ордер #{d.id} · {money.usdt(d.seller_debit)} USDT", f"opq:go:{d.id}", "bell")
           for d in free] if active else []),
        [btn("История долга", "op:h", "list"), btn("Обновить", "op", "refresh")],
        back("menu", "В меню"),
    ), src)


@router.callback_query(F.data == "op")
async def cb_operator(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    if not await allowed(s, user.id):
        return await c.answer("Кабинет оператора доступен только операторам", show_alert=True)
    await operator_screen(bot, s, user, c)


@router.callback_query(F.data == "op:pay")
async def cb_pay(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    """An xRocket invoice for the whole debt; paid -> the debt goes down (wallet.check_deposit)."""
    op = await s.get(Operator, user.id)
    if not op or op.debt <= 0:
        return await operator_screen(bot, s, user, c, ok("Долга нет"))
    if live := await s.scalar(select(Deposit).where(Deposit.user_id == user.id, Deposit.purpose == "debt",
                                                    Deposit.status == "active", Deposit.amount == op.debt)):
        return await invoice_screen(bot, user, live, c)
    dep = Deposit(user_id=user.id, amount=op.debt, credit=op.debt, purpose="debt")
    s.add(dep)
    await s.flush()
    events.add(s, f"op:{user.id}", "invoice", f"Счёт на погашение долга #{dep.id}: {money.usdt(dep.amount)} USDT", user.id,
               notice=True)
    await s.commit()
    try:
        inv = await xrocket.rocket.create_invoice(dep.amount, f"dep-{dep.id}", "Погашение долга оператора Strait Pay")
    except xrocket.XRocketError as e:
        dep.status = "new" if e.uncertain else "failed"
        events.add(s, f"dep:{dep.id}", "xrocket_error", f"xRocket: {e}"[:500], user.id)
        await s.commit()
        return await operator_screen(bot, s, user, c, warn(f"Счёт не создан: {e.human}. Попробуйте позже."))
    dep.invoice_id, dep.link, dep.status = str(inv["id"]), xrocket.XRocket.link(inv), "active"
    await s.commit()
    await invoice_screen(bot, user, dep, c)


async def invoice_screen(bot: Bot, user: User, dep: Deposit, src=None):
    await show(bot, user, "\n".join([
        title(pe("wallet"), f"Погашение долга · счёт #{dep.id}"),
        quote(f"• К оплате: <b>{money.usdt(dep.amount)} USDT</b> в @xRocket",
              "• Комиссии нет: вся сумма идёт в погашение",
              f"• Оплатите до {at(dep.created_at + timedelta(hours=1))}"),
        "После оплаты долг уменьшится автоматически (проверка раз в минуту). Заплатили больше долга — разница "
        "придёт на баланс.",
    ]), kb(btn("Оплатить в xRocket", url=dep.link, icon="wallet", style="success") if dep.link else None,
           btn("Проверить оплату", f"w:chk:{dep.id}", "refresh"), back("op", "Кабинет оператора")), src)


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
