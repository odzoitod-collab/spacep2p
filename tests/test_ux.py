"""Commands, message deletion policy, persistent dialog state, receipts, dispute evidence, log chat."""
from datetime import timedelta
from decimal import Decimal as D

from aiogram.types import Document, PhotoSize
from sqlalchemy import select

from bot import models, tasks
from bot.config import config
from bot.handlers import commands
from bot.models import Adjustment, Deal, User
from bot.services import settings
from tests.harness import cb, msg, plain
from tests.test_scenarios import ADMIN, BUYER, PDF, SELLER, create_deal, get_deal, ready, user

ADMIN2 = 2


def deleted(b, chat):
    return [m.message_id for m in b.session.calls if type(m).__name__ == "DeleteMessage" and m.chat_id == chat]


def test_commands_open_new_screen_and_keep_user_message(go):
    async def fn(b):
        await ready(b)
        start = msg(BUYER, "/start")
        await b.run(start)
        assert start.message.message_id not in deleted(b, BUYER)  # a command stays in the chat
        before = (await user(BUYER)).ui_msg_id
        for command, expect in (("/buy", "RUB ⇄ USDT"), ("/sell", "панель мерчанта"), ("/wallet", "Кошелёк"),
                                ("/deals", "Мои сделки"), ("/help", "Помощь"), ("/support", "Помощь"),
                                ("/admin", "Strait Pay"), ("/menu", "Strait Pay")):
            await b.run(msg(BUYER, command))
            assert expect in plain(b.session.last(BUYER)), command
        assert (await user(BUYER)).ui_msg_id != before
        assert before not in deleted(b, BUYER)  # old screens stay in the chat as history, untouched
        assert not [m for m in b.session.calls if type(m).__name__ == "EditMessageReplyMarkup"]
        await b.run(msg(ADMIN, "/admin"))
        assert "Админ-панель" in plain(b.session.last(ADMIN))
    go(fn)


def test_only_answers_to_bot_questions_are_deleted(go):
    async def fn(b):
        await ready(b)
        chat = msg(BUYER, "привет")
        await b.run(chat)
        assert chat.message.message_id not in deleted(b, BUYER)  # free text is not an answer to a question
        await b.run(cb(BUYER, "w:dep"))
        amount = msg(BUYER, "50")
        await b.run(amount)
        assert amount.message.message_id in deleted(b, BUYER)  # the bot asked for the amount: tidy up
        assert "Счёт #1 · xRocket" in plain(b.session.last(BUYER))
    go(fn)


def test_dialog_state_survives_restart(go):
    async def fn(b):
        await ready(b)
        async with models.Session() as s:
            (await s.get(User, BUYER)).balance = D(50)
            await s.commit()
        await b.run(cb(BUYER, "w:wd"))
        b.restart()  # new process
        await b.run(msg(BUYER, "10"))
        assert "Подтвердите вывод" in plain(b.session.last(BUYER))
        b.restart()
        await b.run(cb(BUYER, "w:go"))
        assert (await user(BUYER)).balance == D(40)
    go(fn)


def test_setup_commands_menu(go):
    async def fn(b):
        await commands.setup_commands(b.bot)
        calls = [m for m in b.session.calls if type(m).__name__ == "SetMyCommands"]
        default = [c.command for c in calls[0].commands]
        assert default[:2] == ["start", "buy"] and "admin" not in default
        assert all("admin" in [c.command for c in m.commands] for m in calls[1:]) and len(calls) == 1 + len(config.admin_ids)
    go(fn)


def test_renamed_file_is_not_accepted_as_pdf(go):
    async def fn(b):
        await ready(b)
        d = await create_deal(b)
        fake = Document(file_id="fake", file_unique_id="fk", mime_type="application/pdf", file_name="чек.pdf")
        b.session.files["fake"] = b"just text renamed to pdf"
        await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, document=fake))
        assert "не похож на PDF" in plain(b.session.last(BUYER))
        assert (await get_deal(d.id)).status == "waiting_payment"
        await b.run(msg(BUYER, document=PDF))
        assert (await get_deal(d.id)).status == "paid"
    go(fn)


def test_photo_receipts_when_enabled(go):
    async def fn(b):
        await ready(b)
        async with models.Session() as s:
            await settings.put(s, "receipt_images", "1")
            await s.commit()
        d = await create_deal(b)
        photo = [PhotoSize(file_id="shot", file_unique_id="shot", width=1, height=1)]
        await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, photo=photo))
        assert (await get_deal(d.id)).receipt_file_id == "photo:shot"
        sent = [m for m in b.session.calls if type(m).__name__ == "SendPhoto" and m.chat_id == SELLER and m.photo == "shot"]
        assert sent and "проверьте поступление" in plain(sent[0].caption)
    go(fn)


def test_evidence_limit_is_per_side(go):
    async def fn(b):
        await ready(b)
        d = await create_deal(b)
        await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, document=PDF))
        async with models.Session() as s:
            deal = await s.get(Deal, d.id)
            deal.status, deal.dispute_reason = "dispute", "not_received"
            deal.dispute_files = [["photo", f"p{i}", "seller"] for i in range(15)]
            await s.commit()
        await b.run(cb(SELLER, f"dl:ev:{d.id}"), msg(SELLER, "ещё одно пояснение"))
        assert "от одной стороны" in plain(b.session.last(SELLER))
        await b.run(cb(BUYER, f"dl:ev:{d.id}"), msg(BUYER, "Перевёл в 12:03"))
        assert (await get_deal(d.id)).dispute_files[-1] == ["text", "Перевёл в 12:03", "buyer"]
    go(fn)


def test_buyer_dispute_goes_straight_to_evidence(go):
    async def fn(b):
        await ready(b)
        d = await create_deal(b)
        await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, document=PDF))
        async with models.Session() as s:
            (await s.get(Deal, d.id)).paid_at = models.now() - timedelta(minutes=40)
            await s.commit()
        await b.run(cb(BUYER, f"dl:bd2:{d.id}"))
        assert "Доказательства по спору" in plain(b.session.last(BUYER))
        await b.run(msg(BUYER, "Сбер, 12:03, списание 10 000"))
        assert len((await get_deal(d.id)).dispute_files) == 1
        await b.run(cb(ADMIN, f"adv:{d.id}"))
        assert f"amsg:{BUYER}" in b.session.buttons(ADMIN) and f"amsg:{SELLER}" in b.session.buttons(ADMIN)
    go(fn)


def test_admin_cannot_credit_himself_alone(go):
    async def fn(b):
        await ready(b)
        await b.run(msg(ADMIN2, "/start"))
        await b.run(cb(ADMIN, f"aum:{ADMIN}:+"), msg(ADMIN, "1000"), cb(ADMIN, "amr:deposit_fix"))
        assert "провести сможет только другой администратор" in plain(b.session.last(ADMIN))
        await b.run(cb(ADMIN, "adj:ok:1"), cb(ADMIN, "adj:ok:1"))
        assert (await user(ADMIN)).balance == 0
        async with models.Session() as s:
            assert (await s.get(Adjustment, 1)).status == "pending"
        await b.run(cb(ADMIN2, "adj:ok:1"))
        assert (await user(ADMIN)).balance == D(1000)
    go(fn)


def test_log_chat_gets_every_deal_step(go):
    async def fn(b):
        await ready(b)
        await b.deliver()
        d = await create_deal(b)
        await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, document=PDF), cb(SELLER, f"dl:ok2:{d.id}"))
        log = "\n".join(await b.deliver())
        for part in ("Открыта: 10 000 ₽ → 94 USDT", "загрузил чек", "Завершена: 10 000 ₽"):
            assert part in log, part
        async with models.Session() as s:
            await settings.put(s, "log_all", "0")
            await s.commit()
        d2 = await create_deal(b, "2000")
        await b.run(cb(BUYER, f"dl:cn2:{d2.id}"))
        assert await b.deliver() == []  # routine steps muted, problems still go through
    go(fn)


def test_withdrawal_refusal_is_alerted(go):
    async def fn(b):
        from bot.services import xrocket
        await ready(b)
        async with models.Session() as s:
            (await s.get(User, BUYER)).balance = D(5)
            await s.commit()
        b.rocket.cheque_error = xrocket.XRocketError("target_user_not_found", "", 400)
        await b.run(cb(BUYER, "w:wd"), msg(BUYER, "1"), cb(BUYER, "w:go"))
        log = await b.deliver()
        assert any("нужно внимание" in t and "аккаунт не найден в xRocket" in t for t in log)
    go(fn)


def test_log_goes_to_forum_topic(go):
    async def fn(b):
        config.log_thread_id = 77
        try:
            await ready(b)
            await b.deliver()
        finally:
            config.log_thread_id = None
        sent = [m for m in b.session.calls if type(m).__name__ == "SendMessage" and m.chat_id == config.log_chat_id]
        assert sent and all(m.message_thread_id == 77 for m in sent)
    go(fn)


def test_long_disputes_remind_admins_once(go):
    async def fn(b):
        await ready(b)
        d = await create_deal(b)
        async with models.Session() as s:
            deal = await s.get(Deal, d.id)
            deal.status, deal.dispute_reason, deal.paid_at = "dispute", "not_received", models.now() - timedelta(hours=3)
            await s.commit()
        await tasks.remind_disputes(b.bot)
        await tasks.remind_disputes(b.bot)
        assert len([t for t in await b.deliver() if "Спор ждёт решения" in t]) == 1
    go(fn)


def test_shift_without_auto_offline(go):
    async def fn(b):
        await ready(b)
        async with models.Session() as s:
            await settings.put(s, "online_minutes", "0")
            (await s.get(User, SELLER)).last_seen = models.now() - timedelta(days=1)
            await s.commit()
        await tasks.auto_offline(b.bot)
        assert (await user(SELLER)).is_online
    go(fn)


def test_fsm_rows_do_not_clash(go):
    async def fn(b):
        await ready(b)
        async with models.Session() as s:
            rows = (await s.scalars(select(models.FsmState))).all()
        assert len({r.key for r in rows}) == len(rows)
    go(fn)


def test_withdrawals_wait_for_xrocket_funds_and_go_out_in_order(go):
    async def fn(b):
        from bot import tasks
        from bot.models import Withdrawal
        from bot.services import xrocket
        await ready(b)
        async with models.Session() as s:
            (await s.get(User, BUYER)).balance = D(50)
            (await s.get(User, SELLER)).balance = D(50)
            await s.commit()
        funds = {"v": "0.5"}

        async def balances():
            return [{"asset": "USDT", "available": funds["v"]}]
        b.rocket.balances = balances
        await b.run(cb(BUYER, "w:wd"), msg(BUYER, "10"), cb(BUYER, "w:go"))
        assert (await user(BUYER)).balance == D(40) and not b.rocket.cheques  # debited, waiting — not refused
        assert "в очереди" in plain(b.session.last(BUYER)) and "w:qc:1" in b.session.buttons(BUYER)
        await b.run(cb(SELLER, "w:wd"), msg(SELLER, "20"), cb(SELLER, "w:go"))
        await b.run(cb(BUYER, "w:wd"), msg(BUYER, "5"), cb(BUYER, "w:go"))
        await tasks.payout_queue(b.bot)  # still nothing on xRocket: all three wait, admins are told
        assert not b.rocket.cheques
        assert any("Выводы ждут пополнения xRocket: 3" in t for t in await b.deliver())

        funds["v"] = "25"  # enough for the first two only (10 + 20 > 25): strictly in order
        xrocket._usdt = None
        await tasks.payout_queue(b.bot)
        assert [c[1] for c in b.rocket.cheques] == ["wd-1"]  # #2 (20) does not fit: #3 (5) must not overtake it
        funds["v"] = "100"
        await tasks.payout_queue(b.bot)
        assert [c[1] for c in b.rocket.cheques] == ["wd-1", "wd-2", "wd-3"]
        assert "Чек на 5 USDT" in plain(b.session.last(BUYER))
        async with models.Session() as s:
            assert [w.status for w in (await s.scalars(select(Withdrawal).order_by(Withdrawal.id))).all()] == ["done"] * 3
    go(fn)


def test_queued_withdrawal_can_be_cancelled(go):
    async def fn(b):
        from bot import tasks
        await ready(b)
        async with models.Session() as s:
            (await s.get(User, BUYER)).balance = D(50)
            await s.commit()

        async def empty():
            return [{"asset": "USDT", "available": "0"}]
        b.rocket.balances = empty
        await b.run(cb(BUYER, "w:wd"), msg(BUYER, "10"), cb(BUYER, "w:go"), cb(BUYER, "w:qc:1"))
        assert (await user(BUYER)).balance == D(50) and "отменён" in plain(b.session.last(BUYER))
        await b.run(cb(BUYER, "w:qc:1"))
        assert "отменить нельзя" in b.session.alerts()[-1]  # nothing is refunded twice
        assert (await user(BUYER)).balance == D(50)
        await tasks.payout_queue(b.bot)
        assert not b.rocket.cheques
    go(fn)


def test_manual_link_in_help_and_texts(go):
    async def fn(b):
        url = "https://telegra.ph/Strait-Pay--P2P-obmen-USDT--RUB-v-Telegram-09-27"
        await ready(b)
        await b.run(cb(BUYER, "info"))
        assert url in b.session.buttons(BUYER)  # «Инструкция» button in Help
        assert f'<a href="{url}">инструкции</a>' in b.session.last(BUYER)  # and a link hidden in the text
        for who, screen in ((SELLER, "sl"), (SELLER, "sl:add"), (BUYER, "om")):
            await b.run(cb(who, screen))
            assert f'href="{url}"' in b.session.last(who), screen
        await b.run(cb(ADMIN, "as:manual_url"), msg(ADMIN, "http://bad"))
        assert "https://" in plain(b.session.last(ADMIN))  # only https links
        await b.run(msg(ADMIN, "-"))
        await b.run(cb(BUYER, "info"))
        assert url not in b.session.buttons(BUYER) and "href" not in b.session.last(BUYER)  # link removed everywhere
    go(fn)
