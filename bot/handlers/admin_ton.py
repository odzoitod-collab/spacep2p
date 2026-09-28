"""Admin panel: USDT on TON — auto-transfer address, gas wallet, deposits and transfers; TON withdrawal checks."""
import asyncio
from datetime import timedelta
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import config
from bot.emoji import back, btn, kb, pe
from bot.models import TonDeposit, TonOp, TonWallet, User, Withdrawal, now
from bot.services import audit, events, money, settings, ton
from bot.ui import at, esc, ok, quote, show, title, warn

router = Router()
router.message.filter(F.from_user.id.in_(config.admin_ids))
router.callback_query.filter(F.from_user.id.in_(config.admin_ids))


def _who(u: User | None, uid: int) -> str:
    return f"{esc(u.name or '—')} (<code>{uid}</code>)" if u else f"<code>{uid}</code>"


OP_STATUS = {"sending": "отправляется", "sent": "в сети", "done": "выполнен", "failed": "ошибка"}


def _hash_link(h: str | None, label: str) -> str:
    return f'<a href="{ton.explorer(h)}">{label}</a>' if h else "—"


@router.callback_query(F.data.in_({"atn", "atn:run"}))
async def cb_ton(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    note = ""
    if c.data == "atn:run" and ton.enabled():
        try:
            found = await ton.scan(s)
            await ton.sweep(s)
            note = ok(f"Проверено: новых пополнений {len(found)}. Автоперевод запущен для всех адресов с USDT.")
        except ton.ChainError as e:
            note = warn(f"Сеть TON не ответила: {esc(str(e)[:150])}. Повторите позже.")
        audit.log(s, user.id, "ton_run")
    await ton_admin_screen(bot, s, user, c, note)


def _gas_status(gas: Decimal | None) -> tuple[str, str]:
    """(badge, words) for the gas wallet balance."""
    if gas is None:
        return "⚪️", "баланс недоступен — сеть TON не ответила"
    ops = int(gas / ton.SWEEP_FEE)
    if gas < ton.GAS_TOPUP + Decimal("0.02"):
        return "🔴", f"{money.fmt(gas, 4)} TON — пусто, автопереводы стоят"
    if gas < ton.GAS_LOW:
        return "🟠", f"{money.fmt(gas, 4)} TON — мало, хватит на ~{ops} автопереводов"
    return "🟢", f"{money.fmt(gas, 4)} TON — хватит на ~{ops} автопереводов"


async def _gas() -> Decimal | None:
    try:
        return await asyncio.wait_for(ton.chain.ton_balance(ton.gas_address()), 8)
    except Exception:  # noqa: BLE001 - the screen must open even if the API is down
        return None


def target_words(target: str) -> str:
    if target == ton.XROCKET:
        return "на баланс приложения xRocket"
    return f"на адрес <code>{esc(target)}</code>" if target else "<b>не задан</b> — USDT копятся на адресах пользователей"


async def ton_admin_screen(bot: Bot, s: AsyncSession, admin: User, src=None, note: str = ""):
    if not ton.enabled():
        return await show(bot, admin, "\n".join([
            title(pe("wallet"), "USDT в сети TON · выключено"),
            "",
            "Чтобы включить пополнение USDT TON с личным адресом у каждого пользователя:",
            quote("1. Сгенерируйте секрет: <code>python -c \"import secrets; print(secrets.token_hex(32))\"</code>",
                  "2. Впишите его в .env: <code>TON_SEED=…</code> и (желательно) <code>TON_API_KEY</code> от @tonapibot",
                  "3. Перезапустите бота и выберите здесь, куда переводить поступления."),
            "TON_SEED — ключ ко всем адресам пополнения. Храните копию офлайн и никому не передавайте.",
        ]), kb(back("a", "Админ-панель")), src)
    day = now() - timedelta(hours=24)
    n24, sum24 = (await s.execute(select(func.count(TonDeposit.id), func.coalesce(func.sum(TonDeposit.amount), 0))
                                  .where(TonDeposit.created_at > day))).one()
    waiting = await s.scalar(select(func.count()).where(TonWallet.need_sweep))
    target = settings.get("ton_sweep_address")
    badge, gas_words = _gas_status(await _gas())
    await show(bot, admin, "\n".join([
        title(pe("wallet"), "USDT в сети TON"),
        "",
        "🟢 Пополнения включены: у каждого пользователя свой адрес",
        f"{'🟢' if target else '🟠'} Автоперевод поступлений: {target_words(target)}",
        f"{badge} Газ: {gas_words}",
        f"Ждут автоперевода: <b>{waiting}</b> адр. · за 24 ч пополнений: <b>{n24}</b> на "
        f"<b>{money.usdt(Decimal(sum24))} USDT</b>",
        "" if config.ton_api_key else "\n⚠️ Нет TON_API_KEY: Toncenter пускает 1 запрос в секунду, всё работает "
                                      "медленно. Ключ — в @tonapibot, строка TON_API_KEY в .env.",
    ]) + note, kb(
        [btn("Автоперевод", "atn:sw"), btn("Газ", "atn:gas")],
        [btn("Пополнения", "atnd"), btn("Автопереводы", "atno")],
        btn("Проверить сейчас", "atn:run"),
        back("a", "Админ-панель"),
    ), src)


async def ton_sweep_screen(bot: Bot, s: AsyncSession, admin: User, src=None, note: str = ""):
    target = settings.get("ton_sweep_address")
    swept = await s.scalar(select(func.coalesce(func.sum(TonOp.amount), 0)).where(TonOp.kind == "sweep",
                                                                                   TonOp.status == "done"))
    await show(bot, admin, "\n".join([
        title(pe("up"), "Автоперевод поступлений"),
        "",
        f"Сейчас: {target_words(target)}",
        f"Переводим с адреса пользователя от <b>{settings.get('ton_sweep_min')} USDT</b> · уже переведено "
        f"<b>{money.usdt(Decimal(swept))} USDT</b>",
        "",
        "<b>Мой кошелёк</b> — поступившие USDT уходят на ваш кошелёк USDT в сети TON (Tonkeeper, Telegram Wallet, "
        "биржа). Нажмите и отправьте адрес — он сохранится, каждая смена приходит алертом всем админам.",
        "<b>Баланс xRocket</b> — поступления сразу пополняют приложение, из которого бот платит выводы; на каждый "
        "перевод создаётся счёт xRocket, xRocket может удержать комиссию за приём.",
    ]) + note, kb(
        btn("Мой кошелёк" + (" ✓" if target and target != ton.XROCKET else ""), "asr:ton_sweep_address",
            style=None if target and target != ton.XROCKET else "success"),
        btn("Баланс xRocket" + (" ✓" if target == ton.XROCKET else ""), "atn:x"),
        [btn("Мин. сумма", "asr:ton_sweep_min"), btn("Остановить", "atn:stop") if target else None],
        back("atn", "USDT в сети TON"),
    ), src)


async def ton_gas_screen(bot: Bot, s: AsyncSession, admin: User, src=None, note: str = ""):
    gas = await _gas()
    badge, words = _gas_status(gas)
    addr = ton.friendly(ton.gas_address())
    waiting_gas = await s.scalar(select(func.count(TonOp.id)).where(TonOp.kind == "gas", TonOp.status.in_(
        ("sending", "sent"))))
    await show(bot, admin, "\n".join([
        title(pe("ruble"), "Газ для автопереводов"),
        "",
        f"{badge} {words}",
        "Адрес газ-кошелька бота:",
        f"<code>{addr}</code>",
        "",
        "<b>Как пополнить</b>",
        "1. Скопируйте адрес кнопкой ниже.",
        "2. В Telegram Wallet или Tonkeeper: «Отправить» → <b>TON</b> (сеть TON) → вставьте адрес.",
        "3. Сумма: держите от 1 TON — первый перевод с нового адреса стоит ~0,1 TON, следующие ~0,05 TON; "
        "неизрасходованное возвращается сюда же.",
        "xRocket не умеет отправлять TON через API, поэтому газ пополняется переводом с вашего кошелька.",
        f"Газ сейчас отправляется на адреса пользователей: {waiting_gas}" if waiting_gas else "",
    ]) + note, kb(
        btn("Скопировать адрес", copy=addr, style="primary"),
        [btn("Обновить баланс", "atn:gas"), btn("Tonviewer", url=ton.address_url(addr))],
        back("atn", "USDT в сети TON"),
    ), src)


@router.callback_query(F.data.in_({"atn:sw", "atn:gas", "atn:x", "atn:stop"}))
async def cb_ton_parts(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    if not ton.enabled():
        return await ton_admin_screen(bot, s, user, c)
    if c.data == "atn:gas":
        return await ton_gas_screen(bot, s, user, c)
    note = ""
    if c.data in ("atn:x", "atn:stop"):
        old = settings.get("ton_sweep_address")
        new = ton.XROCKET if c.data == "atn:x" else ""
        if old != new:
            await settings.put(s, "ton_sweep_address", new)
            audit.log(s, user.id, "setting", "ton_sweep_address", f"{old} → {new}")
            events.add(s, "app:ton", "target_changed", f"Адрес автоперевода USDT TON изменён администратором "
                       f"{user.name} ({user.id}): {old or '—'} → {new or 'остановлен'}", user.id, alert=True)
        note = ok("Поступления переводятся на баланс xRocket" if new else "Автоперевод остановлен")
    await ton_sweep_screen(bot, s, user, c, note)


@router.callback_query(F.data.regexp(r"^wt:(\d+)$"))
async def cb_ton_payout_check(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    """Ask xRocket about a withdrawal to a TON wallet right now."""
    from bot.handlers.admin_ops import withdrawal_screen
    from bot.handlers.ton_wallet import notify_withdrawal, sync_withdrawal
    wd = await s.get(Withdrawal, int(c.data.split(":")[1]), with_for_update=True, populate_existing=True)
    if not wd or wd.method != "ton":
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


@router.callback_query(F.data == "atnd")
async def cb_ton_deposits(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    rows = (await s.scalars(select(TonDeposit).order_by(TonDeposit.id.desc()).limit(15))).all()
    await show(bot, user, title(pe("down"), "Пополнения USDT TON") + ("\n\nНовые сверху." if rows else "\n\nПока нет"),
               kb(*[btn(f"#{d.id} · +{money.usdt(d.amount)} USDT · {d.user_id}", f"atd:{d.id}", "down") for d in rows],
                  back("atn", "USDT TON")), c)


@router.callback_query(F.data.regexp(r"^atd:(\d+)$"))
async def cb_ton_deposit(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    d = await s.get(TonDeposit, int(c.data.split(":")[1]))
    if not d:
        return await c.answer("Пополнение не найдено", show_alert=True)
    u = await s.get(User, d.user_id)
    w = await s.get(TonWallet, d.user_id)
    op = await s.scalar(select(TonOp).where(TonOp.user_id == d.user_id, TonOp.kind == "sweep",
                                            TonOp.created_at >= d.created_at).order_by(TonOp.id).limit(1))
    sweep = (f"автоперевод #{op.id}: {OP_STATUS.get(op.status, op.status)}" if op
             else "ждёт автоперевода" if w and w.need_sweep else "—")
    await show(bot, user, "\n".join([
        title(pe("down"), f"Пополнение USDT TON #{d.id}"),
        "",
        quote(f"{pe('profile')} Пользователь: {_who(u, d.user_id)}",
              f"{pe('dollar')} Зачислено: <b>{money.usdt(d.amount)} USDT</b>",
              f"{pe('clock')} Транзакция: {at(d.tx_time, 'dt')} · зачислено {at(d.created_at, 'dt')}",
              f"{pe('key')} Хеш: <code>{d.tx_hash}</code>",
              f"{pe('down')} Отправитель: <code>{ton.friendly(d.source) if d.source else '—'}</code>",
              f"{pe('wallet')} Адрес пользователя: <code>{ton.friendly(w.address) if w else '—'}</code>",
              f"{pe('up')} Дальше: {sweep}"),
    ]), kb(btn("Tonviewer", icon="search", url=ton.explorer(d.tx_hash)),
           [btn("Профиль", f"auv:{d.user_id}", "profile"), btn("История", f"aev:tdep:{d.id}", "list")],
           back("atnd", "Пополнения")), c)


@router.callback_query(F.data == "atno")
async def cb_ton_ops(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    rows = (await s.scalars(select(TonOp).order_by(TonOp.id.desc()).limit(15))).all()
    await show(bot, user, title(pe("up"), "Автопереводы и газ") + "\n\n" + (
        "Газ — TON с газ-кошелька на адрес пользователя перед автопереводом.\n" + quote(*[
            f"#{o.id} · {'автоперевод' if o.kind == 'sweep' else 'газ'} "
            f"{money.usdt(o.amount) + ' USDT' if o.kind == 'sweep' else money.fmt(o.amount, 3) + ' TON'} · "
            f"{o.user_id} · {OP_STATUS.get(o.status, o.status)} · {at(o.created_at, 'dt')} · "
            + _hash_link(o.tx_hash, "tx") + (f" · msg <code>{o.msg_hash[:10]}…</code>" if o.msg_hash else "")
            for o in rows]) if rows else "Пока нет"),
        kb(*[btn(f"Автоперевод #{o.id} · {money.usdt(o.amount)} USDT", f"ato:{o.id}", "up") for o in rows
             if o.kind == "sweep"][:8], back("atn", "USDT TON")), c)


@router.callback_query(F.data.regexp(r"^ato:(\d+)$"))
async def cb_ton_op(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    o = await s.get(TonOp, int(c.data.split(":")[1]))
    if not o:
        return await c.answer("Операция не найдена", show_alert=True)
    await show(bot, user, "\n".join([
        title(pe("up"), f"Автоперевод #{o.id} · {OP_STATUS.get(o.status, o.status)}"),
        "",
        quote(f"{pe('dollar')} Сумма: <b>{money.usdt(o.amount)} USDT</b> с адреса пользователя <code>{o.user_id}</code>",
              f"{pe('wallet')} Куда: " + ("баланс xRocket" if o.to_address == ton.XROCKET
                                          else f"<code>{ton.friendly(o.to_address)}</code>"),
              f"{pe('clock')} Создан: {at(o.created_at, 'dt')}" + (f" · выполнен {at(o.done_at, 'dt')}" if o.done_at else ""),
              f"{pe('key')} Хеш транзакции: <code>{o.tx_hash or '—'}</code>",
              f"{pe('key')} Хеш сообщения: <code>{o.msg_hash or '—'}</code>",
              f"{pe('warn')} Ошибка: {esc(o.error)}" if o.error else ""),
    ]), kb(btn("Tonviewer", icon="search", url=ton.explorer(o.tx_hash or o.msg_hash))
           if o.tx_hash or o.msg_hash else None,
           [btn("Профиль", f"auv:{o.user_id}", "profile"), btn("История", f"aev:tsw:{o.id}", "list")],
           back("atno", "Автопереводы")), c)
