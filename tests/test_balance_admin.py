"""/balance: every balance and full control — one user at a time (±, exact amount, zero) and everyone at once."""
import asyncio
from decimal import Decimal as D

from sqlalchemy import func, select

from bot import models
from bot.handlers import admin_balance
from bot.handlers.admin_balance import parse_change
from bot.models import Adjustment, Event, Ledger
from bot.services import money, settings
from tests.harness import cb, msg, plain
from tests.test_scenarios import ADMIN, BUYER, OTHER, SELLER, create_deal, ready, user


async def give(uid, amount):
    async with models.Session() as s:
        await money.add(s, uid, D(amount), "deposit", "dep:0")
        await s.commit()


async def drain():
    while admin_balance._notifying:
        await asyncio.gather(*list(admin_balance._notifying))


def test_parse_change():
    assert parse_change("+10") == ("+", D(10)) and parse_change("-5,5") == ("-", D("5.5"))
    assert parse_change("=20") == ("=", D(20)) and parse_change("20") == ("=", D(20))
    assert parse_change("0") == ("=", D(0)) and parse_change("=0") == ("=", D(0)) and parse_change("−3") == ("-", D(3))
    assert parse_change("+0") is None and parse_change("abc") is None and parse_change("") is None


def test_list_shows_holders_and_totals(go):
    async def fn(b):
        await ready(b)  # seller: 200 available
        await give(BUYER, 30)
        await b.run(msg(ADMIN, "/balance"))
        screen = plain(b.session.last(ADMIN))
        assert "Балансы" in screen and "Доступно у всех: 230 USDT" in screen and "С деньгами: 2 из" in screen
        buttons = b.session.buttons(ADMIN)
        assert buttons.index(f"bal:u:{SELLER}") < buttons.index(f"bal:u:{BUYER}")  # the biggest first
        for data in ("bal:m:plus", "bal:m:all", "bal:m:minus", "bal:m:zero"):
            assert data in buttons, data
        await b.run(msg(ADMIN, f"/balance @u{BUYER}"))
        assert f"Баланс · {BUYER}" in plain(b.session.last(ADMIN)) and "Доступно: 30 USDT" in plain(b.session.last(ADMIN))
        await b.run(msg(BUYER, "/balance"))  # not an admin: no balance screen for him
        assert "Доступно у всех" not in plain(b.session.last(BUYER))
    go(fn)


def test_one_user_quick_exact_and_zero(go):
    async def fn(b):
        await ready(b)
        await create_deal(b)  # seller: 105 available, 95 frozen
        await b.run(cb(ADMIN, f"bal:q:{SELLER}:10"), cb(ADMIN, f"bal:q:{SELLER}:-1"))
        assert (await user(SELLER)).balance == D(114)
        assert "Теперь доступно: <b>114 USDT</b>" in b.session.last(SELLER)
        await b.run(cb(ADMIN, f"bal:c:{SELLER}"), msg(ADMIN, "сто"))
        assert "Не понял сумму" in plain(b.session.last(ADMIN))
        await b.run(msg(ADMIN, "=50"))
        assert (await user(SELLER)).balance == D(50)
        await b.run(cb(ADMIN, f"bal:c:{SELLER}"), msg(ADMIN, "-500"))
        assert "Не хватает доступного баланса" in plain(b.session.last(ADMIN))
        await b.run(cb(ADMIN, f"bal:z:{SELLER}"), cb(ADMIN, f"bal:zz:{SELLER}"))
        s_ = await user(SELLER)
        assert (s_.balance, s_.frozen) == (D(0), D(95))  # frozen money is never touched
        async with models.Session() as s:
            adj = (await s.scalars(select(Adjustment).order_by(Adjustment.id))).all()
            assert [(a.status, a.reason, a.delta) for a in adj] == [
                ("done", "manual", D(10)), ("done", "manual", D(-1)), ("done", "manual", D(-64)),
                ("failed", "manual", D(-500)), ("done", "manual", D(-50))]
            assert await s.scalar(select(func.count(Ledger.id)).where(Ledger.kind == "admin")) == 4
    go(fn)


def test_own_balance_waits_for_second_admin(go):
    async def fn(b):
        await ready(b)
        await b.run(cb(ADMIN, f"bal:q:{ADMIN}:50"))
        assert (await user(ADMIN)).balance == 0
        assert "Ждёт подтверждения второго администратора" in plain(b.session.last(ADMIN))
    go(fn)


def test_mass_plus_minus_zero_with_one_summary(go):
    async def fn(b):
        await ready(b)
        await give(BUYER, 3)
        await b.run(msg(OTHER, "/start"))  # in the bot, no money
        await b.run(cb(ADMIN, "bal:m:plus"), msg(ADMIN, "5"))
        assert "Затронет: 2 чел." in plain(b.session.last(ADMIN)) and "Итого: +10 USDT" in plain(b.session.last(ADMIN))
        go_btn = next(x for x in b.session.buttons(ADMIN) if x.startswith("bal:x:"))
        await b.run(cb(ADMIN, go_btn), cb(ADMIN, go_btn))  # a double tap credits once
        assert [(await user(u)).balance for u in (SELLER, BUYER, OTHER)] == [D(205), D(8), D(0)]
        assert "Уже выполнено или устарело" in b.session.alerts()[-1]
        await drain()
        assert "Теперь доступно: <b>8 USDT</b>" in b.session.last(BUYER)

        await b.run(cb(ADMIN, "bal:m:minus"), msg(ADMIN, "10"))
        await b.run(cb(ADMIN, next(x for x in b.session.buttons(ADMIN) if x.startswith("bal:x:"))))
        assert [(await user(u)).balance for u in (SELLER, BUYER)] == [D(195), D(0)]  # never below zero

        await b.run(cb(ADMIN, "bal:m:zero"))
        await b.run(cb(ADMIN, next(x for x in b.session.buttons(ADMIN) if x.startswith("bal:x:"))))
        assert (await user(SELLER)).balance == 0
        async with models.Session() as s:
            mass = (await s.scalars(select(Event).where(Event.ref == f"adm:{ADMIN}", Event.alert))).all()
            assert [e.text.split("\x1f")[0] for e in mass] == ["balance_mass"] * 3  # one post per action
            assert await s.scalar(select(func.count(Event.id)).where(Event.ref.like("adj:%"), Event.alert)) == 0
    go(fn)


def test_mass_all_reaches_everyone_but_banned(go):
    async def fn(b):
        await ready(b)
        await b.run(msg(OTHER, "/start"))
        async with models.Session() as s:
            (await s.get(models.User, OTHER)).is_banned = True
            await s.commit()
        await b.run(cb(ADMIN, "bal:m:all"), msg(ADMIN, "2"))
        await b.run(cb(ADMIN, next(x for x in b.session.buttons(ADMIN) if x.startswith("bal:x:"))))
        assert [(await user(u)).balance for u in (SELLER, BUYER, OTHER)] == [D(202), D(2), D(0)]
        assert (await user(ADMIN)).balance == 0  # own balance: a second admin first
        assert "ждут второго администратора 1" in plain(b.session.last(ADMIN))
    go(fn)


def test_threshold_applies_to_mass_actions(go):
    async def fn(b):
        await ready(b)
        async with models.Session() as s:
            await settings.put(s, "adjust_approval_usdt", "100")
            await s.commit()
        await b.run(cb(ADMIN, "bal:m:zero"))
        await b.run(cb(ADMIN, next(x for x in b.session.buttons(ADMIN) if x.startswith("bal:x:"))))
        assert (await user(SELLER)).balance == D(200)  # 200 ≥ 100: waits for the second admin
        async with models.Session() as s:
            assert (await s.scalar(select(Adjustment))).status == "pending"
    go(fn)


def test_settings_screen_escapes_labels(go):
    """«без Bybit <» / «лимит <» went into the HTML raw: Telegram refused the whole settings screen, and the
    fallback switched the banner and the custom emoji off for everyone (05.10)."""
    async def fn(b):
        await ready(b)
        await b.run(cb(ADMIN, "as"))
        raw = b.session.last(ADMIN)
        assert "без Bybit &lt;" in raw and "лимит &lt;" in raw and "без Bybit <" not in raw
    go(fn)
