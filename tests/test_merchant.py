"""Seller (merchant) tools: sales stats, reused receipt warning, shift reminder, receipt at the expiry second."""
from datetime import timedelta
from decimal import Decimal as D

from aiogram.types import Document

from bot import models, tasks
from bot.models import Deal, Event, User
from bot.services import deals, settings
from sqlalchemy import select
from tests.harness import cb, msg, plain
from tests.test_scenarios import ADMIN, BUYER, OTHER, PDF, SELLER, create_deal, get_deal, ready


def test_sales_stats_and_today_line(go):
    async def fn(b):
        await ready(b)
        d = await create_deal(b)
        await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, document=PDF), cb(SELLER, f"dl:ok2:{d.id}"))
        await b.run(cb(SELLER, "sl"))
        assert "Сегодня: 1 · 10 000 ₽ · +5 USDT" in plain(b.session.last(SELLER))
        await b.run(cb(SELLER, "sl:st"))
        text = plain(b.session.last(SELLER))
        assert "Статистика и доход" in text and "Доход: +5 USDT" in text and "Подтверждаете в среднем" in text
    go(fn)


def test_reused_receipt_file_is_flagged(go):
    async def fn(b):
        await ready(b, balance=D(500))
        d1 = await create_deal(b)
        await b.run(cb(BUYER, f"dl:rc:{d1.id}"), msg(BUYER, document=PDF), cb(SELLER, f"dl:ok2:{d1.id}"))
        await b.run(msg(OTHER, "/start"))
        d2 = await create_deal(b, buyer=OTHER)
        same = Document(file_id="pdf-renamed", file_unique_id=PDF.file_unique_id, mime_type="application/pdf",
                        file_name="new.pdf")
        await b.run(cb(OTHER, f"dl:rc:{d2.id}"), msg(OTHER, document=same))
        assert (await get_deal(d2.id)).status == "paid"  # accepted, but the seller is warned
        caption = [m.caption for m in b.session.calls if type(m).__name__ == "SendDocument" and m.chat_id == SELLER][-1]
        assert f"уже присылали по сделке #{d1.id}" in plain(caption)
        async with models.Session() as s:
            assert await s.scalar(select(Event.id).where(Event.kind == "receipt_reused", Event.alert))
    go(fn)


def test_seller_is_reminded_before_shift_ends(go):
    async def fn(b):
        await ready(b)
        async with models.Session() as s:
            (await s.get(User, SELLER)).last_seen = models.now() - timedelta(
                minutes=settings.num("online_minutes") - 3)
            await s.commit()
        await tasks.auto_offline(b.bot)
        await tasks.auto_offline(b.bot)  # one reminder, not one per minute
        reminders = [t for t in b.session.texts(SELLER) if "Смена завершится через" in t]
        assert len(reminders) == 1 and "sl:on:1" in b.session.buttons(SELLER)
        async with models.Session() as s:
            assert (await s.get(User, SELLER)).is_online
    go(fn)


def test_receipt_at_the_expiry_second_becomes_late_receipt(go, monkeypatch):
    async def fn(b):
        await ready(b)
        d = await create_deal(b)
        await b.run(cb(BUYER, f"dl:rc:{d.id}"))
        real = deals.mark_paid

        async def expired_meanwhile(s, deal_id, buyer_id, file_id):  # the expiry task wins the race
            await deals.expire(s, deal_id)
            return await real(s, deal_id, buyer_id, file_id)
        monkeypatch.setattr(deals, "mark_paid", expired_meanwhile)
        await b.run(msg(BUYER, document=PDF))
        deal = await get_deal(d.id)
        assert deal.status == "paid" and deal.receipt_file_id == PDF.file_id  # not lost: back to the seller
    go(fn)


def test_dispute_evidence_is_sent_to_admin_as_albums(go):
    async def fn(b):
        await ready(b)
        d = await create_deal(b)
        await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, document=PDF))
        async with models.Session() as s:
            deal = await s.get(Deal, d.id)
            deal.status = "dispute"
            deal.dispute_files = ([["photo", f"p{i}", "buyer"] for i in range(3)] + [["video", "v1", "seller"]]
                                  + [["document", f"d{i}", "seller"] for i in range(2)] + [["text", "Перевёл в 12:03", "buyer"]])
            await s.commit()
        n = len(b.session.calls)
        await b.run(cb(ADMIN, f"af:{d.id}"))
        sent = [type(m).__name__ for m in b.session.calls[n:] if getattr(m, "chat_id", None) == ADMIN]
        assert sent == ["SendDocument", "SendMessage", "SendMediaGroup", "SendMediaGroup"]  # receipt, texts, 2 albums
        albums = [m for m in b.session.calls[n:] if type(m).__name__ == "SendMediaGroup"]
        assert [len(a.media) for a in albums] == [4, 2]
    go(fn)


def test_merchant_panel_work_list_settings_and_income(go):
    async def fn(b):
        await ready(b)
        d = await create_deal(b)
        await b.run(cb(SELLER, "sl"))
        assert "панель мерчанта" in plain(b.session.last(SELLER)) and "sl:work" in b.session.buttons(SELLER)
        await b.run(cb(SELLER, "sl:work"))
        assert f"dl:{d.id}" in b.session.buttons(SELLER)
        await b.run(cb(SELLER, "sl:cfg"), cb(SELLER, "sl:quiet"))
        assert "без звука" in plain(b.session.last(SELLER))
        n = len(b.session.calls)
        await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, document=PDF))
        receipt = [m for m in b.session.calls[n:] if type(m).__name__ == "SendDocument" and m.chat_id == SELLER]
        assert receipt and receipt[0].disable_notification  # the seller asked for quiet notifications
        await b.run(cb(SELLER, f"dl:ok2:{d.id}"))
        await b.run(cb(SELLER, "sl:st"))
        text = plain(b.session.last(SELLER))
        for part in ("Статистика и доход", "Сегодня", "Всё время", "средний чек 10 000 ₽", "Успешных: 100%",
                     "Доход по дням", "+5 USDT"):
            assert part in text, part
    go(fn)
