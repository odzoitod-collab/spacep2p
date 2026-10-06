"""Admin operation cards (withdrawal, deposit), the BEP-20 cash desk, event history, support tickets, CSV reports."""
import asyncio
import csv
import io
from datetime import timedelta
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.admins import IsAdmin
from bot.emoji import back, btn, kb, pe
from bot.handlers.admin import DEP_LABEL, WD_LABEL
from bot.handlers.wallet import notify_withdrawal
from bot.models import Audit, BscTx, Deal, Deposit, Event, Ledger, Ticket, User, Withdrawal, now
from bot.services import admins, audit, bsc, events, money
from bot.ui import alink, at, cf, esc, files_to, notify, ok, quote, section, show, title, ulink, warn
from bot.ui import card as fields

router = Router()
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class Ops(StatesGroup):
    reply = State()


def _who(u: User | None, uid: int) -> str:
    return f"{esc(u.name or '—')} (<code>{uid}</code>)" if u else f"<code>{uid}</code>"


# ---------- lists ----------

CHECK = ("unknown", "pending")  # withdrawals an admin must look at (pending: an xRocket-era leftover)

@router.callback_query(F.data.in_({"al", "awl:check"}))
async def cb_payments(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    check_only = c.data == "awl:check"
    q = select(Withdrawal).order_by(Withdrawal.status.in_(CHECK).desc(), Withdrawal.id.desc()).limit(10)
    if check_only:
        q = select(Withdrawal).where(Withdrawal.status.in_(CHECK)).order_by(Withdrawal.id).limit(20)
    wds = (await s.scalars(q)).all()
    deps = [] if check_only else (await s.scalars(select(Deposit).order_by(Deposit.id.desc()).limit(8))).all()
    await show(bot, user, "\n".join([
        title(pe("wallet"), "Выводы на проверке" if check_only else "Пополнения и выводы"),
        "",
        "Откройте операцию: карточка показывает всю цепочку и следующее действие. Поиск — «Найти» → "
        "<code>в482</code> / <code>п12</code>.",
    ]) + ("" if wds or deps else f"\n\n{pe('ok')} Пусто"), kb(
        *[btn(f"Вывод #{w.id} · {'BEP-20' if w.method == 'bsc' else 'чек' if w.method == 'xrocket' else 'TON'} · {money.usdt(w.amount)} USDT · "
              f"{WD_LABEL.get(w.status, w.status)}", f"awv:{w.id}", "up",
              style="danger" if w.status in CHECK else None) for w in wds],
        *[btn(f"Пополнение #{d.id} · {money.usdt(d.amount)} USDT · {DEP_LABEL.get(d.status, d.status)}",
              f"adp:{d.id}", "down") for d in deps],
        [btn("Обновить", c.data, "refresh"), back("a", "Админ-панель")],
    ), c)


# ---------- the BEP-20 cash desk: balances, the payout queue, the seed (owners) ----------

@router.callback_query(F.data == "acash")
async def cb_desk(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.set_state(None)
    await desk_screen(bot, s, user, c)


async def desk_screen(bot: Bot, s: AsyncSession, admin: User, src=None, note: str = ""):
    if not bsc.ready():
        return await show(bot, admin, title(pe("wallet"), "Касса USDT · BEP-20") + "\n\n" + warn(
            "Касса выключена" + (f": {esc(bsc.error)}" if bsc.error else " — запускается, обновите через минуту")),
            kb(btn("Обновить", "acash", "refresh"), back("a", "Админ-панель")), src)
    n, need = (await s.execute(select(func.count(Withdrawal.id), func.coalesce(func.sum(
        Withdrawal.amount - Withdrawal.fee), 0)).where(Withdrawal.method == "bsc", Withdrawal.status == "queued"))).one()
    moving = await s.scalar(select(func.count(Withdrawal.id)).where(Withdrawal.method == "bsc",
                                                                   Withdrawal.status.in_(("sending", "sent"))))
    try:
        d = await asyncio.wait_for(bsc.desk(s), 10)
        nums = [cf("USDT свободно", f"<b>{money.usdt(d.usdt)} USDT</b>", icon="dollar"),
                cf("BNB на газ", f"<b>{d.bnb:.5f} BNB</b>" + (" — мало, пополните" if d.bnb < Decimal("0.003") else ""),
                   icon="fire"),
                cf("Холодный кошелёк", f"{money.usdt(d.cold)} USDT", icon="lock") if d.cold is not None else "",
                cf("На адресах пополнения", f"{money.usdt(d.unswept)} USDT — соберутся сами", icon="down")
                if d.unswept else "",
                cf("Касса всего", f"<b>{money.usdt(d.total)} USDT</b>", icon="wallet")]
    except Exception as e:  # noqa: BLE001 - the screen opens anyway
        nums = [cf("Сеть", f"не ответила: {esc(str(e)[:150])}", icon="warn")]
    await show(bot, admin, "\n".join([
        title(pe("wallet"), "Касса USDT · BEP-20"),
        "",
        fields(cf("Горячий кошелёк", f"<code>{bsc.hot}</code>", icon="key"), *nums,
               cf("Выводы", f"в очереди {n} на {money.usdt(Decimal(need))} USDT · в сети {moving}", icon="clock")),
        "",
        quote("Пополнить кассу: USDT и немного BNB (~0,005) на адрес горячего кошелька, сеть BSC (BEP-20). В MetaMask "
              "его можно смотреть, но не отправлять с него руками — займёт очередь транзакций бота."),
    ]) + note, kb(
        btn("Ключ кассы (seed)", "acash:key", "lock", style="danger") if admins.is_owner(admin.id) else None,
        [btn("Скопировать адрес", icon="key", copy=bsc.hot), btn("BscScan", icon="search", url=bsc.address_url(bsc.hot))],
        [btn("Пополнения и выводы", "al", "list"), btn("Обновить", "acash", "refresh")],
        back("a", "Админ-панель")), src)


@router.callback_query(F.data.in_({"acash:key", "acash:key:go"}))
async def cb_desk_key(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    """The seed of the cash desk: owners only, to their private chat, deleted after 2 minutes; asked twice."""
    from bot.handlers.admin_bsc import KEY_TTL, send_key
    if not admins.is_owner(user.id):
        return await c.answer("Только владельцы", show_alert=True)
    if c.data == "acash:key":
        return await show(bot, user, "\n".join([
            title(pe("lock"), "Ключ кассы"),
            "",
            quote("12 слов (seed) — полный доступ ко всем деньгам кассы: горячий кошелёк и все адреса пополнения. "
                  "Импортируйте их в MetaMask, чтобы видеть кассу, и храните офлайн. Никому не пересылайте."),
            f"Пришлю ключ вам в личку, сообщение удалится через {KEY_TTL // 60} мин.",
        ]), kb(btn("Показать ключ", "acash:key:go", "lock", style="danger"), back("acash", "Отмена")), c)
    err = await send_key(bot, s, user.id)
    await c.answer(err or "Ключ в личке с ботом", show_alert=bool(err))
    await desk_screen(bot, s, user, c, warn(err) if err else ok(f"Ключ отправлен в личку, удалится через "
                                                                f"{KEY_TTL // 60} мин"))


# ---------- withdrawal card ----------

WD_NEXT = {
    "queued": "Списано у пользователя, ждёт отправки по очереди: уйдёт сам, как только горячему кошельку хватит USDT "
              "и BNB на газ. Пока ничего не подписано — владелец может снять: /bscq cancel номер.",
    "sending": "Бот готовит транзакцию (несколько секунд).",
    "sent": "Транзакция подписана и в сети, ждём подтверждения блока. Вернуться в очередь вывод может, только если сеть "
            "докажет, что перевод не прошёл — двойной отправки не будет.",
    "unknown": "Вывод старой сети (TON / xRocket) с неизвестным итогом. Проверьте перевод в обозревателе: нашли — "
               "«Подтвердить выполнение», точно не ушёл — «Вернуть средства».",
    "done": "Выполнен: USDT получены на адрес пользователя.",
    "failed": "Не выполнен, сумма возвращена на баланс.",
    "cancelled": "Отменён пользователем до отправки, сумма возвращена.",
}


async def withdrawal_screen(bot: Bot, s: AsyncSession, admin: User, wd: Withdrawal, src=None, note: str = ""):
    u = await s.get(User, wd.user_id)
    last = await s.scalar(select(Event).where(Event.ref == f"wd:{wd.id}").order_by(Event.id.desc()).limit(1))
    trs = (await s.scalars(select(BscTx).where(BscTx.kind == "withdrawal", BscTx.ref_id == wd.id)
                           .order_by(BscTx.id))).all()
    legacy = wd.method != "bsc"
    where = "USDT · BEP-20" if not legacy else "чек xRocket" if wd.method == "xrocket" else \
        f"{'TON' if wd.method == 'ton' else 'xRocket · ' + (wd.network or 'TON')} (история)"
    owner = admins.is_owner(admin.id)
    await show(bot, admin, "\n".join([
        title(pe("up"), f"Вывод {alink('wd', wd.id, f'#{wd.id}')}") + f" · {WD_LABEL.get(wd.status, wd.status)}",
        "",
        fields(
            cf("Пользователь", ulink(u, wd.user_id), icon="profile"),
            cf("Сумма", f"Списано: <b>{money.usdt(wd.amount)} USDT</b>",
               f"к отправке {money.usdt(wd.amount - wd.fee)} · комиссия {money.usdt(wd.fee)}", icon="wallet"),
            cf("Куда", where, f"<code>{esc(wd.address or '—')}</code>" if wd.address else "",
               f"memo <code>{esc(wd.memo)}</code>" if wd.memo else "", icon="swap"),
            cf("Когда", f"создан {at(wd.created_at, 'dt')}" + (f" · отправлен {at(wd.sent_at, 'dt')}" if wd.sent_at else ""),
               icon="clock"),
            cf("Сеть", *[f"nonce {t.nonce} · {TR_LABEL.get(t.status, t.status)} · tx <code>{t.tx_hash[:16]}…</code>"
                         for t in trs],
               f"tx: <code>{esc(wd.tx_hash)}</code>" if wd.tx_hash else "",
               f"ошибка: {esc(wd.error[:200])}" if wd.error else "", icon="key"),
            cf("Последнее событие", f"{esc(last.text[:150])} · {at(last.created_at, 'dt')}", icon="list") if last else "",
        ),
        "",
        quote(WD_NEXT[wd.status]) if wd.status in WD_NEXT and (not legacy or decidable(wd)) else
        quote("Вывод старой сети — только история.") if legacy else "",
    ]) + note, kb(
        [btn("Подтвердить выполнение", f"wk:done:{wd.id}", "ok"), btn("Вернуть средства", f"wk:rf:{wd.id}", "cross")]
        if owner and decidable(wd) else None,
        btn("Транзакция", icon="search", url=wd.link) if wd.link else None,
        btn("История вывода", f"aev:wd:{wd.id}", "list"),
        back("al", "Пополнения и выводы"),
    ), src)


def decidable(wd: Withdrawal) -> bool:
    """An owner settles it by hand: a withdrawal of a network that is gone (TON, xRocket) left with an open outcome.
    A BEP-20 one never: the chain settles it."""
    return wd.method != "bsc" and wd.status in ("unknown", "pending", "sent", "sending")


TR_LABEL = {"pending": "в сети, ждём блок", "confirmed": "подтверждена", "reverted": "откатилась, USDT не ушли",
            "replaced": "nonce занят другой транзакцией"}


@router.callback_query(F.data.regexp(r"^awv:(\d+)$"))
async def cb_withdrawal(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    wd = await s.get(Withdrawal, int(c.data.split(":")[1]), populate_existing=True)
    if not wd:
        return await c.answer("Вывод не найден", show_alert=True)
    await withdrawal_screen(bot, s, user, wd, c)


@router.callback_query(F.data.regexp(r"^wk:(done|rf)(2?):(\d+)$"))
async def cb_unknown(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    """An owner settles a withdrawal the chain could not: confirmed paid, or refunded. Asked twice."""
    head, wid = c.data.split(":")[1], int(c.data.split(":")[2])
    action, sure = head.rstrip("2"), head.endswith("2")
    if not admins.is_owner(user.id):
        return await c.answer("Только владельцы", show_alert=True)
    wd = await s.get(Withdrawal, wid, with_for_update=True, populate_existing=True)
    if not wd or not decidable(wd):
        return await c.answer("Вывод уже обработан", show_alert=True)
    if not sure:
        return await show(bot, user, "\n".join([
            title(pe("warn"), f"Вывод #{wid}: {'подтвердить выполнение' if action == 'done' else 'вернуть средства'}?"),
            quote(f"Подтвердите, только если нашли перевод USDT пользователю ({where_check(wd)}): вывод станет "
                  "выполненным, комиссия — доходом площадки." if action == "done" else
                  f"Верните, только если убедились ({where_check(wd)}), что USDT НЕ ушли: иначе пользователь "
                  f"получит {money.usdt(wd.amount)} USDT дважды."),
        ]), kb(btn("Да, подтверждаю", f"wk:{action}2:{wid}", "ok", style="danger"), back(f"awv:{wid}", "Отмена")), c)
    await settle(bot, s, user, wd, action)
    await withdrawal_screen(bot, s, user, wd, c, ok("Готово"))


async def settle(bot: Bot, s: AsyncSession, user: User, wd: Withdrawal, action: str) -> str:
    """An owner's decision on a decidable withdrawal (locked by the caller): done | refund. Commits, tells the user."""
    wid = wd.id
    if action == "done":
        wd.status = "done"
        if wd.fee:
            money.platform(s, wd.fee, "withdraw_fee", f"wd:{wid}")
        events.add(s, f"wd:{wid}", "done", f"Выполнение подтверждено владельцем {user.id} вручную", wd.user_id, notice=True)
        audit.log(s, user.id, "wd_confirm", f"wd:{wid}", str(wd.amount))
        result = "done"
    else:
        wd.status, wd.error = "failed", f"Возврат владельцем {user.id}: перевод не найден ({where_check(wd)})"
        await money.add(s, wd.user_id, wd.amount, "withdraw_refund", f"wd:{wid}")
        events.add(s, f"wd:{wid}", "refunded", f"{money.usdt(wd.amount)} USDT возвращены владельцем {user.id}: перевод "
                   "не найден в сети", wd.user_id, notice=True)
        audit.log(s, user.id, "wd_refund", f"wd:{wid}", str(wd.amount))
        result = "refunded"
    await s.commit()
    await notify_withdrawal(bot, wd, result)
    return result


def where_check(wd: Withdrawal) -> str:
    return "в Tonviewer, горячий TON-кошелёк" if wd.method == "ton" else "в приложении xRocket"


# ---------- deposit card ----------

async def deposit_screen(bot: Bot, s: AsyncSession, admin: User, dep: Deposit, src=None, note: str = ""):
    u = await s.get(User, dep.user_id)
    debt = dep.purpose == "debt"
    if dep.tx_hash:
        net = "BEP-20" if dep.network == "BEP20" else "TON (история)"
        how = [f"USDT · {net} на {'адрес долга' if debt else 'личный адрес'} <code>{esc(dep.address or '—')}</code>",
               f"от <code>{esc(dep.source)}</code>" if dep.source else ""]
        nxt = {"paid": "Погашено в долг оператора." if debt else "Зачислено на баланс пользователя.",
               "small": "Меньше минимума — не зачислено. Если нужно, зачислите корректировкой баланса."}.get(
            dep.status, "")
    else:
        how, nxt = [f"счёт xRocket {esc(dep.invoice_id or '—')}" + (f" · {esc(dep.network)}" if dep.network else "")], \
            "Пополнение времён xRocket — только история."
    await show(bot, admin, "\n".join([
        title(pe("down"), f"Пополнение {alink('dep', dep.id, f'#{dep.id}')}") + f" · {DEP_LABEL.get(dep.status, dep.status)}",
        "",
        fields(
            cf("Пользователь", ulink(u, dep.user_id), icon="profile"),
            cf("Способ", *how, icon="swap"),
            cf("Сумма", f"пришло <b>{money.usdt(dep.amount)} USDT</b>",
               (f"погашено долга {money.usdt(dep.credit)}" if debt else f"зачислено <b>{money.usdt(dep.credit)} USDT</b>")
               if dep.status == "paid" else "", icon="dollar"),
            cf("Когда", at(dep.created_at, "dt"), icon="clock"),
            cf("Транзакция", f"<code>{esc(dep.tx_hash)}</code>", icon="key") if dep.tx_hash else "",
        ),
        "",
        quote(nxt) if nxt else "",
    ]) + note, kb(
        btn("Транзакция", icon="search", url=dep.link) if dep.link and dep.tx_hash else None,
        btn("История", f"aev:dep:{dep.id}", "list"),
        back("al", "Пополнения и выводы"),
    ), src)


@router.callback_query(F.data.regexp(r"^adp:(\d+)$"))
async def cb_deposit(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    dep = await s.get(Deposit, int(c.data.split(":")[1]), populate_existing=True)
    if not dep:
        return await c.answer("Пополнение не найдено", show_alert=True)
    await deposit_screen(bot, s, user, dep, c)


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
                 w.amount - w.fee, w.tx_hash or w.cheque_id, w.error)
                for w in (await s.scalars(select(Withdrawal).where(Withdrawal.created_at >= since)))]
    pay_rows += [("deposit" if d.purpose == "deposit" else "debt_repay", d.id, d.created_at, d.user_id, d.status,
                  d.network or "invoice", d.amount, d.amount - d.credit if d.status == "paid" else Decimal(0), d.credit,
                  d.tx_hash or d.invoice_id, "")
                 for d in (await s.scalars(select(Deposit).where(Deposit.created_at >= since)))]
    suffix = f"{days}d_{now():%Y%m%d}"
    files = [
        _csv(f"deals_{suffix}.csv", ["id", "created_at", "closed_at", "status", "close_reason", "buyer_id", "seller_id",
                                     "amount_rub", "rate", "seller_debit_usdt", "buyer_credit_usdt", "platform_fee_usdt",
                                     "dispute_reason"], deal_rows),
        _csv(f"ledger_{suffix}.csv", ["id", "created_at", "user_id(empty=platform)", "kind", "ref", "delta_total_usdt",
                                      "delta_frozen_usdt", "note"], ledger_rows),
        _csv(f"payments_{suffix}.csv", ["type", "id", "created_at", "user_id", "status", "network", "amount_usdt", "fee_usdt",
                                        "net_usdt", "tx_hash", "error"], sorted(pay_rows, key=lambda r: r[2])),
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
