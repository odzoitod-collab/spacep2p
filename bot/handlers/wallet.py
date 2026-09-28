import logging
from decimal import ROUND_DOWN, Decimal
from datetime import timedelta
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.emoji import back, btn, kb, pe
from bot.models import Deposit, Ledger, User, Withdrawal, now
from bot.services import events, money, settings, ton, xrocket
from bot.ui import at, esc, notify, ok, quote, show, title, warn

log = logging.getLogger(__name__)
router = Router()

KINDS = {
    "deposit": "Пополнение xRocket", "ton_deposit": "Пополнение USDT TON", "withdraw": "Вывод", "withdraw_refund": "Возврат вывода",
    "deal_buy": "Покупка", "deal_sell": "Продажа", "admin": "Корректировка",
    "freeze": "Заморозка", "unfreeze": "Разморозка", "migration": "Остаток заморозки при обновлении",
}
WD_STATUS = {"queued": "в обработке", "pending": "отправляется", "sending": "отправляется", "sent": "в сети",
             "unknown": "проверяется", "done": "выполнен", "failed": "не выполнен, возвращён",
             "cancelled": "отменён, возвращён"}


def ref_label(ref: str) -> str:
    kind, _, num = (ref or "").partition(":")
    return {"deal": f"по сделке #{num}", "wd": f"#{num}", "dep": f"счёт #{num}", "adj": f"#{num}",
            "tdep": f"#{num}"}.get(kind, "")


class W(StatesGroup):
    deposit = State()
    withdraw = State()


def signed(v: Decimal) -> str:
    return f"{'+' if v > 0 else '−' if v < 0 else ''}{money.usdt(abs(v))}"


def ledger_line(r: Ledger, notes: bool = True) -> str:
    """One journal row in user terms: how the available balance and the frozen part moved."""
    avail = r.delta - r.frozen_delta
    head = f"{at(r.created_at, 'dt')} · {KINDS.get(r.kind, r.kind)} {ref_label(r.ref)}".rstrip()
    if avail and r.frozen_delta:
        amount = f"<b>{signed(avail)}</b> доступно"
    elif r.frozen_delta:
        amount = f"<b>{signed(r.frozen_delta)}</b> из заморозки"
    else:
        amount = f"<b>{signed(avail)}</b>"
    return f"{head} · {amount}" + (f"\n   причина: {esc(r.note)}" if notes and r.note else "")


def parse_usdt(raw: str | None) -> Decimal | None:
    try:
        v = Decimal((raw or "").replace(" ", "").replace(",", "."))
    except Exception:
        return None
    if not v.is_finite() or not 0 < v < Decimal("10000000"):
        return None
    amount = v.quantize(money.Q, ROUND_DOWN)
    return amount if amount > 0 else None


async def wallet_screen(bot: Bot, s: AsyncSession, user: User, src=None, note: str = ""):
    pending_dep = await s.scalar(select(Deposit).where(
        Deposit.user_id == user.id, Deposit.status == "active").order_by(Deposit.id.desc()).limit(1))
    checking = await s.scalar(select(func.count(Withdrawal.id)).where(
        Withdrawal.user_id == user.id, Withdrawal.status.in_(("pending", "unknown"))))
    queued = (await s.scalars(select(Withdrawal).where(Withdrawal.user_id == user.id, Withdrawal.status == "queued")
                              .order_by(Withdrawal.id))).all()
    lines = [
        title(pe("wallet"), "Кошелёк"),
        "",
        quote(
            f"{pe('dollar')} Доступно: <b>{money.usdt(user.balance)} USDT</b> — можно вывести или продать",
            f"{pe('lock')} Заморожено в сделках: <b>{money.usdt(user.frozen)} USDT</b>" if user.frozen else "",
        ),
        "",
        "Пополнить и вывести можно USDT в сети TON" + (" или через xRocket." if ton.enabled() else " через xRocket."),
    ]
    if checking:
        lines.append(f"{pe('clock')} Выводов на проверке: <b>{checking}</b> — сумма удержана, статус в истории.")
    if queued:
        lines.append(f"{pe('clock')} В обработке: " + ", ".join(
            f"#{w.id} — {money.usdt(w.amount - w.fee)} USDT" for w in queued) + ". Отправим автоматически.")
    await show(bot, user, "\n".join(lines) + note, kb(
        btn(f"Счёт #{pending_dep.id} ждёт оплаты", f"w:dp:{pending_dep.id}", "clock", style="primary") if pending_dep else None,
        [btn("Пополнить", "w:in" if ton.enabled() else "w:dep", "plus", style="success"),
         btn("Вывести", "w:out" if ton.enabled() else "w:wd", "up", style="danger")],
        *[btn(f"Отменить вывод #{w.id}", f"w:qc:{w.id}") for w in queued],
        btn("История операций", inline="операции "),
        back("menu", "В меню"),
    ), src)


@router.callback_query(F.data == "w")
async def cb_wallet(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.set_state(None)
    await wallet_screen(bot, s, user, c)


async def op_screen(bot: Bot, s: AsyncSession, user: User, r: Ledger, src=None):
    """One journal row: what happened, how the balance moved, and a link to the operation behind it."""
    kind, _, num = (r.ref or "").partition(":")
    avail = r.delta - r.frozen_delta
    await show(bot, user, "\n".join([
        title(pe("list"), f"{KINDS.get(r.kind, r.kind)} {ref_label(r.ref)}".strip()),
        f"{at(r.created_at, 'dt')}",
        "",
        quote(f"Доступный баланс: <b>{signed(avail)} USDT</b>" if avail else "",
              f"Заморозка: <b>{signed(r.frozen_delta)} USDT</b>" if r.frozen_delta else "",
              f"Причина: {esc(r.note)}" if r.note else ""),
        f"Сейчас доступно: <b>{money.usdt(user.balance)} USDT</b>"
        + (f", в сделках {money.usdt(user.frozen)}" if user.frozen else ""),
    ]), kb(btn(f"Сделка #{num}", f"dl:{num}", style="primary") if kind == "deal" else None,
           btn("История операций", inline="операции "), back("w", "Кошелёк")), src)


@router.callback_query(F.data == "w:in")
async def cb_deposit_choice(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    await state.set_state(None)
    await show(bot, user, "\n".join([
        title(pe("plus"), "Пополнение USDT"),
        "",
        "<b>USDT в сети TON</b> — ваш личный постоянный адрес: переведите с биржи, Tonkeeper или Telegram Wallet. "
        "Без комиссии, зачисление через 1–2 минуты.",
        "",
        f"<b>Через xRocket</b> — счёт на нужную сумму, оплата в @xRocket. Комиссия {settings.get('deposit_fee')}%.",
    ]), kb(btn("USDT в сети TON", "w:ton", style="success"), btn("Через xRocket", "w:dep"), back("w", "Кошелёк")), c)


@router.callback_query(F.data == "w:h")
async def cb_history(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    rows = (await s.scalars(
        select(Ledger).where(Ledger.user_id == user.id).order_by(Ledger.id.desc()).limit(15)
    )).all()
    lines = [ledger_line(r) for r in rows]
    wds = (await s.scalars(select(Withdrawal).where(Withdrawal.user_id == user.id)
                           .order_by(Withdrawal.id.desc()).limit(5))).all()
    wl = [f"#{w.id} · {money.usdt(w.amount)} USDT · {'TON' if w.method == 'ton' else 'чек'} · "
          f"{WD_STATUS.get(w.status, w.status)}" for w in wds]
    text = "\n".join([
        title(pe("list"), "История операций"),
        "Новые сверху. «доступно» — изменение свободного баланса, «из заморозки» — списание средств, "
        "замороженных под сделку.",
        "",
        quote(*lines) if lines else f"{pe('clock')} Операций пока нет.",
        *(["", title(pe("up"), "Выводы"), quote(*wl)] if wl else []),
    ])
    await show(bot, user, text, kb(
        *[btn(f"Чек #{w.id} · {money.usdt(w.amount - w.fee)} USDT", icon="wallet", url=w.link)
          for w in wds if w.status == "done" and w.link],
        *[btn(f"Вывод #{w.id} · транзакция", icon="search", url=w.link)
          for w in wds if w.method == "ton" and w.link],
        back("w", "Кошелёк")), c)


# ---------- deposit ----------

@router.callback_query(F.data == "w:dep")
async def cb_deposit(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    await state.set_state(W.deposit)
    await show(bot, user, "\n".join([
        title(pe("plus"), "Пополнение через xRocket"),
        "",
        quote(
            f"{pe('down')} Минимум: <b>{settings.get('deposit_min')} USDT</b>",
            f"{pe('percent')} Комиссия xRocket: <b>{settings.get('deposit_fee')}%</b> — удерживается из суммы",
        ),
        "",
        "Отправьте сумму счёта в USDT, например <code>100</code>. Мы создадим счёт в xRocket.",
    ]), kb(back("w", "Отмена")), c)


@router.message(W.deposit, F.text)
async def msg_deposit(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    v = parse_usdt(m.text)
    if v is None or v < settings.dec("deposit_min"):
        return await show(bot, user, f"{title(pe('plus'), 'Пополнение')}\n\nОтправьте сумму в USDT ещё раз."
                          + warn(f"Нужно число не меньше {settings.get('deposit_min')}"), kb(back("w", "Отмена")))
    await state.set_state(None)
    await money.lock(s, user.id)
    recent = await s.scalar(select(func.count(Deposit.id)).where(
        Deposit.user_id == user.id, Deposit.created_at > now() - timedelta(hours=1)))
    if recent >= 10:
        return await wallet_screen(bot, s, user, note=warn("Не более 10 счетов в час"))
    credit = (v * (1 - settings.dec("deposit_fee") / 100)).quantize(money.Q, ROUND_DOWN)
    dep = Deposit(user_id=user.id, amount=v, credit=credit)
    s.add(dep)
    await s.flush()
    events.add(s, f"dep:{dep.id}", "created", f"Счёт на {money.usdt(v)} USDT, ожидается {money.usdt(credit)} USDT",
               user.id)
    await s.commit()
    try:
        inv = await xrocket.rocket.create_invoice(v, f"dep-{dep.id}", f"Пополнение баланса Strait Pay на {v} USDT")
    except xrocket.XRocketError as e:
        log.warning("invoice create failed: %s", e)
        dep.status = "new" if e.uncertain else "failed"  # "new" is re-checked by the poller
        events.add(s, f"dep:{dep.id}", "xrocket_error", f"Создание счёта: {e}"[:500], user.id)
        await s.commit()
        return await wallet_screen(bot, s, user, note=warn(f"Не удалось создать счёт: {e.human}. Попробуйте позже."))
    dep.invoice_id, dep.link, dep.status = str(inv["id"]), xrocket.XRocket.link(inv), "active"
    events.add(s, f"dep:{dep.id}", "invoice", f"Счёт xRocket {dep.invoice_id} создан", user.id)
    await s.commit()
    await deposit_screen(bot, user, dep)


@router.callback_query(F.data.regexp(r"^w:dp:(\d+)$"))
async def cb_deposit_view(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    dep = await s.get(Deposit, int(c.data.split(":")[2]))
    if not dep or dep.user_id != user.id:
        return await c.answer()
    await deposit_screen(bot, user, dep, c)


async def deposit_screen(bot: Bot, user: User, dep: Deposit, src=None, note: str = ""):
    await show(bot, user, "\n".join([
        title(pe("plus"), f"Счёт на пополнение #{dep.id}"),
        "",
        quote(
            f"{pe('dollar')} К оплате: <b>{money.usdt(dep.amount)} USDT</b>",
            f"{pe('percent')} Комиссия xRocket {settings.get('deposit_fee')}%: <b>−{money.usdt(dep.amount - dep.credit)} USDT</b>",
            f"{pe('wallet')} Ожидается к зачислению: <b>{money.usdt(dep.credit)} USDT</b>",
        ),
        "",
        f"{pe('clock')} Оплатите до {at(dep.created_at + timedelta(hours=1))}. Зачислим фактически полученную "
        "xRocket сумму за вычетом комиссии; оплата проверяется автоматически раз в минуту.",
    ]) + note, kb(
        btn("Оплатить в xRocket", icon="wallet", url=dep.link, style="success") if dep.link else None,
        btn("Проверить оплату", f"w:chk:{dep.id}", "refresh"),
        back("w", "Кошелёк"),
    ), src)


async def check_deposit(s: AsyncSession, dep: Deposit) -> str:
    """Poll xRocket and credit once. Returns current status."""
    inv = (await xrocket.rocket.get_invoice(dep.invoice_id) if dep.invoice_id
           else await xrocket.rocket.get_invoice_by_client(f"dep-{dep.id}"))
    if not dep.invoice_id:
        dep.invoice_id = str(inv["id"])
        dep.link = xrocket.XRocket.link(inv)
        dep.status = "active"
        await s.flush()
    st = inv.get("status")
    if st in ("paid", "expired", "cancelled"):  # partially_paid and unknown statuses: keep waiting
        payments = await xrocket.rocket.get_invoice_payments(dep.invoice_id)
        if any(p.get("status") == "pending" for p in payments):
            return "active"  # a payment is still settling: crediting now would lose it
        received = sum((Decimal(p["receiveAmount"]) for p in payments
                        if p.get("status") == "paid" and p.get("receiveCurrency") == "USDT"), Decimal(0))
        received = received.quantize(money.Q, ROUND_DOWN)
        if received <= 0:
            if st in ("expired", "cancelled"):
                res = await s.execute(update(Deposit).where(Deposit.id == dep.id, Deposit.status == "active")
                                      .values(status="expired"))
                if res.rowcount == 1:
                    events.add(s, f"dep:{dep.id}", "expired", f"Счёт {st} без оплаты", dep.user_id)
                await s.commit()
                return "expired"
            return "active"
        res = await s.execute(update(Deposit).where(Deposit.id == dep.id, Deposit.status == "active").values(status="paid", credit=received))
        if res.rowcount == 1:
            await money.add(s, dep.user_id, received, "deposit", f"dep:{dep.id}")
            partial = st != "paid"
            events.add(s, f"dep:{dep.id}", "credited",
                       f"Зачислено {money.usdt(received)} USDT (статус счёта {st}, ожидалось {money.usdt(dep.credit)})"
                       if partial else f"Зачислено {money.usdt(received)} USDT", dep.user_id, alert=partial,
                       notice=True)
            await s.commit()
            return "credited"
        return "paid"
    return "active"


@router.callback_query(F.data.regexp(r"^w:chk:(\d+)$"))
async def cb_check(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    dep = await s.get(Deposit, int(c.data.split(":")[2]))
    if not dep or dep.user_id != user.id:
        return await c.answer()
    if dep.status in ("active", "new"):
        try:
            st = await check_deposit(s, dep)
        except xrocket.XRocketError as e:
            return await c.answer(f"Не удалось проверить: {e.human}. Проверим автоматически.", show_alert=True)
    else:
        st = dep.status
    if st in ("credited", "paid"):
        await s.refresh(user)
        return await wallet_screen(bot, s, user, c, ok(f"Зачислено {money.usdt(dep.credit)} USDT"))
    if st == "expired":
        return await wallet_screen(bot, s, user, c, warn(f"Счёт #{dep.id} истёк без оплаты. Создайте новый."))
    if st == "failed":
        return await wallet_screen(bot, s, user, c, warn(f"Счёт #{dep.id} не создан. Создайте новый."))
    await c.answer("Оплата ещё не поступила. Если вы оплатили — зачислим автоматически в течение пары минут.", show_alert=True)


# ---------- withdraw ----------

@router.callback_query(F.data == "w:wd")
async def cb_withdraw(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    await state.set_state(W.withdraw)
    await show(bot, user, withdraw_prompt(user), kb(back("w", "Отмена")), c)


def withdraw_prompt(user: User, err: str = "") -> str:
    fee = settings.dec("withdraw_fee")
    return "\n".join([
        title(pe("up"), "Вывод чеком xRocket"),
        "",
        quote(
            f"{pe('dollar')} Доступно: <b>{money.usdt(user.balance)} USDT</b>",
            f"{pe('down')} Минимум: <b>{settings.get('withdraw_min')} USDT</b>",
            f"{pe('percent')} Комиссия: <b>{money.usdt(fee)} USDT</b>" if fee else f"{pe('percent')} Без комиссии",
        ),
        "",
        "Отправьте сумму вывода в USDT, например <code>50</code>. Придёт персональный чек xRocket — "
        "активировать его сможете только вы.",
    ]) + (warn(err) if err else "")


@router.message(W.withdraw, F.text)
async def msg_withdraw(m: Message, bot: Bot, user: User, state: FSMContext):
    v = parse_usdt(m.text)
    fee = settings.dec("withdraw_fee")
    err = ("Введите сумму числом" if v is None
           else f"Минимум {settings.get('withdraw_min')} USDT" if v < settings.dec("withdraw_min")
           else "Сумма должна быть больше комиссии" if v <= fee
           else f"Доступно только {money.usdt(user.balance)} USDT" if v > user.balance else "")
    if err:
        return await show(bot, user, withdraw_prompt(user, err), kb(back("w", "Отмена")))
    await state.set_state(None)
    await state.update_data(wd_amount=str(v), wd_request=str(uuid4()))
    await show(bot, user, "\n".join([
        title(pe("up"), "Подтвердите вывод"),
        "",
        quote(
            f"{pe('wallet')} Спишется с баланса: <b>{money.usdt(v)} USDT</b>",
            f"{pe('percent')} Комиссия: {money.usdt(fee)} USDT" if fee else "",
            f"{pe('dollar')} Сумма чека: <b>{money.usdt(v - fee)} USDT</b>",
        ),
        "Чек придёт отдельным сообщением и сохранится в истории.",
    ]), kb(btn("Получить чек", "w:go", "ok", style="success"), back("w", "Отмена")))


@router.callback_query(F.data == "w:go")
async def cb_withdraw_go(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    data = await state.get_data()
    raw, request_id = data.get("wd_amount"), data.get("wd_request")
    await state.update_data(wd_amount=None, wd_request=None)
    if not raw or not request_id:
        return await c.answer("Эта заявка уже обработана или устарела. Проверьте историю или начните вывод заново.",
                              show_alert=True)
    amount = Decimal(raw)
    fee = settings.dec("withdraw_fee")
    if amount < settings.dec("withdraw_min") or amount <= fee:
        return await c.answer("Некорректная сумма", show_alert=True)
    wd = Withdrawal(user_id=user.id, amount=amount, fee=fee, request_id=request_id)
    s.add(wd)
    try:
        await s.flush()
        await money.add(s, user.id, -amount, "withdraw", f"wd:{wd.id}")
        events.add(s, f"wd:{wd.id}", "request", f"Запрос вывода: списано {money.usdt(amount)} USDT, "
                                                f"чек {money.usdt(amount - fee)}, комиссия {money.usdt(fee)}", user.id,
                   notice=True)
        await s.commit()
    except money.NotEnough:
        await s.rollback()
        await s.refresh(user)  # rollback expires every loaded object
        return await c.answer("Недостаточно средств", show_alert=True)
    except IntegrityError:
        await s.rollback()
        await s.refresh(user)  # rollback expires every loaded object
        return await c.answer("Заявка уже обработана", show_alert=True)
    await c.answer("Создаём чек…")
    result = await pay_or_queue(s, wd)
    await s.commit()
    await s.refresh(user)
    await wallet_screen(bot, s, user, c, payout_note(wd, result))
    if result == "done":
        await send_cheque(bot, wd)


# ---------- payout queue: when the xRocket app balance is short, withdrawals wait and go out in order ----------

ACTIVE = ("pending", "unknown", "sent")  # debited and on its way (not queued, not final)


def payout_note(wd: Withdrawal, result: str) -> str:
    net = money.usdt(wd.amount - wd.fee)
    return {
        "done": ok(f"Чек на {net} USDT отправлен отдельным сообщением." if wd.method == "xrocket"
                   else f"Вывод #{wd.id} выполнен: {net} USDT отправлены."),
        "sent": ok(f"Вывод #{wd.id} принят: {net} USDT уйдут в течение нескольких минут. Пришлём уведомление."),
        "queued": ok(f"Вывод #{wd.id} в обработке: {net} USDT отправим автоматически, обычно в течение часа. "
                     "Пришлём уведомление; до отправки вывод можно отменить в кошельке."),
        "unknown": warn(f"Вывод #{wd.id} на проверке: платёжный сервис не ответил. Сумма удержана — если перевод "
                        "создан, он дойдёт; если нет, повторим автоматически."),
    }.get(result) or warn(f"Вывод не выполнен: {result}. Средства возвращены на баланс.")


async def payout_need(wd: Withdrawal) -> Decimal:
    """USDT the xRocket app balance must have for this withdrawal (plus xRocket's network fee for TON)."""
    need = wd.amount - wd.fee
    if wd.method == "ton":
        from bot.handlers.ton_wallet import quota
        q = await quota()
        if q and q.get("withdrawFeeAsset") == "USDT":
            need += Decimal(str(q["withdrawFee"]))
    return need


async def pay_or_queue(s: AsyncSession, wd: Withdrawal) -> str:
    """Pay now if the app balance covers it and nobody is waiting ahead; otherwise queue. Does not commit."""
    ahead = await s.scalar(select(func.count(Withdrawal.id)).where(Withdrawal.status == "queued",
                                                                   Withdrawal.id < wd.id))
    try:
        funds = await xrocket.usdt_available()
    except Exception:  # noqa: BLE001 - balance unknown: let the request itself decide
        funds = None
    if ahead or (funds is not None and funds < await payout_need(wd)):
        return await queue(s, wd)
    return await pay(s, wd)


async def queue(s: AsyncSession, wd: Withdrawal) -> str:
    wd.status = "queued"
    events.add(s, f"wd:{wd.id}", "queued", f"В очереди: на балансе xRocket не хватает USDT для "
               f"{money.usdt(wd.amount - wd.fee)} USDT, отправим автоматически", wd.user_id, notice=True)
    return "queued"


async def pay(s: AsyncSession, wd: Withdrawal) -> str:
    """Ask xRocket to pay this withdrawal (cheque or TON). done | sent | unknown | queued | <reason> (refunded)."""
    if wd.method == "ton":
        from bot.handlers.ton_wallet import send_withdrawal
        return await send_withdrawal(s, wd)
    try:
        ch = await xrocket.rocket.create_cheque(wd.amount - wd.fee, f"wd-{wd.id}", wd.user_id,
                                                "Вывод с баланса Strait Pay")
    except xrocket.XRocketError as e:
        wd.error = str(e)[:1000]
        if e.code == "amount_more_than_app_balance":  # the app ran short meanwhile: wait instead of refusing
            return await queue(s, wd)
        if e.uncertain:
            # outcome unknown: keep funds reserved; the poller and admins reconcile by clientChequeId
            wd.status = "unknown"
            events.add(s, f"wd:{wd.id}", "unknown", f"Ответ xRocket неизвестен ({e.code}): деньги удержаны, "
                                                    "нужна сверка по clientChequeId", wd.user_id, alert=True)
            return "unknown"
        wd.status = "failed"
        await money.add(s, wd.user_id, wd.amount, "withdraw_refund", f"wd:{wd.id}")
        events.add(s, f"wd:{wd.id}", "failed", f"Отказ xRocket ({e.code}): {e.human}. {money.usdt(wd.amount)} USDT "
                                               "возвращены пользователю", wd.user_id, alert=True)
        return e.human
    await finish_withdrawal(s, wd, ch)
    return "done"


@router.callback_query(F.data.regexp(r"^w:qc:(\d+)$"))
async def cb_cancel_queued(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    wd = await s.get(Withdrawal, int(c.data.split(":")[2]), with_for_update=True, populate_existing=True)
    if not wd or wd.user_id != user.id or wd.status != "queued":
        return await c.answer("Вывод уже отправляется — отменить нельзя", show_alert=True)
    wd.status = "cancelled"
    await money.add(s, user.id, wd.amount, "withdraw_refund", f"wd:{wd.id}")
    events.add(s, f"wd:{wd.id}", "cancelled", f"Пользователь отменил вывод из очереди, {money.usdt(wd.amount)} USDT "
               "возвращены", user.id, notice=True)
    await s.flush()
    await s.refresh(user)
    await wallet_screen(bot, s, user, c, ok(f"Вывод #{wd.id} отменён, {money.usdt(wd.amount)} USDT снова на балансе."))


async def app_has(amount: Decimal) -> bool:
    """Pre-check of the xRocket app balance, so users are not debited and refunded for nothing.
    If the balance cannot be read, let the cheque request decide."""
    try:
        return await xrocket.usdt_available() >= amount
    except Exception:  # noqa: BLE001
        return True


async def finish_withdrawal(s: AsyncSession, wd: Withdrawal, cheque: dict) -> None:
    wd.cheque_id, wd.link, wd.status = str(cheque["chequeId"]), xrocket.XRocket.link(cheque), "done"
    events.add(s, f"wd:{wd.id}", "cheque", f"Чек {wd.cheque_id} выдан на {money.usdt(wd.amount - wd.fee)} USDT",
               wd.user_id, notice=True)
    if wd.fee:
        money.platform(s, wd.fee, "withdraw_fee", f"wd:{wd.id}")


async def send_cheque(bot: Bot, wd: Withdrawal) -> None:
    await notify(bot, wd.user_id, "\n".join([
        title(pe("dollar"), f"Чек на {money.usdt(wd.amount - wd.fee)} USDT · вывод #{wd.id}"),
        "",
        "Нажмите «Активировать чек» — USDT поступят на ваш кошелёк xRocket. Активировать может только ваш аккаунт.",
        "Ссылка также сохранена в «Кошелёк → История операций».",
    ]), kb(btn("Активировать чек", icon="wallet", url=wd.link, style="success") if wd.link else None,
           back("x", "Скрыть", "cross")))


async def reconcile(s: AsyncSession, wid: int) -> tuple[str, Withdrawal | None]:
    """Sync a withdrawal with xRocket by clientChequeId (read-only API call, safe to repeat).

    Returns (result, withdrawal): done | refunded | active | missing | manual | busy | error:<human>.
    Does not commit.
    """
    wd = await s.get(Withdrawal, wid)
    if not wd or wd.status not in ("unknown", "done"):
        return "busy", wd
    ref = f"wd:{wid}"
    try:
        cheque = await xrocket.rocket.get_cheque_by_client(f"wd-{wid}")
    except xrocket.XRocketError as e:
        if e.code == "app_cheque_not_found":
            if wd.status == "unknown":
                await events.alert_once(s, ref, "missing", "Чек не найден в xRocket: нужна ручная проверка и возврат",
                                        wd.user_id)
            return "missing", wd
        await events.alert_once(s, ref, "check_error", f"Ошибка сверки: {e}"[:300], wd.user_id)
        return f"error:{e.human}", wd
    wd = await s.get(Withdrawal, wid, with_for_update=True, populate_existing=True)
    if wd.status not in ("unknown", "done"):
        return "busy", wd
    if cheque.get("deleted"):
        was_done = wd.status == "done"
        wd.status = "failed"
        await money.add(s, wd.user_id, wd.amount, "withdraw_refund", f"wd:{wd.id}")
        if was_done and wd.fee:
            money.platform(s, -wd.fee, "withdraw_fee_reversal", f"wd:{wd.id}")
        events.add(s, ref, "refunded", f"Чек отменён в xRocket, {money.usdt(wd.amount)} USDT возвращены на баланс",
                   wd.user_id, alert=True)
        return "refunded", wd
    if not cheque.get("chequeId") or cheque.get("state") == "error":
        await events.alert_once(s, ref, "manual", f"Состояние чека: {cheque.get('state')} — нужна ручная проверка",
                                wd.user_id)
        return "manual", wd
    if wd.status == "done":
        events.add(s, ref, "checked", f"Сверка: чек {cheque.get('state') or 'active'}", wd.user_id)
        return "active", wd
    await finish_withdrawal(s, wd, cheque)
    return "done", wd
