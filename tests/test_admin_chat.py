"""The admin chat: buttons and commands work right in the group (nothing goes to the private chat), outsiders get
nothing, admins are given and taken from the panel, deep links open cards, teams and their chats are managed, admin
actions and bot errors get their own topics."""
import asyncio
import logging
from datetime import datetime

from aiogram.types import CallbackQuery, Chat, Message, Update
from sqlalchemy import select

from bot import models, ui
from bot.handlers import logchat
from bot.models import LogMessage, Team, User
from bot.services import admins, settings
from tests.harness import cb, ids, msg, plain, tg
from tests.test_scenarios import ADMIN, BUYER, OTHER, SELLER, create_deal, ready

GROUP = -100900  # the admin chat
TOPIC = 77
OWNER2 = 2  # the second owner from ADMIN_IDS


async def admin_chat(b, forum: bool = False):
    if forum:
        b.session.forums[GROUP] = {TOPIC}
    async with models.Session() as s:
        await settings.put(s, "log_chat", str(GROUP))
        await s.commit()


def gcb(uid, data, mid=5, thread=TOPIC):
    return Update(update_id=next(ids), callback_query=CallbackQuery(
        id=str(next(ids)), from_user=tg(uid), chat_instance="g", data=data,
        message=Message(message_id=mid, date=datetime.now(), chat=Chat(id=GROUP, type="supergroup"), text="x",
                        message_thread_id=thread)))


def gmsg(uid, text, thread=TOPIC):
    return Update(update_id=next(ids), message=Message(
        message_id=next(ids), date=datetime.now(), chat=Chat(id=GROUP, type="supergroup", is_forum=True),
        from_user=tg(uid), text=text, message_thread_id=thread, is_topic_message=True))


def to(b, chat, since=0):
    return [m for m in b.session.calls[since:] if getattr(m, "chat_id", None) == chat]


def named(b, name, since=0):
    return [m for m in b.session.calls[since:] if type(m).__name__ == name]


def test_a_button_in_the_admin_chat_edits_that_message_there(go):
    async def fn(b):
        await ready(b)
        await admin_chat(b)
        d = await create_deal(b)
        before = len(b.session.calls)
        await b.run(gcb(ADMIN, f"adv:{d.id}"))
        edits = [m for m in named(b, "EditMessageText", before) if m.chat_id == GROUP]
        assert edits and edits[-1].message_id == 5 and f"Сделка #{d.id}" in plain(edits[-1].text)
        assert not to(b, ADMIN, before)  # nothing in the admin's private chat
        await b.run(gcb(ADMIN, "a"))  # the panel opens in the same message too
        assert "Админ-панель" in plain(named(b, "EditMessageText")[-1].text) and not to(b, ADMIN, before)
    go(fn)


def test_outsiders_get_nothing_in_the_admin_chat(go):
    async def fn(b):
        await ready(b)
        await admin_chat(b)
        before = len(b.session.calls)
        await b.run(gcb(BUYER, "a"), gmsg(BUYER, "/admin"), gmsg(BUYER, "/team"), gmsg(BUYER, "/help"))
        assert b.session.alerts()[-1] == "Только для администраторов."
        assert not to(b, GROUP, before) and not named(b, "EditMessageText", before)  # silence for the rest
    go(fn)


def test_commands_and_answers_typed_in_the_admin_chat_stay_there(go):
    async def fn(b):
        await ready(b)
        await admin_chat(b)
        before = len(b.session.calls)
        await b.run(gmsg(ADMIN, "/admin"))
        panel = to(b, GROUP, before)[-1]
        assert "Админ-панель" in plain(panel.text) and panel.message_thread_id == TOPIC
        mid = ui.group_screen(ADMIN, GROUP)
        await b.run(gcb(ADMIN, "au", mid=mid))  # «Найти»: the bot asks, the admin answers in the chat
        typed = gmsg(ADMIN, str(BUYER))
        await b.run(typed)
        screen = named(b, "EditMessageText")[-1]
        assert screen.chat_id == GROUP and screen.message_id == mid
        assert f"Пользователь · {BUYER}" in plain(screen.text)
        assert any(type(m).__name__ == "DeleteMessage" and m.message_id == typed.message.message_id
                   for m in b.session.calls)  # the answer is removed, the screen shows the result
        assert not to(b, ADMIN, before)
        await b.run(gmsg(ADMIN, "просто болтаем"))  # no open question: the chat is left alone
        assert to(b, GROUP)[-1] is screen or type(to(b, GROUP)[-1]).__name__ == "DeleteMessage"
        await b.run(gmsg(ADMIN, "/whatever"))
        assert "Команды админ-чата" in plain(to(b, GROUP)[-1].text)
        await b.run(gmsg(ADMIN, f"/user @u{SELLER}"))
        assert f"Пользователь · {SELLER}" in plain(to(b, GROUP)[-1].text)
        await b.run(gmsg(ADMIN, "/deal 99"))
        assert "Сделка не найдена" in plain(to(b, GROUP)[-1].text)
    go(fn)


def test_owner_gives_and_takes_the_admin_status(go):
    async def fn(b):
        await ready(b)
        await admin_chat(b)
        await b.run(cb(BUYER, "a"))
        assert "Кнопка устарела" in b.session.alerts()[-1]  # not an admin yet
        await b.run(cb(ADMIN, f"auv:{BUYER}"))
        assert f"aga:{BUYER}" in b.session.buttons(ADMIN)
        await b.run(cb(ADMIN, f"aga:{BUYER}"), cb(ADMIN, f"aga:{BUYER}:1"))
        assert admins.is_admin(BUYER) and not admins.is_owner(BUYER)
        note = plain(b.session.last(BUYER))
        assert "Вам выданы права администратора" in note and "a" in b.session.buttons(BUYER)
        invite = [m for m in named(b, "CreateChatInviteLink") if m.chat_id == GROUP]
        assert invite and invite[-1].member_limit == 1  # a way into the admin chat
        menus = [m for m in named(b, "SetMyCommands") if getattr(m.scope, "chat_id", None) == BUYER]
        assert menus and "admin" in [c.command for c in menus[-1].commands]
        await b.run(cb(BUYER, "a"))
        assert "Админ-панель" in plain(b.session.last(BUYER)) and "админ" in plain(b.session.last(BUYER))
        await b.run(gcb(BUYER, "a"))  # and the admin chat's buttons work for him now
        assert "Админ-панель" in plain(named(b, "EditMessageText")[-1].text)

        await b.run(gmsg(BUYER, f"/addadmin {SELLER}"))  # an admin is not an owner: he cannot appoint
        assert "только владельцы" in plain(to(b, GROUP)[-1].text) and not admins.is_admin(SELLER)
        await b.run(gmsg(ADMIN, f"/addadmin @u{SELLER}"))
        assert "Администратор назначен" in plain(to(b, GROUP)[-1].text) and admins.is_admin(SELLER)
        await b.run(msg(OWNER2, "/start"), gmsg(ADMIN, f"/deladmin {OWNER2}"))
        assert "владелец" in plain(to(b, GROUP)[-1].text) and admins.is_admin(OWNER2)

        await b.run(cb(ADMIN, "aadm"))
        screen = plain(b.session.last(ADMIN))
        assert "Администраторы · 4" in screen and f"@u{BUYER}" in screen
        await b.run(cb(ADMIN, f"aga:{BUYER}:0"))
        assert not admins.is_admin(BUYER) and "Права администратора Strait Pay сняты" in plain(b.session.last(BUYER))
        assert [m for m in named(b, "BanChatMember") if m.chat_id == GROUP and m.user_id == BUYER]  # out of the chat
        assert [m for m in named(b, "DeleteMyCommands") if getattr(m.scope, "chat_id", None) == BUYER]
        await b.run(cb(BUYER, "a"))
        assert "Кнопка устарела" in b.session.alerts()[-1]
        async with models.Session() as s:  # kept in the database: a restart keeps the admins
            await settings.load(s)
        assert admins.ids() == [ADMIN, OWNER2, SELLER]
    go(fn)


def test_deep_links_open_admin_cards(go):
    async def fn(b):
        await ready(b)
        d = await create_deal(b)
        start = msg(ADMIN, f"/start a-deal-{d.id}")
        await b.run(start)
        assert f"Сделка #{d.id}" in plain(b.session.last(ADMIN))
        assert any(type(m).__name__ == "DeleteMessage" and m.message_id == start.message.message_id
                   for m in b.session.calls)
        await b.run(msg(ADMIN, f"/start a-user-{SELLER}"))
        assert f"Пользователь · {SELLER}" in plain(b.session.last(ADMIN))
        await b.run(msg(ADMIN, "/start a-team-42"))
        assert "Не найдено" in plain(b.session.last(ADMIN))
        await b.run(msg(BUYER, f"/start a-user-{SELLER}"))  # not an admin: just the menu
        assert f"Пользователь · {SELLER}" not in plain(b.session.last(BUYER))
    go(fn)


def test_admin_manages_a_team_its_members_and_its_chat(go):
    async def fn(b):
        await ready(b)
        team_chat = -100555
        async with models.Session() as s:
            s.add(Team(id=1, leader_id=SELLER, name="Альфа", status="approved", chat_id=team_chat))
            (await s.get(User, SELLER)).team_id = 1
            (await s.get(User, BUYER)).team_id = 1
            await s.commit()
        await b.run(cb(ADMIN, "atml"))
        assert "«Альфа» · работает · 1 чел." in plain(b.session.last(ADMIN))
        await b.run(cb(ADMIN, "atm:1"))
        card = plain(b.session.last(ADMIN))
        assert "Реферальная ссылка: https://t.me/straitpay_bot?start=t1" in card and str(team_chat) in card
        await b.run(cb(ADMIN, "atm:inv:1"))
        link = named(b, "CreateChatInviteLink")[-1]
        assert (link.chat_id, link.member_limit) == (team_chat, 1)
        assert any(x and x.startswith("https://t.me/+inv") for x in b.session.buttons(ADMIN))
        await b.run(cb(ADMIN, "atm:m:1"))
        assert f"@u{BUYER}" in plain(b.session.last(ADMIN))
        await b.run(cb(ADMIN, f"auv:{BUYER}"), cb(ADMIN, f"aux:{BUYER}"), cb(ADMIN, f"aux2:{BUYER}"))
        async with models.Session() as s:
            assert (await s.get(User, BUYER)).team_id is None
        assert [m for m in named(b, "BanChatMember") if m.chat_id == team_chat and m.user_id == BUYER]
        assert "Администрация убрала вас из команды «Альфа»" in plain(b.session.last(BUYER))
        await b.run(cb(ADMIN, "atm:uc:1"), cb(ADMIN, "atm:uc2:1"))
        async with models.Session() as s:
            assert (await s.get(Team, 1)).chat_id is None
        assert "отключила чат команды" in plain(b.session.last(SELLER))
    go(fn)


def test_admin_actions_and_errors_get_their_own_topics(go):
    async def fn(b):
        await ready(b)
        await admin_chat(b, forum=True)
        await b.run(cb(ADMIN, f"aub:{SELLER}:1"), cb(ADMIN, f"aub2:{SELLER}"))
        await b.deliver()
        topic = next(m for m in named(b, "CreateForumTopic") if m.name == "🛡 Действия админов")
        posts = [m for m in to(b, GROUP) if type(m).__name__ == "SendMessage" and "Заблокировал пользователя" in m.text]
        assert posts and posts[-1].message_thread_id is not None and topic is not None
        text = plain(posts[-1].text)
        assert f"Кто: @u{ADMIN}" in text and f"@u{SELLER}" in text

        handler = logchat.ErrorTopic(b.bot)
        log = logging.getLogger("bot.test")
        log.addHandler(handler)
        try:
            for _ in range(3):
                try:
                    {}["amount"]
                except KeyError:
                    log.exception("update failed")
                await asyncio.sleep(0.05)
        finally:
            log.removeHandler(handler)
        errors = [m for m in to(b, GROUP) if type(m).__name__ == "SendMessage" and "Ошибка в боте" in m.text]
        assert len(errors) == 1 and "KeyError: &#x27;amount&#x27;" in errors[0].text or "KeyError" in errors[0].text
        assert "test_admin_chat.py" in plain(errors[0].text)
        repeats = [m for m in to(b, GROUP) if type(m).__name__ == "EditMessageText" and "Повторов" in m.text]
        assert repeats and "×3" in plain(repeats[-1].text)  # repeats fold into the same post
    go(fn)


def test_decisions_on_the_log_card_show_who_took_them(go):
    async def fn(b):
        await ready(b)
        await admin_chat(b)
        await b.run(msg(OTHER, "/start"), cb(OTHER, "tm"), cb(OTHER, "tm:apply"), msg(OTHER, "Бета"),
                    msg(OTHER, "Опыт три года, приведу 5 человек"))
        await b.deliver()
        card = next(m for m in to(b, GROUP) if type(m).__name__ == "SendMessage" and "Команда" in plain(m.text))
        flat = [x.callback_data for row in card.reply_markup.inline_keyboard for x in row]
        assert flat[:2] == ["atm:ok:1", "atm:no:1"] and "atm:1" in flat
        async with models.Session() as s:
            mid = await s.scalar(select(LogMessage.msg_id).where(LogMessage.ref == "team:1"))
        await b.run(gcb(ADMIN, "atm:ok:1", mid=mid))
        edit = named(b, "EditMessageText")[-1]
        assert edit.message_id == mid and "Одобрено · U1" in plain(edit.text)
        assert "одобрена — вы тимлид" in plain(b.session.last(OTHER))
        await b.deliver()  # the card shows the decision from the data, edited in place (an admin's own act: no ring)
        last = [m for m in to(b, GROUP) if type(m).__name__ in ("SendMessage", "EditMessageText")
                and "Команда" in plain(m.text)][-1]
        assert "Решение: @u1 · U1 · 1" in plain(last.text) and "работает" in plain(last.text)
        assert "atm:ok:1" not in [x.callback_data for row in (last.reply_markup.inline_keyboard
                                                               if last.reply_markup else []) for x in row]
    go(fn)


def test_a_log_card_opens_in_place_and_folds_back(go):
    async def fn(b):
        await ready(b)
        await admin_chat(b)
        d = await create_deal(b)
        await b.deliver()
        async with models.Session() as s:
            mid = await s.scalar(select(LogMessage.msg_id).where(LogMessage.ref == f"deal:{d.id}"))
        await b.run(gcb(ADMIN, f"adv:{d.id}", mid=mid))
        full = named(b, "EditMessageText")[-1]
        flat = [x.callback_data for row in full.reply_markup.inline_keyboard for x in row]
        assert full.message_id == mid and "Реквизиты" in plain(full.text) and f"lg:deal:{d.id}" in flat
        assert flat[-1] == "ad"  # the navigation stays the last row
        await b.run(gcb(ADMIN, f"lg:deal:{d.id}", mid=mid))
        card = named(b, "EditMessageText")[-1]
        assert card.message_id == mid and "История" in plain(card.text) and "Подробнее" in str(card.reply_markup)
        await b.run(gmsg(ADMIN, f"/find #{d.id}"))
        assert f"Сделка #{d.id}" in plain(to(b, GROUP)[-1].text)
        await b.run(gmsg(ADMIN, "/find нечто"))
        assert "Ничего не найдено" in plain(to(b, GROUP)[-1].text)
    go(fn)
