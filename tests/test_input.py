"""Commands mixed with messages: ordering per user, unknown commands, where replies appear, error answers."""
import asyncio

from aiogram.types import Chat, Message, Update
from sqlalchemy import func, select

from bot import models
from bot.handlers import start
from bot.models import Ticket
from tests.harness import cb, ids, msg, plain, tg
from tests.test_scenarios import BUYER, ready, user


def retired(b, chat):
    """Screens the bot deleted or stripped of buttons."""
    return {m.message_id for m in b.session.calls if getattr(m, "chat_id", None) == chat
            and type(m).__name__ in ("DeleteMessage", "EditMessageReplyMarkup")}


def test_parallel_updates_of_one_user_do_not_race(go):
    """A command and messages sent together are handled one by one: one live screen, state not clobbered."""
    async def fn(b):
        await ready(b)
        await b.run(cb(BUYER, "w:dep"))
        n = len(b.session.calls)
        updates = [msg(BUYER, "/deals"), msg(BUYER, "привет"), msg(BUYER, "/buy"), msg(BUYER, "/wallet")]
        await asyncio.gather(*(b.dp.feed_update(b.bot, u) for u in updates))
        screens = [plain(m.caption or m.text or "") for m in b.session.calls[n:] if getattr(m, "chat_id", None) == BUYER
                   and type(m).__name__ in ("SendAnimation", "SendMessage")]
        assert len(screens) == 4 and "Кошелёк" in screens[-1]  # one new screen per update, in the order sent
        assert not retired(b, BUYER)  # nothing deleted: earlier screens stay as history
        await b.run(msg(BUYER, "50"))  # the deposit prompt was cancelled by the commands: free text now
        assert "не распознано" in plain(b.session.last(BUYER))
    go(fn)


def test_unknown_command_is_not_taken_as_input(go):
    async def fn(b):
        await ready(b)
        await b.run(cb(BUYER, "sup"), msg(BUYER, "/cancel"))
        assert "Неизвестная команда" in plain(b.session.last(BUYER))
        async with models.Session() as s:
            assert await s.scalar(select(func.count(Ticket.id))) == 0  # not sent to support as a ticket
        await b.run(msg(BUYER, "Помогите со сделкой"))
        assert "не распознано" in plain(b.session.last(BUYER))  # the support prompt was closed
    go(fn)


def test_reply_to_free_text_appears_below_it(go):
    async def fn(b):
        await ready(b)
        before = (await user(BUYER)).ui_msg_id
        text = msg(BUYER, "привет")
        await b.run(text)
        last = [m for m in b.session.calls if getattr(m, "chat_id", None) == BUYER][-1]
        assert type(last).__name__ == "SendAnimation"  # a new screen under the user's message…
        assert (await user(BUYER)).ui_msg_id != before
        assert before not in [m.message_id for m in b.session.calls if type(m).__name__ == "DeleteMessage"]  # …old kept
        assert text.message.message_id not in [m.message_id for m in b.session.calls
                                                if type(m).__name__ == "DeleteMessage"]
    go(fn)


def test_group_messages_are_ignored(go):
    async def fn(b):
        await ready(b)
        n = len(b.session.calls)
        group = Update(update_id=next(ids), message=Message(
            message_id=next(ids), date=msg(BUYER).message.date, chat=Chat(id=-100, type="supergroup"),
            from_user=tg(BUYER), text="/start"))
        await b.run(group)
        assert len(b.session.calls) == n
    go(fn)


def test_failure_is_answered_and_dialog_reset(go, monkeypatch):
    async def fn(b):
        await ready(b)
        await b.run(cb(BUYER, "w:dep"))

        async def boom(*a, **k):
            raise RuntimeError("db down")
        monkeypatch.setattr(start.deals, "open_deals_of", boom)
        await b.run(cb(BUYER, "menu"))
        assert any("Не получилось" in a for a in b.session.alerts())  # the button does not spin forever
        await b.run(msg(BUYER, "/start"))
        assert "Не получилось обработать" in plain(b.session.last(BUYER))
        monkeypatch.undo()
        await b.run(msg(BUYER, "50"))  # the broken step was reset: not taken as a deposit amount
        assert "не распознано" in plain(b.session.last(BUYER))
    go(fn)
