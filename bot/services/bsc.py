"""USDT BEP-20 (BNB Smart Chain): the bot's own cash desk. No payment API and no custodian: official JSON-RPC nodes,
transactions signed locally, PostgreSQL.

Wallets. One BIP-39 seed is the whole desk (import it into MetaMask to see every address):
  m/44'/60'/0'/0/0  the hot wallet (MetaMask «Account 1»): pays withdrawals in USDT and every fee in BNB;
  m/44'/60'/0'/0/N  user's permanent deposit address, N = hd_index ≥ 1, given out in order and never reused.
The bot makes the seed on its first start and stores it once, encrypted with sha256(MASTER_SECRET) (Fernet); nothing
ever overwrites it. Only addresses are stored, keys are derived in memory when a transaction is signed. A seed that
does not decrypt keeps the desk off: a new one would lose the money on the old addresses.

Money is integers only: micro-USDT (6 places) inside, wei on chain (USDT BEP-20 has 18 decimals: micro × 10^12).

Incoming. Transfer logs of USDT to the hot wallet and every deposit address, final blocks only, each recorded once
(bsc_incoming: tx hash + log index) in the same database transaction as its credit.

Outgoing, never twice. One lock for everything the bot signs. An operation is claimed atomically, its transaction is
signed locally (the hash is known before anything is sent) and recorded with its raw bytes together with the
operation's new status in ONE database transaction; only then it is broadcast — to every node, again and again, the
SAME bytes until the chain answers. An operation goes back to the queue only when the chain has proven that its
transaction did not go through: a receipt with status 0, or its nonce taken by another transaction. A partial unique
index keeps one live transaction per operation.
"""
import asyncio
import base64
import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from uuid import uuid4

import httpx
from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot import models
from bot.config import config
from bot.models import (
    BscAuto,
    BscDepositWallet,
    BscIncoming,
    BscPayout,
    BscTx,
    Deposit,
    Operator,
    Setting,
    User,
    Withdrawal,
    now,
)
from bot.services import events, money, settings

log = logging.getLogger(__name__)

CHAIN_ID = 56
USDT = "0x55d398326f99059fF775485246999027B3197955"  # Binance-Peg BSC-USD, 18 decimals
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
SEL_TRANSFER, SEL_BALANCE = "a9059cbb", "70a08231"
# the first one answers eth_getLogs, bsc-dataseed nodes do not
PUBLIC_RPC = ("https://bsc-rpc.publicnode.com", "https://bsc-dataseed.bnbchain.org",
              "https://bsc-dataseed1.defibit.io", "https://bsc-dataseed1.ninicoin.io")
EXPLORER = "https://bscscan.com"
TIMEOUT = 15.0
MICRO = 10 ** 6
WEI_PER_MICRO = 10 ** 12
GWEI = 10 ** 9
BNB = Decimal(10) ** 18
MAX_GAS_PRICE = 10 * GWEI  # above it nothing is signed: a spike or a broken node
GAS_MIN, GAS_MAX, GAS_DEFAULT = 40_000, 150_000, 100_000
BNB_TRANSFER_GAS = 21_000
SWEEP_GAS = 80_000  # gas limit of a deposit address's USDT transfer to the hot wallet
GAS_TOPUP_MIN = 5 * 10 ** 13  # 0.00005 BNB
MIN_GAS_WEI = 5 * 10 ** 14  # 0.0005 BNB: below it the hot wallet signs nothing
LOW_GAS_WEI = 3 * 10 ** 15  # 0.003 BNB: admins are warned
DUST_MICRO = 10_000  # 0.01 USDT
WINDOW, MIN_WINDOW, MAX_SCAN, FIRST_SCAN, NO_FINALIZED_DEPTH = 2000, 20, 20_000, 2000, 15
ADDR_CHUNK = 500  # deposit addresses per eth_getLogs
REPLACED_AFTER = timedelta(minutes=2)
STUCK_ALERT = timedelta(minutes=15)
STALE_SENDING = timedelta(seconds=120)
TRIES = 3  # reverted this many times -> the operation is taken off and the money returned
COLD_MIN = Decimal(10)
OK_SEND = ("already known", "known transaction", "already imported", "nonce too low", "already exists")
SEED_KEY = "bsc_mnemonic_enc"
WAIT_FLAG, GAS_FLAG = "bsc_wait", "bsc_gas_low"
REF = "app:bsc"
OFF = "Кошелёк BEP-20 временно недоступен — попробуйте позже"
ADDR_RE = re.compile(r"0x[0-9a-fA-F]{40}")


class RpcError(Exception):
    pass


class NoFunds(Exception):
    """The node says the transaction would fail: not enough USDT or BNB."""


class DeskError(Exception):
    """The desk cannot start (seed, MASTER_SECRET, BSC_WALLET_ADDRESS). Never carries a secret."""


# ---------- money: integers only ----------

def to_micro(x) -> int:
    """USDT -> micro-USDT, rounded down. Floats go through str() and are never multiplied as floats."""
    return int((Decimal(str(x)) * MICRO).to_integral_value(ROUND_DOWN))


def from_micro(m: int) -> Decimal:
    return (Decimal(int(m)) / MICRO).quantize(money.Q)


def micro_to_wei(m: int) -> int:
    return int(m) * WEI_PER_MICRO


def wei_to_micro(w: int) -> int:
    return int(w) // WEI_PER_MICRO


def bnb(wei: int) -> Decimal:
    return Decimal(int(wei)) / BNB


def fee() -> Decimal:
    return settings.dec("bsc_withdraw_fee")


def minimum() -> Decimal:
    return settings.dec("bsc_withdraw_min")


def net_of(gross: Decimal) -> Decimal:
    """What arrives: round(gross, 2) − the fixed fee."""
    return gross.quantize(money.KOP, ROUND_DOWN) - fee()


# ---------- addresses ----------

def normalize_address(value: str | None, sender: str | None = None) -> str:
    """A recipient as EIP-55 checksum address, or ValueError with what is wrong (shown to the user as is)."""
    from eth_utils import is_checksum_address, to_checksum_address
    v = (value or "").strip()
    if not ADDR_RE.fullmatch(v):
        raise ValueError("Адрес BEP-20 — 42 символа: 0x и 40 знаков")
    body = v[2:]
    if body != body.lower() and body != body.upper() and not is_checksum_address(v):
        raise ValueError("В адресе ошибка (не сходится контрольная сумма) — скопируйте его заново")
    a = to_checksum_address(v)
    if int(a, 16) == 0:
        raise ValueError("Это нулевой адрес — деньги сгорят")
    if a == USDT:
        raise ValueError("Это адрес контракта USDT, а нужен адрес кошелька")
    if sender and a == sender:
        raise ValueError("Нельзя отправить на тот же адрес, с которого отправляем")
    return a


def short(a: str | None) -> str:
    return f"{a[:6]}…{a[-4:]}" if a and len(a) > 12 else a or "—"


def tx_url(h: str) -> str:
    return f"{EXPLORER}/tx/{h}"


def address_url(a: str) -> str:
    return f"{EXPLORER}/address/{a}"


def pad32(a: str) -> str:
    return "0x" + a[2:].lower().rjust(64, "0")


def transfer_data(to: str, wei: int) -> str:
    return "0x" + SEL_TRANSFER + to[2:].lower().rjust(64, "0") + format(int(wei), "064x")


# ---------- the seed: made once, stored encrypted, never overwritten ----------

_seed: bytes = b""  # BIP-39 seed bytes of the mnemonic in use
_mnemonic: str = ""
_addresses: dict[int, str] = {}
hot: str = ""  # the hot wallet (index 0); "" = the desk is off
error: str = ""  # why the desk is off, for /bsc_status
lock = asyncio.Lock()  # everything the bot signs on BSC
wake = asyncio.Event()  # a withdrawal was queued: the loop runs at once


def ready() -> bool:
    return bool(hot)


def _path(index: int) -> str:
    return f"m/44'/60'/0'/0/{int(index)}"


def _key(index: int) -> bytes:
    from eth_account.hdaccount import key_from_seed
    return key_from_seed(_seed, _path(index))


def address(index: int) -> str:
    """The address at m/44'/60'/0'/0/<index> of the seed in use."""
    if index not in _addresses:
        from eth_account import Account
        _addresses[index] = Account.from_key(_key(index)).address
    return _addresses[index]


def mnemonic() -> str:
    """The seed phrase, for /bsc_key only."""
    return _mnemonic


def _use(words: str | None) -> None:
    global _seed, _mnemonic, hot
    _addresses.clear()
    _seed, _mnemonic, hot = b"", "", ""
    if words:
        from eth_account.hdaccount import seed_from_mnemonic
        try:
            seed = seed_from_mnemonic(" ".join(words.split()), "")
        except Exception:
            raise DeskError("seed не является корректной BIP-39 фразой") from None
        _seed, _mnemonic = seed, " ".join(words.split())
        hot = address(0)


def _fernet():
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(config.master_secret.encode()).digest()))


def _insert(model):
    from sqlalchemy.dialects import postgresql, sqlite
    return (postgresql if models.engine.dialect.name == "postgresql" else sqlite).insert(model)


async def _stored_mnemonic(s: AsyncSession) -> tuple[str, bool]:
    """(words, made now). Makes the seed if there is none; a stored one is never replaced."""
    from cryptography.fernet import InvalidToken
    if not config.master_secret:
        raise DeskError("MASTER_SECRET не задан: без него seed кассы не зашифровать")
    f = _fernet()
    stored = await s.scalar(select(Setting.value).where(Setting.key == SEED_KEY))
    token = None
    if stored is None:
        from eth_account import Account
        Account.enable_unaudited_hdwallet_features()
        _, words = Account.create_with_mnemonic()
        token = f.encrypt(words.encode()).decode()
        await s.execute(_insert(Setting).values(key=SEED_KEY, value=token).on_conflict_do_nothing())
        await s.commit()
        stored = await s.scalar(select(Setting.value).where(Setting.key == SEED_KEY))
    try:
        return f.decrypt(stored.encode()).decode(), stored == token
    except (InvalidToken, ValueError):
        raise DeskError("seed кассы в базе не расшифровывается — сменили MASTER_SECRET? Касса выключена, новый seed "
                        "НЕ создан: верните прежний MASTER_SECRET") from None


async def ensure_wallet(s: AsyncSession) -> bool:
    """Load (or make, on the very first start) the seed and turn the desk on. False: it stays off, admins alerted."""
    global error
    created = False
    try:
        if config.bsc_mnemonic.strip():
            words = config.bsc_mnemonic
        else:
            words, created = await _stored_mnemonic(s)
        _use(words)
        if config.bsc_wallet_address.strip():
            try:
                expected = normalize_address(config.bsc_wallet_address)
            except ValueError:
                expected = ""
            if expected != hot:
                raise DeskError(f"BSC_WALLET_ADDRESS {short(config.bsc_wallet_address)} не совпадает с горячим "
                                f"кошельком из seed {short(hot)}")
    except DeskError as e:
        _use(None)
        error = str(e)
        log.error("bsc desk off: %s", error)
        await s.rollback()
        await events.alert_once(s, REF, "config", f"Касса BEP-20 выключена: {error}", minutes=1440)
        await s.commit()
        return False
    error = ""
    if created:
        events.add(s, REF, "created", f"Кошелёк кассы BEP-20 создан: {hot}. Сохраните seed офлайн: команда /bsc_key "
                   "(придёт владельцу в личку). Импорт в MetaMask: Account 1 — касса", alert=True)
    await s.commit()
    log.info("bsc desk on, hot wallet %s", hot)
    return True


# ---------- JSON-RPC ----------

_http: httpx.AsyncClient | None = None
_good = 0  # index of the last node that answered
good_node = ""


def nodes() -> list[str]:
    own = [u.strip() for u in config.bsc_rpc_urls.split(",") if u.strip()]
    return list(dict.fromkeys([*own, *PUBLIC_RPC]))


def _host(url: str) -> str:
    """A node as logs show it: a paid node's key in its path never gets out."""
    return httpx.URL(url).host or "rpc"


async def _post(url: str, method: str, params: list):
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=TIMEOUT)
    try:
        r = await _http.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    except httpx.HTTPError as e:
        raise RpcError(f"{_host(url)}: {type(e).__name__}") from None
    if r.status_code >= 400:
        raise RpcError(f"{_host(url)}: HTTP {r.status_code}")
    try:
        body = r.json()
    except ValueError:
        raise RpcError(f"{_host(url)}: не JSON") from None
    if not isinstance(body, dict):
        raise RpcError(f"{_host(url)}: странный ответ")
    if body.get("error"):
        err = body["error"]
        raise RpcError(f"{_host(url)}: {(err.get('message') if isinstance(err, dict) else err)!s:.200}")
    if "result" not in body:
        raise RpcError(f"{_host(url)}: нет result")
    return body["result"]


async def rpc(method: str, params: list):
    """Every node in turn, from the last one that answered; all failed -> the first one's reason."""
    global _good, good_node
    urls, first = nodes(), None
    for i in range(len(urls)):
        k = (_good + i) % len(urls)
        try:
            result = await asyncio.wait_for(_post(urls[k], method, params), TIMEOUT)
        except (RpcError, asyncio.TimeoutError) as e:
            first = first or (str(e) or f"{_host(urls[k])}: таймаут")
            continue
        _good, good_node = k, _host(urls[k])
        return result
    raise RpcError(first or "нет RPC")


async def close() -> None:
    global _http
    if _http is not None:
        await _http.aclose()
    _http = None


def _int(x) -> int:
    return int(x, 16) if isinstance(x, str) else int(x)


async def usdt_balance(a: str) -> int:
    """wei of USDT."""
    return _int(await rpc("eth_call", [{"to": USDT, "data": "0x" + SEL_BALANCE + pad32(a)[2:]}, "latest"]) or "0x0")


async def bnb_balance(a: str) -> int:
    return _int(await rpc("eth_getBalance", [a, "latest"]))


async def safe_block() -> int:
    """The last final block (BSC fast finality, ~1 s); a node without «finalized» -> head − 15."""
    try:
        b = await rpc("eth_getBlockByNumber", ["finalized", False])
        if isinstance(b, dict) and b.get("number"):
            return _int(b["number"])
    except RpcError:
        pass
    return _int(await rpc("eth_blockNumber", [])) - NO_FINALIZED_DEPTH


async def receipt(h: str) -> dict | None:
    r = await rpc("eth_getTransactionReceipt", [h])
    if not isinstance(r, dict) or not r.get("blockNumber"):
        return None
    return {"ok": _int(r.get("status") or "0x0") == 1, "block": _int(r["blockNumber"])}


async def tx_count(a: str, tag: str) -> int:
    return _int(await rpc("eth_getTransactionCount", [a, tag]))


async def gas_price() -> int:
    return _int(await rpc("eth_gasPrice", [])) * 11 // 10


async def estimate(frm: str, to: str, data: str) -> int:
    try:
        g = _int(await rpc("eth_estimateGas", [{"from": frm, "to": to, "data": data, "value": "0x0"}]))
    except RpcError as e:
        if any(x in str(e).lower() for x in ("revert", "exceeds balance", "insufficient")):
            raise NoFunds(str(e)) from None
        return GAS_DEFAULT
    return min(GAS_MAX, max(GAS_MIN, g * 13 // 10))


async def incoming_transfers(addresses: list[str], frm: int, to: int) -> list[dict]:
    """USDT Transfer logs to these addresses in blocks [frm, to], oldest first: one eth_getLogs for all of them."""
    out, start, window = [], frm, WINDOW
    targets = [pad32(a) for a in addresses]
    while start <= to:
        end = min(to, start + window - 1)
        try:
            logs = await rpc("eth_getLogs", [{"address": USDT, "fromBlock": hex(start), "toBlock": hex(end),
                                              "topics": [TRANSFER_TOPIC, None, targets]}])
        except RpcError:
            if window <= MIN_WINDOW:
                raise
            window = max(MIN_WINDOW, window // 2)
            continue
        out += [x for x in logs or [] if not x.get("removed")]
        start = end + 1
    return sorted(out, key=lambda x: (_int(x["blockNumber"]), _int(x["logIndex"])))


async def broadcast(raw: str) -> bool:
    """The same signed bytes to every node at once. True if any accepted (or already has them)."""
    async def one(url: str) -> bool:
        try:
            await asyncio.wait_for(_post(url, "eth_sendRawTransaction", [raw]), TIMEOUT)
            return True
        except RpcError as e:
            return any(x in str(e).lower() for x in OK_SEND)
        except asyncio.TimeoutError:
            return False
    return any(await asyncio.gather(*(one(u) for u in nodes())))


def _sign(index: int, to: str, value: int, data: str, nonce: int, gp: int, gl: int) -> tuple[str, str]:
    """(raw tx hex, tx hash): signed in memory, nothing sent."""
    from eth_account import Account
    st = Account.sign_transaction({"nonce": nonce, "gasPrice": gp, "gas": gl, "to": to, "value": int(value),
                                   "data": bytes.fromhex(data[2:]) if data else b"", "chainId": CHAIN_ID}, _key(index))
    return "0x" + bytes(st.raw_transaction).hex(), "0x" + bytes(st.hash).hex()


# ---------- deposit addresses ----------

async def deposit_address(s: AsyncSession, uid: int) -> str:
    """The user's permanent deposit address, made on first use (the next hd_index). Commits."""
    q = select(BscDepositWallet.address).where(BscDepositWallet.user_id == uid, BscDepositWallet.master_address == hot)
    for _ in range(5):
        if a := await s.scalar(q):
            return a
        n = (await s.scalar(select(func.coalesce(func.max(BscDepositWallet.hd_index), 0)).where(
            BscDepositWallet.master_address == hot))) + 1
        await s.execute(_insert(BscDepositWallet).values(user_id=uid, hd_index=n, address=address(n),
                                                         master_address=hot, created_at=now()).on_conflict_do_nothing())
        await s.commit()  # a concurrent one took this index or made his address: read again
    raise RuntimeError("deposit address not made")


async def _wallets(s: AsyncSession) -> dict[str, BscDepositWallet]:
    return {w.address: w for w in (await s.scalars(select(BscDepositWallet).where(
        BscDepositWallet.master_address == hot))).all()}


async def is_ours(s: AsyncSession, a: str) -> bool:
    return a == hot or bool(await s.scalar(select(BscDepositWallet.id).where(BscDepositWallet.address == a)))


# ---------- incoming ----------

def _cursor_key() -> str:
    return f"bsc_scan_block:{hot}"  # a new seed is a new wallet: scanned from its own start


async def scan_incoming(s: AsyncSession) -> list[Deposit]:
    """New Transfer logs in final blocks: recorded once, deposits credited — with the cursor, in one transaction."""
    from eth_utils import to_checksum_address
    safe = await safe_block()
    stored = await s.scalar(select(Setting.value).where(Setting.key == _cursor_key()))
    last = int(stored) if stored and stored.isdigit() else safe - FIRST_SCAN
    to = min(safe, last + MAX_SCAN)
    if to <= last:
        return []
    by = await _wallets(s)
    addrs = [hot, *by]
    logs = []
    for i in range(0, len(addrs), ADDR_CHUNK):
        logs += await incoming_transfers(addrs[i:i + ADDR_CHUNK], last + 1, to)
    logs.sort(key=lambda x: (_int(x["blockNumber"]), _int(x["logIndex"])))
    times: dict[int, int] = {}
    found = []
    for lg in logs:
        topics = lg.get("topics") or []
        if (lg.get("address") or "").lower() != USDT.lower() or len(topics) != 3 or topics[0].lower() != TRANSFER_TOPIC:
            continue
        src, dst = to_checksum_address("0x" + topics[1][-40:]), to_checksum_address("0x" + topics[2][-40:])
        block = _int(lg["blockNumber"])
        if lg.get("blockTimestamp"):
            times[block] = _int(lg["blockTimestamp"])
        elif block not in times:
            b = await rpc("eth_getBlockByNumber", [hex(block), False])
            times[block] = _int(b["timestamp"]) if isinstance(b, dict) and b.get("timestamp") else 0
        if dep := await _record(s, by, lg["transactionHash"].lower(), _int(lg["logIndex"]), src, dst,
                                wei_to_micro(_int(lg.get("data") or "0x0")), block, times[block]):
            found.append(dep)
    await settings.put(s, _cursor_key(), str(to))
    await s.commit()
    return found


async def _record(s: AsyncSession, by: dict, h: str, li: int, src: str, dst: str, micro: int, block: int,
                  utime: int) -> Deposit | None:
    kind = "deposit" if dst in by else "collect" if src in by else "unmatched"
    res = await s.execute(_insert(BscIncoming).values(
        tx_hash=h, log_index=li, source=src, destination=dst, amount_micro=micro, block_number=block, utime=utime,
        kind=kind, logged=kind != "unmatched", created_at=now()).on_conflict_do_nothing())
    if res.rowcount != 1 or kind != "deposit":
        return None  # seen before; a collect or an outside top-up of the hot wallet is not anybody's balance
    w = by[dst]
    credit = Decimal(micro // DUST_MICRO) / 100
    if credit <= 0:
        return None  # dust below 0.01 USDT
    dep = Deposit(user_id=w.user_id, amount=from_micro(micro), credit=credit, status="paid", network="BEP20",
                  address=dst, purpose="deposit", tx_hash=f"bsc:{h}:{li}", source=src, link=tx_url(h))
    s.add(dep)
    await s.flush()
    await s.execute(update(BscIncoming).where(BscIncoming.tx_hash == h, BscIncoming.log_index == li)
                    .values(ref_id=dep.id))
    await money.add(s, w.user_id, credit, "deposit", f"dep:{dep.id}")
    events.add(s, f"dep:{dep.id}", "credited", f"Зачислено {money.usdt(credit)} USDT · BEP-20 · от {short(src)} · "
               f"tx {h}", w.user_id, notice=True)
    return dep


async def notify_incoming(s: AsyncSession) -> None:
    """USDT that came to the hot wallet from outside (an owner topping it up): one log line from 1 USDT."""
    rows = (await s.scalars(select(BscIncoming).where(~BscIncoming.logged).limit(50))).all()
    for r in rows:
        r.logged = True
        if r.amount_micro >= MICRO:
            events.add(s, REF, "topup", f"Горячий кошелёк BEP-20 пополнен извне: {money.usdt(from_micro(r.amount_micro))} "
                       f"USDT от {r.source} · tx {r.tx_hash}", alert=True)
    await s.commit()


# ---------- outgoing ----------

def _from(sender: str | None):
    return BscTx.from_address.is_(None) if sender is None else BscTx.from_address == sender


async def _sign_record_send(s: AsyncSession, *, kind: str, ref_id: int, index: int, sender: str | None, to: str,
                            value: int, data: str, gp: int, gl: int, amount: Decimal, mark=None) -> BscTx | None:
    """Sign, record (with whatever mark(tx) changes, in the same commit), then broadcast. None: not recorded, nothing
    sent. The caller holds `lock`."""
    a = sender or hot
    live = await s.scalar(select(func.max(BscTx.nonce)).where(BscTx.status == "pending", _from(sender)))
    nonce = max(await tx_count(a, "pending"), -1 if live is None else live + 1)
    raw, h = _sign(index, to, value, data, nonce, gp, gl)
    tx = BscTx(kind=kind, ref_id=ref_id, from_address=sender, to_address=to, amount=amount, nonce=nonce, tx_hash=h,
               raw_tx=raw, gas_price=gp, gas_limit=gl)
    s.add(tx)
    try:
        await s.flush()
        if mark is not None and not await mark(tx):
            await s.rollback()
            return None
        await s.commit()  # known before it can exist on chain: a crash from here on never leads to a blind resend
    except IntegrityError:
        await s.rollback()  # this operation already has a live transaction
        return None
    if not await broadcast(raw):
        log.warning("bsc %s %s #%s: no node took it yet, reconcile sends it again", kind, ref_id, tx.id)
    return tx


async def _inflight(s: AsyncSession) -> tuple[int, int]:
    """(micro-USDT the hot wallet has signed and not settled, its pending transactions)."""
    total, n = (await s.execute(select(
        func.coalesce(func.sum(case((BscTx.kind.in_(("withdrawal", "payout")), BscTx.amount), else_=0)), 0),
        func.count(BscTx.id)).where(BscTx.status == "pending", BscTx.from_address.is_(None)))).one()
    return to_micro(total), n


FLOW = {"withdrawal": (Withdrawal, "sent", "done"), "payout": (BscPayout, "pending_chain", "sent")}


def _claimable(kind: str):
    model = FLOW[kind][0]
    return (model.method == "bsc",) if model is Withdrawal else ()


async def _set(s: AsyncSession, kind: str, rid: int, old: str, **values) -> bool:
    """Move an operation from status `old`, atomically: False if it is not there any more."""
    model = FLOW[kind][0]
    return (await s.execute(update(model).where(model.id == rid, model.status == old, *_claimable(kind))
                            .values(status_at=now(), **values).returning(model.id))).first() is not None


async def _waiting(s: AsyncSession, why: str) -> None:
    if not settings.raw(WAIT_FLAG):
        await settings.put(s, WAIT_FLAG, "1")
        events.add(s, REF, "queue_wait", f"Очередь выплат BEP-20 ждёт денег: {why}. Пополните горячий кошелёк {hot} "
                   "(сеть BSC / BEP-20)", alert=True)
    await s.commit()


async def _refund(s: AsyncSession, wd: Withdrawal, why: str, out: list) -> None:
    wd.status, wd.error, wd.status_at = "failed", why[:1000], now()
    await money.add(s, wd.user_id, wd.amount, "withdraw_refund", f"wd:{wd.id}")
    events.add(s, f"wd:{wd.id}", "failed", f"{why}. {money.usdt(wd.amount)} USDT возвращены пользователю", wd.user_id,
               alert=True)
    out.append((wd, "refunded"))


async def _pay(s: AsyncSession, kind: str, rid: int, out: list) -> bool:
    """One queued operation: signed, recorded, broadcast — or left waiting. False: the queue stops here (FIFO)."""
    model, chain_status, _ = FLOW[kind]
    obj = await s.get(model, rid, populate_existing=True)
    if obj is None or obj.status != "queued":
        return True
    amount = obj.amount - obj.fee if kind == "withdrawal" else obj.amount
    try:
        to = normalize_address(obj.address, hot)
    except ValueError as e:
        if kind == "withdrawal":
            obj = await s.get(Withdrawal, rid, with_for_update=True, populate_existing=True)
            if obj.status == "queued":
                await _refund(s, obj, f"Адрес получателя не принят: {e}", out)
        else:
            obj.status, obj.comment = "cancelled", f"адрес не принят: {e}"
            events.add(s, REF, "payout_rejected", f"Выплата {kind} #{rid} снята: {e}", alert=True)
        await s.commit()
        return True
    micro = to_micro(amount)
    reserved, n_live = await _inflight(s)
    have = wei_to_micro(await usdt_balance(hot))
    if have - reserved < micro:
        await _waiting(s, f"нужно {money.usdt(from_micro(micro))} USDT, свободно "
                          f"{money.usdt(from_micro(max(have - reserved, 0)))}")
        return False
    gp = await gas_price()
    if gp > MAX_GAS_PRICE:
        await _waiting(s, f"газ {gp / GWEI:.1f} gwei — выше предела {MAX_GAS_PRICE // GWEI} gwei, ждём")
        return False
    data = transfer_data(to, micro_to_wei(micro))
    try:
        gl = await estimate(hot, USDT, data)
    except NoFunds as e:
        await _waiting(s, f"сеть не пропускает перевод ({e})")
        return False
    have_bnb = await bnb_balance(hot)
    if have_bnb < gl * gp * (n_live + 1) or have_bnb < MIN_GAS_WEI:
        await _waiting(s, f"BNB на газ {bnb(have_bnb):.6f}, нужно не меньше {bnb(max(gl * gp * (n_live + 1), MIN_GAS_WEI)):.6f}")
        return False
    if not await _set(s, kind, rid, "queued", status="sending"):  # 1. claimed atomically
        await s.commit()
        return True
    await s.commit()

    async def mark(tx: BscTx) -> bool:  # 5. in the same transaction as the record
        values = {"tx_hash": tx.tx_hash}
        if kind == "withdrawal":
            values |= {"transfer_id": tx.id, "sent_at": now(), "link": tx_url(tx.tx_hash), "error": None}
        if not await _set(s, kind, rid, "sending", status=chain_status, **values):
            return False
        events.add(s, f"wd:{rid}" if kind == "withdrawal" else REF, "sending",
                   f"Отправка {money.usdt(from_micro(micro))} USDT · BEP-20 на {to}: tx {tx.tx_hash}, nonce {tx.nonce}",
                   getattr(obj, "user_id", None), notice=True)
        return True

    try:
        tx = await _sign_record_send(s, kind=kind, ref_id=rid, index=0, sender=None, to=USDT, value=0, data=data,
                                     gp=gp, gl=gl, amount=from_micro(micro), mark=mark)
    except Exception:
        await s.rollback()
        await _set(s, kind, rid, "sending", status="queued")  # nothing recorded, nothing sent
        await s.commit()
        raise
    if tx is None:
        await _set(s, kind, rid, "sending", status="queued")
        await s.commit()
    return True


async def _requeue_stale(s: AsyncSession) -> None:
    """«sending» with no live transaction for over 120 s: the process died before signing — back to the queue."""
    for kind, (model, _, _) in FLOW.items():
        rows = (await s.scalars(select(model.id).where(model.status == "sending", *_claimable(kind),
                                                       model.status_at < now() - STALE_SENDING))).all()
        for rid in rows:
            if not await s.scalar(select(BscTx.id).where(BscTx.kind == kind, BscTx.ref_id == rid,
                                                         BscTx.status == "pending")):
                await _set(s, kind, rid, "sending", status="queued")
    await s.commit()


async def process_queue(s: AsyncSession) -> list:
    """Withdrawals first, then other payouts — strictly in order: a later one never overtakes an earlier one."""
    out: list = []
    async with lock:
        await _requeue_stale(s)
        wds = (await s.scalars(select(Withdrawal.id).where(Withdrawal.method == "bsc", Withdrawal.status == "queued")
                               .order_by(Withdrawal.id).limit(20))).all()
        pays = (await s.scalars(select(BscPayout.id).where(BscPayout.status == "queued")
                                .order_by(BscPayout.id).limit(5))).all()
        for kind, rid in [*(("withdrawal", i) for i in wds), *(("payout", i) for i in pays)]:
            if not await _pay(s, kind, rid, out):
                return out
        if settings.raw(WAIT_FLAG):
            await settings.put(s, WAIT_FLAG, "")
            events.add(s, REF, "queue_flowing", "Очередь выплат BEP-20 снова идёт", notice=True)
            await s.commit()
    return out


# ---------- settling what is in flight ----------

async def _confirmed(s: AsyncSession, tx: BscTx, out: list) -> None:
    if tx.kind == "withdrawal":
        wd = await s.get(Withdrawal, tx.ref_id, with_for_update=True, populate_existing=True)
        if wd.status in ("sending", "sent", "queued"):
            wd.status, wd.tx_hash, wd.link, wd.transfer_id = "done", tx.tx_hash, tx_url(tx.tx_hash), tx.id
            wd.sent_at, wd.status_at = wd.sent_at or now(), now()
            if wd.fee:
                money.platform(s, wd.fee, "withdraw_fee", f"wd:{wd.id}")
            events.add(s, f"wd:{wd.id}", "done", f"Вывод выполнен: {money.usdt(wd.amount - wd.fee)} USDT · BEP-20 на "
                       f"{wd.address} · tx {tx.tx_hash}", wd.user_id, notice=True)
            out.append((wd, "done"))
        elif wd.status in ("failed", "cancelled"):
            events.add(s, f"wd:{wd.id}", "paid_after_refund", f"Транзакция {tx.tx_hash} по выводу подтверждена ПОСЛЕ "
                       f"возврата средств: {money.usdt(tx.amount)} USDT ушли на {wd.address}. Спишите возврат "
                       "корректировкой баланса", wd.user_id, alert=True)
    elif tx.kind == "payout":
        p = await s.get(BscPayout, tx.ref_id, with_for_update=True, populate_existing=True)
        p.status, p.tx_hash, p.status_at = "sent", tx.tx_hash, now()
        events.add(s, REF, "payout_sent", f"Выплата {p.kind} #{p.id}: {money.usdt(p.amount)} USDT на {p.address} · "
                   f"tx {tx.tx_hash}", alert=True)
    elif tx.kind == "collect":
        w = await s.get(BscDepositWallet, tx.ref_id)
        events.add(s, REF, "collected", f"Собрано {money.usdt(tx.amount)} USDT с адреса пополнения пользователя "
                   f"{w.user_id if w else '?'} на горячий кошелёк · tx {tx.tx_hash}", notice=True)
    else:
        events.add(s, REF, "gas", f"Газ {tx.amount:f} BNB отправлен на адрес пополнения {short(tx.to_address)} · "
                   f"tx {tx.tx_hash}", notice=True)


async def _failed(s: AsyncSession, tx: BscTx, why: str, out: list) -> None:
    """The chain proved tx did not move anything: its operation goes back to the queue (after TRIES reverts — off)."""
    if tx.kind == "withdrawal":
        wd = await s.get(Withdrawal, tx.ref_id, with_for_update=True, populate_existing=True)
        if wd.status != "sent" or wd.transfer_id != tx.id:
            return
        tries = await s.scalar(select(func.count(BscTx.id)).where(
            BscTx.kind == "withdrawal", BscTx.ref_id == wd.id, BscTx.status == "reverted"))
        if tries >= TRIES:
            await _refund(s, wd, f"Сеть {TRIES} раза откатила перевод (USDT не ушли)", out)
        else:
            wd.status, wd.status_at = "queued", now()
            events.add(s, f"wd:{wd.id}", "retry", f"{why} — USDT не ушли, отправим снова", wd.user_id, alert=True)
    elif tx.kind == "payout":
        p = await s.get(BscPayout, tx.ref_id, with_for_update=True, populate_existing=True)
        if p.status != "pending_chain" or p.tx_hash != tx.tx_hash:
            return
        tries = await s.scalar(select(func.count(BscTx.id)).where(
            BscTx.kind == "payout", BscTx.ref_id == p.id, BscTx.status == "reverted"))
        p.status, p.status_at = ("cancelled" if tries >= TRIES else "queued"), now()
        events.add(s, REF, "payout_retry", f"Выплата {p.kind} #{p.id}: {why}" + (" — снята" if tries >= TRIES else
                   " — отправим снова"), alert=True)
    else:
        events.add(s, REF, f"{tx.kind}_failed", f"{'Газ' if tx.kind == 'gas' else 'Сбор USDT'} #{tx.id}: {why} — "
                   "повторим", alert=tx.status == "reverted", notice=True)


async def reconcile(s: AsyncSession) -> list[tuple[Withdrawal, str]]:
    """Every pending transaction moves on: final receipt -> confirmed / reverted; its nonce taken by another one for
    2 minutes -> replaced; otherwise the same bytes go out again. Returns withdrawals to tell their users about."""
    out: list = []
    async with lock:
        rows = (await s.scalars(select(BscTx).where(BscTx.status == "pending").order_by(BscTx.id))).all()
        if not rows:
            return out
        safe = await safe_block()
        latest: dict[str, int] = {}
        for tx in rows:
            r = await receipt(tx.tx_hash)
            if r is not None:
                if r["block"] <= safe:
                    tx.status, tx.block_number = ("confirmed" if r["ok"] else "reverted"), r["block"]
                    if r["ok"]:
                        await _confirmed(s, tx, out)
                    else:
                        await _failed(s, tx, f"сеть откатила транзакцию {tx.tx_hash}", out)
                    await s.commit()
                continue  # mined, not final yet
            sender = tx.from_address or hot
            if sender not in latest:
                latest[sender] = await tx_count(sender, "latest")
            if latest[sender] > tx.nonce:  # its nonce is used, and not by it (no receipt)
                if tx.replaced_seen_at is None:
                    tx.replaced_seen_at = now()
                elif now() - aware(tx.replaced_seen_at) > REPLACED_AFTER:
                    tx.status = "replaced"
                    await _failed(s, tx, f"nonce {tx.nonce} занят другой транзакцией (отправлено с кошелька не ботом?)",
                                  out)
                await s.commit()
                continue
            tx.replaced_seen_at = None
            await broadcast(tx.raw_tx)
            if not tx.alerted and now() - aware(tx.created_at) > STUCK_ALERT:
                tx.alerted = True
                events.add(s, REF, "stuck", f"Транзакция {tx.kind} #{tx.ref_id} ({tx.tx_hash}) висит в сети дольше "
                           f"{int(STUCK_ALERT.total_seconds() // 60)} мин — рассылаем её снова", alert=True)
            await s.commit()
    return out


def aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------- deposit addresses -> the hot wallet ----------

async def collect_deposits(s: AsyncSession) -> None:
    """Every deposit address with USDT on it: gas from the hot wallet first, then all its USDT to the hot wallet."""
    async with lock:
        by = await _wallets(s)
        if not by:
            return
        came = dict((await s.execute(select(BscIncoming.destination, func.sum(BscIncoming.amount_micro)).where(
            BscIncoming.kind == "deposit").group_by(BscIncoming.destination))).all())
        went = dict((await s.execute(select(BscIncoming.source, func.sum(BscIncoming.amount_micro)).where(
            BscIncoming.kind == "collect").group_by(BscIncoming.source))).all())
        busy = set((await s.scalars(select(BscTx.ref_id).where(BscTx.status == "pending",
                                                               BscTx.kind.in_(("gas", "collect"))))).all())
        todo = [w for a, w in by.items() if (came.get(a) or 0) > (went.get(a) or 0) and w.id not in busy]
        for w in sorted(todo, key=lambda x: x.id)[:20]:
            usdt = await usdt_balance(w.address)
            if usdt < DUST_MICRO * WEI_PER_MICRO:
                continue
            gp = await gas_price()
            if gp > MAX_GAS_PRICE:
                return
            cost = SWEEP_GAS * gp
            if await bnb_balance(w.address) < cost:
                value = max(GAS_TOPUP_MIN, 3 * cost)
                _, n_live = await _inflight(s)
                if await bnb_balance(hot) < value + BNB_TRANSFER_GAS * gp * (n_live + 1) + MIN_GAS_WEI:
                    await events.alert_once(s, REF, "gas_empty", f"На горячем кошельке BEP-20 мало BNB: сбор USDT с "
                                            f"адресов пополнения стоит. Пополните BNB: {hot}", minutes=180)
                    await s.commit()
                    return
                amount = (bnb(value)).quantize(money.Q, ROUND_UP)
                await _sign_record_send(s, kind="gas", ref_id=w.id, index=0, sender=None, to=w.address,
                                        value=int(amount * BNB), data="", gp=gp, gl=BNB_TRANSFER_GAS, amount=amount)
                continue
            micro = wei_to_micro(usdt)
            await _sign_record_send(s, kind="collect", ref_id=w.id, index=w.hd_index, sender=w.address, to=USDT,
                                    value=0, data=transfer_data(hot, usdt), gp=gp, gl=SWEEP_GAS,
                                    amount=from_micro(micro))


# ---------- withdrawals ----------

async def queue_withdrawal(s: AsyncSession, uid: int, gross: Decimal, to: str, request_id: str,
                           where: str) -> tuple[Withdrawal | None, str]:
    """Debit and queue a BEP-20 withdrawal: (withdrawal, "") or (None, why). request_id is unique: a second call with
    the same one makes nothing. Commits."""
    if not ready():
        return None, OFF
    gross = Decimal(gross).quantize(money.KOP, ROUND_DOWN)
    if gross < minimum() or gross <= fee():
        return None, f"Минимум {money.usdt(max(minimum(), fee() + money.KOP))} USDT"
    try:
        to = normalize_address(to, hot)
    except ValueError as e:
        return None, str(e)
    if await is_ours(s, to):
        return None, "Это адрес Strait Pay, а нужен ваш собственный кошелёк или адрес биржи"
    u = await money.lock(s, uid)
    if gross > (free := money.withdrawable(u)):
        await s.rollback()
        return None, f"Вывести можно только {money.usdt(free)} USDT"
    wd = Withdrawal(user_id=uid, amount=gross, fee=fee(), request_id=request_id, method="bsc", network="BEP20",
                    address=to, status="queued", status_at=now())
    s.add(wd)
    try:
        await s.flush()
        await money.add(s, uid, -gross, "withdraw", f"wd:{wd.id}")
        events.add(s, f"wd:{wd.id}", "request", f"Запрос вывода BEP-20 ({where}): списано {money.usdt(gross)} USDT, к "
                   f"отправке {money.usdt(gross - wd.fee)} на {to}", uid, notice=True)
        await s.commit()
    except (money.NotEnough, IntegrityError) as e:
        await s.rollback()
        return None, "Недостаточно средств" if isinstance(e, money.NotEnough) else "Заявка уже обработана"
    wake.set()
    return wd, ""


async def cancel_unsigned(s: AsyncSession, wid: int, who: str) -> Withdrawal | None:
    """A queued BEP-20 withdrawal nothing was ever signed for: cancelled, the money back. None: too late. Commits."""
    async with lock:
        wd = await s.get(Withdrawal, wid, with_for_update=True, populate_existing=True)
        if not wd or wd.method != "bsc" or wd.status != "queued" or wd.transfer_id:
            await s.rollback()
            return None
        wd.status, wd.status_at = "cancelled", now()
        await money.add(s, wd.user_id, wd.amount, "withdraw_refund", f"wd:{wd.id}")
        events.add(s, f"wd:{wd.id}", "cancelled", f"Вывод снят из очереди ({who}), {money.usdt(wd.amount)} USDT "
                   "возвращены", wd.user_id, alert=True)
        await s.commit()
        return wd


MOVING = ("queued", "sending", "sent", "unknown", "pending")


async def auto_withdrawals(s: AsyncSession) -> None:
    """A user's balance reached his threshold: all of it to his saved address (same queue, same fee)."""
    rows = (await s.execute(select(BscAuto, User).join(User, User.id == BscAuto.user_id).where(
        BscAuto.active, User.balance >= BscAuto.threshold, ~User.is_banned))).all()
    for auto, u in rows:
        if await s.scalar(select(Operator.debt).where(Operator.user_id == u.id, Operator.debt > 0)):
            continue
        if await s.scalar(select(Withdrawal.id).where(Withdrawal.user_id == u.id, Withdrawal.status.in_(MOVING))
                          .limit(1)):
            continue
        gross = money.withdrawable(u).quantize(money.KOP, ROUND_DOWN)
        if gross < max(auto.threshold, minimum()) or gross <= fee():
            continue
        _, err = await queue_withdrawal(s, u.id, gross, auto.address, str(uuid4()), "автовывод")
        if err:
            log.info("bsc auto withdrawal of %s: %s", u.id, err)


# ---------- balances, cold wallet ----------

async def unswept(s: AsyncSession) -> Decimal:
    """USDT that came to deposit addresses and were not collected to the hot wallet yet."""
    came = await s.scalar(select(func.coalesce(func.sum(BscIncoming.amount_micro), 0)).where(BscIncoming.kind == "deposit"))
    went = await s.scalar(select(func.coalesce(func.sum(BscIncoming.amount_micro), 0)).where(BscIncoming.kind == "collect"))
    return from_micro(max(int(came) - int(went), 0))


async def queued_need(s: AsyncSession) -> Decimal:
    wds = await s.scalar(select(func.coalesce(func.sum(Withdrawal.amount - Withdrawal.fee), 0)).where(
        Withdrawal.method == "bsc", Withdrawal.status.in_(("queued", "sending"))))
    pays = await s.scalar(select(func.coalesce(func.sum(BscPayout.amount), 0)).where(
        BscPayout.status.in_(("queued", "sending"))))
    return Decimal(wds) + Decimal(pays)


@dataclass
class Desk:
    usdt: Decimal  # on the hot wallet, minus what it signed and the chain has not settled
    bnb: Decimal
    cold: Decimal | None
    unswept: Decimal
    queued: Decimal
    pending: int

    @property
    def total(self) -> Decimal:
        return self.usdt + (self.cold or 0) + self.unswept


async def desk(s: AsyncSession) -> Desk:
    """The cash desk now. Raises RpcError."""
    reserved, n = await _inflight(s)
    cold = None
    if config.bsc_cold_address:
        cold = from_micro(wei_to_micro(await usdt_balance(normalize_address(config.bsc_cold_address))))
    return Desk(usdt=from_micro(wei_to_micro(await usdt_balance(hot)) - reserved), bnb=bnb(await bnb_balance(hot)),
                cold=cold, unswept=await unswept(s), queued=await queued_need(s), pending=n)


async def check_balances(s: AsyncSession) -> None:
    wei = await bnb_balance(hot)
    if wei < LOW_GAS_WEI and not settings.raw(GAS_FLAG):
        await settings.put(s, GAS_FLAG, "1")
        events.add(s, REF, "gas_low", f"На горячем кошельке BEP-20 {bnb(wei):.5f} BNB — пополните (нужно от 0.003 BNB), "
                   f"иначе выплаты и сбор USDT остановятся: {hot}", alert=True)
    elif wei >= LOW_GAS_WEI and settings.raw(GAS_FLAG):
        await settings.put(s, GAS_FLAG, "")
    await s.commit()


async def move_to_cold(s: AsyncSession) -> None:
    """The hot wallet's surplus over BSC_HOT_MAX_USDT -> the cold wallet, through the common queue, one at a time."""
    if not config.bsc_cold_address or config.bsc_hot_max_usdt is None:
        return
    try:
        cold = normalize_address(config.bsc_cold_address, hot)
    except ValueError as e:
        await events.alert_once(s, REF, "cold_bad", f"BSC_COLD_ADDRESS не принят: {e}", minutes=1440)
        await s.commit()
        return
    if await s.scalar(select(BscPayout.id).where(BscPayout.kind == "cold",
                                                 BscPayout.status.in_(("queued", "sending", "pending_chain")))):
        return
    d = await desk(s)
    surplus = (d.usdt - d.queued - Decimal(config.bsc_hot_max_usdt)).quantize(money.KOP, ROUND_DOWN)
    if surplus < COLD_MIN:
        return
    ref = (await s.scalar(select(func.coalesce(func.max(BscPayout.ref_id), 0)).where(BscPayout.kind == "cold"))) + 1
    s.add(BscPayout(kind="cold", ref_id=ref, address=cold, amount=surplus, comment="излишек горячего кошелька"))
    events.add(s, REF, "cold", f"Излишек горячего кошелька {money.usdt(surplus)} USDT уходит на холодный {cold}",
               alert=True)
    await s.commit()


# ---------- the loop ----------

@dataclass
class Report:
    credited: list[Deposit] = field(default_factory=list)
    finished: list[tuple[Withdrawal, str]] = field(default_factory=list)  # (withdrawal, done | refunded)
    errors: list[str] = field(default_factory=list)


async def tick(s: AsyncSession, n: int = 0) -> Report:
    """One round of the background loop (every 3 s): incoming first, so money that just came pays the queue in the same
    round. Every step on its own: one failing never stops the others."""
    rep = Report()
    if not ready():
        return rep
    steps = [("приход", scan_incoming), ("очередь выплат", process_queue), ("сверка", reconcile)]
    if n % 2 == 0:
        steps += [("автовывод", auto_withdrawals), ("сбор с адресов пополнения", collect_deposits)]
    if n % 10 == 0:
        steps += [("баланс", check_balances), ("приход извне", notify_incoming), ("холодный кошелёк", move_to_cold)]
    for name, step in steps:
        try:
            result = await step(s)
        except Exception as e:
            log.warning("bsc %s: %s", name, e, exc_info=not isinstance(e, (RpcError, NoFunds)))
            await s.rollback()
            rep.errors.append(f"{name}: {e}"[:200])
            continue
        if name == "приход":
            rep.credited += result
        elif result:
            rep.finished += result
    if rep.errors:
        await events.alert_once(s, REF, "chain_error", "BEP-20: " + "; ".join(rep.errors), minutes=60)
        await s.commit()
    return rep


async def retire_legacy(s: AsyncSession) -> list[Withdrawal]:
    """TON and xRocket are gone: their queued withdrawals never left — the money goes back to the balance (returned for
    the users to be told); one that may have left is an owner's decision on its card (status unknown), alerted once.
    Idempotent, commits."""
    refunded = list((await s.scalars(select(Withdrawal).where(
        Withdrawal.method != "bsc", Withdrawal.status == "queued").with_for_update())).all())
    for wd in refunded:
        wd.status, wd.error = "cancelled", "сеть отключена: вывод не отправлялся, сумма возвращена"
        await money.add(s, wd.user_id, wd.amount, "withdraw_refund", f"wd:{wd.id}")
        events.add(s, f"wd:{wd.id}", "cancelled", f"Сеть {wd.network or wd.method} отключена, вывод не отправлялся: "
                   f"{money.usdt(wd.amount)} USDT возвращены на баланс", wd.user_id, notice=True)
    open_ = (await s.scalars(select(Withdrawal).where(Withdrawal.method != "bsc", Withdrawal.status.in_(
        ("sending", "sent"))))).all()
    for wd in open_:
        wd.status = "unknown"
    n = await s.scalar(select(func.count(Withdrawal.id)).where(Withdrawal.method != "bsc",
                                                             Withdrawal.status.in_(("unknown", "pending"))))
    if n:
        await events.alert_once(s, REF, "legacy", f"Выводов старых сетей (TON / xRocket) с неизвестным итогом: {n}. "
                                "Проверьте их в обозревателе и решите в карточке вывода: «Подтвердить выполнение» или "
                                "«Вернуть средства»", minutes=60 * 24 * 365)
    await s.commit()
    return refunded
