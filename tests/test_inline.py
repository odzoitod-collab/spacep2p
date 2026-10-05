"""Inline search of deals and operations; picking a card edits the current screen and removes the sent command."""
from datetime import datetime

from aiogram.types import Chat, InlineQuery, Message, Update
from aiogram.types import User as TgUser

from tests.harness import ids, msg, plain, tg
from tests.test_scenarios import BUYER, PDF, SELLER, create_deal, ready, user


def inline(uid, query, offset=""):
    return Update(update_id=next(ids), inline_query=InlineQuery(id=str(next(ids)), from_user=tg(uid), query=query,
                                                                offset=offset))


def picked(uid, text):
    """What Telegram delivers when the user taps an inline result: a message sent via the bot."""
    return Update(update_id=next(ids), message=Message(
        message_id=next(ids), date=datetime.now(), chat=Chat(id=uid, type="private"), from_user=tg(uid), text=text,
        via_bot=TgUser(id=123, is_bot=True, first_name="Strait Pay")))


def answers(b):
    return [m for m in b.session.calls if type(m).__name__ == "AnswerInlineQuery"]


def test_deal_search_and_pick_edits_the_screen(go):
    async def fn(b):
        await ready(b)
        d = await create_deal(b)
        n = len(b.session.calls)
        await b.run(msg(BUYER, "/start"))
        buttons = [x for m in b.session.calls[n:] if getattr(m, "reply_markup", None)
                   for r in m.reply_markup.inline_keyboard for x in r if x.switch_inline_query_current_chat]
        assert buttons and buttons[0].switch_inline_query_current_chat == "сделки "  # «Мои сделки» opens the search
        await b.run(inline(BUYER, "сделки "))
        res = answers(b)[-1]
        assert res.is_personal and res.cache_time == 0
        assert res.results[0].title.startswith(f"↓ #{d.id} · 10 000 ₽")
        assert res.results[0].input_message_content.message_text == f"/deal {d.id}"
        await b.run(inline(SELLER, f"сделки #{d.id}"))
        assert [r.title[:4] for r in answers(b)[-1].results] == [f"↑ #{d.id}"[:4]]
        await b.run(inline(BUYER, "сделки 99999"))
        assert answers(b)[-1].results == [] and "Ничего не найдено" in answers(b)[-1].button.text

        screen = (await user(BUYER)).ui_msg_id
        pick = picked(BUYER, f"/deal {d.id}")
        n = len(b.session.calls)
        await b.run(pick)
        after = b.session.calls[n:]
        assert any(type(m).__name__ == "DeleteMessage" and m.message_id == pick.message.message_id for m in after)
        edit = [m for m in after if type(m).__name__.startswith("EditMessage") and m.message_id == screen]
        assert edit and f"Сделка #{d.id}" in plain(edit[-1].caption or edit[-1].text)
        assert not [m for m in after if type(m).__name__ in ("SendMessage", "SendAnimation")]  # no new message

        typed = msg(BUYER, f"/deal {d.id}")  # the same command typed by hand: a new message, nothing deleted
        await b.run(typed)
        assert typed.message.message_id not in [m.message_id for m in b.session.calls if type(m).__name__ == "DeleteMessage"]
        await b.run(picked(SELLER, f"/deal {d.id + 1000}"))
        assert "Сделка не найдена" in plain(b.session.last(SELLER))
    go(fn)


def test_operations_search_and_pick(go):
    async def fn(b):
        await ready(b)
        d = await create_deal(b)
        from tests.harness import cb
        await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, document=PDF), cb(SELLER, f"dl:ok2:{d.id}"))
        await b.run(inline(BUYER, "операции "))
        res = answers(b)[-1].results
        assert res and res[0].title == "Покупка +94 USDT" and res[0].input_message_content.message_text.startswith("/op ")
        await b.run(inline(BUYER, "операции покупка"), inline(BUYER, "операции вывод"))
        assert len(answers(b)[-2].results) == 1 and answers(b)[-1].results == []
        await b.run(picked(BUYER, res[0].input_message_content.message_text))
        text = plain(b.session.last(BUYER))
        assert "Покупка по сделке" in text and "+94 USDT" in text and f"dl:{d.id}" in b.session.buttons(BUYER)
        await b.run(picked(SELLER, res[0].input_message_content.message_text))  # someone else's operation
        assert "Операция не найдена" in plain(b.session.last(SELLER))
    go(fn)
