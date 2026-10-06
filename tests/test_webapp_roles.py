"""The mini app's working roles through the API: an order merchant takes a request and gives requisites, the seller
disputes with a file, the admin decides; the operator of a Bybit order; the admin's desk; avatar and the page log."""
from decimal import Decimal as D

from aiohttp import FormData

from bot import models
from bot.models import Deal, Operator, User
from tests.harness import cb, msg
from tests.test_api import PDF_BYTES, http
from tests.test_orders import merchant, request
from tests.test_scenarios import ADMIN, BUYER, SELLER, create_deal, ready
from tests.test_webapp import as_

M = 40
LINK = "https://www.bybit.com/fiat/trade/otc/orderList/1873200077"


def pdf_form(**fields) -> FormData:
    form = FormData()
    for k, v in fields.items():
        form.add_field(k, v)
    form.add_field("file", PDF_BYTES, filename="proof.pdf", content_type="application/pdf")
    return form


def test_merchant_takes_from_balance_and_gives_requisites(go):
    async def fn(b):
        await ready(b)
        await merchant(b, M)
        d = await request(b)
        async with http(b) as c:
            cab = await (await c.get("/app/api/merchant", headers=as_(M))).json()
            assert cab["status"] == "approved" and [o["id"] for o in cab["offers"]] == [d.id]
            offer = (await (await c.get(f"/app/api/requests/{d.id}", headers=as_(M))).json())["deal"]
            assert offer["role"] == "offer" and "take" in offer["actions"] and offer["take"]["balance_problem"] == ""
            assert (await c.get(f"/app/api/requests/{d.id}", headers=as_(BUYER))).status == 409  # his own request
            r = await c.post(f"/app/api/deals/{d.id}/take", headers=as_(M), json={"mode": "balance"})
            taken = (await r.json())["deal"]
            assert taken["status"] == "assigned" and "give" in taken["actions"] and taken["give"]["choices"]
            again = await c.post(f"/app/api/deals/{d.id}/take", headers=as_(SELLER), json={"mode": "balance"})
            assert again.status == 409  # taken
            r = await c.post(f"/app/api/deals/{d.id}/requisites", headers=as_(M),
                             json={"number": "5536 9138 1234 5672", "bank": "Т-Банк", "holder": "Петров Пётр",
                                   "minutes": taken["give"]["choices"][0]})
            given = (await r.json())["deal"]
            assert given["status"] == "waiting_payment"
            buyer = (await (await c.get(f"/app/api/deals/{d.id}", headers=as_(BUYER))).json())["deal"]
            assert buyer["requisites"]["number"] == "5536913812345672" and buyer["requisites"]["bank"] == "Т-Банк"
            # an outsider cannot give requisites for it
            assert (await c.post(f"/app/api/deals/{d.id}/requisites", headers=as_(SELLER),
                                 json={"text": "2200 7001 2345 6781 Сбербанк"})).status == 409
    go(fn)


def test_seller_disputes_with_a_file_and_the_admin_decides(go):
    async def fn(b):
        await ready(b)
        d = await create_deal(b)
        async with http(b) as c:
            form = FormData()
            form.add_field("file", PDF_BYTES, filename="check.pdf", content_type="application/pdf")
            await c.post(f"/app/api/deals/{d.id}/receipt", headers=as_(BUYER), data=form)
            seller = (await (await c.get(f"/app/api/deals/{d.id}", headers=as_(SELLER))).json())["deal"]
            assert {"confirm", "dispute", "receipt_view"} <= set(seller["actions"])
            no_proof = FormData()
            no_proof.add_field("reason", "not_received")
            assert (await c.post(f"/app/api/deals/{d.id}/dispute", headers=as_(SELLER), data=no_proof)).status == 422
            r = await c.post(f"/app/api/deals/{d.id}/dispute", headers=as_(SELLER),
                             data=pdf_form(reason="not_received", text="В выписке нет"))
            disputed = (await r.json())["deal"]
            assert disputed["status"] == "dispute" and disputed["dispute"]["files"] == 2
            assert (await c.post(f"/app/api/deals/{d.id}/resolve", headers=as_(SELLER), json={"verdict": "s"})).status == 403
            admin = (await (await c.get(f"/app/api/deals/{d.id}", headers=as_(ADMIN))).json())["deal"]
            assert "resolve" in admin["actions"] and {v["code"] for v in admin["verdicts"]} >= {"b", "s"}
            assert [f["n"] for f in admin["files"]] == ["r", 0, 1] and admin["parties"][0]["id"] == BUYER
            assert (await c.post(f"/app/api/deals/{d.id}/files/0/send", headers=as_(ADMIN))).status == 200
            assert (await c.post(f"/app/api/deals/{d.id}/files/0/send", headers=as_(BUYER))).status == 403
            desk = await (await c.get("/app/api/admin", headers=as_(ADMIN))).json()
            assert desk["counts"]["disputes"] == 1 and desk["disputes"][0]["id"] == d.id
            assert (await c.get("/app/api/admin", headers=as_(BUYER))).status == 403
            r = await c.post(f"/app/api/deals/{d.id}/resolve", headers=as_(ADMIN),
                             json={"verdict": "s", "comment": "Перевод не найден"})
            assert (await r.json())["deal"]["status"] == "cancelled"
            r = await c.post(f"/app/api/deals/{d.id}/resolve", headers=as_(ADMIN), json={"verdict": "b"})
            assert r.status == 409  # decided once
        async with models.Session() as s:
            deal = await s.get(Deal, d.id)
            assert deal.resolution == "Перевод не найден"
            seller_u = await s.get(User, SELLER)
            assert seller_u.frozen == 0 and seller_u.balance == D(200)  # the seller's USDT back
    go(fn)


def test_operator_flow_bybit_order_and_repay(go):
    async def fn(b):
        await ready(b)
        await merchant(b, M, balance=D(0))
        d = await request(b)
        async with http(b) as c:
            await c.post(f"/app/api/deals/{d.id}/take", headers=as_(M), json={"mode": "bybit"})
            r = await c.post(f"/app/api/deals/{d.id}/link", headers=as_(M), json={"url": "not a link"})
            assert r.status == 422
            r = await c.post(f"/app/api/deals/{d.id}/link", headers=as_(M), json={"url": LINK})
            assert (await r.json())["deal"]["status"] == "checking"
            free = await (await c.get("/app/api/operator", headers=as_(ADMIN))).json()
            assert [x["id"] for x in free["free"]] == [d.id]
            await c.post(f"/app/api/deals/{d.id}/accept", headers=as_(ADMIN))
            r = await c.post(f"/app/api/deals/{d.id}/requisites", headers=as_(ADMIN), json={"text": "+7 900 123-45-67 Сбербанк"})
            assert (await r.json())["deal"]["status"] == "waiting_payment"
            form = FormData()
            form.add_field("file", PDF_BYTES, filename="check.pdf", content_type="application/pdf")
            await c.post(f"/app/api/deals/{d.id}/receipt", headers=as_(BUYER), data=form)
            r = await c.post(f"/app/api/deals/{d.id}/confirm", headers=as_(ADMIN))
            done = (await r.json())["deal"]
            assert done["status"] == "completed" and done["rating_id"]
            r = await c.post(f"/app/api/ratings/{done['rating_id']}", headers=as_(ADMIN), json={"score": 9})
            assert (await r.json())["ok"]
            assert (await c.post(f"/app/api/ratings/{done['rating_id']}", headers=as_(ADMIN), json={"score": 3})).status == 409
            async with models.Session() as s:
                debt = (await s.get(Operator, ADMIN)).debt
                assert debt > 0
                (await s.get(User, ADMIN)).balance = D(1000)
                await s.commit()
            r = await (await c.post("/app/api/operator/repay", headers=as_(ADMIN))).json()
            assert D(r["repaid"]) == debt and D(r["debt"]) == 0
    go(fn)


def test_buyer_dispute_after_the_wait_and_evidence(go):
    async def fn(b):
        await ready(b)
        d = await create_deal(b)
        async with http(b) as c:
            form = FormData()
            form.add_field("file", PDF_BYTES, filename="check.pdf", content_type="application/pdf")
            await c.post(f"/app/api/deals/{d.id}/receipt", headers=as_(BUYER), data=form)
            r = await c.post(f"/app/api/deals/{d.id}/dispute", headers=as_(BUYER), data=pdf_form(text="Перевёл в 12:00"))
            assert r.status == 409  # too early: the seller still has time
            async with models.Session() as s:
                deal = await s.get(Deal, d.id)
                deal.paid_at = deal.paid_at.replace(year=deal.paid_at.year - 1)
                await s.commit()
            r = await c.post(f"/app/api/deals/{d.id}/dispute", headers=as_(BUYER), data=pdf_form(text="Перевёл в 12:00"))
            deal = (await r.json())["deal"]
            assert deal["status"] == "dispute" and deal["dispute"]["mine"] == 2 and "evidence" in deal["actions"]
            r = await c.post(f"/app/api/deals/{d.id}/evidence", headers=as_(SELLER), data=pdf_form())
            assert (await r.json())["deal"]["dispute"]["files"] == 3
            empty = FormData()
            empty.add_field("text", "")
            assert (await c.post(f"/app/api/deals/{d.id}/evidence", headers=as_(SELLER), data=empty)).status == 422
    go(fn)


def test_avatar_log_and_page_headers(go):
    async def fn(b):
        await ready(b)
        await b.run(msg(BUYER, "/start"), cb(BUYER, "menu"))
        async with http(b) as c:
            assert (await (await c.get("/app/api/avatar", headers=as_(BUYER))).json()) == {"url": None}
            r = await c.post("/app/api/log", headers=as_(BUYER), json={"message": "TypeError: x is null", "path": "deal/1"})
            assert (await r.json())["ok"]
            page = await c.get("/app")
            csp = page.headers["Content-Security-Policy"]
            assert "static.cloudflareinsights.com" in csp and "media-src 'self' blob:" in csp
            me = await (await c.get("/app/api/me", headers=as_(SELLER))).json()
            assert me["work"]["cards_on"] == 1 and me["terms"]["seller_pct"] == "5"
    go(fn)
