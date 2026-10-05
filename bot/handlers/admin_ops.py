"""Admin operation cards (withdrawal, deposit), event history, support tickets, CSV reports, alert rendering."""
import csv
import io
from contextlib import suppress
from datetime import timedelta
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import config
from bot.services.admins import IsAdmin
from bot.emoji import back, btn, kb, pe
from bot.handlers.admin import DEP_LABEL, WD_LABEL
from bot.handlers.wallet import (check_deposit, deposit_done_text, notify_withdrawal, reconcile, send_cheque,
                                 sync_withdrawal)
from bot.models import Audit, Deal, Deposit, Event, Ledger, Ticket, User, Withdrawal, now
from bot.services import admins, audit, events, money, settings, xrocket
from bot.ui import alink, at, cf, esc, files_to, notify, ok, quote, section, show, title, ulink, warn
from bot.ui import card as fields

router = Router()
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class Ops(StatesGroup):
    reply = State()
    token = State()


def _who(u: User | None, uid: int) -> str:
    return f"{esc(u.name or '—')} (<code>{uid}</code>)" if u else f"<code>{uid}</code>"


# ---------- lists ----------

@router.callback_query(F.data.in_({"al", "awl:check"}))
async def cb_payments(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    check_only = c.data == "awl:check"
    q = select(Withdrawal).order_by(Withdrawal.status.in_(("unknown", "pending")).desc(), Withdrawal.id.desc()).limit(10)
    if check_only:
        q = select(Withdrawal).where(Withdrawal.status.in_(("unknown", "pending"))).order_by(Withdrawal.id).limit(20)
    wds = (await s.scalars(q)).all()
    deps = [] if check_only else (await s.scalars(select(Deposit).order_by(Deposit.id.desc()).limit(8))).all()
    await show(bot, user, "\n".join([
        title(pe("wallet"), "Выводы на проверке" if check_only else "Пополнения и выводы"),
        "",
        "Откройте операцию: карточка показывает всю цепочку и следующее действие. Поиск — «Найти» → "
        "<code>в482</code> / <code>п12</code>.",
    ]) + ("" if wds or deps else f"\n\n{pe('ok')} Пусто"), kb(
        *[btn(f"Вывод #{w.id} · {'чек' if w.method == 'xrocket' else w.network or 'TON'} · {money.usdt(w.amount)} USDT · "
              f"{WD_LABEL.get(w.status, w.status)}", f"awv:{w.id}", "up",
              style="danger" if w.status in ("unknown", "pending", "sending") else None) for w in wds],
        *[btn(f"Пополнение #{d.id} · {money.usdt(d.amount)} USDT · {DEP_LABEL.get(d.status, d.status)}",
              f"adp:{d.id}", "down") for d in deps],
        [btn("Обновить", c.data, "refresh"), back("a", "Админ-панель")],
    ), c)


# ---------- xRocket: the app, its balance, the payout queue, the token ----------

@router.callback_query(F.data.in_({"axr", "axr:go"}))
async def cb_xrocket(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.set_state(None)
    note = ""
    if c.data == "axr:go":  # send what the balance covers now, in order
        from bot.tasks import payout_queue
        before = await s.scalar(select(func.count(Withdrawal.id)).where(Withdrawal.status == "queued"))
        await payout_queue(bot)
        left = await s.scalar(select(func.count(Withdrawal.id)).where(Withdrawal.status == "queued"))
        audit.log(s, user.id, "payout_queue", "", f"{before} → {left}")
        note = ok(f"Отправлено из очереди: {before - left}, осталось {left}")
    await xrocket_screen(bot, s, user, c, note)


async def xrocket_screen(bot: Bot, s: AsyncSession, admin: User, src=None, note: str = ""):
    tok = xrocket.token(config.xrocket_token)
    try:
        funds = await xrocket.usdt_available(timeout=5)
        status = f"подключён · на балансе приложения <b>{money.usdt(funds)} USDT</b>"
    except Exception as e:  # noqa: BLE001 - shown to the admin as it is
        funds, status = None, f"<b>нет ответа</b>: {esc(getattr(e, 'human', str(e))[:120])}"
    queued = (await s.scalars(select(Withdrawal).where(Withdrawal.status == "queued").order_by(Withdrawal.id)
                              .limit(15))).all()
    need = sum((w.amount - w.fee + (w.net_fee or 0) for w in queued), Decimal(0))
    people = {u.id: u for u in (await s.scalars(select(User).where(User.id.in_({w.user_id for w in queued})))).all()}
    await show(bot, admin, "\n".join([
        title(pe("wallet"), "xRocket"),
        "",
        fields(cf("Статус", status, icon="info"),
               cf("Токен", f"<code>{xrocket.hint(tok)}</code> · " + ("из админ-панели" if settings.raw(xrocket.TOKEN_KEY)
                                                                       else "из .env (XROCKET_TOKEN)"), icon="key"),
               cf("Сети USDT", ", ".join(xrocket.net_name(n) for n in await xrocket.networks()), icon="swap"),
               cf("Очередь выводов", f"<b>{len(queued)}</b> на {money.usdt(need)} USDT"
                  + (f" · не хватает <b>{money.usdt(need - funds)} USDT</b>" if funds is not None and need > funds
                     else ""), icon="clock")),
        "\n".join(f"{i}. {alink('wd', w.id, f'#{w.id}')} · {money.usdt(w.amount - w.fee)} USDT · "
                  f"{'чек' if w.method == 'xrocket' else xrocket.net_name(w.network)} · "
                  f"{ulink(people.get(w.user_id), w.user_id)}" for i, w in enumerate(queued, 1)) if queued else "",
        "",
        quote("Выводы встают в очередь, когда на балансе приложения не хватает USDT, и уходят строго по порядку "
              "сами (проверка раз в 30 с). Пополните приложение в @xRocket — или нажмите «Отправить очередь»."),
    ]).replace("\n\n\n", "\n\n") + note, kb(
        btn("Отправить очередь сейчас", "axr:go", "up", style="success") if queued else None,
        btn("Сменить токен", "axr:tok", "key") if admins.is_owner(admin.id) else None,
        btn("Пополнения и выводы", "al", "list"),
        back("a", "Админ-панель")), src)


@router.callback_query(F.data == "axr:tok")
async def cb_token(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    if not admins.is_owner(user.id):
        return await c.answer("Менять токен могут только владельцы", show_alert=True)
    await state.set_state(Ops.token)
    await show(bot, user, _token_text(), kb(back("axr", "Отмена")), c)


def _token_text(err: str = "") -> str:
    return "\n".join([
        f"{pe('key')} <b>Новый токен xRocket Pay API</b>",
        "",
        quote("Пришлите токен следующим сообщением: @xRocket → Rocket Pay → ваше приложение → API-токен. "
              "Бот проверит его запросом баланса и сразу удалит ваше сообщение. Действует для всех операций с "
              "этой минуты; открытые счета и выводы старого приложения проверяйте в нём самом."),
    ]) + (warn(err) if err else "")


@router.message(Ops.token, F.text)
async def msg_token(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    with suppress(TelegramAPIError):
        await m.delete()  # a secret: gone at once, whatever happens next
    tok = m.text.strip()
    if not admins.is_owner(user.id):
        await state.set_state(None)
        return await show(bot, user, warn("Менять токен могут только владельцы"), kb(back("a", "Админ-панель")))
    if not 20 <= len(tok) <= 200 or any(ch.isspace() for ch in tok):
        return await show(bot, user, _token_text("Это не похоже на токен — скопируйте его целиком"),
                          kb(back("axr", "Отмена")))
    try:
        funds = await xrocket.check_token(tok, config.xrocket_base_url)
    except xrocket.XRocketError as e:
        return await show(bot, user, _token_text(f"xRocket не принял токен: {e.human}"), kb(back("axr", "Отмена")))
    await state.set_state(None)
    old = xrocket.token(config.xrocket_token)
    await settings.put(s, xrocket.TOKEN_KEY, tok)
    audit.log(s, user.id, "xrocket_token", "", f"{xrocket.hint(old)} → {xrocket.hint(tok)}")
    events.add(s, "app:xrocket", "token", f"Токен xRocket сменён ({user.name}): {xrocket.hint(tok)}", user.id,
               alert=True)
    await s.commit()
    await xrocket.switch(tok, config.xrocket_base_url)
    await xrocket_screen(bot, s, user, note=ok(f"Токен принят: на балансе приложения {money.usdt(funds)} USDT"))


# ---------- withdrawal card ----------

WD_NEXT = {
    "pending": "Запрос отправляется в xRocket. Если статус не изменится 5 мин, вывод перейдёт в проверку.",
    "unknown": "Деньги пользователя удержаны. Следующее действие: проверить чек по clientChequeId.",
    "done": "Чек выдан пользователю. Проверка покажет, активирован ли он или отменён.",
    "failed": "Вывод не выполнен, удержанная сумма возвращена на баланс.",
}


async def withdrawal_screen(bot: Bot, s: AsyncSession, admin: User, wd: Withdrawal, src=None, note: str = "",
                            refund: bool = False):
    u = await s.get(User, wd.user_id)
    last = await s.scalar(select(Event).where(Event.ref == f"wd:{wd.id}").order_by(Event.id.desc()).limit(1))
    if wd.method == "chain":
        nxt = {"pending": "Списано, запрос в xRocket ещё не отправлен — фоновая задача отправит его в течение минуты.",
               "unknown": "Ответ xRocket неизвестен — сверим по clientWithdrawalId; если xRocket его не знает, "
                          "запрос повторится с тем же id (двойного вывода не будет).",
               "sent": "xRocket принял вывод, транзакция в пути. Статус проверяется раз в минуту.",
               "done": "Выполнен xRocket.", "failed": "Не выполнен, сумма возвращена на баланс."}.get(wd.status, "")
        return await show(bot, admin, "\n".join([
            title(pe("up"), f"Вывод на кошелёк {alink('wd', wd.id, f'#{wd.id}')}") + f" · {WD_LABEL.get(wd.status, wd.status)}",
            "",
            fields(
                cf("Пользователь", ulink(u, wd.user_id), icon="profile"),
                cf("Сумма", f"Списано: <b>{money.usdt(wd.amount)} USDT</b>",
                   f"к отправке {money.usdt(wd.amount - wd.fee)} · комиссия {money.usdt(wd.fee)} "
                   f"(сеть {money.usdt(wd.net_fee)})", icon="wallet"),
                cf("Куда", f"сеть <b>{xrocket.net_name(wd.network)}</b>", f"<code>{esc(wd.address or '—')}</code>"
                   + (f" · memo <code>{esc(wd.memo)}</code>" if wd.memo else ""), icon="swap"),
                cf("Когда", f"создан {at(wd.created_at, 'dt')}" + (f" · отправлен {at(wd.sent_at, 'dt')}" if wd.sent_at else ""),
                   icon="clock"),
                cf("xRocket", f"clientWithdrawalId: <code>wd-{wd.id}</code>", f"tx: <code>{esc(wd.tx_hash or '—')}</code>",
                   f"ошибка: {esc(wd.error[:200])}" if wd.error else "", icon="key"),
                cf("Последнее событие", f"{esc(last.text[:150])} · {at(last.created_at, 'dt')}", icon="list") if last else "",
            ),
            "",
            quote(nxt) if nxt else "",
        ]) + note, kb(
            btn("Проверить в xRocket", f"wt:{wd.id}", "refresh", style="primary")
            if wd.status in ("pending", "sent", "unknown") else None,
            btn("Транзакция", icon="search", url=wd.link) if wd.link else None,
            btn("История вывода", f"aev:wd:{wd.id}", "list"),
            back("al", "Пополнения и выводы"),
        ), src)
    await show(bot, admin, "\n".join([
        title(pe("up"), f"Вывод {alink('wd', wd.id, f'#{wd.id}')}") + f" · {WD_LABEL.get(wd.status, wd.status)}",
        "",
        fields(
            cf("Пользователь", ulink(u, wd.user_id), icon="profile"),
            cf("Сумма", f"Списано: <b>{money.usdt(wd.amount)} USDT</b>",
               f"Чек: {money.usdt(wd.amount - wd.fee)} USDT · комиссия: {money.usdt(wd.fee)} USDT", icon="wallet"),
            cf("Когда", f"создан {at(wd.created_at, 'dt')}", icon="clock"),
            cf("xRocket", f"clientChequeId: <code>wd-{wd.id}</code>"
               + (f" · chequeId: <code>{esc(wd.cheque_id)}</code>" if wd.cheque_id else ""),
               f"Ответ xRocket: {esc((wd.error or '—')[:200])}", icon="key"),
            cf("Последнее событие", f"{esc(last.text[:150])} · {at(last.created_at, 'dt')}", icon="list") if last else "",
        ),
        "",
        quote(WD_NEXT[wd.status]) if wd.status in WD_NEXT else "",
    ]) + note, kb(
        btn("Проверить в xRocket", f"wr:{wd.id}", "refresh", style="primary") if wd.status in ("unknown", "done") else None,
        btn("Вернуть средства", f"wr:rf:{wd.id}", "cross", style="danger") if refund else None,
        btn("Ссылка на чек", url=wd.link, icon="wallet") if wd.link else None,
        btn("История вывода", f"aev:wd:{wd.id}", "list"),
        back("al", "Пополнения и выводы"),
    ), src)


@router.callback_query(F.data.regexp(r"^awv:(\d+)$"))
async def cb_withdrawal(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    wd = await s.get(Withdrawal, int(c.data.split(":")[1]), populate_existing=True)
    if not wd:
        return await c.answer("Вывод не найден", show_alert=True)
    await withdrawal_screen(bot, s, user, wd, c)


RESULT = {"done": "Чек найден в xRocket и отправлен пользователю.",
          "refunded": "Чек отменён в xRocket — удержанная сумма возвращена на баланс.",
          "active": "Чек существует: действует или уже активирован. Действий не нужно.",
          "manual": "Состояние чека нестандартное — проверьте в xRocket вручную. Автоматических действий нет.",
          "busy": "Заявка уже обработана.",
          "missing": "Чек с этим clientChequeId в xRocket не найден."}


@router.callback_query(F.data.regexp(r"^wr:(\d+)$"))
async def cb_reconcile_withdrawal(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    wid = int(c.data.split(":")[1])
    result, wd = await reconcile(s, wid)
    audit.log(s, user.id, "wd_check", f"wd:{wid}", result)
    await s.commit()
    if result == "done":
        await send_cheque(bot, wd)
    elif result == "refunded":
        await notify(bot, wd.user_id, f"{pe('warn')} Чек по выводу #{wd.id} отменён в xRocket. "
                                      f"{money.usdt(wd.amount)} USDT возвращены на баланс.")
    text = RESULT.get(result) or f"Ответ xRocket не получен ({esc(result[6:])}). Деньги остаются удержанными, повторите позже."
    refund = result == "missing" and wd.status == "unknown"
    if refund:
        text += (" Кнопка ниже повторит создание чека с тем же ключом и сразу отменит его: если исходный запрос "
                 "всё-таки прошёл, дубль не создастся и возврат будет остановлен.")
    wd = await s.get(Withdrawal, wid, populate_existing=True)
    await withdrawal_screen(bot, s, user, wd, c, "\n" + f"{pe('search')} <b>Проверка:</b> {text}", refund)


@router.callback_query(F.data.regexp(r"^wr:rf:(\d+)$"))
async def cb_refund_unknown(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    wid = int(c.data.split(":")[2])
    wd = await s.get(Withdrawal, wid, with_for_update=True, populate_existing=True)
    if not wd or wd.status != "unknown":
        return await c.answer("Заявка уже обработана", show_alert=True)
    client_id = f"wd-{wid}"
    try:
        cheque = await xrocket.rocket.get_cheque_by_client(client_id)
    except xrocket.XRocketError as e:
        if e.code != "app_cheque_not_found":
            return await c.answer("Не удалось подтвердить отсутствие чека", show_alert=True)
        # Reuse the original idempotency key. A delayed original request will
        # produce a duplicate rather than a second cheque; never refund then.
        try:
            await xrocket.rocket.create_cheque(wd.amount - wd.fee, client_id, wd.user_id,
                                               "Проверка неизвестного вывода")
            await xrocket.rocket.delete_cheque_by_client(client_id)
            cheque = await xrocket.rocket.get_cheque_by_client(client_id)
        except xrocket.XRocketError:
            return await c.answer("Чек не отменён, возврат остановлен", show_alert=True)
    if not cheque.get("deleted"):
        return await c.answer("Чек действует — возврат остановлен", show_alert=True)
    wd.status = "failed"
    wd.error = f"Ручной возврат администратором {user.id}; чек не найден"
    await money.add(s, wd.user_id, wd.amount, "withdraw_refund", f"wd:{wid}")
    audit.log(s, user.id, "wd_refund", f"wd:{wid}", str(wd.amount))
    events.add(s, f"wd:{wid}", "refunded", f"Чек не создан — {money.usdt(wd.amount)} USDT возвращены "
                                           f"администратором {user.id}", wd.user_id, alert=True)
    await s.commit()
    await notify(bot, wd.user_id, f"{pe('ok')} Вывод #{wid} не создал чек. {money.usdt(wd.amount)} USDT возвращены на баланс.")
    await withdrawal_screen(bot, s, user, wd, c, ok("Средства возвращены"))


@router.callback_query(F.data.regexp(r"^wt:(\d+)$"))
async def cb_chain_check(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    """Ask xRocket about a withdrawal to an address right now."""
    wd = await s.get(Withdrawal, int(c.data.split(":")[1]), with_for_update=True, populate_existing=True)
    if not wd or wd.method != "chain":
        return await c.answer("Вывод не найден", show_alert=True)
    result = await sync_withdrawal(s, wd) if wd.status in ("pending", "sent", "unknown") else "final"
    audit.log(s, user.id, "wd_check", f"wd:{wd.id}", result)
    await s.commit()
    await notify_withdrawal(bot, wd, result)
    await withdrawal_screen(bot, s, user, wd, c, ok({
        "done": "xRocket: выполнен.", "sent": "xRocket: принят, транзакция ещё в пути.",
        "refunded": "xRocket: не выполнен — сумма возвращена пользователю.",
        "retried": "xRocket не знал этот вывод — отправлен заново (тот же clientWithdrawalId, дубля не будет).",
        "unknown": "xRocket не ответил — повторите позже.", "final": "Вывод уже завершён.",
    }.get(result, f"Результат: {esc(result)}")))


# ---------- deposit card ----------

async def deposit_screen(bot: Bot, s: AsyncSession, admin: User, dep: Deposit, src=None, note: str = ""):
    u = await s.get(User, dep.user_id)
    nxt = {"new": "Счёт мог не создаться: проверка найдёт его по clientInvoiceId.",
           "active": "Ждём оплату; бот проверяет раз в минуту.",
           "paid": "Зачислено на баланс пользователя.", "expired": "Истёк без оплаты.",
           "failed": "xRocket отказал в создании счёта."}.get(dep.status, "")
    await show(bot, admin, "\n".join([
        title(pe("down"), f"Пополнение {alink('dep', dep.id, f'#{dep.id}')}") + f" · {DEP_LABEL.get(dep.status, dep.status)}",
        "",
        fields(
            cf("Пользователь", ulink(u, dep.user_id), icon="profile"),
            cf("Способ", f"адрес {xrocket.net_name(dep.network)}: <code>{esc(dep.address)}</code>" if dep.address
               else "счёт-ссылка xRocket", icon="swap"),
            cf("Сумма", f"<b>{money.usdt(dep.amount)} USDT</b> · "
               + (f"зачислено: <b>{money.usdt(dep.credit)} USDT</b>" if dep.status == "paid"
                  else f"ожидается: {money.usdt(dep.credit)} USDT"), icon="dollar"),
            cf("Когда", f"создан {at(dep.created_at, 'dt')}", icon="clock"),
            cf("xRocket", f"clientInvoiceId: <code>dep-{dep.id}</code>",
               f"invoiceId: <code>{esc(dep.invoice_id or '—')}</code>", icon="key"),
        ),
        "",
        quote(nxt) if nxt else "",
    ]) + note, kb(
        btn("Проверить в xRocket", f"dpc:{dep.id}", "refresh", style="primary") if dep.status in ("new", "active") else None,
        btn("История", f"aev:dep:{dep.id}", "list"),
        back("al", "Пополнения и выводы"),
    ), src)


@router.callback_query(F.data.regexp(r"^adp:(\d+)$"))
async def cb_deposit(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    dep = await s.get(Deposit, int(c.data.split(":")[1]), populate_existing=True)
    if not dep:
        return await c.answer("Пополнение не найдено", show_alert=True)
    await deposit_screen(bot, s, user, dep, c)


@router.callback_query(F.data.regexp(r"^dpc:(\d+)$"))
async def cb_deposit_check(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    dep = await s.get(Deposit, int(c.data.split(":")[1]))
    if not dep or dep.status not in ("new", "active"):
        return await c.answer("Счёт уже обработан", show_alert=True)
    try:
        st = await check_deposit(s, dep)
    except xrocket.XRocketError as e:
        return await deposit_screen(bot, s, user, dep, c, warn(f"xRocket не ответил: {e.human}. Повторите позже."))
    if st == "credited":
        await notify(bot, dep.user_id, await deposit_done_text(s, dep))
    dep = await s.get(Deposit, dep.id, populate_existing=True)
    await deposit_screen(bot, s, user, dep, c, ok({"credited": "Оплачен, зачислено", "expired": "Истёк без оплаты"}
                                                  .get(st, "Ещё не оплачен")))


# ---------- event history of any operation ----------

REF_NAMES = {"om": ("Ордерный мерчант", "aom"), "apa": ("Заявка на API", "aap"), "apc": ("API-клиент", "acl"),
             "wd": ("Вывод", "awv"), "dep": ("Пополнение", "adp"), "deal": ("Сделка", "adv"), "user": ("Пользователь", "auv"),
             "card": ("Карта", "acv"), "adj": ("Корректировка", "adjv"), "ticket": ("Обращение", "atk"),
             "op": ("Оператор", "aop"), "team": ("Команда", "atm")}


@router.callback_query(F.data.regexp(r"^aev:(wd|dep|deal|user|card|adj|ticket|apa|apc|om|op|team):(\d+)$"))
async def cb_events(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    _, kind, oid = c.data.split(":")
    rows = await events.history(s, f"{kind}:{oid}")
    name, cb = REF_NAMES[kind]
    lines = [f"{at(e.created_at, 'dt')} · {esc(e.text[:200])}" + (" · <b>админам</b>" if e.alert else "")
             for e in rows]
    while len("\n".join(lines)) > 3300:  # a long history: the latest events fit into one message
        lines.pop(0)
    await show(bot, user, "\n".join([
        title(pe("list"), f"История · {name} {alink(kind, oid, f'#{oid}')}"),
        "",
        "<blockquote>" + "\n".join(lines) + "</blockquote>" if lines
        else "<i>Событий нет — операция создана до журнала событий.</i>",
        "",
        quote("Последние события, новые внизу. «админам» — ушло в админ-чат."),
    ]), kb(back(f"{cb}:{oid}", "Назад")), c)


# ---------- tickets ----------

TICKET_STATUS = {"open": "ждёт ответа", "answered": "отвечено", "closed": "закрыто"}


@router.callback_query(F.data.regexp(r"^atl(?::(all))?$"))
async def cb_tickets(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.set_state(None)
    show_all = c.data.endswith(":all")
    q = select(Ticket).order_by(Ticket.id.desc()).limit(20) if show_all else \
        select(Ticket).where(Ticket.status == "open").order_by(Ticket.id).limit(20)
    rows = (await s.scalars(q)).all()
    await show(bot, user, title(pe("support"), "Обращения" + (": все" if show_all else ": ждут ответа"))
               + ("" if rows else f"\n\n{pe('ok')} Пусто"), kb(
        *[btn(f"#{t.id} · {t.user_id} · {TICKET_STATUS[t.status]} · {t.text[:20]}", f"atk:{t.id}", "support") for t in rows],
        btn("Ждут ответа" if show_all else "Все обращения", "atl" if show_all else "atl:all", "list"),
        back("a", "Админ-панель"),
    ), c)


async def ticket_screen(bot: Bot, s: AsyncSession, admin: User, t: Ticket, src=None, note: str = ""):
    u = await s.get(User, t.user_id)
    answered = await s.get(User, t.answered_by) if t.answered_by else None
    await show(bot, admin, "\n".join([
        title(pe("support"), f"Обращение {alink('ticket', t.id, f'#{t.id}')}") + f" · {TICKET_STATUS[t.status]}",
        "",
        fields(
            cf("Пользователь", ulink(u, t.user_id), icon="profile"),
            cf("Когда", at(t.created_at, "dt"), icon="clock"),
            cf("Открытая сделка", alink("deal", t.deal_id, f"#{t.deal_id}"), icon="fire") if t.deal_id else "",
        ),
        "",
        section("support", "Текст"),
        f"<blockquote>{esc(t.text)}</blockquote>",
        *(["", section("pencil", "Ответ"), f"<blockquote>{esc(t.answer)}</blockquote>",
           f"<i>{ulink(answered, t.answered_by)}</i>"] if t.answer else []),
    ]) + note, kb(
        [btn("Ответить", f"atr:{t.id}", "pencil", style="primary"),
         btn("Закрыть", f"atc:{t.id}", "ok") if t.status != "closed" else None],
        [btn(f"Сделка #{t.deal_id}", f"adv:{t.deal_id}", "fire")
         if t.deal_id else None],
        back("atl", "Обращения"),
    ), src)


@router.callback_query(F.data.regexp(r"^atk:(\d+)$"))
async def cb_ticket(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    t = await s.get(Ticket, int(c.data.split(":")[1]))
    if not t:
        return await c.answer("Обращение не найдено", show_alert=True)
    await ticket_screen(bot, s, user, t, c)


@router.callback_query(F.data.regexp(r"^atc:(\d+)$"))
async def cb_ticket_close(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    t = await s.get(Ticket, int(c.data.split(":")[1]), with_for_update=True)
    if t.status != "closed":
        t.status, t.updated_at = "closed", now()
        events.add(s, f"ticket:{t.id}", "closed", f"Закрыто администратором {user.id}", t.user_id)
    await ticket_screen(bot, s, user, t, c, ok("Закрыто"))


@router.callback_query(F.data.regexp(r"^atr:(\d+)$"))
async def cb_ticket_reply(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    t = await s.get(Ticket, int(c.data.split(":")[1]))
    await state.set_state(Ops.reply)
    await state.update_data(ticket=t.id)
    await show(bot, user, f"{title(pe('pencil'), f'Ответ на обращение #{t.id}')}\n\n{esc(t.text[:300])}\n\n"
                          "Отправьте ответ (до 2000 символов). Он придёт пользователю от имени поддержки.",
               kb(back(f"atk:{t.id}", "Отмена")), c)


@router.message(Ops.reply, F.text)
async def msg_ticket_reply(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    tid = (await state.get_data())["ticket"]
    await state.set_state(None)
    t = await s.get(Ticket, tid, with_for_update=True)
    text = m.text[:2000]
    sent = await notify(bot, t.user_id, f"{pe('support')} <b>Ответ поддержки на обращение #{t.id}</b>\n\n{esc(text)}",
                        kb(btn("Ответить", "sup", "support"), back("x", "Скрыть", "cross")))
    t.answer, t.answered_by, t.updated_at = text, user.id, now()
    t.status = "answered" if sent else t.status
    events.add(s, f"ticket:{t.id}", "answer", f"Ответ {user.id}" + ("" if sent else " НЕ доставлен"), t.user_id)
    await ticket_screen(bot, s, user, t, note=ok("Ответ доставлен") if sent
                        else warn("Не доставлен: пользователь заблокировал бота"))


# ---------- CSV reports ----------

@router.callback_query(F.data == "arp")
async def cb_reports(c: CallbackQuery, bot: Bot, user: User):
    await show(bot, user, "\n".join([
        title(pe("doc"), "Отчёты CSV"),
        "",
        "Пришлём три файла: сделки, журнал операций и пополнения/выводы за выбранный период. "
        "Кодировка UTF-8, открываются в Excel и Google Sheets.",
    ]), kb([btn("24 часа", "arp:1", "clock"), btn("7 дней", "arp:7", "clock"), btn("30 дней", "arp:30", "clock")],
           back("a", "Админ-панель")), c)


def _cell(v) -> str:
    if v is None:
        return ""
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v  # text (notes, xRocket errors) must not run as a formula in Excel
    return str(v)


def _csv(name: str, header: list[str], rows) -> BufferedInputFile:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow([_cell(v) for v in r])
    return BufferedInputFile(("﻿" + buf.getvalue()).encode(), filename=name)


@router.callback_query(F.data.regexp(r"^arp:(1|7|30)$"))
async def cb_report(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    days = int(c.data.split(":")[1])
    since = now() - timedelta(days=days)
    await c.answer("Готовим файлы…")
    deal_rows = (await s.execute(select(
        Deal.id, Deal.created_at, Deal.closed_at, Deal.status, Deal.close_reason, Deal.buyer_id, Deal.seller_id,
        Deal.amount_rub, Deal.rate, Deal.seller_debit, Deal.buyer_credit, Deal.platform_fee, Deal.dispute_reason,
    ).where(Deal.created_at >= since).order_by(Deal.id))).all()
    ledger_rows = (await s.execute(select(
        Ledger.id, Ledger.created_at, Ledger.user_id, Ledger.kind, Ledger.ref, Ledger.delta, Ledger.frozen_delta,
        Ledger.note).where(Ledger.created_at >= since).order_by(Ledger.id))).all()
    pay_rows = [("withdrawal", w.id, w.created_at, w.user_id, w.status, w.network or "cheque", w.amount, w.fee,
                 w.amount - w.fee, w.cheque_id or w.tx_hash, w.error)
                for w in (await s.scalars(select(Withdrawal).where(Withdrawal.created_at >= since)))]
    pay_rows += [("deposit", d.id, d.created_at, d.user_id, d.status, d.network or "invoice", d.amount,
                  d.amount - d.credit if d.status == "paid" else Decimal(0), d.credit, d.invoice_id, "")
                 for d in (await s.scalars(select(Deposit).where(Deposit.created_at >= since)))]
    suffix = f"{days}d_{now():%Y%m%d}"
    files = [
        _csv(f"deals_{suffix}.csv", ["id", "created_at", "closed_at", "status", "close_reason", "buyer_id", "seller_id",
                                     "amount_rub", "rate", "seller_debit_usdt", "buyer_credit_usdt", "platform_fee_usdt",
                                     "dispute_reason"], deal_rows),
        _csv(f"ledger_{suffix}.csv", ["id", "created_at", "user_id(empty=platform)", "kind", "ref", "delta_total_usdt",
                                      "delta_frozen_usdt", "note"], ledger_rows),
        _csv(f"payments_{suffix}.csv", ["type", "id", "created_at", "user_id", "status", "network", "amount_usdt", "fee_usdt",
                                        "net_usdt", "xrocket_id", "error"], sorted(pay_rows, key=lambda r: r[2])),
    ]
    chat, topic = files_to(c)
    for f in files:
        await bot.send_document(chat, f, caption=f"{f.filename} · за {days} дн.", message_thread_id=topic)
    audit.log(s, user.id, "report", f"{days}d")


# ---------- admin journal ----------

@router.callback_query(F.data == "aa")
async def cb_audit(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    from bot.handlers.logchat import ACTIONS, target_text
    rows = (await s.scalars(select(Audit).order_by(Audit.id.desc()).limit(20))).all()
    people = {u.id: u for u in (await s.scalars(select(User).where(User.id.in_({r.actor_id for r in rows})))).all()}
    lines = [f"{at(r.created_at, 'dt')} · {ulink(people.get(r.actor_id), r.actor_id)} · "
             f"<b>{ACTIONS.get(r.action, ('', r.action))[1]}</b>"
             + (f" · {await target_text(s, r.target)}" if r.target else "")
             + (f" · <i>{esc(r.details[:60])}</i>" if r.details else "") for r in rows]
    await show(bot, user, "\n".join([
        title(pe("doc"), "Журнал действий админов"),
        "",
        "\n".join(lines) if lines else "<i>Пусто.</i>",
        "",
        quote("Последние 20, новые сверху. В админ-чате каждое действие приходит отдельным постом в тему "
              "«Действия админов»."),
    ]), kb(back("a", "Админ-панель")), c)
