"""Admin operation cards (withdrawal, deposit), event history, support tickets, CSV reports, alert rendering."""
import csv
import io
from datetime import timedelta
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import config
from bot.emoji import back, btn, kb, pe
from bot.handlers.admin import DEP_LABEL, WD_LABEL
from bot.handlers.wallet import (check_deposit, deposit_done_text, notify_withdrawal, reconcile, send_cheque,
                                 sync_withdrawal)
from bot.models import Audit, Deal, Deposit, Event, Ledger, Ticket, User, Withdrawal, now
from bot.services import audit, events, money, xrocket
from bot.ui import at, esc, notify, ok, quote, show, title, warn

router = Router()
router.message.filter(F.from_user.id.in_(config.admin_ids))
router.callback_query.filter(F.from_user.id.in_(config.admin_ids))


class Ops(StatesGroup):
    reply = State()


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
        return await show(bot, admin, "\n".join([
            title(pe("up"), f"Вывод на кошелёк #{wd.id} · {WD_LABEL.get(wd.status, wd.status)}"),
            quote(
                f"{pe('profile')} Пользователь: {_who(u, wd.user_id)}",
                f"{pe('swap')} Сеть: <b>{xrocket.net_name(wd.network)}</b>",
                f"{pe('wallet')} Списано: <b>{money.usdt(wd.amount)} USDT</b> · к отправке "
                f"{money.usdt(wd.amount - wd.fee)} · комиссия {money.usdt(wd.fee)} (сеть {money.usdt(wd.net_fee)})",
                f"{pe('key')} Адрес: <code>{esc(wd.address or '—')}</code>"
                + (f" · memo <code>{esc(wd.memo)}</code>" if wd.memo else ""),
                f"{pe('clock')} Создан: {at(wd.created_at, 'dt')}" + (f" · отправлен {at(wd.sent_at, 'dt')}" if wd.sent_at else ""),
                f"{pe('key')} clientWithdrawalId: <code>wd-{wd.id}</code> · tx: <code>{esc(wd.tx_hash or '—')}</code>",
                f"{pe('info')} Ошибка: {esc(wd.error[:200])}" if wd.error else "",
                f"{pe('list')} Последнее событие: {esc(last.text[:150])} · {at(last.created_at, 'dt')}" if last else "",
            ),
            {"pending": "Списано, запрос в xRocket ещё не отправлен — фоновая задача отправит его в течение минуты.",
             "unknown": "Ответ xRocket неизвестен — сверим по clientWithdrawalId; если xRocket его не знает, "
                        "запрос повторится с тем же id (двойного вывода не будет).",
             "sent": "xRocket принял вывод, транзакция в пути. Статус проверяется раз в минуту.",
             "done": "Выполнен xRocket.", "failed": "Не выполнен, сумма возвращена на баланс."}.get(wd.status, ""),
        ]) + note, kb(
            btn("Проверить в xRocket", f"wt:{wd.id}", "refresh", style="primary")
            if wd.status in ("pending", "sent", "unknown") else None,
            btn("Транзакция", icon="search", url=wd.link) if wd.link else None,
            [btn("Профиль", f"auv:{wd.user_id}", "profile"), btn("История вывода", f"aev:wd:{wd.id}", "list")],
            back("al", "Пополнения и выводы"),
        ), src)
    await show(bot, admin, "\n".join([
        title(pe("up"), f"Вывод #{wd.id} · {WD_LABEL.get(wd.status, wd.status)}"),
        "",
        quote(
            f"{pe('profile')} Пользователь: {_who(u, wd.user_id)}",
            f"{pe('wallet')} Списано: <b>{money.usdt(wd.amount)} USDT</b>",
            f"{pe('dollar')} Чек: {money.usdt(wd.amount - wd.fee)} USDT · комиссия: {money.usdt(wd.fee)} USDT",
            f"{pe('clock')} Создан: {at(wd.created_at, 'dt')}",
            f"{pe('key')} clientChequeId: <code>wd-{wd.id}</code>" + (f" · chequeId: <code>{esc(wd.cheque_id)}</code>"
                                                                      if wd.cheque_id else ""),
            f"{pe('info')} Ответ xRocket: {esc((wd.error or '—')[:200])}",
            f"{pe('list')} Последнее событие: {esc(last.text[:150])} · {at(last.created_at, 'dt')}" if last else "",
        ),
        WD_NEXT.get(wd.status, ""),
    ]) + note, kb(
        btn("Проверить в xRocket", f"wr:{wd.id}", "refresh", style="primary") if wd.status in ("unknown", "done") else None,
        btn("Вернуть средства", f"wr:rf:{wd.id}", "cross", style="danger") if refund else None,
        btn("Ссылка на чек", url=wd.link, icon="wallet") if wd.link else None,
        [btn("Профиль", f"auv:{wd.user_id}", "profile"), btn("История вывода", f"aev:wd:{wd.id}", "list")],
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
        title(pe("down"), f"Пополнение #{dep.id} · {DEP_LABEL.get(dep.status, dep.status)}"),
        "",
        quote(
            f"{pe('profile')} Пользователь: {_who(u, dep.user_id)}",
            f"{pe('swap')} Адрес {xrocket.net_name(dep.network)}: <code>{esc(dep.address)}</code>" if dep.address
            else f"{pe('wallet')} Счёт-ссылка xRocket",
            f"{pe('dollar')} Сумма: <b>{money.usdt(dep.amount)} USDT</b> · "
            + (f"зачислено: <b>{money.usdt(dep.credit)} USDT</b>" if dep.status == "paid"
               else f"ожидается: {money.usdt(dep.credit)} USDT"),
            f"{pe('clock')} Создан: {at(dep.created_at, 'dt')}",
            f"{pe('key')} clientInvoiceId: <code>dep-{dep.id}</code> · invoiceId: <code>{esc(dep.invoice_id or '—')}</code>",
        ),
        nxt,
    ]) + note, kb(
        btn("Проверить в xRocket", f"dpc:{dep.id}", "refresh", style="primary") if dep.status in ("new", "active") else None,
        [btn("Профиль", f"auv:{dep.user_id}", "profile"), btn("История", f"aev:dep:{dep.id}", "list")],
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
    lines = [f"• {at(e.created_at, 'dt')} · {esc(e.text[:200])}" + (f" {pe('bell')}" if e.alert else "") for e in rows]
    while len("\n".join(lines)) > 3300:  # a long history: the latest events fit into one message
        lines.pop(0)
    await show(bot, user, title(pe("list"), f"История · {name} #{oid}") + "\nПоследние события, новые внизу.\n\n"
               + (quote(*lines) if lines else "Событий нет (операция создана до журнала событий)."),
               kb(back(f"{cb}:{oid}", "Назад")), c)


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
    await show(bot, admin, "\n".join([
        title(pe("support"), f"Обращение #{t.id} · {TICKET_STATUS[t.status]}"),
        "",
        quote(f"{pe('profile')} {_who(u, t.user_id)} · {at(t.created_at, 'dt')}",
              f"{pe('fire')} Открытая сделка: #{t.deal_id}" if t.deal_id else ""),
        esc(t.text),
        *(["", f"<b>Ответ</b> (<code>{t.answered_by}</code>):", esc(t.answer)] if t.answer else []),
    ]) + note, kb(
        [btn("Ответить", f"atr:{t.id}", "pencil", style="primary"),
         btn("Закрыть", f"atc:{t.id}", "ok") if t.status != "closed" else None],
        [btn("Профиль", f"auv:{t.user_id}", "profile"), btn(f"Сделка #{t.deal_id}", f"adv:{t.deal_id}", "fire")
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
    for f in files:
        await bot.send_document(user.id, f, caption=f"{f.filename} · за {days} дн.")
    audit.log(s, user.id, "report", f"{days}d")


# ---------- admin journal ----------

@router.callback_query(F.data == "aa")
async def cb_audit(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    rows = (await s.scalars(select(Audit).order_by(Audit.id.desc()).limit(20))).all()
    lines = [f"{at(r.created_at, 'dt')} <code>{r.actor_id}</code> {esc(r.action)} {esc(r.target)} "
             f"{esc(r.details[:60])}" for r in rows]
    await show(bot, user, title(pe("doc"), "Журнал действий") + "\n\n" + (quote(*lines) if lines else "Пусто"),
               kb(back("a", "Админ-панель")), c)
