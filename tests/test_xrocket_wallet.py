"""Wallet through xRocket only (address deposits in any network, withdrawals to any network), API client terms,
community chat (personal invite links, pinned summary), broadcasts, button layout and structured log cards."""
from datetime import datetime
from decimal import Decimal as D

from aiogram.types import Chat, ChatInviteLink, ChatMemberLeft, ChatMemberMember, ChatMemberUpdated, Update
from sqlalchemy import func, select

from bot import models, tasks
from bot.emoji import btn, kb
from bot.handlers import admin_chat
from bot.models import ApiClient, Deposit, Event, Ledger, Withdrawal
from bot.services import money
from tests.harness import cb, msg, plain, tg
from tests.test_api import apply_and_approve, auth, http
from tests.test_scenarios import ADMIN, BUYER, SELLER, create_deal, ready, user

TRX = "T" + "9" * 33
CHAT = -100777


async def fund(uid, amount):
    async with models.Session() as s:
        await money.add(s, uid, D(amount), "deposit", "dep:0")
        await s.commit()


def rows(b, chat):
    """Rows of callback data of the last keyboard shown to `chat`."""
    for m in reversed(b.session.calls):
        if getattr(m, "chat_id", None) == chat and getattr(m, "reply_markup", None):
            return [[bt.callback_data or bt.url for bt in r] for r in m.reply_markup.inline_keyboard]
    return []


async def platform_income():
    async with models.Session() as s:
        return D(await s.scalar(select(func.coalesce(func.sum(Ledger.delta), 0)).where(Ledger.user_id.is_(None))))


def test_deposit_by_address_in_any_network_minus_fee(go):
    async def fn(b):
        await ready(b)
        await b.run(cb(BUYER, "w:in"))
        assert {"w:dep", "w:adr:TON", "w:adr:TRX", "w:adr:ETH"} <= set(b.session.buttons(BUYER))
        assert "1.5%" in plain(b.session.last(BUYER))
        await b.run(cb(BUYER, "w:adr:TRX"))
        text = plain(b.session.last(BUYER))
        assert "TRC-20" in text and "TRXaddr1" in text and "Только USDT" in text
        assert b.rocket.invoices[-1][0] is None and b.rocket.addresses == [("inv1", "TRX")]  # open amount, xRocket address
        await b.run(cb(BUYER, "w:adr:TRX"))  # the live address is shown again, not a new one
        assert len(b.rocket.addresses) == 1
        b.rocket.invoice_status = "paid"
        b.rocket.payments = [{"id": "p1", "status": "paid", "receiveAmount": "200", "receiveCurrency": "USDT"}]
        await tasks.poll_deposits(b.bot)
        assert (await user(BUYER)).balance == D("197")  # 200 − 1.5%
        assert await platform_income() == D("3")
        assert "Баланс пополнен на 197 USDT" in plain(b.session.last(BUYER))
        async with models.Session() as s:
            dep = await s.scalar(select(Deposit).where(Deposit.network == "TRX"))
            assert (dep.status, dep.amount, dep.credit) == ("paid", D(200), D(197))
    go(fn)


def test_withdraw_to_any_network_through_xrocket(go):
    async def fn(b):
        await ready(b)
        await fund(BUYER, 100)
        await b.run(cb(BUYER, "w:out"))
        assert "w:wn:TRX" in b.session.buttons(BUYER) and "w:wd" in b.session.buttons(BUYER)
        await b.run(cb(BUYER, "w:wn:TRX"), msg(BUYER, "UQ-not-a-tron-address"))
        assert "Это не адрес сети TRC-20" in plain(b.session.last(BUYER))
        await b.run(msg(BUYER, TRX))
        assert "шаг 3 из 3" in plain(b.session.last(BUYER))  # no memo step outside TON
        await b.run(msg(BUYER, "10"))
        assert "Придёт: 6.85 USDT" in plain(b.session.last(BUYER))  # 10 − (1.5% + 3 fixed, network 0.1 inside)
        await b.run(cb(BUYER, "w:wn:go"))
        assert b.rocket.withdrawal_calls == [("wd-1", "TRX", TRX, D("6.85"), None)]
        assert (await user(BUYER)).balance == D(90)
        b.rocket.withdrawals["wd-1"]["status"] = "COMPLETED"
        await tasks.sync_chain_withdrawals(b.bot)
        async with models.Session() as s:
            wd = await s.get(Withdrawal, 1)
            assert (wd.status, wd.network, wd.net_fee) == ("done", "TRX", D("0.1"))
        assert await platform_income() == D("3.05")  # 3.15 fee − 0.1 network part paid to xRocket
        assert "Вывод #1 выполнен" in plain(b.session.last(BUYER))
    go(fn)


def test_api_client_has_one_set_of_terms_for_cards_and_orders(go):
    async def fn(b):
        await ready(b)
        token = await apply_and_approve(b, BUYER)
        async with models.Session() as s:
            cl = await s.scalar(select(ApiClient))
        await b.run(cb(ADMIN, f"acl:{cl.id}"))
        assert "10 000 ₽ → клиенту 94 USDT" in plain(b.session.last(ADMIN))
        await b.run(cb(ADMIN, f"acl:t:{cl.id}:pct"), msg(ADMIN, "7"))
        text = plain(b.session.last(ADMIN))
        assert "Сохранено" in text and "клиенту 93 USDT" in text and "карта 2 · ордер 3.15 USDT" in text
        await b.run(cb(ADMIN, f"acl:t:{cl.id}:pct"), msg(ADMIN, "120"))
        assert "Процент от 0 до 99,999" in plain(b.session.last(ADMIN))
        async with http(b) as c:
            rates = await (await c.get("/v1/rates", headers=auth(token))).json()
            assert rates["fee_percent"] == "7" and D(rates["example"]["amount_usdt"]) == D(93)
            order = await (await c.post("/v1/orders", headers=auth(token), json={"amount_rub": "10000"})).json()
        assert (D(order["amount_usdt"]), D(order["fee_percent"]), D(order["rate"])) == (D(93), D(7), D(100))
        async with models.Session() as s:
            d = await s.get(models.Deal, order["id"])
            assert (d.seller_debit, d.buyer_credit, d.platform_fee) == (D(95), D(93), D(2))  # merchant terms unchanged
    go(fn)


def test_chat_gives_personal_one_time_links_and_keeps_a_pinned_summary(go):
    async def fn(b):
        await ready(b)
        await b.run(cb(ADMIN, "ach"))
        assert "Чат не подключён" in plain(b.session.last(ADMIN))
        await b.run(cb(ADMIN, "acx:chat_id"), msg(ADMIN, "12345"))
        assert "начинается с «-»" in plain(b.session.last(ADMIN))
        await b.run(msg(ADMIN, str(CHAT)))
        assert "Права бота: ✅ приглашать, ✅ закреплять" in plain(b.session.last(ADMIN))
        await b.run(msg(BUYER, "/start"))
        assert "chat" in b.session.buttons(BUYER)
        await b.run(cb(BUYER, "chat"))
        link = [m for m in b.session.calls if type(m).__name__ == "CreateChatInviteLink"][-1]
        assert (link.chat_id, link.member_limit, link.name) == (CHAT, 1, str(BUYER))
        assert any(x and x.startswith("https://t.me/+inv") for x in b.session.buttons(BUYER))
        joined = ChatMemberUpdated(
            chat=Chat(id=CHAT, type="supergroup"), from_user=tg(BUYER), date=datetime.now(),
            old_chat_member=ChatMemberLeft(user=tg(BUYER)), new_chat_member=ChatMemberMember(user=tg(BUYER)),
            invite_link=ChatInviteLink(invite_link="https://t.me/+x", creator=tg(123), creates_join_request=False,
                                       is_primary=False, is_revoked=False, name=str(BUYER)))
        await b.run(Update(update_id=1, chat_member=joined))
        async with models.Session() as s:
            assert await s.scalar(select(func.count(Event.id)).where(Event.kind == "chat_join",
                                                                     Event.ref == f"user:{BUYER}")) == 1
        await create_deal(b)
        await tasks.chat_pin(b.bot)
        sent = [m for m in b.session.calls if type(m).__name__ == "SendAnimation" and m.chat_id == CHAT]
        assert len(sent) == 1 and "Курс" in sent[0].caption and "Активных сделок: <b>1</b>" in sent[0].caption
        urls = [x.url for row in sent[0].reply_markup.inline_keyboard for x in row]
        assert "https://t.me/straitpay_bot?start=om" in urls  # the banner pin leads into the bot
        assert [m for m in b.session.calls if type(m).__name__ == "PinChatMessage"]
        await tasks.chat_pin(b.bot)  # the second run edits the pinned banner's caption in place
        assert len([m for m in b.session.calls if type(m).__name__ == "SendAnimation" and m.chat_id == CHAT]) == 1
        assert [m for m in b.session.calls if type(m).__name__ == "EditMessageCaption" and m.chat_id == CHAT]
    go(fn)


def test_broadcast_previews_then_copies_to_everyone(go):
    async def fn(b):
        await ready(b)
        await b.run(cb(ADMIN, "abc"))
        assert "abc:all" in b.session.buttons(ADMIN) and "abc:chat" not in b.session.buttons(ADMIN)  # no chat set
        b.session.blocked.add(SELLER)
        await b.run(cb(ADMIN, "abc:all"), msg(ADMIN, "Новости: курс обновлён"))
        copies = [m for m in b.session.calls if type(m).__name__ == "CopyMessage"]
        assert len(copies) == 1 and copies[0].chat_id == ADMIN  # the preview
        assert "abc:go" in b.session.buttons(ADMIN) and "Кому: Все пользователи · 3" in plain(b.session.last(ADMIN))
        await b.run(cb(ADMIN, "abc:go"))
        assert await admin_chat._task == (2, 1)  # buyer and admin got it, the seller blocked the bot
        assert {m.chat_id for m in b.session.calls if type(m).__name__ == "CopyMessage"} == {ADMIN, BUYER}
        assert "доставлено 2 из 3" in plain(b.session.last(ADMIN))
        await b.run(cb(ADMIN, "abc:go"))
        assert "уже отправлена" in b.session.alerts()[-1]
    go(fn)


def test_toggles_keep_their_place_and_order_section_has_no_buy_buttons(go):
    async def fn(b):
        layout = kb(btn("Мин.", "a"), btn("Макс.", "b"), btn("Режим", "c", wide=True), btn("x", "d"), btn("y", "e"))
        assert [len(r) for r in layout.inline_keyboard] == [1, 1, 1, 2]  # a wide one never pairs up
        await ready(b)
        async with models.Session() as s:
            s.add(models.OrderMerchant(user_id=SELLER, status="approved", source="s", speed="5"))
            await s.commit()
        await b.run(cb(SELLER, "om"))
        kb_rows = rows(b, SELLER)
        assert ["om:pay"] in kb_rows  # no on/off switch and no mode: every request comes, the way is chosen per take
        flat = sum(kb_rows, [])
        assert "om:acc:0" not in flat and "om:mode" not in flat
        assert "orb:new" not in flat and "w" not in flat and "sl:st" not in flat
        assert "На линии: заявки приходят все" in plain(b.session.last(SELLER))
        await b.run(cb(BUYER, "om"))
        assert "orb:new" not in b.session.buttons(BUYER) and "om:apply" in b.session.buttons(BUYER)
    go(fn)


def test_log_card_lists_people_with_usernames_one_fact_per_line(go):
    async def fn(b):
        await ready(b)
        await create_deal(b)
        texts = await b.deliver()
        card = next(t for t in texts if t.startswith("🟡 Сделка #1"))
        assert "/deal 1" in card.split("\n")[0]  # the number to follow the deal by
        lines = card.split("\n")
        assert f"• Создал (покупатель): @u{BUYER} · U{BUYER} · {BUYER}" in lines  # one fact per line, as a bullet
        assert f"• Принял (мерчант): @u{SELLER} · U{SELLER} · {SELLER}" in lines
        assert "• Сумма: 10 000 ₽" in lines and "История" in lines
    go(fn)
