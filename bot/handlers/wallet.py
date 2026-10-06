"""Wallet: USDT BEP-20 (BNB Smart Chain) only. Deposits — the user's permanent address (services/bsc.py credits every
Transfer once); withdrawals — to any wallet or exchange in the BSC network, paid by the bot's hot wallet in order: at
once while it has the USDT, otherwise from the queue as soon as it gets them. A permanent wallet with auto-withdrawal."""
import logging
from decimal import ROUND_DOWN, Decimal
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.emoji import back, btn, kb, pe
from bot.models import BscAuto, Deposit, Ledger, Operator, User, Withdrawal
from bot.services import bsc, events, money
from bot.ui import app_btn, at, esc, field, notify, ok, quote, section, show, title, warn

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
WD_STATUS = {"queued": "в очереди", "pending": "отправляется", "sending": "отправляется", "sent": "в сети",
             "unknown": "проверяется", "done": "выполнен", "failed": "не выполнен, возвращён",
             "cancelled": "отменён, возвращён"}
MOVING = ("queued", "pending", "sending", "sent", "unknown")  # debited, not paid out yet
OFF = "Кошелёк временно недоступен — попробуйте позже"


def ref_label(ref: str) -> str:
    kind, _, num = (ref or "").partition(":")
    return {"deal": f"по сделке #{num}", "wd": f"#{num}", "dep": f"#{num}", "adj": f"#{num}",
            "tdep": f"#{num}"}.get(kind, "")


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


def withdraw_terms() -> str:
    """The withdrawal fee in words: «1 USDT»."""
    return f"{money.usdt(bsc.fee())} USDT"


async def queue_place(s: AsyncSession, wd: Withdrawal) -> tuple[int, Decimal]:
    """(withdrawals ahead, USDT ahead) of a queued withdrawal."""
    n, total = (await s.execute(select(func.count(Withdrawal.id), func.coalesce(
        func.sum(Withdrawal.amount - Withdrawal.fee), 0)).where(Withdrawal.method == wd.method, Withdrawal.status == "queued",
                                                               Withdrawal.id < wd.id))).one()
    return n, Decimal(total)


async def wallet_screen(bot: Bot, s: AsyncSession, user: User, src=None, note: str = ""):
    moving = (await s.scalars(select(Withdrawal).where(Withdrawal.user_id == user.id, Withdrawal.status.in_(MOVING))
                              .order_by(Withdrawal.id))).all()
    op = await s.get(Operator, user.id)
    free = money.withdrawable(user)
    lines = [
        title(pe("wallet"), "Кошелёк"),
        "",
        field("Доступно", f"<b>{money.usdt(user.balance)} USDT</b>"),
        field("Можно вывести", f"<b>{money.usdt(free)} USDT</b> · ещё прокрутить {money.usdt(user.balance - free)} USDT "
              "в сделках") if free < user.balance else None,
        field("В сделках", f"{money.usdt(user.frozen)} USDT") if user.frozen else None,
        field("Командный баланс", f"{money.usdt(user.team_balance)} USDT — перевести в «Команда»")
        if user.team_balance else None,
        field("Долг оператора", f"<b>{money.usdt(op.debt)} USDT</b> — погасить в «Оператор»") if op and op.debt else None,
    ]
    if moving:
        lines += ["", section("clock", "Выводы в пути")]
        for w in moving:
            where = f"{money.usdt(w.amount - w.fee)} USDT → {esc(bsc.short(w.address))} · {WD_STATUS.get(w.status, w.status)}"
            if w.status == "queued":
                ahead_n, ahead = await queue_place(s, w)
                where += f", место {ahead_n + 1}" + (f", перед вами {money.usdt(ahead)} USDT" if ahead_n else "")
            lines.append(field(f"#{w.id}", where))
        lines.append(quote("Отправляем сами по очереди, обычно за 1–2 минуты. Если у сервиса не хватает USDT, вывод "
                           "ждёт и уходит автоматически. Пока вывод не отправлялся, его можно отменить."))
    lines += ["", quote(f"USDT в сети BEP-20 (BSC) · пополнение без комиссии · вывод — комиссия {withdraw_terms()}")]
    await show(bot, user, "\n".join(x for x in lines if x is not None) + note, kb(
        [btn("Пополнить", "w:in", "plus", style="success"), btn("Вывести", "w:out", "up", style="danger")],
        *[btn(f"Отменить вывод #{w.id}", f"w:qc:{w.id}") for w in moving if w.status == "queued" and not w.transfer_id],
        [btn("Автовывод", "w:auto", "refresh"), btn("История операций", inline="операции ")],
        app_btn("Кошелёк в приложении", "", "wallet", style="primary", wide=True),
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
    link = None
    if kind == "dep" and num.isdigit() and (dep := await s.get(Deposit, int(num))) and dep.user_id == user.id:
        link = dep.link
    elif kind == "wd" and num.isdigit() and (wd := await s.get(Withdrawal, int(num))) and wd.user_id == user.id:
        link = wd.link
    await show(bot, user, "\n".join([
        title(pe("list"), f"{KINDS.get(r.kind, r.kind)} {ref_label(r.ref)}".strip()),
        f"{at(r.created_at, 'dt')}",
        quote(f"Доступный баланс: <b>{signed(avail)} USDT</b>" if avail else "",
              f"Заморозка: <b>{signed(r.frozen_delta)} USDT</b>" if r.frozen_delta else "",
              f"Причина: {esc(r.note)}" if r.note else ""),
        f"Сейчас доступно: <b>{money.usdt(user.balance)} USDT</b>",
    ]), kb(btn(f"Сделка #{num}", f"dl:{num}", style="primary") if kind == "deal" else None,
           btn("Транзакция", icon="search", url=link) if link else None,
           btn("История операций", inline="операции "), back("w", "Кошелёк")), src)


@router.callback_query(F.data == "w:h")
async def cb_history(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    rows = (await s.scalars(select(Ledger).where(Ledger.user_id == user.id).order_by(Ledger.id.desc()).limit(15))).all()
    wds = (await s.scalars(select(Withdrawal).where(Withdrawal.user_id == user.id)
                           .order_by(Withdrawal.id.desc()).limit(5))).all()
    wl = [f"#{w.id} · {money.usdt(w.amount)} USDT · {esc(bsc.short(w.address)) if w.address else 'чек'} · "
          f"{WD_STATUS.get(w.status, w.status)}" for w in wds]
    await show(bot, user, "\n".join([
        title(pe("list"), "История операций"),
        "Новые сверху.",
        quote(*[ledger_line(r) for r in rows]) if rows else "Операций пока нет.",
        *(["<b>Выводы</b>", quote(*wl)] if wl else []),
    ]), kb(*[btn(f"Вывод #{w.id} · транзакция", icon="search", url=w.link) for w in wds
             if w.link],
           back("w", "Кошелёк")), c)


# ---------- deposit ----------

@router.callback_query(F.data.in_({"w:in", "w:in:bsc"}))
async def cb_deposit(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.set_state(None)
    await bsc_deposit_screen(bot, s, user, c)


async def deposit_done_text(s: AsyncSession, dep: Deposit) -> str:
    return (f"{pe('ok')} <b>Баланс пополнен на {money.usdt(dep.credit)} USDT</b> · BEP-20 · поступление #{dep.id}\n"
            f"• Транзакция: <code>{esc(dep.link.rsplit('/', 1)[-1] if dep.link else '')}</code>")


async def notify_deposit(bot: Bot, s: AsyncSession, dep: Deposit) -> None:
    await notify(bot, dep.user_id, await deposit_done_text(s, dep), kb(
        btn("Транзакция в BscScan", icon="search", url=dep.link) if dep.link else None,
        [btn("Кошелёк", "w", "wallet"), app_btn("В приложении", "history")],
        back("x", "Скрыть", "cross")))


# ---------- withdraw ----------

def lock_note(user: User) -> str:
    """Why part of the balance cannot be withdrawn yet ("" if all of it can)."""
    free = money.withdrawable(user)
    if free >= user.balance:
        return ""
    return (f"Вывести можно {money.usdt(free)} USDT. Ещё {money.usdt(user.balance - free)} USDT — пополнение, которое "
            "нужно прокрутить: продайте эти USDT покупателям в сделках, и они станут доступны к выводу.")


@router.callback_query(F.data.in_({"w:out", "w:out:bsc"}))
async def cb_withdraw(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await bsc_withdraw(bot, s, user, state, c)


def accepted(wd: Withdrawal) -> str:
    return (f"Вывод #{wd.id} принят: {money.usdt(wd.amount - wd.fee)} USDT отправим автоматически, обычно за минуту. "
            "Если у сервиса не хватит USDT — вывод подождёт в очереди и уйдёт сам.")


async def cancel_queued(s: AsyncSession, user: User, wid: int, where: str) -> Withdrawal | None:
    """A queued withdrawal nothing was ever signed for goes back to the balance. None: too late. Does not commit."""
    wd = await s.get(Withdrawal, wid, with_for_update=True, populate_existing=True)
    if not wd or wd.user_id != user.id or wd.status != "queued" or wd.transfer_id:
        return None
    wd.status = "cancelled"
    await money.add(s, user.id, wd.amount, "withdraw_refund", f"wd:{wd.id}")
    events.add(s, f"wd:{wd.id}", "cancelled", f"Пользователь отменил вывод из очереди ({where}), "
               f"{money.usdt(wd.amount)} USDT возвращены", user.id, notice=True)
    return wd


@router.callback_query(F.data.regexp(r"^w:qc:(\d+)$"))
async def cb_cancel_queued(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    wd = await cancel_queued(s, user, int(c.data.split(":")[2]), "бот")
    if wd is None:
        return await c.answer("Вывод уже отправляется — отменить нельзя", show_alert=True)
    await s.commit()
    await s.refresh(user)
    await wallet_screen(bot, s, user, c, ok(f"Вывод #{wd.id} отменён, {money.usdt(wd.amount)} USDT снова на балансе."))


async def notify_withdrawal(bot: Bot, wd: Withdrawal, result: str) -> None:
    net = money.usdt(wd.amount - wd.fee)
    if result == "done":
        await notify(bot, wd.user_id, "\n".join([
            f"{pe('ok')} <b>Вывод #{wd.id} выполнен:</b> {net} USDT · BEP-20",
            f"• Сумма {money.usdt(wd.amount)} USDT, комиссия {money.usdt(wd.fee)} USDT",
            f"• Адрес: <code>{esc(wd.address or '')}</code>",
            f"• Транзакция: <code>{esc(wd.tx_hash or '')}</code>"]),
                     kb(btn("Транзакция в BscScan", icon="search", url=wd.link) if wd.link else None,
                        back("x", "Скрыть", "cross")))
    elif result == "refunded":
        await notify(bot, wd.user_id, f"{pe('warn')} Вывод #{wd.id} не выполнен. {money.usdt(wd.amount)} USDT "
                                      "возвращены на баланс.", kb([btn("Кошелёк", "w", "wallet"),
                                                                    app_btn("В приложении", "history")],
                                                                   back("x", "Скрыть", "cross")))


# ---------- screens ----------

class WB(StatesGroup):
    amount = State()
    address = State()
    auto_address = State()


THRESHOLDS = (5, 10, 25, 50, 100, 250)


async def bsc_deposit_screen(bot: Bot, s: AsyncSession, user: User, src=None):
    if not bsc.ready():
        return await wallet_screen(bot, s, user, src, warn(bsc.OFF))
    addr = await bsc.deposit_address(s, user.id)
    recent = (await s.scalars(select(Deposit).where(Deposit.user_id == user.id, Deposit.network == "BEP20")
                              .order_by(Deposit.id.desc()).limit(3))).all()
    await show(bot, user, "\n".join([
        title(pe("plus"), "Пополнение USDT · BEP-20"),
        "",
        section("key", "Ваш адрес для пополнения"),
        f"<code>{addr}</code>",
        "",
        quote("Только <b>USDT в сети BNB Smart Chain (BEP-20)</b> — другая монета или сеть не зачислится. Без "
              "комиссии, любая сумма от 0.01 USDT. На бирже выбирайте сеть «BSC (BEP20)»."),
        "Адрес постоянный и только ваш: пополняйте сколько угодно раз. Зачислим автоматически через несколько "
        "секунд после подтверждения в сети и пришлём уведомление.",
        *([""] + [section("list", "Последние поступления")] + [
            f"#{d.id} · {at(d.created_at, 'dt')} · <b>{money.usdt(d.credit)} USDT</b>" for d in recent] if recent else []),
    ]), kb(btn("Скопировать адрес", icon="key", copy=addr, style="primary"),
           [btn("Адрес в BscScan", icon="search", url=bsc.address_url(addr)), app_btn("В приложении", "deposit")],
           back("w", "Кошелёк")), src)


def withdraw_problem(user: User, v: Decimal | None) -> str:
    """Why `v` USDT cannot be withdrawn in BEP-20 ("" — it can)."""
    low = bsc.minimum()
    return ("Введите сумму числом" if v is None
            else f"Минимум {money.usdt(low)} USDT" if v < low
            else "Сумма должна быть больше комиссии" if v <= bsc.fee()
            else f"Доступно только {money.usdt(user.balance)} USDT" if v > user.balance
            else f"Вывести можно только {money.usdt(money.withdrawable(user))} USDT — остальное пополнение ещё не "
                 "прокручено в сделках" if v > money.withdrawable(user) else "")


def _bstep(n: int, text: str, err: str = "") -> str:
    return f"{title(pe('up'), 'Вывод USDT · BEP-20')} · шаг {n} из 2\n{text}" + (warn(err) if err else "")


def _bsc_terms(user: User) -> str:
    free = money.withdrawable(user)
    return quote(f"• Можно вывести: <b>{money.usdt(free)} USDT</b>" + (f" из {money.usdt(user.balance)}"
                                                                         if free < user.balance else ""),
                 f"• Комиссия: <b>{money.usdt(bsc.fee())} USDT</b> · минимум {money.usdt(bsc.minimum())} USDT")


async def bsc_withdraw(bot: Bot, s: AsyncSession, user: User, state: FSMContext, src=None, err: str = ""):
    if not bsc.ready():
        return await show(bot, user, warn(bsc.OFF), kb(back("w", "Кошелёк")), src)
    await state.set_state(WB.amount)
    await state.update_data(b_amount=None, b_addr=None, b_request=None)
    free = money.withdrawable(user).quantize(money.KOP, ROUND_DOWN)
    await show(bot, user, _bstep(1, "\n".join(x for x in [
        _bsc_terms(user), quote(lock_note(user)) if lock_note(user) else None,
        "Отправьте сумму списания — придёт сумма минус комиссия."] if x), err), kb(
        btn(f"Вывести всё: {money.usdt(free)} USDT", "wb:all", "up") if not withdraw_problem(user, free) else None,
        back("w", "Отмена")), src)


async def _bsc_amount(bot: Bot, s: AsyncSession, user: User, state: FSMContext, v: Decimal | None, src=None):
    v = v.quantize(money.KOP, ROUND_DOWN) if v is not None else None
    if err := withdraw_problem(user, v):
        return await bsc_withdraw(bot, s, user, state, src, err)
    await state.update_data(b_amount=str(v))
    await state.set_state(WB.address)
    auto = await s.get(BscAuto, user.id)
    last = auto.address if auto else await s.scalar(select(Withdrawal.address).where(
        Withdrawal.user_id == user.id, Withdrawal.method == "bsc").order_by(Withdrawal.id.desc()).limit(1))
    await show(bot, user, _bstep(2, "\n".join([
        quote(f"• Сумма: <b>{money.usdt(v)} USDT</b>", f"• Комиссия: {money.usdt(bsc.fee())} USDT",
              f"• К получению: <b>{money.usdt(bsc.net_of(v))} USDT</b>"),
        "Отправьте адрес кошелька <b>USDT в сети BNB Smart Chain (BEP-20)</b>: 0x и 40 знаков (или адрес пополнения "
        "биржи в сети BSC).",
        f"{pe('warn')} Адрес другой сети — деньги не вернуть."])),
        kb(btn(f"Как в прошлый раз: {bsc.short(last)}", "wb:last", "refresh") if last else None, back("w", "Отмена")),
        src)


@router.message(WB.amount, F.text)
async def msg_bsc_amount(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await _bsc_amount(bot, s, user, state, parse_usdt(m.text))


@router.callback_query(WB.amount, F.data == "wb:all")
async def cb_bsc_all(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await _bsc_amount(bot, s, user, state, money.withdrawable(user), c)


async def check_address(s: AsyncSession, raw_text: str | None) -> tuple[str | None, str]:
    """(checksum address, "") or (None, why)."""
    try:
        a = bsc.normalize_address(raw_text, bsc.hot)
    except ValueError as e:
        return None, str(e)
    if await bsc.is_ours(s, a):
        return None, "Это адрес Strait Pay, а нужен ваш собственный кошелёк или адрес биржи"
    return a, ""


async def _bsc_confirm(bot: Bot, user: User, state: FSMContext, addr: str, src=None):
    data = await state.get_data()
    if not data.get("b_amount"):
        return await show(bot, user, warn("Заявка устарела — начните вывод заново"), kb(back("w", "Кошелёк")), src)
    v = Decimal(data["b_amount"])
    await state.set_state(None)
    await state.update_data(b_addr=addr, b_fee=str(bsc.fee()), b_request=str(uuid4()))
    await show(bot, user, "\n".join([
        title(pe("up"), "Проверьте вывод"),
        quote("• Сеть: <b>BNB Smart Chain (BEP-20)</b>, монета <b>USDT</b>",
              f"• Адрес: <code>{addr}</code>",
              f"• Сумма: <b>{money.usdt(v)} USDT</b>",
              f"• Комиссия: −{money.usdt(bsc.fee())} USDT",
              f"• К получению: <b>{money.usdt(bsc.net_of(v))} USDT</b>"),
        f"{pe('warn')} Перевод в блокчейне не отменить. Сверьте начало и конец адреса.",
    ]), kb(btn("Отправить", "wb:go", "ok", style="success"), back("w", "Отмена")), src)


@router.message(WB.address, F.text)
async def msg_bsc_address(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    addr, err = await check_address(s, m.text)
    if not addr:
        return await show(bot, user, _bstep(2, "Отправьте адрес кошелька ещё раз.", err), kb(back("w", "Отмена")))
    await _bsc_confirm(bot, user, state, addr)


@router.callback_query(WB.address, F.data == "wb:last")
async def cb_bsc_last(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    auto = await s.get(BscAuto, user.id)
    last = auto.address if auto else await s.scalar(select(Withdrawal.address).where(
        Withdrawal.user_id == user.id, Withdrawal.method == "bsc").order_by(Withdrawal.id.desc()).limit(1))
    addr, _ = await check_address(s, last)
    if not addr:
        return await c.answer("Прошлого адреса нет — отправьте адрес сообщением", show_alert=True)
    await _bsc_confirm(bot, user, state, addr, c)


@router.callback_query(F.data == "wb:go")
async def cb_bsc_go(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    data = await state.get_data()
    await state.update_data(b_amount=None, b_request=None)
    if not data.get("b_amount") or not data.get("b_request") or not data.get("b_addr"):
        return await c.answer("Эта заявка уже обработана или устарела. Начните вывод заново.", show_alert=True)
    if Decimal(data["b_fee"]) != bsc.fee():
        return await c.answer("Комиссия изменилась — начните вывод заново", show_alert=True)
    wd, err = await bsc.queue_withdrawal(s, user.id, Decimal(data["b_amount"]), data["b_addr"], data["b_request"], "бот")
    if err:
        await s.refresh(user)
        return await c.answer(err[:190], show_alert=True)
    await c.answer("Вывод принят")
    await s.refresh(user)
    await wallet_screen(bot, s, user, c, ok(accepted(wd)))


# ---------- BEP-20: permanent wallet and auto-withdrawal ----------

async def auto_screen(bot: Bot, s: AsyncSession, user: User, src=None, note: str = ""):
    if not bsc.ready():
        return await wallet_screen(bot, s, user, src, warn(bsc.OFF))
    a = await s.get(BscAuto, user.id, populate_existing=True)
    steps = [t for t in THRESHOLDS if t >= bsc.minimum()]
    await show(bot, user, "\n".join(x for x in [
        title(pe("refresh"), "Автовывод · BEP-20"),
        "",
        field("Кошелёк", f"<code>{a.address}</code>" if a else "не сохранён"),
        field("Порог", f"<b>{money.usdt(a.threshold)} USDT</b>") if a else None,
        field("Автовывод", "<b>включён</b>" if a and a.active else "выключен"),
        "",
        quote("Сохраните свой кошелёк USDT BEP-20 и порог: как только доступный к выводу баланс дойдёт до порога, бот "
              f"сам выведет его целиком на этот кошелёк. Комиссия та же — {money.usdt(bsc.fee())} USDT. Не срабатывает, "
              "пока есть долг оператора или вывод в пути."),
    ] if x is not None) + note, kb(
        btn("Сменить кошелёк" if a else "Сохранить кошелёк", "wa:addr", "key", style=None if a else "primary"),
        *[[btn(f"{'✓ ' if a and a.threshold == t else ''}от {t} USDT", f"wa:t:{t}") for t in steps[i:i + 2]]
          for i in range(0, len(steps), 2)] if a else [],
        btn("Выключить автовывод", "wa:on:0", "pause") if a and a.active else
        btn("Включить автовывод", "wa:on:1", "ok", style="success") if a else None,
        back("w", "Кошелёк")), src)


@router.callback_query(F.data == "w:auto")
async def cb_auto(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.set_state(None)
    await auto_screen(bot, s, user, c)


@router.callback_query(F.data == "wa:addr")
async def cb_auto_addr(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    await state.set_state(WB.auto_address)
    await show(bot, user, "\n".join([
        title(pe("key"), "Кошелёк для автовывода"),
        "Отправьте адрес своего кошелька <b>USDT в сети BNB Smart Chain (BEP-20)</b>: 0x и 40 знаков.",
        f"{pe('warn')} Адрес другой сети — деньги не вернуть."]), kb(back("w:auto", "Отмена")), c)


@router.message(WB.auto_address, F.text)
async def msg_auto_addr(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    addr, err = await check_address(s, m.text)
    if not addr:
        return await show(bot, user, warn(err) + "\nОтправьте адрес ещё раз.", kb(back("w:auto", "Отмена")))
    await state.set_state(None)
    a = await s.get(BscAuto, user.id)
    if a is None:
        s.add(BscAuto(user_id=user.id, address=addr, threshold=max(Decimal(10), bsc.minimum()), active=False))
    else:
        a.address = addr
    events.add(s, f"user:{user.id}", "bsc_auto", f"Кошелёк автовывода BEP-20: {addr}", user.id, notice=True)
    await s.commit()
    await auto_screen(bot, s, user, note=ok("Кошелёк сохранён. Выберите порог и включите автовывод."))


@router.callback_query(F.data.regexp(r"^wa:(t|on):(\d+)$"))
async def cb_auto_set(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    _, what, v = c.data.split(":")
    a = await s.get(BscAuto, user.id, with_for_update=True)
    if a is None:
        return await c.answer("Сначала сохраните кошелёк", show_alert=True)
    if what == "t":
        if int(v) not in THRESHOLDS or Decimal(v) < bsc.minimum():
            return await c.answer("Такого порога нет", show_alert=True)
        a.threshold, a.active = Decimal(v), True
    else:
        a.active = v == "1"
    await s.commit()
    await auto_screen(bot, s, user, c, ok(f"Автовывод включён: от {money.usdt(a.threshold)} USDT" if a.active
                                          else "Автовывод выключен"))
