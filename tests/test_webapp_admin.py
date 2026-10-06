"""The mini app's new parts: a merchant's line, the team leader's cabinet, the admin's people, applications and cash
desk, a PDF receipt drawn in the page."""
from decimal import Decimal as D

from sqlalchemy import select

from bot import models
from bot.models import OrderMerchant, Signup, Team, User
from bot.services import admins, orders
from tests.harness import cb, msg
from tests.test_api import http
from tests.test_orders import M1, merchant, request
from tests.test_scenarios import ADMIN, BUYER, PDF, SELLER, create_deal, ready, user
from tests.test_webapp import as_

STAFF = 30


def test_merchant_leaves_the_line_and_comes_back(go):
    async def fn(b):
        await ready(b)
        await merchant(b, M1)
        async with http(b) as c:
            m = await (await c.get("/app/api/merchant", headers=as_(M1))).json()
            assert m["online"] is True
            r = await c.post("/app/api/merchant", headers=as_(M1), json={"online": False})
            assert (await r.json())["online"] is False and (await r.json())["offers"] == []
            me = await (await c.get("/app/api/me", headers=as_(M1))).json()
            assert me["work"]["line"] is False
        d = await request(b)
        async with models.Session() as s:  # off the line: not offered, and cannot take
            assert M1 not in [u.id for _, u in await orders.eligible(s, d)]
            m, u = await s.get(OrderMerchant, M1), await s.get(User, M1)
            assert "не на линии" in orders.fit_problem(m, u, d, True)
        await b.run(cb(M1, "om"), cb(M1, "om:line:1"))  # the bot's cabinet switches it back too
        async with models.Session() as s:
            assert not (await s.get(OrderMerchant, M1)).offline
    go(fn)


def test_team_leader_cabinet_in_the_app(go):
    async def fn(b):
        await ready(b)
        async with models.Session() as s:
            t = Team(leader_id=SELLER, name="Альфа", status="approved")
            s.add(t)
            await s.flush()
            (await s.get(User, SELLER)).team_id = t.id
            (await s.get(User, SELLER)).team_balance = D("12.5")
            (await s.get(User, BUYER)).team_id = t.id
            await s.commit()
        async with http(b) as c:
            t = await (await c.get("/app/api/team", headers=as_(SELLER))).json()
            assert t["leader"] and t["members"] == 1 and t["balance"] == "12.5" and t["link"].endswith("start=t1")
            assert [x["id"] for x in t["list"]] == [BUYER]
            member = await (await c.get("/app/api/team", headers=as_(BUYER))).json()
            assert not member["leader"] and "list" not in member and "balance" not in member
            assert (await c.post("/app/api/team/out", headers=as_(BUYER))).status == 403
            r = await (await c.post("/app/api/team/out", headers=as_(SELLER))).json()
            assert r["moved"] == "12.5"
        u = await user(SELLER)
        assert (u.team_balance, u.balance) == (0, D("212.5"))
    go(fn)


def test_admin_people_in_the_app(go):
    async def fn(b):
        await ready(b)
        await b.run(msg(STAFF, "/start"))
        async with models.Session() as s:
            await admins.grant(s, STAFF)
            await s.commit()
        async with http(b) as c:
            assert (await c.get("/app/api/admin/users", headers=as_(BUYER))).status == 403
            found = await (await c.get("/app/api/admin/users?q=u20", headers=as_(ADMIN))).json()
            assert [u["id"] for u in found["users"]] == [BUYER]
            card = await (await c.get(f"/app/api/admin/users/{SELLER}", headers=as_(ADMIN))).json()
            assert card["balance"] == "200" and card["roles"]["seller"] and card["owner"]

            act = lambda who, body: c.post(f"/app/api/admin/users/{BUYER}", headers=as_(who), json=body)  # noqa: E731
            r = await (await act(ADMIN, {"action": "balance", "value": "+25", "comment": "компенсация"})).json()
            assert "0 → 25" in r["message"]
            r = await (await act(STAFF, {"action": "balance", "value": "5"})).json()  # a granted admin: own rules
            assert (await user(BUYER)).balance == D(30) and "Проведено" in r["message"]
            assert (await act(ADMIN, {"action": "balance", "value": "-100"})).status == 409  # never below zero
            assert (await act(ADMIN, {"action": "rating", "value": "11"})).status == 422
            r = await (await act(ADMIN, {"action": "rating", "value": "8,5"})).json()
            assert "8.5/10" in r["message"] and (await user(BUYER)).rating == D("8.5")
            r = await (await act(ADMIN, {"action": "ban"})).json()
            assert "Заблокирован" in r["message"] and (await user(BUYER)).is_banned
            await act(ADMIN, {"action": "unban"})
            assert not (await user(BUYER)).is_banned
            no = await c.post(f"/app/api/admin/users/{STAFF}", headers=as_(ADMIN), json={"action": "ban"})
            assert no.status == 409  # an admin is never banned
    go(fn)


def test_admin_applications_and_desk_in_the_app(go, monkeypatch):
    async def fn(b):
        from bot.handlers import admin_bsc
        monkeypatch.setattr(admin_bsc, "KEY_TTL", 0)
        await ready(b)
        await b.run(msg(STAFF, "/start"))
        async with models.Session() as s:
            await admins.grant(s, STAFF)
            (await s.get(User, STAFF)).access = "pending"
            s.add(Signup(user_id=STAFF, role="seller", turnover="100к"))
            await s.commit()
        b.chain.fund_hot(usdt="70")
        async with http(b) as c:
            sus = await (await c.get("/app/api/admin/signups", headers=as_(ADMIN))).json()
            assert [x["user_id"] for x in sus["signups"]] == [STAFF]
            r = await (await c.post(f"/app/api/admin/signups/{sus['signups'][0]['id']}", headers=as_(ADMIN),
                                    json={"approve": True})).json()
            assert "Одобрена" in r["message"] and (await user(STAFF)).access == "approved"
            desk = await (await c.get("/app/api/admin/desk", headers=as_(ADMIN))).json()
            assert desk["on"] and desk["usdt"] == "70" and desk["owner"] and desk["address"].startswith("0x")
            fin = await (await c.get("/app/api/admin/finance", headers=as_(ADMIN))).json()
            assert fin["hot"] == "70" and fin["users"] == "200"
            assert (await c.post("/app/api/admin/desk/key", headers=as_(STAFF))).status == 403
            assert (await (await c.post("/app/api/admin/desk/key", headers=as_(ADMIN))).json())["sent"]
    go(fn)


def test_a_pdf_receipt_is_streamed_to_the_page(go):
    async def fn(b):
        await ready(b)
        d = await create_deal(b)
        await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, document=PDF))
        async with http(b) as c:
            r = await c.get(f"/app/api/deals/{d.id}/files/r", headers=as_(SELLER))
            assert r.status == 200 and r.content_type == "application/pdf" and (await r.read()).startswith(b"%PDF")
            assert (await c.get(f"/app/api/deals/{d.id}/files/r", headers=as_(STAFF))).status in (403, 404)
            page = await c.get("/app")
            assert "script-src 'self'" in page.headers["Content-Security-Policy"]
            js = await c.get("/app/vendor/pdf.min.js")
            assert js.status == 200 and (await c.get("/app/vendor/evil.js")).status == 404
    go(fn)
