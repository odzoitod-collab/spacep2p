"""PostgreSQL-only guarantees: row locks under real concurrency, migrations of an old database,
single bot process. Skipped unless P2P_TEST_PG points to a disposable database."""
import asyncio
from decimal import Decimal as D
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from bot import models
from bot.models import Card, Deal, Ledger, User
from bot.services import deals, money, settings
from tests import harness

pytestmark = pytest.mark.skipif(not harness.PG, reason="P2P_TEST_PG is not set: PostgreSQL checks not run")


def pg(fn):
    async def main():
        await harness.reset_db(harness.PG)
        try:
            await fn()
        finally:
            await models.engine.dispose()
    asyncio.run(main())


async def seed(seller_balance=D(200), buyers=(2,)):
    async with models.Session() as s:
        s.add_all([User(id=1, balance=seller_balance, is_online=True)] + [User(id=b) for b in buyers])
        await s.flush()
        s.add(Card(user_id=1, kind="card", bank="Сбер", requisites="4111111111111111", holder="Иван Иванов",
                   min_rub=D(1000), max_rub=D(50000), is_active=True))
        await s.commit()


async def paid_deal():
    async with models.Session() as s:
        d = await deals.create(s, await s.get(User, 2), 1, D(10000))
        await deals.mark_paid(s, d.id, 2, "pdf")
        await s.commit()
        return d.id


async def attempt(fn):
    """Run fn(session) in its own transaction; returns fn's result or the exception."""
    async with models.Session() as s:
        try:
            res = await fn(s)
            await s.commit()
            return res
        except Exception as e:  # noqa: BLE001
            await s.rollback()
            return e


def test_parallel_confirm_and_admin_cancel_settle_once():
    async def fn():
        await seed()
        did = await paid_deal()
        results = await asyncio.gather(
            attempt(lambda s: deals.complete(s, did, frm=("paid",))),
            attempt(lambda s: deals.complete(s, did)),
            attempt(lambda s: deals.cancel(s, did, ("paid", "dispute"))),
        )
        assert sum(isinstance(r, Deal) for r in results) == 1, results
        async with models.Session() as s:
            seller, buyer = await s.get(User, 1), await s.get(User, 2)
            platform = await s.scalar(select(func.coalesce(func.sum(Ledger.delta), 0)).where(Ledger.user_id.is_(None)))
            assert seller.frozen == 0
            assert seller.balance + buyer.balance + platform == D(200)  # nothing minted or lost
    pg(fn)


def test_parallel_deals_on_one_card_only_one_wins():
    async def fn():
        await seed(buyers=(2, 3, 4))
        results = await asyncio.gather(*[
            attempt(lambda s, b=b: _create(s, b)) for b in (2, 3, 4)])
        assert sum(isinstance(r, Deal) for r in results) == 1, results
        async with models.Session() as s:
            assert (await s.get(User, 1)).frozen == D(95)
    pg(fn)


async def _create(s, buyer_id):
    return await deals.create(s, await s.get(User, buyer_id), 1, D(10000))


def test_parallel_debits_never_overdraw():
    async def fn():
        async with models.Session() as s:
            s.add(User(id=5, balance=D(100)))
            await s.commit()
        results = await asyncio.gather(*[attempt(lambda s: money.add(s, 5, D(-30), "withdraw")) for _ in range(10)])
        assert sum(isinstance(r, User) for r in results) == 3
        async with models.Session() as s:
            assert (await s.get(User, 5)).balance == D(10)
            assert await s.scalar(select(func.count(Ledger.id))) == 3
    pg(fn)


def test_migrates_first_release_database():
    async def fn():
        eng = create_async_engine(harness.PG)
        async with eng.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
            sql = "\n".join(line for line in Path(__file__).with_name("schema_v0.sql").read_text().splitlines()
                            if not line.startswith("--"))
            for stmt in [x for x in sql.split(";") if x.strip()]:
                await conn.execute(text(stmt))
            await conn.execute(text(
                "INSERT INTO users VALUES (1,NULL,'s',200,95,false,true,now(),NULL,now()),"
                "(2,NULL,'b',0,0,false,false,now(),NULL,now())"))
            await conn.execute(text(
                "INSERT INTO cards VALUES (1,1,'card','Сбер','4111111111111111','Иван Иванов',1000,50000,"
                "true,false,false,now())"))
            await conn.execute(text(
                "INSERT INTO deals VALUES (1,2,1,1,10000,100,5,6,95,94,1,'paid','pdf',now(),NULL,NULL,'[]',"
                "NULL,now() - interval '2 hours',NULL)"))
            await conn.execute(text("INSERT INTO withdrawals (user_id, amount, fee, status, created_at) "
                                    "VALUES (2, 1, 0, 'done', now())"))
            # old journal: frozen moves were not recorded, a completed sale was
            await conn.execute(text("INSERT INTO ledger (user_id, delta, kind, ref, created_at) VALUES "
                                    "(1, 390, 'deposit', 'dep:1', now()), (1, -95, 'deal_sell', 'deal:0', now())"))
            await conn.execute(text(
                "INSERT INTO deals VALUES (2,2,1,1,10000,100,5,6,95,94,1,'cancelled',NULL,now(),NULL,NULL,'[]',"
                "NULL,now(),now())"))
        await eng.dispose()

        await models.engine.dispose()
        await models.init_db(harness.PG)
        await models.init_db(harness.PG)  # second start is a no-op
        async with models.Session() as s:
            await settings.load(s)
            versions = (await s.execute(text("SELECT version FROM schema_version ORDER BY version"))).scalars().all()
            assert versions == [v for v, _ in models.MIGRATIONS]
            d = await s.get(Deal, 1)
            assert d.paid_at is not None and d.reminded is False  # backfilled for the escalation timer
            assert await s.scalar(text("SELECT count(*) FROM audit")) == 0
            idx = await s.scalar(text("SELECT count(*) FROM pg_indexes WHERE indexname='ix_withdrawals_request_id'"))
            assert idx == 1
            assert (await s.get(Deal, 2)).close_reason == "buyer_cancel"
            total, frozen = (await s.execute(text(
                "SELECT sum(delta), sum(frozen_delta) FROM ledger WHERE user_id = 1"))).one()
            assert (total, frozen) == (D(295), D(95))  # users.balance + frozen = 200 + 95
            # the migrated database is fully usable: settle the old deal
            assert await deals.complete(s, 1)
            await s.commit()
            assert (await s.get(User, 2, populate_existing=True)).balance == D(94)
    pg(fn)


def test_second_bot_process_is_refused():
    async def fn():
        a, b = create_async_engine(harness.PG), create_async_engine(harness.PG)
        async with a.connect() as ca, b.connect() as cb:
            q = text("SELECT pg_try_advisory_lock(:k)")
            assert await ca.scalar(q, {"k": models.INSTANCE_LOCK}) is True
            assert await cb.scalar(q, {"k": models.INSTANCE_LOCK}) is False
        await a.dispose()
        await b.dispose()
    pg(fn)


def test_migrated_schema_equals_fresh_schema():
    """Explicit ALTER/CREATE migrations must produce exactly the tables, columns, indexes and checks of the models."""
    query = text("SELECT table_name, column_name, data_type, is_nullable, numeric_precision, numeric_scale, "
                 "character_maximum_length FROM information_schema.columns WHERE table_schema = 'public' "
                 "ORDER BY table_name, column_name")

    extra = [text("SELECT 'index', tablename, indexname FROM pg_indexes WHERE schemaname = 'public'"),
             text("SELECT 'check', conrelid::regclass::text, conname FROM pg_constraint "
                  "WHERE contype = 'c' AND connamespace = 'public'::regnamespace")]

    async def columns():  # columns, index names and check constraints
        async with models.engine.connect() as conn:
            out = set((await conn.execute(query)).all())
            for q in extra:
                out |= set((await conn.execute(q)).all())
            return out

    async def fn():
        fresh = await columns()
        eng = create_async_engine(harness.PG)
        async with eng.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
            sql = "\n".join(line for line in Path(__file__).with_name("schema_v0.sql").read_text().splitlines()
                            if not line.startswith("--"))
            for stmt in [x for x in sql.split(";") if x.strip()]:
                await conn.execute(text(stmt))
        await eng.dispose()
        await models.engine.dispose()
        await models.init_db(harness.PG)
        migrated = await columns()
        assert fresh == migrated, fresh ^ migrated
    pg(fn)
