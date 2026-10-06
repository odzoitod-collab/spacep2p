"""Admin workspace: profile by Telegram ID, safe balance adjustments, operation cards, alert outbox."""
import asyncio
from decimal import Decimal as D

from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage
from sqlalchemy import func, select

from bot import models, tasks, ui
from bot.models import Adjustment, Event, Ledger, User, Withdrawal
from bot.services import admins, settings
from tests.harness import bsc_tick, cb, msg, plain
from tests.test_scenarios import ADMIN, BUYER, PDF, SELLER, create_deal, ready, user

ADMIN2 = 2


async def completed_deal(b):
    d = await create_deal(b)
    await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, document=PDF),
                cb(SELLER, f"dl:ok:{d.id}"), cb(SELLER, f"dl:ok2:{d.id}"))
    return d


def test_profile_splits_buys_and_sells_with_outcomes(go):
    async def fn(b):
        await ready(b)
        await completed_deal(b)
        d2 = await create_deal(b, "2000")
        await b.run(cb(BUYER, f"dl:cn2:{d2.id}"))
        await b.run(cb(ADMIN, f"auv:{BUYER}"))
        buyer_view = plain(b.session.last(ADMIN))
        assert f"Пользователь · {BUYER}" in buyer_view
        assert "Покупки: 1 завершено на 10 000 ₽" in buyer_view and "отменил покупатель 1" in buyer_view
        assert "Продажи: 0 завершено" in buyer_view
        await b.run(cb(ADMIN, f"auv:{SELLER}"))
        assert "Продажи: 1 завершено на 10 000 ₽" in plain(b.session.last(ADMIN))
        buttons = b.session.buttons(ADMIN)
        for data in (f"aud:{SELLER}", f"auh:{SELLER}", f"auc:{SELLER}", f"auw:{SELLER}", f"bal:u:{SELLER}",
                     f"dm:0:{SELLER}", f"aub:{SELLER}:1"):
            assert data in buttons, data
    go(fn)


DEST = "0xdD2FD4581271e230360230F9337D5c0430Bf44C0"


def test_search_by_withdrawal_opens_owner_profile(go):
    async def fn(b):
        await ready(b)
        async with models.Session() as s:
            s.add(Withdrawal(user_id=BUYER, amount=D(25), fee=D(1), status="unknown", method="ton", address=DEST,
                             error="не найден в сети"))
            await s.commit()
        await b.run(cb(ADMIN, "au"), msg(ADMIN, "в1"))
        screen = plain(b.session.last(ADMIN))
        assert "Найдено: вывод #1 · требует проверки" in screen and f"Пользователь · {BUYER}" in screen
        assert "awv:1" in b.session.buttons(ADMIN)
        await b.run(cb(ADMIN, "awv:1"))
        card = plain(b.session.last(ADMIN))
        for part in ("Вывод #1 · требует проверки", "Списано: 25 USDT", "к отправке 24 · комиссия 1", DEST,
                     "ошибка: не найден в сети", "Вывод старой сети (TON / xRocket)"):
            assert part in card, part
        buttons = b.session.buttons(ADMIN)
        assert "wk:rf:1" in buttons and "wk:done:1" in buttons  # an owner decides
    go(fn)


def test_withdrawal_card_keeps_whole_chain(go):
    async def fn(b):
        await ready(b)
        async with models.Session() as s:
            (await s.get(User, BUYER)).balance = D(50)
            await s.commit()
        b.chain.fund_hot(usdt="100")
        await b.run(cb(BUYER, "w:out"), msg(BUYER, "20"), msg(BUYER, DEST), cb(BUYER, "wb:go"))
        await bsc_tick(b)
        b.chain.mine()
        await bsc_tick(b)
        await b.run(cb(ADMIN, "awv:1"))
        card = plain(b.session.last(ADMIN))
        assert "Вывод #1 · выполнен" in card and "nonce 0 · подтверждена" in card and "USDT · BEP-20" in card
        assert "wk:rf:1" not in b.session.buttons(ADMIN)  # the chain settled it: nothing to decide by hand
        await b.run(cb(ADMIN, "aev:wd:1"))
        history = plain(b.session.last(ADMIN))
        assert history.index("Запрос вывода") < history.index("Отправка 19 USDT") < history.index("Вывод выполнен")
        assert (await user(BUYER)).balance == D(30) and b.chain.usdt_of(DEST) == D(19)
    go(fn)


def test_adjustment_only_from_available_and_idempotent(go):
    async def fn(b):
        await ready(b)
        await create_deal(b)  # seller: 105 available, 95 frozen
        await b.run(cb(ADMIN, f"aum:{SELLER}:-"), msg(ADMIN, "150"), cb(ADMIN, "amr:tech"))
        assert "Доступного баланса не хватает" in plain(b.session.last(ADMIN))
        await b.run(cb(ADMIN, "adj:ok:1"))
        s_ = await user(SELLER)
        assert (s_.balance, s_.frozen) == (D(105), D(95))  # frozen money is never touched
        async with models.Session() as s:
            assert (await s.get(Adjustment, 1)).status == "failed"
            assert await s.scalar(select(func.count(Ledger.id)).where(Ledger.kind == "admin")) == 0
    go(fn)


STAFF = 30  # an admin granted in the panel, not an owner


async def grant_staff(b):
    await b.run(msg(STAFF, "/start"))
    async with models.Session() as s:
        await admins.grant(s, STAFF)
        await s.commit()


def test_large_adjustment_needs_second_admin(go):
    async def fn(b):
        await ready(b)
        await grant_staff(b)
        async with models.Session() as s:
            await settings.put(s, "adjust_approval_usdt", "100")
            await s.commit()
        await b.run(cb(STAFF, f"aum:{BUYER}:+"), msg(STAFF, "500"), cb(STAFF, "amr:compensation"),
                    cb(STAFF, "adj:ok:1"), cb(STAFF, "adj:ok:1"))
        assert (await user(BUYER)).balance == 0
        assert "Подтвердить должен другой администратор" in plain(b.session.last(STAFF))
        assert any("ждёт второго администратора" in t for t in await b.deliver())
        await b.run(msg(ADMIN2, "/start"), cb(ADMIN2, "aadjl"), cb(ADMIN2, "adj:ok:1"), cb(ADMIN2, "adj:ok:1"))
        assert (await user(BUYER)).balance == D(500)
        async with models.Session() as s:
            a = await s.get(Adjustment, 1)
            assert (a.status, a.admin_id, a.approved_by) == ("done", STAFF, ADMIN2)
            assert await s.scalar(select(func.count(Ledger.id)).where(Ledger.kind == "admin")) == 1
    go(fn)


def test_owner_adjusts_any_balance_alone(go):
    async def fn(b):
        await ready(b)
        async with models.Session() as s:
            await settings.put(s, "adjust_approval_usdt", "100")
            await s.commit()
        await b.run(cb(ADMIN, f"aum:{BUYER}:+"), msg(ADMIN, "500"), cb(ADMIN, "amr:compensation"),
                    cb(ADMIN, "adj:ok:1"))
        assert (await user(BUYER)).balance == D(500)  # above the threshold, no second admin for an owner
        await b.run(cb(ADMIN, f"aum:{BUYER}:-"), msg(ADMIN, "200"), cb(ADMIN, "amr:refund"), cb(ADMIN, "adj:ok:2"))
        assert (await user(BUYER)).balance == D(300)  # taking off too
        await b.run(cb(ADMIN, f"aum:{ADMIN}:+"), msg(ADMIN, "7"), cb(ADMIN, "amr:other"), msg(ADMIN, "проверка"),
                    cb(ADMIN, "adj:ok:3"))
        assert (await user(ADMIN)).balance == D(7)  # even his own balance
        async with models.Session() as s:
            assert [a.status for a in (await s.scalars(select(Adjustment).order_by(Adjustment.id))).all()] == [
                "done"] * 3
    go(fn)


def test_alerts_survive_telegram_outage(go):
    async def fn(b):
        await ready(b)
        b.session.blocked.add(ADMIN)  # log chat unavailable
        await b.run(cb(BUYER, "sup"), msg(BUYER, "Не пришли USDT по сделке"))
        assert await b.deliver() == []
        async with models.Session() as s:
            ev = await s.scalar(select(Event).where(Event.alert))
            assert ev.sent_at is None and ev.attempts == 1
        b.session.blocked.clear()
        assert any("Обращение #1" in t for t in await b.deliver())
        assert await b.deliver() == []  # delivered exactly once
    go(fn)


def test_dispute_alert_is_short_and_links_to_card(go):
    async def fn(b):
        await ready(b)
        d = await create_deal(b)
        await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, document=PDF), cb(SELLER, f"dl:ds:{d.id}"),
                    cb(SELLER, f"dl:dr:{d.id}:wrong_amount"), msg(SELLER, "9000"))
        from aiogram.types import Video
        await b.run(msg(SELLER, video=Video(file_id="v", file_unique_id="v", width=1, height=1, duration=1)))
        alerts = await b.deliver()
        assert any(f"Сделка #{d.id}" in t and "пришло 9 000 ₽" in t for t in alerts)
        assert not any("4111" in t for t in alerts)  # requisites only on the card
        assert f"adv:{d.id}" in b.session.buttons(ADMIN)
    go(fn)


def test_frozen_moves_are_journaled(go):
    async def fn(b):
        await ready(b)
        await completed_deal(b)
        await b.run(cb(SELLER, "w:h"))
        history = plain(b.session.last(SELLER))
        assert "Заморозка по сделке #1 · −95 доступно" in history
        assert "Продажа по сделке #1 · −95 из заморозки" in history
        await b.run(cb(ADMIN, f"auh:{SELLER}"))
        assert "Журнал сходится" in plain(b.session.last(ADMIN))
    go(fn)


def test_csv_reports(go):
    async def fn(b):
        await ready(b)
        await completed_deal(b)
        await b.run(cb(ADMIN, "arp"), cb(ADMIN, "arp:7"))
        docs = [m for m in b.session.calls if type(m).__name__ == "SendDocument" and m.chat_id == ADMIN]
        names = [d.document.filename for d in docs]
        assert [n.split("_")[0] for n in names] == ["deals", "ledger", "payments"]
        deals_csv = docs[0].document.data.decode("utf-8-sig").splitlines()
        assert deals_csv[0].startswith("id,created_at") and ",completed,confirmed," in deals_csv[1]
    go(fn)




def test_pacing_retries_after_flood_limit():
    async def scenario():
        calls = []

        async def send():
            calls.append(1)
            if len(calls) == 1:
                raise TelegramRetryAfter(method=SendMessage(chat_id=1, text="x"), message="flood", retry_after=0)
            return "sent"

        assert await ui.paced(send) == "sent" and len(calls) == 2
    asyncio.run(scenario())


def test_network_errors_alert_once(go):
    async def fn(b):
        await ready(b)
        b.chain.down = True
        await bsc_tick(b)
        await bsc_tick(b)
        alerts = [t for t in await b.deliver() if "BEP-20:" in t]
        assert len(alerts) == 1  # repeated background failures do not flood the admin chat
    go(fn)
