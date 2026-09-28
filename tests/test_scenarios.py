"""User journeys and failure modes, run through the real dispatcher on SQLite and (if configured) PostgreSQL."""
import asyncio
from datetime import timedelta
from decimal import Decimal as D

from aiogram.types import Document, PhotoSize
from sqlalchemy import func, select

from bot import models, tasks
from bot.models import Audit, Card, Deal, Ledger, Ticket, User, Withdrawal, now
from bot.services import deals, money, settings, xrocket
from tests.harness import cb, msg, plain

SELLER, BUYER, ADMIN, OTHER = 10, 20, 1, 30
PDF = Document(file_id="pdf1", file_unique_id="u1", mime_type="application/pdf", file_name="check.pdf")


async def ready(b, balance=D(200)):
    """Seller with one card on shift, buyer and admin registered."""
    await b.run(msg(SELLER, "/start"), msg(BUYER, "/start"), msg(ADMIN, "/start"))
    async with models.Session() as s:
        await money.add(s, SELLER, balance, "deposit", "dep:0")  # through the journal, like a real top-up
        await s.commit()
    await b.run(cb(SELLER, "sl"), cb(SELLER, "sl:on:1"), cb(SELLER, "sl:add"), cb(SELLER, "sl:add:card"),
                cb(SELLER, "sl:bank:0"), msg(SELLER, "4111 1111 1111 1111"), msg(SELLER, "Иванов Иван"),
                msg(SELLER, "1000"), msg(SELLER, "50000"), cb(SELLER, "sl:save"))


async def create_deal(b, amount="10000", buyer=BUYER):
    """Buyer picks the card, types an amount and presses the confirm button that the UI showed."""
    await b.run(cb(buyer, "bc:1"), msg(buyer, amount))
    go_btn = next(x for x in b.session.buttons(buyer) if x and x.startswith("bgo:"))
    await b.run(cb(buyer, go_btn))
    async with models.Session() as s:
        return await s.scalar(select(Deal).where(Deal.buyer_id == buyer).order_by(Deal.id.desc()))


async def user(uid):
    async with models.Session() as s:
        return await s.get(User, uid)


async def get_deal(did):
    async with models.Session() as s:
        return await s.get(Deal, did)


async def shift_deal(did, **values):
    async with models.Session() as s:
        d = await s.get(Deal, did)
        for k, v in values.items():
            setattr(d, k, v)
        await s.commit()


# ---------- buyer ----------

def test_first_visit_and_empty_market_explain_next_step(go):
    async def fn(b):
        await b.run(msg(BUYER, "/start"), cb(BUYER, "buy:0"))
        first, market = b.session.texts(BUYER)[-2:]
        assert "RUB ⇄ USDT — купить USDT за рубли" in plain(first) and "USDT ⇄ RUB — продать" in plain(first)
        assert "нет продавцов" in plain(market) and "Например: 10 000 ₽ → 94 USDT" in plain(market)
        await b.run(msg(BUYER, "привет"))  # free text outside any step is not swallowed silently
        assert "Сообщение не распознано" in plain(b.session.last(BUYER))
        await b.run(cb(BUYER, "zz:old"))  # button from a very old message
        assert "Кнопка устарела" in b.session.alerts()[-1]
    go(fn)


def test_buyer_happy_path_with_exact_economics(go):
    async def fn(b):
        await ready(b)
        d = await create_deal(b)
        screen = plain(b.session.last(BUYER))
        assert "4111111111111111" in screen and "Иванов Иван" in screen and "Ровно: 10000 ₽" in screen
        assert "Вы получите: 94 USDT" in screen
        await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, document=PDF))
        assert "Чек отправлен продавцу" in plain(b.session.last(BUYER))
        assert "проверьте поступление" in plain(b.session.last(SELLER))  # document caption to the seller
        await b.run(cb(SELLER, f"dl:ok:{d.id}"), cb(SELLER, f"dl:ok2:{d.id}"), cb(SELLER, f"dl:ok2:{d.id}"))
        s_, b_ = await user(SELLER), await user(BUYER)
        assert (s_.balance, s_.frozen, b_.balance) == (D(105), D(0), D(94))
        await b.run(cb(BUYER, "w:h"))
        assert "Покупка по сделке #1" in plain(b.session.last(BUYER))
    go(fn)


def test_wrong_file_types_keep_deal_waiting(go):
    async def fn(b):
        await ready(b)
        d = await create_deal(b)
        photo = [PhotoSize(file_id="ph", file_unique_id="ph", width=1, height=1)]
        await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, photo=photo))
        assert "Это фото" in plain(b.session.last(BUYER))
        doc = Document(file_id="x", file_unique_id="x", mime_type="image/png", file_name="scan.png")
        await b.run(msg(BUYER, document=doc))
        assert "не PDF" in plain(b.session.last(BUYER))
        big = Document(file_id="b", file_unique_id="b", mime_type="application/pdf", file_size=25 * 2 ** 20)
        await b.run(msg(BUYER, document=big))
        assert "больше 20 МБ" in plain(b.session.last(BUYER))
        assert (await get_deal(d.id)).status == "waiting_payment"
        # octet-stream with .pdf name (some banks) is accepted
        odd = Document(file_id="o", file_unique_id="o", mime_type="application/octet-stream", file_name="Чек.PDF")
        await b.run(msg(BUYER, document=odd))
        assert (await get_deal(d.id)).status == "paid"
    go(fn)


def test_receipt_after_restart_without_input_state(go):
    async def fn(b):
        await ready(b)
        d = await create_deal(b)
        await b.run(cb(BUYER, f"dl:rc:{d.id}"))
        b.restart()  # in-memory FSM is gone
        await b.run(msg(BUYER, document=PDF))
        assert (await get_deal(d.id)).status == "paid"
    go(fn)


def test_terms_change_between_confirm_and_create(go):
    async def fn(b):
        await ready(b)
        await b.run(cb(BUYER, "bc:1"), msg(BUYER, "10000"))
        go_btn = next(x for x in b.session.buttons(BUYER) if x and x.startswith("bgo:"))
        async with models.Session() as s:
            await settings.put(s, "rate", "90")
            await s.commit()
        await b.run(cb(BUYER, go_btn))
        async with models.Session() as s:
            assert await s.scalar(select(func.count(Deal.id))) == 0
        assert "Курс или комиссия изменились" in plain(b.session.last(BUYER))
        assert "По курсу 90 ₽" in plain(b.session.last(BUYER))
    go(fn)


def test_cancel_confirm_and_double_click(go):
    async def fn(b):
        await ready(b)
        d = await create_deal(b)
        await b.run(cb(BUYER, f"dl:cn:{d.id}"))
        assert "Не отменяйте" in plain(b.session.last(BUYER))
        await b.run(cb(BUYER, f"dl:cn2:{d.id}"), cb(BUYER, f"dl:cn2:{d.id}"))
        assert (await get_deal(d.id)).status == "cancelled"
        s_ = await user(SELLER)
        assert (s_.balance, s_.frozen) == (D(200), D(0))
    go(fn)


def test_buyer_fail_limit_blocks_fake_buyer(go):
    async def fn(b):
        await ready(b)
        async with models.Session() as s:
            await settings.put(s, "buyer_fail_limit", "2")
            await s.commit()
        for _ in range(2):
            d = await create_deal(b, "1000")
            await b.run(cb(BUYER, f"dl:cn2:{d.id}"))
        await b.run(cb(BUYER, "bc:1"), msg(BUYER, "1000"))
        await b.run(cb(BUYER, next(x for x in b.session.buttons(BUYER) if x and x.startswith("bgo:"))))
        assert any("Слишком много отменённых" in a for a in b.session.alerts())
    go(fn)


async def expire_now(b, d):
    await shift_deal(d.id, expires_at=now() - timedelta(minutes=1))
    await tasks.expire_deals(b.bot)


async def end_hold(b, d):
    await shift_deal(d.id, hold_until=now() - timedelta(seconds=1))
    await tasks.release_holds(b.bot)


def test_late_receipt_during_hold_returns_deal_to_seller(go):
    async def fn(b):
        await ready(b)
        d = await create_deal(b)
        await expire_now(b, d)
        d1 = await get_deal(d.id)
        assert d1.status == "expired" and d1.funds_held
        assert (await user(SELLER)).frozen == D(95)  # still held: the late receipt is covered
        assert "Уже перевели деньги?" in plain(b.session.last(BUYER))
        assert "удерживается до" in plain(b.session.last(SELLER))
        await b.run(cb(BUYER, f"dl:late:{d.id}"), msg(BUYER, document=PDF))
        d2 = await get_deal(d.id)
        assert d2.status == "paid" and d2.receipt_file_id == "pdf1" and not d2.funds_held
        s_ = await user(SELLER)
        assert (s_.balance, s_.frozen) == (D(105), D(95))  # frozen once, not twice
        assert "после окончания срока" in plain(b.session.last(SELLER))
    go(fn)


def test_seller_cannot_withdraw_held_funds_after_expiry(go):
    """Scam vector: buyer pays at the last minute, deal expires, seller withdraws before the late receipt."""
    async def fn(b):
        await ready(b)
        d = await create_deal(b)
        await expire_now(b, d)
        await b.run(cb(SELLER, "w:wd"), msg(SELLER, "200"))
        assert "Доступно только 105 USDT" in plain(b.session.last(SELLER))
        await end_hold(b, d)
        assert (await user(SELLER)).frozen == 0 and not (await get_deal(d.id)).funds_held
        await end_hold(b, d)  # released once
        assert (await user(SELLER)).balance == D(200)
    go(fn)


def test_late_receipt_after_hold_when_seller_spent_funds_goes_to_admins(go):
    async def fn(b):
        await ready(b)
        d = await create_deal(b)
        await expire_now(b, d)
        await end_hold(b, d)
        async with models.Session() as s:
            await money.add(s, SELLER, D(-190), "withdraw", "wd:x")
            await s.commit()
        await b.run(cb(BUYER, f"dl:late:{d.id}"), msg(BUYER, document=PDF))
        assert (await get_deal(d.id)).status == "expired"
        assert "нет свободных средств" in plain(b.session.last(BUYER))
        assert (await get_deal(d.id)).receipt_file_id == "pdf1"  # kept for the admin
        assert any("Поздний чек" in t for t in await b.deliver())
    go(fn)


def test_receipt_uploaded_after_deadline_before_task_ran(go):
    async def fn(b):
        await ready(b)
        d = await create_deal(b)
        await b.run(cb(BUYER, f"dl:rc:{d.id}"))
        await shift_deal(d.id, expires_at=now() - timedelta(seconds=5))
        await b.run(msg(BUYER, document=PDF))
        d2 = await get_deal(d.id)
        assert d2.status == "paid" and d2.paid_at is not None
        s_ = await user(SELLER)
        assert (s_.balance, s_.frozen) == (D(105), D(95))
    go(fn)


def test_silent_seller_reminder_then_buyer_dispute_with_evidence(go):
    async def fn(b):
        await ready(b)
        d = await create_deal(b)
        await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, document=PDF))
        await b.run(cb(BUYER, f"dl:bd2:{d.id}"))
        assert (await get_deal(d.id)).status == "paid"  # too early
        await shift_deal(d.id, paid_at=now() - timedelta(minutes=31))
        await tasks.remind_sellers(b.bot)
        await tasks.remind_sellers(b.bot)
        reminders = [t for t in b.session.texts(SELLER) if "ждёт подтверждения" in t]
        assert len(reminders) == 1
        assert f"dl:bd:{d.id}" in b.session.buttons(BUYER)
        await b.run(cb(BUYER, f"dl:bd:{d.id}"), cb(BUYER, f"dl:bd2:{d.id}"))
        assert (await get_deal(d.id)).status == "dispute"
        photo = [PhotoSize(file_id="ph", file_unique_id="ph", width=1, height=1)]
        await b.run(cb(BUYER, f"dl:ev:{d.id}"), msg(BUYER, "Перевёл в 12:03 со Сбера"),
                    cb(SELLER, f"dl:ev:{d.id}"), msg(SELLER, photo=photo))
        files = (await get_deal(d.id)).dispute_files
        assert files == [["text", "Перевёл в 12:03 со Сбера", "buyer"], ["photo", "ph", "seller"]]
        await b.run(cb(ADMIN, f"adv:{d.id}"))
        assert "покупатель 1, продавец 1" in plain(b.session.last(ADMIN))
        await b.run(cb(ADMIN, f"af:{d.id}"))
        assert any("Перевёл в 12:03" in t for t in b.session.texts(ADMIN))
    go(fn)


# ---------- seller ----------

def test_seller_sees_why_card_is_hidden(go):
    async def fn(b):
        await ready(b, balance=D(5))
        await b.run(cb(SELLER, "cd:1"))
        assert "мало свободного баланса" in plain(b.session.last(SELLER))
        async with models.Session() as s:
            (await s.get(User, SELLER)).balance = D(200)
            await s.commit()
        await b.run(cb(SELLER, "sl:on:0"), cb(SELLER, "cd:1"))
        assert "вы не на смене" in plain(b.session.last(SELLER))
        await b.run(cb(SELLER, "sl:on:1"), cb(SELLER, "sl:on:1"))  # double tap keeps the shift on
        assert (await user(SELLER)).is_online
        await create_deal(b)
        await b.run(cb(SELLER, "cd:1"))
        assert "занята сделкой #1" in plain(b.session.last(SELLER))
    go(fn)


def test_duplicate_requisites_rejected(go):
    async def fn(b):
        await ready(b)
        await b.run(msg(OTHER, "/start"), cb(OTHER, "sl:add"), cb(OTHER, "sl:add:card"), cb(OTHER, "sl:bank:0"),
                    msg(OTHER, "4111111111111111"))
        assert "уже используются другим продавцом" in plain(b.session.last(OTHER))
        async with models.Session() as s:
            assert await s.scalar(select(func.count(Card.id))) == 1
    go(fn)


def test_seller_menu_points_to_deal_awaiting_check(go):
    async def fn(b):
        await ready(b)
        d = await create_deal(b)
        await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, document=PDF), cb(SELLER, "menu"))
        assert f"dl:{d.id}" in b.session.buttons(SELLER)
    go(fn)


def test_seller_blocked_bot_admins_are_told(go):
    async def fn(b):
        await ready(b)
        b.session.blocked.add(SELLER)
        d = await create_deal(b)
        assert d.status == "waiting_payment"  # buyer is not affected by the seller's Telegram problems
        assert any("не получил уведомление" in t for t in await b.deliver())
        await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, document=PDF))
        assert any("не получил чек" in t for t in await b.deliver())
    go(fn)


# ---------- admin ----------

def test_ban_seller_cancels_unpaid_and_disputes_paid(go):
    async def fn(b):
        await ready(b)
        async with models.Session() as s:
            s.add(Card(user_id=SELLER, kind="sbp", bank="ВТБ", requisites="+79990000000", holder="Иванов Иван",
                       min_rub=D(1000), max_rub=D(5000), is_active=True))
            s.add(User(id=OTHER))
            await s.commit()
        d1 = await create_deal(b)
        await b.run(cb(BUYER, f"dl:rc:{d1.id}"), msg(BUYER, document=PDF))
        await b.run(msg(OTHER, "/start"), cb(OTHER, "bc:2"), msg(OTHER, "2000"))
        await b.run(cb(OTHER, next(x for x in b.session.buttons(OTHER) if x and x.startswith("bgo:"))))
        await b.run(cb(ADMIN, f"aub:{SELLER}:1"))
        assert "Сделок без оплаты будет отменено: 1" in plain(b.session.last(ADMIN))
        await b.run(cb(ADMIN, f"aub2:{SELLER}"), cb(ADMIN, f"aub2:{SELLER}"))
        assert (await get_deal(d1.id)).status == "dispute"
        assert (await get_deal(2)).status == "void"
        async with models.Session() as s:
            assert await deals.buyer_failures(s, OTHER) == 0  # the ban is not the buyer's fault
        assert any("Не переводите деньги" in t for t in b.session.texts(OTHER))
        s_ = await user(SELLER)
        assert s_.is_banned and s_.frozen == D(95)  # only the paid deal stays frozen
        await b.run(msg(SELLER, "/start"))
        assert "Аккаунт заблокирован" in plain(b.session.last(SELLER))
        async with models.Session() as s:
            assert await s.scalar(select(func.count(Audit.id)).where(Audit.action == "ban")) == 1
    go(fn)


def test_admin_resolution_needs_confirmation_and_runs_once(go):
    async def fn(b):
        await ready(b)
        d = await create_deal(b)
        await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, document=PDF), cb(ADMIN, f"ar:{d.id}:b"))
        assert (await get_deal(d.id)).status == "paid"
        assert "Покупателю +94 USDT" in plain(b.session.last(ADMIN))
        await b.run(cb(ADMIN, f"ar2:{d.id}:b"), cb(ADMIN, f"ar2:{d.id}:s"))
        assert (await get_deal(d.id)).status == "completed"
        b_ = await user(BUYER)
        assert b_.balance == D(94)
        async with models.Session() as s:
            fees = await s.scalar(select(func.sum(Ledger.delta)).where(Ledger.user_id.is_(None)))
            assert fees == D(1)
    go(fn)


def test_balance_adjustment_requires_reason_and_is_auditable(go):
    async def fn(b):
        await ready(b)
        await b.run(cb(ADMIN, f"aadj:{BUYER}"), cb(ADMIN, f"aum:{BUYER}:+"), msg(ADMIN, "12.5"),
                    cb(ADMIN, "amr:other"), msg(ADMIN, "ok"))
        assert "От 5 до 200" in plain(b.session.last(ADMIN))
        await b.run(msg(ADMIN, "Возврат по обращению #7"))
        card = plain(b.session.last(ADMIN))
        assert "Корректировка #1 · черновик" in card and "0 → 12.5 USDT" in card
        assert (await user(BUYER)).balance == 0  # a draft moves no money
        b.restart()  # confirmation lives in the DB, not in the FSM
        await b.run(cb(ADMIN, "adj:ok:1"), cb(ADMIN, "adj:ok:1"))
        assert (await user(BUYER)).balance == D("12.5")
        assert "Причина: Другое: Возврат по обращению #7" in plain(b.session.last(BUYER))
        await b.run(cb(ADMIN, f"auh:{BUYER}"))
        assert "Журнал сходится" in plain(b.session.last(ADMIN))
        async with models.Session() as s:
            rows = (await s.scalars(select(Ledger).where(Ledger.kind == "admin"))).all()
            assert len(rows) == 1 and rows[0].ref == "adj:1" and rows[0].note == "Другое: Возврат по обращению #7"
        assert any("Корректировка #1" in t and "0 → 12.5" in t for t in await b.deliver())
    go(fn)


def test_admin_search_by_deal_and_requisites(go):
    async def fn(b):
        await ready(b)
        d = await create_deal(b)
        await b.run(cb(ADMIN, "au"), msg(ADMIN, f"#{d.id}"))
        assert f"Сделка #{d.id}" in plain(b.session.last(ADMIN))
        await b.run(cb(ADMIN, "au"), msg(ADMIN, "4111 1111 1111 1111"))
        assert "Карта #1" in plain(b.session.last(ADMIN))
    go(fn)


def test_support_ticket_and_admin_reply(go):
    async def fn(b):
        await ready(b)
        await b.run(cb(BUYER, "info"), cb(BUYER, "sup"), msg(BUYER, "Перевёл деньги, но сделка отменилась"))
        assert "Обращение #1 принято" in plain(b.session.last(BUYER))
        assert any("Обращение #1" in t for t in await b.deliver())
        assert "atk:1" in b.session.buttons(ADMIN)
        await b.run(cb(ADMIN, "atk:1"), cb(ADMIN, "atr:1"), msg(ADMIN, "Проверили, вернули средства"))
        assert "Ответ поддержки на обращение #1" in plain(b.session.last(BUYER))
        async with models.Session() as s:
            assert (await s.get(Ticket, 1)).status == "answered"
        await b.run(cb(ADMIN, "atc:1"), cb(ADMIN, "atc:1"))
        async with models.Session() as s:
            assert (await s.get(Ticket, 1)).status == "closed"
        await b.run(cb(ADMIN, f"amsg:{BUYER}"), msg(ADMIN, "Ещё один вопрос к вам"))
        assert "Сообщение от поддержки" in plain(b.session.last(BUYER))
        for _ in range(6):
            await b.run(cb(BUYER, "sup"), msg(BUYER, "ещё вопрос по сделке"))
        assert "дождитесь ответа" in plain(b.session.last(BUYER))
    go(fn)


def test_two_admins_same_card_ban_is_idempotent(go):
    async def fn(b):
        await ready(b)
        await b.run(cb(ADMIN, "acb:1:1"), cb(ADMIN, "acb:1:1"))
        async with models.Session() as s:
            assert (await s.get(Card, 1)).is_banned
    go(fn)


# ---------- wallet & xRocket failures ----------

def test_withdraw_http_500_keeps_funds_reserved_then_reconciles(go):
    async def fn(b):
        await ready(b)
        async with models.Session() as s:
            (await s.get(User, BUYER)).balance = D(50)
            await s.commit()
        b.rocket.cheque_error = xrocket.XRocketError("internal_error", "boom", 500)
        await b.run(cb(BUYER, "w:wd"), msg(BUYER, "20"), cb(BUYER, "w:go"), cb(BUYER, "w:go"))
        async with models.Session() as s:
            wd = await s.scalar(select(Withdrawal))
            assert wd.status == "unknown"
            assert (await s.get(User, BUYER)).balance == D(30)  # NOT refunded: the cheque may exist
            assert await s.scalar(select(Ledger.ref).where(Ledger.kind == "withdraw")) == f"wd:{wd.id}"
        assert "на проверке" in plain(b.session.last(BUYER))
        b.rocket.lookup = {"chequeId": "c9", "state": "active", "deleted": False,
                           "links": {"telegramBotLink": "https://t.me/xRocket?start=c9"}}
        await tasks.reconcile_withdrawals(b.bot)
        async with models.Session() as s:
            assert (await s.scalar(select(Withdrawal))).status == "done"
        assert "Чек на 20 USDT" in plain(b.session.last(BUYER))
    go(fn)


def test_withdraw_definite_error_refunds(go):
    async def fn(b):
        await ready(b)
        async with models.Session() as s:
            (await s.get(User, BUYER)).balance = D(50)
            await s.commit()
        b.rocket.cheque_error = xrocket.XRocketError("target_user_not_found", "", 400)
        await b.run(cb(BUYER, "w:wd"), msg(BUYER, "20"), cb(BUYER, "w:go"))
        assert (await user(BUYER)).balance == D(50)
        assert "Средства возвращены" in plain(b.session.last(BUYER))
    go(fn)


def test_stale_pending_withdrawal_becomes_unknown(go):
    async def fn(b):
        await ready(b)
        async with models.Session() as s:
            s.add(Withdrawal(user_id=BUYER, amount=D(5), fee=D(0), status="pending",
                             created_at=now() - timedelta(minutes=10)))
            await s.commit()
        await tasks.reconcile_withdrawals(b.bot)
        async with models.Session() as s:
            assert (await s.scalar(select(Withdrawal))).status == "unknown"  # cheque not found: admin decides
    go(fn)


def test_deposit_waits_for_pending_payment(go):
    async def fn(b):
        await ready(b)
        b.rocket.invoice_status = "expired"
        b.rocket.payments = [{"status": "paid", "receiveAmount": "10", "receiveCurrency": "USDT"},
                             {"status": "pending", "receiveAmount": None, "receiveCurrency": None}]
        await b.run(cb(BUYER, "w:dep"), msg(BUYER, "30"))
        await tasks.poll_deposits(b.bot)
        assert (await user(BUYER)).balance == 0
        b.rocket.payments[1] = {"status": "paid", "receiveAmount": "19.5", "receiveCurrency": "USDT"}
        await tasks.poll_deposits(b.bot)
        assert (await user(BUYER)).balance == D("29.5")
    go(fn)


# ---------- settings cache ----------

def test_settings_reload_never_exposes_defaults(go):
    async def fn(b):
        async with models.Session() as s:
            await settings.put(s, "rate", "91")
            await s.commit()
        seen = []

        async def reader():
            for _ in range(50):
                seen.append(settings.get("rate"))
                await asyncio.sleep(0)

        async def reloader():
            for _ in range(10):
                async with models.Session() as s:
                    await settings.load(s)

        await asyncio.gather(reader(), reloader())
        assert set(seen) == {"91"}
    go(fn)
