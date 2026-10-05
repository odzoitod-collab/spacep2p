"""xRocket in the admin panel: status, the payout queue (positions for users, «send now» for admins), the token
changed by an owner and checked before it is used."""
from decimal import Decimal as D

from sqlalchemy import select

from bot import models
from bot.models import Withdrawal
from bot.services import admins, settings, xrocket
from tests.harness import cb, msg, plain
from tests.test_scenarios import ADMIN, BUYER, SELLER, ready

OWNER2 = 2


async def queued(*rows):
    async with models.Session() as s:
        for uid, amount in rows:
            s.add(Withdrawal(user_id=uid, amount=D(amount), fee=D(1), status="queued"))
        await s.commit()


def test_queue_positions_and_send_now(go):
    async def fn(b):
        await ready(b)
        await queued((SELLER, 51), (BUYER, 21))
        await b.run(cb(BUYER, "w"))
        screen = plain(b.session.last(BUYER))
        assert "Очередь на вывод" in screen and "#2: 20 USDT · место 2, перед вами 50 USDT" in screen
        assert "w:qc:2" in b.session.buttons(BUYER)
        await b.run(cb(ADMIN, "axr"))
        screen = plain(b.session.last(ADMIN))
        assert "Статус: подключён · на балансе приложения 1 000 USDT" in screen
        assert "Очередь выводов: 2 на 70 USDT" in screen and "1. #1 · 50 USDT · чек" in screen
        await b.run(cb(ADMIN, "axr:go"))
        assert "Отправлено из очереди: 2, осталось 0" in plain(b.session.last(ADMIN))
        async with models.Session() as s:
            assert {w.status for w in (await s.scalars(select(Withdrawal))).all()} == {"done"}
    go(fn)


def test_owner_changes_the_token_and_it_is_checked_first(go, monkeypatch):
    switched = []

    async def check(tok, base):
        if tok.startswith("bad"):
            raise xrocket.XRocketError("unauthorized", "no", 401)
        return D(321)

    async def switch(tok, base):
        switched.append(tok)

    monkeypatch.setattr(xrocket, "check_token", check)
    monkeypatch.setattr(xrocket, "switch", switch)

    async def fn(b):
        await ready(b)
        await b.run(msg(OWNER2, "/start"))
        async with models.Session() as s:
            await admins.grant(s, BUYER)
            await s.commit()
        await b.run(cb(BUYER, "axr"))
        assert "axr:tok" not in b.session.buttons(BUYER)  # an admin, not an owner
        await b.run(cb(BUYER, "axr:tok"))
        assert "только владельцы" in b.session.alerts()[-1]

        await b.run(cb(ADMIN, "axr"), cb(ADMIN, "axr:tok"))
        bad = msg(ADMIN, "bad" + "x" * 40)
        await b.run(bad)
        assert "xRocket не принял токен: ошибка авторизации API" in plain(b.session.last(ADMIN)) and not switched
        good_token = "good" + "y" * 40
        good = msg(ADMIN, good_token)
        await b.run(good)
        deleted = {m.message_id for m in b.session.calls if type(m).__name__ == "DeleteMessage" and m.chat_id == ADMIN}
        assert {bad.message.message_id, good.message.message_id} <= deleted  # the secret never stays in the chat
        assert switched == [good_token] and settings.raw(xrocket.TOKEN_KEY) == good_token
        screen = plain(b.session.last(ADMIN))
        assert "Токен принят: на балансе приложения 321 USDT" in screen and good_token not in screen
        assert "…yyyy · из админ-панели" in screen
        assert all(good_token not in t for t in b.session.texts())  # nowhere in what the bot sent
    go(fn)
