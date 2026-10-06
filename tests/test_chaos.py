"""Chaos: buyers, sellers, order merchants, operators and admins press random buttons of whatever they see, type
amounts, links and receipts, while time jumps and the background jobs run. After every step the money must add up:
every balance is backed by the journal, frozen USDT are exactly the open deals', nothing is negative, no handler
fails. Several seeds, hundreds of steps each."""
import logging
import random
from datetime import timedelta
from decimal import Decimal as D

import pytest
from aiogram.types import Document
from sqlalchemy import func, select

from bot import models, tasks
from bot.models import Deal, Ledger, User
from bot.services import deals, money
from tests.harness import cb, msg
from tests.test_orders import LINK, merchant
from tests.test_scenarios import ADMIN, BUYER, SELLER, ready

BUYERS = [BUYER, 21, 22]
SELLERS = [SELLER]
MERCHANTS = [40, 41]
OPERATORS = [ADMIN, 2]
EVERYONE = BUYERS + SELLERS + MERCHANTS + OPERATORS
SKIP = {"x", "menu"}  # closing a message or going home just ends the walk
DANGEROUS = ("aub", "aga", "acb", "aum", "aadj", "axr:tok", "as:", "acs:", "acx:", "acc:", "abc", "arp", "atm:uc")


class Errors(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.records = []

    def emit(self, record):
        if record.name.startswith("bot"):
            self.records.append(record)


async def invariants():
    async with models.Session() as s:
        for u in (await s.scalars(select(User))).all():
            assert u.balance >= 0 and u.frozen >= 0 and u.team_balance >= 0 and u.deposit_lock >= 0, u.id
            main = await s.scalar(select(func.coalesce(func.sum(Ledger.delta), 0)).where(
                Ledger.user_id == u.id, Ledger.kind.not_in(money.TEAM)))
            assert D(main) == u.balance + u.frozen, (u.id, main, u.balance, u.frozen)
            held = sum((d.seller_debit for d in (await s.scalars(select(Deal).where(Deal.seller_id == u.id))).all()
                        if deals.frozen(d) and (d.status in deals.FUNDED or (d.status == "expired" and d.funds_held))),
                       D(0))
            assert held == u.frozen, (u.id, held, u.frozen)


def typed(rng: random.Random, uid: int):
    """Something a person types when the bot asks: an amount, a link, a card, a name, a receipt."""
    pick = rng.random()
    if pick < 0.35:
        return msg(uid, str(rng.choice([1500, 5000, 10000, 20000, 52000])))
    if pick < 0.5:
        return msg(uid, f"{LINK}{rng.randint(1, 10**6)}")
    if pick < 0.6:
        return msg(uid, "5536 9138 1234 5672")
    if pick < 0.7:
        return msg(uid, "Петров Пётр П.")
    if pick < 0.85:
        return msg(uid, document=Document(file_id=f"pdf{rng.randint(1, 10**6)}", file_unique_id=f"u{rng.random()}",
                                          mime_type="application/pdf", file_name="check.pdf"))
    return msg(uid, rng.choice(["/start", "/wallet", "/deals", "текст", "@spam", "-"]))


async def open_deals(uid=None):
    async with models.Session() as s:
        q = select(Deal).where(Deal.status.in_(deals.OPEN + ("expired",)))
        return list((await s.scalars(q)).all())


async def act(b, rng: random.Random):
    """A whole action of a role, as people do them."""
    live = await open_deals()
    kind = rng.choice(["buy", "buy", "receipt", "take", "link", "operator", "seller", "dispute", "cancel", "chat"])
    if kind == "buy":
        buyer = rng.choice(BUYERS)
        await b.run(cb(buyer, "buy:0"), msg(buyer, str(rng.choice([1500, 5000, 10000, 20000, 52000]))))
        go = [x for x in b.session.buttons(buyer) if x and (x.startswith("bgo:") or x == "orb:go")]
        if go:
            await b.run(cb(buyer, rng.choice(go)))
    elif kind == "receipt" and (ds := [d for d in live if d.status in ("waiting_payment", "expired")]):
        d = rng.choice(ds)
        await b.run(cb(d.buyer_id, f"dl:rc:{d.id}"), typed(rng, d.buyer_id) if rng.random() < 0.2 else
                    msg(d.buyer_id, document=Document(file_id=f"pdf{rng.randint(1, 10**9)}",
                                                      file_unique_id=f"u{rng.random()}", mime_type="application/pdf",
                                                      file_name="check.pdf")))
    elif kind == "take" and (ds := [d for d in live if d.status == "searching"]):
        d = rng.choice(ds)
        await b.run(cb(rng.choice(MERCHANTS), f"orq:take:{d.id}:{rng.choice('bBBw')}"))
    elif kind == "link" and (ds := [d for d in live if d.status == "assigned"]):
        d = rng.choice(ds)
        if d.via_bybit:
            await b.run(msg(d.seller_id, f"{LINK}{rng.randint(1, 10**9)}"))
        else:
            await b.run(cb(d.seller_id, f"orq:give:{d.id}"), msg(d.seller_id, "5536 9138 1234 5672 Сбер"),
                        cb(d.seller_id, f"orq:t:{d.id}:15"), cb(d.seller_id, f"orq:ok:{d.id}"))
    elif kind == "operator" and (ds := [d for d in live if d.status == "checking"]):
        d, op = rng.choice(ds), rng.choice(OPERATORS)
        await b.run(cb(op, f"opq:go:{d.id}"))
        what = rng.random()
        if what < 0.5:
            await b.run(cb(op, f"orq:req:{d.id}"), msg(op, "+7 900 123-45-67 Т-Банк\nИван Иванович И."), cb(op, f"orq:t:{d.id}:15"),
                        cb(op, f"orq:ok:{d.id}"))
        else:
            await b.run(cb(op, rng.choice([f"opq:nr:{d.id}", f"opq:nm:{d.id}", f"opq:rj:{d.id}", f"opq:back:{d.id}",
                                           f"opq:cl2:{d.id}"])))
        rate = [x for x in b.session.buttons(op) if x and x.startswith("opr:")]
        if rate:
            await b.run(cb(op, rng.choice(rate)))
    elif kind == "operator" and (ds := [d for d in live if d.status == "waiting_payment" and d.operator_id]):
        d = rng.choice(ds)  # an operator's deal: recreate the order or close it, no clock does it
        await b.run(cb(d.operator_id, rng.choice([f"opq:rj:{d.id}", f"opq:cl:{d.id}", f"opq:cl2:{d.id}"])))
    elif kind == "seller" and (ds := [d for d in live if d.status == "paid"]):
        d = rng.choice(ds)
        who = deals.checker(d)
        await b.run(cb(who, f"dl:{d.id}"), cb(who, rng.choice([f"dl:ok:{d.id}", f"dl:ok2:{d.id}", f"dl:ds:{d.id}"])))
        await b.run(cb(who, f"dl:ok2:{d.id}"))
    elif kind == "dispute" and (ds := [d for d in live if d.status in ("paid", "dispute")]):
        d = rng.choice(ds)
        await b.run(cb(ADMIN, f"adv:{d.id}"), cb(ADMIN, f"ar:{d.id}:{rng.choice('bs')}"))
        await b.run(cb(ADMIN, rng.choice([x for x in b.session.buttons(ADMIN) if x and x.startswith("ar2:")]
                                         or ["menu"])))
    elif kind == "chat" and live:
        d = rng.choice(live)
        who = rng.choice([x for x in (d.buyer_id, d.seller_id, d.operator_id, ADMIN) if x])
        await b.run(cb(who, f"dch:{d.id}"), msg(who, rng.choice(["Перевёл", "@user", "pay.ru", "Жду чек", "ок"])))
    elif kind == "cancel" and (ds := [d for d in live if d.status in deals.UNPAID]):
        d = rng.choice(ds)
        await b.run(cb(d.buyer_id, f"dl:{d.id}"))
        cancel = [x for x in b.session.buttons(d.buyer_id) if x and ("cn" in x)]
        for x in cancel[:2]:
            await b.run(cb(d.buyer_id, x))


async def step(b, rng: random.Random):
    uid = rng.choice(EVERYONE)
    roll = rng.random()
    if roll < 0.08:  # time passes: deadlines expire, the jobs run
        shift = timedelta(minutes=rng.choice([1, 5, 40]))
        async with models.Session() as s:
            for d in (await s.scalars(select(Deal).where(Deal.status.in_(deals.OPEN + ("expired",))))).all():
                d.expires_at = deals.aware(d.expires_at) - shift
                if d.paid_at:
                    d.paid_at = deals.aware(d.paid_at) - shift
                if d.hold_until:
                    d.hold_until = deals.aware(d.hold_until) - shift
            await s.commit()
        for job in (tasks.expire_deals, tasks.order_timeouts, tasks.release_holds, tasks.remind_sellers,
                    tasks.escalate_unanswered_deals, tasks.ton_cycle, tasks.deliver_alerts):
            await job(b.bot)
        return
    if roll < 0.55:
        return await act(b, rng)
    if roll < 0.62:
        return await b.run(cb(uid, rng.choice(["menu", "buy:0", "sl", "om", "op", "w", "a"])))
    if roll < 0.72:
        return await b.run(typed(rng, uid))
    buttons = [x for x in b.session.buttons(uid) if x and not x.startswith("http") and x not in SKIP
               and not x.startswith(DANGEROUS)]
    await b.run(cb(uid, rng.choice(buttons)) if buttons else cb(uid, "menu"))


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_chaos_keeps_the_money_right(go, seed):
    errors = Errors()
    logging.getLogger().addHandler(errors)

    async def fn(b):
        from bot.services import settings
        async with models.Session() as s:
            await settings.put(s, "withdraw_turnover", "1")  # as in production
            await s.commit()
        await ready(b, balance=D(500))
        for uid in BUYERS[1:]:
            await b.run(msg(uid, "/start"))
        await merchant(b, MERCHANTS[0], balance=D(0))
        await merchant(b, MERCHANTS[1], balance=D(800))
        rng = random.Random(seed)
        for n in range(400):
            await step(b, rng)
            await invariants()
        assert not errors.records, [r.getMessage() for r in errors.records][:5]

    try:
        go(fn)
    finally:
        logging.getLogger().removeHandler(errors)
