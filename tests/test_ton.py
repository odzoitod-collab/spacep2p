"""USDT on TON: personal deposit addresses, crediting by tx hash, gas top-up and sweep to the admin's address."""
from datetime import timedelta
from decimal import Decimal as D

from sqlalchemy import select

from bot import models, tasks
from bot.models import Event, Ledger, TonDeposit, TonOp, TonWallet, Withdrawal
from bot.services import money, settings, ton, xrocket
from tests.harness import cb, msg, plain
from tests.test_scenarios import ADMIN, BUYER, user

TARGET = "UQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_p0p"  # any valid address stands for the admin's wallet


async def cycle(b):
    await tasks.ton_cycle(b.bot)


async def ops():
    async with models.Session() as s:
        return list((await s.scalars(select(TonOp).order_by(TonOp.id))).all())


async def set_target(value=TARGET, minimum="1"):
    async with models.Session() as s:
        await settings.put(s, "ton_sweep_address", value)
        await settings.put(s, "ton_sweep_min", minimum)
        await s.commit()


def test_addresses_are_personal_and_stable():
    a, b = ton.deposit_address(1), ton.deposit_address(2)
    assert a != b and a == ton.deposit_address(1) and ton.gas_address() not in (a, b)
    assert ton.friendly(a).startswith("UQ")


def test_user_gets_address_and_deposit_is_credited_once(go):
    async def fn(b):
        await b.run(msg(BUYER, "/start"), cb(BUYER, "w"), cb(BUYER, "w:ton"))
        text = plain(b.session.last(BUYER))
        addr = ton.friendly(ton.deposit_address(BUYER))
        assert addr in text and "только USDT (Tether USD) в сети TON" in text
        copies = [x.copy_text.text for m in b.session.calls if getattr(m, "chat_id", None) == BUYER
                  and getattr(m, "reply_markup", None) for row in m.reply_markup.inline_keyboard for x in row
                  if x.copy_text]
        assert addr in copies
        h = b.chain.pay(BUYER, "25.5")
        b.chain.pay(BUYER, "100", n=2, master="0:" + "EE" * 32)  # fake "USDT" from another jetton master
        b.chain.pay(BUYER, "100", n=3, aborted=True)  # failed transaction
        await cycle(b)
        await cycle(b)  # the same transfers again: nothing is credited twice
        assert (await user(BUYER)).balance == D("25.5")
        assert "Баланс пополнен на 25.5 USDT" in plain(b.session.last(BUYER))
        async with models.Session() as s:
            dep = await s.scalar(select(TonDeposit))
            assert dep.tx_hash == h and dep.amount == D("25.5")
            assert await s.scalar(select(Ledger.kind).where(Ledger.ref == f"tdep:{dep.id}")) == "ton_deposit"
            assert (await s.get(TonWallet, BUYER)).need_sweep
        delivered = await b.deliver()
        assert any("Пополнение USDT TON #1" in t and h in t for t in delivered)  # admin sees amount and tx hash
    go(fn)


def test_manual_check_credits_and_is_rate_limited(go):
    async def fn(b):
        await b.run(msg(BUYER, "/start"), cb(BUYER, "w:ton"))
        await b.run(cb(BUYER, "w:tonchk"))
        assert "Новых поступлений пока нет" in b.session.alerts()[-1]
        b.chain.pay(BUYER, "10")
        await b.run(cb(BUYER, "w:tonchk"))
        assert "через 20 секунд" in b.session.alerts()[-1] and (await user(BUYER)).balance == 0
        from bot.handlers import ton_wallet
        ton_wallet._checked.clear()
        await b.run(cb(BUYER, "w:tonchk"))
        assert (await user(BUYER)).balance == D(10) and "Зачислено 10 USDT" in plain(b.session.last(BUYER))
    go(fn)


def test_gas_then_sweep_to_admin_address(go):
    async def fn(b):
        await b.run(msg(BUYER, "/start"), cb(BUYER, "w:ton"))
        owner = ton.deposit_address(BUYER)
        b.chain.pay(BUYER, "50")
        await cycle(b)
        assert b.chain.sent == []  # no target yet: nothing moves, admins are told
        async with models.Session() as s:
            assert await s.scalar(select(Event.id).where(Event.ref == "app:ton", Event.kind == "no_target"))
        await set_target()
        b.chain.ton[ton.gas_address()] = D(5)
        await cycle(b)
        assert b.chain.sent == [("gas", owner, ton.GAS_TOPUP)]  # the deposit wallet has no TON for fees yet
        await cycle(b)
        assert len(b.chain.sent) == 1  # gas still on its way: nothing is sent twice
        b.chain.ton[owner] = D("0.1")
        await cycle(b)
        assert b.chain.sent[-1] == ("usdt", BUYER, D(50), TARGET)
        await cycle(b)
        assert len(b.chain.sent) == 2  # the jetton transfer is in flight
        b.chain.usdt[owner] = D(0)
        b.chain.out[owner] = {"transaction_hash": "zMzMzMzMzMzMzMzMzMzMzMzMzMzMzMzMzMzMzMzMzMw=",
                              "transaction_now": int(models.now().timestamp()), "transaction_aborted": False}
        await cycle(b)
        sweep = (await ops())[-1]
        assert (sweep.kind, sweep.status, sweep.tx_hash) == ("sweep", "done", "cc" * 32)
        async with models.Session() as s:
            assert not (await s.get(TonWallet, BUYER)).need_sweep
        delivered = await b.deliver()
        assert any("Автоперевод USDT TON" in t and "выполнен" in t and "cc" * 32 in t for t in delivered)
    go(fn)


def test_small_amounts_wait_and_empty_gas_stops(go):
    async def fn(b):
        await b.run(msg(BUYER, "/start"), cb(BUYER, "w:ton"))
        await set_target(minimum="10")
        b.chain.pay(BUYER, "3")
        await cycle(b)
        assert b.chain.sent == []  # below the minimum: waits on the user's address
        b.chain.pay(BUYER, "8", n=2)
        await cycle(b)  # 11 USDT now, but the gas wallet is empty
        assert b.chain.sent == []
        async with models.Session() as s:
            assert await s.scalar(select(Event.id).where(Event.ref == "app:ton", Event.kind == "gas_empty"))
        assert (await user(BUYER)).balance == D(11)
    go(fn)


def test_stuck_sweep_is_retried_after_timeout(go):
    async def fn(b):
        await b.run(msg(BUYER, "/start"), cb(BUYER, "w:ton"))
        await set_target()
        owner = ton.deposit_address(BUYER)
        b.chain.pay(BUYER, "20")
        b.chain.ton[owner] = D(1)
        await cycle(b)
        async with models.Session() as s:
            (await s.get(TonOp, 1)).created_at = models.now() - timedelta(minutes=11)
            await s.commit()
        await cycle(b)
        assert [o.status for o in await ops()] == ["failed", "sent"] and len(b.chain.sent) == 2
    go(fn)


def test_deposit_during_sweep_check_keeps_flag(go):
    async def fn(b):
        await b.run(msg(BUYER, "/start"), cb(BUYER, "w:ton"))
        checked = models.now() - timedelta(seconds=5)
        b.chain.pay(BUYER, "5")
        await cycle(b)  # credited after `checked`
        async with models.Session() as s:
            await ton._clear(s, BUYER, checked)
            await s.commit()
            assert (await s.get(TonWallet, BUYER)).need_sweep
    go(fn)


def test_admin_sets_address_and_sees_gas_wallet(go):
    async def fn(b):
        await b.run(msg(ADMIN, "/start"), cb(ADMIN, "atn"))
        assert "Автоперевод поступлений: не задан" in plain(b.session.last(ADMIN))
        await b.run(cb(ADMIN, "atn:gas"))
        text = plain(b.session.last(ADMIN))
        assert ton.friendly(ton.gas_address()) in text and "Как пополнить" in text and "🔴" in text  # empty
        await b.run(cb(ADMIN, "atn:sw"), cb(ADMIN, "atn:x"))
        assert settings.get("ton_sweep_address") == "xrocket" and "на баланс приложения xRocket" in plain(
            b.session.last(ADMIN))
        await b.run(cb(ADMIN, "as:ton_sweep_address"), msg(ADMIN, "not an address"))
        assert "Это не адрес TON" in plain(b.session.last(ADMIN))
        await b.run(msg(ADMIN, ton.friendly(ton.gas_address())))
        assert "газ-кошелёк бота" in plain(b.session.last(ADMIN))
        await b.run(msg(ADMIN, "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"))
        assert settings.get("ton_sweep_address") == TARGET
        assert any("Адрес автоперевода USDT TON изменён" in t and TARGET in t for t in await b.deliver())
        b.chain.pay(BUYER, "7")
        await b.run(msg(BUYER, "/start"), cb(BUYER, "w:ton"), cb(ADMIN, "atn:run"), cb(ADMIN, "atnd"), cb(ADMIN, "atd:1"))
        assert "Зачислено: 7 USDT" in plain(b.session.last(ADMIN))
        await b.run(cb(ADMIN, "atno"), cb(ADMIN, "aev:tdep:1"))
        assert "Пополнение USDT TON" in plain(b.session.last(ADMIN))
    go(fn)


def test_deposit_arriving_during_sweep_is_swept_next(go):
    async def fn(b):
        await b.run(msg(BUYER, "/start"), cb(BUYER, "w:ton"))
        await set_target()
        owner = ton.deposit_address(BUYER)
        b.chain.ton[owner] = D(1)
        b.chain.pay(BUYER, "30")
        await cycle(b)
        assert b.chain.sent[-1] == ("usdt", BUYER, D(30), TARGET)
        b.chain.usdt[owner] = D(0)  # the sweep went through…
        b.chain.out[owner] = {"transaction_hash": "3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d0=",
                              "transaction_now": int(models.now().timestamp()), "transaction_aborted": False}
        b.chain.pay(BUYER, "4", n=2)  # …and a new deposit came right after
        await cycle(b)
        first, second = await ops()
        assert first.status == "done" and second.status == "sent" and b.chain.sent[-1] == ("usdt", BUYER, D(4), TARGET)
        assert (await user(BUYER)).balance == D(34)
    go(fn)


CLIENT = "UQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_p0p"


async def fund(b, uid=BUYER, amount=D(100)):
    async with models.Session() as s:
        await money.add(s, uid, amount, "deposit", "dep:0")
        await s.commit()


async def withdrawal(wid=1):
    async with models.Session() as s:
        return await s.get(Withdrawal, wid)


async def request_ton_withdrawal(b, amount="50", memo=None, address=CLIENT):
    await b.run(cb(BUYER, "w:out"), cb(BUYER, "w:wdt"), msg(BUYER, address))
    await b.run(msg(BUYER, memo) if memo else cb(BUYER, "w:wdt:nomemo"))
    await b.run(msg(BUYER, amount), cb(BUYER, "w:wdt:go"))


def test_withdraw_to_ton_wallet_goes_through_xrocket(go):
    async def fn(b):
        await b.run(msg(BUYER, "/start"))
        await fund(b)
        await request_ton_withdrawal(b, memo="123456")
        assert settings.dec("ton_withdraw_fee") == D(2)
        assert b.rocket.withdrawal_calls == [("wd-1", CLIENT, D(48), "123456")]  # 50 debited, 48 sent, fee 2
        assert b.chain.sent == []  # nothing from our own wallets: xRocket pays from the app balance
        assert "Вывод #1 принят" in plain(b.session.last(BUYER))
        assert (await withdrawal()).status == "sent" and (await user(BUYER)).balance == D(50)
        b.rocket.withdrawals["wd-1"].update(status="COMPLETED", txHash="ab" * 32, txLink="https://tonviewer.com/tx")
        await tasks.sync_ton_withdrawals(b.bot)
        wd = await withdrawal()
        assert (wd.status, wd.tx_hash, wd.link) == ("done", "ab" * 32, "https://tonviewer.com/tx")
        assert "Вывод #1 выполнен" in plain(b.session.last(BUYER))
        async with models.Session() as s:  # the 2 USDT fee is platform income, recorded once
            fees = (await s.scalars(select(Ledger.delta).where(Ledger.kind == "withdraw_fee"))).all()
            assert fees == [D(2)]
        await tasks.sync_ton_withdrawals(b.bot)
        assert len(b.rocket.withdrawal_calls) == 1
    go(fn)


def test_ton_withdrawal_input_checks(go):
    async def fn(b):
        await b.run(msg(BUYER, "/start"), cb(BUYER, "w:ton"))
        await fund(b, amount=D(10))
        await b.run(cb(BUYER, "w:out"), cb(BUYER, "w:wdt"), msg(BUYER, "TRx9c3...не тон"))
        assert "Это не адрес TON" in plain(b.session.last(BUYER))
        await b.run(msg(BUYER, ton.friendly(ton.deposit_address(BUYER))))
        assert "адрес пополнения Strait Pay" in plain(b.session.last(BUYER))
        await b.run(msg(BUYER, CLIENT), cb(BUYER, "w:wdt:nomemo"), msg(BUYER, "50"))
        assert "Доступно только 10 USDT" in plain(b.session.last(BUYER))
        await b.run(msg(BUYER, "2.3"))
        assert "Минимум 3 USDT" in plain(b.session.last(BUYER))
        await b.run(cb(BUYER, "w:wdt:all"))
        assert "Придёт: 8 USDT" in plain(b.session.last(BUYER))
        await b.run(cb(BUYER, "w:wdt:go"), cb(BUYER, "w:wdt:go"))  # double tap
        assert len(b.rocket.withdrawal_calls) == 1 and (await user(BUYER)).balance == 0
    go(fn)


def test_xrocket_refusal_refunds_and_failure_later_refunds(go):
    async def fn(b):
        await b.run(msg(BUYER, "/start"))
        await fund(b)
        b.rocket.withdrawal_error = xrocket.XRocketError("withdrawal_incorrect_address", status=400)
        await request_ton_withdrawal(b, amount="40")
        assert (await withdrawal()).status == "failed" and (await user(BUYER)).balance == D(100)
        assert "адрес не принят" in plain(b.session.last(BUYER))
        b.rocket.withdrawal_error = None
        await request_ton_withdrawal(b, amount="40")
        b.rocket.withdrawals["wd-2"]["status"] = "FAIL"
        await tasks.sync_ton_withdrawals(b.bot)
        assert (await withdrawal(2)).status == "failed" and (await user(BUYER)).balance == D(100)
        assert "возвращены на баланс" in plain(b.session.last(BUYER))
    go(fn)


def test_unknown_answer_is_resent_with_the_same_id(go):
    async def fn(b):
        await b.run(msg(BUYER, "/start"))
        await fund(b)
        b.rocket.withdrawal_error = xrocket.XRocketError("network", "timeout")
        await request_ton_withdrawal(b, amount="30")
        assert (await withdrawal()).status == "unknown" and (await user(BUYER)).balance == D(70)  # held
        b.rocket.withdrawal_error = None
        await tasks.sync_ton_withdrawals(b.bot)  # xRocket does not know wd-1: sent again with the same id
        assert [c[0] for c in b.rocket.withdrawal_calls] == ["wd-1"] and (await withdrawal()).status == "sent"
    go(fn)


def test_xrocket_reconcile_ignores_ton_withdrawals(go):
    async def fn(b):
        await b.run(msg(BUYER, "/start"))
        await fund(b)
        await request_ton_withdrawal(b)
        async with models.Session() as s:
            (await s.get(Withdrawal, 1)).created_at = models.now() - timedelta(minutes=30)
            await s.commit()
        await tasks.reconcile_withdrawals(b.bot)
        assert (await withdrawal()).status == "sent"
    go(fn)


def test_sweep_to_xrocket_balance_uses_a_fresh_invoice_address(go, monkeypatch):
    async def fn(b):
        await b.run(msg(BUYER, "/start"), cb(BUYER, "w:ton"))
        await set_target("xrocket")
        owner = ton.deposit_address(BUYER)
        pay_to = "UQAg9GeuItlrymiqkGHjK6ehjxXhrViiOiv91toryJfZ-ZBJ"
        asked = []

        async def deposit_address(amount, client_id):
            asked.append((amount, client_id))
            return pay_to
        b.rocket.deposit_address = deposit_address
        b.chain.pay(BUYER, "40")
        b.chain.ton[owner] = D(1)
        await cycle(b)
        assert asked == [(D(40), "sweep-1")] and b.chain.sent[-1] == ("usdt", BUYER, D(40), pay_to)
        assert (await ops())[-1].to_address == ton.raw(pay_to)
    go(fn)


def test_ton_withdrawal_waits_for_xrocket_funds(go):
    async def fn(b):
        await b.run(msg(BUYER, "/start"))
        await fund(b)
        funds = {"v": "10"}

        async def balances():
            return [{"asset": "USDT", "available": funds["v"]}]
        b.rocket.balances = balances
        await request_ton_withdrawal(b, amount="50")  # 48 to send + 0.1 xRocket fee > 10
        assert (await withdrawal()).status == "queued" and not b.rocket.withdrawal_calls
        assert "в обработке" in plain(b.session.last(BUYER)) and (await user(BUYER)).balance == D(50)
        b.rocket.withdrawal_error = xrocket.XRocketError("amount_more_than_app_balance", status=400)
        funds["v"] = "100"
        xrocket._usdt = None
        await tasks.payout_queue(b.bot)  # xRocket still says "not enough" (race): back to the queue, not refunded
        assert (await withdrawal()).status == "queued" and (await user(BUYER)).balance == D(50)
        b.rocket.withdrawal_error = None
        await tasks.payout_queue(b.bot)
        assert b.rocket.withdrawal_calls[-1] == ("wd-1", CLIENT, D(48), None)
        assert (await withdrawal()).status == "sent" and "уже в пути" in plain(b.session.last(BUYER))
    go(fn)
