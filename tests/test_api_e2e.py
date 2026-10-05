"""Another bot or site works through the API end to end, against the real bot: an order on a static card, an order
requisites request worked by a merchant through his Bybit order and an operator (not an admin, never applied for
entry), one worked from a merchant's balance, a dispute, a cancel, a late receipt. Every status reaches the client
both by GET and by a signed webhook, in order; the money ends where it should."""
from decimal import Decimal as D

from aiohttp import FormData
from sqlalchemy import select

from bot import models, tasks
from bot.models import Deal, Operator, OrderOffer, User
from bot.services import api, settings
from tests.harness import cb, plain
from tests.test_api import PDF_BYTES, apply_and_approve, auth, http
from tests.test_orders import LINK, M1, give, merchant
from tests.test_scenarios import ADMIN, BUYER, OTHER, SELLER, ready

OPER = OTHER  # an operator added in the panel who never passed the entry application


class Client:
    """What the other bot does: create, poll, upload the receipt, cancel, dispute."""

    def __init__(self, c, token):
        self.c, self.h = c, auth(token)

    async def create(self, amount, ext, **kw):
        r = await self.c.post("/v1/orders", headers=self.h, json={"amount_rub": amount, "external_id": ext, **kw})
        return r.status, await r.json()

    async def get(self, oid):
        async with models.Session() as s:
            await api.enqueue_changes(s)
        return await (await self.c.get(f"/v1/orders/{oid}", headers=self.h)).json()

    async def receipt(self, oid):
        form = FormData()
        form.add_field("file", PDF_BYTES, filename="check.pdf", content_type="application/pdf")
        r = await self.c.post(f"/v1/orders/{oid}/receipt", headers=self.h, data=form)
        return r.status, await r.json()

    async def post(self, oid, action):
        r = await self.c.post(f"/v1/orders/{oid}/{action}", headers=self.h)
        return r.status, await r.json()


def capture(monkeypatch):
    """Webhooks the client receives: (event id, status, order) — delivered and signed like in production."""
    got = []

    async def post(client, body, event_id):
        import json
        sig = api.sign(client.webhook_secret, "1", body)
        assert len(sig) == 64
        data = json.loads(body)
        got.append((data["id"], data["status"], data["order"]))
        return True, "HTTP 200"

    monkeypatch.setattr(api, "post", post)
    return got


async def with_webhook(b, uid=BUYER):
    async with models.Session() as s:
        cl = await s.scalar(select(api.ApiClient).where(api.ApiClient.user_id == uid))
        cl.webhook_url = "https://shop.one/hook"
        await s.commit()


async def operator_without_entry():
    async with models.Session() as s:
        s.add(Operator(user_id=OPER, active=True, debt=D(0)))
        await settings.put(s, "signup_review", "1")
        await s.commit()


async def statuses(got, oid):
    return [st for _, st, order in got if order["id"] == oid]


def test_bybit_order_from_another_bot_with_an_operator(go, monkeypatch):
    got = capture(monkeypatch)

    async def fn(b):
        await ready(b)
        await merchant(b, M1, balance=D(0))
        token = await apply_and_approve(b)
        await with_webhook(b)
        await b.run(cb(OPER, "menu"))  # registered, not let in
        await operator_without_entry()
        async with http(b) as c:
            cl = Client(c, token)
            code, order = await cl.create("52000", "bb-1", sender_bank="Т-Банк")
            oid = order["id"]
            assert code == 202 and order["detail"] == "searching_merchant" and order["requisites"] is None
            assert f"orq:take:{oid}:b" in b.session.buttons(M1)
            await b.run(cb(M1, f"orq:take:{oid}:b"))
            assert (await cl.get(oid))["detail"] == "waiting_bybit_order"
            await b.run(cb(M1, f"orq:give:{oid}"))
            from tests.harness import msg
            await b.run(msg(M1, LINK))
            got_ = await cl.get(oid)
            assert (got_["status"], got_["detail"]) == ("requisites_check", "waiting_operator")
            assert f"opq:go:{oid}" in b.session.buttons(OPER)  # the operator gets the order
            await b.run(cb(OPER, f"opq:go:{oid}"))
            assert (await cl.get(oid))["detail"] == "operator_checking_order"
            await give(b, OPER, oid)  # the operator gives the requisites in the bot…
            order = await cl.get(oid)  # …and the other bot sees them
            assert order["status"] == "awaiting_payment" and order["requisites"]["number"] == "5536913812345672"
            assert order["flow"] == {"type": "order_requisites", "via": "bybit_order", "operator_assigned": True}

            code, order = await cl.receipt(oid)  # the payer's PDF through the API
            assert code == 200 and order["status"] == "verifying"
            pdfs = [m for m in b.session.calls if type(m).__name__ == "SendDocument" and m.chat_id == OPER]
            assert pdfs and "проверьте поступление" in plain(pdfs[-1].caption)
            assert f"dl:ok:{oid}" in [x.callback_data for row in pdfs[-1].reply_markup.inline_keyboard for x in row]
            copy = [m for m in b.session.calls if type(m).__name__ == "SendDocument" and m.chat_id == BUYER]
            assert copy  # the token owner keeps a copy
            await b.run(cb(OPER, f"dl:ok:{oid}"), cb(OPER, f"dl:ok2:{oid}"))
            order = await cl.get(oid)
            assert order["status"] == "success" and order["requisites"] is None
            bal = await (await c.get("/v1/balance", headers=auth(token))).json()
            assert D(bal["available"]) == D(order["amount_usdt"])
        async with models.Session() as s:
            assert (await s.get(Operator, OPER)).debt == D(500)  # 52 000 / 104: USDT came to his Bybit
            assert (await s.get(User, M1)).frozen == 0
        await tasks.api_webhooks(b.bot)
        # every status after the creation answer, verifying too — though the operator confirmed at once
        assert await statuses(got, oid) == ["merchant_assigned", "requisites_check", "awaiting_payment", "verifying",
                                            "success"]
    go(fn)


def test_balance_order_and_static_card_from_another_bot(go, monkeypatch):
    got = capture(monkeypatch)

    async def fn(b):
        await ready(b)
        await merchant(b, M1, balance=D(1000))
        token = await apply_and_approve(b)
        await with_webhook(b)
        async with http(b) as c:
            cl = Client(c, token)
            code, static = await cl.create("10000", "st-1")  # a seller's card takes it
            assert code == 201 and static["flow"]["type"] == "static_card" and static["requisites"]["number"]
            await cl.receipt(static["id"])
            await b.run(cb(SELLER, f"dl:ok:{static['id']}"), cb(SELLER, f"dl:ok2:{static['id']}"))
            assert (await cl.get(static["id"]))["status"] == "success"

            code, order = await cl.create("52000", "bal-1")  # no card for it: order requisites
            oid = order["id"]
            await b.run(cb(M1, f"orq:take:{oid}:w"))
            o = await cl.get(oid)
            assert (o["detail"], o["flow"]["via"]) == ("merchant_preparing_requisites", "merchant_balance")
            async with models.Session() as s:
                assert (await s.get(User, M1)).frozen == D(500)
            await give(b, M1, oid)
            assert (await cl.get(oid))["status"] == "awaiting_payment"
            await cl.receipt(oid)
            await b.run(cb(M1, f"dl:ok:{oid}"), cb(M1, f"dl:ok2:{oid}"))
            assert (await cl.get(oid))["status"] == "success"
            bal = await (await c.get("/v1/balance", headers=auth(token))).json()
            assert D(bal["available"]) == D(static["amount_usdt"]) + D(order["amount_usdt"])
        async with models.Session() as s:
            m = await s.get(User, M1)
            assert (m.frozen, m.balance) == (0, D(500))
        await tasks.api_webhooks(b.bot)
        assert await statuses(got, oid) == ["merchant_assigned", "awaiting_payment", "verifying", "success"]
        assert await statuses(got, static["id"]) == ["verifying", "success"]
        ids = [i for i, _, _ in got]
        assert ids == sorted(ids) and len(set(ids)) == len(ids)  # one event each, in order
    go(fn)


def test_cancel_dispute_and_late_receipt_through_the_api(go, monkeypatch):
    capture(monkeypatch)

    async def fn(b):
        await ready(b)
        await merchant(b, M1, balance=D(0))
        token = await apply_and_approve(b)
        async with http(b) as c:
            cl = Client(c, token)
            _, req = await cl.create("52000", "c-1")
            code, req = await cl.post(req["id"], "cancel")
            assert code == 200 and req["status"] == "cancelled"
            offers = [m for m in b.session.calls if type(m).__name__ == "EditMessageText" and m.chat_id == M1]
            assert offers  # the merchants' offers close
            async with models.Session() as s:
                assert not await s.scalar(select(OrderOffer).where(OrderOffer.deal_id == req["id"],
                                                                   ~OrderOffer.declined, OrderOffer.msg_id.is_not(None)))

            _, o = await cl.create("10000", "d-1")  # the merchant stays silent: a dispute
            await cl.receipt(o["id"])
            code, err = await cl.post(o["id"], "dispute")
            assert code == 409 and err["error"]["code"] == "dispute_not_available"  # not yet
            async with models.Session() as s:
                d = await s.get(Deal, o["id"])
                d.paid_at = models.now().replace(year=2020)
                await s.commit()
            code, o = await cl.post(o["id"], "dispute")
            assert code == 200 and o["status"] == "dispute"
            await b.run(cb(ADMIN, f"ar:{o['id']}:b"), cb(ADMIN, f"ar2:{o['id']}:b"))
            assert (await cl.get(o["id"]))["status"] == "success"

            _, late = await cl.create("10000", "l-1")  # paid in the last minute: the receipt after the time
            async with models.Session() as s:
                d = await s.get(Deal, late["id"])
                d.expires_at = models.now().replace(year=2020)
                await s.commit()
            await tasks.expire_deals(b.bot)
            late = await cl.get(late["id"])
            assert late["status"] == "expired" and late["next_action"] == "upload_late_receipt"
            code, late = await cl.receipt(late["id"])
            assert code == 200 and late["status"] == "verifying"
    go(fn)
