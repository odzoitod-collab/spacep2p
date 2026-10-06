"""The admin panel after BEP-20 only: sections, the cash desk with its seed for owners, a merchant's manual rating, a
quiet admin chat, TON leftovers never hanging with users' money."""
import asyncio
from decimal import Decimal as D

from sqlalchemy import select

from bot import models
from bot.handlers import admin_bsc
from bot.models import Event, User, Withdrawal
from bot.services import admins, audit, bsc, events, orders, settings
from tests.harness import HOT, WORDS, cb, msg, plain
from tests.test_orders import M1, merchant
from tests.test_scenarios import ADMIN, BUYER, ready, user

STAFF = 30


def test_panel_is_short_with_sections(go):
    async def fn(b):
        await ready(b)
        await b.run(cb(ADMIN, "a"))
        buttons = b.session.buttons(ADMIN)
        assert {"au", "ad", "a:people", "a:money", "atl", "a:more"} <= set(buttons) and len(buttons) <= 10
        assert "Касса BEP-20" in plain(b.session.last(ADMIN))
        await b.run(cb(ADMIN, "a:money"))
        assert {"acash", "bal:p:0", "aadjl", "al", "afin", "acm"} <= set(b.session.buttons(ADMIN))
        await b.run(cb(ADMIN, "a:people"))
        assert {"asu", "aoml", "aopl", "atml", "aadm"} <= set(b.session.buttons(ADMIN))
        await b.run(cb(ADMIN, "a:more"))
        assert {"as", "ach", "aapi", "aa"} <= set(b.session.buttons(ADMIN))
    go(fn)


def test_desk_screen_and_the_seed_for_owners_only(go, monkeypatch):
    async def fn(b):
        monkeypatch.setattr(admin_bsc, "KEY_TTL", 0)
        await ready(b)
        b.chain.fund_hot(usdt="70", bnb="0.002")
        await b.run(cb(ADMIN, "acash"))
        text = plain(b.session.last(ADMIN))
        assert HOT in text and "70 USDT" in text and "мало, пополните" in text
        assert "acash:key" in b.session.buttons(ADMIN)
        await b.run(cb(ADMIN, "acash:key"))
        assert "Показать ключ" in str([x for x in b.session.buttons(ADMIN)]) or "acash:key:go" in b.session.buttons(ADMIN)
        assert WORDS not in plain(b.session.last(ADMIN))  # asked first, nothing shown yet
        await b.run(cb(ADMIN, "acash:key:go"))
        sent = [m for m in b.session.calls if type(m).__name__ == "SendMessage" and m.chat_id == ADMIN
                and WORDS in (m.text or "")]
        assert len(sent) == 1
        await asyncio.sleep(0.05)
        assert any(type(m).__name__ == "DeleteMessage" and m.chat_id == ADMIN for m in b.session.calls)
        async with models.Session() as s:  # the admin chat is told who opened the key, never the words
            posts = (await s.scalars(select(Event).where(Event.alert, Event.kind == "a:bsc_key"))).all()
            assert len(posts) == 1 and not any(WORDS in e.text for e in await s.scalars(select(Event)))

        await b.run(msg(STAFF, "/start"))
        async with models.Session() as s:
            await admins.grant(s, STAFF)
            await s.commit()
        await b.run(cb(STAFF, "acash"))
        assert "acash:key" not in b.session.buttons(STAFF)
        await b.run(cb(STAFF, "acash:key:go"))
        assert "Только владельцы" in b.session.alerts()[-1]
        assert not [m for m in b.session.calls if getattr(m, "chat_id", None) == STAFF and WORDS in (getattr(m, "text", None) or "")]
    go(fn)


def test_manual_rating_overrides_the_operators_and_shows_well(go):
    async def fn(b):
        await ready(b)
        await merchant(b, M1)
        async with models.Session() as s:
            assert await orders.reputation(s, M1) == (None, 0)
        await b.run(cb(ADMIN, f"aom:{M1}"))
        assert f"art:{M1}:om" in b.session.buttons(ADMIN)
        assert "считается по оценкам операторов" in plain(b.session.last(ADMIN))
        await b.run(cb(ADMIN, f"art:{M1}:om"), msg(ADMIN, "11"))
        assert "Нужно число от 1 до 10" in plain(b.session.last(ADMIN))
        await b.run(msg(ADMIN, "8,5"))
        card = plain(b.session.last(ADMIN))
        assert "Готово: рейтинг 8.5/10 вручную" in card and "★★★★☆ 8.5 из 10 · выставлен вручную" in card
        assert "★★★★☆ 8.5 из 10" in plain(b.session.last(M1))  # the merchant is told
        async with models.Session() as s:
            assert await orders.reputation(s, M1) == (D("8.5"), 0)
            assert (await s.get(User, M1)).rating == D("8.5")
        await b.run(cb(ADMIN, f"art:{M1}:u"), msg(ADMIN, "3"))  # from the user card too; low: no Bybit orders
        async with models.Session() as s:
            rep, _ = await orders.reputation(s, M1)
        assert rep == D(3) and "только с баланса" in orders.bybit_problem(rep, models.Deal(amount_rub=D(1000)))
        await b.run(cb(ADMIN, f"art:{M1}:u"), msg(ADMIN, "-"))
        async with models.Session() as s:
            assert await orders.reputation(s, M1) == (None, 0) and (await s.get(User, M1)).rating is None
        async with models.Session() as s:  # an important action: a post in the admin chat
            assert len((await s.scalars(select(Event).where(Event.alert, Event.kind == "a:rating"))).all()) == 3
    go(fn)


def test_admin_chat_gets_only_what_matters_by_default(go):
    async def fn(b):
        async with models.Session() as s:
            await settings.put(s, "log_all", settings.SPEC["log_all"][0])  # the default: quiet
            await s.commit()
        assert settings.get("log_all") == "0"
        await b.run(msg(777, "/start"))  # somebody new: no news
        async with models.Session() as s:
            assert not (await s.scalars(select(Event).where(Event.ref == "user:777"))).all()
            events.add(s, "dep:1", "credited", "Зачислено 10 USDT", BUYER, notice=True)  # routine: journal only
            audit.log(s, ADMIN, "report", "", "csv")  # a routine admin action: journal only
            audit.log(s, ADMIN, "ban", f"user:{BUYER}", "")  # an important one: a post
            await s.commit()
            alerts = (await s.scalars(select(Event).where(Event.alert))).all()
        assert [e.text.split("\x1f")[0] for e in alerts] == ["ban"]
    go(fn)


def test_ton_leftovers_go_back_to_the_balance_or_to_an_owner(go):
    async def fn(b):
        await ready(b)
        async with models.Session() as s:
            s.add_all([Withdrawal(user_id=BUYER, amount=D(25), fee=D(1), status="queued", method="ton", address="UQx"),
                       Withdrawal(user_id=BUYER, amount=D(7), fee=D(1), status="sent", method="ton", address="UQx")])
            await s.commit()
            refunded = await bsc.retire_legacy(s)
            assert [w.id for w in refunded] == [1]
            assert [w.status for w in (await s.scalars(select(Withdrawal).order_by(Withdrawal.id))).all()] == [
                "cancelled", "unknown"]
            assert await bsc.retire_legacy(s) == []  # once
        assert (await user(BUYER)).balance == D(25)
        assert any("неизвестным итогом: 1" in t for t in await b.deliver())
        await b.run(cb(ADMIN, "awv:2"))
        assert "wk:rf:2" in b.session.buttons(ADMIN)  # an owner decides on the card
    go(fn)
