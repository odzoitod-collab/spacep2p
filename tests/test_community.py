"""The community gate: the main menu opens only after joining the chat and subscribing to the info channel; a join
gets a greeting in the chat; deal screens are never blocked."""
from datetime import datetime

from aiogram.types import Chat, ChatInviteLink, ChatMemberLeft, ChatMemberMember, ChatMemberUpdated, Update

from bot import models
from bot.models import User
from bot.services import settings
from tests.harness import cb, ids, msg, plain, tg
from tests.test_scenarios import ADMIN, BUYER, SELLER, create_deal, ready

CHAT, CHANNEL = -100700, -100800


async def community(b, required="1"):
    async with models.Session() as s:
        for key, value in (("chat_id", str(CHAT)), ("channel_id", str(CHANNEL)), ("join_required", required)):
            await settings.put(s, key, value)
        await s.commit()


def member_update(chat, uid, joined=True, link_name=None):
    old, new = (ChatMemberLeft(user=tg(uid)), ChatMemberMember(user=tg(uid)))
    if not joined:
        old, new = new, old
    invite = ChatInviteLink(invite_link="https://t.me/+x", creator=tg(123), creates_join_request=False,
                            is_primary=False, is_revoked=False, name=link_name) if link_name else None
    return Update(update_id=next(ids), chat_member=ChatMemberUpdated(
        chat=Chat(id=chat, type="channel" if chat == CHANNEL else "supergroup"), from_user=tg(uid), date=datetime.now(),
        old_chat_member=old, new_chat_member=new, invite_link=invite))


async def flags(uid):
    async with models.Session() as s:
        u = await s.get(User, uid)
        return u.in_chat, u.in_channel


def test_menu_opens_after_joining_the_chat_and_the_channel(go):
    async def fn(b):
        await ready(b)
        await community(b)
        b.session.outsiders |= {(CHAT, BUYER), (CHANNEL, BUYER)}
        await b.run(msg(BUYER, "/start"))
        screen = plain(b.session.last(BUYER))
        assert "Последний шаг" in screen and "Чат Strait Pay: нужно вступить" in screen
        assert "Инфо-канал: нужно подписаться" in screen and "join:chk" in b.session.buttons(BUYER)
        await b.run(cb(BUYER, "w"), cb(BUYER, "buy:0"))  # every entry point leads here
        assert "Последний шаг" in plain(b.session.last(BUYER))

        await b.run(cb(BUYER, "join:chat"))
        link = [m for m in b.session.calls if type(m).__name__ == "CreateChatInviteLink" and m.chat_id == CHAT][-1]
        assert (link.member_limit, link.name) == (1, str(BUYER))
        assert any(x and x.startswith("https://t.me/+inv") for x in b.session.buttons(BUYER))

        await b.run(member_update(CHAT, BUYER, link_name=str(BUYER)))  # he joined by the link
        assert await flags(BUYER) == (True, False)
        greeting = [m for m in b.session.calls if type(m).__name__ == "SendMessage" and m.chat_id == CHAT][-1]
        assert "Добро пожаловать, U20" in plain(greeting.text) and "Правила" in plain(greeting.text)
        await b.run(member_update(CHAT, SELLER))  # the next greeting replaces the previous one
        assert [m for m in b.session.calls if type(m).__name__ == "DeleteMessage" and m.chat_id == CHAT]

        await b.run(cb(BUYER, "join:chk"))
        assert "Пока не вижу вас в канале" in plain(b.session.last(BUYER))
        b.session.outsiders.discard((CHANNEL, BUYER))  # subscribed
        await b.run(cb(BUYER, "join:chk"))
        assert "добро пожаловать в Strait Pay" in plain(b.session.last(BUYER)) and await flags(BUYER) == (True, True)
        await b.run(cb(BUYER, "w"))
        assert "Кошелёк" in plain(b.session.last(BUYER))

        await b.run(member_update(CHAT, BUYER, joined=False))  # he left the chat: the gate is back
        assert await flags(BUYER) == (False, True)
        await b.run(cb(BUYER, "menu"))
        assert "Последний шаг" in plain(b.session.last(BUYER))
    go(fn)


def test_the_gate_never_blocks_a_deal_or_an_admin(go):
    async def fn(b):
        await ready(b)
        d = await create_deal(b)
        await community(b)
        b.session.outsiders |= {(CHAT, BUYER), (CHANNEL, BUYER)}
        await b.run(cb(BUYER, f"dl:{d.id}"))
        assert f"Сделка #{d.id}" in plain(b.session.last(BUYER))  # a deal in progress goes on
        await b.run(cb(BUYER, "info"))
        assert "Помощь" in plain(b.session.last(BUYER))
        await b.run(cb(ADMIN, "menu"))
        assert "Последний шаг" not in plain(b.session.last(ADMIN))
        await community(b, required="0")  # only offered, not required
        await b.run(cb(BUYER, "menu"))
        assert "Последний шаг" not in plain(b.session.last(BUYER))
    go(fn)


def test_the_bot_lays_out_the_info_channel(go):
    from bot.handlers.channel import POSTS

    async def fn(b):
        await ready(b)
        await b.run(cb(ADMIN, "ach"), cb(ADMIN, "achn"))
        assert "acc:channel_id" in b.session.buttons(ADMIN) and "Инфо-канал" in plain(b.session.last(ADMIN))
        await b.run(cb(ADMIN, "acc:channel_id"), msg(ADMIN, str(CHANNEL)))
        assert "Инфо-канал" in plain(b.session.last(ADMIN)) and "achn:pub" in b.session.buttons(ADMIN)
        await b.run(cb(ADMIN, "achn:pub"))
        sent = [m for m in b.session.calls if getattr(m, "chat_id", None) == CHANNEL
                and type(m).__name__ in ("SendMessage", "SendAnimation")]
        assert len(sent) == len(POSTS) and type(sent[0]).__name__ == "SendAnimation"  # the greeting under the banner
        texts = [plain(getattr(m, "text", None) or m.caption) for m in sent]
        assert texts[0].startswith("💱 Strait Pay — обмен USDT") and "👉 Начать: @straitpay_bot" in texts[0]
        assert texts[2].startswith("🚀 Начните здесь: 3 шага") and any("пауза 72 ч" in t for t in texts)
        assert "tg-emoji" not in "".join(getattr(m, "text", None) or m.caption for m in sent)  # channels: plain emoji
        contents = [m for m in b.session.calls if type(m).__name__ == "EditMessageText" and m.chat_id == CHANNEL][-1]
        assert contents.text.count('href="https://t.me/c/800/') == len(POSTS) - 2  # a link to every section
        assert [m for m in b.session.calls if type(m).__name__ == "PinChatMessage" and m.chat_id == CHANNEL]
        assert f"Канал оформлен: новых постов {len(POSTS)}" in plain(b.session.last(ADMIN))
        before = len(sent)
        await b.run(cb(ADMIN, "achn:pub"))  # again: the same posts are edited, nothing new
        assert len([m for m in b.session.calls if getattr(m, "chat_id", None) == CHANNEL
                    and type(m).__name__ in ("SendMessage", "SendAnimation")]) == before
        assert "новых постов 0" in plain(b.session.last(ADMIN))
    go(fn)


def test_promo_autoposts_go_in_turn(go):
    from datetime import timedelta

    from bot import tasks
    from bot.handlers.channel import PROMOS
    from bot.models import Setting, now

    async def fn(b):
        await ready(b)
        async with models.Session() as s:
            await settings.put(s, "channel_id", str(CHANNEL))
            await s.commit()
        await tasks.channel_autopost(b.bot)
        first = [m for m in b.session.calls if getattr(m, "chat_id", None) == CHANNEL]
        assert len(first) == 1 and "Курсы Strait Pay сегодня" in plain(first[0].caption)
        assert "@straitpay_bot" in plain(first[0].caption)
        await tasks.channel_autopost(b.bot)  # not due yet
        assert len([m for m in b.session.calls if getattr(m, "chat_id", None) == CHANNEL]) == 1
        async with models.Session() as s:  # a day later
            row = await s.get(Setting, "chpromo")
            row.value = f"0:{(now() - timedelta(hours=25)).isoformat()}"
            await s.commit()
        await tasks.channel_autopost(b.bot)
        posts = [m for m in b.session.calls if getattr(m, "chat_id", None) == CHANNEL]
        assert len(posts) == 2 and "Почему с нами безопасно" in plain(posts[-1].text)
        await b.run(cb(ADMIN, "achn"), cb(ADMIN, "achn:promo"))
        assert f"Опубликовано промо 3 из {len(PROMOS)}" in plain(b.session.last(ADMIN))
        async with models.Session() as s:
            await settings.put(s, "channel_autopost_hours", "0")
            await s.commit()
        await tasks.channel_autopost(b.bot)  # off
        assert len([m for m in b.session.calls if getattr(m, "chat_id", None) == CHANNEL
                    and type(m).__name__.startswith("Send")]) == 3
    go(fn)
