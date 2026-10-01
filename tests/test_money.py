import asyncio
from decimal import Decimal as D

import pytest
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from bot import models
from bot.models import Card, Deal, User, Withdrawal
from bot.services import deals, money, settings


def test_quote_example():
    q = money.quote(D(10000), D(100), D(5), D(6))
    assert (q.usdt, q.seller_debit, q.buyer_credit, q.platform_fee) == (D(100), D(95), D(94), D(1))


def test_quote_rounding_never_leaks():
    q = money.quote(D("777.77"), D("93.13"), D("4.7"), D("6.3"))
    assert q.seller_debit >= q.buyer_credit + q.platform_fee - D("0.000001")
    assert q.platform_fee == q.seller_debit - q.buyer_credit


def test_max_rub_fits_balance():
    rub = money.max_rub(D(95), D(100), D(5))
    assert rub == D(10000)
    assert money.seller_debit(rub, D(100), D(5)) <= D(95)
    odd = money.max_rub(D("12.345678"), D("97.3"), D("4.4"))
    assert money.seller_debit(odd, D("97.3"), D("4.4")) <= D("12.345678")


def test_quote_refuses_negative_platform_fee():
    """seller_pct > platform_pct would make the platform pay every deal out of its own pocket."""
    with pytest.raises(ValueError):
        money.quote(D(10000), D(100), D(7), D(6))


def test_fmt():
    assert money.fmt(D("10000.00")) == "10 000"
    assert money.fmt(D("94.5")) == "94.5"


def test_settings_validate():
    assert settings.validate("rate", "100") == "100"
    assert settings.validate("rate", "98,50") == "98.5"
    with pytest.raises(ValueError, match="101.59"):
        settings.validate("rate", "95,50")  # the order rate 104 would cost the platform money at 95.5 and 6%
    assert settings.validate("order_rate", "101,5") == "101.5"
    with pytest.raises(ValueError, match="106.38"):
        settings.validate("order_rate", "107")
    with pytest.raises(ValueError):
        settings.validate("platform_pct", "3")  # below seller 5


def run(coro):
    async def main():
        try:
            return await coro
        finally:  # close SQLite connections on this loop, not from a worker thread after it is gone
            if models.engine is not None:
                await models.engine.dispose()
    return asyncio.run(main())


async def _setup():
    await models.init_db("sqlite+aiosqlite:///:memory:")
    async with models.Session() as s:
        await settings.load(s)
        s.add_all([User(id=1, balance=D(200), is_online=True), User(id=2)])
        s.add(Card(user_id=1, kind="card", bank="Сбер", requisites="2200", holder="Ivan",
                   min_rub=D(1000), max_rub=D(50000), is_active=True))
        await s.commit()


async def _flow(resolve):
    await _setup()
    async with models.Session() as s:
        buyer = await s.get(User, 2)
        found = await deals.market(s, 2, D(10000), None, None)
        assert len(found) == 1 and found[0][3] == D("21052.63")  # capped by seller balance
        d = await deals.create(s, buyer, 1, D(10000))
        await s.commit()
        seller = await s.get(User, 1, populate_existing=True)
        assert (seller.balance, seller.frozen) == (D(105), D(95))
        assert await deals.market(s, 2, None, None, None) == []  # card busy
        with pytest.raises(deals.DealError):
            await deals.create(s, buyer, 1, D(5000))
        assert await deals.mark_paid(s, d.id, 2, "file")
        await resolve(s, d.id)
        await s.commit()
        return (await s.get(User, 1, populate_existing=True), await s.get(User, 2, populate_existing=True))


def test_complete_once():
    async def resolve(s, deal_id):
        assert await deals.complete(s, deal_id)
        assert await deals.complete(s, deal_id) is None  # double click is a no-op
    seller, buyer = run(_flow(resolve))
    assert (seller.balance, seller.frozen, buyer.balance) == (D(105), D(0), D(94))


def test_cancel_unfreezes():
    async def resolve(s, deal_id):
        assert await deals.cancel(s, deal_id, ("paid",))
    seller, buyer = run(_flow(resolve))
    assert (seller.balance, seller.frozen, buyer.balance) == (D(200), D(0), D(0))


def test_dispute_actual_amount():
    async def resolve(s, deal_id):
        assert await deals.open_dispute(s, deal_id, 1, "wrong_amount", [], D(5000))
        assert await deals.complete(s, deal_id, actual_rub=D(5000))
    seller, buyer = run(_flow(resolve))
    assert (seller.balance, seller.frozen, buyer.balance) == (D("152.5"), D(0), D(47))


def test_expired_receipt_cannot_resurrect_deal():
    async def scenario():
        await _setup()
        async with models.Session() as s:
            d = await deals.create(s, await s.get(User, 2), 1, D(10000))
            d.expires_at = models.now()
            await s.commit()
            assert await deals.mark_paid(s, d.id, 2, "late.pdf") is None
            assert (await s.get(Deal, d.id, populate_existing=True)).status == "waiting_payment"
    run(scenario())


def test_balance_lock_refreshes_stale_identity():
    async def scenario():
        await _setup()
        async with models.Session() as s:
            user = await s.get(User, 1)
            await s.execute(update(User).where(User.id == 1).values(balance=D(150))
                            .execution_options(synchronize_session=False))
            assert user.balance == D(200)
            await money.add(s, 1, D(10), "admin")
            await s.commit()
            assert (await s.get(User, 1, populate_existing=True)).balance == D(160)
    run(scenario())


def test_withdrawal_request_id_unique():
    async def scenario():
        await _setup()
        async with models.Session() as s:
            s.add(Withdrawal(user_id=1, request_id="same", amount=D(10), fee=D(0)))
            await s.commit()
        async with models.Session() as s:
            await money.add(s, 1, D(-10), "withdraw")
            s.add(Withdrawal(user_id=1, request_id="same", amount=D(10), fee=D(0)))
            with pytest.raises(IntegrityError):
                await s.commit()
            await s.rollback()
            assert (await s.get(User, 1)).balance == D(200)
    run(scenario())


def test_dispute_larger_amount_reprices_without_minting():
    async def resolve(s, deal_id):
        assert await deals.open_dispute(s, deal_id, 1, "wrong_amount", [], D(15000))
        assert await deals.complete(s, deal_id, actual_rub=D(15000))

    seller, buyer = run(_flow(resolve))
    assert (seller.balance, seller.frozen, buyer.balance) == (D("57.5"), D(0), D(141))
    assert seller.balance + seller.frozen + buyer.balance + D("1.5") == D(200)


def test_dispute_larger_amount_rejects_insufficient_seller_funds():
    async def scenario():
        await _setup()
        async with models.Session() as s:
            d = await deals.create(s, await s.get(User, 2), 1, D(10000))
            await deals.mark_paid(s, d.id, 2, "receipt")
            await deals.open_dispute(s, d.id, 1, "wrong_amount", [], D(50000))
            with pytest.raises(deals.DealError):
                await deals.complete(s, d.id, actual_rub=D(50000))
            assert (await s.get(Deal, d.id, populate_existing=True)).status == "dispute"
            assert (await s.get(User, 1, populate_existing=True)).frozen == D(95)
    run(scenario())
