"""USDT BEP-20 (BNB Smart Chain): the seed made once and kept, deposits credited once per Transfer log, deposit addresses
collected to the hot wallet, withdrawals paid in order — and never twice, whatever the nodes or the process do.

harness.FakeBsc is the chain behind the JSON-RPC: it decodes the raw transactions the bot really signs (rlp + sender
recovery), keeps balances, nonces, a mempool, receipts and Transfer logs."""
import asyncio
import os
from datetime import timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import func, select, update

from bot import models, tasks
from bot.config import config
from bot.handlers import admin_bsc
from bot.models import BscAuto, BscIncoming, BscTx, Deposit, Event, Ledger, Setting, Withdrawal, now
from bot.services import admins, bsc, money
from tests.harness import EXCH, GWEI, HOT, WORDS, FakeBsc, W, cb, msg, plain
from tests.test_scenarios import ADMIN, BUYER, OTHER, user

A1 = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"  # /1 — the first deposit address
A2 = "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC"  # /2
DEST = "0xdD2FD4581271e230360230F9337D5c0430Bf44C0"  # the user's own wallet


async def boot(monkeypatch, words=WORDS, **cfg) -> FakeBsc:
    fake = FakeBsc()
    monkeypatch.setattr(config, "bsc_mnemonic", words)
    for k, v in cfg.items():
        monkeypatch.setattr(config, k, v)

    async def post(url, method, params):
        return fake.handle(method, params)
    monkeypatch.setattr(bsc, "_post", post)
    async with models.Session() as s:
        await bsc.ensure_wallet(s)
    return fake


async def tick(b) -> bsc.Report:
    return await tasks.bsc_tick(b.bot, 0)


async def rows(model, *where):
    async with models.Session() as s:
        return list((await s.scalars(select(model).where(*where))).all())


async def give(uid, amount):
    async with models.Session() as s:
        await money.add(s, uid, D(amount), "admin", "adj:0")
        await s.commit()


async def queue(uid, amount, to=DEST, request=None) -> tuple[Withdrawal | None, str]:
    async with models.Session() as s:
        return await bsc.queue_withdrawal(s, uid, D(amount), to, request or os.urandom(8).hex(), "тест")


async def wd_of(wid) -> Withdrawal:
    async with models.Session() as s:
        return await s.get(Withdrawal, wid)


# ---------- pure functions ----------

def test_normalize_address():
    assert bsc.normalize_address(DEST) == DEST
    assert bsc.normalize_address(" " + DEST.lower() + " ") == DEST  # one case: no checksum to check
    assert bsc.normalize_address("0x" + DEST[2:].upper()) == DEST
    broken = DEST[:5] + DEST[5].swapcase() + DEST[6:]  # one letter's case changed: the checksum fails
    for bad, why in [(broken, "скопируйте"), ("0x" + "0" * 40, "нулевой"), (bsc.USDT, "контракта"),
                     (bsc.USDT.lower(), "контракта"), (DEST[:-1], "42 символа"), (DEST[2:], "42 символа"),
                     ("0x" + "g" * 40, "42 символа"), ("", "42 символа"), (None, "42 символа")]:
        with pytest.raises(ValueError, match=why):
            bsc.normalize_address(bad)
    with pytest.raises(ValueError, match="тот же адрес"):
        bsc.normalize_address(HOT.lower(), HOT)


def test_money_is_integers():
    assert bsc.to_micro("1.0000019") == 1_000_001  # down, never up
    assert bsc.to_micro(0.1) == 100_000 and bsc.to_micro(D("0.3")) == 300_000  # a float goes through str()
    assert bsc.to_micro(1.1 + 2.2) == 3_300_000  # 3.3000000000000003 -> rounded down, no float drift up
    assert bsc.micro_to_wei(1_000_001) == 1_000_001 * 10 ** 12
    assert bsc.wei_to_micro(W("1.2345678999")) == 1_234_567
    assert bsc.from_micro(1_234_567) == D("1.234567") and isinstance(bsc.from_micro(1), D)
    assert bsc.transfer_data(DEST, 10 ** 18) == "0xa9059cbb" + DEST[2:].lower().rjust(64, "0") + format(10 ** 18, "064x")


def test_hd_addresses_match_metamask():
    bsc._use(WORDS)
    try:
        assert [bsc.address(i) for i in range(3)] == [HOT, A1, A2] and bsc.hot == HOT
    finally:
        bsc._use(None)


# ---------- the seed ----------

def test_seed_made_once_encrypted_and_never_replaced(go, monkeypatch):
    async def fn(b):
        await boot(monkeypatch, words="", master_secret="s3cret")
        assert bsc.ready() and len(bsc.mnemonic().split()) == 12
        first, hot = bsc.mnemonic(), bsc.hot
        stored = (await rows(Setting, Setting.key == bsc.SEED_KEY))[0].value
        assert first not in stored and bsc._fernet().decrypt(stored.encode()).decode() == first
        assert any("Кошелёк кассы BEP-20 создан" in t and hot in t for t in await b.deliver())
        bsc._use(None)  # a restart
        async with models.Session() as s:
            assert await bsc.ensure_wallet(s)
        assert (bsc.hot, (await rows(Setting, Setting.key == bsc.SEED_KEY))[0].value) == (hot, stored)
        assert len([e for e in await rows(Event) if e.kind == "created"]) == 1
        monkeypatch.setattr(config, "master_secret", "another")  # the secret changed: off, nothing new made
        async with models.Session() as s:
            assert not await bsc.ensure_wallet(s)
        assert not bsc.ready() and "MASTER_SECRET" in bsc.error
        assert (await rows(Setting, Setting.key == bsc.SEED_KEY))[0].value == stored
        delivered = await b.deliver()
        assert any("Касса BEP-20 выключена" in t for t in delivered) and not any(first in t for t in delivered)
    go(fn)


def test_env_seed_wins_and_wallet_address_guard(go, monkeypatch):
    async def fn(b):
        await boot(monkeypatch, bsc_wallet_address=HOT.lower())
        assert bsc.hot == HOT and not await rows(Setting, Setting.key == bsc.SEED_KEY)  # nothing stored
        monkeypatch.setattr(config, "bsc_wallet_address", A1)
        async with models.Session() as s:
            assert not await bsc.ensure_wallet(s)
        assert not bsc.ready() and "BSC_WALLET_ADDRESS" in bsc.error
        monkeypatch.setattr(config, "bsc_mnemonic", "test " * 12)  # not a valid phrase: its words never get out
        monkeypatch.setattr(config, "bsc_wallet_address", "")
        async with models.Session() as s:
            assert not await bsc.ensure_wallet(s)
        assert "BIP-39" in bsc.error and "test test" not in bsc.error
        await b.run(msg(BUYER, "/start"), cb(BUYER, "w:in"))  # the desk is off: no address is given out
        assert "временно недоступен" in plain(b.session.last(BUYER)) and "0x" not in plain(b.session.last(BUYER))
    go(fn)


# ---------- incoming ----------

def test_deposit_credited_once_and_collected_to_hot_wallet(go, monkeypatch):
    async def fn(b):
        fake = await boot(monkeypatch)
        await b.run(msg(BUYER, "/start"), cb(BUYER, "w"), cb(BUYER, "w:in"))
        assert A1 in plain(b.session.last(BUYER)) and "BNB Smart Chain (BEP-20)" in plain(b.session.last(BUYER))
        await b.run(msg(OTHER, "/start"), cb(OTHER, "w:in:bsc"))
        assert A2 in plain(b.session.last(OTHER))  # the next index, never reused
        h = fake.pay(A1, "100.129999")
        fake.pay(A1, "0.009")  # dust: recorded, not credited
        await tick(b)
        await tick(b)
        assert (await user(BUYER)).balance == D("100.12")
        dep = (await rows(Deposit))[0]
        assert (dep.tx_hash, dep.amount, dep.credit, dep.network, dep.link) == (
            f"bsc:{h}:0", D("100.129999"), D("100.12"), "BEP20", f"https://bscscan.com/tx/{h}")
        assert len(await rows(Deposit)) == 1
        note = plain(b.session.last(BUYER))
        assert "Баланс пополнен на 100.12 USDT" in note and h in note
        async with models.Session() as s:  # the scan goes over the same blocks again: nothing twice
            await s.execute(update(Setting).where(Setting.key == bsc._cursor_key()).values(value=str(fake.head - 50)))
            await s.commit()
        await tick(b)
        assert len(await rows(BscIncoming)) == 2 and (await user(BUYER)).balance == D("100.12")
        fake.fund(HOT, bnb="0.01")
        for _ in range(3):  # gas from the hot wallet -> the whole USDT of the address -> the hot wallet
            await tick(b)
            fake.mine()
        await tick(b)
        assert fake.usdt.get(A1) == 0 and fake.usdt[HOT] == W("100.138999")
        kinds = sorted((r.kind, r.source) for r in await rows(BscIncoming))
        assert kinds == [("collect", A1), ("deposit", EXCH), ("deposit", EXCH)]
        txs = {t.kind: t for t in await rows(BscTx)}
        assert txs["gas"].from_address is None and txs["collect"].from_address == A1
        assert all(t.status == "confirmed" for t in txs.values())
        async with models.Session() as s:
            assert await bsc.unswept(s) == 0
    go(fn)


# ---------- withdrawals ----------

def test_withdrawal_paid_once_with_fee_hash_and_link(go, monkeypatch):
    async def fn(b):
        fake = await boot(monkeypatch)
        fake.fund(HOT, usdt="500", bnb="0.05")
        await b.run(msg(BUYER, "/start"))
        await give(BUYER, "80")
        await b.run(cb(BUYER, "w:in:bsc"))  # his deposit address exists now
        await b.run(cb(BUYER, "w:out"), cb(BUYER, "w:out:bsc"), msg(BUYER, "4"))
        assert "Минимум 5 USDT" in plain(b.session.last(BUYER))
        await b.run(msg(BUYER, "50.567"))
        assert "К получению: 49.56 USDT" in plain(b.session.last(BUYER))
        await b.run(msg(BUYER, DEST[:-1]))
        assert "42 символа" in plain(b.session.last(BUYER))
        await b.run(msg(BUYER, A1))  # one of our own addresses
        assert "адрес Strait Pay" in plain(b.session.last(BUYER))
        await b.run(msg(BUYER, DEST.lower()))
        text = plain(b.session.last(BUYER))
        assert DEST in text and "Сумма: 50.56 USDT" in text and "Комиссия: −1 USDT" in text
        await b.run(cb(BUYER, "wb:go"), cb(BUYER, "wb:go"))  # a double tap: one withdrawal
        assert len(await rows(Withdrawal)) == 1 and (await user(BUYER)).balance == D("29.44")
        wd = (await rows(Withdrawal))[0]
        assert (wd.method, wd.network, wd.amount, wd.fee, wd.status) == ("bsc", "BEP20", D("50.56"), D(1), "queued")
        await tick(b)
        wd = await wd_of(wd.id)
        tx = (await rows(BscTx))[0]
        assert (wd.status, wd.tx_hash, wd.transfer_id) == ("sent", tx.tx_hash, tx.id)
        assert (tx.kind, tx.to_address, tx.amount, tx.nonce, tx.status) == ("withdrawal", bsc.USDT, D("49.56"), 0, "pending")
        assert len(fake.pool) == 1
        await tick(b)  # not mined yet: the same bytes again, nothing new signed
        assert len(await rows(BscTx)) == 1 and set(fake.sends) == {tx.tx_hash}
        fake.mine()
        await tick(b)
        wd = await wd_of(wd.id)
        assert wd.status == "done" and wd.link == f"https://bscscan.com/tx/{tx.tx_hash}"
        assert fake.usdt[DEST] == W("49.56") and fake.usdt[HOT] == W("450.44")
        note = plain(b.session.last(BUYER))
        assert "Вывод #1 выполнен: 49.56 USDT · BEP-20" in note and "комиссия 1 USDT" in note and tx.tx_hash in note
        assert {r.kind: r.delta for r in await rows(Ledger, Ledger.ref == f"wd:{wd.id}")} == {
            "withdraw": D("-50.56"), "withdraw_fee": D(1)}
        for _ in range(3):
            fake.mine()
            await tick(b)
        assert fake.usdt[DEST] == W("49.56") and len(await rows(BscTx)) == 1  # never twice
    go(fn)


def test_queue_withdrawal_twice_makes_one(go, monkeypatch):
    async def fn(b):
        await boot(monkeypatch)
        await b.run(msg(BUYER, "/start"))
        await give(BUYER, "100")
        first = await queue(BUYER, "30", request="same")
        second = await queue(BUYER, "30", request="same")
        assert first[0] is not None and second == (None, "Заявка уже обработана")
        assert len(await rows(Withdrawal)) == 1 and (await user(BUYER)).balance == D(70)
        assert (await queue(BUYER, "71"))[1].startswith("Вывести можно только")
        assert (await queue(BUYER, "10", to=HOT))[1] == "Нельзя отправить на тот же адрес, с которого отправляем"
    go(fn)


def test_repeated_broadcast_is_one_transaction(go, monkeypatch):
    async def fn(b):
        fake = await boot(monkeypatch)
        fake.fund(HOT, bnb="1")
        raw, h = bsc._sign(0, DEST, 1, "", 0, GWEI, 21_000)
        assert await bsc.broadcast(raw) and await bsc.broadcast(raw)  # «already known» from every other node is fine
        assert list(fake.pool) == [h] and len(fake.sends) == 2 * len(bsc.nodes())
        fake.mine()
        assert await bsc.broadcast(raw) and not fake.pool and fake.bnb[DEST] == 1
    go(fn)


def test_crash_between_record_and_broadcast_loses_nothing(go, monkeypatch):
    async def fn(b):
        fake = await boot(monkeypatch)
        fake.fund(HOT, usdt="100", bnb="0.05")
        await b.run(msg(BUYER, "/start"))
        await give(BUYER, "20")
        wd, _ = await queue(BUYER, "20")
        real = bsc.broadcast

        async def crash(raw):
            raise RuntimeError("the process died right after the commit")
        monkeypatch.setattr(bsc, "broadcast", crash)
        rep = await tick(b)
        assert any("очередь выплат" in e for e in rep.errors)
        tx = (await rows(BscTx))[0]
        assert tx.status == "pending" and (await wd_of(wd.id)).status == "sent" and not fake.sends
        monkeypatch.setattr(bsc, "broadcast", real)
        fake.accept = False  # no node takes it either: still nothing new is signed
        await tick(b)
        fake.accept = True
        await tick(b)
        fake.mine()
        await tick(b)
        assert len(await rows(BscTx)) == 1 and set(fake.sends) == {tx.tx_hash}
        assert (await wd_of(wd.id)).status == "done" and fake.usdt[DEST] == W(19)
    go(fn)


def test_queue_waits_for_money_in_order_with_one_alert(go, monkeypatch):
    async def fn(b):
        fake = await boot(monkeypatch)
        fake.fund(HOT, usdt="50", bnb="0.05")
        await b.run(msg(BUYER, "/start"), msg(OTHER, "/start"))
        await give(BUYER, "100")
        await give(OTHER, "10")
        big, _ = await queue(BUYER, "100")
        small, _ = await queue(OTHER, "10")
        await tick(b)
        await tick(b)
        assert not await rows(BscTx)  # the big one waits and the small one does not overtake it
        assert [(await wd_of(i)).status for i in (big.id, small.id)] == ["queued", "queued"]
        assert sum("ждёт денег" in t and HOT in t for t in await b.deliver()) == 1
        fake.pay(HOT, "60", src=EXCH)  # an owner tops the hot wallet up
        await tick(b)
        txs = sorted(await rows(BscTx), key=lambda t: t.id)
        assert [(t.ref_id, t.nonce) for t in txs] == [(big.id, 0), (small.id, 1)]
        fake.mine()
        await tick(b)
        assert fake.usdt[DEST] == W(99 + 9) and fake.usdt[HOT] == W(2)
        assert any("пополнен извне: 60 USDT" in t for t in await b.deliver())
    go(fn)


def test_no_bnb_or_expensive_gas_signs_nothing(go, monkeypatch):
    async def fn(b):
        fake = await boot(monkeypatch)
        fake.fund(HOT, usdt="100", bnb="0.0001")
        await b.run(msg(BUYER, "/start"))
        await give(BUYER, "10")
        await queue(BUYER, "10")
        await tick(b)
        assert not await rows(BscTx) and not fake.sends
        fake.fund(HOT, bnb="0.05")
        fake.gp = 20 * GWEI
        await tick(b)
        assert not await rows(BscTx)
        fake.gp = GWEI
        await tick(b)
        assert len(await rows(BscTx)) == 1
        texts = [t for t in await b.deliver() if "ждёт денег" in t]
        assert len(texts) == 1 and "BNB на газ" in texts[0]  # one alert until the queue moves again
        assert any("снова идёт" in e.text for e in await rows(Event))
    go(fn)


def test_reverted_three_times_is_refunded(go, monkeypatch):
    async def fn(b):
        fake = await boot(monkeypatch)
        fake.fund(HOT, usdt="100", bnb="0.05")
        await b.run(msg(BUYER, "/start"))
        await give(BUYER, "30")
        wd, _ = await queue(BUYER, "30")
        fake.revert = 3
        for _ in range(3):
            await tick(b)  # signed
            fake.mine()  # reverted: nothing moved
            await tick(b)  # back to the queue (and signed again in the next round)
        wd = await wd_of(wd.id)
        assert wd.status == "failed" and (await user(BUYER)).balance == D(30)
        assert [t.status for t in await rows(BscTx)] == ["reverted"] * 3 and fake.usdt[HOT] == W(100)
        assert "возвращены на баланс" in plain(b.session.last(BUYER))
    go(fn)


def test_nonce_taken_outside_the_bot_is_replaced_then_paid_once(go, monkeypatch):
    async def fn(b):
        fake = await boot(monkeypatch)
        fake.fund(HOT, usdt="100", bnb="0.05")
        await b.run(msg(BUYER, "/start"))
        await give(BUYER, "30")
        wd, _ = await queue(BUYER, "30")
        await tick(b)
        fake.replace(HOT)  # an owner sent something from MetaMask with the same nonce
        await tick(b)
        await tick(b)
        first = (await rows(BscTx))[0]
        assert first.status == "pending" and first.replaced_seen_at is not None  # not yet: 2 minutes of proof
        async with models.Session() as s:
            await s.execute(update(BscTx).values(replaced_seen_at=now() - timedelta(minutes=3)))
            await s.commit()
        await tick(b)
        assert (await rows(BscTx, BscTx.id == first.id))[0].status == "replaced"
        await tick(b)
        second = (await rows(BscTx, BscTx.id != first.id))[0]
        assert second.nonce == 1 and (await wd_of(wd.id)).transfer_id == second.id
        fake.mine()
        await tick(b)
        assert (await wd_of(wd.id)).status == "done" and fake.usdt[DEST] == W(29)
    go(fn)


def test_stale_sending_and_bad_address_go_back(go, monkeypatch):
    async def fn(b):
        fake = await boot(monkeypatch)
        fake.fund(HOT, usdt="100", bnb="0.05")
        await b.run(msg(BUYER, "/start"))
        await give(BUYER, "40")
        stale, _ = await queue(BUYER, "20")
        bad, _ = await queue(BUYER, "20")
        async with models.Session() as s:  # the process died after claiming, before signing
            await s.execute(update(Withdrawal).where(Withdrawal.id == stale.id).values(
                status="sending", status_at=now() - timedelta(seconds=30)))
            await s.execute(update(Withdrawal).where(Withdrawal.id == bad.id).values(address="0x1234"))
            await s.commit()
        await tick(b)
        assert (await wd_of(stale.id)).status == "sending"  # 30 s: maybe still signing
        assert (await wd_of(bad.id)).status == "failed" and (await user(BUYER)).balance == D(20)
        async with models.Session() as s:
            await s.execute(update(Withdrawal).where(Withdrawal.id == stale.id).values(
                status_at=now() - timedelta(seconds=200)))
            await s.commit()
        await tick(b)
        assert (await wd_of(stale.id)).status == "sent"
    go(fn)


def test_network_down_changes_nothing(go, monkeypatch):
    async def fn(b):
        fake = await boot(monkeypatch)
        fake.fund(HOT, usdt="100", bnb="0.05")
        await b.run(msg(BUYER, "/start"))
        await give(BUYER, "10")
        wd, _ = await queue(BUYER, "10")
        fake.down = True
        rep = await tick(b)
        assert rep.errors and not await rows(BscTx) and (await wd_of(wd.id)).status == "queued"
        assert any("BEP-20:" in t for t in await b.deliver())
        fake.down = False
        await tick(b)
        assert (await wd_of(wd.id)).status == "sent"
    go(fn)


# ---------- auto-withdrawal, cold wallet ----------

def test_auto_withdrawal_to_saved_wallet(go, monkeypatch):
    async def fn(b):
        fake = await boot(monkeypatch)
        fake.fund(HOT, usdt="100", bnb="0.05")
        await b.run(msg(BUYER, "/start"), cb(BUYER, "w:auto"), cb(BUYER, "wa:addr"), msg(BUYER, DEST.lower()))
        assert "Кошелёк сохранён" in plain(b.session.last(BUYER))
        await b.run(cb(BUYER, "wa:t:25"))
        assert (await rows(BscAuto))[0].active and "от 25 USDT" in plain(b.session.last(BUYER))
        await give(BUYER, "24.99")
        await tick(b)
        assert not await rows(Withdrawal)
        await give(BUYER, "0.01")
        await tick(b)
        await tick(b)
        wds = await rows(Withdrawal)
        assert len(wds) == 1 and (wds[0].amount, wds[0].address) == (D(25), DEST)
        await give(BUYER, "30")
        await tick(b)
        assert len(await rows(Withdrawal)) == 1  # one at a time: the first is still on its way
        fake.mine()
        await tick(b)
        await tick(b)
        assert len(await rows(Withdrawal)) == 2 and fake.usdt[DEST] == W(24)
    go(fn)


def test_surplus_goes_to_cold_wallet(go, monkeypatch):
    async def fn(b):
        fake = await boot(monkeypatch, bsc_cold_address=A2.lower(), bsc_hot_max_usdt=D(100))
        fake.fund(HOT, usdt="150", bnb="0.05")
        await tick(b)
        await tick(b)
        fake.mine()
        await tick(b)
        assert fake.usdt[A2] == W(50) and fake.usdt[HOT] == W(100)
        async with models.Session() as s:
            d = await bsc.desk(s)
        assert (d.usdt, d.cold, d.total) == (D(100), D(50), D(150))
        await tick(b)
        assert len(await rows(BscTx)) == 1  # below the limit now: nothing more
    go(fn)


# ---------- admin commands ----------

def test_admin_commands(go, monkeypatch):
    async def fn(b):
        fake = await boot(monkeypatch)
        fake.fund(HOT, usdt="12.5", bnb="0.002")
        monkeypatch.setattr(admin_bsc, "KEY_TTL", 0)
        await b.run(msg(ADMIN, "/start"), msg(BUYER, "/start"), msg(OTHER, "/start"))
        await b.run(msg(ADMIN, "/gaz"))
        text = plain(b.session.last(ADMIN))
        assert HOT in text and "BNB: 0.00200 — мало" in text and "USDT: 12.5" in text
        await b.run(msg(ADMIN, "/bsc_status"))
        assert "Касса всего: 12.5 USDT" in plain(b.session.last(ADMIN))
        async with models.Session() as s:
            await admins.grant(s, OTHER)
            await s.commit()
        await b.run(msg(OTHER, "/bsc_key"))
        assert "Только владельцы" in plain(b.session.last(OTHER)) and WORDS not in "".join(b.session.texts(OTHER))
        await b.run(msg(ADMIN, "/bsc_key"))
        assert WORDS in plain(b.session.last(ADMIN)) and HOT in plain(b.session.last(ADMIN))
        await asyncio.sleep(0.05)
        assert any(type(m).__name__ == "DeleteMessage" and m.chat_id == ADMIN for m in b.session.calls)
        assert not any(WORDS in t for t in await b.deliver())  # the log says who asked, never the words
        await give(BUYER, "10")
        wd, _ = await queue(BUYER, "10")
        await b.run(msg(OTHER, "/bscq"))
        assert f"в{wd.id} · 9 USDT" in plain(b.session.last(OTHER))
        await b.run(msg(OTHER, f"/bscq cancel {wd.id}"))
        assert "только владельцы" in plain(b.session.last(OTHER))
        await b.run(msg(ADMIN, f"/bscq cancel {wd.id}"))
        assert (await wd_of(wd.id)).status == "cancelled" and (await user(BUYER)).balance == D(10)
        async with models.Session() as s:
            assert await s.scalar(select(func.count(Ledger.id)).where(Ledger.kind == "withdraw_refund")) == 1
    go(fn)
