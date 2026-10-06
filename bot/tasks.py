"""Background loops: deal timeouts and reminders, the BEP-20 cash desk, seller auto-offline."""
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
from bot.models import Deal, Session, User, now
from bot.services import api, bsc, deals, events, money, operators, orders, settings
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
        # a working operator's Bybit deal is never sent to a dispute by the clock: he confirms or opens it himself;
        # one whose operator is gone goes to the administration as usual
        working = await operators.ids(s)
        ids = (await s.scalars(select(Deal.id).where(Deal.status == "paid", Deal.paid_at < edge, ~(
            Deal.via_bybit & Deal.operator_id.in_(working or [0]))))).all()
        for did in ids:
            d = await deals.escalate_unanswered(s, did)
            if d:
                events.add(s, f"deal:{d.id}", "dispute", f"Автоспор: {REASONS['seller_timeout']}, "
                                                         f"{money.fmt(d.amount_rub)} ₽", alert=True)
            await s.commit()
            if d:
                await push(bot, s, d.buyer_id, d, f"Сделка #{d.id} передана администрации: продавец не ответил")
                await push(bot, s, deals.checker(d), d, f"Сделка #{d.id} передана администрации: вы не ответили вовремя")


BSC_EVERY = 3
BSC_START = 25  # seconds after the start before the BEP-20 desk loads its seed


async def bsc_tick(bot: Bot, n: int = 0) -> bsc.Report:
    """USDT BEP-20: one round — incoming, the payout queue, settling what is in flight — then tell the users."""
    async with Session() as s:
        if not bsc.ready():
            if n % 20 == 0:  # the seed could not be loaded (database down...): try again every minute
                await bsc.ensure_wallet(s)
            return bsc.Report()
        rep = await bsc.tick(s, n)
        for dep in rep.credited:
            await wallet_handlers.notify_deposit(bot, s, dep)
        for wd, result in rep.finished:
            await wallet_handlers.notify_withdrawal(bot, wd, result)
    return rep


async def bsc_loop(bot: Bot) -> None:
    """Every BSC_EVERY seconds, or at once when a withdrawal is queued."""
    await asyncio.sleep(BSC_START)
    n = 0
    while True:
        try:
            async with Session() as s:
                await settings.load(s)
            await bsc_tick(bot, n)
        except Exception:
            log.exception("bsc tick failed")
        n += 1
        try:
            await asyncio.wait_for(bsc.wake.wait(), BSC_EVERY)
        except asyncio.TimeoutError:
            pass
        bsc.wake.clear()


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
            merchant, bybit, operator = before.seller_id, before.via_bybit, before.operator_id
            d = await orders.release(s, did)
            if d is None:
                continue
            if bybit:  # took a request without a ready card: it counts against his reputation
                await orders.auto_score(s, d, merchant, orders.SCORE_LATE_LINK)
            if operator:
                await notify(bot, operator, f"{pe('info')} Мерчант не прислал новый ордер по заявке #{d.id} вовремя — "
                                            "она снова ищет мерчанта.")
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


async def stats_topic(bot: Bot) -> None:
    async with Session() as s:
        await finance_handlers.publish(bot, s)


async def chat_posts(bot: Bot) -> None:
    """The requests' posts in the chats follow their status: taken, link received, requisites, paid, done."""
    from bot.models import OrderOffer
    async with Session() as s:
        ids = (await s.scalars(select(Deal.id).where(
            Deal.id.in_(select(OrderOffer.deal_id).where(OrderOffer.kind == "chat")),
            Deal.created_at > now() - timedelta(days=1),
            Deal.status.in_(deals.OPEN) | (Deal.closed_at > now() - timedelta(hours=1))))).all()
        for did in ids:
            await order_handlers.sync_chat_posts(bot, s, await s.get(Deal, did))


async def channel_autopost(bot: Bot) -> None:
    from bot.handlers import channel
    async with Session() as s:
        await channel.autopost(bot, s)


async def chat_pin(bot: Bot) -> None:
    async with Session() as s:
        await admin_chat.publish_pin(bot, s)


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


HELD_REMIND = timedelta(hours=1)  # requisites given, no receipt: the operator is reminded once
HELD_ALERT = timedelta(hours=6)  # still nothing: the administration looks at it


async def held_watch(bot: Bot) -> None:
    """An operator's deal has no deadline, so it must not hang unnoticed: an hour after the requisites without a
    receipt the operator is reminded (once), after six hours the admins are alerted."""
    async with Session() as s:
        rows = (await s.scalars(select(Deal).where(Deal.status == "waiting_payment", Deal.via_bybit,
                                                   Deal.operator_id.is_not(None)))).all()
        remind = []
        for d in rows:
            given = deals.aware(d.expires_at) - deals.HOLD  # the requisites were given with expires = now + HOLD
            waited = now() - given
            if waited > HELD_REMIND and not d.reminded:
                d.reminded = True
                remind.append(d)
            if waited > HELD_ALERT:
                await events.alert_once(s, f"deal:{d.id}", "held_long", f"Сделка #{d.id} у оператора {d.operator_id} "
                                        f"больше {HELD_ALERT.seconds // 3600} ч без оплаты — проверьте", minutes=720)
        await s.commit()
        for d in remind:
            await push(bot, s, d.operator_id, d, f"Сделка #{d.id}: час без оплаты. Покупатель не платит — напишите ему "
                                                 "в чат сделки или закройте сделку")


async def cards_flow(bot: Bot) -> None:
    """A card stays in the flow only while its seller's free balance covers card_min_rub: the others are taken off,
    and the seller is told why (once — the card stays off until he puts it back)."""
    from bot.handlers.seller import mask
    from bot.models import Card
    async with Session() as s:
        rows = (await s.execute(select(Card, User).join(User, User.id == Card.user_id).where(
            Card.is_active, ~Card.is_deleted, ~Card.is_banned))).all()
        off = []
        for card, seller in rows:
            if problem := deals.flow_problem(card, seller):
                card.is_active = False
                events.add(s, f"card:{card.id}", "flow_off", f"Снята с потока: {problem}", seller.id, notice=True)
                off.append((seller.id, card, problem))
        await s.commit()
        for uid, card, problem in off:
            await notify(bot, uid, f"{pe('pause')} <b>Карта {card.bank} {mask(card)} снята с потока</b>\n"
                                   f"Причина: {problem}. Исправьте и включите карту снова.",
                         kb(btn("Мои карты", "sl", "card", style="primary"), back("x", "Скрыть", "cross")))


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
            asyncio.create_task(bsc_loop(bot)),
            asyncio.create_task(loop(auto_offline, bot, 60)),
            asyncio.create_task(alert_loop(bot)),
            asyncio.create_task(loop(api_webhooks, bot, 2)),
            asyncio.create_task(loop(order_timeouts, bot, 20)),
            asyncio.create_task(loop(stats_topic, bot, 600)),
            asyncio.create_task(loop(chat_pin, bot, 300)),
            asyncio.create_task(loop(channel_autopost, bot, 600)),
            asyncio.create_task(loop(chat_posts, bot, 5)),
            asyncio.create_task(loop(cards_flow, bot, 60)),
            asyncio.create_task(loop(held_watch, bot, 300))]
