"""Background loops: deal timeouts and reminders, xRocket polling, seller auto-offline."""
import asyncio
import logging
from datetime import timedelta

from aiogram import Bot
from sqlalchemy import select, update

from bot.emoji import back, btn, kb, pe
from bot.handlers import logchat
from bot.handlers.deal import REASONS, push
from bot.handlers import admin_chat
from bot.handlers import orders as order_handlers
from bot.handlers import finance as finance_handlers
from bot.handlers import wallet as wallet_handlers
from bot.handlers.wallet import check_deposit, notify_withdrawal, reconcile, sync_withdrawal
from bot.models import Deal, Deposit, Session, User, Withdrawal, now
from bot.services import api, deals, events, money, operators, orders, settings, xrocket
from bot.ui import notify

log = logging.getLogger(__name__)


async def expire_deals(bot: Bot) -> None:
    async with Session() as s:
        ids = (await s.scalars(
            select(Deal.id).where(Deal.status == "waiting_payment", Deal.expires_at < now())
        )).all()
        for did in ids:
            d = await deals.expire(s, did)
            if d:
                events.add(s, f"deal:{d.id}", "expired", "Истёк срок оплаты" + (
                    f", залог продавца удерживается до {d.hold_until.astimezone(deals.MSK):%H:%M} МСК"
                    if d.funds_held else ", заморозка снята"), notice=True)
            await s.commit()
            if d:
                await push(bot, s, d.buyer_id, d, f"Сделка #{d.id}: время на оплату вышло")
                for uid in deals.sellers(d):
                    await push(bot, s, uid, d, f"Сделка #{d.id} отменена: покупатель не оплатил вовремя")


async def release_holds(bot: Bot) -> None:
    """Expired deals without a late receipt: return the held funds to the seller."""
    async with Session() as s:
        ids = (await s.scalars(select(Deal.id).where(
            Deal.status == "expired", Deal.funds_held, Deal.hold_until < now()))).all()
        for did in ids:
            d = await deals.release_hold(s, did)
            if d:
                events.add(s, f"deal:{d.id}", "released", f"Чек не пришёл: {money.usdt(d.seller_debit)} USDT "
                                                          "возвращены продавцу")
            await s.commit()
            if d:
                await push(bot, s, d.seller_id, d, f"Сделка #{d.id}: чек не пришёл, "
                                                   f"{money.usdt(d.seller_debit)} USDT снова доступны")


async def remind_disputes(bot: Bot) -> None:
    """Disputes waiting for an admin too long: remind the log chat (at most every 3 hours per deal)."""
    async with Session() as s:
        edge = now() - timedelta(hours=2)
        rows = (await s.scalars(select(Deal).where(Deal.status == "dispute", Deal.paid_at < edge))).all()
        for d in rows:
            await events.alert_once(s, f"deal:{d.id}", "dispute_waiting",
                                    f"Спор ждёт решения больше 2 ч ({money.fmt(d.amount_rub)} ₽)", minutes=180)
        await s.commit()


async def remind_sellers(bot: Bot) -> None:
    """Once per deal: seller has not confirmed within confirm_minutes -> remind; buyer may now dispute."""
    async with Session() as s:
        edge = now() - timedelta(minutes=settings.num("confirm_minutes"))
        ids = (await s.scalars(update(Deal).where(
            Deal.status == "paid", ~Deal.reminded, Deal.paid_at < edge,
        ).values(reminded=True).returning(Deal.id))).all()
        await s.commit()
        for did in ids:
            d = await s.get(Deal, did)
            if not await push(bot, s, deals.checker(d), d, f"Покупатель ждёт подтверждения по сделке #{d.id}"):
                events.add(s, f"deal:{d.id}", "notify_failed", f"{'Оператор' if d.via_bybit else 'Продавец'} "
                                                               f"{deals.checker(d)} недоступен, сделка ждёт "
                                                               "подтверждения", alert=True)
                await s.commit()
            await push(bot, s, d.buyer_id, d, f"Продавец пока не подтвердил сделку #{d.id} — можно открыть спор")


async def escalate_unanswered_deals(bot: Bot) -> None:
    async with Session() as s:
        edge = now() - timedelta(minutes=settings.num("escalate_minutes"))
        ids = (await s.scalars(select(Deal.id).where(Deal.status == "paid", Deal.paid_at < edge))).all()
        for did in ids:
            d = await deals.escalate_unanswered(s, did)
            if d:
                events.add(s, f"deal:{d.id}", "dispute", f"Автоспор: {REASONS['seller_timeout']}, "
                                                         f"{money.fmt(d.amount_rub)} ₽", alert=True)
            await s.commit()
            if d:
                await push(bot, s, d.buyer_id, d, f"Сделка #{d.id} передана администрации: продавец не ответил")
                await push(bot, s, deals.checker(d), d, f"Сделка #{d.id} передана администрации: вы не ответили вовремя")


async def poll_deposits(bot: Bot) -> None:
    """Every open invoice is checked each run (read-only API calls, xRocket allows it)."""
    async with Session() as s:
        deps = (await s.scalars(select(Deposit).where(Deposit.status.in_(("active", "new")))
                                .order_by(Deposit.id).limit(300))).all()
        for dep in deps:
            try:
                st = await check_deposit(s, dep)
            except xrocket.XRocketError as e:
                log.warning("deposit %s poll: %s", dep.id, e)
                continue
            if st == "credited":
                await notify(bot, dep.user_id, await wallet_handlers.deposit_done_text(s, dep))


async def reconcile_withdrawals(bot: Bot) -> None:
    """Withdrawals with unknown outcome are checked in xRocket by clientChequeId (read-only)."""
    async with Session() as s:
        # 'pending' for minutes = process died between debit and API call: outcome unknown
        await s.execute(update(Withdrawal).where(
            Withdrawal.method == "xrocket", Withdrawal.status == "pending",
            Withdrawal.created_at < now() - timedelta(minutes=5),
        ).values(status="unknown", error="stale pending"))
        await s.commit()
        ids = (await s.scalars(select(Withdrawal.id).where(Withdrawal.method == "xrocket", Withdrawal.status == "unknown")
                               .order_by(Withdrawal.id).limit(20))).all()
        for wid in ids:
            result, wd = await reconcile(s, wid)
            await s.commit()
            if result == "done":
                await notify_withdrawal(bot, wd, "done")
            elif result == "refunded":
                await notify(bot, wd.user_id, f"{pe('warn')} Чек по выводу #{wd.id} отменён. "
                                              f"{money.usdt(wd.amount)} USDT возвращены на баланс.")


async def deliver_alerts(bot: Bot) -> None:
    """Outbox: events are stored with the change; the log chat's cards are created / updated until Telegram
    accepts them (handlers.logchat)."""
    async with Session() as s:
        await logchat.deliver(bot, s)


async def alert_loop(bot: Bot) -> None:
    while True:
        try:
            await deliver_alerts(bot)
        except Exception:
            log.exception("alert delivery failed")
        await events.wait(10)


async def order_timeouts(bot: Bot) -> None:
    """Order requisites: nobody took a request in time -> closed; a merchant did not give requisites (or a Bybit
    link) in time -> his funds are unfrozen and the request goes to the other merchants; no operator gave the
    requisites of a Bybit order in time -> closed, admins alerted."""
    async with Session() as s:
        searching, assigned, checking = await orders.stale(s)
        for did in checking:
            d = await orders.cancel(s, did, "cancelled", "no_merchant")
            if d is None:
                continue
            events.add(s, f"deal:{d.id}", "check_timeout", ("Оператор принял Bybit-ордер, но не выдал реквизиты"
                       if d.operator_id else "Ни один оператор не принял Bybit-ордер") + f" на "
                       f"{money.fmt(d.amount_rub)} ₽ вовремя — заявка закрыта", alert=True)
            if d.operator_id:
                operators.log(s, d.operator_id, d, "timeout", "время на реквизиты вышло, заявка закрыта")
            await s.commit()
            await order_handlers.close_offers(bot, s, d, f"Ордер по заявке #{d.id} закрыт: время вышло",
                                              kinds=("operator",))
            await push(bot, s, d.buyer_id, d, f"Реквизиты под {money.fmt(d.amount_rub)} ₽ не успели выдать. "
                                              "Попробуйте ещё раз")
            if d.operator_id and d.bybit_url and d.seller_id:  # who failed: the merchant or the operator
                await notify(bot, d.operator_id, "\n".join([
                    f"{pe('warn')} <b>Заявка #{d.id} закрыта: реквизиты не выданы вовремя</b>",
                    "",
                    "Мерчант дал реквизиты в своём ордере? Если нет — ему засчитается пропуск "
                    f"({settings.get('strike_limit')} подряд — пауза {settings.human('strike_sleep_hours')})."]),
                    kb([btn("Не дал", f"opq:ans:{d.id}:0", "cross", style="danger"),
                        btn("Дал, не успел я", f"opq:ans:{d.id}:1", "ok")]))
            elif d.operator_id:
                await notify(bot, d.operator_id, f"{pe('warn')} Заявка #{d.id} закрыта: реквизиты не выданы вовремя.")
            if d.seller_id and d.bybit_url:  # an admin's own request has no merchant and no order to cancel
                await notify(bot, d.seller_id, f"{pe('warn')} Заявка #{d.id} закрыта: оператор не успел обработать ваш "
                                               "ордер. Отмените ордер на Bybit.")
        for did in searching:
            d = await orders.cancel(s, did, "cancelled", "no_merchant")
            if d is None:
                continue
            events.add(s, f"deal:{d.id}", "no_merchant", f"Ордерные реквизиты на {money.fmt(d.amount_rub)} ₽ не нашлись",
                       notice=True)
            await s.commit()
            await order_handlers.close_offers(bot, s, d, f"Заявка #{d.id} {order_handlers.CLOSED['expired']}")
            await push(bot, s, d.buyer_id, d, f"Реквизиты под {money.fmt(d.amount_rub)} ₽ не нашлись. "
                                              "Попробуйте другую сумму или повторите позже")
        for did in assigned:
            before = await s.get(Deal, did)
            merchant, bybit = before.seller_id, before.via_bybit
            d = await orders.release(s, did)
            if d is None:
                continue
            events.add(s, f"deal:{d.id}", "released", (f"Мерчант {merchant} не прислал ссылку на Bybit-ордер за "
                       f"{settings.get('order_link_minutes')} мин — заявка не засчитана и передана другим" if bybit else
                       f"Мерчант {merchant} не выдал реквизиты вовремя, заявка передана другим"), alert=not bybit,
                       notice=bybit)
            await s.commit()
            await notify(bot, merchant, (
                f"{pe('warn')} <b>Заявка #{d.id} не засчитана:</b> ссылки на ордер не было "
                f"{settings.get('order_link_minutes')} мин — она ушла другим мерчантам. Берите заявку, когда ордер "
                "под неё уже готов." if bybit else
                f"{pe('warn')} Время на выдачу реквизитов по заявке #{d.id} вышло — она передана другим мерчантам, "
                f"заморозка {money.usdt(d.seller_debit)} USDT снята."))
            await order_handlers.broadcast(bot, s, d)
        # requests still searching reach merchants approved and chats connected since the last send
        for d in (await s.scalars(select(Deal).where(Deal.status == "searching", Deal.expires_at >= now()))).all():
            await order_handlers.broadcast(bot, s, d, first=False)


async def payout_queue(bot: Bot) -> None:
    """Withdrawals waiting for the xRocket app balance go out strictly in order as soon as it covers them."""
    async with Session() as s:
        rows = (await s.scalars(select(Withdrawal).where(Withdrawal.status == "queued")
                                .order_by(Withdrawal.id).limit(50))).all()
        if not rows:
            return
        try:
            funds = await xrocket.usdt_available()
        except Exception as e:  # noqa: BLE001 - xRocket unreachable: try next time
            log.warning("payout queue: balance: %s", e)
            return
        for wd in rows:
            need = await wallet_handlers.payout_need(wd)
            if funds < need:
                waiting = sum(w.amount - w.fee for w in rows if w.status == "queued")
                await events.alert_once(s, "app:xrocket", "payout_queue", f"Выводы ждут пополнения xRocket: "
                                        f"{sum(1 for w in rows if w.status == 'queued')} на {money.usdt(waiting)} USDT, "
                                        f"на балансе {money.usdt(funds)} USDT — пополните приложение", minutes=60)
                await s.commit()
                break  # first in, first out: a big withdrawal is not overtaken by smaller ones
            wd.status = "pending"  # after a crash the usual reconcile of pending withdrawals picks it up
            await s.commit()
            result = await wallet_handlers.pay(s, wd)
            await s.commit()
            if result == "queued":  # the balance dropped meanwhile
                break
            funds -= need
            if result in ("done", "sent"):
                await notify_withdrawal(bot, wd, result)
            elif result != "unknown":
                await notify(bot, wd.user_id, f"{pe('warn')} Вывод #{wd.id} не выполнен: {result}. "
                                              f"{money.usdt(wd.amount)} USDT возвращены на баланс.")


async def stats_topic(bot: Bot) -> None:
    async with Session() as s:
        await finance_handlers.publish(bot, s)


async def channel_autopost(bot: Bot) -> None:
    from bot.handlers import channel
    async with Session() as s:
        await channel.autopost(bot, s)


async def chat_pin(bot: Bot) -> None:
    async with Session() as s:
        await admin_chat.publish_pin(bot, s)


async def sync_chain_withdrawals(bot: Bot) -> None:
    """Withdrawals to addresses are paid by xRocket: follow each one until COMPLETED or FAIL."""
    async with Session() as s:
        rows = (await s.scalars(select(Withdrawal).where(
            Withdrawal.method == "chain",
            (Withdrawal.status.in_(("unknown", "sent")))
            | ((Withdrawal.status == "pending") & (Withdrawal.created_at < now() - timedelta(minutes=2))))
            .order_by(Withdrawal.id).limit(30))).all()
        for wd in rows:
            result = await sync_withdrawal(s, wd)
            await s.commit()
            await notify_withdrawal(bot, wd, result)


WARN_BEFORE = 5  # minutes: a seller about to be taken off shift gets one reminder with a button to stay
_warned: dict[int, object] = {}  # user id -> last_seen the reminder was sent for


async def api_webhooks(bot: Bot) -> None:
    """Merchant API: queue a webhook for every order status change, then deliver due ones."""
    async with Session() as s:
        await api.enqueue_changes(s)
        await api.deliver(s)


async def auto_offline(bot: Bot) -> None:
    minutes = settings.num("online_minutes")
    if not minutes:  # 0 = sellers stay on shift until they leave themselves
        return
    async with Session() as s:
        edge = now() - timedelta(minutes=minutes)
        if minutes > WARN_BEFORE:
            soon = (await s.execute(select(User.id, User.last_seen).where(
                User.is_online, User.last_seen < now() - timedelta(minutes=minutes - WARN_BEFORE),
                User.last_seen >= edge))).all()
            for uid, seen in soon:
                if _warned.get(uid) != seen:
                    _warned[uid] = seen
                    await notify(bot, uid, f"{pe('clock')} <b>Смена завершится через {WARN_BEFORE} мин</b> без "
                                           "активности — карты скроются от покупателей. Остаётесь?",
                                 kb(btn("Остаюсь на смене", "sl:on:1", "live", style="success"), back("x", "Скрыть", "cross")))
        ids = (await s.scalars(
            update(User).where(User.is_online, User.last_seen < edge).values(is_online=False).returning(User.id)
        )).all()
        await s.commit()
    for uid in ids:
        await notify(bot, uid, f"{pe('pause')} <b>Смена завершена</b> из-за {settings.get('online_minutes')} мин "
                               "без активности. Карты скрыты от покупателей, открытые сделки продолжаются. "
                               "Чтобы продолжить — «Продать USDT» → «Выйти на смену».")


async def loop(fn, bot: Bot, every: int) -> None:
    while True:
        try:
            async with Session() as s:
                await settings.load(s)
            await fn(bot)
        except Exception:
            log.exception("task %s failed", fn.__name__)
        await asyncio.sleep(every)


def start(bot: Bot) -> list[asyncio.Task]:
    return [asyncio.create_task(loop(expire_deals, bot, 30)),
            asyncio.create_task(loop(remind_sellers, bot, 60)),
            asyncio.create_task(loop(release_holds, bot, 60)),
            asyncio.create_task(loop(remind_disputes, bot, 1800)),
            asyncio.create_task(loop(escalate_unanswered_deals, bot, 60)),
            asyncio.create_task(loop(poll_deposits, bot, 60)),
            asyncio.create_task(loop(reconcile_withdrawals, bot, 300)),
            asyncio.create_task(loop(auto_offline, bot, 60)),
            asyncio.create_task(alert_loop(bot)),
            asyncio.create_task(loop(api_webhooks, bot, 2)),
            asyncio.create_task(loop(sync_chain_withdrawals, bot, 60)),
            asyncio.create_task(loop(order_timeouts, bot, 20)),
            asyncio.create_task(loop(payout_queue, bot, 30)),
            asyncio.create_task(loop(stats_topic, bot, 600)),
            asyncio.create_task(loop(chat_pin, bot, 300)),
            asyncio.create_task(loop(channel_autopost, bot, 600))]
