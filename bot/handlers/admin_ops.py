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
from bot.handlers.wallet import notify_withdrawal, parse_usdt
from bot.models import Audit, Deal, Deposit, Event, Ledger, Ticket, TonAddress, TonTransfer, User, Withdrawal, now
from bot.services import admins, audit, events, money, settings, ton
from bot.ui import alink, at, cf, esc, files_to, notify, ok, quote, section, show, title, ulink, warn
from bot.ui import card as fields

router = Router()
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class Ops(StatesGroup):
    reply = State()
    token = State()
    out_addr = State()
    out_amount = State()


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
        *[btn(f"Вывод #{w.id} · {'чек' if w.method == 'xrocket' else 'TON'} · {money.usdt(w.amount)} USDT · "
              f"{WD_LABEL.get(w.status, w.status)}", f"awv:{w.id}", "up",
              style="danger" if w.status in CHECK else None) for w in wds],
        *[btn(f"Пополнение #{d.id} · {money.usdt(d.amount)} USDT · {DEP_LABEL.get(d.status, d.status)}",
              f"adp:{d.id}", "down") for d in deps],
        [btn("Обновить", c.data, "refresh"), back("a", "Админ-панель")],
    ), c)


# ---------- TON: the hot wallet, its balances, the payout queue, the Toncenter key, owner transfers ----------

@router.callback_query(F.data.in_({"aton", "aton:go"}))
async def cb_ton(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.set_state(None)
    note = ""
    if c.data == "aton:go":  # one cycle now: credit, settle, collect, pay the queue
        if ton.chain is None:
            return await c.answer("TON_SEED не задан", show_alert=True)
        await c.answer("Запускаем…")
        from bot.tasks import ton_cycle
        before = (await ton.queue_need(s))[0]
        rep = await ton_cycle(bot)
        left = (await ton.queue_need(s))[0]
        audit.log(s, user.id, "ton_cycle", "", f"зачислено {len(rep.credited)}, очередь {before} → {left}")
        await s.commit()
        note = (ok(f"Цикл выполнен: зачислено поступлений {len(rep.credited)}, завершено выводов {len(rep.finished)}, "
                   f"в очереди {left}") + (warn("; ".join(rep.errors)) if rep.errors else ""))
    await ton_screen(bot, s, user, c, note)


async def ton_screen(bot: Bot, s: AsyncSession, admin: User, src=None, note: str = ""):
    if not ton.enabled():
        return await show(bot, admin, "\n".join([
            title(pe("wallet"), "TON-кошелёк"),
            "",
            warn("TON_SEED не задан в .env — пополнения и выводы выключены."),
            "",
            quote("Создайте ключ: <code>python -m bot.services.ton seed</code>, впишите в .env строкой "
                  "<code>TON_SEED=…</code> и перезапустите бота. Храните копию ключа офлайн: без него средства на "
                  "кошельках бота не достать, а с ним их может вывести кто угодно."),
        ]) + note, kb(back("a", "Админ-панель")), src)
    hot = ton.friendly(ton.hot_address())
    try:
        usdt, gas = await ton.hot_balances(max_age=15)
        status = f"работает · {'testnet' if config.ton_testnet else 'mainnet'}"
        free = usdt - await ton.in_flight_usdt(s)
        balances = f"<b>{money.fmt(gas, 4)} TON</b> (газ) · <b>{money.usdt(usdt)} USDT</b>" + (
            f" · свободно {money.usdt(free)}" if free != usdt else "")
    except Exception as e:  # noqa: BLE001 - shown to the admin as it is
        usdt = free = None
        status, balances = f"<b>нет ответа</b>: {esc(str(e)[:150])}", "неизвестно"
    unswept, addrs = (await s.execute(select(func.coalesce(func.sum(TonAddress.unswept), 0),
                                             func.count(TonAddress.id)))).one()
    queued = (await s.scalars(select(Withdrawal).where(Withdrawal.method == "ton", Withdrawal.status == "queued")
                              .order_by(Withdrawal.id).limit(15))).all()
    n_queued, need = await ton.queue_need(s)
    moving = await s.scalar(select(func.count(Withdrawal.id)).where(Withdrawal.method == "ton",
                                                                    Withdrawal.status.in_(("sending", "sent"))))
    unknown = await s.scalar(select(func.count(Withdrawal.id)).where(Withdrawal.status == "unknown"))
    people = {u.id: u for u in (await s.scalars(select(User).where(User.id.in_({w.user_id for w in queued})))).all()}
    key = ton.api_key()
    owner = admins.is_owner(admin.id)
    await show(bot, admin, "\n".join([
        title(pe("wallet"), "TON-кошелёк · USDT"),
        "",
        fields(cf("Статус", status, icon="info"),
               cf("Ключ Toncenter", f"<code>{ton.hint(key)}</code>" + (f" · {ton.key_source()}" if key else
                                                                         " — лимит 1 запрос/с, задайте ключ"), icon="key"),
               cf("Горячий кошелёк — газ и выплаты", f"<code>{hot}</code>", balances, icon="wallet"),
               cf("На адресах пополнения", f"{money.usdt(Decimal(unswept))} USDT ещё не собрано · адресов {addrs}",
                  f"сбор от {settings.get('ton_sweep_min')} USDT на адресе", icon="down"),
               cf("Очередь выводов", f"<b>{n_queued}</b> на {money.usdt(need)} USDT"
                  + (f" · не хватает <b>{money.usdt(need - free)} USDT</b>" if free is not None and need > free else ""),
                  f"в пути {moving}" + (f" · <b>на проверке {unknown}</b>" if unknown else ""), icon="clock")),
        "\n".join(f"{i}. {alink('wd', w.id, f'#{w.id}')} · {money.usdt(w.amount - w.fee)} USDT · "
                  f"{ulink(people.get(w.user_id), w.user_id)}" for i, w in enumerate(queued, 1)) if queued else "",
        "",
        quote("На горячий кошелёк пополняйте <b>TON</b> — им оплачивается газ всех переводов (держите от "
              f"{ton.HOT_LOW} TON). USDT пользователей собираются на него с личных адресов сами; можно и докинуть USDT "
              "сюда напрямую, если выводам не хватает. Выводы уходят по очереди сами, проверка каждые "
              f"{20} с."),
    ]).replace("\n\n\n", "\n\n") + note, kb(
        btn("Скопировать адрес кошелька", icon="key", copy=hot, style="primary"),
        btn("Запустить цикл сейчас", "aton:go", "refresh", style="success" if n_queued else None),
        btn("Открыть в Tonviewer", url=ton.explorer_address(ton.hot_address()), icon="search"),
        btn("Выводы на проверке", "awl:check", "warn", style="danger") if unknown else None,
        [btn("Ключ Toncenter", "aton:key", "key") if owner else None,
         btn("Вывести с кошелька", "aton:out", "up") if owner else None],
        btn("Пополнения и выводы", "al", "list"),
        back("a", "Админ-панель")), src)


@router.callback_query(F.data == "aton:key")
async def cb_key(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    if not admins.is_owner(user.id):
        return await c.answer("Менять ключ могут только владельцы", show_alert=True)
    await state.set_state(Ops.token)
    await show(bot, user, _key_text(), kb(back("aton", "Отмена")), c)


def _key_text(err: str = "") -> str:
    return "\n".join([
        f"{pe('key')} <b>Ключ Toncenter API</b>",
        "",
        quote("Получите ключ в @tonapibot (бесплатно: 10 запросов/с) и пришлите его следующим сообщением. Бот проверит "
              "его запросом к Toncenter и сразу удалит ваше сообщение. «-» — вернуть ключ из .env (TON_API_KEY)."),
    ]) + (warn(err) if err else "")


@router.message(Ops.token, F.text)
async def msg_key(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    with suppress(TelegramAPIError):
        await m.delete()  # a secret: gone at once, whatever happens next
    key = m.text.strip()
    if not admins.is_owner(user.id):
        await state.set_state(None)
        return await show(bot, user, warn("Менять ключ могут только владельцы"), kb(back("a", "Админ-панель")))
    if key != "-":
        if not 20 <= len(key) <= 200 or any(not (ch.isalnum() or ch in "-_") for ch in key):
            return await show(bot, user, _key_text("Это не похоже на ключ — скопируйте его целиком"),
                              kb(back("aton", "Отмена")))
        try:
            await ton.check_key(key)
        except ton.ChainError as e:
            return await show(bot, user, _key_text(f"Toncenter не принял ключ: {esc(str(e))}"), kb(back("aton", "Отмена")))
    await state.set_state(None)
    old = ton.api_key()
    await settings.put(s, ton.API_KEY, "" if key == "-" else key)
    new = ton.api_key()
    audit.log(s, user.id, "ton_key", "", f"{ton.hint(old)} → {ton.hint(new)}")
    events.add(s, "app:ton", "key", f"Ключ Toncenter сменён ({user.name}): {ton.hint(new)}", user.id, alert=True)
    await s.commit()
    async with ton.busy:  # never swap the client under a running cycle
        await ton.start()
    await ton_screen(bot, s, user, note=ok(f"Ключ принят: {ton.hint(new)}"))


# owner transfers out of the hot wallet: asset -> address -> amount -> confirm

@router.callback_query(F.data.in_({"aton:out", "aton:out:USDT", "aton:out:TON"}))
async def cb_out(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    if not admins.is_owner(user.id):
        return await c.answer("Выводить с кошелька могут только владельцы", show_alert=True)
    if ton.chain is None:
        return await c.answer("TON_SEED не задан", show_alert=True)
    if c.data == "aton:out":
        from bot.services import finance
        sn = await finance.snapshot(s)
        await state.set_state(None)
        return await show(bot, user, "\n".join([
            title(pe("up"), "Вывод с горячего кошелька"),
            "",
            quote(f"• Можно забрать без денег пользователей: <b>{money.usdt(max(sn.free, Decimal(0)))} USDT</b>",
                  "• USDT нужны для выплат пользователям, TON — для газа: оставляйте запас"),
            "Что выводим?",
        ]), kb([btn("USDT", "aton:out:USDT", "dollar"), btn("TON", "aton:out:TON", "wallet")],
               back("aton", "Отмена")), c)
    await state.set_state(Ops.out_addr)
    await state.update_data(out_asset=c.data.split(":")[2])
    await show(bot, user, f"{pe('up')} <b>Вывод {c.data.split(':')[2]} с горячего кошелька</b>\n\nОтправьте адрес "
                          "получателя в сети TON.", kb(back("aton", "Отмена")), c)


@router.message(Ops.out_addr, F.text)
async def msg_out_addr(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    addr = ton.parse_address(m.text)
    if not addr or await ton.is_ours(s, addr):
        return await show(bot, user, warn("Нужен внешний адрес TON (не адрес бота). Отправьте ещё раз."),
                          kb(back("aton", "Отмена")))
    await state.update_data(out_addr=addr)
    await state.set_state(Ops.out_amount)
    asset = (await state.get_data())["out_asset"]
    await show(bot, user, f"Адрес: <code>{esc(addr)}</code>\n\nОтправьте сумму в {asset}.", kb(back("aton", "Отмена")))


@router.message(Ops.out_amount, F.text)
async def msg_out_amount(m: Message, bot: Bot, user: User, state: FSMContext):
    data = await state.get_data()
    v = parse_usdt(m.text)
    if v is None or (data["out_asset"] == "TON" and v.as_tuple().exponent < -9):
        return await show(bot, user, warn("Введите сумму числом"), kb(back("aton", "Отмена")))
    await state.set_state(None)
    await state.update_data(out_amount=str(v))
    await show(bot, user, "\n".join([
        title(pe("up"), "Подтвердите вывод с горячего кошелька"),
        quote(f"• Сумма: <b>{format(v.normalize(), 'f')} {data['out_asset']}</b>",
              f"• Адрес: <code>{esc(data['out_addr'])}</code>"),
        f"{pe('warn')} Перевод в блокчейне не отменить.",
    ]), kb(btn("Отправить", "aton:out:go", "ok", style="danger"), back("aton", "Отмена")))


@router.callback_query(F.data == "aton:out:go")
async def cb_out_go(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    data = await state.get_data()
    await state.update_data(out_amount=None)
    if not admins.is_owner(user.id) or not data.get("out_amount") or not data.get("out_addr"):
        return await c.answer("Заявка устарела — начните заново", show_alert=True)
    await c.answer("Отправляем…")
    amount = Decimal(data["out_amount"]).normalize()
    err = await ton.admin_send(s, user.id, data["out_asset"], data["out_addr"], amount)
    audit.log(s, user.id, "ton_out", "", f"{amount:f} {data['out_asset']} → {data['out_addr']}" + (f": {err}" if err else ""))
    await s.commit()
    await ton_screen(bot, s, user, c, warn(err) if err else ok(f"Отправлено: {amount:f} {data['out_asset']} → "
                                                                f"{ton.short(data['out_addr'])}. Итог — в «Сервис» "
                                                                "админ-чата и в Tonviewer."))


# ---------- withdrawal card ----------

WD_NEXT = {
    "queued": "Списано у пользователя, ждёт отправки по очереди: уйдёт сам, как только горячему кошельку хватит USDT "
              "и TON на газ.",
    "sending": "Сообщение подписано и отправлено в сеть, ждём, когда кошелёк его исполнит. Если сеть не исполнит его "
               "до срока — вывод вернётся в очередь (двойной отправки не будет).",
    "sent": "Кошелёк исполнил перевод, ждём транзакцию USDT в сети (обычно до минуты).",
    "unknown": "Кошелёк исполнил перевод, но транзакция USDT не найдена в сети. Деньги пользователя удержаны, "
               "повтора не будет. Проверьте горячий кошелёк в Tonviewer: нашли перевод — «Подтвердить выполнение», "
               "точно не ушёл — «Вернуть средства».",
    "done": "Выполнен: USDT получены на адрес пользователя.",
    "failed": "Не выполнен, сумма возвращена на баланс.",
    "cancelled": "Отменён пользователем до отправки, сумма возвращена.",
}


async def withdrawal_screen(bot: Bot, s: AsyncSession, admin: User, wd: Withdrawal, src=None, note: str = ""):
    u = await s.get(User, wd.user_id)
    last = await s.scalar(select(Event).where(Event.ref == f"wd:{wd.id}").order_by(Event.id.desc()).limit(1))
    trs = (await s.scalars(select(TonTransfer).where(TonTransfer.ref == f"wd:{wd.id}").order_by(TonTransfer.id))).all()
    legacy = wd.method != "ton"
    where = ("чек xRocket" if wd.method == "xrocket" else f"xRocket · {wd.network or 'TON'}") if legacy else "USDT · TON"
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
            cf("Сеть", *[f"#{t.id} · seqno {t.seqno} · {TR_LABEL.get(t.status, t.status)}"
                         + (f" · tx <code>{t.tx_hash[:16]}…</code>" if t.tx_hash else "") for t in trs],
               f"tx: <code>{esc(wd.tx_hash)}</code>" if wd.tx_hash else "",
               f"ошибка: {esc(wd.error[:200])}" if wd.error else "", icon="key"),
            cf("Последнее событие", f"{esc(last.text[:150])} · {at(last.created_at, 'dt')}", icon="list") if last else "",
        ),
        "",
        quote(WD_NEXT[wd.status]) if wd.status in WD_NEXT and not legacy else
        quote("Вывод времён xRocket, итог неизвестен: проверьте его в приложении xRocket и решите вручную.")
        if legacy and decidable(wd) else quote("Вывод времён xRocket — только история.") if legacy else "",
    ]) + note, kb(
        btn("Проверить в сети", f"wt:{wd.id}", "refresh", style="primary")
        if not legacy and wd.status in ("queued", "sending", "sent", "unknown") else None,
        [btn("Подтвердить выполнение", f"wk:done:{wd.id}", "ok"), btn("Вернуть средства", f"wk:rf:{wd.id}", "cross")]
        if owner and decidable(wd) else None,
        btn("Транзакция", icon="search", url=wd.link) if wd.link else None,
        btn("История вывода", f"aev:wd:{wd.id}", "list"),
        back("al", "Пополнения и выводы"),
    ), src)


def decidable(wd: Withdrawal) -> bool:
    """An owner settles it by hand: TON could not find it on chain, or xRocket was switched off with it open."""
    return wd.status == "unknown" or (wd.method != "ton" and wd.status in ("pending", "sent"))


TR_LABEL = {"sending": "в сети, ждём исполнения", "sent": "исполнено, ждём транзакцию", "done": "выполнено",
            "failed": "отклонено сетью", "expired": "не исполнено (истёк срок)", "unknown": "не найдено в сети"}


@router.callback_query(F.data.regexp(r"^awv:(\d+)$"))
async def cb_withdrawal(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    wd = await s.get(Withdrawal, int(c.data.split(":")[1]), populate_existing=True)
    if not wd:
        return await c.answer("Вывод не найден", show_alert=True)
    await withdrawal_screen(bot, s, user, wd, c)


@router.callback_query(F.data.regexp(r"^wt:(\d+)$"))
async def cb_chain_check(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    """Run the TON cycle now: it settles this withdrawal like every other one."""
    wid = int(c.data.split(":")[1])
    if ton.chain is None:
        return await c.answer("TON_SEED не задан", show_alert=True)
    await c.answer("Проверяем…")
    from bot.tasks import ton_cycle
    rep = await ton_cycle(bot)
    wd = await s.get(Withdrawal, wid, populate_existing=True)
    audit.log(s, user.id, "wd_check", f"wd:{wid}", wd.status)
    await s.commit()
    await withdrawal_screen(bot, s, user, wd, c, (warn("; ".join(rep.errors)) if rep.errors else
                                                  ok(f"Проверено: {WD_LABEL.get(wd.status, wd.status)}")))


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
    if action == "done":
        wd.status = "done"
        if wd.fee:
            money.platform(s, wd.fee, "withdraw_fee", f"wd:{wid}")
        events.add(s, f"wd:{wid}", "done", f"Выполнение подтверждено владельцем {user.id} вручную", wd.user_id, alert=True)
        audit.log(s, user.id, "wd_confirm", f"wd:{wid}", str(wd.amount))
        result = "done"
    else:
        wd.status, wd.error = "failed", f"Возврат владельцем {user.id}: перевод не найден ({where_check(wd)})"
        await money.add(s, wd.user_id, wd.amount, "withdraw_refund", f"wd:{wid}")
        events.add(s, f"wd:{wid}", "refunded", f"{money.usdt(wd.amount)} USDT возвращены владельцем {user.id}: перевод "
                   "не найден в сети", wd.user_id, alert=True)
        audit.log(s, user.id, "wd_refund", f"wd:{wid}", str(wd.amount))
        result = "refunded"
    await s.commit()
    await notify_withdrawal(bot, wd, result)
    await withdrawal_screen(bot, s, user, wd, c, ok("Готово"))


def where_check(wd: Withdrawal) -> str:
    return "в Tonviewer, горячий кошелёк" if wd.method == "ton" else "в приложении xRocket"


# ---------- deposit card ----------

async def deposit_screen(bot: Bot, s: AsyncSession, admin: User, dep: Deposit, src=None, note: str = ""):
    u = await s.get(User, dep.user_id)
    debt = dep.purpose == "debt"
    if dep.tx_hash:
        how = [f"USDT · TON на {'адрес долга' if debt else 'личный адрес'} <code>{esc(dep.address or '—')}</code>",
               f"от <code>{esc(ton.friendly(dep.source))}</code>" if dep.source else ""]
        nxt = {"paid": "Погашено в долг оператора." if debt else "Зачислено на баланс пользователя.",
               "small": f"Меньше минимума {settings.get('deposit_min')} USDT — не зачислено. Если нужно, зачислите "
                        "корректировкой баланса."}.get(dep.status, "")
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
