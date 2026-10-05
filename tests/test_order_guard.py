"""Order requisites guard: a Bybit-order take counts only with the link (order_link_minutes), an operator says when
the merchant's order had no requisites, strike_limit misses in a row put the merchant on a pause; every operator
action is a post in the admin chat."""
from datetime import timedelta
from decimal import Decimal as D

from sqlalchemy import update

from bot import models, tasks
from bot.models import Deal, OrderMerchant
from bot.services import settings
from tests.harness import cb, msg, plain
from tests.test_orders import LINK, M1, OP, deal, give, merchant, offers, request
from tests.test_scenarios import BUYER, ready


async def link_given(b, d, link=LINK):
    await b.run(cb(M1, f"orq:take:{d.id}:b"), msg(M1, link))
    return await deal(d.id)


async def past(did):
    async with models.Session() as s:
        await s.execute(update(Deal).where(Deal.id == did).values(expires_at=models.now() - timedelta(seconds=1)))
        await s.commit()


async def om():
    async with models.Session() as s:
        return await s.get(OrderMerchant, M1)


def test_no_link_in_time_means_not_taken(go):
    async def fn(b):
        await ready(b)
        await merchant(b, M1, balance=D(0))
        d = await request(b)
        before = plain(b.session.last(BUYER))
        await b.run(cb(M1, f"orq:take:{d.id}:b"))
        taken = await deal(d.id)
        assert taken.status == "assigned"
        assert timedelta(minutes=1) < taken.expires_at.replace(tzinfo=models.now().tzinfo) - models.now() \
            <= timedelta(minutes=2)  # order_link_minutes
        assert "Ссылка — до" in plain(b.session.last(M1))
        assert plain(b.session.last(BUYER)) == before  # the buyer is not told about a take without a link
        await past(d.id)
        await tasks.order_timeouts(b.bot)
        d = await deal(d.id)
        assert (d.status, d.seller_id) == ("searching", None)
        assert "не засчитана" in plain(b.session.last(M1))
        assert (await om()).strikes == 0  # no link is no miss: only an order without requisites is
    go(fn)


def test_three_orders_without_requisites_in_a_row_put_the_merchant_to_sleep(go):
    async def fn(b):
        await ready(b)
        await merchant(b, M1, balance=D(0))
        for n in range(1, 4):
            d = await link_given(b, await request(b), f"{LINK}{n}")
            await b.run(cb(OP, f"opq:go:{d.id}"), cb(OP, f"opq:pr:{d.id}"))
            assert "Мерчант дал реквизиты в ордере?" in plain(b.session.last(OP))
            await b.run(cb(OP, f"opq:nr:{d.id}"))
            d = await deal(d.id)
            assert (d.status, d.seller_id, d.operator_id) == ("searching", None, None)  # on to the other merchants
            m = await om()
            if n < 3:
                assert m.strikes == n and m.sleep_until is None
                assert f"Пропуск {n} из 3" in plain(b.session.last(M1))
            await b.run(cb(BUYER, f"orb:cn:{d.id}"))
        m = await om()
        assert m.strikes == 0 and m.sleep_until is not None
        assert "пауза до" in plain(b.session.last(M1))
        seen = len(offers(b, M1))
        d = await request(b)
        assert len(offers(b, M1)) == seen  # asleep: no requests come
        await b.run(cb(M1, f"orq:take:{d.id}:b"))
        assert any("пауза до" in a for a in b.session.alerts()) and (await deal(d.id)).status == "searching"
        await b.run(cb(M1, "om"))
        assert "Пауза до" in plain(b.session.last(M1))

        await b.run(cb(OP, f"aom:{M1}"), cb(OP, f"aom:wake:{M1}"))  # the admin lifts it
        assert (await om()).sleep_until is None and "сняла паузу" in plain(b.session.last(M1))
    go(fn)


def test_requisites_given_reset_the_misses(go):
    async def fn(b):
        await ready(b)
        await merchant(b, M1, balance=D(0))
        d = await link_given(b, await request(b), f"{LINK}a")
        await b.run(cb(OP, f"opq:go:{d.id}"), cb(OP, f"opq:nr:{d.id}"), cb(BUYER, f"orb:cn:{d.id}"))
        assert (await om()).strikes == 1
        d = await link_given(b, await request(b), f"{LINK}b")
        await b.run(cb(OP, f"opq:go:{d.id}"))
        await give(b, OP, d.id)
        assert (await deal(d.id)).status == "waiting_payment" and (await om()).strikes == 0
    go(fn)


def test_after_a_timeout_the_operator_is_asked_who_failed(go):
    async def fn(b):
        await ready(b)
        await merchant(b, M1, balance=D(0))
        d = await link_given(b, await request(b))
        await b.run(cb(OP, f"opq:go:{d.id}"))
        await past(d.id)
        await tasks.order_timeouts(b.bot)
        assert (await deal(d.id)).status == "cancelled"
        assert "Мерчант дал реквизиты в своём ордере?" in plain(b.session.last(OP))
        assert {f"opq:ans:{d.id}:0", f"opq:ans:{d.id}:1"} <= set(b.session.buttons(OP))
        await b.run(cb(OP, f"opq:ans:{d.id}:0"), cb(OP, f"opq:ans:{d.id}:0"))  # the second tap changes nothing
        assert (await om()).strikes == 1 and any("уже записан" in a for a in b.session.alerts())
    go(fn)


def test_operator_actions_are_posted_to_the_admin_chat(go):
    async def fn(b):
        await ready(b)
        await merchant(b, M1, balance=D(0))
        group = -100901
        b.session.forums[group] = set()
        async with models.Session() as s:
            await settings.put(s, "log_chat", str(group))
            await s.commit()
        d = await link_given(b, await request(b))
        await b.run(cb(OP, f"opq:go:{d.id}"))
        await give(b, OP, d.id)
        await b.deliver()
        posts = [plain(m.text) for m in b.session.calls if type(m).__name__ == "SendMessage" and m.chat_id == group
                 and m.message_thread_id and "Оператор" in (m.text or "")]
        accepted = next(p for p in posts if p.startswith("✅ Оператор принял ордер"))
        assert f"Заявка: #{d.id} · 52 000 ₽ · 500 USDT" in accepted and f"Мерчант: @u{M1}" in accepted
        assert any(p.startswith("🔑 Оператор выдал реквизиты") for p in posts)
    go(fn)


async def rate_all(merchant, score, n=10, first=1000):
    from bot.models import MerchantRating
    async with models.Session() as s:
        for i in range(n):
            s.add(MerchantRating(deal_id=first + i, merchant_id=merchant, operator_id=OP, score=score))
        await s.commit()


def test_operator_rates_the_merchant_once(go):
    async def fn(b):
        await ready(b)
        await merchant(b, M1, balance=D(0))
        d = await link_given(b, await request(b))
        await b.run(cb(OP, f"opq:go:{d.id}"))
        await give(b, OP, d.id)
        assert "Оцените мерчанта" in plain(b.session.last(OP))
        rate = next(x for x in b.session.buttons(OP) if x and x.startswith("opr:") and x.endswith(":8"))
        await b.run(cb(OP, rate), cb(OP, rate.replace(":8", ":2")))
        assert "Оценка уже стоит: 8 из 10" in b.session.alerts()[-1]
        assert "пока нет (1 из 10 оценок)" in plain(b.session.last(OP))
        await b.run(cb(M1, "om"))
        assert "Репутация: пока нет (1 из 10 оценок)" in plain(b.session.last(M1))
    go(fn)


def test_another_merchant_without_a_miss_and_no_way_back(go):
    async def fn(b):
        await ready(b)
        await merchant(b, M1, balance=D(0))
        d = await link_given(b, await request(b))
        await b.run(cb(OP, f"opq:go:{d.id}"), cb(OP, f"opq:pr:{d.id}"), cb(OP, f"opq:nm:{d.id}"))
        assert (await deal(d.id)).status == "searching" and (await om()).strikes == 0
        assert "передал заявку" in plain(b.session.last(M1))
        assert any(x and x.startswith("opr:") for x in b.session.buttons(OP))  # he still scores the merchant
        await b.run(cb(M1, f"orq:take:{d.id}:b"))
        assert any("уже работали с этой заявкой" in a for a in b.session.alerts())
        assert (await deal(d.id)).status == "searching"
    go(fn)


def test_reputation_limits_bybit_orders(go):
    async def fn(b):
        await ready(b)
        await merchant(b, M1, balance=D(1000))
        await rate_all(M1, 3)  # low: only with the balance
        d = await request(b, "10400")
        assert f"orq:take:{d.id}:b" not in b.session.buttons(M1) and f"orq:take:{d.id}:w" in b.session.buttons(M1)
        assert "Bybit-ордер недоступен: репутация 3" in plain(offers(b, M1)[-1])
        await b.run(cb(M1, f"orq:take:{d.id}:b"))
        assert any("только с баланса" in a for a in b.session.alerts())
        await b.run(cb(BUYER, f"orb:cn:{d.id}"))
        await rate_all(M1, 9, n=30, first=2000)  # the latest 30 scores count: now 9
        d = await request(b, "52000")
        assert f"orq:take:{d.id}:b" in b.session.buttons(M1)
    go(fn)


def test_middle_reputation_caps_the_bybit_amount(go):
    async def fn(b):
        await ready(b)
        await merchant(b, M1, balance=D(0))
        await rate_all(M1, 6)
        big = await request(b, "52000")
        assert not offers(b, M1) or f"orq:take:{big.id}:b" not in b.session.buttons(M1)
        await b.run(cb(M1, f"orq:take:{big.id}:b"))
        assert any("до 30 000 ₽" in a for a in b.session.alerts())
        await b.run(cb(BUYER, f"orb:cn:{big.id}"))
        small = await request(b, "20800")
        await b.run(cb(M1, f"orq:take:{small.id}:b"))
        assert (await deal(small.id)).status == "assigned"
    go(fn)


def test_an_operator_without_an_entry_application_takes_orders_and_confirms(go):
    """The bug: an operator added in the panel who never passed the entry application had every button of his
    swallowed by the entry gate — «Принять ордер» did nothing, only an admin could confirm the payment."""
    from bot.services import settings
    from tests.test_scenarios import OTHER, PDF

    async def fn(b):
        await ready(b)
        await merchant(b, M1, balance=D(0))
        async with models.Session() as s:
            await settings.put(s, "signup_review", "1")
            await s.commit()
        await b.run(msg(OTHER, "/start"))  # a new user: the entry application screen
        from bot.models import Operator, User
        async with models.Session() as s:  # an operator from before: his entry application never decided
            s.add(Operator(user_id=OTHER, active=True, debt=D(0)))
            assert (await s.get(User, OTHER)).access != "approved"
            await s.commit()
        d = await link_given(b, await request(b))
        assert f"opq:go:{d.id}" in b.session.buttons(OTHER)
        await b.run(cb(OTHER, f"opq:go:{d.id}"))
        assert (await deal(d.id)).operator_id == OTHER
        await give(b, OTHER, d.id)
        assert (await deal(d.id)).status == "waiting_payment"
        await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, document=PDF))
        assert f"dl:ok:{d.id}" in b.session.buttons(OTHER)  # the receipt with «Подтвердить» comes to him
        await b.run(cb(OTHER, f"dl:ok:{d.id}"), cb(OTHER, f"dl:ok2:{d.id}"))
        assert (await deal(d.id)).status == "completed"
    go(fn)


def test_the_deal_chat_reaches_everyone_and_keeps_links_out(go):
    from sqlalchemy import select

    from bot.models import DealMessage, Event
    from tests.test_scenarios import ADMIN

    async def fn(b):
        await ready(b)
        await merchant(b, M1, balance=D(0))
        d = await link_given(b, await request(b))
        await b.run(cb(OP, f"opq:go:{d.id}"))
        await give(b, OP, d.id)
        await b.run(cb(BUYER, f"dl:{d.id}"), cb(BUYER, f"dch:{d.id}"))
        assert "Чат сделки" in plain(b.session.last(BUYER)) and "Покупатель · Мерчант · Оператор" in plain(
            b.session.last(BUYER))
        await b.run(msg(BUYER, "Перевёл с Т-Банка"))
        for uid in (M1, OP):  # the merchant and the operator both get it, with a way back into the chat
            assert "Перевёл с Т-Банка" in plain(b.session.last(uid)) and f"dch:{d.id}" in b.session.buttons(uid)
        assert "Перевёл с Т-Банка" in plain(b.session.last(BUYER))  # in his chat screen too
        await b.run(cb(OP, f"dch:{d.id}"), msg(OP, "Вижу, проверяю"), msg(OP, "Пришло"))
        assert "Пришло" in plain(b.session.last(BUYER)) and "Оператор" in plain(b.session.last(BUYER))
        before = len(b.session.texts(M1))
        for bad in ("пиши в @ivan_pay", "мой сайт pay-fast.ru", "https://t.me/+abc"):
            await b.run(cb(BUYER, f"dch:{d.id}"), msg(BUYER, bad))
            assert "ссылки и @юзернеймы запрещены" in plain(b.session.last(BUYER))
        assert len(b.session.texts(M1)) == before  # none got through
        await b.run(msg(30, "/start"), cb(30, f"dch:{d.id}"))
        assert "Чат недоступен" in b.session.alerts()[-1]  # strangers stay out
        await b.run(cb(ADMIN, f"dch:{d.id}"), msg(ADMIN, "Инструкция: https://straitpay.best/docs"))
        assert "straitpay.best" in plain(b.session.last(BUYER))  # the administration may send links
        async with models.Session() as s:
            assert len((await s.scalars(select(DealMessage).where(DealMessage.deal_id == d.id))).all()) == 4
            assert await s.scalar(select(Event.id).where(Event.kind == "message_blocked", Event.alert))
    go(fn)


def test_leaving_the_chat_by_a_button_ends_the_chat_mode(go):
    from sqlalchemy import func, select

    from bot.models import DealMessage
    from tests.test_scenarios import create_deal

    async def fn(b):
        await ready(b)
        d = await create_deal(b)
        await b.run(cb(BUYER, f"dch:{d.id}"), msg(BUYER, "первое"), cb(BUYER, f"dl:{d.id}"), msg(BUYER, "второе"))
        async with models.Session() as s:
            assert await s.scalar(select(func.count(DealMessage.id))) == 1  # «второе» is not a chat message
    go(fn)


def test_the_chat_post_follows_the_request_step_by_step(go):
    from bot import tasks
    from bot.services import settings
    from tests.test_scenarios import PDF

    chat = -100700

    async def fn(b):
        await ready(b)
        await merchant(b, M1, balance=D(0))
        async with models.Session() as s:
            await settings.put(s, "chat_id", str(chat))
            await s.commit()

        def post():
            texts = [m for m in b.session.calls if getattr(m, "chat_id", None) == chat
                     and type(m).__name__ in ("SendMessage", "EditMessageText")]
            return plain(texts[-1].text), texts[-1].reply_markup

        d = await request(b)
        text, markup = post()
        assert "Новая заявка" in text and "Ищем мерчанта" in text and markup is not None
        await b.run(cb(M1, f"orq:take:{d.id}:b"))
        text, markup = post()
        assert "✅ Мерчант взял заявку" in text and "⏳ Ссылка на Bybit-ордер получена — ждём ссылку до" in text
        assert markup is None
        await b.run(msg(M1, LINK))
        await tasks.chat_posts(b.bot)
        assert "⏳ Оператор выдал реквизиты — ждём оператора" in post()[0]
        await b.run(cb(OP, f"opq:go:{d.id}"))
        await tasks.chat_posts(b.bot)
        assert "оператор проверяет ордер" in post()[0]
        await give(b, OP, d.id)
        await tasks.chat_posts(b.bot)
        assert "✅ Оператор выдал реквизиты" in post()[0] and "⏳ Покупатель оплатил — ждём перевод и чек" in post()[0]
        await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, document=PDF))
        await tasks.chat_posts(b.bot)
        assert "✅ Покупатель оплатил" in post()[0] and "⏳ Оплата подтверждена — чек на проверке" in post()[0]
        n = len(b.session.calls)
        await tasks.chat_posts(b.bot)  # nothing changed: no edit
        assert not [m for m in b.session.calls[n:] if getattr(m, "chat_id", None) == chat]
        await b.run(cb(OP, f"dl:ok2:{d.id}"))
        await tasks.chat_posts(b.bot)
        text = post()[0]
        assert "Выполнена" in text and "✅ Оплата подтверждена" in text and "⏳" not in text
        assert "4111" not in text and "5536" not in text  # never the requisites in a public chat
    go(fn)
