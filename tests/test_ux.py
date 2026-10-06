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
        async with models.Session() as s:
            (await s.get(User, BUYER)).balance = D(50)
            await s.commit()
        await b.run(cb(BUYER, "w:out"))
        answer = msg(BUYER, "10")
        await b.run(answer)
        assert answer.message.message_id in deleted(b, BUYER)  # the bot asked for the amount: tidy up
        assert "шаг 2 из 2" in plain(b.session.last(BUYER))
    go(fn)


def test_dialog_state_survives_restart(go):
    async def fn(b):
        await ready(b)
        async with models.Session() as s:
            (await s.get(User, BUYER)).balance = D(50)
            await s.commit()
        await b.run(cb(BUYER, "w:out"))
        b.restart()  # new process
        await b.run(msg(BUYER, "10"))
        b.restart()
        await b.run(msg(BUYER, "0xdD2FD4581271e230360230F9337D5c0430Bf44C0"))
        assert "Проверьте вывод" in plain(b.session.last(BUYER))
        b.restart()
        await b.run(cb(BUYER, "wb:go"))
        assert (await user(BUYER)).balance == D(40)
    go(fn)


def test_setup_commands_menu(go):
    async def fn(b):
        await commands.setup_commands(b.bot)
        calls = [m for m in b.session.calls if type(m).__name__ == "SetMyCommands"]
        default = [c.command for c in calls[0].commands]
        assert default[:2] == ["start", "buy"] and "admin" not in default
        assert [c.command for c in calls[1].commands] == ["help"]  # the community and team chats
        assert type(calls[1].scope).__name__ == "BotCommandScopeAllGroupChats"
        assert all("admin" in [c.command for c in m.commands] for m in calls[2:]) and len(calls) == 2 + len(config.admin_ids)
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


def test_receipts_are_pdf_only(go):
    async def fn(b):
        await ready(b)
        d = await create_deal(b)
        photo = [PhotoSize(file_id="shot", file_unique_id="shot", width=1, height=1)]
        await b.run(cb(BUYER, f"dl:rc:{d.id}"))
        assert "фото и скриншоты не принимаются" in plain(b.session.last(BUYER))
        await b.run(msg(BUYER, photo=photo))
        assert (await get_deal(d.id)).status == "waiting_payment" and "Это фото" in plain(b.session.last(BUYER))
        assert not [m for m in b.session.calls if type(m).__name__ == "SendPhoto" and m.chat_id == SELLER]
        await b.run(msg(BUYER, document=PDF))
        assert (await get_deal(d.id)).status == "paid"
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
        await b.run(cb(ADMIN, f"adv:{d.id}"), cb(ADMIN, f"dmc:{d.id}"))  # one «Написать», then whom
        assert f"dm:{d.id}:{BUYER}" in b.session.buttons(ADMIN) and f"dm:{d.id}:{SELLER}" in b.session.buttons(ADMIN)
        assert f"adv:{d.id}" in b.session.buttons(ADMIN)  # and back to the admin's card, not the user's screen
    go(fn)


def test_admin_cannot_credit_himself_alone(go):
    async def fn(b):
        from bot.services import admins
        staff = 40  # a granted admin, not an owner: an owner's word is final
        await ready(b)
        await b.run(msg(ADMIN2, "/start"), msg(staff, "/start"))
        async with models.Session() as s:
            await admins.grant(s, staff)
            await s.commit()
        await b.run(cb(staff, f"aum:{staff}:+"), msg(staff, "1000"), cb(staff, "amr:deposit_fix"))
        assert "провести сможет только другой администратор" in plain(b.session.last(staff))
        await b.run(cb(staff, "adj:ok:1"), cb(staff, "adj:ok:1"))
        assert (await user(staff)).balance == 0
        async with models.Session() as s:
            assert (await s.get(Adjustment, 1)).status == "pending"
        await b.run(cb(ADMIN2, "adj:ok:1"))
        assert (await user(staff)).balance == D(1000)
    go(fn)


def test_log_chat_gets_every_deal_step(go):
    async def fn(b):
        await ready(b)
        await b.deliver()
        d = await create_deal(b)
        await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, document=PDF), cb(SELLER, f"dl:ok2:{d.id}"))
        log = "\n".join(await b.deliver())
        for part in ("Открыта: 10 000 ₽ → 94 USDT", "создал покупатель @u20", "загрузил чек",
                     "Подтвердил продавец @u10 · U10 (10): 10 000 ₽"):
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
        from bot.services import bsc
        from tests.harness import bsc_tick
        await ready(b)
        async with models.Session() as s:
            (await s.get(User, BUYER)).balance = D(5)
            await s.commit()
        b.chain.fund_hot(usdt="100")
        b.chain.revert = bsc.TRIES
        await b.run(cb(BUYER, "w:out"), msg(BUYER, "5"), msg(BUYER, "0xdD2FD4581271e230360230F9337D5c0430Bf44C0"), cb(BUYER, "wb:go"))
        for _ in range(bsc.TRIES):
            await bsc_tick(b)
            b.chain.mine()
            await bsc_tick(b)
        log = await b.deliver()
        assert any("сеть откатила транзакцию" in t for t in log)
        assert any("Сеть 3 раза откатила перевод" in t and "возвращены пользователю" in t for t in log)
        assert (await user(BUYER)).balance == D(5)
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


async def wd(b, uid, amount):
    await b.run(cb(uid, "w:out"), msg(uid, str(amount)), msg(uid, "0xdD2FD4581271e230360230F9337D5c0430Bf44C0"), cb(uid, "wb:go"))


def test_withdrawals_wait_for_hot_wallet_funds_and_go_out_in_order(go):
    async def fn(b):
        from bot import tasks
        from bot.models import Withdrawal
        await ready(b)
        async with models.Session() as s:
            (await s.get(User, BUYER)).balance = D(50)
            (await s.get(User, SELLER)).balance = D(50)
            await s.commit()
        from tests.harness import bsc_tick
        dest = "0xdD2FD4581271e230360230F9337D5c0430Bf44C0"
        b.chain.fund_hot(usdt="0.5")
        await wd(b, BUYER, 10)
        assert (await user(BUYER)).balance == D(40)  # debited, waiting — not refused
        await bsc_tick(b)
        await b.run(cb(BUYER, "w"))
        assert "в очереди" in plain(b.session.last(BUYER)) and "w:qc:1" in b.session.buttons(BUYER)
        await wd(b, SELLER, 20)
        await wd(b, BUYER, 5)
        await bsc_tick(b)  # still nothing on the hot wallet: all three wait, admins are told
        await bsc_tick(b)
        assert not b.chain.pool
        assert len([t for t in await b.deliver() if "ждёт денег" in t]) == 1  # once, not every 3 s

        b.chain.fund_hot(usdt="24.5")  # 25: enough for #1 (9) but not #1 + #2 (19): strictly in order
        await bsc_tick(b)
        b.chain.mine()
        assert b.chain.usdt_of(dest) == D(9)  # #3 (4) must not overtake #2
        b.chain.fund_hot(usdt="100")
        for _ in range(3):
            await bsc_tick(b)
            b.chain.mine()
        await bsc_tick(b)
        assert b.chain.usdt_of(dest) == D(9 + 19 + 4)  # each minus the fixed 1 USDT
        assert "Вывод #3 выполнен" in plain(b.session.last(BUYER))
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
        await wd(b, BUYER, 10)
        await b.run(cb(BUYER, "w:qc:1"))
        assert (await user(BUYER)).balance == D(50) and "отменён" in plain(b.session.last(BUYER))
        await b.run(cb(BUYER, "w:qc:1"))
        assert "отменить нельзя" in b.session.alerts()[-1]  # nothing is refunded twice
        assert (await user(BUYER)).balance == D(50)
        b.chain.fund_hot(usdt="100")
        await tasks.bsc_tick(b.bot, 0)
        assert not b.chain.pool and not b.chain.sends
    go(fn)


def test_manual_link_in_help_and_texts(go):
    async def fn(b):
        url = "https://telegra.ph/Strait-Pay--P2P-obmen-USDT--RUB-v-Telegram-09-27"
        await ready(b)
        await b.run(cb(BUYER, "info"), cb(BUYER, "info:g"))
        assert url in b.session.buttons(BUYER)  # «Памятка продавца» among the guides
        assert f'<a href="{url}">отдельной статье</a>' in b.session.last(BUYER)  # and a link hidden in the text
        for who, screen in ((SELLER, "sl"), (SELLER, "sl:add"), (BUYER, "om")):
            await b.run(cb(who, screen))
            assert f'href="{url}"' in b.session.last(who), screen
        await b.run(cb(ADMIN, "as:manual_url"), msg(ADMIN, "http://bad"))
        assert "https://" in plain(b.session.last(ADMIN))  # only https links
        await b.run(msg(ADMIN, "-"))
        await b.run(cb(BUYER, "info:g"))
        assert url not in b.session.buttons(BUYER) and url not in b.session.last(BUYER)  # link removed everywhere
    go(fn)
