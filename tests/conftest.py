import asyncio

import pytest

from tests import harness


@pytest.fixture(params=["sqlite", "postgres"])
def db_url(request):
    if request.param == "postgres":
        if not harness.PG:
            pytest.skip("P2P_TEST_PG is not set: PostgreSQL scenario not run")
        return harness.PG
    return "sqlite+aiosqlite:///:memory:"


@pytest.fixture
def go(db_url):
    """go(fn): run `async fn(bench)` on a fresh database, always disposing the engine."""
    def runner(fn):
        async def main():
            await harness.reset_db(db_url)
            try:
                bench = harness.Bench()
                await fn(bench)
                harness.check_telegram_limits(bench.session)
            finally:
                await harness.models.engine.dispose()
        asyncio.run(main())
    return runner
