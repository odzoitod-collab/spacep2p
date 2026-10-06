"""Wallet: USDT on TON only. Deposits — the user's personal address (services/ton.py credits every transfer once by
its transaction hash); withdrawals — to any TON wallet or exchange, paid by the bot's hot wallet in order: at once
while it has the USDT, otherwise from the queue as soon as it gets them."""
import logging
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.emoji import back, btn, kb, pe
from bot.models import Deposit, Ledger, Operator, User, Withdrawal
from bot.services import events, money, settings, ton
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


class W(StatesGroup):
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


def withdraw_fee(amount: Decimal) -> Decimal:
    """withdraw_pct of the amount (to whole cents, up) plus the fixed part that covers the network fee."""
    return (amount * settings.dec("withdraw_pct") / 100).quantize(money.KOP, ROUND_UP) + settings.dec("chain_withdraw_fee")


def withdraw_terms() -> str:
    """The fee in words: «1,5% + 1 USDT»."""
    pct = f"{money.fmt(settings.dec('withdraw_pct'), 3)}%"
    fixed = settings.dec("chain_withdraw_fee")
    return pct + (f" + {money.usdt(fixed)} USDT" if fixed else "")


async def queue_place(s: AsyncSession, wd: Withdrawal) -> tuple[int, Decimal]:
    """(withdrawals ahead, USDT ahead) of a queued withdrawal."""
    n, total = (await s.execute(select(func.count(Withdrawal.id), func.coalesce(
        func.sum(Withdrawal.amount - Withdrawal.fee), 0)).where(Withdrawal.method == "ton", Withdrawal.status == "queued",
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
            where = f"{money.usdt(w.amount - w.fee)} USDT → {esc(ton.short(w.address))} · {WD_STATUS.get(w.status, w.status)}"
            if w.status == "queued":
                ahead_n, ahead = await queue_place(s, w)
                where += f", место {ahead_n + 1}" + (f", перед вами {money.usdt(ahead)} USDT" if ahead_n else "")
            lines.append(field(f"#{w.id}", where))
        lines.append(quote("Отправляем сами по очереди, обычно за 1–2 минуты. Если у сервиса не хватает USDT, вывод "
                           "ждёт и уходит автоматически. Пока вывод не отправлялся, его можно отменить."))
    lines += ["", quote(f"USDT в сети TON · пополнение {fee_pct()} · вывод {withdraw_terms()}")]
    await show(bot, user, "\n".join(x for x in lines if x is not None) + note, kb(
        [btn("Пополнить", "w:in", "plus", style="success"), btn("Вывести", "w:out", "up", style="danger")],
        *[btn(f"Отменить вывод #{w.id}", f"w:qc:{w.id}") for w in moving if w.status == "queued" and not w.transfer_id],
        [btn("История операций", inline="операции "), app_btn("Кошелёк в приложении")],
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
        link = wd.link if wd.method == "ton" else None
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
    wl = [f"#{w.id} · {money.usdt(w.amount)} USDT · {esc(ton.short(w.address)) if w.address else 'чек'} · "
          f"{WD_STATUS.get(w.status, w.status)}" for w in wds]
    await show(bot, user, "\n".join([
        title(pe("list"), "История операций"),
        "Новые сверху.",
        quote(*[ledger_line(r) for r in rows]) if rows else "Операций пока нет.",
        *(["<b>Выводы</b>", quote(*wl)] if wl else []),
    ]), kb(*[btn(f"Вывод #{w.id} · транзакция", icon="search", url=w.link) for w in wds
             if w.link and w.method == "ton"],
           back("w", "Кошелёк")), c)


# ---------- deposit: the user's personal address ----------

def deposit_rules() -> str:
    return (f"Только <b>USDT (Tether) в сети TON</b> — другая монета или сеть не зачислится. От "
            f"{settings.get('deposit_min')} USDT, комиссия {fee_pct()}. Memo не нужен.")


async def deposit_screen(bot: Bot, s: AsyncSession, user: User, src=None, note: str = ""):
    if ton.chain is None:
        return await wallet_screen(bot, s, user, src, warn(OFF))
    a = await ton.personal(s, user.id, "deposit")
    await s.commit()
    addr = ton.friendly(a.address)
    recent = (await s.scalars(select(Deposit).where(Deposit.user_id == user.id, Deposit.purpose == "deposit",
                                                    Deposit.tx_hash.is_not(None))
                              .order_by(Deposit.id.desc()).limit(3))).all()
    await show(bot, user, "\n".join(x for x in [
        title(pe("plus"), "Пополнение USDT · TON"),
        "",
        section("key", "Ваш адрес для пополнения"),
        f"<code>{addr}</code>",
        "",
        quote(deposit_rules()),
        "Адрес постоянный и только ваш: пополняйте сколько угодно раз. Зачислим автоматически после подтверждения "
        "в сети (обычно 1–2 минуты) и пришлём уведомление.",
        *([""] + [section("list", "Последние поступления")] + [
            f"#{d.id} · {at(d.created_at, 'dt')} · <b>{money.usdt(d.amount)} USDT</b> · "
            + ("зачислено " + money.usdt(d.credit) if d.status == "paid" else "меньше минимума, не зачислено")
            for d in recent] if recent else []),
    ] if x is not None) + note, kb(
        btn("Скопировать адрес", icon="key", copy=addr, style="primary"),
        btn("Проверить поступление", "w:chk", "refresh"),
        back("w", "Кошелёк")), src)


@router.callback_query(F.data == "w:in")
async def cb_deposit(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.set_state(None)
    await deposit_screen(bot, s, user, c)


@router.callback_query(F.data == "w:chk")
async def cb_check(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    try:
        found = await ton.check_user(s, user.id)
    except ton.ChainError as e:
        log.warning("deposit check %s: %s", user.id, e)
        return await c.answer("Сеть сейчас не отвечает. Поступление зачислим автоматически.", show_alert=True)
    if found is None:
        return await c.answer("Проверка уже идёт — зачислим автоматически.", show_alert=True)
    mine = [d for d in found if d.user_id == user.id]
    if not mine:
        return await c.answer("Новых поступлений пока нет. Перевод в сети TON подтверждается за 1–2 минуты — зачислим "
                              "автоматически.", show_alert=True)
    await s.refresh(user)
    for d in mine:
        if d.purpose == "debt":
            await notify_deposit(bot, s, d)
    credited = sum((d.credit for d in mine if d.purpose == "deposit" and d.status == "paid"), Decimal(0))
    small = [d for d in mine if d.status == "small"]
    await deposit_screen(bot, s, user, c, (ok(f"Зачислено {money.usdt(credited)} USDT") if credited else "")
                         + (warn(f"{len(small)} перевод(а) меньше минимума {settings.get('deposit_min')} USDT — не "
                                 "зачислены, напишите в поддержку") if small else ""))


async def deposit_done_text(s: AsyncSession, dep: Deposit) -> str:
    if dep.purpose == "debt":
        op = await s.get(Operator, dep.user_id, populate_existing=True)
        return (f"{pe('ok')} <b>Долг погашен: {money.usdt(dep.credit)} USDT</b> · поступление #{dep.id}\n"
                f"• Осталось долга: <b>{money.usdt(op.debt if op else Decimal(0))} USDT</b>")
    if dep.status == "small":
        return (f"{pe('warn')} <b>Пришло {money.usdt(dep.amount)} USDT — меньше минимума "
                f"{settings.get('deposit_min')} USDT</b>, на баланс не зачислено. Напишите в поддержку, указав "
                f"поступление #{dep.id}.")
    return (f"{pe('ok')} <b>Баланс пополнен на {money.usdt(dep.credit)} USDT</b> · поступление #{dep.id}\n"
            f"• Пришло {money.usdt(dep.amount)} USDT, комиссия {money.usdt(dep.amount - dep.credit)}")


async def notify_deposit(bot: Bot, s: AsyncSession, dep: Deposit) -> None:
    await notify(bot, dep.user_id, await deposit_done_text(s, dep), kb(
        btn("Транзакция", icon="search", url=dep.link) if dep.link else None,
        btn("Кошелёк", "w", "wallet") if dep.purpose == "deposit" else btn("Кабинет оператора", "op", "shop"),
        back("x", "Скрыть", "cross")))


# ---------- withdraw: to any TON wallet or exchange ----------

def lock_note(user: User) -> str:
    """Why part of the balance cannot be withdrawn yet ("" if all of it can)."""
    free = money.withdrawable(user)
    if free >= user.balance:
        return ""
    return (f"Вывести можно {money.usdt(free)} USDT. Ещё {money.usdt(user.balance - free)} USDT — пополнение, которое "
            "нужно прокрутить: продайте эти USDT покупателям в сделках, и они станут доступны к выводу.")


def withdraw_problem(user: User, v: Decimal | None, fee: Decimal) -> str:
    """Why `v` USDT cannot be withdrawn ("" — it can): the same rules in the bot and the mini app."""
    low = settings.dec("chain_withdraw_min")
    return ("Введите сумму числом" if v is None
            else f"Минимум {money.usdt(low)} USDT" if v < low
            else "Сумма должна быть больше комиссии" if v <= fee
            else f"Доступно только {money.usdt(user.balance)} USDT" if v > user.balance
            else f"Вывести можно только {money.usdt(money.withdrawable(user))} USDT — остальное пополнение ещё не "
                 "прокручено в сделках" if v > money.withdrawable(user) else "")


async def check_address(s: AsyncSession, raw_text: str | None) -> tuple[str | None, str]:
    """(address, "") or (None, why): the same check in the bot and the mini app."""
    addr = ton.parse_address(raw_text)
    if not addr:
        return None, "Это не адрес TON. Скопируйте адрес кошелька USDT в сети TON целиком (UQ… или EQ…)"
    if await ton.is_ours(s, addr):
        return None, "Это адрес Strait Pay, а нужен ваш собственный кошелёк или адрес биржи"
    return addr, ""


def valid_memo(memo) -> str | None:
    """A comment an exchange needs, or None if it is not acceptable."""
    if not isinstance(memo, str):
        return None
    memo = memo.strip()
    return memo if 1 <= len(memo) <= 120 and memo.isprintable() else None


def _step(n: int, text: str, err: str = "") -> str:
    return f"{title(pe('up'), 'Вывод USDT · TON')} · шаг {n} из 3\n{text}" + (warn(err) if err else "")


async def _last_address(s: AsyncSession, uid: int) -> str | None:
    return await s.scalar(select(Withdrawal.address).where(Withdrawal.user_id == uid, Withdrawal.method == "ton")
                          .order_by(Withdrawal.id.desc()).limit(1))


@router.callback_query(F.data == "w:out")
async def cb_withdraw(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    if ton.chain is None:
        return await c.answer(OFF, show_alert=True)
    await state.set_state(W.address)
    await state.update_data(t_addr=None, t_memo=None, t_amount=None, t_request=None)
    last = await _last_address(s, user.id)
    free = money.withdrawable(user)
    await show(bot, user, _step(1, "\n".join(x for x in [
        quote(f"• Можно вывести: <b>{money.usdt(free)} USDT</b>" + (f" из {money.usdt(user.balance)}"
                                                                     if free < user.balance else ""),
              f"• Комиссия: {withdraw_terms()} · минимум {settings.get('chain_withdraw_min')} USDT"),
        quote(lock_note(user)) if lock_note(user) else None,
        f"Отправьте адрес кошелька <b>USDT в сети TON</b> (или адрес пополнения биржи в сети TON).\n"
        f"{pe('warn')} Адрес другой сети — деньги не вернуть.",
    ] if x)), kb(btn(f"Как в прошлый раз: {ton.short(last)}", "w:last", "refresh") if last else None,
                 back("w", "Отмена")), c)


async def _after_address(bot, user, state: FSMContext, addr: str, src=None):
    await state.update_data(t_addr=addr)
    await state.set_state(W.memo)
    await show(bot, user, _step(2, f"Адрес: <code>{esc(addr)}</code>\n\nВыводите на <b>биржу</b>? Отправьте memo "
                                   "(комментарий) из её раздела пополнения — без него биржа не зачислит. Личному "
                                   "кошельку memo не нужен."),
               kb(btn("Без комментария", "w:nomemo", "ok", style="primary"), back("w", "Отмена")), src)


@router.message(W.address, F.text)
async def msg_address(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    addr, err = await check_address(s, m.text)
    if not addr:
        return await show(bot, user, _step(1, "Отправьте адрес кошелька ещё раз.", err), kb(back("w", "Отмена")))
    await _after_address(bot, user, state, addr)


@router.callback_query(W.address, F.data == "w:last")
async def cb_last(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    addr, _ = await check_address(s, await _last_address(s, user.id))
    if not addr:
        return await c.answer("Прошлого адреса нет — отправьте адрес сообщением", show_alert=True)
    await _after_address(bot, user, state, addr, c)


@router.callback_query(W.memo, F.data == "w:nomemo")
async def cb_nomemo(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    await _ask_amount(bot, user, state, None, c)


@router.message(W.memo, F.text)
async def msg_memo(m: Message, bot: Bot, user: User, state: FSMContext):
    memo = valid_memo(m.text)
    if memo is None:
        return await show(bot, user, _step(2, "Memo — до 120 обычных символов. Отправьте его ещё раз или нажмите «Без "
                                              "комментария»."),
                          kb(btn("Без комментария", "w:nomemo", "ok", style="primary"), back("w", "Отмена")))
    await _ask_amount(bot, user, state, memo)


def _amount_text(user: User, data: dict, err: str = "") -> str:
    return _step(3, "\n".join([
        quote(f"• Адрес: <code>{esc(data['t_addr'])}</code>",
              f"• Memo: <code>{esc(data['t_memo'])}</code>" if data.get("t_memo") else "",
              f"• Можно вывести: <b>{money.usdt(money.withdrawable(user))} USDT</b>",
              f"• Комиссия: <b>{withdraw_terms()}</b> · минимум {settings.get('chain_withdraw_min')} USDT"),
        "Отправьте сумму списания — придёт сумма минус комиссия."]), err)


async def _ask_amount(bot, user, state: FSMContext, memo: str | None, src=None):
    await state.update_data(t_memo=memo)
    await state.set_state(W.amount)
    free = money.withdrawable(user)
    await show(bot, user, _amount_text(user, await state.get_data()), kb(
        btn(f"Вывести всё: {money.usdt(free)} USDT", "w:all", "up")
        if free >= settings.dec("chain_withdraw_min") and free > withdraw_fee(free) else None,
        back("w", "Отмена")), src)


async def _confirm(bot, user, state: FSMContext, v: Decimal | None, src=None):
    data = await state.get_data()
    if not data.get("t_addr"):
        return await show(bot, user, warn("Заявка устарела — начните вывод заново"), kb(back("w", "Кошелёк")), src)
    fee = withdraw_fee(v) if v is not None else Decimal(0)
    if err := withdraw_problem(user, v, fee):
        return await show(bot, user, _amount_text(user, data, err), kb(back("w", "Отмена")), src)
    await state.set_state(None)
    await state.update_data(t_amount=str(v), t_fee=str(fee), t_request=str(uuid4()))
    await show(bot, user, "\n".join([
        title(pe("up"), "Проверьте вывод"),
        quote("• Сеть: <b>TON</b>, монета <b>USDT</b>",
              f"• Адрес: <code>{esc(data['t_addr'])}</code>",
              f"• Memo: <code>{esc(data['t_memo'])}</code>" if data.get("t_memo") else "",
              f"• Спишется: <b>{money.usdt(v)} USDT</b>",
              f"• Комиссия {withdraw_terms()}: −{money.usdt(fee)} USDT",
              f"• Придёт: <b>{money.usdt(v - fee)} USDT</b>"),
        f"{pe('warn')} Перевод в блокчейне не отменить. Сверьте начало и конец адреса.",
    ]), kb(btn("Отправить", "w:go", "ok", style="success"), back("w", "Отмена")), src)


@router.message(W.amount, F.text)
async def msg_amount(m: Message, bot: Bot, user: User, state: FSMContext):
    await _confirm(bot, user, state, parse_usdt(m.text))


@router.callback_query(W.amount, F.data == "w:all")
async def cb_all(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    await _confirm(bot, user, state, money.withdrawable(user), c)


@router.callback_query(F.data == "w:go")
async def cb_go(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    data = await state.get_data()
    await state.update_data(t_amount=None, t_request=None)
    if not data.get("t_amount") or not data.get("t_request") or not data.get("t_addr"):
        return await c.answer("Эта заявка уже обработана или устарела. Начните вывод заново.", show_alert=True)
    amount = Decimal(data["t_amount"])
    fee = withdraw_fee(amount)
    if Decimal(data["t_fee"]) != fee:
        return await c.answer("Комиссия изменилась — начните вывод заново", show_alert=True)
    if err := withdraw_problem(user, amount, fee):
        return await c.answer(err, show_alert=True)
    addr, err = await check_address(s, data["t_addr"])
    if not addr:
        return await c.answer(err, show_alert=True)
    wd = Withdrawal(user_id=user.id, amount=amount, fee=fee, request_id=data["t_request"], method="ton", network="TON",
                    address=addr, memo=data.get("t_memo"))
    if err := await submit(s, user, wd, "бот"):
        return await c.answer(err, show_alert=True)
    await c.answer("Вывод принят")
    await s.refresh(user)
    await wallet_screen(bot, s, user, c, ok(accepted(wd)))


def accepted(wd: Withdrawal) -> str:
    return (f"Вывод #{wd.id} принят: {money.usdt(wd.amount - wd.fee)} USDT отправим автоматически, обычно за 1–2 минуты. "
            "Если у сервиса не хватит USDT — вывод подождёт в очереди и уйдёт сам.")


async def submit(s: AsyncSession, user: User, wd: Withdrawal, where: str) -> str:
    """Debit and queue a withdrawal, commit, wake the payout cycle. "" or why not. The bot and the mini app alike."""
    if ton.chain is None:
        return OFF
    u = await money.lock(s, user.id)  # the final word, under the row lock: only what was turned over leaves
    if wd.amount > money.withdrawable(u):
        await s.rollback()
        return (f"Вывести можно только {money.usdt(money.withdrawable(u))} USDT: пополнение сначала нужно прокрутить "
                "в сделках")
    wd.status, wd.method, wd.network = "queued", "ton", "TON"
    s.add(wd)
    try:
        await s.flush()
        await money.add(s, user.id, -wd.amount, "withdraw", f"wd:{wd.id}")
        events.add(s, f"wd:{wd.id}", "request", f"Запрос вывода ({where}): списано {money.usdt(wd.amount)} USDT, к "
                   f"отправке {money.usdt(wd.amount - wd.fee)} на {wd.address}" + (f", memo {wd.memo}" if wd.memo else ""),
                   user.id, notice=True)
        await s.commit()
    except (money.NotEnough, IntegrityError) as e:
        await s.rollback()
        await s.refresh(user)  # rollback expires every loaded object
        return "Недостаточно средств" if isinstance(e, money.NotEnough) else "Заявка уже обработана"
    ton.wake.set()
    return ""


async def cancel_queued(s: AsyncSession, user: User, wid: int, where: str) -> Withdrawal | None:
    """A queued withdrawal no message was ever signed for goes back to the balance. None: too late. Does not commit."""
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
        await notify(bot, wd.user_id, f"{pe('ok')} <b>Вывод #{wd.id} выполнен:</b> {net} USDT · TON · "
                                      f"<code>{esc(wd.address or '')}</code>",
                     kb(btn("Транзакция", icon="search", url=wd.link) if wd.link else None, back("x", "Скрыть", "cross")))
    elif result == "refunded":
        await notify(bot, wd.user_id, f"{pe('warn')} Вывод #{wd.id} не выполнен. {money.usdt(wd.amount)} USDT "
                                      "возвращены на баланс.")
