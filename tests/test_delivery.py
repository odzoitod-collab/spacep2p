"""Nothing is lost on the way: a Bybit order reaches every operator (with sound, again after a passing Telegram
failure, and operators added later), a request reaches merchants and chats again after a failure, an API order opens
in the mini app for the administration and never answers slowly because of the broadcast."""
from datetime import timedelta
from decimal import Decimal as D

from sqlalchemy import select, update

from bot import models, tasks
from bot.models import Deal, OrderOffer, User
from tests.harness import cb, msg
from tests.test_api import apply_and_approve, auth, http
from tests.test_order_guard import link_given
from tests.test_orders import M1, OP, OP2, merchant, request
from tests.test_scenarios import ADMIN, BUYER, ready
from tests.test_webapp import as_


def offer_to(b, uid, did):
    return [m for m in b.session.calls if getattr(m, "chat_id", None) == uid and getattr(m, "reply_markup", None)
            and f"opq:go:{did}" in [x.callback_data for row in m.reply_markup.inline_keyboard for x in row]]


async def age_offers(did):
    async with models.Session() as s:
        await s.execute(update(OrderOffer).where(OrderOffer.deal_id == did).values(
            created_at=models.now() - timedelta(minutes=2)))
        await s.commit()


def test_order_reaches_every_operator_loudly_and_again_after_a_failure(go):
    async def fn(b):
        await ready(b)
        await merchant(b, M1, balance=D(0))
        async with models.Session() as s:
            (await s.get(User, OP)).quiet = True  # a quiet operator still hears an order waiting for him
            await s.commit()
        b.session.fail_once.add(OP2)  # flood control / network on the way to the second operator
        d = await link_given(b, await request(b))
        assert d.status == "checking"
        loud = offer_to(b, OP, d.id)
        assert loud and loud[-1].disable_notification is False
        assert not offer_to(b, OP2, d.id)
        await tasks.order_timeouts(b.bot)  # within the minute: not yet
        assert not offer_to(b, OP2, d.id)
        await age_offers(d.id)
        await tasks.order_timeouts(b.bot)
        assert offer_to(b, OP2, d.id) and len(offer_to(b, OP, d.id)) == 1  # the one who got it is not spammed
        await tasks.order_timeouts(b.bot)
        assert len(offer_to(b, OP2, d.id)) == 1
    go(fn)


def test_a_blocked_operator_is_not_retried(go):
    async def fn(b):
        await ready(b)
        await merchant(b, M1, balance=D(0))
        b.session.blocked.add(OP2)
        d = await link_given(b, await request(b))
        await age_offers(d.id)
        await tasks.order_timeouts(b.bot)
        async with models.Session() as s:
            rows = (await s.execute(select(OrderOffer.user_id, OrderOffer.msg_id).where(
                OrderOffer.deal_id == d.id, OrderOffer.kind == "operator"))).all()
        assert dict(rows)[OP2] == 0 and len(rows) == 2  # gone for good: remembered once, not sent again
    go(fn)


def test_merchant_gets_the_request_again_after_a_failure(go):
    async def fn(b):
        await ready(b)
        await merchant(b, M1)
        b.session.fail_once.add(M1)
        d = await request(b)
        assert f"orq:take:{d.id}:b" not in b.session.buttons(M1)
        await age_offers(d.id)
        async with models.Session() as s:  # past the first wave: everyone and the chats
            await s.execute(update(Deal).where(Deal.id == d.id).values(expires_at=models.now() + timedelta(minutes=1)))
            await s.commit()
        await tasks.order_timeouts(b.bot)
        assert f"orq:take:{d.id}:b" in b.session.buttons(M1)
    go(fn)


def test_api_order_opens_in_the_app_for_the_admin_owner(go):
    async def fn(b):
        await ready(b)
        await b.run(msg(OP2, "/start"))
        token = await apply_and_approve(b, OP2)  # the platform's own service: its token belongs to an admin
        async with http(b) as c:
            r = await c.post("/v1/orders", headers=auth(token), json={"amount_rub": "10000", "external_id": "x-1"})
            order = await r.json()
            view = await c.get(f"/app/api/deals/{order['id']}", headers=as_(OP2))
            assert view.status == 200
            deal = (await view.json())["deal"]
            assert deal["role"] == "admin" and deal["requisites"]["number"] == "4111111111111111"
            assert "cancel" not in deal["actions"] and "receipt" not in deal["actions"]  # the service pays, not him
            assert (await c.post(f"/app/api/deals/{order['id']}/cancel", headers=as_(OP2))).status == 403
            assert (await c.get(f"/app/api/deals/{order['id']}", headers=as_(ADMIN))).status == 200
        async with http(b) as c:
            token2 = await apply_and_approve(b, BUYER)
            r = await c.post("/v1/orders", headers=auth(token2), json={"amount_rub": "10000", "external_id": "y-1"})
            own = await c.get(f"/app/api/deals/{(await r.json())['id']}", headers=as_(BUYER))
            assert own.status == 404 and "API" in (await own.json())["error"]["message"]
    go(fn)
