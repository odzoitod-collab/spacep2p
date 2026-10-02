"""Strait Pay merchant API end to end: application -> approval -> token -> order -> receipt -> success, webhooks."""
import json
import re
from decimal import Decimal as D

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from bot import models, tasks
from bot.api.server import build_app, limiter
from bot.models import ApiApplication, ApiClient, ApiEvent
from bot.services import api
from tests.harness import cb, msg, plain
from tests.test_scenarios import ADMIN, BUYER, OTHER, SELLER, ready, user

PDF_BYTES = b"%PDF-1.4\n% bank receipt\n"


async def apply_and_approve(b, uid=BUYER) -> str:
    """The user fills the form, the admin approves, the user issues a token. Returns the token."""
    await b.run(msg(uid, "/start"), cb(uid, "api"), cb(uid, "api:apply"), msg(uid, "Shop One"),
                msg(uid, "https://shop.one"), cb(uid, "api:t:0"), cb(uid, "api:v:1"), cb(uid, "api:skip"))
    assert "Заявка отправлена" in plain(b.session.last(uid))
    async with models.Session() as s:
        app = await s.scalar(select(ApiApplication).where(ApiApplication.user_id == uid))
    await b.run(cb(ADMIN, f"aap:{app.id}"), cb(ADMIN, f"aap:ok:{app.id}"))
    assert "одобрена" in plain(b.session.last(uid)).lower()
    await b.run(cb(uid, "api:tok"))
    return re.search(r"sp_live_[\w-]+", plain(b.session.last(uid))).group(0)


def http(b) -> TestClient:
    return TestClient(TestServer(build_app(b.bot)))


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_full_order_flow(go):
    async def fn(b):
        await ready(b)
        token = await apply_and_approve(b)
        async with models.Session() as s:  # only the hash is stored
            client = await s.scalar(select(ApiClient))
            assert client.token_hash == api.hash_token(token) and token not in (client.token_hash or "")
        async with http(b) as c:
            assert (await c.get("/v1/me")).status == 401
            assert (await c.get("/v1/me", headers=auth("sp_live_wrong"))).status == 401
            me = await (await c.get("/v1/me", headers=auth(token))).json()
            assert me["project"] == "Shop One" and me["limits"]["max_open_orders"] == 10
            liq = await (await c.get("/v1/liquidity", headers=auth(token))).json()
            assert liq["available"] and liq["ranges"][0]["bank"] == "Сбербанк"

            r = await c.post("/v1/orders", headers=auth(token), json={"amount_rub": "10000", "external_id": "o-1"})
            assert r.status == 201
            order = await r.json()
            assert order["status"] == "awaiting_payment" and order["amount_usdt"] == "94.000000"
            assert order["requisites"]["number"] == "4111111111111111" and order["requisites"]["holder"] == "Иванов Иван"
            again = await c.post("/v1/orders", headers=auth(token), json={"amount_rub": "10000", "external_id": "o-1"})
            assert again.status == 200 and (await again.json())["id"] == order["id"]  # idempotent

            bad = await c.post(f"/v1/orders/{order['id']}/receipt", headers=auth(token), data=b"not a pdf")
            assert bad.status == 422 and (await bad.json())["error"]["code"] == "not_a_receipt"
            form = FormData()
            form.add_field("file", PDF_BYTES, filename="check.pdf", content_type="application/pdf")
            r = await c.post(f"/v1/orders/{order['id']}/receipt", headers=auth(token), data=form)
            assert r.status == 200 and (await r.json())["status"] == "verifying"
            assert "Покупатель прислал чек" in plain([m.caption for m in b.session.calls if type(m).__name__ ==
                                                      "SendDocument" and m.chat_id == SELLER][-1])

            await b.run(cb(SELLER, f"dl:ok2:{order['id']}"))  # the merchant sees the money and confirms
            done = await (await c.get(f"/v1/orders/{order['id']}", headers=auth(token))).json()
            assert done["status"] == "success" and done["requisites"] is None
            bal = await (await c.get("/v1/balance", headers=auth(token))).json()
            assert D(bal["available"]) == D(94)  # credited to the token owner like a normal purchase
            listed = await (await c.get("/v1/orders?status=success", headers=auth(token))).json()
            assert [o["id"] for o in listed["orders"]] == [order["id"]]

        # the owner's personal chat is not flooded: no deal notifications, personal lists stay clean
        assert not [t for t in b.session.texts(BUYER) if "Сделка #" in t and "завершена" in t]
        await b.run(cb(BUYER, "deals"))
        assert "Сделок пока нет" in plain(b.session.last(BUYER))
    go(fn)


def test_webhooks_are_signed_and_retried(go, monkeypatch):
    async def fn(b):
        await ready(b)
        token = await apply_and_approve(b)
        async with models.Session() as s:
            (await s.scalar(select(ApiClient))).webhook_url = "https://hooks.example.com/strait"
            await s.commit()
        sent, answers = [], [False, True, True, True]

        async def fake_post(client, body, event_id):
            sent.append(json.loads(body))
            return answers.pop(0), "HTTP 500"
        monkeypatch.setattr(api, "post", fake_post)
        async with http(b) as c:
            order = await (await c.post("/v1/orders", headers=auth(token), json={"amount_rub": "5000"})).json()
            await c.post(f"/v1/orders/{order['id']}/cancel", headers=auth(token))
        await tasks.api_webhooks(b.bot)  # first attempt fails
        assert sent[-1]["status"] == "cancelled" and sent[-1]["order"]["id"] == order["id"]
        async with models.Session() as s:
            ev = await s.scalar(select(ApiEvent))
            assert ev.attempts == 1 and ev.delivered_at is None
            ev.next_at = models.now()
            await s.commit()
        await tasks.api_webhooks(b.bot)  # retried and delivered
        async with models.Session() as s:
            assert (await s.scalar(select(ApiEvent))).delivered_at is not None
        body = b'{"a":1}'
        assert api.sign("secret", "1700000000", body) == api.sign("secret", "1700000000", body) != api.sign(
            "other", "1700000000", body)
    go(fn)


def test_limits_errors_and_rate_limit(go):
    async def fn(b):
        await ready(b, balance=D(1000))
        token = await apply_and_approve(b)
        async with models.Session() as s:
            (await s.scalar(select(ApiClient))).max_open = 1
            await s.commit()
        async with http(b) as c:
            r = await c.post("/v1/orders", headers=auth(token), json={"amount_rub": "abc"})
            assert r.status == 422 and (await r.json())["error"]["code"] == "invalid_amount"
            r = await c.post("/v1/orders", headers=auth(token), json={"amount_rub": "100"})
            assert (await r.json())["error"]["code"] == "amount_limit"
            assert (await c.post("/v1/orders", headers=auth(token), json={"amount_rub": "3000"})).status == 201
            r = await c.post("/v1/orders", headers=auth(token), json={"amount_rub": "3000"})
            assert r.status == 409  # one open order allowed, and the only card is busy anyway
            assert (await c.get("/v1/orders/999", headers=auth(token))).status == 404
            limiter._buckets.clear()
            codes = [(await c.get("/v1/balance", headers=auth(token))).status for _ in range(15)]
            assert 429 in codes and codes[0] == 200
    go(fn)


def test_foreign_orders_suspension_and_ssrf(go):
    async def fn(b):
        await ready(b)
        token = await apply_and_approve(b)
        async with http(b) as c:
            order = await (await c.post("/v1/orders", headers=auth(token), json={"amount_rub": "5000"})).json()
        other = await apply_and_approve(b, OTHER)
        async with http(b) as c:
            assert (await c.get(f"/v1/orders/{order['id']}", headers=auth(other))).status == 404  # not yours
        await b.run(cb(BUYER, "api:wh"), msg(BUYER, "https://127.0.0.1/hook"))
        assert "внутреннюю сеть" in plain(b.session.last(BUYER))
        async with models.Session() as s:
            cid = (await s.scalar(select(ApiClient).where(ApiClient.user_id == BUYER))).id
        await b.run(cb(ADMIN, f"acl:st:{cid}:0"))
        async with http(b) as c:
            r = await c.get("/v1/me", headers=auth(token))
            assert r.status == 403 and (await r.json())["error"]["code"] == "suspended"
        await b.run(cb(ADMIN, f"acl:st:{cid}:1"), cb(ADMIN, f"acl:rv:{cid}"))
        async with http(b) as c:
            assert (await c.get("/v1/me", headers=auth(token))).status == 401  # revoked
    go(fn)


def test_rejection_with_reason_and_docs_served(go):
    async def fn(b):
        await ready(b)
        await b.run(msg(OTHER, "/start"), cb(OTHER, "api"), cb(OTHER, "api:apply"), msg(OTHER, "Casino X"),
                    msg(OTHER, "https://x.bet"), msg(OTHER, "реклама в каналах"), msg(OTHER, "50 млн"),
                    msg(OTHER, "Ставки на спорт, средний чек 3000"))
        await b.run(cb(ADMIN, "aapi"), cb(ADMIN, "aap:1"), cb(ADMIN, "aap:no:1"), msg(ADMIN, "Гемблинг не принимаем"))
        assert "Гемблинг не принимаем" in plain(b.session.last(OTHER))
        await b.run(cb(OTHER, "api"))
        assert "Подать заявку снова можно после" in plain(b.session.last(OTHER))
        async with http(b) as c:
            text = await (await c.get("/docs")).text()
            assert "<h1" in text and "Strait Pay API" in text and "/v1/orders" in text  # an HTML page
            assert "# Strait Pay API" in await (await c.get("/docs.md")).text()
            for slug in ("help", "start", "buy", "sell", "merchant", "orders", "operator", "team", "wallet", "disputes"):
                r = await c.get(f"/docs/{slug}")
                assert r.status == 200 and "<article>" in await r.text(), slug
            assert (await c.get("/docs/nope")).status == 404
            assert (await (await c.get("/")).json())["name"] == "Strait Pay API"
        assert (await user(OTHER)).balance == 0
    go(fn)
