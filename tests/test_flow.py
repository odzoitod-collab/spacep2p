"""End-to-end run of the bot handlers against a fake Telegram API and fake xRocket."""
import asyncio
import itertools
import os
from datetime import datetime, timedelta
from decimal import Decimal as D
from sqlalchemy import select

os.environ.setdefault("BOT_TOKEN", "123:abc")
os.environ["LOG_CHAT_ID"] = "1"

from aiogram import Bot  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.types import CallbackQuery, Chat, Document, Message, Update, Video  # noqa: E402
from aiogram.types import User as TgUser  # noqa: E402

from bot import models, tasks  # noqa: E402
from bot.handlers import wallet  # noqa: E402
from tests import harness  # noqa: E402
from tests.harness import make_dp  # noqa: E402
from bot.models import Deal, Deposit, Ledger, User, Withdrawal  # noqa: E402
from bot.services import settings, xrocket  # noqa: E402

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


class FakeRocket:
    def __init__(self):
        self.cheques = []

    async def create_invoice(self, amount, client_id, description):
        return {"id": "inv1", "links": {"telegramBotLink": "https://t.me/xRocket?start=inv1"}}

    async def get_invoice(self, invoice_id):
        return {"id": invoice_id, "status": "paid"}

    async def get_invoice_payments(self, invoice_id):
        return [{"id": "payment1", "status": "paid", "receiveAmount": "98.5", "receiveCurrency": "USDT"}]

    async def create_cheque(self, amount, client_id, tg_id, description):
        self.cheques.append((amount, client_id, tg_id))
        return {"chequeId": "c1", "links": {"telegramBotLink": "https://t.me/xRocket?start=c1"}}

    async def balances(self):
        return [{"asset": "USDT", "available": "1000"}]


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
        await settings.load(s)
    rocket = FakeRocket()
    xrocket.rocket = rocket
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
    await run(cb(BUYER, "mk"), cb(BUYER, "buy:0"), cb(BUYER, "flt"), cb(BUYER, "flt:kind"), cb(BUYER, "flt:bank"),
              cb(BUYER, "flt:b:0"), cb(BUYER, "flt:amt"), msg(BUYER, "10000"),
              cb(BUYER, "bc:1"), cb(BUYER, "bgo:1:10000.00"), cb(BUYER, "dl:rc:1"), msg(BUYER, document=pdf),
              cb(SELLER, "dl:1"), cb(SELLER, "dl:pdf:1"), cb(SELLER, "dl:ok:1"), cb(SELLER, "dl:ok2:1"),
              cb(SELLER, "dl:ok2:1"), cb(BUYER, "deals"))
    async with models.Session() as s:
        seller_u, buyer_u = await s.get(User, SELLER), await s.get(User, BUYER)
        assert (seller_u.balance, seller_u.frozen, buyer_u.balance) == (D(105), D(0), D(94)), \
            (seller_u.balance, seller_u.frozen, buyer_u.balance)

    # deal 2: dispute "wrong amount", admin settles by actual amount
    video = Video(file_id="v1", file_unique_id="v1", width=1, height=1, duration=1)
    await run(cb(BUYER, "flt:reset"), cb(BUYER, "bc:1"), msg(BUYER, "5000"), cb(BUYER, "bgo:1:5000.00"),
              cb(BUYER, "dl:rc:2"), msg(BUYER, document=pdf),
              cb(SELLER, "dl:ds:2"), cb(SELLER, "dl:dr:2:wrong_amount"), msg(SELLER, "4000"), msg(SELLER, video=video),
              cb(ADMIN, "a"), cb(ADMIN, "adl:dispute"), cb(ADMIN, "adv:2"), cb(ADMIN, "af:2"), cb(ADMIN, "ar:2:a"), cb(ADMIN, "ar2:2:a"), cb(ADMIN, "ar2:2:a"))
    async with models.Session() as s:
        d = await s.get(Deal, 2)
        assert d.status == "completed" and d.amount_rub == D(4000), (d.status, d.amount_rub)
        seller_u, buyer_u = await s.get(User, SELLER), await s.get(User, BUYER)
        assert (seller_u.balance, seller_u.frozen, buyer_u.balance) == (D(67), D(0), D("131.6"))

    # deal 3: expires
    await run(cb(BUYER, "bc:1"), msg(BUYER, "1000"), cb(BUYER, "bgo:1:1000.00"))
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

    # wallet: withdraw by cheque, deposit via polling
    await run(cb(BUYER, "w"), cb(BUYER, "w:wd"), msg(BUYER, "10"), cb(BUYER, "w:go"), cb(BUYER, "w:go"),
              cb(BUYER, "w:dep"), msg(BUYER, "100"), cb(BUYER, "w:h"))
    assert rocket.cheques and rocket.cheques[0][2] == BUYER
    await tasks.poll_deposits(bot)
    async with models.Session() as s:
        assert (await s.get(User, BUYER)).balance == D("131.6") - 10 + D("98.5")

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


def test_deposit_credits_actual_net_payment_once():
    async def scenario():
        await models.init_db("sqlite+aiosqlite:///:memory:")
        async with models.Session() as s:
            s.add(User(id=777))
            s.add(Deposit(user_id=777, invoice_id="inv-partial", amount=D(100),
                          credit=D("98.5"), status="active"))
            await s.commit()

        class PartialRocket(FakeRocket):
            async def get_invoice(self, invoice_id):
                return {"id": invoice_id, "status": "expired"}

            async def get_invoice_payments(self, invoice_id):
                return [{"status": "paid", "receiveAmount": "47.3", "receiveCurrency": "USDT"}]

        xrocket.rocket = PartialRocket()
        async with models.Session() as s:
            dep = await s.get(Deposit, 1)
            assert await wallet.check_deposit(s, dep) == "credited"
            assert await wallet.check_deposit(s, dep) == "paid"
            assert (await s.get(User, 777)).balance == D("47.3")
            assert dep.credit == D("47.3")
    asyncio.run(scenario())


def test_cancelled_cheque_refunds_once_and_reverses_fee():
    async def scenario():
        await harness.reset_db("sqlite+aiosqlite:///:memory:")
        async with models.Session() as s:
            s.add_all([User(id=1), User(id=20, balance=D(90)),
                       Withdrawal(user_id=20, amount=D(10), fee=D(2), status="done",
                                  cheque_id="ch1", link="https://t.me/x"),
                       Ledger(user_id=None, delta=D(2), kind="withdraw_fee", ref="wd:1")])
            await s.commit()
        b = harness.Bench()
        b.rocket.lookup = {"chequeId": "ch1", "deleted": True}
        await b.run(cb(1, "wr:1"), cb(1, "wr:1"))  # second click: already processed
        async with models.Session() as s:
            assert (await s.get(User, 20)).balance == D(100)
            assert (await s.get(Withdrawal, 1)).status == "failed"
            rows = (await s.scalars(select(Ledger).where(Ledger.user_id.is_(None)))).all()
            assert sum((row.delta for row in rows), D(0)) == 0
        assert "возвращена на баланс" in harness.plain(b.session.texts(1)[-2])
    asyncio.run(scenario())


def test_unknown_withdrawal_manual_refund_once():
    async def scenario():
        await harness.reset_db("sqlite+aiosqlite:///:memory:")
        async with models.Session() as s:
            s.add_all([User(id=1), User(id=20, balance=D(90)),
                       Withdrawal(user_id=20, amount=D(10), fee=D(0), status="unknown")])
            await s.commit()

        class MissingRocket(harness.FakeRocket):
            deleted = False

            async def get_cheque_by_client(self, client_id):
                if self.deleted:
                    return {"chequeId": "check", "deleted": True}
                raise xrocket.XRocketError("app_cheque_not_found", status=404)

            async def create_cheque(self, amount, client_id, tg_id, description):
                return {"chequeId": "check", "deleted": False}

            async def delete_cheque_by_client(self, client_id):
                self.deleted = True

        b = harness.Bench()
        xrocket.rocket = MissingRocket()
        await b.run(cb(1, "wr:1"))
        assert "wr:rf:1" in b.session.buttons(1)  # refund offered only after xRocket says "not found"
        await b.run(cb(1, "wr:rf:1"), cb(1, "wr:rf:1"))
        async with models.Session() as s:
            assert (await s.get(User, 20)).balance == D(100)
            assert (await s.get(Withdrawal, 1)).status == "failed"
    asyncio.run(scenario())
