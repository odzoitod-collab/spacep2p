"""Order requisites: merchant application, request, race to take, requisites, payment, timeouts, API."""
import asyncio
import re

import pytest
from datetime import timedelta
from decimal import Decimal as D

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from bot import models, tasks
from bot.api.server import build_app
from bot.models import Deal, Event, OrderMerchant, User
from bot.services import deals, money
from tests.harness import cb, msg, plain
from tests.test_api import apply_and_approve, auth
from tests.test_scenarios import ADMIN, BUYER, OTHER, PDF, SELLER, create_deal, ready, user

M1, M2 = 40, 41


async def merchant(b, uid, balance=D(1000)):
    """Registers, fills the application, gets approved by the admin, has USDT on the balance."""
    await b.run(msg(uid, "/start"), cb(uid, "om"), cb(uid, "om:apply"), msg(uid, "свои карты и команда"),
                cb(uid, "om:sp:0"), msg(uid, "5000"), msg(uid, "100000"), msg(uid, "200000"),
                msg(uid, "Сбер, Т-Банк"), cb(uid, "om:skip"))
    assert "Анкета отправлена" in plain(b.session.last(uid))
    assert any(f"aom:{uid}" in (b_ or "") for b_ in b.session.buttons(ADMIN))  # straight to the admin's chat
    await b.run(cb(ADMIN, f"aom:{uid}"), cb(ADMIN, f"aom:ok:{uid}"))
    async with models.Session() as s:
        await money.add(s, uid, balance, "deposit", "dep:0")
        await s.commit()
        assert (await s.get(OrderMerchant, uid)).accepting


async def request(b, amount="50000", buyer=BUYER):
    await b.run(cb(buyer, "buy:0"), cb(buyer, "orb:new"), msg(buyer, amount), cb(buyer, "orb:b:1"), cb(buyer, "orb:go"))
    async with models.Session() as s:
        return await s.scalar(select(Deal).where(Deal.buyer_id == buyer).order_by(Deal.id.desc()))


async def deal(did):
    async with models.Session() as s:
        return await s.get(Deal, did)


def offers(b, uid):
    return [t for t in b.session.texts(uid) if "Ордерная заявка #" in t]


async def give(b, uid, did, minutes=15):
    await b.run(cb(uid, f"orq:k:{did}:card"), cb(uid, f"orq:b:{did}:0"), msg(uid, "5536 9138 1234 5672"),
                msg(uid, "Петров Пётр П."), cb(uid, f"orq:t:{did}:{minutes}"), cb(uid, f"orq:ok:{did}"))


def test_full_order_requisites_flow(go):
    async def fn(b):
        await ready(b)  # the only static card cannot take 50 000 ₽
        await merchant(b, M1)
        d = await request(b)
        assert d.status == "searching" and d.is_order and d.sender_bank == "Т-Банк" and d.seller_id is None
        assert "Ищем ордерного мерчанта" in plain(b.session.last(BUYER))
        assert offers(b, M1) and f"orq:take:{d.id}" in b.session.buttons(M1)

        await b.run(cb(M1, f"orq:take:{d.id}"))
        taken = await deal(d.id)
        assert taken.status == "assigned" and taken.seller_id == M1
        m1 = await user(M1)
        assert m1.frozen == taken.seller_debit == D("480")  # 50 000 / 100 × (1 − 4%), frozen when taken
        assert "Мерчант взял заявку" in plain(b.session.last(BUYER))

        await give(b, M1, d.id, 20)
        d = await deal(d.id)
        assert d.status == "waiting_payment" and d.card_id is not None
        assert timedelta(minutes=19) < deals.aware(d.expires_at) - models.now() <= timedelta(minutes=20)
        text = plain(b.session.last(BUYER))
        assert "Реквизиты по заявке" in text and "5536913812345672" in text and "Петров Пётр П." in text

        await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, document=PDF), cb(M1, f"dl:ok2:{d.id}"))
        assert (await deal(d.id)).status == "completed"
        assert (await user(BUYER)).balance == D(470) and (await user(M1)).balance == D(520)  # +20 USDT at 4%
        assert (await user(M1)).frozen == 0

        d2 = await request(b, "10000")  # the merchant gives the same requisites again with one tap
        await b.run(cb(M1, f"orq:take:{d2.id}"))
        assert any(x.startswith(f"orq:tpl:{d2.id}:") for x in b.session.buttons(M1) if x)
    go(fn)


def test_two_merchants_race_for_one_request(go):
    async def fn(b):
        if models.engine.dialect.name == "sqlite":  # one shared in-memory connection: no real row locks
            pytest.skip("the race needs PostgreSQL row locks (P2P_TEST_PG)")
        await ready(b)
        await merchant(b, M1)
        await merchant(b, M2)
        d = await request(b)
        await asyncio.gather(b.dp.feed_update(b.bot, cb(M1, f"orq:take:{d.id}")),
                             b.dp.feed_update(b.bot, cb(M2, f"orq:take:{d.id}")))
        won = await deal(d.id)
        loser = M2 if won.seller_id == M1 else M1
        assert won.status == "assigned" and (await user(loser)).frozen == 0  # only the winner's funds are frozen
        assert (await user(won.seller_id)).frozen == won.seller_debit
        assert any("уже взял другой мерчант" in a for a in b.session.alerts()) or \
            any("взял другой мерчант" in t for t in b.session.texts(loser))
    go(fn)


def test_decline_and_timeouts(go):
    async def fn(b):
        await ready(b)
        await merchant(b, M1)
        await merchant(b, M2)
        d = await request(b)
        await b.run(cb(M1, f"orq:take:{d.id}"), cb(M1, f"orq:drop:{d.id}"))
        d = await deal(d.id)
        assert d.status == "searching" and (await user(M1)).frozen == 0
        assert len(offers(b, M2)) == 2 and len(offers(b, M1)) == 1  # re-offered to the others at once
        await tasks.order_timeouts(b.bot)
        assert len(offers(b, M2)) == 2 and len(offers(b, M1)) == 1  # nobody twice, never to the one who declined

        await b.run(cb(M2, f"orq:take:{d.id}"))
        async with models.Session() as s:  # M2 took it and went silent
            (await s.get(Deal, d.id)).expires_at = models.now() - timedelta(seconds=1)
            await s.commit()
        await tasks.order_timeouts(b.bot)
        d = await deal(d.id)
        assert d.status == "searching" and (await user(M2)).frozen == 0
        assert "Время на выдачу реквизитов" in plain(b.session.last(M2))

        async with models.Session() as s:  # nobody takes it in time
            (await s.get(Deal, d.id)).expires_at = models.now() - timedelta(seconds=1)
            await s.commit()
        await tasks.order_timeouts(b.bot)
        d = await deal(d.id)
        assert (d.status, d.close_reason) == ("cancelled", "no_merchant")
        assert "не нашлись" in plain(b.session.last(BUYER))
        async with models.Session() as s:
            assert await deals.buyer_failures(s, BUYER) == 0  # not the buyer's fault
    go(fn)


def test_merchant_limits_and_buyer_cancel(go):
    async def fn(b):
        await ready(b)
        await merchant(b, M1, balance=D(100))  # 100 USDT covers ~10 500 ₽ only
        big = await request(b, "50000")
        assert not offers(b, M1)
        async with models.Session() as s:
            assert await s.scalar(select(Event.id).where(Event.kind == "no_merchants"))
        await b.run(cb(BUYER, f"orb:cn:{big.id}"))
        assert (await deal(big.id)).status == "cancelled"
        small = await request(b, "8000")
        assert f"orq:take:{small.id}" in b.session.buttons(M1)
        await b.run(cb(M1, f"orq:take:{small.id}"), cb(BUYER, f"orb:cn:{small.id}"))
        assert (await deal(small.id)).status == "cancelled" and (await user(M1)).frozen == 0
        assert "Покупатель отменил заявку" in plain(b.session.last(M1))
        await b.run(cb(M1, "om"), cb(M1, "om:acc:0"))
        await request(b, "9000")
        assert len(offers(b, M1)) == 1  # switched off: no new offers
    go(fn)


def test_api_falls_back_to_order_requisites(go):
    async def fn(b):
        await ready(b)
        await merchant(b, M1)
        token = await apply_and_approve(b)
        async with TestClient(TestServer(build_app(b.bot))) as c:
            r = await c.post("/v1/orders", headers=auth(token), json={"amount_rub": "50000", "external_id": "big-1",
                                                                     "sender_bank": "Альфа-Банк"})
            assert r.status == 202
            order = await r.json()
            assert order["status"] == "searching_requisites" and order["requisites"] is None and order["order_requisites"]
            assert order["search_expires_at"]
            await b.run(cb(M1, f"orq:take:{order['id']}"))
            await give(b, M1, order["id"])
            got = await (await c.get(f"/v1/orders/{order['id']}", headers=auth(token))).json()
            assert got["status"] == "awaiting_payment" and got["requisites"]["number"] == "5536913812345672"
            off = await c.post("/v1/orders", headers=auth(token), json={"amount_rub": "50000",
                                                                       "order_requisites": False})
            assert off.status == 409 and (await off.json())["error"]["code"] == "no_liquidity"
        async with models.Session() as s:
            u = await s.get(User, BUYER)
            assert u.frozen == 0  # the API owner pays nothing up front
        assert not [t for t in b.session.texts(BUYER) if re.search(r"Реквизиты по заявке", t)]  # webhook, not chat
    go(fn)


def test_order_cabinet_default_time_and_two_tap_template(go):
    async def fn(b):
        await ready(b)
        await merchant(b, M1)
        await b.run(cb(M1, "om"), cb(M1, "om:pay"))
        assert "на оплату: 20 мин" in plain(b.session.last(M1))
        d = await request(b)
        await b.run(cb(M1, f"orq:take:{d.id}"))
        await give(b, M1, d.id, 20)
        await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, document=PDF), cb(M1, f"dl:ok2:{d.id}"))
        await b.run(cb(M1, "om"))
        text = plain(b.session.last(M1))
        assert "Сегодня: 1 на 50 000 ₽ · +20 USDT" in text and "Ордера: результаты" in text
        d2 = await request(b, "10000")
        await b.run(cb(M1, f"orq:take:{d2.id}"))
        tpl = next(x for x in b.session.buttons(M1) if x and x.startswith(f"orq:tpl:{d2.id}:"))
        await b.run(cb(M1, tpl))  # tap 1: the saved requisites, default time — straight to the check screen
        assert "На оплату: 20 мин" in plain(b.session.last(M1)) and f"orq:ok:{d2.id}" in b.session.buttons(M1)
        await b.run(cb(M1, f"orq:ok:{d2.id}"))  # tap 2
        assert (await deal(d2.id)).status == "waiting_payment"
    go(fn)


def test_static_merchant_earns_more_than_order_merchant(go):
    async def fn(b):
        await ready(b, balance=D(1000))
        await merchant(b, M1)
        static = await create_deal(b, amount="10000")
        order = await request(b, "10000", buyer=OTHER)
        assert (static.seller_pct, order.seller_pct) == (D(5), D(4))  # fixed in each deal when it is created
        assert static.buyer_credit == order.buyer_credit == D(94)  # the buyer pays the same either way
        assert static.seller_debit == D(95) and order.seller_debit == D(96)  # merchant earns 5 vs 4 USDT
        assert order.platform_fee == D(2) and static.platform_fee == D(1)  # the difference stays with the platform
        await b.run(cb(ADMIN, "as:order_seller_pct"), msg(ADMIN, "6"))
        assert "не может получать больше" in plain(b.session.last(ADMIN))
    go(fn)


def test_admin_commissions_and_personal_rates(go):
    async def fn(b):
        await ready(b, balance=D(1000))
        await merchant(b, M1)
        await b.run(cb(ADMIN, "a"), cb(ADMIN, "acm"))
        text = plain(b.session.last(ADMIN))
        assert "Комиссии и проценты" in text and "статичная карта: 5%" in text and "площадке 2%" in text
        await b.run(cb(ADMIN, "acs:order_seller_pct"), msg(ADMIN, "3.5"))
        assert "Сохранено: 4% → 3.5%" in plain(b.session.last(ADMIN)) and "Комиссии и проценты" in plain(
            b.session.last(ADMIN))  # back on the same screen after saving

        await b.run(cb(ADMIN, f"aup:{SELLER}"), cb(ADMIN, f"aup:set:{SELLER}:s"), msg(ADMIN, "7"))
        assert "меньше процента покупателя" in plain(b.session.last(ADMIN))  # 7% ≥ 6%: refused
        await b.run(msg(ADMIN, "5.5"))
        await b.run(cb(ADMIN, f"aup:{M1}"), cb(ADMIN, f"aup:set:{M1}:o"), msg(ADMIN, "4.5"))
        assert "Ваш процент по ордерам: 4.5%" in plain(b.session.last(M1))

        static = await create_deal(b, amount="10000")
        assert (static.seller_pct, static.seller_debit, static.buyer_credit) == (D("5.5"), D("94.5"), D(94))
        order = await request(b, "10000", buyer=OTHER)
        assert "(4.5%)" in plain([t for t in b.session.texts(M1) if "Ордерная заявка" in t][-1])
        await b.run(cb(M1, f"orq:take:{order.id}"))
        taken = await deal(order.id)
        assert (taken.seller_pct, taken.seller_debit, taken.buyer_credit) == (D("4.5"), D("95.5"), D(94))
        assert (await user(M1)).frozen == D("95.5")  # frozen at his own rate

        async with models.Session() as s:  # the platform fee is cut below a personal rate: it is capped
            from bot.services import settings
            await settings.put(s, "platform_pct", "5")
            await s.commit()
            assert settings.merchant_pct(await s.get(User, SELLER), False) == D(5)
        await b.run(cb(ADMIN, f"aup:rs:{SELLER}"))
        async with models.Session() as s:
            assert (await s.get(User, SELLER)).pct_static is None
    go(fn)
