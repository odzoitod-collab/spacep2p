"""Tests for merchant insurance deposit (1500 USDT, BEP-20 address generation)."""
from decimal import Decimal as D

from bot import models
from tests.harness import cb, msg, plain
from tests.test_scenarios import BUYER, SELLER, ready


def test_seller_deposit_screen_on_sl_button(go):
    async def fn(b):
        await b.run(msg(SELLER, "/start"))
        await b.run(cb(SELLER, "sl"))
        text = plain(b.session.last(SELLER))
        assert "Страховой депозит" in text
        assert "1 500 USDT" in text or "1500" in text
        assert "BEP-20" in text
        assert "sl:dep_pay" in b.session.buttons(SELLER)
    go(fn)


def test_seller_deposit_pay_button_generates_bep20_address(go):
    async def fn(b):
        await b.run(msg(SELLER, "/start"))
        await b.run(cb(SELLER, "sl"))
        await b.run(cb(SELLER, "sl:dep_pay"))
        text = plain(b.session.last(SELLER))
        assert "Оплата страхового депозита" in text
        assert "1 500 USDT" in text
        assert "0x" in text
    go(fn)


def test_user_typing_pay_message_generates_bep20_address(go):
    async def fn(b):
        await b.run(msg(SELLER, "/start"))
        await b.run(cb(SELLER, "sl"))
        # User writes "оплатить" in chat
        await b.run(msg(SELLER, "оплатить"))
        text = plain(b.session.last(SELLER))
        assert "Оплата страхового депозита" in text
        assert "1 500 USDT" in text
        assert "0x" in text
    go(fn)


def test_user_typing_sell_trigger(go):
    async def fn(b):
        await b.run(msg(SELLER, "/start"))
        # User writes "продать юсдт в руб"
        await b.run(msg(SELLER, "продать юсдт в руб"))
        text = plain(b.session.last(SELLER))
        assert "Страховой депозит" in text
        assert "1 500 USDT" in text
    go(fn)


def test_cmd_sell_shows_deposit_for_new_seller(go):
    async def fn(b):
        await b.run(msg(SELLER, "/start"))
        await b.run(msg(SELLER, "/sell"))
        text = plain(b.session.last(SELLER))
        assert "Страховой депозит" in text
        assert "1 500 USDT" in text
    go(fn)


def test_admin_toggle_insurance_deposit(go):
    async def fn(b):
        from bot.services import settings
        async with models.Session() as s:
            await settings.put(s, "insurance_deposit_enabled", "0")
            await s.commit()
        await b.run(msg(SELLER, "/start"))
        await b.run(cb(SELLER, "sl"))
        text = plain(b.session.last(SELLER))
        assert "USDT ⇄ RUB · панель мерчанта" in text
        assert "sl:create_deal" in b.session.buttons(SELLER)
        # /sell command also goes directly to seller menu
        await b.run(msg(SELLER, "/sell"))
        assert "USDT ⇄ RUB · панель мерчанта" in plain(b.session.last(SELLER))
    go(fn)


def test_admin_toggle_chat_hides_it_completely(go):
    async def fn(b):
        from bot.handlers import community
        from bot.services import settings
        async with models.Session() as s:
            await settings.put(s, "chat_id", "-100123456789")
            await settings.put(s, "chat_enabled", "0")
            await s.commit()
        await b.run(msg(BUYER, "/start"))
        # Chat button should NOT be in main menu
        assert "chat" not in b.session.buttons(BUYER)

        # Community missing check should not require chat
        async with models.Session() as s:
            u = await s.get(models.User, BUYER)
            assert community.chat_id() is None
            assert "chat" not in community.missing(u)
    go(fn)


def test_seller_creates_sell_deal_flow(go):
    async def fn(b):
        from sqlalchemy import select
        await b.run(msg(SELLER, "/start"))
        # Seller enters deposit screen
        await b.run(cb(SELLER, "sl"))
        assert "sl:create_deal" in b.session.buttons(SELLER)

        # Click create deal
        await b.run(cb(SELLER, "sl:create_deal"))
        text1 = plain(b.session.last(SELLER))
        assert "шаг 1 из 5" in text1
        assert "Сделка на продажу USDT" in text1

        # Enter amount 500 USDT
        await b.run(msg(SELLER, "500"))
        text2 = plain(b.session.last(SELLER))
        assert "шаг 2 из 5" in text2
        assert "500.00 USDT" in text2 or "500" in text2
        assert "sl:csd_b:0" in b.session.buttons(SELLER)

        # Select Sberbank (bank 0)
        await b.run(cb(SELLER, "sl:csd_b:0"))
        text3 = plain(b.session.last(SELLER))
        assert "шаг 3 из 5" in text3
        assert "Сбербанк" in text3

        # Enter valid card number
        await b.run(msg(SELLER, "4111 1111 1111 1111"))
        text4 = plain(b.session.last(SELLER))
        assert "шаг 4 из 5" in text4

        # Enter holder name
        await b.run(msg(SELLER, "Иванов Иван"))
        text5 = plain(b.session.last(SELLER))
        assert "Проверьте данные сделки" in text5
        assert "sl:csd_save" in b.session.buttons(SELLER)

        # Confirm and publish deal
        await b.run(cb(SELLER, "sl:csd_save"))
        text6 = plain(b.session.last(SELLER))
        assert "Сделка на продажу опубликована" in text6
        assert "Вы на смене" in text6

        # Check in DB that card exists and seller is online
        async with models.Session() as s:
            u = await s.get(models.User, SELLER)
            assert u.is_online is True
            cards = (await s.scalars(select(models.Card).where(models.Card.user_id == SELLER))).all()
            assert len(cards) == 1
            assert cards[0].bank == "Сбербанк"
            assert cards[0].requisites == "4111111111111111"
            assert cards[0].holder == "Иванов Иван"
            assert cards[0].is_active is True
            assert cards[0].max_rub > 0
    go(fn)


def test_admin_panel_toggle_buttons(go):
    async def fn(b):
        from bot.services import settings
        from tests.test_scenarios import ADMIN
        await b.run(msg(ADMIN, "/start"))
        await b.run(cb(ADMIN, "a:more"))
        # Check toggles in admin keyboard
        btns = b.session.buttons(ADMIN)
        dep_btn = next((x for x in btns if x and x.startswith("as:tg:dep:")), None)
        chat_btn = next((x for x in btns if x and x.startswith("as:tg:chat:")), None)
        assert dep_btn is not None
        assert chat_btn is not None

        # Toggle deposit off
        await b.run(cb(ADMIN, dep_btn))
        async with models.Session() as s:
            assert settings.get("insurance_deposit_enabled") == "0"

        # Toggle chat off
        await b.run(cb(ADMIN, chat_btn))
        async with models.Session() as s:
            assert settings.get("chat_enabled") == "0"
    go(fn)

