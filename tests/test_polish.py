"""Deal screen helpers, dispute verdict comments, card confirmation step, grouped admin settings."""
from sqlalchemy import func, select

from bot import models
from bot.handlers.seller import check_holder
from bot.models import Card, Deal
from tests.harness import cb, msg, plain
from tests.test_scenarios import ADMIN, BUYER, OTHER, PDF, SELLER, create_deal, get_deal, ready


def last_markup(b, chat):
    return next(m.reply_markup for m in reversed(b.session.calls)
                if getattr(m, "chat_id", None) == chat and getattr(m, "reply_markup", None))


def test_buyer_can_copy_requisites_and_amount(go):
    async def fn(b):
        await ready(b)
        await create_deal(b)
        copies = [x.copy_text.text for row in last_markup(b, BUYER).inline_keyboard for x in row if x.copy_text]
        assert copies == ["4111111111111111", "10000"]
        text = plain(b.session.last(BUYER))
        assert "этап 1 из 3" in text and "Комментарий к переводу не пишите" in text
    go(fn)


def test_verdict_comment_reaches_both_sides(go):
    async def fn(b):
        await ready(b)
        d = await create_deal(b)
        await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, document=PDF))
        async with models.Session() as s:
            (await s.get(Deal, d.id)).status = "dispute"
            await s.commit()
        await b.run(cb(ADMIN, f"ar:{d.id}:b"), cb(ADMIN, f"ar3:{d.id}:b"), msg(ADMIN, "ok"))
        assert "От 5 до 500" in plain(b.session.last(ADMIN))  # too short: asked again, nothing decided
        assert (await get_deal(d.id)).status == "dispute"
        await b.run(msg(ADMIN, "Перевод найден в выписке продавца"))
        deal = await get_deal(d.id)
        assert deal.status == "completed" and deal.resolution == "Перевод найден в выписке продавца"
        for uid in (BUYER, SELLER):
            assert "Комментарий администрации: Перевод найден в выписке продавца" in plain(b.session.last(uid))
    go(fn)


def test_card_is_saved_only_after_review(go):
    async def fn(b):
        await b.run(msg(OTHER, "/start"), cb(OTHER, "sl:add"), cb(OTHER, "sl:add:sbp"), cb(OTHER, "sl:bank:0"),
                    msg(OTHER, "8 900 123-45-67"), msg(OTHER, "Иван Иванович И."), msg(OTHER, "1000"),
                    msg(OTHER, "20000"))
        text = plain(b.session.last(OTHER))
        assert "Проверьте реквизиты" in text and "+79001234567" in text
        async with models.Session() as s:
            assert await s.scalar(select(func.count(Card.id))) == 0
        await b.run(cb(OTHER, "sl:save"), cb(OTHER, "sl:save"))  # second tap: the button is stale, no duplicate
        async with models.Session() as s:
            assert await s.scalar(select(func.count(Card.id))) == 1
    go(fn)


def test_holder_accepts_bank_style_initials():
    assert check_holder("Иван  Иванович И.") == "Иван Иванович И."
    assert check_holder("Анна-Мария Петрова") == "Анна-Мария Петрова"
    assert check_holder("Иван") is None and check_holder("Иван 123") is None


def test_settings_are_grouped_and_readable(go):
    async def fn(b):
        await ready(b)
        await b.run(cb(ADMIN, "as"))
        assert "Курс и комиссии" in plain(b.session.last(ADMIN))
        await b.run(cb(ADMIN, "asg:0"), cb(ADMIN, "as:rate"))
        assert "По умолчанию: 100 ₽" in plain(b.session.last(ADMIN))
        await b.run(msg(ADMIN, "98"))
        assert "Сохранено: 100 ₽ → 98 ₽" in plain(b.session.last(ADMIN))
    go(fn)
