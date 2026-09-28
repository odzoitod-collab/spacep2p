"""USDT on TON for users: personal deposit address (on-chain), withdrawal to the user's own TON wallet (paid by
xRocket from the app balance via its Pay API: POST /api/v1/withdrawals, idempotent by clientWithdrawalId)."""
import asyncio
import logging
from decimal import Decimal
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.emoji import back, btn, kb, pe
from bot.handlers.wallet import parse_usdt, pay_or_queue, payout_note, wallet_screen
from bot.models import TonDeposit, TonWallet, User, Withdrawal
from bot.services import events, money, settings, ton, xrocket
from bot.ui import at, esc, notify, ok, quote, show, title, warn

log = logging.getLogger(__name__)
router = Router()


class TonW(StatesGroup):
    address = State()
    memo = State()
    amount = State()


# ---------- deposit ----------

CHECK_EVERY = 20  # seconds between manual "check" presses of one user: each one queries the blockchain API
_checked: dict[int, float] = {}



async def ton_screen(bot: Bot, s: AsyncSession, user: User, src=None, note: str = ""):
    w = await ton.ensure_wallet(s, user.id)
    addr = ton.friendly(w.address)
    recent = (await s.scalars(select(TonDeposit).where(TonDeposit.user_id == user.id)
                              .order_by(TonDeposit.id.desc()).limit(3))).all()
    await show(bot, user, "\n".join([
        title(pe("plus"), "Пополнение USDT в сети TON"),
        "",
        "Ваш личный адрес для пополнения:",
        f"<code>{addr}</code>",
        "",
        quote(
            f"{pe('dollar')} Принимаем только <b>USDT (Tether USD) в сети TON</b>",
            f"{pe('warn')} USDT в других сетях (TRC-20, ERC-20, BEP-20) и другие монеты сюда не отправляйте — "
            "они будут потеряны",
            f"{pe('info')} TON на этот адрес на баланс не зачисляется",
            f"{pe('clock')} Зачисление автоматическое, обычно через 1–2 мин после перевода, без комиссии",
        ),
        "Адрес постоянный: пополняйте сколько угодно раз. Биржа может попросить memo/комментарий — "
        "он не нужен, оставьте пустым.",
        *(["", "Последние пополнения:", quote(*[f"+{money.usdt(d.amount)} USDT · {at(d.tx_time, 'dt')}"
                                                for d in recent])] if recent else []),
    ]) + note, kb(
        btn("Скопировать адрес", icon="key", copy=addr, style="primary"),
        [btn("Проверить", "w:tonchk", "refresh"), btn("Tonviewer", icon="search", url=ton.address_url(addr))],
        back("w", "Кошелёк"),
    ), src)


@router.callback_query(F.data == "w:ton")
async def cb_ton(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.set_state(None)
    if not ton.enabled():
        return await c.answer("Пополнение в сети TON сейчас недоступно", show_alert=True)
    await ton_screen(bot, s, user, c)


@router.callback_query(F.data == "w:tonchk")
async def cb_ton_check(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    if not ton.enabled():
        return await c.answer("Пополнение в сети TON сейчас недоступно", show_alert=True)
    loop_now = asyncio.get_running_loop().time()
    if loop_now - _checked.get(user.id, -CHECK_EVERY) < CHECK_EVERY:
        return await c.answer("Проверяем автоматически каждые 30 секунд. Повторить вручную можно через 20 секунд.",
                              show_alert=True)
    _checked[user.id] = loop_now
    await ton.ensure_wallet(s, user.id)
    try:
        found = await ton.scan(s, user.id)
    except ton.ChainError as e:
        log.warning("ton check %s: %s", user.id, e)
        return await c.answer("Сеть TON сейчас не отвечает. Поступление зачислим автоматически.", show_alert=True)
    await s.refresh(user)
    if found:
        return await ton_screen(bot, s, user, c, ok(f"Зачислено {money.usdt(sum(d.amount for d in found))} USDT"))
    await c.answer("Новых поступлений пока нет. Если вы только что отправили — подождите 1–2 минуты.", show_alert=True)




# ---------- withdrawal: method -> address -> memo -> amount -> confirm -> queue ----------

@router.callback_query(F.data == "w:out")
async def cb_withdraw_choice(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    await state.set_state(None)
    await show(bot, user, "\n".join([
        title(pe("up"), "Вывод USDT"),
        "",
        quote(f"{pe('dollar')} Доступно: <b>{money.usdt(user.balance)} USDT</b>"),
        f"{pe('wallet')} <b>На кошелёк USDT в сети TON</b> — Tonkeeper, Telegram Wallet, биржа. Комиссия "
        f"{money.usdt(settings.dec('ton_withdraw_fee'))} USDT, минимум {settings.get('ton_withdraw_min')} USDT, "
        "обычно несколько минут.",
        f"{pe('dollar')} <b>Чеком xRocket</b> — персональный чек в @xRocket, активировать может только ваш аккаунт.",
    ]), kb(btn("На кошелёк TON", "w:wdt", "wallet", style="success"),
           btn("Чеком xRocket", "w:wd", "dollar"), back("w", "Кошелёк")), c)


def _address_prompt(err: str = "") -> str:
    return "\n".join([
        title(pe("wallet"), "Вывод на TON · шаг 1 из 3"),
        "",
        "Отправьте адрес вашего кошелька <b>USDT в сети TON</b> (начинается с UQ… или EQ…).",
        f"{pe('warn')} Проверьте, что это сеть TON: адреса TRC-20/ERC-20 не подойдут, отправленное не вернуть.",
    ]) + (warn(err) if err else "")


@router.callback_query(F.data == "w:wdt")
async def cb_ton_withdraw(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.set_state(TonW.address)
    last = await s.scalar(select(Withdrawal).where(Withdrawal.user_id == user.id, Withdrawal.method == "ton")
                          .order_by(Withdrawal.id.desc()).limit(1))
    await show(bot, user, _address_prompt(), kb(
        btn(f"Как в прошлый раз: {last.address[:6]}…{last.address[-4:]}", "w:wdt:last", "refresh") if last else None,
        back("w:out", "Отмена")), c)


async def _check_address(s: AsyncSession, raw_text: str) -> tuple[str | None, str]:
    addr = ton.parse_address(raw_text or "")
    if not addr:
        return None, "Это не адрес TON. Скопируйте адрес целиком"
    r = ton.raw(addr)
    if (ton.enabled() and r == ton.gas_address()) or await s.scalar(select(exists().where(TonWallet.address == r))):
        return None, "Это адрес пополнения Strait Pay, а нужен ваш собственный кошелёк"
    return addr, ""


async def _ask_memo(bot, user, state: FSMContext, addr: str, src=None):
    await state.update_data(t_addr=addr, t_memo=None)
    await state.set_state(TonW.memo)
    await show(bot, user, "\n".join([
        title(pe("wallet"), "Вывод на TON · шаг 2 из 3"),
        "",
        f"Адрес: <code>{addr}</code>",
        "",
        "Выводите на <b>биржу</b>? Отправьте memo / комментарий из её раздела пополнения USDT TON — без него биржа "
        "не зачислит перевод. Для личного кошелька комментарий не нужен.",
    ]), kb(btn("Без комментария", "w:wdt:nomemo", "ok", style="primary"), back("w:out", "Отмена")), src)


@router.message(TonW.address, F.text)
async def msg_ton_address(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    addr, err = await _check_address(s, m.text)
    if not addr:
        return await show(bot, user, _address_prompt(err), kb(back("w:out", "Отмена")))
    await _ask_memo(bot, user, state, addr)


@router.callback_query(TonW.address, F.data == "w:wdt:last")
async def cb_ton_last_address(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    last = await s.scalar(select(Withdrawal).where(Withdrawal.user_id == user.id, Withdrawal.method == "ton")
                          .order_by(Withdrawal.id.desc()).limit(1))
    if not last:
        return await c.answer("Прошлых выводов на TON нет", show_alert=True)
    await _ask_memo(bot, user, state, last.address, c)


def _amount_prompt(user: User, addr: str, memo: str | None, err: str = "") -> str:
    fee = settings.dec("ton_withdraw_fee")
    return "\n".join([
        title(pe("wallet"), "Вывод на TON · шаг 3 из 3"),
        "",
        quote(f"{pe('key')} Адрес: <code>{addr}</code>",
              f"{pe('support')} Комментарий: <code>{esc(memo)}</code>" if memo else "",
              f"{pe('dollar')} Доступно: <b>{money.usdt(user.balance)} USDT</b>",
              f"{pe('percent')} Комиссия: <b>{money.usdt(fee)} USDT</b> · минимум {settings.get('ton_withdraw_min')} USDT"),
        "Отправьте сумму списания в USDT — на кошелёк придёт сумма за вычетом комиссии.",
    ]) + (warn(err) if err else "")


async def _ask_amount(bot, user, state: FSMContext, memo: str | None, src=None):
    await state.update_data(t_memo=memo)
    await state.set_state(TonW.amount)
    data = await state.get_data()
    await show(bot, user, _amount_prompt(user, data["t_addr"], memo), kb(
        btn(f"Вывести всё: {money.usdt(user.balance)} USDT", "w:wdt:all", "up") if user.balance > 0 else None,
        back("w:out", "Отмена")), src)


@router.callback_query(TonW.memo, F.data == "w:wdt:nomemo")
async def cb_ton_nomemo(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    await _ask_amount(bot, user, state, None, c)


@router.message(TonW.memo, F.text)
async def msg_ton_memo(m: Message, bot: Bot, user: User, state: FSMContext):
    memo = m.text.strip()
    if not 1 <= len(memo) <= 120 or not memo.isprintable():
        data = await state.get_data()
        return await _ask_memo(bot, user, state, data["t_addr"])
    await _ask_amount(bot, user, state, memo)


QUOTA_TTL = 600
_quota: tuple[float, dict] | None = None


async def quota() -> dict | None:
    """xRocket's minimum and own fee for USDT in TON (cached 10 min); None if xRocket does not answer."""
    global _quota
    t = asyncio.get_running_loop().time()
    if _quota is None or t - _quota[0] > QUOTA_TTL:
        try:
            _quota = t, await asyncio.wait_for(xrocket.rocket.withdrawal_quota(), 5)
        except Exception:  # noqa: BLE001 - the withdrawal request itself will be the final check
            return None
    return _quota[1]


async def _confirm(bot, user, state: FSMContext, v: Decimal | None, src=None):
    data = await state.get_data()
    fee = settings.dec("ton_withdraw_fee")
    q = await quota()
    xmin = Decimal(str(q["withdrawMinSize"])) if q else Decimal(0)
    err = ("Введите сумму числом" if v is None
           else f"Минимум {settings.get('ton_withdraw_min')} USDT" if v < settings.dec("ton_withdraw_min")
           else "Сумма должна быть больше комиссии" if v <= fee
           else f"Доступно только {money.usdt(user.balance)} USDT" if v > user.balance
           else f"После комиссии должно остаться не меньше {money.usdt(xmin)} USDT" if v - fee < xmin else "")
    if err:
        return await show(bot, user, _amount_prompt(user, data["t_addr"], data.get("t_memo"), err),
                          kb(back("w:out", "Отмена")), src)
    await state.set_state(None)
    await state.update_data(t_amount=str(v), t_request=str(uuid4()))
    await show(bot, user, "\n".join([
        title(pe("up"), "Проверьте вывод"),
        "",
        quote(f"{pe('key')} Адрес: <code>{data['t_addr']}</code>",
              f"{pe('support')} Комментарий: <code>{esc(data['t_memo'])}</code>" if data.get("t_memo") else "",
              f"{pe('wallet')} Спишется: <b>{money.usdt(v)} USDT</b>",
              f"{pe('percent')} Комиссия: {money.usdt(fee)} USDT",
              f"{pe('dollar')} Придёт: <b>{money.usdt(v - fee)} USDT</b> в сети TON"),
        f"{pe('warn')} Перевод в блокчейне отменить нельзя. Сверьте первые и последние символы адреса.",
    ]), kb(btn("Отправить", "w:wdt:go", "ok", style="success"), back("w:out", "Отмена")), src)


@router.message(TonW.amount, F.text)
async def msg_ton_amount(m: Message, bot: Bot, user: User, state: FSMContext):
    await _confirm(bot, user, state, parse_usdt(m.text))


@router.callback_query(TonW.amount, F.data == "w:wdt:all")
async def cb_ton_all(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    await _confirm(bot, user, state, user.balance, c)


@router.callback_query(F.data == "w:wdt:go")
async def cb_ton_go(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    data = await state.get_data()
    await state.update_data(t_amount=None, t_request=None)
    if not data.get("t_amount") or not data.get("t_request"):
        return await c.answer("Эта заявка уже обработана или устарела. Начните вывод заново.", show_alert=True)
    amount, fee = Decimal(data["t_amount"]), settings.dec("ton_withdraw_fee")
    if amount <= fee or amount < settings.dec("ton_withdraw_min"):
        return await c.answer("Условия вывода изменились — начните заново", show_alert=True)
    wd = Withdrawal(user_id=user.id, amount=amount, fee=fee, request_id=data["t_request"], method="ton",
                    address=data["t_addr"], memo=data.get("t_memo"))
    s.add(wd)
    try:
        await s.flush()
        await money.add(s, user.id, -amount, "withdraw", f"wd:{wd.id}")
        events.add(s, f"wd:{wd.id}", "request", f"Вывод на TON: списано {money.usdt(amount)} USDT, к отправке "
                   f"{money.usdt(amount - fee)} на {wd.address}" + (f", memo {wd.memo}" if wd.memo else ""),
                   user.id, notice=True)
        await s.commit()  # debited before xRocket is asked: a crash in between leaves "pending" for the sync task
    except (money.NotEnough, IntegrityError) as e:
        await s.rollback()
        await s.refresh(user)
        return await c.answer("Недостаточно средств" if isinstance(e, money.NotEnough) else "Заявка уже обработана",
                              show_alert=True)
    await c.answer("Отправляем…")
    result = await pay_or_queue(s, wd)
    await s.commit()
    await s.refresh(user)
    await wallet_screen(bot, s, user, c, payout_note(wd, result))
    if result == "done":
        await notify_withdrawal(bot, wd, "done")


# ---------- xRocket side of a TON withdrawal ----------

def _apply(s: AsyncSession, wd: Withdrawal, r: dict) -> str:
    """Apply xRocket's answer: done | sent | failed (refunded). Final states are never changed again."""
    ref = f"wd:{wd.id}"
    wd.tx_hash = (r.get("txHash") or wd.tx_hash or "")[:64] or None
    wd.link = r.get("txLink") or wd.link
    st = r.get("status")
    if st == "COMPLETED":
        wd.status = "done"
        if wd.fee:
            money.platform(s, wd.fee, "withdraw_fee", ref)
        events.add(s, ref, "done", f"Вывод на TON выполнен xRocket: {money.usdt(wd.amount - wd.fee)} USDT на "
                   f"{wd.address}" + (f", tx {wd.tx_hash}" if wd.tx_hash else ""), wd.user_id, notice=True)
        return "done"
    if st == "FAIL":
        wd.status, wd.error = "failed", "xRocket: FAIL"
        events.add(s, ref, "failed", f"xRocket отклонил вывод на TON: {money.usdt(wd.amount)} USDT возвращены "
                   "пользователю", wd.user_id, alert=True)
        return "refund"
    if wd.status != "sent":
        wd.status = "sent"
        events.add(s, ref, "sent", "xRocket принял вывод, ждём транзакцию в сети", wd.user_id)
    return "sent"


async def send_withdrawal(s: AsyncSession, wd: Withdrawal) -> str:
    """Ask xRocket to pay. done | sent | unknown | <human reason> (refunded). Does not commit.
    Safe to call again for the same withdrawal: clientWithdrawalId = wd-<id> is executed once."""
    try:
        r = await xrocket.rocket.create_withdrawal(f"wd-{wd.id}", wd.address, wd.amount - wd.fee, wd.memo)
    except xrocket.XRocketError as e:
        wd.error = str(e)[:1000]
        if e.code == "amount_more_than_app_balance":  # the app ran short: wait in the queue instead of refusing
            from bot.handlers.wallet import queue
            return await queue(s, wd)
        if e.uncertain:
            wd.status = "unknown"
            events.add(s, f"wd:{wd.id}", "unknown", f"Ответ xRocket неизвестен ({e.code}) — сверим по "
                       "clientWithdrawalId", wd.user_id, alert=True)
            return "unknown"
        await _refund(s, wd, f"Отказ xRocket ({e.code}): {e.human}")
        return e.human
    result = _apply(s, wd, r)
    if result == "refund":
        await money.add(s, wd.user_id, wd.amount, "withdraw_refund", f"wd:{wd.id}")
        return "xRocket отклонил перевод"
    return result


async def _refund(s: AsyncSession, wd: Withdrawal, why: str) -> None:
    wd.status = "failed"
    await money.add(s, wd.user_id, wd.amount, "withdraw_refund", f"wd:{wd.id}")
    events.add(s, f"wd:{wd.id}", "failed", f"{why}. {money.usdt(wd.amount)} USDT возвращены пользователю",
               wd.user_id, alert=True)


async def sync_withdrawal(s: AsyncSession, wd: Withdrawal) -> str:
    """Check a TON withdrawal in xRocket. done | sent | refunded | unknown | retried. Does not commit."""
    if wd.status == "pending":  # debited, but the process stopped before asking xRocket
        return await send_withdrawal(s, wd)
    try:
        r = await xrocket.rocket.get_withdrawal(f"wd-{wd.id}")
    except xrocket.XRocketError as e:
        if e.code == "app_withdrawal_not_found" and wd.status == "unknown":
            await send_withdrawal(s, wd)  # never reached xRocket: the same id cannot be paid twice
            return "retried"
        await events.alert_once(s, f"wd:{wd.id}", "check_error", f"Ошибка сверки вывода на TON: {e}"[:300],
                                wd.user_id)
        return "unknown"
    result = _apply(s, wd, r)
    if result == "refund":
        await money.add(s, wd.user_id, wd.amount, "withdraw_refund", f"wd:{wd.id}")
        return "refunded"
    return result


async def notify_withdrawal(bot: Bot, wd: Withdrawal, result: str) -> None:
    if result == "done":
        await notify(bot, wd.user_id, f"{pe('ok')} <b>Вывод #{wd.id} выполнен:</b> {money.usdt(wd.amount - wd.fee)} USDT "
                                      f"отправлены на <code>{wd.address}</code>",
                     kb(btn("Транзакция", icon="search", url=wd.link) if wd.link else None, back("x", "Скрыть", "cross")))
    elif result == "refunded":
        await notify(bot, wd.user_id, f"{pe('warn')} Вывод #{wd.id} на TON не выполнен. {money.usdt(wd.amount)} USDT "
                                      "возвращены на баланс.")
