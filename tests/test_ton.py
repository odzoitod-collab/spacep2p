"""USDT on TON: personal deposit and debt addresses credited by transaction hash, collecting them to the hot wallet,
withdrawals paid from it in order — and never twice, whatever the network does."""
import time
from datetime import timedelta
from decimal import Decimal as D

from sqlalchemy import func, select

from bot import models
from bot.models import Deposit, Event, Ledger, Operator, TonAddress, TonTransfer, Withdrawal
from bot.services import money, settings, ton
from tests.harness import cb, msg, plain, ton_cycle
from tests.test_scenarios import ADMIN, BUYER, user

DEST = "UQBvW8Z5huBkMJYdnfAEM5JqTNkuWX3diqYENkWsIL0XglxD"  # any valid address stands for the user's own wallet


async def rows(model, *where):
    async with models.Session() as s:
        return list((await s.scalars(select(model).where(*where).order_by(model.id))).all())


async def give(uid, amount):
    async with models.Session() as s:
        await money.add(s, uid, D(amount), "admin", "adj:0")
        await s.commit()


async def withdraw(b, amount, memo=None, uid=BUYER, to=DEST):
    await b.run(cb(uid, "w:out"), msg(uid, to))
    await b.run(msg(uid, memo) if memo else cb(uid, "w:nomemo"))
    await b.run(msg(uid, str(amount)), cb(uid, "w:go"))


def test_addresses_are_personal_stable_and_derived_from_the_seed():
    a, d, h = ton.address("deposit:5"), ton.address("debt:5"), ton.hot_address()
    assert len({a, d, h, ton.address("deposit:6")}) == 4 and a == ton.address("deposit:5")
    assert h == ton.address("gas")  # the hot wallet keeps the historic gas-wallet address
    assert ton.friendly(a).startswith("UQ") and ton.raw(ton.friendly(a)) == a


def test_address_parsing():
    assert ton.parse_address(DEST) == DEST
    assert ton.parse_address("EQBvW8Z5huBkMJYdnfAEM5JqTNkuWX3diqYENkWsIL0XggGG").startswith("EQ")  # bounceable kept
    for bad in ("", "hello", "TQ3x" * 12, DEST + "x", "-1:" + "ab" * 32, "0x" + "ab" * 20):
        assert ton.parse_address(bad) is None, bad


def test_deposit_is_credited_once_by_tx_hash_only_for_real_usdt(go):
    async def fn(b):
        await b.run(msg(BUYER, "/start"), cb(BUYER, "w"), cb(BUYER, "w:in"))
        text = plain(b.session.last(BUYER))
        addr = ton.friendly(ton.address(f"deposit:{BUYER}"))
        assert addr in text and "USDT (Tether) в сети TON" in text and "Memo не нужен" in text
        copies = [x.copy_text.text for m in b.session.calls if getattr(m, "chat_id", None) == BUYER
                  and getattr(m, "reply_markup", None) for row in m.reply_markup.inline_keyboard for x in row
                  if x.copy_text]
        assert addr in copies
        h = b.chain.pay(BUYER, "100")
        b.chain.pay(BUYER, "500", master=ton.raw("EQBvW8Z5huBkMJYdnfAEM5JqTNkuWX3diqYENkWsIL0XggGG"))  # fake "USDT"
        b.chain.pay(BUYER, "500", aborted=True)  # failed transaction
        await ton_cycle(b)
        await ton_cycle(b)  # the same transfers again: nothing twice
        assert (await user(BUYER)).balance == D("98.5")  # 1.5% deposit fee
        assert "Баланс пополнен на 98.5 USDT" in plain(b.session.last(BUYER))
        dep = (await rows(Deposit))[0]
        assert (dep.tx_hash, dep.amount, dep.credit, dep.status) == (h, D(100), D("98.5"), "paid")
        assert dep.link.endswith(h) and "tonviewer.com" in dep.link
        assert len(await rows(Deposit)) == 1
        kinds = {r.kind: r.delta for r in await rows(Ledger, Ledger.ref == f"dep:{dep.id}")}
        assert kinds == {"deposit": D("98.5"), "deposit_fee": D("1.5")}
        assert (await rows(TonAddress))[0].unswept == D(100)
        delivered = await b.deliver()
        assert any("Зачислено 98.5 USDT" in t and h in t for t in delivered)  # the admin sees amount and tx hash
    go(fn)


def test_small_and_historic_transfers_are_not_credited(go):
    async def fn(b):
        await b.run(msg(BUYER, "/start"), cb(BUYER, "w:in"))
        async with models.Session() as s:
            await settings.put(s, "deposit_min", "5")
            await settings.put(s, ton.CURSOR, str(int(time.time()) - 7200))
            await s.commit()
        b.chain.pay(BUYER, "1")
        b.chain.pay(BUYER, "50")
        b.chain.transfers[-1]["transaction_now"] = int(time.time()) - 3600  # before the bot gave this address out
        await ton_cycle(b)
        assert (await user(BUYER)).balance == 0
        dep = (await rows(Deposit))[0]
        assert dep.status == "small" and dep.credit == 0 and len(await rows(Deposit)) == 1
        assert "меньше минимума" in plain(b.session.last(BUYER))
    go(fn)


def test_manual_check_credits_at_once(go):
    async def fn(b):
        await b.run(msg(BUYER, "/start"), cb(BUYER, "w:in"), cb(BUYER, "w:chk"))
        assert "Новых поступлений пока нет" in b.session.alerts()[-1]
        b.chain.pay(BUYER, "10")
        await b.run(cb(BUYER, "w:chk"))
        assert (await user(BUYER)).balance == D("9.85") and "Зачислено 9.85 USDT" in plain(b.session.last(BUYER))
        await ton_cycle(b)
        assert (await user(BUYER)).balance == D("9.85")
    go(fn)


def test_operator_debt_address_repays_without_fee_excess_to_balance(go):
    async def fn(b):
        await b.run(msg(BUYER, "/start"))
        async with models.Session() as s:
            s.add(Operator(user_id=BUYER, active=True, debt=D(70)))
            await s.commit()
        await b.run(cb(BUYER, "op"), cb(BUYER, "op:pay"))
        addr = ton.friendly(ton.address(f"debt:{BUYER}"))
        assert addr in plain(b.session.last(BUYER)) and addr != ton.friendly(ton.address(f"deposit:{BUYER}"))
        b.chain.pay(BUYER, "100", purpose="debt")
        await ton_cycle(b)
        async with models.Session() as s:
            assert (await s.get(Operator, BUYER)).debt == 0
        assert (await user(BUYER)).balance == D(30)  # no fee; above the debt — to the balance
        assert "Долг погашен: 100 USDT" in plain(b.session.last(BUYER))
        dep = (await rows(Deposit))[0]
        assert dep.purpose == "debt" and dep.credit == D(100)
    go(fn)


def test_addresses_are_collected_to_the_hot_wallet_gas_first(go):
    async def fn(b):
        await b.run(msg(BUYER, "/start"), cb(BUYER, "w:in"))
        owner, hot = ton.address(f"deposit:{BUYER}"), ton.hot_address()
        b.chain.pay(BUYER, "3")
        b.chain.fund_hot(gas="5")
        await ton_cycle(b)
        assert b.chain.sent == []  # 3 < ton_sweep_min (5): it waits on the user's address
        b.chain.pay(BUYER, "7")
        await ton_cycle(b)
        assert b.chain.sent == [("gas", "TON", owner, ton.GAS_TOPUP, None, 1)]  # TON for the fee first
        await ton_cycle(b)
        assert len(b.chain.sent) == 1  # the gas was sent just now: nothing more meanwhile
        async with models.Session() as s:
            (await s.get(TonTransfer, 1)).created_at = models.now() - timedelta(minutes=6)
            await s.commit()
        await ton_cycle(b)
        assert b.chain.sent[-1][:4] == (f"deposit:{BUYER}", "USDT", hot, D(10))
        await ton_cycle(b)
        sweep = (await rows(TonTransfer))[-1]
        assert (sweep.kind, sweep.status) == ("sweep", "done") and sweep.tx_hash
        assert (await rows(TonAddress))[0].unswept == 0 and b.chain.usdt[hot] == D(10)
        assert len(b.chain.sent) == 2
    go(fn)


def test_withdrawal_paid_from_hot_wallet_with_memo_and_tx_hash(go):
    async def fn(b):
        await b.run(msg(BUYER, "/start"))
        await give(BUYER, 100)
        b.chain.fund_hot(usdt="500")
        await withdraw(b, 50, memo="777123")
        wd = (await rows(Withdrawal))[0]
        assert (wd.status, wd.amount, wd.fee, wd.address, wd.memo) == ("queued", D(50), D("1.75"), DEST, "777123")
        assert (await user(BUYER)).balance == D(50) and "Вывод #1 принят" in plain(b.session.last(BUYER))
        assert ton.wake.is_set()  # the cycle is woken at once
        await ton_cycle(b)
        assert b.chain.sent == [("gas", "USDT", ton.raw(DEST), D("48.25"), "777123", 1)]
        wd = (await rows(Withdrawal))[0]
        assert wd.status == "done" and wd.tx_hash and wd.link.endswith(wd.tx_hash) and wd.transfer_id == 1
        assert "Вывод #1 выполнен" in plain(b.session.last(BUYER))
        async with models.Session() as s:
            fee = await s.scalar(select(Ledger.delta).where(Ledger.kind == "withdraw_fee"))
        assert fee == D("1.75")
        await ton_cycle(b)
        assert len(b.chain.sent) == 1
        delivered = await b.deliver()
        assert any("Вывод выполнен" in t and wd.tx_hash in t for t in delivered)
    go(fn)


def test_withdrawals_queue_when_hot_wallet_is_short_and_go_in_order(go):
    async def fn(b):
        await b.run(msg(BUYER, "/start"))
        await give(BUYER, 300)
        b.chain.fund_hot(usdt="60")
        await withdraw(b, 100)
        await withdraw(b, 20)
        await ton_cycle(b)
        assert b.chain.sent == []  # the first one does not fit: the small one does not overtake it
        assert [w.status for w in await rows(Withdrawal)] == ["queued", "queued"]
        await b.run(cb(BUYER, "w"))
        assert "место 2, перед вами 97.5 USDT" in plain(b.session.last(BUYER))
        async with models.Session() as s:
            assert await s.scalar(select(Event.id).where(Event.ref == "app:ton", Event.kind == "payout_queue"))
        b.chain.fund_hot(usdt="100")
        await ton_cycle(b)
        await ton_cycle(b)
        assert [w.status for w in await rows(Withdrawal)] == ["done", "done"]
        assert [x[3] for x in b.chain.sent] == [D("97.5"), D("18.7")]
    go(fn)


def test_cancel_only_before_anything_was_signed(go):
    async def fn(b):
        await b.run(msg(BUYER, "/start"))
        await give(BUYER, 100)
        await withdraw(b, 40)
        await b.run(cb(BUYER, "w:qc:1"))
        assert (await user(BUYER)).balance == D(100) and (await rows(Withdrawal))[0].status == "cancelled"
        await withdraw(b, 40)
        async with models.Session() as s:
            (await s.get(Withdrawal, 2)).transfer_id = 99  # a message was signed for it once
            await s.commit()
        await b.run(cb(BUYER, "w:qc:2"))
        assert "отменить нельзя" in b.session.alerts()[-1] and (await user(BUYER)).balance == D(60)
    go(fn)


def test_no_gas_no_payout(go):
    async def fn(b):
        await b.run(msg(BUYER, "/start"))
        await give(BUYER, 100)
        b.chain.fund_hot(usdt="500", gas="0.1")
        await withdraw(b, 50)
        await ton_cycle(b)
        assert b.chain.sent == [] and (await rows(Withdrawal))[0].status == "queued"
        async with models.Session() as s:
            assert await s.scalar(select(Event.id).where(Event.ref == "app:ton", Event.kind == "gas_empty"))
    go(fn)


async def age(tr_id, seconds):
    """valid_until of a message is long gone."""
    async with models.Session() as s:
        tr = await s.get(TonTransfer, tr_id)
        tr.valid_until -= seconds
        await s.commit()


def test_lost_message_expires_and_is_signed_again_once(go):
    async def fn(b):
        await b.run(msg(BUYER, "/start"))
        await give(BUYER, 100)
        b.chain.fund_hot(usdt="500")
        b.chain.apply = False  # the broadcast never reaches the network
        await withdraw(b, 50)
        await ton_cycle(b)
        assert (await rows(Withdrawal))[0].status == "sending" and b.chain.sent == []
        await ton_cycle(b)
        assert len(await rows(TonTransfer)) == 1  # still in flight: never a second message meanwhile
        await age(1, ton.TTL + ton.DEAD_AFTER + 1)
        b.chain.apply = True
        await ton_cycle(b)  # dead for good -> back to the queue -> signed again with the same seqno
        trs = await rows(TonTransfer)
        assert [t.status for t in trs] == ["expired", "done"] and trs[0].seqno == trs[1].seqno == 0
        assert len(b.chain.sent) == 1 and (await rows(Withdrawal))[0].status == "done"
    go(fn)


def test_message_taken_for_dead_but_executed_is_restored_not_paid_twice(go):
    async def fn(b):
        await b.run(msg(BUYER, "/start"))
        await give(BUYER, 100)
        b.chain.fund_hot(usdt="500")
        b.chain.index = False  # executed, but the indexer has not shown the transfer yet
        await withdraw(b, 50)
        await ton_cycle(b)
        assert len(b.chain.sent) == 1
        async with models.Session() as s:  # a stale read once made the bot think it was never executed
            tr = await s.get(TonTransfer, 1)
            tr.status = "expired"
            (await s.get(Withdrawal, 1)).status = "queued"
            await s.commit()
        await ton_cycle(b)
        assert len(b.chain.sent) == 1  # the chain seqno gave it away: restored, nothing signed on top
        assert (await rows(TonTransfer))[0].status == "sent" and (await rows(Withdrawal))[0].status == "sent"
        b.chain.index = True
        b.chain._transfer(ton.hot_address(), ton.raw(DEST), D("48.25"), query_id=1)
        await ton_cycle(b)
        assert (await rows(Withdrawal))[0].status == "done" and len(b.chain.sent) == 1
        async with models.Session() as s:
            assert await s.scalar(select(Event.id).where(Event.kind == "restored"))
    go(fn)


def test_crash_between_record_and_broadcast(go):
    async def fn(b):
        await b.run(msg(BUYER, "/start"))
        await give(BUYER, 100)
        b.chain.fund_hot(usdt="500")
        await withdraw(b, 50)
        real = b.chain.broadcast

        async def crash(boc):
            raise ton.ChainError("process killed")
        b.chain.broadcast = crash
        await ton_cycle(b)
        assert (await rows(TonTransfer))[0].status == "sending" and b.chain.sent == []
        b.chain.broadcast = real
        await ton_cycle(b)
        assert len(await rows(TonTransfer)) == 1  # not known to be dead yet: wait, never a blind second send
        await age(1, ton.TTL + ton.DEAD_AFTER + 1)
        await ton_cycle(b)
        assert len(b.chain.sent) == 1 and (await rows(Withdrawal))[0].status == "done"
    go(fn)


def test_aborted_jetton_transfer_is_retried_then_refunded(go):
    async def fn(b):
        await b.run(msg(BUYER, "/start"))
        await give(BUYER, 100)
        b.chain.fund_hot(usdt="500")
        b.chain.abort = True
        await withdraw(b, 50)
        for _ in range(ton.PAYOUT_TRIES + 1):
            await ton_cycle(b)
        assert [t.status for t in await rows(TonTransfer)] == ["failed"] * ton.PAYOUT_TRIES
        assert (await rows(Withdrawal))[0].status == "failed" and (await user(BUYER)).balance == D(100)
        assert "не выполнен" in plain(b.session.last(BUYER))
    go(fn)


def test_unknown_withdrawal_is_decided_by_an_owner(go):
    async def fn(b):
        await b.run(msg(BUYER, "/start"), msg(ADMIN, "/start"))
        await give(BUYER, 200)
        b.chain.fund_hot(usdt="500")
        b.chain.index = False
        await withdraw(b, 50)
        await withdraw(b, 50)
        await ton_cycle(b)
        await ton_cycle(b)
        async with models.Session() as s:
            for tr in (await s.scalars(select(TonTransfer))).all():
                tr.created_at = models.now() - ton.LOST - timedelta(minutes=1)
            await s.commit()
        await ton_cycle(b)
        assert [w.status for w in await rows(Withdrawal)] == ["unknown", "unknown"]
        await b.run(cb(ADMIN, "awv:1"))
        assert "wk:done:1" in b.session.buttons(ADMIN) and "Tonviewer" in plain(b.session.last(ADMIN))
        await b.run(cb(ADMIN, "wk:rf:1"))
        assert "дважды" in plain(b.session.last(ADMIN)) and (await rows(Withdrawal))[0].status == "unknown"
        await b.run(cb(ADMIN, "wk:rf2:1"), cb(ADMIN, "wk:done2:2"))
        assert [w.status for w in await rows(Withdrawal)] == ["failed", "done"]
        assert (await user(BUYER)).balance == D(150)
        await b.run(cb(BUYER, "w:qc:1"))
        assert (await user(BUYER)).balance == D(150)  # a refunded one cannot be refunded again
    go(fn)


def test_network_down_changes_nothing_and_alerts(go):
    async def fn(b):
        await b.run(msg(BUYER, "/start"))
        await give(BUYER, 100)
        b.chain.fund_hot(usdt="500")
        await withdraw(b, 50)
        b.chain.down = True
        rep = await ton_cycle(b)
        assert rep.errors and (await rows(Withdrawal))[0].status == "queued" and not await rows(TonTransfer)
        async with models.Session() as s:
            assert await s.scalar(select(Event.id).where(Event.ref == "app:ton", Event.kind == "chain_error"))
        b.chain.down = False
        await ton_cycle(b)
        assert (await rows(Withdrawal))[0].status == "done"
    go(fn)


def test_withdrawal_address_and_amount_checks(go):
    async def fn(b):
        await b.run(msg(BUYER, "/start"))
        await give(BUYER, 100)
        await b.run(cb(BUYER, "w:out"), msg(BUYER, "TXYZ1234567890"))
        assert "Это не адрес TON" in plain(b.session.last(BUYER))
        await b.run(msg(BUYER, ton.friendly(ton.hot_address())))
        assert "Это адрес Strait Pay" in plain(b.session.last(BUYER))
        await b.run(msg(BUYER, DEST), cb(BUYER, "w:nomemo"), msg(BUYER, "2"))
        assert "Минимум 3 USDT" in plain(b.session.last(BUYER))
        await b.run(msg(BUYER, "500"))
        assert "Доступно только 100 USDT" in plain(b.session.last(BUYER))
        await b.run(cb(BUYER, "w:all"))
        assert "Придёт: 97.5 USDT" in plain(b.session.last(BUYER))
        assert not await rows(Withdrawal)
    go(fn)


def test_admin_ton_screen_key_and_run_now(go, monkeypatch):
    async def fn(b):
        checked = []

        async def check_key(key):
            checked.append(key)
            if key.startswith("bad"):
                raise ton.ChainError("toncenter: ключ API не принят")
        monkeypatch.setattr(ton, "check_key", check_key)
        monkeypatch.setattr(ton, "start", _noop)
        await b.run(msg(ADMIN, "/start"), msg(BUYER, "/start"))
        b.chain.fund_hot(usdt="12.5", gas="3")
        await b.run(cb(ADMIN, "aton"))
        text = plain(b.session.last(ADMIN))
        assert ton.friendly(ton.hot_address()) in text and "3 TON" in text and "12.5 USDT" in text
        await b.run(cb(ADMIN, "aton:key"), msg(ADMIN, "bad" + "x" * 30))
        assert "Toncenter не принял ключ" in plain(b.session.last(ADMIN)) and not settings.raw(ton.API_KEY)
        await b.run(msg(ADMIN, "good" + "y" * 30))
        assert settings.raw(ton.API_KEY) == "good" + "y" * 30 and "Ключ принят: …yyyy" in plain(b.session.last(ADMIN))
        b.chain.pay(BUYER, "10")
        await b.run(cb(BUYER, "w:in"), cb(ADMIN, "aton:go"))
        assert "зачислено поступлений 1" in plain(b.session.last(ADMIN))
        async with models.Session() as s:
            assert await s.scalar(select(func.count()).select_from(models.Audit).where(
                models.Audit.action.in_(("ton_key", "ton_cycle")))) == 2
    go(fn)


async def _noop():
    pass


def test_owner_sends_from_hot_wallet(go):
    async def fn(b):
        await b.run(msg(ADMIN, "/start"))
        b.chain.fund_hot(usdt="100", gas="5")
        await b.run(cb(ADMIN, "aton:out"), cb(ADMIN, "aton:out:USDT"), msg(ADMIN, DEST), msg(ADMIN, "40"),
                    cb(ADMIN, "aton:out:go"))
        assert b.chain.sent == [("gas", "USDT", ton.raw(DEST), D(40), None, 1)]
        assert "Отправлено: 40 USDT" in plain(b.session.last(ADMIN))
        await b.run(cb(ADMIN, "aton:out"), cb(ADMIN, "aton:out:USDT"), msg(ADMIN, DEST), msg(ADMIN, "100"),
                    cb(ADMIN, "aton:out:go"))
        assert len(b.chain.sent) == 1 and "свободно" in plain(b.session.last(ADMIN))
    go(fn)


def test_xrocket_leftovers_are_retired_without_losing_money(go):
    async def fn(b):
        await b.run(msg(BUYER, "/start"), msg(ADMIN, "/start"))
        async with models.Session() as s:
            u = await s.get(models.User, BUYER)
            u.balance = D(10)
            s.add_all([Withdrawal(user_id=BUYER, amount=D(30), fee=D(1), status="queued", method="xrocket"),
                       Withdrawal(user_id=BUYER, amount=D(20), fee=D(1), status="sent", method="chain", network="TRX"),
                       Deposit(user_id=BUYER, invoice_id="inv1", amount=D(50), credit=D(49), status="active"),
                       models.Setting(key="xrocket_token", value="secret-token")])
            await s.commit()
        async with models.Session() as s:
            refunded = await ton.retire_legacy(s)
            assert [w.id for w in refunded] == [1]
            assert await ton.retire_legacy(s) == []  # once
            assert await s.get(models.Setting, "xrocket_token") is None
        assert (await user(BUYER)).balance == D(40)
        assert [w.status for w in await rows(Withdrawal)] == ["cancelled", "sent"]
        assert (await rows(Deposit))[0].status == "expired"
        assert len([t for t in await b.deliver() if "xRocket отключён" in t and "неизвестным итогом — 1" in t]) == 1
        await b.run(cb(ADMIN, "awv:2"))
        assert "приложении xRocket" in plain(b.session.last(ADMIN)) and "wk:rf:2" in b.session.buttons(ADMIN)
        await b.run(cb(ADMIN, "wk:rf2:2"))
        assert (await user(BUYER)).balance == D(60) and (await rows(Withdrawal))[1].status == "failed"
    go(fn)
