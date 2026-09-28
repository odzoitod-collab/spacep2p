"""Seller cards: what makes a card visible to buyers, card management, banner and navigation rules."""
from datetime import timedelta
from decimal import Decimal as D

from bot import emoji, models, tasks, ui
from bot.models import Card, User
from bot.services import settings
from tests.harness import cb, cb_main, is_emoji, msg, plain
from tests.test_scenarios import BUYER, OTHER, PDF, SELLER, create_deal, ready, user


async def card(cid=1):
    async with models.Session() as s:
        return await s.get(Card, cid)


def market_buttons(b):
    return [x for x in b.session.buttons(BUYER) if x and x.startswith("bc:")]


def test_all_emoji_fallbacks_are_real_emoji():
    """₽ or % inside <tg-emoji> makes Telegram reject the whole message (ENTITY_TEXT_INVALID)."""
    bad = {name: fb for name, (_, fb) in emoji.E.items() if not is_emoji(fb)}
    assert not bad, bad


def test_bad_custom_emoji_does_not_break_screens(go):
    async def fn(b):
        old = emoji.E["ruble"]
        emoji.E["ruble"] = (old[0], "₽")  # e.g. an invalid id/fallback slipped in again
        try:
            await ready(b)
            await b.run(cb(SELLER, "cd:1"))
        finally:
            emoji.E["ruble"] = old
        assert "Сумма одной сделки" in plain(b.session.last(SELLER))  # shown without custom emoji
        assert (await card()).is_active
    go(fn)


def test_card_is_saved_even_if_its_screen_fails(go):
    """Reported bug: the card screen crashed, the transaction rolled back, the card never reached buyers."""
    async def fn(b):
        await b.run(msg(SELLER, "/start"), msg(BUYER, "/start"))
        async with models.Session() as s:
            from bot.services import money
            await money.add(s, SELLER, D(200), "deposit", "dep:0")
            await s.commit()
        await b.run(cb(SELLER, "sl:add"), cb(SELLER, "sl:add:card"), cb(SELLER, "sl:bank:0"),
                    msg(SELLER, "4111 1111 1111 1111"), msg(SELLER, "Иванов Иван"), msg(SELLER, "1000"),
                    msg(SELLER, "50000"))
        b.session.fail_once.add(SELLER)
        try:
            await b.run(cb(SELLER, "sl:save"))
        except Exception:
            pass  # the screen failed; the data must stay
        c = await card()
        assert c and c.is_active and (await user(SELLER)).is_online
        await b.run(cb(BUYER, "buy:0"))
        assert market_buttons(b) == ["bc:1"]
    go(fn)


def test_enabling_card_starts_shift_and_shows_it_to_buyers(go):
    async def fn(b):
        await ready(b)
        await b.run(cb(SELLER, "cd:off:1"), cb(SELLER, "sl:on:0"), cb(BUYER, "buy:0"))
        assert market_buttons(b) == []
        await b.run(cb(SELLER, "cd:on:1"))
        assert "вы вышли на смену" in plain(b.session.last(SELLER))
        assert (await user(SELLER)).is_online
        await b.run(cb(BUYER, "buy:0"))
        assert market_buttons(b) == ["bc:1"]
        await b.run(cb(SELLER, "buy:0"))
        assert "Ваши карты (1) здесь не показываются" in plain(b.session.last(SELLER))
    go(fn)


def test_auto_offline_explains_and_card_reappears_after_shift(go):
    async def fn(b):
        await ready(b)
        async with models.Session() as s:
            (await s.get(User, SELLER)).last_seen = models.now() - timedelta(hours=2)
            await s.commit()
        await tasks.auto_offline(b.bot)
        await b.run(cb(BUYER, "buy:0"))
        assert market_buttons(b) == []
        await b.run(cb(SELLER, "cd:1"))
        assert "вы не на смене" in plain(b.session.last(SELLER))
        assert "cd:shift:1" in b.session.buttons(SELLER)
        await b.run(cb(SELLER, "cd:shift:1"), cb(BUYER, "buy:0"))
        assert market_buttons(b) == ["bc:1"]
    go(fn)


def test_daily_limit_caps_and_hides_card(go):
    async def fn(b):
        await ready(b, balance=D(1000))
        await b.run(cb(SELLER, "ce:daily:1"), msg(SELLER, "15000"))
        assert (await card()).daily_limit_rub == D(15000)
        d = await create_deal(b, "10000")
        await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, document=PDF), cb(SELLER, f"dl:ok2:{d.id}"))
        await b.run(cb(BUYER, "buy:0"))
        assert any("1 000–5 000 ₽" in (bt.text or "") for m in b.session.calls[-3:]
                   for row in getattr(getattr(m, "reply_markup", None), "inline_keyboard", []) or [] for bt in row)
        await b.run(cb(BUYER, "bc:1"), msg(BUYER, "6000"))
        assert "Сумма вне лимитов продавца: 1 000 – 5 000 ₽" in plain(b.session.last(BUYER))
        d2 = await create_deal(b, "5000")
        await b.run(cb(BUYER, f"dl:rc:{d2.id}"), msg(BUYER, document=PDF), cb(SELLER, f"dl:ok2:{d2.id}"))
        await b.run(cb(SELLER, "cd:1"))
        assert "дневной лимит исчерпан: принято 15 000 из 15 000 ₽" in plain(b.session.last(SELLER))
        await b.run(cb(BUYER, "buy:0"))
        assert market_buttons(b) == []
        await b.run(cb(SELLER, "ce:daily:1"), msg(SELLER, "0"))
        assert (await card()).daily_limit_rub is None
    go(fn)


def test_edit_limits_bank_holder_requisites(go):
    async def fn(b):
        await ready(b)
        await b.run(cb(SELLER, "ce:min:1"), msg(SELLER, "60000"))
        assert "Минимум не может быть больше максимума" in plain(b.session.last(SELLER))
        await b.run(msg(SELLER, "2000"), cb(SELLER, "ce:max:1"), msg(SELLER, "30000"),
                    cb(SELLER, "ce:bank:1"), cb(SELLER, "ceb:1:1"),
                    cb(SELLER, "ce:holder:1"), msg(SELLER, "Петров Пётр"),
                    cb(SELLER, "ce:req:1"), msg(SELLER, "5555 5555 5555 4444"))
        c = await card()
        assert (c.min_rub, c.max_rub, c.bank, c.holder, c.requisites) == (
            D(2000), D(30000), "Т-Банк", "Петров Пётр", "5555555555554444")
    go(fn)


def test_requisites_locked_during_open_deal(go):
    async def fn(b):
        await ready(b)
        d = await create_deal(b)
        await b.run(cb(SELLER, "ce:req:1"))
        assert any("покупатель видит эти реквизиты" in a for a in b.session.alerts())
        await b.run(cb(SELLER, "ce:max:1"), msg(SELLER, "40000"))  # limits may change any time
        assert (await card()).max_rub == D(40000)
        await b.run(cb(BUYER, f"dl:cn2:{d.id}"), cb(SELLER, "ce:req:1"), msg(SELLER, "5555555555554444"))
        assert (await card()).requisites == "5555555555554444"
    go(fn)


def test_edit_requisites_rejects_duplicates(go):
    async def fn(b):
        await ready(b)
        async with models.Session() as s:
            s.add(User(id=OTHER))
            await s.flush()
            s.add(Card(user_id=OTHER, kind="card", bank="ВТБ", requisites="5555555555554444", holder="Иван Иванов",
                       min_rub=D(1000), max_rub=D(5000)))
            await s.commit()
        await b.run(cb(SELLER, "ce:req:1"), msg(SELLER, "5555555555554444"))
        assert "используются другим продавцом" in plain(b.session.last(SELLER))
        assert (await card()).requisites == "4111111111111111"
    go(fn)


def test_only_this_card_in_work(go):
    async def fn(b):
        await ready(b)
        async with models.Session() as s:
            s.add(Card(user_id=SELLER, kind="sbp", bank="ВТБ", requisites="+79990000000", holder="Иванов Иван",
                       min_rub=D(1000), max_rub=D(5000), is_active=True))
            await s.commit()
        await b.run(cb(SELLER, "cd:2"))
        assert "cd:solo:2" in b.session.buttons(SELLER)
        await b.run(cb(SELLER, "cd:solo:2"))
        assert [(c.id, c.is_active) for c in [await card(1), await card(2)]] == [(1, False), (2, True)]
        await b.run(cb(SELLER, "sl"), cb(SELLER, "sl:alloff"))
        assert not (await card(2)).is_active
    go(fn)


def test_banner_on_screens_and_text_fallback_for_long_ones(go):
    async def fn(b):
        def names():
            return [type(m).__name__ for m in b.session.calls
                    if getattr(m, "chat_id", None) == BUYER and type(m).__name__ != "DeleteMessage"]
        await b.run(msg(BUYER, "/start"))
        assert names()[-1] == "SendAnimation"
        first = [m for m in b.session.calls if type(m).__name__ == "SendAnimation"][0]
        assert first.disable_notification and str(first.animation.path).endswith("banner.mp4")  # silent screen
        await b.run(await cb_main(BUYER, "w"))
        assert names()[-1] == "EditMessageCaption" and "Кошелёк" in b.session.last(BUYER)  # edited in place
        async with models.Session() as s:
            await settings.put(s, "tutorial", "Очень подробно. " * 150)  # longer than a 1024 caption
            await s.commit()
        await b.run(await cb_main(BUYER, "info"))  # caption edit fails -> plain text message instead
        text_msg = [m for m in b.session.calls if type(m).__name__ == "SendMessage" and m.chat_id == BUYER][-1]
        assert "Как это работает" in text_msg.text
        await b.run(await cb_main(BUYER, "menu"))  # a press in the text message edits that message in place
        assert names()[-1] == "EditMessageText" and "Strait Pay" in b.session.last(BUYER)
        await b.run(msg(BUYER, "/start"))  # a new screen carries the banner again, sent by file_id
        sends = [m for m in b.session.calls if type(m).__name__ == "SendAnimation"]
        assert sends[0].animation != "banner-file-id" and sends[-1].animation == "banner-file-id"  # uploaded once
    go(fn)


def test_navigation_is_last_single_row(go):
    """The generic check in conftest validates every keyboard; this makes sure it is exercised."""
    async def fn(b):
        await ready(b)
        await b.run(cb(SELLER, "cd:1"))
        rows = next(m for m in reversed(b.session.calls) if getattr(m, "reply_markup", None)).reply_markup.inline_keyboard
        assert [x.text for x in rows[-1]] == ["Панель мерчанта"]
        assert ui.CAPTION_LIMIT == 1024
        # the layout rule on every keyboard the bot sent: rows of at most two, one back button alone at the bottom
        for m in b.session.calls:
            markup = getattr(m, "reply_markup", None)
            if markup is None or not hasattr(markup, "inline_keyboard"):
                continue
            assert all(len(r) <= 2 for r in markup.inline_keyboard), [[x.text for x in r] for r in markup.inline_keyboard]
            navs = [x for r in markup.inline_keyboard for x in r if isinstance(x, emoji.NavButton)]
            assert len(navs) <= 1 and (not navs or markup.inline_keyboard[-1] == navs)
        await b.run(cb(BUYER, "menu"))
        menu = next(m for m in reversed(b.session.calls) if getattr(m, "reply_markup", None) and m.chat_id == BUYER)
        rows = [[x.text for x in r] for r in menu.reply_markup.inline_keyboard]
        assert rows[0] == ["Кошелёк · 0 USDT"] and rows[1] == ["RUB ⇄ USDT", "USDT ⇄ RUB"]  # one big, two small
    go(fn)


def test_banner_uploaded_once_when_many_users_start_together(go):
    async def fn(b):
        import asyncio
        await asyncio.gather(*(b.dp.feed_update(b.bot, msg(uid, "/start")) for uid in (501, 502, 503, 504)))
        sends = [m for m in b.session.calls if type(m).__name__ == "SendAnimation"]
        uploads = [m for m in sends if m.animation != "banner-file-id"]
        assert len(sends) == 4 and len(uploads) == 1 and all(m.disable_notification for m in sends)
        await b.run(msg(501, "привет"))
        assert type(b.session.calls[-2]).__name__ == "SendAnimation"  # screens stay silent
    go(fn)


def test_button_press_on_banner_screen_edits_it_in_place(go):
    """Telegram fills `document` on every animation: the banner screen must still be edited, not re-sent."""
    async def fn(b):
        await ready(b)
        await b.run(msg(BUYER, "/start"))
        screen = (await user(BUYER)).ui_msg_id
        n = len(b.session.calls)
        for data in ("w", "w:in", "w", "menu", "buy:0", "menu", "deals", "menu", "info", "menu"):
            await b.run(await cb_main(BUYER, data, b.session))
        sent = [type(m).__name__ for m in b.session.calls[n:] if getattr(m, "chat_id", None) == BUYER
                and type(m).__name__.startswith("Send")]
        assert sent == [], sent  # ten presses, not a single new message
        edits = [m for m in b.session.calls[n:] if type(m).__name__ == "EditMessageCaption"]
        assert len(edits) == 10 and {m.message_id for m in edits} == {screen}
        assert (await user(BUYER)).ui_msg_id == screen
    go(fn)


def test_receipt_file_message_is_never_overwritten(go):
    async def fn(b):
        from tests.test_scenarios import create_deal
        from aiogram.types import CallbackQuery, Chat, Document, Message, Update
        from datetime import datetime
        from tests.harness import ids, tg
        await ready(b)
        d = await create_deal(b)
        await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, document=PDF))
        receipt = Message(message_id=777, date=datetime.now(), chat=Chat(id=SELLER, type="private"), caption="чек",
                          document=Document(file_id="pdf1", file_unique_id="u1"))
        await b.run(Update(update_id=next(ids), callback_query=CallbackQuery(
            id="c1", from_user=tg(SELLER), chat_instance="ci", data=f"dl:ok:{d.id}", message=receipt)))
        assert not [m for m in b.session.calls if getattr(m, "message_id", None) == 777
                    and type(m).__name__.startswith("Edit")]  # the receipt stays; the answer comes below it
    go(fn)
