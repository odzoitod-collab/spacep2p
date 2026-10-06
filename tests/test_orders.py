"""Order requisites: merchant application, request, race to take, requisites, payment, timeouts, API; Bybit orders
(link -> «Принять ордер» for every operator -> the first one gives the requisites -> confirms (his debt) or wins the
dispute with proof); fixed order rate. Every merchant gets every request and picks Bybit order or balance per take.

The order rate is 104 ₽: a 52 000 ₽ request is a 500 USDT order, the buyer gets 52 000 / 100 × 94% = 488.8 USDT."""
import asyncio
import re

import pytest
from datetime import timedelta
from decimal import Decimal as D

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from bot import models, tasks
from bot.api.server import build_app
from bot.models import Deal, Event, Ledger, Operator, OrderMerchant, User
from bot.services import api, deals, money
from tests.harness import cb, msg, plain
from tests.test_api import apply_and_approve, auth
from tests.test_scenarios import ADMIN, BUYER, OTHER, PDF, SELLER, create_deal, ready, user

M1, M2 = 40, 41


async def merchant(b, uid, balance=D(1000)):
    """Registers, fills the application, gets approved by the admin, has USDT on the balance."""
    await b.run(msg(uid, "/start"), cb(uid, "om"), cb(uid, "om:apply"), msg(uid, "свои карты и команда"),
                cb(uid, "om:sp:0"), msg(uid, "Сбер, Т-Банк"), cb(uid, "om:skip"))
    assert "Анкета отправлена" in plain(b.session.last(uid))
    await b.deliver()  # the card in the admin chat (here: the admin's private chat) with the decision on it
    assert f"aom:ok:{uid}" in b.session.buttons(ADMIN) and f"aom:{uid}" in b.session.buttons(ADMIN)
    await b.run(cb(ADMIN, f"aom:{uid}"), cb(ADMIN, f"aom:ok:{uid}"))
    async with models.Session() as s:
        if balance:
            await money.add(s, uid, balance, "deposit", "dep:0")
            await s.commit()
        assert (await s.get(OrderMerchant, uid)).status == "approved"


async def request(b, amount="52000", buyer=BUYER):
    """A request for requisites under the amount: the bot makes one when no static card takes the amount, so the
    sellers with cards step off their shift for the moment of the request."""
    async with models.Session() as s:
        online = list((await s.scalars(select(User.id).where(User.is_online))).all())
        for uid in online:
            (await s.get(User, uid)).is_online = False
        await s.commit()
    await b.run(cb(buyer, "buy:0"), msg(buyer, amount))
    assert "orb:go" in b.session.buttons(buyer), plain(b.session.last(buyer))
    await b.run(cb(buyer, "orb:go"))
    async with models.Session() as s:
        for uid in online:
            (await s.get(User, uid)).is_online = True
        await s.commit()
        return await s.scalar(select(Deal).where(Deal.buyer_id == buyer).order_by(Deal.id.desc()))


async def deal(did):
    async with models.Session() as s:
        return await s.get(Deal, did)


def offers(b, uid):
    return [t for t in b.session.texts(uid) if re.search(r"Новая заявка #\d+ · [\d ]+ ₽", plain(t))]


async def give(b, uid, did, minutes=15):
    await b.run(cb(uid, f"orq:give:{did}"), cb(uid, f"orq:req:{did}"), msg(uid, "5536 9138 1234 5672 Сбербанк\nПетров Пётр П."),
                cb(uid, f"orq:t:{did}:{minutes}"), cb(uid, f"orq:ok:{did}"))


def test_full_order_requisites_flow(go):
    async def fn(b):
        await ready(b)  # the only static card cannot take 50 000 ₽
        await merchant(b, M1)
        d = await request(b)
        assert d.status == "searching" and d.is_order and d.sender_bank is None and d.seller_id is None
        assert (d.merchant_rate, d.seller_debit, d.buyer_credit) == (D(104), D(500), D("488.8"))
        assert "Ищем мерчанта под вашу сумму" in plain(b.session.last(BUYER))
        assert offers(b, M1) and f"orq:take:{d.id}:b" in b.session.buttons(M1)
        offer = plain(offers(b, M1)[-1])
        assert "Доход" not in offer and "доход" not in offer  # the merchant's earnings are not ours to say
        for part in ("Сумма перевода: 52 000 ₽", "Курс площадки для ордера: 104 ₽", "Зайти в ордер на: 500 USDT",
                     "ссылка на ордер — за 5 мин"):
            assert part in offer, part

        assert f"orq:take:{d.id}:w" in b.session.buttons(M1)  # 1000 USDT cover it: both ways are offered
        await b.run(cb(M1, f"orq:take:{d.id}:w"))
        taken = await deal(d.id)
        assert taken.status == "assigned" and taken.seller_id == M1 and not taken.via_bybit
        m1 = await user(M1)
        assert m1.frozen == taken.seller_debit == D("500")  # 52 000 / 104, frozen when taken
        assert "Мерчант взял заявку" in plain(b.session.last(BUYER))

        await give(b, M1, d.id, 20)
        d = await deal(d.id)
        assert d.status == "waiting_payment" and d.card_id is not None
        assert timedelta(minutes=19) < deals.aware(d.expires_at) - models.now() <= timedelta(minutes=20)
        text = plain(b.session.last(BUYER))
        assert "Реквизиты по заявке" in text and "5536913812345672" in text and "Петров Пётр П." in text

        await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, document=PDF), cb(M1, f"dl:ok2:{d.id}"))
        assert (await deal(d.id)).status == "completed"
        assert (await user(BUYER)).balance == D("488.8") and (await user(M1)).balance == D(500)
        assert (await user(M1)).frozen == 0

        d2 = await request(b, "10400")  # no old cards and no card/SBP choice: one message with the requisites
        await b.run(cb(M1, f"orq:take:{d2.id}:w"))
        assert not [x for x in b.session.buttons(M1) if x and x.startswith(("orq:tpl", "orq:k"))]
        assert "Одним сообщением" in plain(b.session.last(M1))
    go(fn)


def test_two_merchants_race_for_one_request(go):
    async def fn(b):
        if models.engine.dialect.name == "sqlite":  # one shared in-memory connection: no real row locks
            pytest.skip("the race needs PostgreSQL row locks (P2P_TEST_PG)")
        await ready(b)
        await merchant(b, M1)
        await merchant(b, M2)
        d = await request(b)
        await asyncio.gather(b.dp.feed_update(b.bot, cb(M1, f"orq:take:{d.id}:w")),
                             b.dp.feed_update(b.bot, cb(M2, f"orq:take:{d.id}:w")))
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
        await b.run(cb(M1, f"orq:take:{d.id}:w"), cb(M1, f"orq:drop:{d.id}"))
        d = await deal(d.id)
        assert d.status == "searching" and (await user(M1)).frozen == 0
        assert len(offers(b, M2)) == 2 and len(offers(b, M1)) == 1  # re-offered to the others at once
        await tasks.order_timeouts(b.bot)
        assert len(offers(b, M2)) == 2 and len(offers(b, M1)) == 1  # nobody twice, never to the one who declined

        await b.run(cb(M2, f"orq:take:{d.id}:w"))
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
    go(fn)


def test_every_merchant_gets_every_request_and_picks_the_way(go):
    async def fn(b):
        await ready(b)
        await merchant(b, M1, balance=D(100))  # 100 USDT cover 10 400 ₽ only
        big = await request(b, "52000")
        assert offers(b, M1)  # no amount limits: the request comes anyway
        assert f"orq:take:{big.id}:b" in b.session.buttons(M1) and f"orq:take:{big.id}:w" not in b.session.buttons(M1)
        assert "свободно только 100" in plain(offers(b, M1)[-1])
        await b.run(cb(M1, f"orq:take:{big.id}:w"))  # an old button or a forged one: refused with the reason
        assert any("нужно 500 USDT свободных" in a for a in b.session.alerts())
        assert (await deal(big.id)).status == "searching"
        await b.run(cb(BUYER, f"orb:cn:{big.id}"))
        assert (await deal(big.id)).status == "cancelled"
        assert any("отменена покупателем" in plain(t) for t in b.session.texts(M1))  # the offer was closed
        small = await request(b, "8000")
        assert f"orq:take:{small.id}:w" in b.session.buttons(M1)
        await b.run(cb(M1, f"orq:take:{small.id}:w"))
        assert (await user(M1)).frozen == (await deal(small.id)).seller_debit
        await b.run(cb(BUYER, f"orb:cn:{small.id}"))
        assert (await deal(small.id)).status == "cancelled" and (await user(M1)).frozen == 0
        assert "Покупатель отменил заявку" in plain(b.session.last(M1))
    go(fn)


def test_api_falls_back_to_order_requisites(go):
    async def fn(b):
        await ready(b)
        await merchant(b, M1)
        token = await apply_and_approve(b)
        async with TestClient(TestServer(build_app(b.bot))) as c:
            r = await c.post("/v1/orders", headers=auth(token), json={"amount_rub": "52000", "external_id": "big-1",
                                                                     "sender_bank": "Альфа-Банк"})
            assert r.status == 202
            order = await r.json()
            assert order["status"] == "searching_requisites" and order["requisites"] is None and order["order_requisites"]
            assert order["search_expires_at"]
            await b.run(cb(M1, f"orq:take:{order['id']}:w"))
            await give(b, M1, order["id"])
            got = await (await c.get(f"/v1/orders/{order['id']}", headers=auth(token))).json()
            assert got["status"] == "awaiting_payment" and got["requisites"]["number"] == "5536913812345672"
            off = await c.post("/v1/orders", headers=auth(token), json={"amount_rub": "52000",
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
        await b.run(cb(M1, f"orq:take:{d.id}:w"))
        await give(b, M1, d.id, 20)
        await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, document=PDF), cb(M1, f"dl:ok2:{d.id}"))
        await b.run(cb(M1, "om"))
        text = plain(b.session.last(M1))
        assert "Сегодня: 1 · 52 000 ₽ · +20 USDT" in text and "Результаты" in text  # 520 − 500 at 104 ₽
        d2 = await request(b, "10400")
        await b.run(cb(M1, f"orq:take:{d2.id}:w"))
        await b.run(msg(M1, "+7 (900) 123-45-67, тинькофф"))  # one message, default time — the check screen
        text = plain(b.session.last(M1))
        assert "Т-Банк · СБП" in text and "+79001234567" in text and "На оплату: 20 мин" in text
        assert f"orq:ok:{d2.id}" in b.session.buttons(M1)
        await b.run(cb(M1, f"orq:ok:{d2.id}"))  # one tap
        d2 = await deal(d2.id)
        assert d2.status == "waiting_payment"
        buyer = plain(b.session.last(BUYER))
        assert "+79001234567" in buyer and "None" not in buyer  # no name given: no empty name line
    go(fn)


def test_order_merchant_works_at_a_fixed_rate(go):
    async def fn(b):
        await ready(b, balance=D(1000))
        await merchant(b, M1)
        static = await create_deal(b, amount="10400")
        order = await request(b, "10400", buyer=OTHER)
        assert static.seller_pct == D(5) and order.merchant_rate == D(104) and order.seller_pct == 0
        assert static.buyer_credit == order.buyer_credit == D("97.76")  # the buyer pays the same either way
        assert static.seller_debit == D("98.8") and order.seller_debit == D(100)  # 10 400 / 104, no percent
        assert order.platform_fee == D("2.24")
        await b.run(cb(ADMIN, "as:order_rate"), msg(ADMIN, "107"))  # above 100 / (1 − 6%)
        assert "не может быть выше 106.38" in plain(b.session.last(ADMIN))
    go(fn)


def test_admin_commissions_and_order_rate(go):
    async def fn(b):
        await ready(b, balance=D(1000))
        await merchant(b, M1)
        await b.run(cb(ADMIN, "a"), cb(ADMIN, "acm"))
        text = plain(b.session.last(ADMIN))
        assert "Комиссии и проценты" in text and "статичная карта: 5%" in text and "курс 104 ₽ за USDT" in text
        assert "acs:order_seller_pct" not in b.session.buttons(ADMIN) and "acs:order_rate" in b.session.buttons(ADMIN)
        await b.run(cb(ADMIN, "acs:order_rate"), msg(ADMIN, "102"))
        assert "Сохранено: 104 ₽ → 102 ₽" in plain(b.session.last(ADMIN)) and "Комиссии и проценты" in plain(
            b.session.last(ADMIN))  # back on the same screen after saving

        await b.run(cb(ADMIN, f"aup:{SELLER}"), cb(ADMIN, f"aup:set:{SELLER}:s"), msg(ADMIN, "7"))
        assert "меньше процента покупателя" in plain(b.session.last(ADMIN))  # 7% ≥ 6%: refused
        await b.run(msg(ADMIN, "5.5"))
        assert f"aup:set:{M1}:o" not in b.session.buttons(ADMIN)  # no personal order percent any more
        static = await create_deal(b, amount="10000")
        assert (static.seller_pct, static.seller_debit, static.buyer_credit) == (D("5.5"), D("94.5"), D(94))
        order = await request(b, "10200", buyer=OTHER)
        assert "Курс площадки для ордера: 102 ₽" in plain(offers(b, M1)[-1])
        await b.run(cb(M1, f"orq:take:{order.id}:w"))
        assert (await deal(order.id)).seller_debit == D(100)  # 10 200 / 102

        async with models.Session() as s:  # the platform fee is cut below a personal rate: it is capped
            from bot.services import settings
            await settings.put(s, "platform_pct", "5")
            await s.commit()
            assert settings.merchant_pct(await s.get(User, SELLER)) == D(5)
        await b.run(cb(ADMIN, f"aup:rs:{SELLER}"))
        async with models.Session() as s:
            assert (await s.get(User, SELLER)).pct_static is None
    go(fn)


# ---------- Bybit orders ----------

LINK = "https://www.bybit.com/fiat/trade/otc/orderList/1873200011"
OP, OP2 = ADMIN, 2  # OPERATOR_IDS is not set: the admins are the operators


def docs_to(b, uid):
    return [m for m in b.session.calls if type(m).__name__ == "SendDocument" and m.chat_id == uid]


def kb_data(m):
    return [x.callback_data for row in (m.reply_markup.inline_keyboard if m.reply_markup else []) for x in row]


async def bybit_requisites(b, did, link=LINK):
    """The merchant sends the link, the operator takes it and gives the requisites of the order."""
    await b.run(cb(M1, f"orq:take:{did}:B"), msg(M1, link), cb(OP, f"opq:go:{did}"))
    await give(b, OP, did)


def test_bybit_order_flow_without_balance(go):
    async def fn(b):
        await ready(b)
        await merchant(b, M1, balance=D(0))  # a Bybit order needs no balance in the bot
        d = await request(b)
        assert f"orq:take:{d.id}:b" in b.session.buttons(M1) and f"orq:take:{d.id}:w" not in b.session.buttons(M1)
        assert "Bybit-ордер: баланс не нужен" in plain(offers(b, M1)[-1])

        await b.run(cb(M1, f"orq:take:{d.id}:B"))
        d = await deal(d.id)
        assert d.status == "assigned" and d.via_bybit and (await user(M1)).frozen == 0
        assert "Пришлите сюда ссылку на ордер" in plain(b.session.last(M1)) and "500 USDT" in plain(b.session.last(M1))

        await b.run(msg(M1, "https://evil.example/order/1"))
        assert "Нужна ссылка на ордер Bybit" in plain(b.session.last(M1))
        await b.run(msg(OP2, "/start"), msg(M1, LINK))
        d = await deal(d.id)
        assert (d.status, d.bybit_url, d.operator_id) == ("checking", LINK, None)
        assert "оператор проверяет" in plain(b.session.last(BUYER))
        to_op = [t for t in b.session.texts(OP) if "Bybit-ордер · заявка" in t][-1]
        for part in ("Сумма ордера: 52 000 ₽", "Курс мерчанта: 104 ₽", "Зайти на: 500 USDT"):
            assert part in plain(to_op)
        assert LINK not in to_op  # the link only goes to the operator who accepts the order
        assert f"opq:go:{d.id}" in b.session.buttons(OP) and f"opq:go:{d.id}" in b.session.buttons(OP2)

        await b.run(cb(OP, f"opq:go:{d.id}"))
        assert (await deal(d.id)).operator_id == OP
        assert LINK in b.session.last(OP)  # now he has it
        closed = [m for m in b.session.calls if type(m).__name__ == "EditMessageText" and m.chat_id == OP2]
        assert closed and "принял другой оператор" in plain(closed[-1].text)  # gone for the others
        await b.run(cb(OP2, f"opq:go:{d.id}"))
        assert any("уже принял другой оператор" in a for a in b.session.alerts())
        assert (await deal(d.id)).operator_id == OP
        await b.run(cb(M1, f"orq:give:{d.id}"))  # the merchant cannot give the requisites himself
        assert any("уже не у вас" in a for a in b.session.alerts())
        await give(b, OP, d.id)
        d = await deal(d.id)
        assert d.status == "waiting_payment" and "5536913812345672" in plain(b.session.last(BUYER))
        assert "Оператор выдал покупателю реквизиты" in plain(b.session.last(M1))

        await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, document=PDF))
        assert (await deal(d.id)).status == "paid"
        assert f"dl:ok:{d.id}" in kb_data(docs_to(b, OP)[-1])  # the operator checks: buttons
        assert kb_data(docs_to(b, M1)[-1]) == ["x"]  # the merchant: the PDF only
        await b.run(cb(M1, f"dl:ok2:{d.id}"))
        assert (await deal(d.id)).status == "paid"  # not his to confirm

        await b.run(cb(OP, f"dl:ok:{d.id}"), cb(OP, f"dl:ok2:{d.id}"))
        d = await deal(d.id)
        assert (d.status, d.close_reason) == ("completed", "confirmed")
        assert (await user(BUYER)).balance == D("488.8")  # paid by the platform: the 500 USDT came on Bybit
        m1 = await user(M1)
        assert m1.balance == m1.frozen == 0
        async with models.Session() as s:
            fee = await s.scalar(select(Ledger.delta).where(Ledger.user_id.is_(None), Ledger.ref == f"deal:{d.id}"))
            assert fee == D("11.2")
            assert (await s.get(Operator, OP)).debt == D(500)  # the order's USDT are on his Bybit: he owes them
        assert "Оператор подтвердил оплату" in plain(b.session.last(M1))
    go(fn)


def test_operator_dispute_wins_with_proof(go):
    async def fn(b):
        await ready(b)
        await merchant(b, M1, balance=D(0))
        d = await request(b)
        await bybit_requisites(b, d.id)
        await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, document=PDF),
                    cb(OP, f"dl:ds:{d.id}"), cb(OP, f"dl:dr:{d.id}:not_received"))
        assert "Спор решится сразу" in plain(b.session.last(OP))
        await b.run(msg(OP, "нет видео"))
        assert "Нужен видеофайл, PDF или фото" in plain(b.session.last(OP))
        await b.run(msg(OP, document=PDF))  # a bank statement is enough
        d = await deal(d.id)
        assert (d.status, d.close_reason) == ("cancelled", "dispute_seller") and "оплата не поступила" in d.resolution
        assert (await user(BUYER)).balance == 0
        assert "Спор по сделке" in plain(b.session.last(BUYER)) and "решён" in plain(b.session.last(BUYER))

        d2 = await request(b, "20800", buyer=OTHER)
        await b.run(msg(OTHER, "/start"))
        await bybit_requisites(b, d2.id, LINK + "2")
        await b.run(cb(OTHER, f"dl:rc:{d2.id}"), msg(OTHER, document=PDF),
                    cb(OP, f"dl:ds:{d2.id}"), cb(OP, f"dl:dr:{d2.id}:wrong_amount"), msg(OP, "10400"),
                    msg(OP, document=PDF))
        d2 = await deal(d2.id)
        assert (d2.status, d2.close_reason, d2.amount_rub) == ("completed", "dispute_actual", D(10400))
        assert (await user(OTHER)).balance == D("97.76")  # by the amount actually received
    go(fn)


def test_operator_recreates_the_order_and_closes_the_deal_himself(go):
    async def fn(b):
        await ready(b)
        await merchant(b, M1, balance=D(0))
        d = await request(b)
        await b.run(cb(M1, f"orq:take:{d.id}:B"), msg(M1, LINK), cb(OP, f"opq:go:{d.id}"))
        assert (await deal(d.id)).expires_at.replace(tzinfo=models.now().tzinfo) - models.now() > timedelta(days=300)
        await b.run(cb(OP, f"opq:rj:{d.id}"))  # «Пересоздать ордер»
        d = await deal(d.id)
        assert (d.status, d.bybit_url, d.operator_id) == ("assigned", None, OP)  # the deal stays his
        assert "пересоздать ордер" in plain(b.session.last(M1)) and f"orq:give:{d.id}" in b.session.buttons(M1)
        await b.run(cb(M1, f"orq:give:{d.id}"), msg(M1, LINK + "2"))
        d = await deal(d.id)
        assert (d.status, d.operator_id) == ("checking", OP)
        assert "Новый ордер по заявке" in plain(b.session.last(OP))  # straight to him, not to every operator

        d2 = await request(b, "10400", buyer=OTHER)
        await b.run(cb(M1, f"orq:take:{d2.id}:B"), msg(M1, LINK + "2"))  # the same order for another request
        assert "уже была в заявке" in plain(b.session.last(M1)) and (await deal(d2.id)).status == "assigned"

        async with models.Session() as s:  # time passes: an operator's deal has no deadline
            (await s.get(Deal, d.id)).expires_at = models.now() - timedelta(seconds=1)
            await s.commit()
        await tasks.order_timeouts(b.bot)
        assert (await deal(d.id)).status == "checking"

        await give(b, OP, d.id)
        d = await deal(d.id)
        assert d.status == "waiting_payment" and d.expires_at.replace(tzinfo=models.now().tzinfo) - models.now() \
            > timedelta(days=300)  # no payment deadline either
        assert "Оплатите сейчас — сделку ведёт оператор" in plain(b.session.last(BUYER))
        assert f"opq:cl:{d.id}" in b.session.buttons(OP) and f"opq:rj:{d.id}" in b.session.buttons(OP)
        await b.run(cb(OP, f"opq:cl:{d.id}"), cb(OP, f"opq:cl2:{d.id}"))
        d = await deal(d.id)
        assert (d.status, d.close_reason) == ("expired", "operator_close")  # a buyer who paid still sends the receipt
        assert "Отмените ордер на Bybit" in plain(b.session.last(M1))
        assert any("не переводите" in t for t in b.session.texts(BUYER))
    go(fn)


def test_an_order_nobody_accepted_still_times_out(go):
    async def fn(b):
        await ready(b)
        await merchant(b, M1, balance=D(0))
        d = await request(b)
        await b.run(cb(M1, f"orq:take:{d.id}:B"), msg(M1, LINK))
        async with models.Session() as s:
            (await s.get(Deal, d.id)).expires_at = models.now() - timedelta(seconds=1)
            await s.commit()
        await tasks.order_timeouts(b.bot)
        d = await deal(d.id)
        assert (d.status, d.close_reason) == ("cancelled", "no_merchant")
        async with models.Session() as s:
            assert await s.scalar(select(Event.id).where(Event.kind == "check_timeout", Event.alert))
        assert "оператор не успел" in plain(b.session.last(M1))
    go(fn)


def test_buyer_cancels_while_operator_checks(go):
    async def fn(b):
        await ready(b)
        await merchant(b, M1, balance=D(0))
        d = await request(b)
        await b.run(cb(M1, f"orq:take:{d.id}:B"), msg(M1, LINK), cb(OP, f"opq:go:{d.id}"), cb(BUYER, f"orb:cn:{d.id}"))
        assert (await deal(d.id)).status == "cancelled"
        assert "Отмените ордер на Bybit" in plain(b.session.last(M1))
        assert any("не выдавайте реквизиты" in t for t in b.session.texts(OP))
    go(fn)


def test_api_bybit_order_statuses_history_and_liquidity(go):
    async def fn(b):
        await ready(b)
        await merchant(b, M1, balance=D(0))
        token = await apply_and_approve(b)
        async with TestClient(TestServer(build_app(b.bot))) as c:
            liq = await (await c.get("/v1/liquidity", headers=auth(token))).json()
            assert liq["order_requisites"] == {"available": True, "merchants": 1, "min_rub": "1000.00",
                                               "max_rub": "100000.00"}  # any amount of the order range
            r = await c.post("/v1/orders", headers=auth(token), json={"amount_rub": "52000", "external_id": "b-1"})
            order = await r.json()
            assert (r.status, order["status"], order["next_action"]) == (202, "searching_requisites", "wait")
            assert order["stage"] == {"step": 1, "of": 4, "title": "Подбор реквизитов"}

            async def status():
                async with models.Session() as s:
                    await api.enqueue_changes(s)
                return await (await c.get(f"/v1/orders/{order['id']}", headers=auth(token))).json()

            assert (order["detail"], order["flow"]["type"]) == ("searching_merchant", "order_requisites")
            await b.run(cb(M1, f"orq:take:{order['id']}:B"))
            got = await status()
            assert (got["status"], got["detail"]) == ("merchant_assigned", "waiting_bybit_order")
            assert got["flow"] == {"type": "order_requisites", "via": "bybit_order", "operator_assigned": False}
            await b.run(msg(M1, LINK))
            got = await status()
            assert got["status"] == "requisites_check" and got["stage"]["title"] == "Проверка реквизитов"
            assert got["requisites"] is None and got["search_expires_at"] and got["detail"] == "waiting_operator"
            await b.run(cb(OP, f"opq:go:{order['id']}"))
            got = await status()
            assert got["detail"] == "operator_checking_order" and got["flow"]["operator_assigned"]
            assert got["status_text"] == "Оператор проверяет ордер и выдаёт реквизиты"
            # a long poll answers at once when the order already differs from what the client saw
            fast = await (await c.get(f"/v1/orders/{order['id']}?wait=30&since=waiting_operator",
                                      headers=auth(token))).json()
            assert fast["detail"] == "operator_checking_order"
            await give(b, OP, order["id"])
            got = await status()
            assert got["status"] == "awaiting_payment" and got["next_action"] == "pay_and_upload_receipt"
            assert got["requisites"]["number"] == "5536913812345672" and got["stage"]["step"] == 2
            held = await (await c.get(f"/v1/orders/{order['id']}?wait=1&since=awaiting_payment",
                                      headers=auth(token))).json()
            assert held["status"] == "awaiting_payment"  # nothing changed: answered after the wait
            assert (await c.get(f"/v1/orders/{order['id']}?wait=x&since=a", headers=auth(token))).status == 422
            hist = await (await c.get(f"/v1/orders/{order['id']}/history", headers=auth(token))).json()
            assert [h["status"] for h in hist["history"]] == ["searching_requisites", "merchant_assigned",
                                                              "requisites_check", "awaiting_payment"]
            assert (await c.post(f"/v1/orders/{order['id']}/cancel", headers=auth(token))).status == 200
    go(fn)


def test_requisites_in_one_message():
    from bot.handlers.orders import parse_requisites
    assert parse_requisites("2200 7001 2345 6781 Сбербанк")[0][1] == "2200700123456781"
    assert parse_requisites("2200 7001 2345 6789 Сбербанк")[0] is None  # a typo in the card: Luhn
    assert parse_requisites("5536 9138 1234 5672 Сбер") == (("card", "5536913812345672", "Сбербанк", ""), "")
    assert parse_requisites("Альфа-Банк 5536913812345672\nИванов Иван") == \
        (("card", "5536913812345672", "Альфа-Банк", "Иванов Иван"), "")
    assert parse_requisites("89001234567 Почта Банк") == (("sbp", "+79001234567", "Почта Банк", ""), "")
    assert parse_requisites("+7 900 123-45-67")[1] == "Добавьте банк получателя"
    assert parse_requisites("Сбербанк")[1] == "Не вижу номера карты или телефона"
