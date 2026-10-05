"""Wallet: everything goes through xRocket Pay API — deposits by invoice link or by an on-chain address that xRocket
gives for any network it supports, withdrawals by a personal cheque or to an external address in any network."""
import logging
import re
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.emoji import back, btn, kb, pe
from bot.models import Deposit, Ledger, Operator, User, Withdrawal, now
from bot.services import events, money, operators, settings, xrocket
from bot.ui import at, esc, field, notify, ok, quote, section, show, title, warn

log = logging.getLogger(__name__)
router = Router()

KINDS = {
    "deposit": "Пополнение", "ton_deposit": "Пополнение USDT TON", "withdraw": "Вывод", "withdraw_refund": "Возврат вывода",
    "deal_buy": "Покупка", "deal_sell": "Продажа", "admin": "Корректировка", "team_fee": "Доход тимлида",
    "team_income": "Доход команды (командный баланс)", "team_out": "С командного баланса",
    "team_in": "С командного баланса на основной",
    "debt_repay": "Погашение долга оператора", "freeze": "Заморозка", "unfreeze": "Разморозка",
    "migration": "Остаток заморозки при обновлении",
}
WD_STATUS = {"queued": "в очереди", "pending": "отправляется", "sent": "в сети", "unknown": "проверяется",
             "done": "выполнен", "failed": "не выполнен, возвращён", "cancelled": "отменён, возвращён"}
ADDRESS_HOURS = 24  # an address deposit invoice lives this long


def ref_label(ref: str) -> str:
    kind, _, num = (ref or "").partition(":")
    return {"deal": f"по сделке #{num}", "wd": f"#{num}", "dep": f"#{num}", "adj": f"#{num}",
            "tdep": f"#{num}"}.get(kind, "")


class W(StatesGroup):
    deposit = State()
    withdraw = State()
    address = State()
    memo = State()
    amount = State()


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


def fee_pct() -> str:
    return f"{money.fmt(settings.dec('deposit_fee'), 3)}%"


def withdraw_fee(amount: Decimal, method: str, net_fee: Decimal = Decimal(0)) -> Decimal:
    """withdraw_pct of the amount (to whole cents, up) plus the fixed part: withdraw_fee for a cheque; for an address
    chain_withdraw_fee, which already covers xRocket's network fee — if the network takes more, that fee instead."""
    pct = (amount * settings.dec("withdraw_pct") / 100).quantize(money.KOP, ROUND_UP)
    fixed = settings.dec("withdraw_fee") if method == "xrocket" else max(settings.dec("chain_withdraw_fee"), net_fee)
    return pct + fixed


def withdraw_terms(method: str, net_fee: Decimal = Decimal(0)) -> str:
    """The fee in words: «1,5% + 3 USDT (сеть включена)»."""
    pct = f"{money.fmt(settings.dec('withdraw_pct'), 3)}%"
    if method == "xrocket":
        fixed = settings.dec("withdraw_fee")
        return pct + (f" + {money.usdt(fixed)} USDT" if fixed else "")
    fixed = max(settings.dec("chain_withdraw_fee"), net_fee)
    return f"{pct} + {money.usdt(fixed)} USDT" + (" (сеть включена)" if fixed > net_fee or not net_fee else
                                                  " (комиссия сети)")


async def wallet_screen(bot: Bot, s: AsyncSession, user: User, src=None, note: str = ""):
    pending_dep = await s.scalar(select(Deposit).where(
        Deposit.user_id == user.id, Deposit.status == "active").order_by(Deposit.id.desc()).limit(1))
    checking = await s.scalar(select(func.count(Withdrawal.id)).where(
        Withdrawal.user_id == user.id, Withdrawal.status.in_(("pending", "unknown", "sent"))))
    queued = (await s.scalars(select(Withdrawal).where(Withdrawal.user_id == user.id, Withdrawal.status == "queued")
                              .order_by(Withdrawal.id))).all()
    op = await s.get(Operator, user.id)
    lines = [
        title(pe("wallet"), "Кошелёк"),
        "",
        field("Доступно", f"<b>{money.usdt(user.balance)} USDT</b>"),
        field("Можно вывести", f"<b>{money.usdt(money.withdrawable(user))} USDT</b> · ещё прокрутить "
              f"{money.usdt(user.balance - money.withdrawable(user))} USDT в сделках")
        if money.withdrawable(user) < user.balance else None,
        field("В сделках", f"{money.usdt(user.frozen)} USDT") if user.frozen else None,
        field("Командный баланс", f"{money.usdt(user.team_balance)} USDT — перевести в «Команда»")
        if user.team_balance else None,
        field("Выводов в пути", f"{checking} — статус в истории") if checking else None,
        field("Долг оператора", f"<b>{money.usdt(op.debt)} USDT</b> — погасить в «Оператор»") if op and op.debt else None,
    ]
    if queued:
        lines += ["", section("clock", "Очередь на вывод")]
        for w in queued:
            ahead_n, ahead = (await s.execute(select(func.count(Withdrawal.id), func.coalesce(
                func.sum(Withdrawal.amount - Withdrawal.fee), 0)).where(Withdrawal.status == "queued",
                                                                       Withdrawal.id < w.id))).one()
            lines.append(field(f"#{w.id}", f"{money.usdt(w.amount - w.fee)} USDT · место {ahead_n + 1}"
                               + (f", перед вами {money.usdt(Decimal(ahead))} USDT" if ahead_n else "")))
        lines.append(quote("Уйдёт автоматически, как только у сервиса хватит USDT — обычно в течение часа. До "
                           "отправки вывод можно отменить."))
    lines += ["", quote(f"Пополнение {fee_pct()} · вывод чеком {withdraw_terms('xrocket')} · на кошелёк "
                        f"{withdraw_terms('chain')}")]
    await show(bot, user, "\n".join(x for x in lines if x is not None) + note, kb(
        btn(f"Пополнение #{pending_dep.id} ждёт оплаты", f"w:dp:{pending_dep.id}", "clock", style="primary")
        if pending_dep else None,
        [btn("Пополнить", "w:in", "plus", style="success"), btn("Вывести", "w:out", "up", style="danger")],
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
        quote(f"Доступный баланс: <b>{signed(avail)} USDT</b>" if avail else "",
              f"Заморозка: <b>{signed(r.frozen_delta)} USDT</b>" if r.frozen_delta else "",
              f"Причина: {esc(r.note)}" if r.note else ""),
        f"Сейчас доступно: <b>{money.usdt(user.balance)} USDT</b>",
    ]), kb(btn(f"Сделка #{num}", f"dl:{num}", style="primary") if kind == "deal" else None,
           btn("История операций", inline="операции "), back("w", "Кошелёк")), src)


# ---------- deposit: invoice link or address in a network ----------

def _net_rows(prefix: str, nets: list[str]):
    return [[btn(xrocket.net_name(n), f"{prefix}:{n}") for n in nets[i:i + 2]] for i in range(0, len(nets), 2)]


@router.callback_query(F.data == "w:in")
async def cb_deposit_choice(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    await state.set_state(None)
    nets = await xrocket.networks()
    await show(bot, user, "\n".join([
        title(pe("plus"), "Пополнение USDT"),
        "",
        section("swap", "Адрес в сети — с биржи или кошелька"),
        "Выберите сеть — бот выдаст адрес прямо здесь, зачислим сами.",
        "",
        section("wallet", "Счёт xRocket — из @xRocket"),
        "На любую сумму, оплата в два нажатия.",
        "",
        quote(f"Комиссия {fee_pct()} · минимум {settings.get('deposit_min')} USDT. Отправляйте только USDT и только "
              "в выбранной сети."),
    ]), kb(*_net_rows("w:adr", nets), btn("Счёт xRocket", "w:dep", "wallet", style="success"), back("w", "Кошелёк")), c)


@router.callback_query(F.data == "w:h")
async def cb_history(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    rows = (await s.scalars(select(Ledger).where(Ledger.user_id == user.id).order_by(Ledger.id.desc()).limit(15))).all()
    wds = (await s.scalars(select(Withdrawal).where(Withdrawal.user_id == user.id)
                           .order_by(Withdrawal.id.desc()).limit(5))).all()
    wl = [f"#{w.id} · {money.usdt(w.amount)} USDT · {_wd_where(w)} · {WD_STATUS.get(w.status, w.status)}" for w in wds]
    await show(bot, user, "\n".join([
        title(pe("list"), "История операций"),
        "Новые сверху.",
        quote(*[ledger_line(r) for r in rows]) if rows else "Операций пока нет.",
        *(["<b>Выводы</b>", quote(*wl)] if wl else []),
    ]), kb(
        *[btn(f"Вывод #{w.id} · {'чек' if w.method == 'xrocket' else 'транзакция'}", icon="wallet", url=w.link)
          for w in wds if w.link and (w.status == "done" or w.method == "chain")],
        back("w", "Кошелёк")), c)


def _wd_where(w: Withdrawal) -> str:
    return "чек" if w.method == "xrocket" else xrocket.net_name(w.network)


@router.callback_query(F.data == "w:dep")
async def cb_deposit(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    await state.set_state(W.deposit)
    await show(bot, user, _deposit_prompt(), kb(back("w:in", "Назад")), c)


def _deposit_prompt(err: str = "") -> str:
    return "\n".join([
        title(pe("plus"), "Счёт xRocket"),
        quote(f"Минимум: <b>{settings.get('deposit_min')} USDT</b> · комиссия {fee_pct()}"),
        "Отправьте сумму в USDT, например <code>100</code>.",
    ]) + (warn(err) if err else "")


async def _deposit_allowed(s: AsyncSession, user: User) -> bool:
    await money.lock(s, user.id)
    recent = await s.scalar(select(func.count(Deposit.id)).where(
        Deposit.user_id == user.id, Deposit.created_at > now() - timedelta(hours=1)))
    return recent < 10


@router.message(W.deposit, F.text)
async def msg_deposit(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    v = parse_usdt(m.text)
    if v is None or v < settings.dec("deposit_min"):
        return await show(bot, user, _deposit_prompt(f"Нужно число не меньше {settings.get('deposit_min')}"),
                          kb(back("w:in", "Назад")))
    await state.set_state(None)
    if not await _deposit_allowed(s, user):
        return await wallet_screen(bot, s, user, note=warn("Не более 10 пополнений в час"))
    dep = Deposit(user_id=user.id, amount=v, credit=credit_of(v))
    s.add(dep)
    await s.flush()
    events.add(s, f"dep:{dep.id}", "created", f"Счёт xRocket на {money.usdt(v)} USDT", user.id)
    await s.commit()
    try:
        inv = await xrocket.rocket.create_invoice(v, f"dep-{dep.id}", f"Пополнение Strait Pay на {v} USDT")
    except xrocket.XRocketError as e:
        return await _deposit_failed(bot, s, user, dep, e)
    dep.invoice_id, dep.link, dep.status = str(inv["id"]), xrocket.XRocket.link(inv), "active"
    events.add(s, f"dep:{dep.id}", "invoice", f"Счёт xRocket {dep.invoice_id} создан", user.id)
    await s.commit()
    await deposit_screen(bot, user, dep)


async def _deposit_failed(bot, s, user, dep: Deposit, e: xrocket.XRocketError, src=None):
    log.warning("deposit %s: %s", dep.id, e)
    # "new" is re-checked by the poller; an address deposit without its address cannot be paid: failed
    dep.status = "new" if e.uncertain and not dep.invoice_id and not dep.network else "failed"
    events.add(s, f"dep:{dep.id}", "xrocket_error", f"xRocket: {e}"[:500], user.id)
    await s.commit()
    await wallet_screen(bot, s, user, src, warn(f"Не удалось создать пополнение: {e.human}. Попробуйте позже."))


def credit_of(received: Decimal) -> Decimal:
    return (received * (1 - settings.dec("deposit_fee") / 100)).quantize(money.Q, ROUND_DOWN)


@router.callback_query(F.data.regexp(r"^w:adr:([A-Z]{2,5})$"))
async def cb_address(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.set_state(None)
    net = c.data.split(":")[2]
    if net not in await xrocket.networks():
        return await c.answer("Сеть сейчас недоступна", show_alert=True)
    dep = await s.scalar(select(Deposit).where(
        Deposit.user_id == user.id, Deposit.network == net, Deposit.status == "active", Deposit.address.is_not(None),
        Deposit.expires_at > now() + timedelta(minutes=15)).order_by(Deposit.id.desc()).limit(1))
    if dep:  # a live address of this network: show it again instead of a new one
        return await deposit_screen(bot, user, dep, c)
    if not await _deposit_allowed(s, user):
        return await wallet_screen(bot, s, user, c, warn("Не более 10 пополнений в час"))
    dep = Deposit(user_id=user.id, amount=Decimal(0), credit=Decimal(0), network=net)
    s.add(dep)
    await s.flush()
    events.add(s, f"dep:{dep.id}", "created", f"Адрес для пополнения в сети {xrocket.net_name(net)}", user.id)
    await s.commit()
    try:
        inv = await xrocket.rocket.create_invoice(None, f"dep-{dep.id}", "Пополнение Strait Pay",
                                                  min_payment=settings.dec("deposit_min"),
                                                  expires_ms=ADDRESS_HOURS * 3_600_000)
        dep.invoice_id, dep.link = str(inv["id"]), xrocket.XRocket.link(inv)
        addr = await xrocket.rocket.payment_address(dep.invoice_id, net)
    except xrocket.XRocketError as e:
        return await _deposit_failed(bot, s, user, dep, e, c)
    dep.address, dep.status = addr["address"][:128], "active"
    dep.expires_at = _when(addr.get("expiresAt")) or now() + timedelta(hours=ADDRESS_HOURS)
    events.add(s, f"dep:{dep.id}", "address", f"Адрес {dep.address} ({xrocket.net_name(net)}), счёт {dep.invoice_id}",
               user.id)
    await s.commit()
    await deposit_screen(bot, user, dep, c)


def _when(raw: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else None
    except ValueError:
        return None


@router.callback_query(F.data.regexp(r"^w:dp:(\d+)$"))
async def cb_deposit_view(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    dep = await s.get(Deposit, int(c.data.split(":")[2]))
    if not dep or dep.user_id != user.id:
        return await c.answer()
    await deposit_screen(bot, user, dep, c)


async def deposit_screen(bot: Bot, user: User, dep: Deposit, src=None, note: str = ""):
    if dep.address:
        text = "\n".join(x for x in [
            title(pe("plus"), f"Пополнение · {xrocket.net_name(dep.network)}"),
            "",
            section("key", "Адрес для перевода"),
            f"<code>{esc(dep.address)}</code>",
            "",
            field("Сеть", f"<b>{xrocket.net_name(dep.network)}</b> — только USDT, другое не зачислится"),
            field("Сумма", f"от {settings.get('deposit_min')} USDT одним переводом · комиссия {fee_pct()}"),
            field("Действует до", at(dep.expires_at, "dt")) if dep.expires_at else None,
            "",
            quote("Нажмите на адрес — он скопируется. Зачислим автоматически после подтверждения сети, придёт "
                  "уведомление."),
        ] if x is not None)
        rows = [btn("Скопировать адрес", icon="key", copy=dep.address, style="primary")]
    else:
        text = "\n".join([
            title(pe("plus"), f"Счёт #{dep.id} · xRocket"),
            quote(f"К оплате: <b>{money.usdt(dep.amount)} USDT</b>",
                  f"Комиссия {fee_pct()}: −{money.usdt(dep.amount - dep.credit)} USDT",
                  f"Зачислим: <b>{money.usdt(dep.credit)} USDT</b>"),
            f"Оплатите до {at(dep.created_at + timedelta(hours=1))}. Проверка — автоматически раз в минуту.",
        ])
        rows = [btn("Оплатить в xRocket", icon="wallet", url=dep.link, style="success") if dep.link else None]
    await show(bot, user, text + note, kb(*rows, btn("Проверить оплату", f"w:chk:{dep.id}", "refresh"),
                                         back("w", "Кошелёк")), src)


async def check_deposit(s: AsyncSession, dep: Deposit) -> str:
    """Poll xRocket and credit once, minus deposit_fee. Returns the current status."""
    inv = (await xrocket.rocket.get_invoice(dep.invoice_id) if dep.invoice_id
           else await xrocket.rocket.get_invoice_by_client(f"dep-{dep.id}"))
    if not dep.invoice_id:
        dep.invoice_id = str(inv["id"])
        dep.link = xrocket.XRocket.link(inv)
        dep.status = "active"
        await s.flush()
    st = inv.get("status")
    if st not in ("paid", "expired", "cancelled"):  # partially_paid and unknown statuses: keep waiting
        return "active"
    payments = await xrocket.rocket.get_invoice_payments(dep.invoice_id)
    if any(p.get("status") == "pending" for p in payments):
        return "active"  # a payment is still settling: crediting now would lose it
    received = sum((Decimal(p["receiveAmount"]) for p in payments
                    if p.get("status") == "paid" and p.get("receiveCurrency") == "USDT"), Decimal(0))
    received = received.quantize(money.Q, ROUND_DOWN)
    if received <= 0:
        if st == "paid":
            return "active"
        res = await s.execute(update(Deposit).where(Deposit.id == dep.id, Deposit.status == "active")
                              .values(status="expired"))
        if res.rowcount == 1:
            events.add(s, f"dep:{dep.id}", "expired", f"Счёт {st} без оплаты", dep.user_id)
        await s.commit()
        return "expired"
    if dep.purpose == "debt":
        return await _repaid(s, dep, received, st)
    credit = credit_of(received)
    expected = dep.amount
    res = await s.execute(update(Deposit).where(Deposit.id == dep.id, Deposit.status == "active")
                          .values(status="paid", credit=credit, amount=received))
    if res.rowcount != 1:
        return "paid"
    await s.refresh(dep)
    await money.add(s, dep.user_id, credit, "deposit", f"dep:{dep.id}")
    if received > credit:
        money.platform(s, received - credit, "deposit_fee", f"dep:{dep.id}")
    partial = st != "paid" or (expected and received < expected)
    events.add(s, f"dep:{dep.id}", "credited",
               f"Зачислено {money.usdt(credit)} USDT (пришло {money.usdt(received)}, комиссия "
               f"{money.usdt(received - credit)})" + (f" · счёт {st}, ожидалось {money.usdt(expected)}" if partial else ""),
               dep.user_id, alert=bool(partial), notice=True)
    await s.commit()
    return "credited"


async def _repaid(s: AsyncSession, dep: Deposit, received: Decimal, st: str) -> str:
    """An operator's invoice for his debt is paid: no fee, the debt goes down; anything above it goes to his balance."""
    res = await s.execute(update(Deposit).where(Deposit.id == dep.id, Deposit.status == "active")
                          .values(status="paid", credit=received, amount=received))
    if res.rowcount != 1:
        return "paid"
    await s.refresh(dep)
    paid, extra = await operators.repay(s, dep.user_id, received, f"счёт xRocket #{dep.id}")
    if extra > 0:
        await money.add(s, dep.user_id, extra, "deposit", f"dep:{dep.id}")
    events.add(s, f"dep:{dep.id}", "credited", f"Погашение долга оператора: пришло {money.usdt(received)} USDT, "
               f"погашено {money.usdt(paid)}" + (f", {money.usdt(extra)} сверх долга — на баланс" if extra else "")
               + (f" · счёт {st}" if st != "paid" else ""), dep.user_id, notice=True)
    await s.commit()
    return "credited"


async def deposit_done_text(s: AsyncSession, dep: Deposit) -> str:
    if dep.purpose != "debt":
        return f"{pe('ok')} <b>Баланс пополнен на {money.usdt(dep.credit)} USDT</b> · пополнение #{dep.id}"
    op = await s.get(Operator, dep.user_id, populate_existing=True)
    return (f"{pe('ok')} <b>Долг погашен: {money.usdt(dep.credit)} USDT</b> · счёт #{dep.id}\n"
            f"• Осталось долга: <b>{money.usdt(op.debt if op else Decimal(0))} USDT</b>")


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
    if dep.purpose == "debt" and st in ("credited", "paid", "active"):
        from bot.handlers.operator import operator_screen
        await s.refresh(user)
        return await operator_screen(bot, s, user, c, ok(f"Погашено {money.usdt(dep.credit)} USDT")
                                     if st != "active" else warn("Оплата ещё не поступила — проверим автоматически."))
    if st in ("credited", "paid"):
        await s.refresh(user)
        return await wallet_screen(bot, s, user, c, ok(f"Зачислено {money.usdt(dep.credit)} USDT"))
    if st == "expired":
        return await wallet_screen(bot, s, user, c, warn(f"Пополнение #{dep.id} истекло без оплаты. Создайте новое."))
    if st == "failed":
        return await wallet_screen(bot, s, user, c, warn(f"Пополнение #{dep.id} не создано. Создайте новое."))
    await c.answer("Оплата ещё не поступила. Если отправили — зачислим автоматически.", show_alert=True)


# ---------- withdraw: cheque or address in a network ----------

@router.callback_query(F.data == "w:out")
async def cb_withdraw_choice(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    await state.set_state(None)
    nets = await xrocket.networks()
    await show(bot, user, "\n".join(x for x in [
        title(pe("up"), "Вывод USDT"),
        "",
        field("Можно вывести", f"<b>{money.usdt(money.withdrawable(user))} USDT</b>"
              + (f" из {money.usdt(user.balance)}" if money.withdrawable(user) < user.balance else "")),
        field("Можно вывести", f"<b>{money.usdt(money.withdrawable(user))} USDT</b> · ещё прокрутить "
              f"{money.usdt(user.balance - money.withdrawable(user))} USDT в сделках")
        if money.withdrawable(user) < user.balance else None,
        field("Чек xRocket", f"мгновенно · {withdraw_terms('xrocket')}"),
        field("На кошелёк", f"любая сеть ниже · {withdraw_terms('chain')}"),
        "",
        quote("Не хватает USDT у сервиса — вывод встанет в очередь и уйдёт сам; место видно в «Кошельке»."),
    ] if x is not None), kb(btn("Чеком xRocket", "w:wd", "dollar", style="success"), *_net_rows("w:wn", nets), back("w", "Кошелёк")), c)


@router.callback_query(F.data == "w:wd")
async def cb_withdraw(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    await state.set_state(W.withdraw)
    await show(bot, user, withdraw_prompt(user), kb(back("w:out", "Назад")), c)


def withdraw_prompt(user: User, err: str = "") -> str:
    return "\n".join([
        title(pe("up"), "Вывод чеком xRocket"),
        quote(f"• Можно вывести: <b>{money.usdt(money.withdrawable(user))} USDT</b>"
              + (f" из {money.usdt(user.balance)}" if money.withdrawable(user) < user.balance else ""),
              f"• Минимум: {settings.get('withdraw_min')} USDT",
              f"• Комиссия: {withdraw_terms('xrocket')}"),
        quote(lock_note(user)) if lock_note(user) else "",
        "Отправьте сумму списания в USDT — чек придёт на сумму минус комиссия. Активирует его только ваш аккаунт.",
    ]) + (warn(err) if err else "")


@router.message(W.withdraw, F.text)
async def msg_withdraw(m: Message, bot: Bot, user: User, state: FSMContext):
    v = parse_usdt(m.text)
    fee = withdraw_fee(v, "xrocket") if v is not None else Decimal(0)
    err = ("Введите сумму числом" if v is None
           else f"Минимум {settings.get('withdraw_min')} USDT" if v < settings.dec("withdraw_min")
           else "Сумма должна быть больше комиссии" if v <= fee
           else f"Доступно только {money.usdt(user.balance)} USDT" if v > user.balance
           else f"Вывести можно только {money.usdt(money.withdrawable(user))} USDT — остальное пополнение ещё не "
                "прокручено в сделках" if v > money.withdrawable(user) else "")
    if err:
        return await show(bot, user, withdraw_prompt(user, err), kb(back("w:out", "Назад")))
    await state.set_state(None)
    await state.update_data(wd_amount=str(v), wd_fee=str(fee), wd_request=str(uuid4()))
    await show(bot, user, "\n".join([
        title(pe("up"), "Подтвердите вывод"),
        quote(f"• Спишется: <b>{money.usdt(v)} USDT</b>",
              f"• Комиссия {withdraw_terms('xrocket')}: −{money.usdt(fee)} USDT",
              f"• Сумма чека: <b>{money.usdt(v - fee)} USDT</b>"),
    ]), kb(btn("Получить чек", "w:go", "ok", style="success"), back("w", "Отмена")))


@router.callback_query(F.data == "w:go")
async def cb_withdraw_go(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    data = await state.get_data()
    raw, request_id = data.get("wd_amount"), data.get("wd_request")
    await state.update_data(wd_amount=None, wd_fee=None, wd_request=None)
    if not raw or not request_id:
        return await c.answer("Заявка уже обработана или устарела. Начните вывод заново.", show_alert=True)
    amount = Decimal(raw)
    fee = withdraw_fee(amount, "xrocket")
    if data.get("wd_fee") and Decimal(data["wd_fee"]) != fee:
        return await c.answer("Комиссия изменилась — начните вывод заново", show_alert=True)
    if amount < settings.dec("withdraw_min") or amount <= fee:
        return await c.answer("Некорректная сумма", show_alert=True)
    wd = Withdrawal(user_id=user.id, amount=amount, fee=fee, request_id=request_id)
    await _submit(bot, s, user, wd, c, f"Запрос вывода чеком: списано {money.usdt(amount)} USDT, чек {money.usdt(amount - fee)}")


def lock_note(user: User) -> str:
    """Why part of the balance cannot be withdrawn yet ("" if all of it can)."""
    free = money.withdrawable(user)
    if free >= user.balance:
        return ""
    return (f"Вывести можно {money.usdt(free)} USDT. Ещё {money.usdt(user.balance - free)} USDT — пополнение, которое "
            "нужно прокрутить: продайте эти USDT покупателям в сделках, и они станут доступны к выводу.")


async def _submit(bot: Bot, s: AsyncSession, user: User, wd: Withdrawal, c: CallbackQuery, what: str):
    """Debit, commit (a crash after this leaves "pending" for the sync tasks), then pay or queue."""
    u = await money.lock(s, user.id)  # the final word, under the row lock: only what was turned over leaves
    if wd.amount > money.withdrawable(u):
        return await c.answer(f"Вывести можно только {money.usdt(money.withdrawable(u))} USDT: пополнение "
                              "сначала нужно прокрутить в сделках", show_alert=True)
    s.add(wd)
    try:
        await s.flush()
        await money.add(s, user.id, -wd.amount, "withdraw", f"wd:{wd.id}")
        events.add(s, f"wd:{wd.id}", "request", what, user.id, notice=True)
        await s.commit()
    except (money.NotEnough, IntegrityError) as e:
        await s.rollback()
        await s.refresh(user)  # rollback expires every loaded object
        return await c.answer("Недостаточно средств" if isinstance(e, money.NotEnough) else "Заявка уже обработана",
                              show_alert=True)
    await c.answer("Отправляем…")
    result = await pay_or_queue(s, wd)
    await s.commit()
    await s.refresh(user)
    await wallet_screen(bot, s, user, c, payout_note(wd, result))
    if result == "done":
        await notify_withdrawal(bot, wd, "done")


ADDRESS = {"TON": r"[A-Za-z0-9_-]{48}|-?[01]:[0-9a-fA-F]{64}", "TRX": r"T[1-9A-HJ-NP-Za-km-z]{33}",
           "ETH": r"0x[0-9a-fA-F]{40}", "BSC": r"0x[0-9a-fA-F]{40}", "SOL": r"[1-9A-HJ-NP-Za-km-z]{32,44}",
           "BTC": r"bc1[0-9a-z]{25,62}|[13][1-9A-HJ-NP-Za-km-z]{25,34}"}


def parse_address(net: str, raw: str | None) -> str | None:
    """Shape check only: xRocket validates the address itself and refuses a wrong one (the user is refunded)."""
    v = (raw or "").strip()
    return v if re.fullmatch(ADDRESS.get(net, r"[A-Za-z0-9:_-]{20,128}"), v) else None


def _chain_step(net: str, n: int, text: str, err: str = "") -> str:
    return (f"{title(pe('wallet'), f'Вывод · {xrocket.net_name(net)}')} · шаг {n} из 3\n{text}"
            + (warn(err) if err else ""))


@router.callback_query(F.data.regexp(r"^w:wn:([A-Z]{2,5})$"))
async def cb_chain(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    net = c.data.split(":")[2]
    if net not in await xrocket.networks():
        return await c.answer("Сеть сейчас недоступна", show_alert=True)
    await state.set_state(W.address)
    await state.update_data(t_net=net, t_addr=None, t_memo=None)
    last = await s.scalar(select(Withdrawal).where(Withdrawal.user_id == user.id, Withdrawal.method == "chain",
                                                   Withdrawal.network == net).order_by(Withdrawal.id.desc()).limit(1))
    await show(bot, user, _chain_step(net, 1, f"Отправьте адрес кошелька <b>USDT в сети {xrocket.net_name(net)}</b>.\n"
                                                f"{pe('warn')} Адрес другой сети — деньги не вернуть."), kb(
        btn(f"Как в прошлый раз: {last.address[:6]}…{last.address[-4:]}", "w:wn:last", "refresh") if last else None,
        back("w:out", "Отмена")), c)


async def _check_address(s: AsyncSession, net: str, raw: str | None) -> tuple[str | None, str]:
    addr = parse_address(net, raw)
    if not addr:
        return None, f"Это не адрес сети {xrocket.net_name(net)}. Скопируйте его целиком"
    if await s.scalar(select(exists().where(Deposit.address == addr))):
        return None, "Это адрес пополнения Strait Pay, а нужен ваш собственный кошелёк"
    return addr, ""


async def _after_address(bot, user, state: FSMContext, addr: str, src=None):
    net = (await state.get_data())["t_net"]
    await state.update_data(t_addr=addr)
    if net != "TON":  # a comment (memo) exists only in TON
        return await _ask_amount(bot, user, state, None, src)
    await state.set_state(W.memo)
    await show(bot, user, _chain_step(net, 2, f"Адрес: <code>{esc(addr)}</code>\n\nВыводите на <b>биржу</b>? Отправьте "
                                              "memo из её раздела пополнения. Личному кошельку memo не нужен."),
               kb(btn("Без комментария", "w:wn:nomemo", "ok", style="primary"), back("w:out", "Отмена")), src)


@router.message(W.address, F.text)
async def msg_chain_address(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    net = (await state.get_data()).get("t_net", "TON")
    addr, err = await _check_address(s, net, m.text)
    if not addr:
        return await show(bot, user, _chain_step(net, 1, "Отправьте адрес кошелька ещё раз.", err),
                          kb(back("w:out", "Отмена")))
    await _after_address(bot, user, state, addr)


@router.callback_query(W.address, F.data == "w:wn:last")
async def cb_chain_last(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    net = (await state.get_data()).get("t_net")
    last = await s.scalar(select(Withdrawal).where(Withdrawal.user_id == user.id, Withdrawal.method == "chain",
                                                   Withdrawal.network == net).order_by(Withdrawal.id.desc()).limit(1))
    if not last:
        return await c.answer("Прошлых выводов в этой сети нет", show_alert=True)
    await _after_address(bot, user, state, last.address, c)


@router.callback_query(W.memo, F.data == "w:wn:nomemo")
async def cb_chain_nomemo(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    await _ask_amount(bot, user, state, None, c)


@router.message(W.memo, F.text)
async def msg_chain_memo(m: Message, bot: Bot, user: User, state: FSMContext):
    memo = m.text.strip()
    if not 1 <= len(memo) <= 120 or not memo.isprintable():
        return await _after_address(bot, user, state, (await state.get_data())["t_addr"])
    await _ask_amount(bot, user, state, memo)


async def chain_quota(net: str) -> tuple[Decimal, Decimal]:
    """(xRocket's network fee, xRocket's minimum) for a withdrawal in `net`."""
    q = await xrocket.quota(net)
    return xrocket.net_fee(q), Decimal(str(q["withdrawMinSize"])) if q else Decimal(0)


def _amount_text(user: User, data: dict, nf: Decimal, err: str = "") -> str:
    return _chain_step(data["t_net"], 3, "\n".join([
        quote(f"• Адрес: <code>{esc(data['t_addr'])}</code>",
              f"• Memo: <code>{esc(data['t_memo'])}</code>" if data.get("t_memo") else "",
              f"• Можно вывести: <b>{money.usdt(money.withdrawable(user))} USDT</b>",
              f"• Комиссия: <b>{withdraw_terms('chain', nf)}</b> · минимум {settings.get('chain_withdraw_min')} USDT",
              f"• {lock_note(user)}" if lock_note(user) else ""),
        "Отправьте сумму списания — придёт сумма минус комиссия."]), err)


async def _ask_amount(bot, user, state: FSMContext, memo: str | None, src=None):
    await state.update_data(t_memo=memo)
    await state.set_state(W.amount)
    data = await state.get_data()
    nf, _ = await chain_quota(data["t_net"])
    await show(bot, user, _amount_text(user, data, nf), kb(
        btn(f"Вывести всё: {money.usdt(money.withdrawable(user))} USDT", "w:wn:all", "up")
        if money.withdrawable(user) > withdraw_fee(money.withdrawable(user), "chain", nf) else None,
        back("w:out", "Отмена")), src)


async def _chain_confirm(bot, user, state: FSMContext, v: Decimal | None, src=None):
    data = await state.get_data()
    nf, xmin = await chain_quota(data["t_net"])
    fee = withdraw_fee(v, "chain", nf) if v is not None else Decimal(0)
    err = ("Введите сумму числом" if v is None
           else f"Минимум {settings.get('chain_withdraw_min')} USDT" if v < settings.dec("chain_withdraw_min")
           else "Сумма должна быть больше комиссии" if v <= fee
           else f"Доступно только {money.usdt(user.balance)} USDT" if v > user.balance
           else f"Вывести можно только {money.usdt(money.withdrawable(user))} USDT — остальное пополнение ещё не "
                "прокручено в сделках" if v > money.withdrawable(user)
           else f"После комиссии должно остаться не меньше {money.usdt(xmin)} USDT" if v - fee < xmin else "")
    if err:
        return await show(bot, user, _amount_text(user, data, nf, err), kb(back("w:out", "Отмена")), src)
    await state.set_state(None)
    await state.update_data(t_amount=str(v), t_fee=str(fee), t_netfee=str(nf), t_request=str(uuid4()))
    await show(bot, user, "\n".join([
        title(pe("up"), "Проверьте вывод"),
        quote(f"• Сеть: <b>{xrocket.net_name(data['t_net'])}</b>",
              f"• Адрес: <code>{esc(data['t_addr'])}</code>",
              f"• Memo: <code>{esc(data['t_memo'])}</code>" if data.get("t_memo") else "",
              f"• Спишется: <b>{money.usdt(v)} USDT</b>",
              f"• Комиссия {withdraw_terms('chain', nf)}: −{money.usdt(fee)} USDT",
              f"• Придёт: <b>{money.usdt(v - fee)} USDT</b>"),
        f"{pe('warn')} Перевод в блокчейне не отменить. Сверьте начало и конец адреса.",
    ]), kb(btn("Отправить", "w:wn:go", "ok", style="success"), back("w:out", "Отмена")), src)


@router.message(W.amount, F.text)
async def msg_chain_amount(m: Message, bot: Bot, user: User, state: FSMContext):
    await _chain_confirm(bot, user, state, parse_usdt(m.text))


@router.callback_query(W.amount, F.data == "w:wn:all")
async def cb_chain_all(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    await _chain_confirm(bot, user, state, money.withdrawable(user), c)


@router.callback_query(F.data == "w:wn:go")
async def cb_chain_go(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    data = await state.get_data()
    await state.update_data(t_amount=None, t_request=None)
    if not data.get("t_amount") or not data.get("t_request"):
        return await c.answer("Эта заявка уже обработана или устарела. Начните вывод заново.", show_alert=True)
    amount, fee = Decimal(data["t_amount"]), Decimal(data["t_fee"])
    if amount <= fee or amount < settings.dec("chain_withdraw_min"):
        return await c.answer("Условия вывода изменились — начните заново", show_alert=True)
    wd = Withdrawal(user_id=user.id, amount=amount, fee=fee, net_fee=Decimal(data["t_netfee"]),
                    request_id=data["t_request"], method="chain", network=data["t_net"], address=data["t_addr"],
                    memo=data.get("t_memo"))
    await _submit(bot, s, user, wd, c, f"Запрос вывода в сети {xrocket.net_name(wd.network)}: списано {money.usdt(amount)} "
                                       f"USDT, к отправке {money.usdt(amount - fee)} на {wd.address}"
                                       + (f", memo {wd.memo}" if wd.memo else ""))


# ---------- payout queue: when the xRocket app balance is short, withdrawals wait and go out in order ----------

ACTIVE = ("pending", "unknown", "sent")  # debited and on its way (not queued, not final)


def payout_note(wd: Withdrawal, result: str) -> str:
    net = money.usdt(wd.amount - wd.fee)
    return {
        "done": ok(f"Чек на {net} USDT отправлен отдельным сообщением." if wd.method == "xrocket"
                   else f"Вывод #{wd.id} выполнен: {net} USDT отправлены."),
        "sent": ok(f"Вывод #{wd.id} принят: {net} USDT уйдут в течение нескольких минут."),
        "queued": ok(f"Вывод #{wd.id} в очереди: {net} USDT отправим автоматически, обычно в течение часа. "
                     "До отправки его можно отменить."),
        "unknown": warn(f"Вывод #{wd.id} на проверке: xRocket не ответил. Сумма удержана — сверим автоматически."),
    }.get(result) or warn(f"Вывод не выполнен: {result}. Средства возвращены на баланс.")


async def payout_need(wd: Withdrawal) -> Decimal:
    """USDT the xRocket app balance must have for this withdrawal (plus xRocket's network fee)."""
    return wd.amount - wd.fee + (wd.net_fee or 0)


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
    """Ask xRocket to pay this withdrawal. done | sent | unknown | queued | <reason> (refunded). Does not commit."""
    if wd.method == "chain":
        return await send_withdrawal(s, wd)
    try:
        ch = await xrocket.rocket.create_cheque(wd.amount - wd.fee, f"wd-{wd.id}", wd.user_id,
                                                "Вывод с баланса Strait Pay")
    except xrocket.XRocketError as e:
        return await _failed(s, wd, e, "clientChequeId")
    await finish_withdrawal(s, wd, ch)
    return "done"


async def _failed(s: AsyncSession, wd: Withdrawal, e: xrocket.XRocketError, key: str) -> str:
    wd.error = str(e)[:1000]
    if e.code == "amount_more_than_app_balance":  # the app ran short meanwhile: wait instead of refusing
        return await queue(s, wd)
    if e.uncertain:  # outcome unknown: keep funds reserved; the poller reconciles by the client id
        wd.status = "unknown"
        events.add(s, f"wd:{wd.id}", "unknown", f"Ответ xRocket неизвестен ({e.code}): деньги удержаны, нужна "
                   f"сверка по {key}", wd.user_id, alert=True)
        return "unknown"
    await _refund(s, wd, f"Отказ xRocket ({e.code}): {e.human}")
    return e.human


async def _refund(s: AsyncSession, wd: Withdrawal, why: str) -> None:
    wd.status = "failed"
    await money.add(s, wd.user_id, wd.amount, "withdraw_refund", f"wd:{wd.id}")
    events.add(s, f"wd:{wd.id}", "failed", f"{why}. {money.usdt(wd.amount)} USDT возвращены пользователю",
               wd.user_id, alert=True)


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


async def finish_withdrawal(s: AsyncSession, wd: Withdrawal, cheque: dict) -> None:
    wd.cheque_id, wd.link, wd.status = str(cheque["chequeId"]), xrocket.XRocket.link(cheque), "done"
    events.add(s, f"wd:{wd.id}", "cheque", f"Чек {wd.cheque_id} выдан на {money.usdt(wd.amount - wd.fee)} USDT",
               wd.user_id, notice=True)
    if wd.fee:
        money.platform(s, wd.fee, "withdraw_fee", f"wd:{wd.id}")


async def notify_withdrawal(bot: Bot, wd: Withdrawal, result: str) -> None:
    net = money.usdt(wd.amount - wd.fee)
    if result == "done" and wd.method == "xrocket":
        await notify(bot, wd.user_id, "\n".join([
            title(pe("dollar"), f"Чек на {net} USDT · вывод #{wd.id}"),
            "Нажмите «Активировать чек» — USDT придут в @xRocket. Ссылка есть и в истории операций.",
        ]), kb(btn("Активировать чек", icon="wallet", url=wd.link, style="success") if wd.link else None,
               back("x", "Скрыть", "cross")))
    elif result == "done":
        await notify(bot, wd.user_id, f"{pe('ok')} <b>Вывод #{wd.id} выполнен:</b> {net} USDT · "
                                      f"{xrocket.net_name(wd.network)} · <code>{esc(wd.address or '')}</code>",
                     kb(btn("Транзакция", icon="search", url=wd.link) if wd.link else None, back("x", "Скрыть", "cross")))
    elif result == "sent":
        await notify(bot, wd.user_id, f"{pe('ok')} Вывод #{wd.id} отправлен: {net} USDT уже в пути.")
    elif result == "refunded":
        await notify(bot, wd.user_id, f"{pe('warn')} Вывод #{wd.id} не выполнен. {money.usdt(wd.amount)} USDT "
                                      "возвращены на баланс.")


async def send_cheque(bot: Bot, wd: Withdrawal) -> None:
    await notify_withdrawal(bot, wd, "done")


async def reconcile(s: AsyncSession, wid: int) -> tuple[str, Withdrawal | None]:
    """Sync a cheque withdrawal with xRocket by clientChequeId (read-only API call, safe to repeat).

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


# ---------- xRocket side of a withdrawal to an address ----------

def _apply(s: AsyncSession, wd: Withdrawal, r: dict) -> str:
    """Apply xRocket's answer: done | sent | refund. Final states are never changed again."""
    ref = f"wd:{wd.id}"
    wd.tx_hash = (r.get("txHash") or wd.tx_hash or "")[:128] or None
    wd.link = r.get("txLink") or wd.link
    st = r.get("status")
    if st == "COMPLETED":
        wd.status = "done"
        if wd.fee > wd.net_fee:
            money.platform(s, wd.fee - wd.net_fee, "withdraw_fee", ref)
        events.add(s, ref, "done", f"Вывод выполнен xRocket: {money.usdt(wd.amount - wd.fee)} USDT · "
                   f"{xrocket.net_name(wd.network)} · {wd.address}" + (f" · tx {wd.tx_hash}" if wd.tx_hash else ""),
                   wd.user_id, notice=True)
        return "done"
    if st == "FAIL":
        wd.status, wd.error = "failed", "xRocket: FAIL"
        events.add(s, ref, "failed", f"xRocket отклонил вывод: {money.usdt(wd.amount)} USDT возвращены пользователю",
                   wd.user_id, alert=True)
        return "refund"
    if wd.status != "sent":
        wd.status, wd.sent_at = "sent", now()
        events.add(s, ref, "sent", "xRocket принял вывод, ждём транзакцию в сети", wd.user_id)
    return "sent"


async def send_withdrawal(s: AsyncSession, wd: Withdrawal) -> str:
    """Ask xRocket to pay. done | sent | unknown | queued | <human reason> (refunded). Does not commit.
    Safe to call again for the same withdrawal: clientWithdrawalId = wd-<id> is executed once."""
    try:
        r = await xrocket.rocket.create_withdrawal(f"wd-{wd.id}", wd.network or "TON", wd.address,
                                                   wd.amount - wd.fee, wd.memo)
    except xrocket.XRocketError as e:
        return await _failed(s, wd, e, "clientWithdrawalId")
    result = _apply(s, wd, r)
    if result == "refund":
        await money.add(s, wd.user_id, wd.amount, "withdraw_refund", f"wd:{wd.id}")
        return "xRocket отклонил перевод"
    return result


async def sync_withdrawal(s: AsyncSession, wd: Withdrawal) -> str:
    """Check a withdrawal to an address in xRocket. done | sent | refunded | unknown | retried. Does not commit."""
    if wd.status == "pending":  # debited, but the process stopped before asking xRocket
        return await send_withdrawal(s, wd)
    try:
        r = await xrocket.rocket.get_withdrawal(f"wd-{wd.id}")
    except xrocket.XRocketError as e:
        if e.code == "app_withdrawal_not_found" and wd.status == "unknown":
            await send_withdrawal(s, wd)  # never reached xRocket: the same id cannot be paid twice
            return "retried"
        await events.alert_once(s, f"wd:{wd.id}", "check_error", f"Ошибка сверки вывода: {e}"[:300], wd.user_id)
        return "unknown"
    result = _apply(s, wd, r)
    if result == "refund":
        await money.add(s, wd.user_id, wd.amount, "withdraw_refund", f"wd:{wd.id}")
        return "refunded"
    return result
