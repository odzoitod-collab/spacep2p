"""Unread chat messages in the mini app (counted on the server per user) and the bot's notifications that open the
deal right in the app."""
from bot.emoji import back, btn, kb
from bot.ui import with_app
from tests.harness import cb, msg
from tests.test_api import http
from tests.test_scenarios import ADMIN, BUYER, PDF, SELLER, create_deal, ready
from tests.test_webapp import as_


def test_unread_messages_are_counted_until_the_chat_is_open(go):
    async def fn(b):
        await ready(b)
        d = await create_deal(b)
        async with http(b) as c:
            say = lambda who, text: c.post(f"/app/api/deals/{d.id}/chat", headers=as_(who), json={"text": text})  # noqa: E731
            assert (await say(BUYER, "Перевёл")).status == 200
            assert (await say(BUYER, "Проверьте, пожалуйста")).status == 200
            u = await (await c.get("/app/api/chat/unread", headers=as_(SELLER))).json()
            assert u == {"total": 2, "deals": {str(d.id): 2}}
            mine = await (await c.get("/app/api/chat/unread", headers=as_(BUYER))).json()
            assert mine["total"] == 0  # one's own messages are never unread
            listed = await (await c.get("/app/api/deals?scope=active", headers=as_(SELLER))).json()
            assert listed["deals"][0]["unread"] == 2
            me = await (await c.get("/app/api/me", headers=as_(SELLER))).json()
            assert me["counts"]["unread"] == 2
            peek = await c.get(f"/app/api/deals/{d.id}/chat", headers=as_(SELLER))  # a preview does not read
            assert peek.status == 200
            assert (await (await c.get("/app/api/chat/unread", headers=as_(SELLER))).json())["total"] == 2
            await c.get(f"/app/api/deals/{d.id}/chat?read=1", headers=as_(SELLER))  # the chat is open
            assert (await (await c.get("/app/api/chat/unread", headers=as_(SELLER))).json())["total"] == 0
            await say(SELLER, "Вижу, подтверждаю")  # writing reads too, and the other side gets one unread
            assert (await (await c.get("/app/api/chat/unread", headers=as_(SELLER))).json())["total"] == 0
            assert (await (await c.get("/app/api/chat/unread", headers=as_(BUYER))).json())["total"] == 1
        await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, document=PDF), cb(SELLER, f"dl:ok2:{d.id}"))
        async with http(b) as c:  # a closed deal is no longer counted
            assert (await (await c.get("/app/api/chat/unread", headers=as_(BUYER))).json())["total"] == 0
    go(fn)


def test_deal_notifications_open_the_deal_in_the_app(go):
    async def fn(b):
        await ready(b)
        app = lambda m: [x.web_app.url for r in m.inline_keyboard for x in r if x.web_app]  # noqa: E731
        m = with_app(BUYER, "⚠️ Покупатель отменил заявку #7.", kb(btn("Мои сделки", "deals"), back("x", "Скрыть")))
        assert app(m) == ["https://straitpay.best/app?p=deal/7"]
        assert m.inline_keyboard[-1][0].callback_data == "x"  # «Скрыть» stays last, alone
        offer = with_app(BUYER, "Новая <b>заявка #9</b> на 5 000 ₽", kb(btn("Взять", "orq:take:9:B")))
        assert app(offer) == ["https://straitpay.best/app?p=request/9"]
        assert not app(with_app(-100500, "Сделка #7", kb(btn("x", "x"))))  # a group: no web_app buttons there
        assert not app(with_app(BUYER, "Баланс пополнен", kb(btn("x", "x"))))  # nothing about a deal
        d = await create_deal(b)  # a real notification: the seller's new deal
        sent = [x for x in b.session.calls if type(x).__name__ == "SendMessage" and x.chat_id == SELLER
                and f"#{d.id}" in (x.text or "")]
        assert sent and any(f"p=deal/{d.id}" in u for u in app(sent[-1].reply_markup))
        await b.run(cb(ADMIN, "a"))  # screens are not notifications: nothing added there
    go(fn)


def test_statuses_say_exactly_where_an_order_is():
    """Never «ищем реквизиты» once a merchant took the request, never «ищем мерчанта» once the order is given."""
    from decimal import Decimal as D

    from bot.handlers.deal import status_of
    from bot.models import Deal
    B, M, OP = 1, 2, 3
    d = Deal(id=5, buyer_id=B, status="searching", is_order=True, via_bybit=False, amount_rub=D(1000))
    assert status_of(d, B)[1] == "Ищем мерчанта под вашу сумму"
    d.status, d.seller_id = "assigned", M  # taken, from the balance
    assert status_of(d, B)[1] == "Мерчант взял заявку, готовит реквизиты" and status_of(d, M)[1] == "Вы взяли — выдайте реквизиты"
    d.via_bybit = True  # taken through a Bybit order, no link yet
    assert status_of(d, B)[1] == "Мерчант взял, создаёт Bybit-ордер" and status_of(d, M)[1] == "Пришлите ссылку на Bybit-ордер"
    d.status, d.bybit_url = "checking", "https://www.bybit.com/x"  # the order is given, no operator yet
    assert status_of(d, B)[1] == "Мерчант дал ордер — ждём оператора" and status_of(d)[1] == "Ордер получен — ждёт оператора"
    d.operator_id = OP  # an operator took it
    assert status_of(d, OP)[1] == "Выдайте реквизиты из ордера" and status_of(d, B)[1] == "Оператор принял ордер, выдаёт реквизиты"
    d.status = "waiting_payment"
    assert status_of(d, B)[1] == "Переведите и прикрепите чек" and status_of(d, M)[1] == "Реквизиты выданы — ждём перевод"
    d.status = "paid"
    assert status_of(d, OP)[1] == "Чек пришёл — проверьте поступление" and status_of(d, B)[1] == "Чек отправлен — оператор проверяет"
    d.status, d.close_reason = "cancelled", "no_merchant"
    assert status_of(d, B)[1] == "Закрыта: мерчант не нашёлся"
