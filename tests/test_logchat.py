"""Log chat as a forum: topics created by the bot, one live card per operation, "needs attention" topic."""
from sqlalchemy import select

from bot import models
from bot.config import config
from bot.handlers import logchat
from bot.models import Card, Deal, LogMessage
from tests.harness import cb, msg, plain
from tests.test_scenarios import BUYER, PDF, SELLER, create_deal, ready

FORUM = -1001234


def calls(b, name, chat=FORUM):
    return [m for m in b.session.calls if type(m).__name__ == name and getattr(m, "chat_id", None) == chat]


def topic_of(b, title):
    return next(m for m in calls(b, "CreateForumTopic") if m.name == title)


def test_forum_topics_and_live_cards(go, monkeypatch):
    async def fn(b):
        monkeypatch.setattr(config, "log_chat_id", FORUM)
        b.session.forums[FORUM] = set()
        await ready(b)
        d = await create_deal(b)
        await b.deliver()
        created = {m.name for m in calls(b, "CreateForumTopic")}
        assert "💱 Сделки" in created and "👤 Пользователи" in created and "💳 Карты" in created
        card = [m for m in calls(b, "SendMessage") if f"Сделка #{d.id}" in m.text][0]
        assert "Ждём перевод" in plain(card.text) and "4111" not in card.text  # status, no requisites
        async with models.Session() as s:
            lm = await s.scalar(select(LogMessage).where(LogMessage.ref == f"deal:{d.id}"))
        deals_thread = card.message_thread_id
        assert deals_thread is not None and lm.thread_id == deals_thread

        await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, document=PDF))
        await b.deliver()
        edit = calls(b, "EditMessageText")[-1]
        assert edit.message_id == lm.msg_id and "Чек у продавца" in plain(edit.text)  # the same card, new status
        assert not [m for m in calls(b, "SendMessage") if f"Сделка #{d.id}" in m.text and m is not card]

        async with models.Session() as s:  # the seller says the money did not come
            deal = await s.get(Deal, d.id)
            deal.status, deal.dispute_reason = "dispute", "not_received"
            from bot.services import events
            events.add(s, f"deal:{d.id}", "dispute", "Спор: деньги не пришли", alert=True)
            await s.commit()
        await b.deliver()
        attention = topic_of(b, "⚠️ Требует внимания")
        rang = [m for m in calls(b, "SendMessage") if m.message_thread_id and "Спор: деньги не пришли" in m.text]
        assert rang and not rang[-1].disable_notification and "нужно внимание" in plain(rang[-1].text)
        assert attention is not None and "Спор" in plain(calls(b, "EditMessageText")[-1].text)

        b.session.forums[FORUM].discard(deals_thread)  # an admin deleted the topic
        async with models.Session() as s:  # a second card, so another buyer can open a deal
            s.add(Card(user_id=SELLER, kind="card", bank="Т-Банк", requisites="5536913812345672", holder="Иванов Иван",
                       min_rub=1000, max_rub=5000, is_active=True))
            await s.commit()
        await b.run(msg(30, "/start"), cb(30, "buy:0"), msg(30, "2000"))
        go_btn = next(x for x in b.session.buttons(30) if x and x.startswith("bgo:"))
        await b.run(cb(30, go_btn))
        await b.deliver()
        assert len([m for m in calls(b, "CreateForumTopic") if m.name == "💱 Сделки"]) == 2  # re-created once
    go(fn)


def test_plain_chat_rings_for_problems_by_reposting_the_card(go):
    async def fn(b):
        await ready(b)
        d = await create_deal(b)
        await b.deliver()
        first = [m for m in b.session.calls if type(m).__name__ == "SendMessage" and m.chat_id == 1
                 and f"Сделка #{d.id}" in m.text][0]
        async with models.Session() as s:
            from bot.services import events
            events.add(s, f"deal:{d.id}", "dispute", "Спор открыт", alert=True)
            await s.commit()
        await b.deliver()
        cards = [m for m in b.session.calls if type(m).__name__ == "SendMessage" and m.chat_id == 1
                 and f"Сделка #{d.id}" in m.text]
        assert len(cards) == 2 and "нужно внимание" in plain(cards[-1].text)
        stub = [m for m in b.session.calls if type(m).__name__ == "EditMessageText" and m.chat_id == 1
                and "обновлено ниже" in (m.text or "")]
        assert stub and first is cards[0]
        assert logchat.important is not None
    go(fn)


def group_msg(uid, chat, text):
    from datetime import datetime
    from aiogram.types import Chat, Message, Update
    from tests.harness import ids, tg
    return Update(update_id=next(ids), message=Message(message_id=next(ids), date=datetime.now(),
                                                       chat=Chat(id=chat, type="supergroup", title="log"),
                                                       from_user=tg(uid), text=text))


def test_setts_sets_up_the_log_chat_from_the_group(go):
    async def fn(b):
        from tests.test_scenarios import ADMIN, OTHER
        group = -100777
        b.session.forums[group] = set()
        await b.run(group_msg(OTHER, group, "/setts"))  # not an admin: ignored
        assert not calls(b, "CreateForumTopic", group)
        await b.run(group_msg(ADMIN, group, "/setts"))
        created = [m.name for m in calls(b, "CreateForumTopic", group)]
        assert len(created) == len(logchat.TOPICS) and "⚠️ Требует внимания" in created and "🔑 API: заявки и клиенты" in created
        abouts = [m for m in calls(b, "SendMessage", group) if m.message_thread_id]
        assert len(abouts) == len(logchat.TOPICS)  # every topic starts with what it is for
        assert "Лог-чат Strait Pay настроен" in plain(calls(b, "SendMessage", group)[-1].text)
        assert logchat.targets() == [group]

        await b.run(group_msg(ADMIN, group, "/setts"))  # again: nothing duplicated
        assert len(calls(b, "CreateForumTopic", group)) == len(logchat.TOPICS)
        await ready(b)
        await b.deliver()  # logs now go to the new group, into topics
        assert [m for m in calls(b, "SendMessage", group) if "Пользователь #" in m.text and m.message_thread_id]

        plain_group = -100888
        await b.run(group_msg(ADMIN, plain_group, "/setts"))
        assert "включить" in plain(calls(b, "SendMessage", plain_group)[-1].text)  # how to enable topics
    go(fn)
