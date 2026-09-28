"""USDT on TON: a deposit address per user, crediting by transaction hash, sweeping to the admin's address.

Keys. Every wallet key is HMAC-SHA256(TON_SEED, label): "deposit:<user id>" for deposit wallets, "gas" for the
bot's own TON wallet that pays fees. Only addresses are stored in the database; with TON_SEED any key can be
re-derived, without it nobody (including the bot) can move the funds.

Flow. scan(): incoming USDT transfers to deposit addresses (Toncenter v3) are credited once per transaction
hash, only for the real USDT jetton and only if the transaction succeeded. sweep(): a deposit wallet holding
USDT first gets TON for fees from the gas wallet, then sends all its USDT to the address set in the admin panel;
the unused fee returns to the gas wallet. One operation per wallet is in flight at a time.

Withdrawals to users' TON wallets do not use the blockchain from here: they are paid by xRocket from the app
balance (handlers.ton_wallet). The hot ("gas") wallet only pays fees for sweeps.
"""
import asyncio
import base64
import hashlib
import hmac
import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
from sqlalchemy import exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import config
from bot.models import Setting, TonDeposit, TonOp, TonWallet, now
from bot.services import events, money, settings

log = logging.getLogger(__name__)

USDT_UNIT = Decimal(10) ** 6  # USD₮ on TON has 6 decimals
TON_UNIT = Decimal(10) ** 9
GAS_TOPUP = Decimal("0.1")  # TON sent to a deposit wallet before its sweep (deploy + jetton transfer)
SWEEP_FEE = Decimal("0.05")  # TON attached to the jetton transfer; the excess comes back to the gas wallet
MIN_TON = Decimal("0.06")  # a deposit wallet with less TON gets gas first
GAS_LOW = Decimal("0.5")  # alert admins when the gas wallet holds less
INFLIGHT = timedelta(minutes=10)  # a sent operation is waited for this long before it is re-evaluated
OVERLAP = 600  # seconds re-scanned before the cursor: Toncenter indexes with a delay
BATCH = 100  # owner addresses per Toncenter request
XROCKET = "xrocket"  # ton_sweep_address value: sweep to the xRocket app balance (a fresh invoice address each time)


class ChainError(Exception):
    pass


class NotApplied(ChainError):
    """The wallet's seqno did not move before the message expired: it was not executed and never will be."""


TTL = 60  # seconds a signed wallet message stays valid
CONFIRM_WAIT = 90  # > TTL: after this, a message that did not move the seqno is dead
POLL = 5


def enabled() -> bool:
    return bool(config.ton_seed)


# ---------- keys and addresses (pure, no network) ----------

def _private_key(label: str):
    from ton_core import PrivateKey
    return PrivateKey(hmac.new(bytes.fromhex(config.ton_seed), label.encode(), hashlib.sha256).digest())


def _wallet(label: str, client=None):
    from tonutils.contracts import WalletV4R2
    return WalletV4R2.from_private_key(client, _private_key(label))


def raw(address) -> str:
    """0:HEX in upper case: the form Toncenter uses in responses."""
    from ton_core import Address
    a = address if isinstance(address, Address) else Address(address)
    return f"{a.wc}:{a.hash_part.hex().upper()}"


def friendly(address: str, bounceable: bool = False) -> str:
    """UQ… (non-bounceable) is what wallets show for a personal address."""
    from ton_core import Address
    return Address(address).to_str(is_bounceable=bounceable, is_test_only=config.ton_testnet)


def parse_address(text: str) -> str | None:
    """Normalized user-friendly address or None if the text is not a TON address."""
    from ton_core import Address
    try:
        return friendly(raw(Address(text.strip())))
    except Exception:  # noqa: BLE001 - any parse error means "not an address"
        return None


def deposit_address(uid: int) -> str:
    return raw(_wallet(f"deposit:{uid}").address)


def gas_address() -> str:
    return raw(_wallet("gas").address)


def tx_hex(h: str) -> str:
    """Toncenter returns base64 hashes; explorers and our logs use hex."""
    return base64.b64decode(h).hex()


def explorer(tx_hash: str) -> str:
    return f"https://{'testnet.' if config.ton_testnet else ''}tonviewer.com/transaction/{tx_hash}"


def address_url(address: str) -> str:
    return f"https://{'testnet.' if config.ton_testnet else ''}tonviewer.com/{friendly(address)}"


# ---------- network ----------

class Chain:
    """Toncenter v3 for reading, tonutils for signing and sending."""

    def __init__(self) -> None:
        base = f"https://{'testnet.' if config.ton_testnet else ''}toncenter.com"
        self._http = httpx.AsyncClient(base_url=base, timeout=20,
                                       headers={"X-API-Key": config.ton_api_key} if config.ton_api_key else {})
        self._gap = 0.11 if config.ton_api_key else 1.05  # public limits: 10 rps with a key, 1 rps without
        self._pace = asyncio.Lock()
        self._last = 0.0
        self._client = None
        self._hot = asyncio.Lock()

    async def close(self) -> None:
        await self._http.aclose()
        if self._client is not None:
            await self._client.close()

    async def _get(self, path: str, params) -> dict:
        for attempt in range(4):  # 429: the public limit is shared per IP, wait and retry
            async with self._pace:
                loop = asyncio.get_running_loop()
                wait = self._last + self._gap * (attempt + 1) - loop.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last = loop.time()
                try:
                    r = await self._http.get(path, params=params)
                except httpx.HTTPError as e:
                    raise ChainError(f"network: {e}") from e
            if r.status_code != 429:
                break
        if not r.is_success:
            raise ChainError(f"toncenter {r.status_code}: {r.text[:200]}")
        return r.json()

    async def incoming(self, owners: list[str], since: int) -> list[dict]:
        """Incoming USDT transfers to these owners since a unix time, oldest first."""
        out, offset = [], 0
        while True:
            page = (await self._get("/api/v3/jetton/transfers", [
                *[("owner_address", o) for o in owners], ("jetton_master", config.ton_usdt_master),
                ("direction", "in"), ("start_utime", since), ("limit", 256), ("offset", offset), ("sort", "asc"),
            ]))["jetton_transfers"]
            out += page
            if len(page) < 256:
                return out
            offset += 256

    async def last_outgoing(self, owner: str) -> dict | None:
        rows = (await self._get("/api/v3/jetton/transfers", [
            ("owner_address", owner), ("jetton_master", config.ton_usdt_master), ("direction", "out"),
            ("limit", 1), ("sort", "desc")]))["jetton_transfers"]
        return rows[0] if rows else None

    async def usdt_balance(self, owner: str) -> Decimal:
        rows = (await self._get("/api/v3/jetton/wallets", [
            ("owner_address", owner), ("jetton_address", config.ton_usdt_master), ("limit", 1)]))["jetton_wallets"]
        return Decimal(rows[0]["balance"]) / USDT_UNIT if rows else Decimal(0)

    async def ton_balance(self, address: str) -> Decimal:
        rows = (await self._get("/api/v3/accountStates", [("address", address), ("include_boc", "false")]))["accounts"]
        return Decimal(rows[0]["balance"]) / TON_UNIT if rows else Decimal(0)

    async def _signer(self):
        if self._client is None:
            from ton_core import NetworkGlobalID
            from tonutils.clients import ToncenterClient
            from tonutils.types import HTTP_RATE_LIMIT_CODES, HTTP_TRANSIENT_CODES, RetryPolicy, RetryRule
            # re-sending the same signed message is safe (same seqno), so rate limits are simply waited out
            retry = RetryPolicy(rules=(
                RetryRule(codes=HTTP_RATE_LIMIT_CODES, max_retries=6, base_delay=1.0, max_delay=4.0),
                RetryRule(codes=HTTP_TRANSIENT_CODES, max_retries=3, base_delay=1.0, max_delay=5.0),
            ), total_timeout=45.0)
            self._client = ToncenterClient(
                network=NetworkGlobalID.TESTNET if config.ton_testnet else NetworkGlobalID.MAINNET,
                api_key=config.ton_api_key or None, rps_limit=10 if config.ton_api_key else 1, rps_period=1.0,
                retry_policy=retry)
            await self._client.connect()
        return self._client

    async def _send(self, label: str, builders: list, wait: bool) -> str:
        """Sign and send from one wallet. The message expires in TTL seconds (also the first, deploying one), so
        with wait=True the answer is final: the seqno moved (executed) or NotApplied (can be retried safely)."""
        from ton_core import WalletV4Params
        wallet = _wallet(label, await self._signer())
        try:
            await wallet.refresh()
            seqno = wallet.state_data.seqno if wallet.is_active else 0
            msg = await wallet.build_external_message(builders, WalletV4Params(
                seqno=seqno, valid_until=int(time.time()) + TTL))
        except Exception as e:  # noqa: BLE001 - nothing was sent
            raise NotApplied(f"{label}: not sent: {e}") from e
        try:
            await wallet.client.send_message(msg.as_hex)
        except Exception as e:  # noqa: BLE001 - it may still have reached the network: the seqno decides
            log.warning("ton %s: send error, checking seqno: %s", label, e)
        if not wait:
            return msg.normalized_hash
        deadline = asyncio.get_running_loop().time() + CONFIRM_WAIT
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(POLL)
            try:
                await wallet.refresh()
            except Exception:  # noqa: BLE001 - a missed poll is not a verdict
                continue
            if wallet.is_active and wallet.state_data.seqno > seqno:
                return msg.normalized_hash
        raise NotApplied(f"{label}: seqno {seqno} did not move in {CONFIRM_WAIT} s")

    async def send_gas(self, to: str, amount: Decimal) -> str:
        """TON from the gas wallet; non-bounceable: the deposit wallet may not be deployed yet."""
        from ton_core import Address, to_nano
        from tonutils.contracts import TONTransferBuilder
        async with self._hot:  # one gas-wallet message at a time: back-to-back sends would share a seqno
            return await self._send("gas", [TONTransferBuilder(destination=Address(to), amount=to_nano(amount),
                                                               bounce=False)], wait=True)

    async def send_usdt(self, uid: int, amount: Decimal, to: str) -> str:
        """All `amount` USDT of the user's deposit wallet to `to`; the fee excess returns to the gas wallet."""
        from ton_core import Address, to_nano
        from tonutils.contracts import JettonTransferBuilder
        return await self._send(f"deposit:{uid}", [JettonTransferBuilder(
            destination=Address(to), jetton_amount=int(amount * USDT_UNIT),
            jetton_master_address=Address(config.ton_usdt_master),
            response_address=Address(gas_address()), amount=to_nano(SWEEP_FEE), forward_amount=1)], wait=False)


chain: Chain | None = None
busy = asyncio.Lock()  # scan/sweep never overlap: background task, admin "run now" and user "check" share wallets


def usdt_master_raw() -> str:
    return raw(config.ton_usdt_master)


# ---------- deposits ----------

async def ensure_wallet(s: AsyncSession, uid: int) -> TonWallet:
    w = await s.get(TonWallet, uid)
    if w is None:
        w = TonWallet(user_id=uid, address=deposit_address(uid))
        s.add(w)
        await s.flush()
    return w


async def _cursor(s: AsyncSession) -> int:
    row = await s.get(Setting, "ton_cursor")
    return int(row.value) if row else int((now() - timedelta(days=1)).timestamp())


async def _credit(s: AsyncSession, wallets: dict[str, TonWallet], t: dict) -> TonDeposit | None:
    """Credit one Toncenter transfer if it is a successful USDT transfer to one of our wallets, once."""
    w = wallets.get((t.get("destination") or "").upper())
    amount = Decimal(t.get("amount") or 0) / USDT_UNIT
    if (w is None or t.get("transaction_aborted") or amount <= 0
            or (t.get("jetton_master") or "").upper() != usdt_master_raw()):
        return None  # fake jettons, failed transfers and foreign addresses are never credited
    h = tx_hex(t["transaction_hash"])
    if await s.scalar(select(TonDeposit.id).where(TonDeposit.tx_hash == h)):
        return None
    dep = TonDeposit(user_id=w.user_id, tx_hash=h, amount=amount, source=(t.get("source") or "")[:70],
                     tx_time=datetime.fromtimestamp(int(t["transaction_now"]), timezone.utc))
    s.add(dep)
    await s.flush()
    await money.add(s, w.user_id, amount, "ton_deposit", f"tdep:{dep.id}")
    w.need_sweep = True
    events.add(s, f"tdep:{dep.id}", "credited", f"Пополнение USDT TON: +{money.usdt(amount)} USDT, "
               f"tx {h}, от {friendly(dep.source) if dep.source else '—'}", w.user_id, alert=True)
    return dep


async def scan(s: AsyncSession, uid: int | None = None) -> list[TonDeposit]:
    """Credit new deposits of every wallet (or of one user). Commits. Returns what was credited."""
    async with busy:
        return await _scan(s, uid)


async def _scan(s: AsyncSession, uid: int | None) -> list[TonDeposit]:
    q = select(TonWallet) if uid is None else select(TonWallet).where(TonWallet.user_id == uid)
    wallets = {w.address: w for w in (await s.scalars(q)).all()}
    started = int(now().timestamp())
    since = (await _cursor(s) if uid is None else started - 3 * 86400) - OVERLAP
    found = []
    addresses = list(wallets)
    for i in range(0, len(addresses), BATCH):
        for t in await chain.incoming(addresses[i:i + BATCH], since):
            if dep := await _credit(s, wallets, t):
                found.append(dep)
    if uid is None:
        await s.merge(Setting(key="ton_cursor", value=str(started)))
    await s.commit()
    return found


# ---------- sweeping ----------

async def _clear(s: AsyncSession, uid: int, checked_at: datetime) -> None:
    """Wallet is empty as of checked_at; a deposit credited meanwhile (user pressed "check") keeps the flag."""
    await s.execute(update(TonWallet).where(TonWallet.user_id == uid, ~exists().where(
        TonDeposit.user_id == uid, TonDeposit.created_at >= checked_at)).values(need_sweep=False)
        .execution_options(synchronize_session=False))


async def _sent_transfer(w: TonWallet, op: TonOp) -> dict | None:
    """The wallet's successful outgoing USDT transfer made for this operation, if it is on chain already."""
    t = await chain.last_outgoing(w.address)
    if t and not t.get("transaction_aborted") and int(t["transaction_now"]) >= _aware(op.created_at).timestamp() - 60:
        return t
    return None


def _finish_sweep(s: AsyncSession, w: TonWallet, op: TonOp, t: dict) -> None:
    op.status, op.done_at, op.tx_hash = "done", now(), tx_hex(t["transaction_hash"])
    events.add(s, f"tsw:{op.id}", "swept", f"Автоперевод {money.usdt(op.amount)} USDT пользователя {w.user_id} "
               f"на {friendly(op.to_address)} выполнен, tx {op.tx_hash}", w.user_id, notice=True)


async def _sweep_one(s: AsyncSession, w: TonWallet, target: str) -> None:
    op = await s.scalar(select(TonOp).where(TonOp.user_id == w.user_id, TonOp.status.in_(("sending", "sent")))
                        .order_by(TonOp.id.desc()).limit(1))
    checked_at = now()
    usdt = await chain.usdt_balance(w.address)
    if op is not None:
        if op.kind == "sweep":
            if t := await _sent_transfer(w, op):
                _finish_sweep(s, w, op, t)  # USDT still there = a deposit that came meanwhile: swept below
                checked_at, usdt = now(), await chain.usdt_balance(w.address)  # read after the transfer is indexed
            elif now() - _aware(op.created_at) < INFLIGHT:
                return  # the jetton transfer is still on its way
            else:
                op.status, op.error = "failed", "USDT не ушли за 10 мин"
                events.add(s, f"tsw:{op.id}", "failed", f"Автоперевод {money.usdt(op.amount)} USDT пользователя "
                           f"{w.user_id} не подтвердился за 10 мин — будет повтор", w.user_id, alert=True)
        else:  # gas
            if await chain.ton_balance(w.address) >= MIN_TON:
                op.status, op.done_at = "done", now()
            elif now() - _aware(op.created_at) < INFLIGHT:
                return
            else:
                op.status, op.error = "failed", "TON не поступили за 10 мин"
                return
    if usdt == 0:
        return await _clear(s, w.user_id, checked_at)
    if usdt < settings.dec("ton_sweep_min"):
        return  # small amounts wait until they add up: every transfer costs fees
    if await chain.ton_balance(w.address) < MIN_TON:
        gas = await chain.ton_balance(gas_address())
        if gas < GAS_TOPUP + Decimal("0.02"):
            await events.alert_once(s, "app:ton", "gas_empty", f"Газ-кошелёк пуст ({gas} TON): автоперевод USDT "
                                    "остановлен. Пополните TON: админ-панель → USDT в сети TON", minutes=180)
            raise ChainError("gas wallet is empty")
        op = TonOp(user_id=w.user_id, kind="gas", amount=GAS_TOPUP, to_address=w.address)
    else:
        op = TonOp(user_id=w.user_id, kind="sweep", amount=usdt,
                   to_address="xrocket" if target == XROCKET else raw(target))
    s.add(op)
    await s.commit()  # recorded before sending: a crash in between cannot lead to a blind second send
    where = "баланс xRocket" if target == XROCKET else friendly(target)
    try:
        if op.kind == "gas":
            op.msg_hash = await chain.send_gas(w.address, op.amount)
        else:
            dest = target
            if target == XROCKET:  # a fresh xRocket invoice for exactly this amount, paid on chain
                from bot.services import xrocket
                try:
                    dest = await xrocket.rocket.deposit_address(op.amount, f"sweep-{op.id}")
                except xrocket.XRocketError as e:
                    raise ChainError(f"xRocket: {e}") from e
                op.to_address = raw(dest)
            op.msg_hash = await chain.send_usdt(w.user_id, op.amount, dest)
        op.status = "sent"
    except ChainError as e:
        op.status, op.error = "failed", str(e)[:500]
        raise
    finally:
        if op.kind == "sweep":
            events.add(s, f"tsw:{op.id}", "sent" if op.status == "sent" else "failed",
                       f"Автоперевод {money.usdt(op.amount)} USDT пользователя {w.user_id} на {where}: "
                       + (f"отправлен, msg {op.msg_hash}" if op.status == "sent" else f"ошибка {op.error}"),
                       w.user_id, alert=op.status != "sent", notice=True)
        await s.commit()


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def sweep(s: AsyncSession) -> None:
    """Move credited USDT to the admin's address. Commits after every step."""
    async with busy:
        await _sweep(s)


async def _sweep(s: AsyncSession) -> None:
    target = settings.get("ton_sweep_address")
    wallets = (await s.scalars(select(TonWallet).where(TonWallet.need_sweep))).all()
    if not wallets:
        return
    if not target:
        await events.alert_once(s, "app:ton", "no_target", "USDT TON поступили, но адрес для автоперевода не задан: "
                                "админ-панель → TON → «Адрес для автоперевода»", minutes=360)
        return await s.commit()
    for w in wallets:
        try:
            await _sweep_one(s, w, target)
            await s.commit()
        except ChainError as e:
            log.warning("ton sweep user %s: %s", w.user_id, e)
            await s.commit()
            if "gas wallet is empty" in str(e):
                return
    gas = await chain.ton_balance(gas_address())
    if gas < GAS_LOW:
        await events.alert_once(s, "app:ton", "gas_low", f"В газ-кошельке {gas} TON — пополните, иначе автоперевод "
                                "USDT остановится", minutes=360)
        await s.commit()
