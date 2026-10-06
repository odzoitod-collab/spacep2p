"""End-to-end run of the bot handlers against a fake Telegram API and a fake TON network."""
import asyncio
import itertools
import os
from datetime import datetime, timedelta
from decimal import Decimal as D

os.environ.setdefault("BOT_TOKEN", "123:abc")
os.environ["LOG_CHAT_ID"] = "1"

from aiogram import Bot  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.types import CallbackQuery, Chat, Document, Message, Update, Video  # noqa: E402
from aiogram.types import User as TgUser  # noqa: E402

from bot import models, tasks  # noqa: E402
from tests import harness  # noqa: E402
from tests.harness import make_dp  # noqa: E402
from bot.models import Deal, User, Withdrawal  # noqa: E402
from bot.services import settings, ton  # noqa: E402

ids = itertools.count(100)


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        name = type(method).__name__
        if name.startswith(("Send", "Edit")):
            return Message(message_id=next(ids), date=datetime.now(),
                           chat=Chat(id=getattr(method, "chat_id", 0) or 0, type="private"), text="x")
        return True

    async def stream_content(self, *a, **k):  # pragma: no cover
        yield b""

    async def close(self):
        pass


def tg(uid):
    return TgUser(id=uid, is_bot=False, first_name=f"U{uid}", username=f"u{uid}")


def msg(uid, text=None, **kw):
    return Update(update_id=next(ids), message=Message(
        message_id=next(ids), date=datetime.now(), chat=Chat(id=uid, type="private"),
        from_user=tg(uid), text=text, **kw))


def cb(uid, data):
    return Update(update_id=next(ids), callback_query=CallbackQuery(
        id=str(next(ids)), from_user=tg(uid), chat_instance="ci", data=data,
        message=Message(message_id=999, date=datetime.now(), chat=Chat(id=uid, type="private"), text="x")))


async def scenario():
    await models.init_db("sqlite+aiosqlite:///:memory:")
    async with models.Session() as s:
        await settings.put(s, "signup_review", "0")  # entry by application is covered by test_signup
        await s.commit()
        await settings.load(s)
    chain = harness.FakeChain()
    ton.chain = chain
    ton.POLL, ton.WAIT = 0, 0.01
    chain.fund_hot(usdt="100")
    session = FakeSession()
    bot = Bot("123:abc", session=session, default=DefaultBotProperties(parse_mode="HTML"))
    dp = make_dp()

    async def run(*updates):
        for u in updates:
            await dp.feed_update(bot, u)

    SELLER, BUYER, ADMIN = 10, 20, 1
    await run(msg(SELLER, "/start"), msg(BUYER, "/start"), msg(ADMIN, "/start"))
    async with models.Session() as s:
        (await s.get(User, SELLER)).balance = D(200)
        await s.commit()

    # seller adds a card and goes online
    await run(cb(SELLER, "sl"), cb(SELLER, "sl:on:1"), cb(SELLER, "sl:add"), cb(SELLER, "sl:add:card"),
              cb(SELLER, "sl:bank:0"), msg(SELLER, "4111 1111 1111 1111"), msg(SELLER, "Иванов Иван"),
              msg(SELLER, "1000"), msg(SELLER, "50000"), cb(SELLER, "sl:save"), cb(SELLER, "cd:1"), cb(SELLER, "ce:min:1"),
              msg(SELLER, "500"), cb(SELLER, "ce:max:1"), msg(SELLER, "60000"))

    pdf = Document(file_id="pdf1", file_unique_id="u1", mime_type="application/pdf")
    # deal 1: happy path
    await run(cb(BUYER, "mk"), cb(BUYER, "buy:0"), msg(BUYER, "10000"),
              cb(BUYER, "bgo:1:10000.00"), cb(BUYER, "dl:rc:1"), msg(BUYER, document=pdf),
              cb(SELLER, "dl:1"), cb(SELLER, "dl:pdf:1"), cb(SELLER, "dl:ok:1"), cb(SELLER, "dl:ok2:1"),
              cb(SELLER, "dl:ok2:1"), cb(BUYER, "deals"))
    async with models.Session() as s:
        seller_u, buyer_u = await s.get(User, SELLER), await s.get(User, BUYER)
        assert (seller_u.balance, seller_u.frozen, buyer_u.balance) == (D(105), D(0), D(94)), \
            (seller_u.balance, seller_u.frozen, buyer_u.balance)

    # deal 2: dispute "wrong amount", admin settles by actual amount
    video = Video(file_id="v1", file_unique_id="v1", width=1, height=1, duration=1)
    await run(cb(BUYER, "buy:0"), msg(BUYER, "5000"), cb(BUYER, "bgo:1:5000.00"),
              cb(BUYER, "dl:rc:2"), msg(BUYER, document=pdf),
              cb(SELLER, "dl:ds:2"), cb(SELLER, "dl:dr:2:wrong_amount"), msg(SELLER, "4000"), msg(SELLER, video=video),
              cb(ADMIN, "a"), cb(ADMIN, "adl:dispute"), cb(ADMIN, "adv:2"), cb(ADMIN, "af:2"), cb(ADMIN, "ar:2:a"), cb(ADMIN, "ar2:2:a"), cb(ADMIN, "ar2:2:a"))
    async with models.Session() as s:
        d = await s.get(Deal, 2)
        assert d.status == "completed" and d.amount_rub == D(4000), (d.status, d.amount_rub)
        seller_u, buyer_u = await s.get(User, SELLER), await s.get(User, BUYER)
        assert (seller_u.balance, seller_u.frozen, buyer_u.balance) == (D(67), D(0), D("131.6"))

    # deal 3: expires
    await run(cb(BUYER, "buy:0"), msg(BUYER, "1000"), cb(BUYER, "bgo:1:1000.00"))
    async with models.Session() as s:
        (await s.get(Deal, 3)).expires_at = models.now() - timedelta(minutes=1)
        await s.commit()
    await tasks.expire_deals(bot)
    async with models.Session() as s:
        d3 = await s.get(Deal, 3)
        assert d3.status == "expired" and d3.funds_held
        assert (await s.get(User, SELLER)).frozen == D("9.5")  # held for a possible late receipt
        d3.hold_until = models.now() - timedelta(seconds=1)
        await s.commit()
    await tasks.release_holds(bot)
    async with models.Session() as s:
        assert (await s.get(User, SELLER)).frozen == 0

    # wallet: withdraw to a TON address (double click: one withdrawal), deposit to the personal address
    await run(cb(BUYER, "w"), cb(BUYER, "w:out"), msg(BUYER, "UQBvW8Z5huBkMJYdnfAEM5JqTNkuWX3diqYENkWsIL0XglxD"),
              cb(BUYER, "w:nomemo"), msg(BUYER, "10"), cb(BUYER, "w:go"), cb(BUYER, "w:go"), cb(BUYER, "w:in"))
    chain.pay(BUYER, "100")
    await tasks.ton_cycle(bot)
    await run(cb(BUYER, "w:h"))
    assert [x[3] for x in chain.sent if x[:2] == ("gas", "USDT")] == [D("8.85")]  # 10 − 1.5% − 1 USDT
    async with models.Session() as s:
        assert (await s.get(Withdrawal, 1)).status == "done"
        assert (await s.get(User, BUYER)).balance == D("131.6") - 10 + D("98.5")  # 100 − 1.5% fee

    # admin panel
    await run(cb(ADMIN, "as"), cb(ADMIN, "as:rate"), msg(ADMIN, "95"))
    assert settings.get("rate") == "100"  # refused: the order rate 104 would not fit under 95 / (1 − 6%)
    await run(cb(ADMIN, "as:order_rate"), msg(ADMIN, "99"),
              cb(ADMIN, "as:rate"), msg(ADMIN, "95"), cb(ADMIN, "as:platform_pct"), msg(ADMIN, "1"),
              cb(ADMIN, "as:tutorial"), msg(ADMIN, "Новый <тутор>"),
              cb(ADMIN, "au"), msg(ADMIN, "@u10"), cb(ADMIN, "aum:10:+"), msg(ADMIN, "5"),
              cb(ADMIN, "amr:compensation"), cb(ADMIN, "adj:ok:1"), cb(ADMIN, "adj:ok:1"),
              cb(ADMIN, "auc:10"), cb(ADMIN, "ac:0"), cb(ADMIN, "acv:1"), cb(ADMIN, "aco:1"), cb(ADMIN, "acb:1:1"),
              cb(ADMIN, "aub:10:1"), cb(ADMIN, "aub2:10"), cb(ADMIN, "aub2:10"), cb(ADMIN, "al"), cb(BUYER, "info"), cb(BUYER, "a"))
    assert settings.get("rate") == "95" and settings.get("platform_pct") == "6"
    async with models.Session() as s:
        seller_u = await s.get(User, SELLER)
        assert seller_u.is_banned and seller_u.balance == D(72)
    await run(cb(SELLER, "menu"))  # banned user is gated
    await tasks.auto_offline(bot)
    return session


def test_full_flow():
    session = asyncio.run(scenario())
    assert len(session.calls) > 50


def test_unknown_withdrawal_manual_refund_once():
    async def scenario():
        await harness.reset_db("sqlite+aiosqlite:///:memory:")
        async with models.Session() as s:
            s.add_all([User(id=1), User(id=20, balance=D(90)),
                       Withdrawal(user_id=20, amount=D(10), fee=D(0), status="unknown", method="ton",
                                  address="UQBvW8Z5huBkMJYdnfAEM5JqTNkuWX3diqYENkWsIL0XglxD")])
            await s.commit()
        b = harness.Bench()
        await b.run(cb(1, "awv:1"))
        assert "wk:rf:1" in b.session.buttons(1)
        await b.run(cb(1, "wk:rf2:1"), cb(1, "wk:rf2:1"))  # second click: already processed
        async with models.Session() as s:
            assert (await s.get(User, 20)).balance == D(100)
            assert (await s.get(Withdrawal, 1)).status == "failed"
        await harness.models.engine.dispose()
    asyncio.run(scenario())
