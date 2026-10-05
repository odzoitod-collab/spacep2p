"""Operators added in the admin panel (first «Принять ордер» wins, the debt for accepted orders and its repayment),
teams (application, referral link, team chat, the leader's percent), requests posted in chats with a link into the
bot, personal buyer terms, withdrawal fees."""
from datetime import datetime
from decimal import Decimal as D

from aiogram.types import Chat, Message, Update
from sqlalchemy import select

from bot import models, tasks
from bot.models import Deposit, Ledger, Operator, User
from bot.services import money, operators
from tests.harness import cb, ids, msg, plain, tg
from tests.test_orders import LINK, deal, give, merchant, request
from tests.test_scenarios import ADMIN, BUYER, OTHER, PDF, create_deal, ready, user

OPA, OPB, LEAD, MEMBER = 50, 51, 60, 61
TEAM_CHAT, CHAT = -100500, -100700


def group_msg(uid, chat, text):
    return Update(update_id=next(ids), message=Message(
        message_id=next(ids), date=datetime.now(), chat=Chat(id=chat, type="supergroup", title="group"),
        from_user=tg(uid), text=text))


def urls(m):
    return [x.url for row in (m.reply_markup.inline_keyboard if m.reply_markup else []) for x in row]


def posts(b, chat):
    return [m for m in b.session.calls if type(m).__name__ == "SendMessage" and m.chat_id == chat]


async def add_operator(b, uid):
    await b.run(msg(uid, "/start"), cb(ADMIN, "aopl"), cb(ADMIN, "aop:add"), msg(ADMIN, f"@u{uid}"))
    assert "Оператор добавлен" in plain(b.session.last(ADMIN))
    assert "Вы — оператор Strait Pay" in plain(b.session.last(uid))


def test_operators_from_the_panel_first_accept_wins_and_the_debt_is_repaid(go):
    async def fn(b):
        await ready(b)
        await merchant(b, 40, balance=D(0))
        await add_operator(b, OPA)
        await add_operator(b, OPB)
        async with models.Session() as s:
            assert await operators.ids(s) == [OPA, OPB]  # the admins are no longer operators by default
        d = await request(b)
        await b.run(cb(40, f"orq:take:{d.id}:b"), msg(40, LINK))
        assert f"opq:go:{d.id}" in b.session.buttons(OPA) and f"opq:go:{d.id}" in b.session.buttons(OPB)
        assert not any("Bybit-ордер · заявка" in t for t in b.session.texts(ADMIN))

        await b.run(cb(OPB, f"opq:go:{d.id}"))
        closed = [m for m in b.session.calls if type(m).__name__ == "EditMessageText" and m.chat_id == OPA]
        assert closed and "принял другой оператор" in plain(closed[-1].text)
        await give(b, OPB, d.id)
        await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, document=PDF), cb(OPB, f"dl:ok2:{d.id}"))
        assert (await deal(d.id)).status == "completed"

        await b.run(cb(OPB, "op"))
        text = plain(b.session.last(OPB))
        assert "не погашено: 500 USDT" in text and "op:pay" in b.session.buttons(OPB)
        await b.run(cb(OPB, "op:pay"))
        assert b.rocket.invoices[-1][:2] == (D(500), "dep-1")
        assert "К оплате: 500 USDT" in plain(b.session.last(OPB))
        b.rocket.payments = [{"id": "p", "status": "paid", "receiveAmount": "520", "receiveCurrency": "USDT"}]
        await tasks.poll_deposits(b.bot)
        async with models.Session() as s:
            assert (await s.get(Operator, OPB)).debt == 0
            assert (await s.get(Deposit, 1)).purpose == "debt"
            assert (await s.get(User, OPB)).balance == D(20)  # paid above the debt: to his balance, no fee
        assert "Долг погашен: 520 USDT" in plain(b.session.last(OPB))

        async with models.Session() as s:  # another order later: repaid from the balance this time
            await operators.accrue(s, OPB, D(50), "deal:99")
            await s.commit()
        await b.run(cb(OPB, "op"), cb(OPB, "op:bal"), cb(OPB, "op:bal2"))
        async with models.Session() as s:
            assert (await s.get(Operator, OPB)).debt == D(30) and (await s.get(User, OPB)).balance == 0
            assert await s.scalar(select(Ledger.delta).where(Ledger.kind == "debt_repay")) == D(-20)

        await b.run(cb(ADMIN, f"aop:{OPA}"), cb(ADMIN, f"aop:st:{OPA}:0"))
        async with models.Session() as s:
            assert await operators.ids(s) == [OPB]
        await b.run(cb(ADMIN, "afin"))
        assert "Долг операторов за Bybit-ордера: 30 USDT" in plain(b.session.last(ADMIN))
    go(fn)


def test_team_leader_link_chat_and_one_percent_of_member_deals(go):
    async def fn(b):
        await ready(b)
        await b.run(msg(LEAD, "/start"), cb(LEAD, "tm"), cb(LEAD, "tm:apply"), msg(LEAD, "Альфа"),
                    msg(LEAD, "Опыт два года, приведу 10 человек"))
        assert "Заявка отправлена" in plain(b.session.last(LEAD))
        await b.deliver()  # the application card with the decision right on it
        assert {"atm:ok:1", "atm:no:1", "atm:1"} <= set(b.session.buttons(ADMIN))
        await b.run(cb(ADMIN, "atm:1"), cb(ADMIN, "atm:ok:1"))
        assert "Одобрено · U1" in plain(b.session.last(ADMIN))  # the verdict and who took it
        assert "https://t.me/straitpay_bot?start=t1" in plain(b.session.last(LEAD))

        await b.run(group_msg(OTHER, TEAM_CHAT, "/team"))  # not a leader: refused
        assert "только тимлид" in plain(b.session.last(TEAM_CHAT))
        await b.run(group_msg(LEAD, TEAM_CHAT, "/team"))
        assert "Чат команды «Альфа» подключён" in plain(b.session.last(TEAM_CHAT))

        await b.run(msg(MEMBER, "/start t1"))
        assert "Вы в команде «Альфа»" in plain(b.session.last(MEMBER)) and "tm:chat" in b.session.buttons(MEMBER)
        await b.run(cb(MEMBER, "tm:chat"))
        link = [m for m in b.session.calls if type(m).__name__ == "CreateChatInviteLink"][-1]
        assert (link.chat_id, link.member_limit) == (TEAM_CHAT, 1)
        await b.run(msg(OTHER, "/start"), msg(OTHER, "/start t1"), msg(MEMBER, "/start t1"))
        async with models.Session() as s:
            assert (await s.get(User, MEMBER)).team_id == 1 and (await s.get(User, OTHER)).team_id == 1

        await merchant(b, MEMBER)
        d = await request(b, "10400")
        post = posts(b, TEAM_CHAT)[-1]
        assert f"https://t.me/straitpay_bot?start=o{d.id}_t1" in urls(post) and "10 400 ₽" in plain(post.text)
        await b.run(msg(MEMBER, f"/start o{d.id}_t1"))
        assert f"orq:take:{d.id}:w" in b.session.buttons(MEMBER)
        await b.run(cb(MEMBER, f"orq:take:{d.id}:w"))
        edited = [m for m in b.session.calls if type(m).__name__ == "EditMessageText" and m.chat_id == TEAM_CHAT]
        assert edited and "✅ Мерчант взял заявку" in plain(edited[-1].text)  # the post follows the request
        assert edited[-1].reply_markup is None  # no take button once it is taken
        await give(b, MEMBER, d.id)
        await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, document=PDF), cb(MEMBER, f"dl:ok2:{d.id}"))
        d = await deal(d.id)
        assert (d.status, d.team_id, d.team_fee) == ("completed", 1, D("1.04"))  # 1% of 10 400 ₽ / 100
        leader = await user(LEAD)
        assert (leader.team_balance, leader.balance) == (D("1.04"), 0)  # onto the team balance, not the main one
        await b.run(cb(LEAD, "tm"))
        assert "Командный баланс: 1.04 USDT" in plain(b.session.last(LEAD)) and "tm:out" in b.session.buttons(LEAD)
        await b.run(cb(LEAD, "tm:out"), cb(LEAD, "tm:out"))  # the second tap: nothing left to move
        leader = await user(LEAD)
        assert (leader.team_balance, leader.balance) == (0, D("1.04")) and "пуст" in b.session.alerts()[-1]
        await b.run(cb(ADMIN, f"auh:{LEAD}"))
        assert "Журнал сходится" in plain(b.session.last(ADMIN))
        async with models.Session() as s:
            platform = sum((r.delta for r in (await s.scalars(select(Ledger).where(
                Ledger.user_id.is_(None), Ledger.ref == f"deal:{d.id}"))).all()), D(0))
            assert platform == D("1.2")  # 2.24 fee − 1.04 to the leader
        await b.run(cb(LEAD, "tm"))
        assert "Сегодня: +1.04 USDT" in plain(b.session.last(LEAD)) and "Участников: 2" in plain(b.session.last(LEAD))

        static = await create_deal(b, amount="10000")  # a member's deal with a static card earns nothing: not his
        assert static.team_id is None
    go(fn)


def test_requests_go_to_the_community_chat_and_outsiders_are_asked_to_apply(go):
    async def fn(b):
        await ready(b)
        await merchant(b, 40)
        await b.run(cb(ADMIN, "acx:chat_id"), msg(ADMIN, str(CHAT)))
        d = await request(b)
        post = posts(b, CHAT)[-1]
        assert urls(post) == [f"https://t.me/straitpay_bot?start=o{d.id}"]
        for part in ("Сумма перевода: 52 000 ₽", "Курс площадки для ордера: 104 ₽", "Зайти в ордер на: 500 USDT"):
            assert part in plain(post.text)
        await b.run(msg(OTHER, f"/start o{d.id}"))  # not an order merchant
        assert "Брать заявки могут ордерные мерчанты" in plain(b.session.last(OTHER))
        assert "om" in b.session.buttons(OTHER) and f"orq:take:{d.id}:b" not in b.session.buttons(OTHER)
        await tasks.order_timeouts(b.bot)
        assert len(posts(b, CHAT)) == 1  # posted once, not on every re-send
        await b.run(cb(40, f"orq:take:{d.id}:b"))
        await b.run(msg(OTHER, f"/start o{d.id}"))
        assert "уже взяли" in plain(b.session.last(OTHER))
    go(fn)


def test_personal_buyer_terms_with_a_loss_guard(go):
    async def fn(b):
        await ready(b)
        await b.run(cb(ADMIN, f"auv:{BUYER}"), cb(ADMIN, f"aut:{BUYER}"), cb(ADMIN, f"aut:set:{BUYER}:rate"),
                    msg(ADMIN, "98"), cb(ADMIN, f"aut:set:{BUYER}:pct"), msg(ADMIN, "4"))
        assert "Сохранено" in plain(b.session.last(ADMIN)) and "Ваши условия покупки изменены" in plain(
            b.session.last(BUYER))
        await b.run(cb(BUYER, "buy:0"), msg(BUYER, "10000"))
        assert "Вы получите: 97.95 USDT" in plain(b.session.last(BUYER))  # 10 000 / 98 − 4%
        go_btn = next(x for x in b.session.buttons(BUYER) if x and x.startswith("bgo:"))
        await b.run(cb(BUYER, go_btn))
        assert any("площадка ушла бы в минус" in a for a in b.session.alerts())  # the card gives only 95 USDT
        await b.run(cb(ADMIN, f"aut:set:{BUYER}:rate"), msg(ADMIN, "99"), cb(ADMIN, f"aut:set:{BUYER}:pct"),
                    msg(ADMIN, "6"))
        d = await create_deal(b)
        assert (d.buyer_rate, d.platform_pct, d.buyer_credit, d.seller_debit) == (D(99), D(6), D("94.949494"), D(95))
        await b.run(cb(ADMIN, f"aut:rs:{BUYER}"))
        async with models.Session() as s:
            u = await s.get(User, BUYER)
            assert u.buy_rate is None and u.buy_pct is None
    go(fn)


def test_withdrawal_fee_is_one_and_a_half_percent(go):
    async def fn(b):
        await ready(b)
        async with models.Session() as s:
            await money.add(s, BUYER, D(100), "deposit", "dep:0")
            await s.commit()
        await b.run(cb(BUYER, "w"), cb(BUYER, "w:out"))
        assert "Чек xRocket: мгновенно · 1.5%" in plain(b.session.last(BUYER))
        await b.run(cb(BUYER, "w:wd"), msg(BUYER, "100"))
        assert "Сумма чека: 98.5 USDT" in plain(b.session.last(BUYER))
        await b.run(cb(BUYER, "w:go"))
        assert b.rocket.cheques[-1][0] == D("98.5")
        async with models.Session() as s:
            assert await s.scalar(select(Ledger.delta).where(Ledger.kind == "withdraw_fee")) == D("1.5")
    go(fn)
