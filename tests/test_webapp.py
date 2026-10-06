"""The mini app: Telegram sign-in by initData, the same gates as the bot, and the money flows through the same
functions — buying, the deal with its requisites, the PDF receipt, the seller's confirmation, the deal chat, the
wallet and the cards."""
import hashlib
import hmac
import json
import time
import urllib.parse
from decimal import Decimal as D

from aiohttp import FormData

from bot import models
from bot.config import config
from bot.models import Card, Deal, User
from tests.test_api import PDF_BYTES, http
from tests.test_scenarios import BUYER, SELLER, ready


def init_data(uid: int, age: int = 0, token: str | None = None) -> str:
    fields = {"auth_date": str(int(time.time()) - age), "query_id": "AAE",
              "user": json.dumps({"id": uid, "first_name": "Тест"}, ensure_ascii=False)}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", (token or config.bot_token).encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(fields)


def as_(uid: int, **kw) -> dict:
    return {"X-Telegram-Init-Data": init_data(uid, **kw)}


def test_sign_in_and_gates(go):
    async def fn(b):
        await ready(b)
        async with http(b) as c:
            page = await c.get("/app")
            assert page.status == 200 and "app.js?v=" in await page.text()
            assert "Content-Security-Policy" in page.headers
            assert (await c.get("/app/api/me")).status == 401  # outside Telegram
            assert (await c.get("/app/api/me", headers=as_(BUYER, token="1:forged"))).status == 401
            r = await c.get("/app/api/me", headers=as_(BUYER, age=2 * 86400))
            assert r.status == 401 and (await r.json())["error"]["code"] == "expired"
            r = await c.get("/app/api/me", headers=as_(777))  # never opened the bot
            assert (await r.json())["error"]["code"] == "start_bot"
            me = await (await c.get("/app/api/me", headers=as_(BUYER))).json()
            assert me["user"]["id"] == BUYER and me["rate"]["rate"]
            async with models.Session() as s:
                (await s.get(User, BUYER)).is_banned = True
                await s.commit()
            r = await c.get("/app/api/me", headers=as_(BUYER))
            assert r.status == 403 and (await r.json())["error"]["code"] == "banned"
    go(fn)


def test_buy_pay_confirm_and_chat_in_the_app(go):
    async def fn(b):
        await ready(b)
        async with http(b) as c:
            q = await (await c.get("/app/api/buy/quote?amount=10000", headers=as_(BUYER))).json()
            assert q["mode"] == "card" and q["card_id"]
            r = await c.post("/app/api/buy", headers=as_(BUYER), json={"amount_rub": q["amount_rub"],
                                                                        "credit": q["credit"], "card_id": q["card_id"]})
            d = (await r.json())["deal"]
            assert d["status"] == "waiting_payment" and d["requisites"]["number"] == "4111111111111111"
            assert "receipt" in d["actions"] and "cancel" in d["actions"]
            assert any("Новая сделка" in str(getattr(m, "text", "")) for m in b.session.calls
                       if getattr(m, "chat_id", None) == SELLER)  # the seller is told as from the bot

            seller = await (await c.get(f"/app/api/deals/{d['id']}", headers=as_(SELLER))).json()
            assert seller["deal"]["role"] == "seller" and seller["deal"]["income"]

            r = await c.post(f"/app/api/deals/{d['id']}/chat", headers=as_(BUYER), json={"text": "пиши @someone"})
            assert r.status == 422  # no usernames, as in the bot
            r = await c.post(f"/app/api/deals/{d['id']}/chat", headers=as_(BUYER), json={"text": "Перевёл"})
            chat = await r.json()
            assert chat["messages"][-1]["text"] == "Перевёл" and chat["messages"][-1]["mine"]
            other = await (await c.get(f"/app/api/deals/{d['id']}/chat", headers=as_(SELLER))).json()
            assert other["messages"][-1]["role"] == "Покупатель" and not other["messages"][-1]["mine"]

            form = FormData()
            form.add_field("file", b"not a pdf", filename="x.pdf")
            assert (await c.post(f"/app/api/deals/{d['id']}/receipt", headers=as_(BUYER), data=form)).status == 422
            form = FormData()
            form.add_field("file", PDF_BYTES, filename="check.pdf", content_type="application/pdf")
            r = await c.post(f"/app/api/deals/{d['id']}/receipt", headers=as_(BUYER), data=form)
            assert (await r.json())["deal"]["status"] == "paid"
            assert (await c.post(f"/app/api/deals/{d['id']}/confirm", headers=as_(BUYER))).status == 409
            r = await c.post(f"/app/api/deals/{d['id']}/confirm", headers=as_(SELLER))
            assert (await r.json())["deal"]["status"] == "completed"
            assert (await c.post(f"/app/api/deals/{d['id']}/confirm", headers=as_(SELLER))).status == 409  # once
        async with models.Session() as s:
            assert (await s.get(Deal, d["id"])).status == "completed"
            assert (await s.get(User, BUYER)).balance == D(d["usdt"])
    go(fn)


def test_withdraw_cards_and_history_in_the_app(go):
    async def fn(b):
        await ready(b)
        async with http(b) as c:
            w = await (await c.get("/app/api/wallet", headers=as_(SELLER))).json()
            assert D(w["balance"]["available"]) == D(200)
            q = await (await c.get("/app/api/withdraw/quote?amount=50", headers=as_(SELLER))).json()
            assert D(q["receive"]) == D("48.25") and not q["error"]
            dep = await (await c.get("/app/api/deposit", headers=as_(SELLER))).json()
            assert dep["address"].startswith("UQ") and dep["network"] == "TON"
            own = {"amount": "50", "fee": q["fee"], "request_id": "7a6e0804-2bd0-4672-b79d-d97b2fd3c1b4",
                   "address": dep["address"]}
            assert (await c.post("/app/api/withdraw", headers=as_(SELLER), json=own)).status == 422  # our own address
            body = {"amount": "50", "fee": q["fee"], "request_id": "8a6e0804-2bd0-4672-b79d-d97b2fd3c1b4",
                    "address": "UQBvW8Z5huBkMJYdnfAEM5JqTNkuWX3diqYENkWsIL0XglxD", "memo": "123"}
            r = await (await c.post("/app/api/withdraw", headers=as_(SELLER), json=body)).json()
            assert r["ok"] and D(r["available"]) == D(150) and r["withdrawal"]["cancellable"]
            assert r["withdrawal"]["memo"] == "123"
            again = await c.post("/app/api/withdraw", headers=as_(SELLER), json=body)  # the same request: once
            assert again.status == 409
            too_much = dict(body, amount="1000", request_id="9a6e0804-2bd0-4672-b79d-d97b2fd3c1b4")
            assert (await c.post("/app/api/withdraw", headers=as_(SELLER), json=too_much)).status == 422
            hist = await (await c.get("/app/api/history", headers=as_(SELLER))).json()
            assert hist["items"][0]["kind"] == "withdraw" and D(hist["items"][0]["delta"]) == D(-50)

            cards = await (await c.get("/app/api/cards", headers=as_(SELLER))).json()
            cid = cards["cards"][0]["id"]
            r = await c.post(f"/app/api/cards/{cid}", headers=as_(SELLER), json={"min": "60000", "max": "90000"})
            assert r.status == 200  # both raised at once: max first
            r = await c.post(f"/app/api/cards/{cid}", headers=as_(SELLER), json={"active": False})
            assert not (await r.json())["cards"][0]["active"]
            r = await c.post(f"/app/api/cards/{cid}", headers=as_(BUYER), json={"active": True})
            assert r.status == 404  # not his card
        async with models.Session() as s:
            card = await s.get(Card, cid)
            assert (card.min_rub, card.max_rub, card.is_active) == (D(60000), D(90000), False)
    go(fn)


def test_the_operator_works_a_bybit_order_in_the_app(go):
    from tests.test_order_guard import link_given
    from tests.test_orders import M1, OP, merchant, request

    async def fn(b):
        await ready(b)
        await merchant(b, M1, balance=D(0))
        d = await link_given(b, await request(b))
        async with http(b) as c:
            cab = await (await c.get("/app/api/operator", headers=as_(OP))).json()
            assert [x["id"] for x in cab["free"]] == [d.id]
            assert (await c.get("/app/api/operator", headers=as_(BUYER))).status == 403
            assert (await c.post(f"/app/api/deals/{d.id}/accept", headers=as_(BUYER))).status == 403
            view = (await (await c.get(f"/app/api/deals/{d.id}", headers=as_(OP))).json())["deal"]
            assert "accept" in view["actions"] and view["bybit_url"]
            r = (await (await c.post(f"/app/api/deals/{d.id}/accept", headers=as_(OP))).json())["deal"]
            assert {"give", "pass_on", "recreate", "close"} <= set(r["actions"])
            bad = await c.post(f"/app/api/deals/{d.id}/requisites", headers=as_(OP), json={"text": "Сбербанк"})
            assert bad.status == 422
            r = await c.post(f"/app/api/deals/{d.id}/requisites", headers=as_(OP),
                             json={"text": "5536 9138 1234 5672 Сбер"})
            r = (await r.json())["deal"]
            assert r["status"] == "waiting_payment" and r["held"] and r["expires_at"] is None
            buyer = (await (await c.get(f"/app/api/deals/{d.id}", headers=as_(BUYER))).json())["deal"]
            assert buyer["requisites"]["number"] == "5536913812345672" and buyer["requisites"]["bank"] == "Сбербанк"
            r = (await (await c.post(f"/app/api/deals/{d.id}/recreate", headers=as_(OP))).json())["deal"]
            assert r["status"] == "assigned"
            cab = await (await c.get("/app/api/operator", headers=as_(OP))).json()
            assert [x["status"] for x in cab["working"]] == ["assigned"]
    go(fn)
