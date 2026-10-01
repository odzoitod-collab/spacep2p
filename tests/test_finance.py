"""Finance snapshot: assets vs users' money, profit, what can be taken out; the live stats message."""
from decimal import Decimal as D

from bot import models, tasks
from bot.config import config
from bot.services import finance
from tests.harness import cb, msg, plain
from tests.test_scenarios import ADMIN, BUYER, PDF, SELLER, create_deal, ready


def test_numbers_add_up_and_stats_message_is_edited(go, monkeypatch):
    async def fn(b):
        await ready(b)  # the seller holds 200 USDT
        d = await create_deal(b)
        await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, document=PDF), cb(SELLER, f"dl:ok2:{d.id}"))
        async with models.Session() as s:
            sn = await finance.snapshot(s)
        assert sn.xrocket == D(1000)
        assert (sn.users_available, sn.users_frozen) == (D(105) + D(94), D(0))  # seller 105, buyer 94
        assert sn.profit["all"] == D(1) and sn.profit["24h"] == D(1)  # 95 − 94 stays with the platform
        assert sn.liabilities == D(199) and sn.free == D(801)
        assert sn.volume["24h"] == (1, D(10000))

        await b.run(cb(ADMIN, "a"), cb(ADMIN, "afin"))
        text = plain(b.session.last(ADMIN))
        assert "Можно забрать: 801 USDT" in text and "всего +1 USDT" in text and "отсюда выводы: 1 000" in text

        forum = -100555
        monkeypatch.setattr(config, "log_chat_id", forum)
        b.session.forums[forum] = set()
        await tasks.stats_topic(b.bot)
        posted = [m for m in b.session.calls if type(m).__name__ == "SendMessage" and m.chat_id == forum]
        assert len(posted) == 1 and posted[0].message_thread_id and "Финансы Strait Pay" in posted[0].text
        assert any(m.name == "📊 Статистика и финансы" for m in b.session.calls if type(m).__name__ == "CreateForumTopic")
        await tasks.stats_topic(b.bot)  # ten minutes later: the same message is edited, not a new one
        assert len([m for m in b.session.calls if type(m).__name__ == "SendMessage" and m.chat_id == forum]) == 1
        assert [m for m in b.session.calls if type(m).__name__ == "EditMessageText" and m.chat_id == forum]
    go(fn)


def test_shortfall_is_shown_when_assets_are_below_users_money(go):
    async def fn(b):
        await ready(b)

        async def poor():
            return [{"asset": "USDT", "available": "50"}]
        b.rocket.balances = poor
        async with models.Session() as s:
            sn = await finance.snapshot(s)
            assert sn.free == D(50) - D(200)
            from bot.handlers.finance import render
            assert "Не хватает: 150 USDT" in plain(render(sn))
    go(fn)
