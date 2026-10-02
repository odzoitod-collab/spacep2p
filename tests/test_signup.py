"""Entry by application, messages through the bot, full control of a deal from the admin panel, /help in chats."""
from datetime import datetime, timedelta
from decimal import Decimal as D

from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, Update
from sqlalchemy import select

from bot import models
from bot.models import Event, Operator, Signup, User
from bot.services import settings
from tests.harness import cb, ids, msg, plain, tg
from tests.test_orders import deal, give, merchant, request
from tests.test_scenarios import ADMIN, BUYER, PDF, SELLER, create_deal, ready, user
from tests.test_teams_operators import group_msg

NEW, NEW2 = 70, 71
FORUM = -100900
SHOT = [PhotoSize(file_id="shot1", file_unique_id="s1", width=10, height=10)]


async def review_on():
    async with models.Session() as s:
        await settings.put(s, "signup_review", "1")
        await s.commit()


def group_cb(uid, chat, data, mid=1):
    return Update(update_id=next(ids), callback_query=CallbackQuery(
        id=str(next(ids)), from_user=tg(uid), chat_instance="ci", data=data,
        message=Message(message_id=mid, date=datetime.now(), chat=Chat(id=chat, type="supergroup"), caption="x")))


def test_new_user_applies_and_the_admin_lets_him_in_from_the_topic(go):
    async def fn(b):
        await ready(b)
        b.session.forums[FORUM] = set()
        async with models.Session() as s:
            await settings.put(s, "log_chat", str(FORUM))
            await s.commit()
        await review_on()
        await b.run(msg(NEW, "/start"))
        assert "вход по заявке" in plain(b.session.last(NEW)) and "su:r:seller" in b.session.buttons(NEW)
        await b.run(cb(NEW, "w"), msg(NEW, "/buy"))  # nothing of the bot before the decision
        assert "вход по заявке" in plain(b.session.last(NEW)) and "Кошелёк" not in plain(b.session.last(NEW))

        await b.run(cb(NEW, "su:r:seller"))
        assert "Какой оборот в день" in plain(b.session.last(NEW))
        await b.run(cb(NEW, "su:t:1"))
        assert "скриншот баланса или оборота" in plain(b.session.last(NEW))
        await b.run(msg(NEW, "вот"))  # text instead of a screenshot
        assert "Нужен скриншот" in plain(b.session.last(NEW))
        await b.run(msg(NEW, photo=SHOT))
        assert "Проверьте заявку" in plain(b.session.last(NEW)) and "Скриншот: прикреплён" in plain(b.session.last(NEW))
        await b.run(cb(NEW, "su:go"), cb(NEW, "su:go"))  # a double click sends one application
        assert "на рассмотрении" in plain(b.session.last(NEW))
        async with models.Session() as s:
            assert len((await s.scalars(select(Signup))).all()) == 1
            assert (await s.get(User, NEW)).access == "pending"

        posted = [m for m in b.session.calls if type(m).__name__ == "SendPhoto" and m.chat_id == FORUM]
        assert len(posted) == 1 and posted[0].photo == "shot1" and posted[0].message_thread_id
        card = plain(posted[0].caption)
        for part in ("Заявка на вход #1", f"@u{NEW}", "Роль: P2P-продавец", "Оборот в день: 100 000 – 500 000 ₽"):
            assert part in card, part
        topic = next(m for m in b.session.calls if type(m).__name__ == "CreateForumTopic"
                     and m.name == "📝 Заявки на вход")
        assert topic and ["sua:ok:1", "sua:no:1"] == [x.callback_data for r in posted[0].reply_markup.inline_keyboard
                                                      for x in r]

        await b.run(group_cb(SELLER, FORUM, "sua:ok:1"))  # not an admin: nothing happens
        assert (await user(NEW)).access == "pending"
        await b.run(group_cb(ADMIN, FORUM, "sua:ok:1"))
        assert (await user(NEW)).access == "approved"
        assert "Заявка одобрена" in plain(b.session.last(NEW)) and "menu" in b.session.buttons(NEW)
        edited = [m for m in b.session.calls if type(m).__name__ == "EditMessageCaption" and m.chat_id == FORUM]
        assert edited and "одобрена" in plain(edited[-1].caption) and not edited[-1].reply_markup
        await b.run(cb(NEW, "menu"))
        assert "Кошелёк" in str(b.session.buttons(NEW)) or "w" in b.session.buttons(NEW)
        await b.run(group_cb(ADMIN, FORUM, "sua:no:1"))
        assert any("уже одобрена" in a for a in b.session.alerts())
    go(fn)


def test_buyer_application_rejected_with_reason_and_team_link_kept(go):
    async def fn(b):
        await ready(b)
        await review_on()
        async with models.Session() as s:  # a working team: its link counts even before the entry is approved
            from bot.models import Team
            s.add(Team(leader_id=SELLER, name="Альфа", status="approved"))
            await s.commit()
        await b.run(msg(NEW2, "/start t1"))
        assert (await user(NEW2)).team_id == 1
        await b.run(cb(NEW2, "su:r:buyer"), msg(NEW2, "около 50 тысяч"), cb(NEW2, "su:go"))
        async with models.Session() as s:
            su = await s.scalar(select(Signup))
            assert (su.role, su.turnover, su.proof) == ("buyer", "около 50 тысяч", None)
        assert any("Заявка на вход #1" in plain(t) for t in b.session.texts(ADMIN))  # no log forum: admins' chats
        await b.run(cb(ADMIN, "a"))
        assert "asu" in b.session.buttons(ADMIN)  # «Заявки на вход (1)» on the dashboard
        await b.run(cb(ADMIN, "asu"), cb(ADMIN, "asu:1"), cb(ADMIN, "asu:rs:1"), msg(ADMIN, "нет"),
                    msg(ADMIN, "мало информации о себе"))
        assert (await user(NEW2)).access == "rejected"
        text = plain(b.session.last(NEW2))
        assert "отклонена" in text and "мало информации о себе" in text
        await b.run(msg(NEW2, "/start"))
        assert "Новую заявку можно подать после" in plain(b.session.last(NEW2)) and "su:new" not in b.session.buttons(NEW2)
        async with models.Session() as s:
            (await s.scalar(select(Signup))).decided_at -= timedelta(hours=25)
            await s.commit()
        await b.run(msg(NEW2, "/start"), cb(NEW2, "su:new"))
        assert "su:r:buyer" in b.session.buttons(NEW2)
    go(fn)


def test_entry_is_open_when_review_is_off(go):
    async def fn(b):
        await b.run(msg(NEW, "/start"))
        assert (await user(NEW)).access == "approved"  # nobody waits and nobody is locked out later
        await review_on()
        await b.run(cb(NEW, "w"))
        assert "Кошелёк" in plain(b.session.last(NEW))
    go(fn)


def test_messages_through_the_bot_with_reply_and_no_links(go):
    async def fn(b):
        await ready(b)
        d = await create_deal(b)
        await b.run(cb(BUYER, f"dl:{d.id}"))
        assert f"dmc:{d.id}" in b.session.buttons(BUYER)
        await b.run(cb(BUYER, f"dmc:{d.id}"))
        assert f"dm:{d.id}:{SELLER}" in b.session.buttons(BUYER) and f"dm:{d.id}:{ADMIN}" in b.session.buttons(BUYER)
        await b.run(cb(BUYER, f"dm:{d.id}:{SELLER}"), msg(BUYER, "Перевёл, проверьте пожалуйста"))
        got = plain(b.session.last(SELLER))
        assert "От: Покупатель" in got and "Перевёл, проверьте" in got and f"dm:{d.id}:{BUYER}" in b.session.buttons(SELLER)
        assert "Сообщение доставлено" in plain(b.session.last(BUYER))
        await b.run(cb(SELLER, f"dm:{d.id}:{BUYER}"), msg(SELLER, "Пришло, подтверждаю"))
        assert "От: Мерчант" in plain(b.session.last(BUYER))

        n = len(b.session.texts(SELLER))
        for bad in ("пиши мне в @ivan_pay", "мой сайт pay-fast.ru", "https://t.me/+abc"):
            await b.run(cb(BUYER, f"dm:{d.id}:{SELLER}"), msg(BUYER, bad))
            assert "ссылки и @юзернеймы запрещены" in plain(b.session.last(BUYER))
        assert len(b.session.texts(SELLER)) == n  # none of them reached the merchant
        async with models.Session() as s:
            assert await s.scalar(select(Event.id).where(Event.kind == "message_blocked", Event.alert))

        await b.run(cb(BUYER, f"dm:{d.id}:{BUYER}"))
        assert any("самому себе" in a for a in b.session.alerts())
        await b.run(msg(30, "/start"), cb(30, f"dm:{d.id}:{SELLER}"))  # not in the deal
        assert any("не участвует в сделке" in a for a in b.session.alerts())

        await b.run(cb(ADMIN, f"dm:{d.id}:{SELLER}"), msg(ADMIN, "Детали тут: https://straitpay.best/docs"))
        assert "От: Администрация" in plain(b.session.last(SELLER))  # admins may send links
        async with models.Session() as s:
            history = [e.text for e in (await s.scalars(select(Event).where(
                Event.ref == f"deal:{d.id}", Event.kind == "message"))).all()]
        assert history[0].startswith("Покупатель → Мерчант: Перевёл")
    go(fn)


def test_admin_controls_a_deal_amount_time_requisites(go):
    async def fn(b):
        await ready(b)
        d = await create_deal(b)  # 10 000 ₽, 95 USDT frozen
        await b.run(msg(ADMIN, f"/deal {d.id}"))
        card = plain(b.session.last(ADMIN))
        for part in (f"Сделка #{d.id}", f"Создал (покупатель): @u{BUYER}", f"Принял (мерчант): @u{SELLER}",
                     "Реквизиты", "История"):
            assert part in card, part
        await b.run(cb(ADMIN, f"adm:amt:{d.id}"), msg(ADMIN, "12000"))
        d = await deal(d.id)
        assert (d.amount_rub, d.seller_debit, d.buyer_credit) == (D(12000), D(114), D("112.8"))
        assert (await user(SELLER)).frozen == D(114)
        assert "изменила сумму сделки" in plain(b.session.last(BUYER))
        await b.run(cb(ADMIN, f"adm:amt:{d.id}"), msg(ADMIN, "99000"))  # the seller has no USDT for it
        assert "не хватает свободных USDT" in plain(b.session.last(ADMIN)) and (await deal(d.id)).amount_rub == 12000
        before = (await deal(d.id)).expires_at
        await b.run(cb(ADMIN, f"adm:ext:{d.id}"))
        assert (await deal(d.id)).expires_at - before == timedelta(minutes=15)
        await b.run(cb(ADMIN, f"ar:{d.id}:c"), cb(ADMIN, f"ar2:{d.id}:c"))
        assert (await deal(d.id)).status == "void" and (await user(SELLER)).frozen == 0

        await merchant(b, 40)
        r = await request(b)
        await b.run(cb(40, f"orq:take:{r.id}:w"))
        assert (await user(40)).frozen == D(500)
        await b.run(cb(ADMIN, f"adv:{r.id}"), cb(ADMIN, f"adm:give:{r.id}"))
        r = await deal(r.id)
        assert (r.status, r.operator_id, r.seller_id, r.via_bybit) == ("checking", ADMIN, None, True)
        assert (await user(40)).frozen == 0 and "забрала администрация" in plain(b.session.last(40))
        await give(b, ADMIN, r.id)
        assert (await deal(r.id)).status == "waiting_payment" and "5536913812345672" in plain(b.session.last(BUYER))
        await b.run(cb(BUYER, f"dl:rc:{r.id}"), msg(BUYER, document=PDF), cb(ADMIN, f"dl:ok2:{r.id}"))
        assert (await deal(r.id)).status == "completed" and (await user(BUYER)).balance == D("488.8")
        async with models.Session() as s:
            op = await s.get(Operator, ADMIN)
            assert op is None or op.debt == 0  # the admin's own requisites: no Bybit order, no debt
    go(fn)


def test_help_in_chats_and_guides(go):
    async def fn(b):
        await ready(b)
        await b.run(group_msg(BUYER, -100123, "/help"))
        reply = b.session.last(-100123)
        assert 'href="https://straitpay.best/docs/buy"' in reply and 'href="https://straitpay.best/docs/team"' in reply
        assert "Кто такой тимлид" in plain(reply)
        await b.run(cb(BUYER, "info"))
        text = b.session.last(BUYER)
        assert 'href="https://straitpay.best/docs/orders"' in text and "https://straitpay.best/docs/help" in \
            b.session.buttons(BUYER)
    go(fn)

