"""USDT on TON: the bot's own wallets — incoming transfers credited by transaction hash, collected to the hot wallet,
withdrawals paid from it.

Wallets. Every key is HMAC-SHA256(TON_SEED, label), wallet v4r2:
  gas            the hot wallet: TON for every fee and USDT for withdrawals; admins top it up. The label is the
                 historic "gas", so the address stays the one admins already know;
  deposit:<uid>  a user's personal deposit address: USDT that come here are credited to his balance;
  debt:<uid>     an operator's personal address: USDT that come here repay his debt for Bybit orders.
Only addresses are stored. With TON_SEED every key can be derived again; without it nobody can move the funds.

Incoming. scan(): Toncenter v3 lists USDT transfers to our addresses; each is credited once (deposits.tx_hash is
unique), only the real USD₮ jetton, only if its transaction was not aborted.

Outgoing, never twice. Every message is a TonTransfer recorded (seqno, valid_until, hash) and committed BEFORE it is
broadcast; a wallet has one message in flight at a time. A message is executed iff the wallet's seqno moved past it;
once valid_until (+ a margin) has passed with the seqno unmoved, it is dead for good and the operation may be signed
again. Before every new message the chain seqno is compared with our records: a message taken for dead that was
executed after all is found there and restored, and nothing is signed on top of it. A jetton transfer carries
query_id = the TonTransfer id, so the transfer on chain is matched to its record exactly.

cycle() (a background task, also «run now» in the admin panel): scan -> settle messages in flight -> collect
addresses to the hot wallet (TON for their fee first) -> pay queued withdrawals strictly in order while the hot wallet
covers them.
"""
import asyncio
import base64
import binascii
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from functools import lru_cache

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import config
from bot.models import Deposit, TonAddress, TonTransfer, Withdrawal, now
from bot.services import events, money, operators, settings

log = logging.getLogger(__name__)

HOT = "gas"
USDT_UNIT = Decimal(10) ** 6  # USD₮ on TON has 6 decimals
TON_UNIT = Decimal(10) ** 9
GAS_TOPUP = Decimal("0.1")  # TON sent to a personal address before its first sweep (deploy + jetton transfer)
JETTON_TON = Decimal("0.05")  # TON attached to a jetton transfer; the excess comes back to the hot wallet
MIN_ADDR_TON = Decimal("0.06")  # a personal address with less TON gets gas first
HOT_RESERVE = Decimal("0.2")  # the hot wallet keeps this much TON: below it nothing is paid out
HOT_LOW = Decimal("1")  # admins are warned below this
TTL = 60  # seconds a signed message stays valid
DEAD_AFTER = 120  # seconds after valid_until: an unexecuted message is dead for good
WAIT = 25.0  # seconds a cycle waits for its own message to be executed (then the next cycle settles it)
POLL = 3.0
LOST = timedelta(minutes=30)  # executed, its jetton transfer still not on chain: an admin looks at it
UNKNOWN_DAYS = timedelta(days=7)  # an unknown transfer is still looked for on chain this long
GAS_WAIT = timedelta(minutes=5)  # TON sent to an address are waited for this long before more is sent
OVERLAP = 600  # seconds re-scanned before the cursor: Toncenter indexes with a delay
ISSUED_SLACK = timedelta(minutes=10)  # transfers older than the address here are history, never credited
BATCH = 100  # owner addresses per Toncenter request
PAYOUT_TRIES = 3  # a payout whose jetton transfer was aborted is sent again this many times, then refunded
API_KEY = "ton_api_key"  # settings: a key set in the admin panel wins over TON_API_KEY from .env
CURSOR = "ton_cursor"


class ChainError(Exception):
    pass


def enabled() -> bool:
    return bool(config.ton_seed)


def aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------- keys and addresses (no network) ----------

def _private_key(label: str):
    from ton_core import PrivateKey
    return PrivateKey(hmac.new(bytes.fromhex(config.ton_seed), label.encode(), hashlib.sha256).digest())


def _wallet(label: str, client=None):
    from tonutils.contracts import WalletV4R2
    return WalletV4R2.from_private_key(client, _private_key(label))


@lru_cache(maxsize=8192)
def _derive(seed: str, label: str) -> str:
    return raw(_wallet(label).address)


def address(label: str) -> str:
    """Raw address (0:HEX) of one of our wallets."""
    return _derive(config.ton_seed, label)


def hot_address() -> str:
    return address(HOT)


def raw(a) -> str:
    """0:HEX upper case: the form Toncenter answers with. Raises ValueError for anything that is not an address."""
    from ton_core import Address
    try:
        x = a if isinstance(a, Address) else Address(str(a).strip())
    except Exception as e:  # noqa: BLE001 - any parse error means "not an address"
        raise ValueError(f"not a TON address: {a!r}") from e
    return f"{x.wc}:{x.hash_part.hex().upper()}"


def friendly(a: str, bounceable: bool = False) -> str:
    """UQ… (non-bounceable) is what wallets show for a personal address."""
    from ton_core import Address
    return Address(raw(a)).to_str(is_bounceable=bounceable, is_test_only=config.ton_testnet)


def parse_address(text: str | None) -> str | None:
    """A user-friendly basechain address as the user meant it (bounceability kept), or None."""
    from ton_core import Address
    v = (text or "").strip()
    if not 48 <= len(v) <= 67 or any(c.isspace() for c in v):
        return None
    try:
        a = Address(v)
    except Exception:  # noqa: BLE001
        return None
    if a.wc != 0:
        return None
    return a.to_str(is_bounceable=a.is_bounceable if not v.startswith(("0:", "-")) else False,
                    is_test_only=config.ton_testnet)


@lru_cache(maxsize=4)
def _master(master: str) -> str:
    return raw(master)


def usdt_master() -> str:
    return _master(config.ton_usdt_master)


def tx_hex(h: str) -> str:
    """Toncenter v3 answers with base64 hashes; logs, explorers and our records use hex."""
    h = (h or "").strip()
    if len(h) == 64 and all(c in "0123456789abcdefABCDEF" for c in h):
        return h.lower()
    try:
        return base64.b64decode(h.replace("-", "+").replace("_", "/")).hex()
    except (binascii.Error, ValueError):
        return h[:64]


def explorer_tx(tx_hash: str) -> str:
    return f"https://{'testnet.' if config.ton_testnet else ''}tonviewer.com/transaction/{tx_hash}"


def explorer_address(a: str) -> str:
    return f"https://{'testnet.' if config.ton_testnet else ''}tonviewer.com/{friendly(a)}"


def short(a: str | None) -> str:
    return f"{a[:6]}…{a[-6:]}" if a and len(a) > 14 else a or "—"


def api_key() -> str:
    return settings.raw(API_KEY) or config.ton_api_key


def key_source() -> str:
    return "админ-панель" if settings.raw(API_KEY) else ".env (TON_API_KEY)" if config.ton_api_key else ""


def hint(key: str) -> str:
    """A key as admins see it: never in full."""
    return f"…{key[-4:]}" if len(key) > 8 else "задан" if key else "не задан"


def credit_of(received: Decimal) -> Decimal:
    """A deposit to the balance: minus deposit_fee."""
    return (received * (1 - settings.dec("deposit_fee") / 100)).quantize(money.Q, ROUND_DOWN)


# ---------- network ----------

class Chain:
    """Toncenter v3 (indexer) for jetton transfers and balances; Toncenter v2 through tonutils for wallet state,
    signing and sending."""

    def __init__(self, key: str) -> None:
        self.key = key
        base = f"https://{'testnet.' if config.ton_testnet else ''}toncenter.com"
        self._http = httpx.AsyncClient(base_url=base, timeout=20, headers={"X-API-Key": key} if key else {})
        self._gap = 0.2 if key else 1.1  # v2 and v3 share the key's limit (10 rps; 1 rps without a key)
        self._pace = asyncio.Lock()
        self._last = 0.0
        self._client = None
        self._jw: dict[str, object] = {}  # owner raw -> its USDT jetton wallet address

    async def close(self) -> None:
        await self._http.aclose()
        if self._client is not None:
            await self._client.close()

    async def _get(self, path: str, params) -> dict:
        r = None
        for attempt in range(4):  # 429: the limit is shared, wait and retry
            async with self._pace:
                loop = asyncio.get_running_loop()
                wait = self._last + self._gap * (attempt + 1) - loop.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last = loop.time()
                try:
                    r = await self._http.get(path, params=params)
                except httpx.HTTPError as e:
                    raise ChainError(f"toncenter: {type(e).__name__} {e}") from e
            if r.status_code != 429:
                break
        if r.status_code in (401, 403):
            raise ChainError("toncenter: ключ API не принят")
        if not r.is_success:
            raise ChainError(f"toncenter {r.status_code}: {r.text[:200]}")
        try:
            return r.json()
        except ValueError as e:
            raise ChainError("toncenter: invalid json") from e

    async def check(self) -> None:
        await self._get("/api/v3/masterchainInfo", [])

    async def _transfers(self, params: list) -> list[dict]:
        out, offset = [], 0
        while True:
            page = (await self._get("/api/v3/jetton/transfers", [*params, ("limit", 256), ("offset", offset),
                                                                  ("sort", "asc")])).get("jetton_transfers") or []
            out += page
            if len(page) < 256:
                return out
            offset += 256

    async def incoming(self, owners: list[str], since: int) -> list[dict]:
        """USDT transfers to these owners since a unix time, oldest first."""
        return await self._transfers([*[("owner_address", o) for o in owners], ("jetton_master", usdt_master()),
                                      ("direction", "in"), ("start_utime", since)])

    async def outgoing(self, owner: str, since: int) -> list[dict]:
        return await self._transfers([("owner_address", owner), ("jetton_master", usdt_master()),
                                      ("direction", "out"), ("start_utime", since)])

    async def usdt_balance(self, owner: str) -> Decimal:
        rows = (await self._get("/api/v3/jetton/wallets", [
            ("owner_address", owner), ("jetton_address", usdt_master()), ("limit", 1)])).get("jetton_wallets") or []
        return Decimal(str(rows[0]["balance"])) / USDT_UNIT if rows else Decimal(0)

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
            client = ToncenterClient(
                network=NetworkGlobalID.TESTNET if config.ton_testnet else NetworkGlobalID.MAINNET,
                api_key=self.key or None, rps_limit=5 if self.key else 1, rps_period=1.0, retry_policy=retry)
            await client.connect()
            self._client = client
        return self._client

    async def state(self, label: str) -> tuple[int, Decimal]:
        """(seqno, TON balance) of one of our wallets, from a liteserver (fresh, not the indexer). Never guesses:
        anything but a clear answer raises (tonutils' own refresh() turns errors into an empty wallet)."""
        body = await self._get("/api/v2/getWalletInformation", [("address", address(label))])
        r = body.get("result") if isinstance(body, dict) and body.get("ok") else None
        if not isinstance(r, dict) or "balance" not in r:
            raise ChainError(f"state {label}: bad answer {str(body)[:200]}")
        try:
            balance = Decimal(str(r["balance"])) / TON_UNIT
            if r.get("account_state") == "active":
                if r.get("seqno") is None:
                    raise ChainError(f"state {label}: active wallet without seqno")
                return int(r["seqno"]), balance
            return 0, balance  # not deployed yet: its first message deploys it with seqno 0
        except (ArithmeticError, TypeError, ValueError) as e:
            raise ChainError(f"state {label}: {e}") from e

    async def _jetton_wallet(self, owner: str):
        if owner not in self._jw:
            from ton_core import Address
            from tonutils.contracts import get_wallet_address_get_method
            try:
                jw = await get_wallet_address_get_method(
                    client=await self._signer(), address=Address(usdt_master()), owner_address=Address(owner))
            except Exception as e:  # noqa: BLE001
                raise ChainError(f"jetton wallet of {owner}: {type(e).__name__} {e}") from e
            if not isinstance(jw, Address):
                raise ChainError(f"jetton wallet of {owner}: {jw!r}")
            self._jw[owner] = jw
        return self._jw[owner]

    async def build(self, tr: TonTransfer) -> tuple[str, str]:
        """Sign tr: (BoC hex, normalized message hash hex). Nothing is sent."""
        from ton_core import Address, WalletV4Params, to_nano
        from tonutils.contracts import JettonTransferBuilder, TONTransferBuilder
        try:
            w = _wallet(tr.wallet, await self._signer())
            if tr.asset == "TON":
                msg = TONTransferBuilder(destination=Address(tr.to_address), amount=to_nano(tr.amount), bounce=False,
                                         body=tr.memo or None)
            else:
                msg = JettonTransferBuilder(
                    destination=Address(tr.to_address), jetton_amount=int(Decimal(tr.amount) * USDT_UNIT),
                    jetton_wallet_address=await self._jetton_wallet(address(tr.wallet)),
                    response_address=Address(hot_address()), forward_payload=tr.memo or None, forward_amount=1,
                    amount=to_nano(JETTON_TON), query_id=tr.id)
            ext = await w.build_external_message([msg], WalletV4Params(seqno=tr.seqno, valid_until=tr.valid_until))
            return ext.as_hex, ext.normalized_hash
        except Exception as e:  # noqa: BLE001 - nothing was sent
            raise ChainError(f"not signed: {type(e).__name__} {e}") from e

    async def broadcast(self, boc: str) -> None:
        try:
            await (await self._signer()).send_message(boc)
        except Exception as e:  # noqa: BLE001 - it may still have reached the network: the seqno decides
            raise ChainError(f"broadcast: {type(e).__name__} {e}") from e


chain: Chain | None = None
busy = asyncio.Lock()  # one cycle at a time: the background task, «run now», a user's «check», an admin transfer
wake = asyncio.Event()  # set when a withdrawal is queued: the cycle runs at once instead of waiting
_hot: tuple[float, Decimal, Decimal] | None = None  # (loop time, USDT, TON) of the hot wallet, for dashboards


async def start() -> None:
    """(Re)connect with the current API key; no wallet without TON_SEED."""
    global chain, _hot
    old, chain, _hot = chain, (Chain(api_key()) if enabled() else None), None
    if old is not None:
        await old.close()


async def close() -> None:
    global chain
    if chain is not None:
        await chain.close()
    chain = None


async def check_key(key: str) -> None:
    """Raises ChainError if Toncenter does not accept the key."""
    c = Chain(key)
    try:
        await asyncio.wait_for(c.check(), 15)
    except asyncio.TimeoutError as e:
        raise ChainError("toncenter: нет ответа") from e
    finally:
        await c.close()


async def hot_balances(max_age: float = 60, timeout: float = 8) -> tuple[Decimal, Decimal]:
    """(USDT, TON) on the hot wallet; max_age > 0 reuses a recent answer. Raises ChainError / TimeoutError."""
    global _hot
    if chain is None:
        raise ChainError("TON_SEED не задан")
    t = asyncio.get_running_loop().time()
    if _hot is None or t - _hot[0] > max_age:
        usdt = await asyncio.wait_for(chain.usdt_balance(hot_address()), timeout)
        _, ton = await asyncio.wait_for(chain.state(HOT), timeout)
        _hot = t, usdt, ton
    return _hot[1], _hot[2]


# ---------- personal addresses ----------

async def personal(s: AsyncSession, uid: int, purpose: str = "deposit") -> TonAddress:
    """The user's address for `purpose` (deposit | debt), made on first use. Does not commit."""
    q = select(TonAddress).where(TonAddress.user_id == uid, TonAddress.purpose == purpose)
    a = await s.scalar(q)
    if a is None:
        try:  # two requests may make it at once: the loser reads the winner's row
            async with s.begin_nested():
                a = TonAddress(user_id=uid, purpose=purpose, address=address(f"{purpose}:{uid}"), unswept=Decimal(0))
                s.add(a)
        except IntegrityError:
            a = await s.scalar(q)
    return a


async def is_ours(s: AsyncSession, addr: str) -> bool:
    """An address of the bot itself (hot wallet or anyone's personal address): never a withdrawal target."""
    r = raw(addr)
    return r == hot_address() or bool(await s.scalar(select(TonAddress.id).where(TonAddress.address == r)))


# ---------- incoming ----------

async def _credit(s: AsyncSession, by: dict[str, TonAddress], t: dict) -> Deposit | None:
    """Credit one Toncenter transfer once, if it is a successful USD₮ transfer to one of our addresses."""
    try:
        a = by.get(raw(t.get("destination") or ""))
        master = raw(t.get("jetton_master") or "")
        amount = (Decimal(str(t.get("amount") or 0)) / USDT_UNIT).quantize(money.Q, ROUND_DOWN)
        when = datetime.fromtimestamp(int(t["transaction_now"]), timezone.utc)
        h = tx_hex(t["transaction_hash"])
    except (ValueError, KeyError, TypeError, ArithmeticError):
        return None
    if a is None or t.get("transaction_aborted") or master != usdt_master() or amount <= 0 or len(h) != 64:
        return None  # fake jettons, failed transfers and foreign addresses are never credited
    if when < aware(a.created_at) - ISSUED_SLACK:
        return None  # came before the bot gave this address out: history, not a deposit
    if await s.scalar(select(Deposit.id).where(Deposit.tx_hash == h)):
        return None
    try:
        source = raw(t.get("source") or "")
    except ValueError:
        source = ""
    dep = Deposit(user_id=a.user_id, amount=amount, credit=Decimal(0), status="paid", network="TON",
                  address=friendly(a.address), purpose=a.purpose, tx_hash=h, source=source, link=explorer_tx(h))
    try:
        async with s.begin_nested():
            s.add(dep)
            await s.flush()
    except IntegrityError:
        return None  # credited by a concurrent scan
    a.unswept += amount
    ref = f"dep:{dep.id}"
    sender = f"от {short(friendly(source))}" if source else "отправитель не указан"
    if a.purpose == "debt":
        paid, extra = await operators.repay(s, a.user_id, amount, f"перевод USDT TON, пополнение #{dep.id}")
        dep.credit = amount
        if extra > 0:
            await money.add(s, a.user_id, extra, "deposit", ref)
        events.add(s, ref, "credited", f"Погашение долга оператора: пришло {money.usdt(amount)} USDT, погашено "
                   f"{money.usdt(paid)}" + (f", {money.usdt(extra)} сверх долга — на баланс" if extra else "")
                   + f" · {sender} · tx {h}", a.user_id, notice=True)
    elif amount < settings.dec("deposit_min"):
        dep.status = "small"
        events.add(s, ref, "small", f"Пришло {money.usdt(amount)} USDT — меньше минимума {settings.get('deposit_min')} "
                   f"USDT, не зачислено · {sender} · tx {h}", a.user_id, notice=True)
    else:
        dep.credit = credit_of(amount)
        await money.add(s, a.user_id, dep.credit, "deposit", ref)
        if amount > dep.credit:
            money.platform(s, amount - dep.credit, "deposit_fee", ref)
        events.add(s, ref, "credited", f"Зачислено {money.usdt(dep.credit)} USDT (пришло {money.usdt(amount)}, "
                   f"комиссия {money.usdt(amount - dep.credit)}) · {sender} · tx {h}", a.user_id, notice=True)
    return dep


async def _scan(s: AsyncSession, uid: int | None = None) -> list[Deposit]:
    q = select(TonAddress) if uid is None else select(TonAddress).where(TonAddress.user_id == uid)
    rows = (await s.scalars(q)).all()
    if not rows:
        return []
    started = int(time.time())
    if uid is None:
        cursor = settings.raw(CURSOR)
        since = (int(cursor) if cursor.isdigit() else started) - OVERLAP
    else:  # one user's «check»: everything since his addresses exist (at most 3 days)
        since = max(started - 3 * 86400, int(min(aware(a.created_at) for a in rows).timestamp()) - OVERLAP)
    by = {a.address: a for a in rows}
    owners = list(by)
    found = []
    for i in range(0, len(owners), BATCH):
        for t in await chain.incoming(owners[i:i + BATCH], since):
            if dep := await _credit(s, by, t):
                found.append(dep)
    if uid is None:
        await settings.put(s, CURSOR, str(started))
    await s.commit()
    return found


async def check_user(s: AsyncSession, uid: int) -> list[Deposit] | None:
    """A user pressed «check»: scan his addresses now. None if a cycle is running (it scans them anyway)."""
    if chain is None:
        return []
    try:
        await asyncio.wait_for(busy.acquire(), 5)
    except asyncio.TimeoutError:
        return None
    try:
        return await _scan(s, uid)
    finally:
        busy.release()


# ---------- outgoing: one message at a time per wallet, never twice ----------

async def _seqno_for(s: AsyncSession, label: str) -> int | None:
    """The seqno to sign the next message of `label` with, or None: not now (a message in flight, the chain read is
    behind our records, or a message we took for dead turned out executed — it is restored here)."""
    last = await s.scalar(select(TonTransfer).where(TonTransfer.wallet == label).order_by(TonTransfer.id.desc())
                          .limit(1))
    if last is not None and last.status == "sending":
        return None
    seqno, _ = await chain.state(label)
    if last is None:
        return seqno
    if last.status == "expired":
        if seqno > last.seqno:  # executed after all: never sign on top of it
            log.error("ton %s: transfer %s taken for dead was executed (seqno %s > %s)", label, last.id, seqno,
                      last.seqno)
            events.add(s, "app:ton", "restored", f"Сообщение #{last.id} кошелька {label} считалось несостоявшимся, но "
                       "сеть его исполнила — восстановлено, повторной отправки не будет", alert=True)
            await _applied(s, last)
            await s.commit()
            return None
        return seqno if seqno == last.seqno else None
    if seqno <= last.seqno:
        return None  # a stale read: our own executed message is not visible yet
    if seqno > last.seqno + 1:
        await events.alert_once(s, "app:ton", f"foreign:{label}"[:32], f"Кошелёк {label} отправил сообщения не через "
                                f"бота (seqno {seqno}, у бота последний {last.seqno}). Если это не вы — ключ TON_SEED "
                                "мог утечь: выведите средства и смените его", minutes=1440)
    return seqno


async def _send(s: AsyncSession, tr: TonTransfer, before_commit=None) -> bool:
    """Sign and broadcast tr (filled except seqno, valid_until and hash). The record is committed before the
    broadcast, together with whatever before_commit(tr) changes. False: not now, nothing recorded or sent.
    Raises ChainError if the message could not be signed (recorded as expired, nothing sent)."""
    seqno = await _seqno_for(s, tr.wallet)
    if seqno is None:
        return False
    tr.seqno, tr.valid_until, tr.status = seqno, int(time.time()) + TTL, "sending"
    s.add(tr)
    await s.flush()  # the id is the jetton transfer's query_id
    try:
        boc, tr.msg_hash = await chain.build(tr)
    except ChainError as e:
        tr.status, tr.error = "expired", str(e)[:500]
        await s.commit()
        raise
    if before_commit:
        before_commit(tr)
    await s.commit()  # known before it can exist on chain: a crash from here on never leads to a blind second send
    try:
        await chain.broadcast(boc)
    except ChainError as e:
        log.warning("ton %s #%s: %s — the seqno decides", tr.wallet, tr.id, e)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WAIT
    while loop.time() < deadline:
        await asyncio.sleep(POLL)
        try:
            seqno_now, _ = await chain.state(tr.wallet)
        except ChainError:
            continue
        if seqno_now > tr.seqno:
            await _applied(s, tr)
            await s.commit()
            break
    return True


async def _wd(s: AsyncSession, tr: TonTransfer) -> Withdrawal | None:
    if tr.kind != "payout":
        return None
    return await s.get(Withdrawal, int(tr.ref.split(":")[1]), with_for_update=True, populate_existing=True)


async def _addr(s: AsyncSession, tr: TonTransfer) -> TonAddress | None:
    if not tr.ref.startswith("addr:"):
        return None
    return await s.get(TonAddress, int(tr.ref.split(":")[1]), with_for_update=True, populate_existing=True)


async def _applied(s: AsyncSession, tr: TonTransfer) -> None:
    """The wallet executed tr. A TON transfer is done with it; a jetton transfer is now waited for on chain."""
    if tr.asset == "TON":
        tr.status, tr.done_at = "done", now()
        events.add(s, "app:ton", "gas" if tr.kind == "gas" else "ton_out",
                   f"{'Газ' if tr.kind == 'gas' else 'TON'}: {money.fmt(Decimal(tr.amount), 4)} TON с горячего кошелька "
                   f"на {short(friendly(tr.to_address))} отправлены (#{tr.id})", notice=True,
                   alert=tr.kind == "admin")
        return
    tr.status = "sent"
    if wd := await _wd(s, tr):
        if wd.status in ("queued", "sending"):
            wd.status, wd.sent_at, wd.transfer_id = "sent", wd.sent_at or now(), tr.id
            events.add(s, f"wd:{wd.id}", "sent", f"Перевод принят сетью (сообщение #{tr.id}), ждём транзакцию",
                       wd.user_id)


async def _expired(s: AsyncSession, tr: TonTransfer) -> None:
    """valid_until passed with the seqno unmoved: tr will never be executed; its operation is free to go again."""
    tr.status, tr.error = "expired", tr.error or "сеть не исполнила сообщение до его срока"
    if (wd := await _wd(s, tr)) and wd.status == "sending" and wd.transfer_id == tr.id:
        wd.status = "queued"
        events.add(s, f"wd:{wd.id}", "retry", f"Сообщение #{tr.id} не исполнено сетью до срока — отправим снова",
                   wd.user_id, notice=True)


async def _done(s: AsyncSession, tr: TonTransfer, t: dict, out: list) -> None:
    tr.status, tr.done_at, tr.tx_hash = "done", now(), tx_hex(t.get("transaction_hash") or "") or None
    if wd := await _wd(s, tr):
        if wd.status in ("sending", "sent", "unknown", "queued"):
            wd.status, wd.tx_hash, wd.transfer_id = "done", tr.tx_hash, tr.id
            wd.link = explorer_tx(tr.tx_hash) if tr.tx_hash else None
            wd.sent_at = wd.sent_at or now()
            if wd.fee:
                money.platform(s, wd.fee, "withdraw_fee", f"wd:{wd.id}")
            events.add(s, f"wd:{wd.id}", "done", f"Вывод выполнен: {money.usdt(wd.amount - wd.fee)} USDT на "
                       f"{wd.address}" + (f" · memo {wd.memo}" if wd.memo else "") + f" · tx {tr.tx_hash}",
                       wd.user_id, notice=True)
            out.append((wd, "done"))
        elif wd.status in ("failed", "cancelled"):  # refunded by hand, yet the USDT did leave: the platform paid twice
            events.add(s, f"wd:{wd.id}", "paid_after_refund", f"Перевод #{tr.id} по выводу найден в сети ПОСЛЕ возврата "
                       f"средств: {money.usdt(Decimal(tr.amount))} USDT ушли на {wd.address}, tx {tr.tx_hash}. "
                       "Спишите возврат корректировкой баланса", wd.user_id, alert=True)
    elif a := await _addr(s, tr):
        a.unswept = max(a.unswept - Decimal(tr.amount), Decimal(0))
        events.add(s, "app:ton", "swept", f"Собрано {money.usdt(Decimal(tr.amount))} USDT с адреса "
                   f"{'долга' if a.purpose == 'debt' else 'пополнения'} пользователя {a.user_id} на горячий кошелёк · "
                   f"tx {tr.tx_hash}", a.user_id, notice=True)
    else:
        events.add(s, "app:ton", "usdt_out", f"USDT: {money.usdt(Decimal(tr.amount))} с горячего кошелька на "
                   f"{friendly(tr.to_address)} отправлены · tx {tr.tx_hash}", alert=True, notice=True)


async def _refund(s: AsyncSession, wd: Withdrawal, why: str, out: list) -> None:
    wd.status, wd.error = "failed", why[:1000]
    await money.add(s, wd.user_id, wd.amount, "withdraw_refund", f"wd:{wd.id}")
    events.add(s, f"wd:{wd.id}", "failed", f"{why}. {money.usdt(wd.amount)} USDT возвращены пользователю", wd.user_id,
               alert=True)
    out.append((wd, "refunded"))


async def _failed(s: AsyncSession, tr: TonTransfer, out: list) -> None:
    """The jetton transfer was aborted on chain: the USDT never left. A payout goes again (a few times)."""
    tr.status, tr.done_at, tr.error = "failed", now(), "перевод USDT отклонён сетью (aborted)"
    if wd := await _wd(s, tr):
        if wd.status not in ("sending", "sent", "unknown"):
            return
        tries = await s.scalar(select(func.count(TonTransfer.id)).where(
            TonTransfer.ref == tr.ref, TonTransfer.status == "failed"))
        if tries < PAYOUT_TRIES:
            wd.status = "queued"
            events.add(s, f"wd:{wd.id}", "aborted", f"Сеть отклонила перевод #{tr.id} (USDT не ушли) — попытка "
                       f"{tries} из {PAYOUT_TRIES}, отправим снова", wd.user_id, alert=True)
        else:
            await _refund(s, wd, f"Сеть {PAYOUT_TRIES} раза отклонила перевод (USDT не ушли)", out)
    else:
        events.add(s, "app:ton", "aborted", f"Перевод #{tr.id} ({tr.kind}, {money.usdt(Decimal(tr.amount))} USDT) "
                   "отклонён сетью — USDT остались на месте", alert=tr.kind == "admin", notice=True)


async def _lost(s: AsyncSession, tr: TonTransfer) -> None:
    tr.status = "unknown"
    if (wd := await _wd(s, tr)) and wd.status in ("sending", "sent"):
        wd.status = "unknown"
        events.add(s, f"wd:{wd.id}", "unknown", f"Сообщение #{tr.id} исполнено кошельком, но перевод USDT не виден в "
                   f"сети {int(LOST.total_seconds() // 60)} мин. Деньги удержаны, повтора не будет — проверьте "
                   f"горячий кошелёк в обозревателе", wd.user_id, alert=True)
    else:
        events.add(s, "app:ton", "unknown", f"Перевод #{tr.id} ({tr.kind}) исполнен кошельком, но не виден в сети "
                   f"{int(LOST.total_seconds() // 60)} мин — проверьте в обозревателе", alert=True)


async def _settle(s: AsyncSession) -> list[tuple[Withdrawal, str]]:
    """Move every message in flight forward. Returns withdrawals with an outcome to tell their users. Commits."""
    out: list = []
    rows = (await s.scalars(select(TonTransfer).where(
        TonTransfer.status.in_(("sending", "sent"))  # an unknown one is looked for a week: an owner decides meanwhile
        | ((TonTransfer.status == "unknown") & (TonTransfer.created_at > now() - UNKNOWN_DAYS)))
        .order_by(TonTransfer.id))).all()
    seqnos: dict[str, int] = {}
    for tr in rows:
        if tr.status != "sending":
            continue
        if tr.wallet not in seqnos:
            seqnos[tr.wallet] = (await chain.state(tr.wallet))[0]
        if seqnos[tr.wallet] > tr.seqno:
            await _applied(s, tr)
        elif time.time() > tr.valid_until + DEAD_AFTER:
            await _expired(s, tr)
    await s.commit()
    waiting: dict[str, list[TonTransfer]] = {}
    for tr in rows:
        if tr.status in ("sent", "unknown") and tr.asset == "USDT":
            waiting.setdefault(tr.wallet, []).append(tr)
    for label, trs in waiting.items():
        since = int(min(aware(t.created_at) for t in trs).timestamp()) - 120
        found = {}
        for t in await chain.outgoing(address(label), since):
            try:
                found.setdefault(int(t.get("query_id") or -1), t)
            except (TypeError, ValueError):
                continue
        for tr in trs:
            ids = [tr.id]
            if tr.kind == "payout":  # any message ever signed for this withdrawal settles it
                ids = (await s.scalars(select(TonTransfer.id).where(TonTransfer.ref == tr.ref))).all()
            t = next((found[i] for i in ids if i in found), None)
            if t is not None and t.get("transaction_aborted"):
                await _failed(s, tr, out)
            elif t is not None:
                await _done(s, tr, t, out)
            elif tr.status == "sent" and now() - aware(tr.created_at) > LOST:
                await _lost(s, tr)
        await s.commit()
    return out


# ---------- collecting personal addresses to the hot wallet ----------

async def _sweeps(s: AsyncSession) -> None:
    minimum = settings.dec("ton_sweep_min")
    rows = (await s.scalars(select(TonAddress).where(TonAddress.unswept >= minimum, TonAddress.unswept > 0)
                            .order_by(TonAddress.id).limit(30))).all()
    for a in rows:
        busy_now = await s.scalar(select(TonTransfer.id).where(
            ((TonTransfer.wallet == a.label) & TonTransfer.status.in_(("sending", "sent")))
            | ((TonTransfer.ref == f"addr:{a.id}") & (TonTransfer.kind == "gas")
               & ((TonTransfer.status == "sending") | (TonTransfer.created_at > now() - GAS_WAIT)))).limit(1))
        if busy_now:
            continue  # a transfer of this address, or its gas, is on its way
        usdt = (await chain.usdt_balance(a.address)).quantize(money.Q, ROUND_DOWN)
        if usdt < minimum:
            if usdt < a.unswept:
                a.unswept = usdt  # already collected by an earlier transfer
                await s.commit()
            continue
        _, ton = await chain.state(a.label)
        if ton < MIN_ADDR_TON:
            _, hot_ton = await chain.state(HOT)
            if hot_ton < GAS_TOPUP + HOT_RESERVE:
                await events.alert_once(s, "app:ton", "gas_empty", f"На горячем кошельке {money.fmt(hot_ton, 4)} TON: "
                                        "сбор USDT с адресов пополнения и выплаты остановлены. Пополните TON: "
                                        f"{friendly(hot_address())}", minutes=180)
                await s.commit()
                return
            await _send(s, TonTransfer(wallet=HOT, kind="gas", ref=f"addr:{a.id}", asset="TON", amount=GAS_TOPUP,
                                       to_address=a.address))
            continue
        await _send(s, TonTransfer(wallet=a.label, kind="sweep", ref=f"addr:{a.id}", asset="USDT", amount=usdt,
                                   to_address=hot_address()))


# ---------- paying withdrawals from the hot wallet ----------

async def in_flight_usdt(s: AsyncSession) -> Decimal:
    """USDT the hot wallet has sent but the indexer may not have subtracted yet."""
    return Decimal(await s.scalar(select(func.coalesce(func.sum(TonTransfer.amount), 0)).where(
        TonTransfer.wallet == HOT, TonTransfer.asset == "USDT", TonTransfer.status.in_(("sending", "sent")))))


async def queue_need(s: AsyncSession) -> tuple[int, Decimal]:
    n, total = (await s.execute(select(func.count(Withdrawal.id), func.coalesce(func.sum(
        Withdrawal.amount - Withdrawal.fee), 0)).where(Withdrawal.method == "ton", Withdrawal.status == "queued"))).one()
    return n, Decimal(total)


async def _payouts(s: AsyncSession) -> list[tuple[Withdrawal, str]]:
    out: list = []
    ids = (await s.scalars(select(Withdrawal.id).where(Withdrawal.method == "ton", Withdrawal.status == "queued")
                           .order_by(Withdrawal.id).limit(20))).all()
    if not ids:
        return out
    usdt = await chain.usdt_balance(hot_address()) - await in_flight_usdt(s)
    _, ton = await chain.state(HOT)
    for wid in ids:
        wd = await s.get(Withdrawal, wid, with_for_update=True, populate_existing=True)
        if wd is None or wd.status != "queued":
            await s.commit()
            continue
        need = wd.amount - wd.fee
        try:
            to = raw(wd.address)
        except ValueError:
            await _refund(s, wd, "Адрес вывода не распознан", out)
            await s.commit()
            continue
        if ton < HOT_RESERVE + JETTON_TON:
            await events.alert_once(s, "app:ton", "gas_empty", f"На горячем кошельке {money.fmt(ton, 4)} TON — выплаты "
                                    f"стоят. Пополните TON: {friendly(hot_address())}", minutes=180)
            await s.commit()
            break
        if usdt < need:  # first in, first out: a big withdrawal is not overtaken by smaller ones
            n, total = await queue_need(s)
            await events.alert_once(s, "app:ton", "payout_queue", f"Выводы ждут USDT: {n} на {money.usdt(total)} USDT, "
                                    f"на горячем кошельке свободно {money.usdt(max(usdt, Decimal(0)))} USDT. "
                                    f"Пополните USDT: {friendly(hot_address())}", minutes=60)
            await s.commit()
            break

        def mark(tr: TonTransfer, wd=wd) -> None:
            wd.status, wd.transfer_id, wd.error = "sending", tr.id, None
            events.add(s, f"wd:{wd.id}", "sending", f"Отправка {money.usdt(wd.amount - wd.fee)} USDT с горячего "
                       f"кошелька: сообщение #{tr.id}, seqno {tr.seqno}", wd.user_id)

        if not await _send(s, TonTransfer(wallet=HOT, kind="payout", ref=f"wd:{wd.id}", asset="USDT", amount=need,
                                          to_address=to, memo=wd.memo), mark):
            await s.commit()
            break  # the hot wallet has a message in flight: the next cycle goes on
        usdt -= need
        ton -= JETTON_TON
    return out


# ---------- the cycle ----------

@dataclass
class Report:
    credited: list[Deposit] = field(default_factory=list)
    finished: list[tuple[Withdrawal, str]] = field(default_factory=list)  # (withdrawal, done | refunded)
    errors: list[str] = field(default_factory=list)


async def cycle(s: AsyncSession) -> Report:
    rep = Report()
    if chain is None:
        return rep
    async with busy:
        wake.clear()
        steps = (("зачисление", lambda: _scan(s)), ("сверка отправок", lambda: _settle(s)),
                 ("сбор на горячий кошелёк", lambda: _sweeps(s)), ("выплаты", lambda: _payouts(s)),
                 ("сверка новых отправок", lambda: _settle(s)))  # what was just sent may be on chain already
        for name, step in steps:
            try:
                result = await step()
            except Exception as e:  # noqa: BLE001 - one failed step never stops the others
                log.warning("ton %s: %s", name, e, exc_info=not isinstance(e, ChainError))
                await s.rollback()
                rep.errors.append(f"{name}: {e}"[:200])
                continue
            if name == "зачисление":
                rep.credited += result
            elif result:
                rep.finished += result
        try:
            usdt, ton = await hot_balances(max_age=0)
            if ton < HOT_LOW:
                await events.alert_once(s, "app:ton", "gas_low", f"На горячем кошельке {money.fmt(ton, 4)} TON — "
                                        f"пополните, иначе выплаты и сбор USDT остановятся: "
                                        f"{friendly(hot_address())}", minutes=360)
        except Exception as e:  # noqa: BLE001
            rep.errors.append(f"баланс: {e}"[:200])
        if rep.errors:
            await events.alert_once(s, "app:ton", "chain_error", "TON: " + "; ".join(rep.errors), minutes=60)
        await s.commit()
    return rep


async def admin_send(s: AsyncSession, admin_id: int, asset: str, to: str, amount: Decimal) -> str:
    """An owner moves USDT or TON from the hot wallet to his address. "" or why not. Commits."""
    if chain is None:
        return "TON_SEED не задан"
    try:
        await asyncio.wait_for(busy.acquire(), 30)
    except asyncio.TimeoutError:
        return "Кошелёк занят отправкой — повторите через минуту"
    try:
        usdt, ton = await hot_balances(max_age=0)
        if asset == "USDT" and amount > usdt - await in_flight_usdt(s):
            return f"На горячем кошельке свободно {money.usdt(usdt - await in_flight_usdt(s))} USDT"
        if (asset == "TON" and amount > ton - HOT_RESERVE) or (asset == "USDT" and ton < HOT_RESERVE + JETTON_TON):
            return f"Не хватит TON на комиссию: на кошельке {money.fmt(ton, 4)} TON, резерв {HOT_RESERVE} TON"
        tr = TonTransfer(wallet=HOT, kind="admin", ref=f"user:{admin_id}", asset=asset, amount=amount, to_address=raw(to))
        events.add(s, "app:ton", "admin_out", f"Владелец {admin_id} отправляет {amount:f} {asset} с горячего кошелька на "
                   f"{to}", admin_id, alert=True)
        if not await _send(s, tr):
            await s.rollback()
            return "Предыдущее сообщение кошелька ещё в пути — повторите через минуту"
        return ""
    except ChainError as e:
        return f"Сеть не ответила: {e}"
    finally:
        busy.release()


async def retire_legacy(s: AsyncSession) -> list[Withdrawal]:
    """xRocket is gone: its leftovers must not hang with users' money. Queued withdrawals it never paid go back to
    the balance (returned for the users to be told); ones it may have paid and still-open invoices are left to an
    owner, who is alerted once; the old token is deleted. Idempotent, commits."""
    from bot.models import Setting
    refunded = list((await s.scalars(select(Withdrawal).where(
        Withdrawal.method != "ton", Withdrawal.status == "queued").with_for_update())).all())
    for wd in refunded:
        wd.status, wd.error = "cancelled", "xRocket отключён: вывод не отправлялся, сумма возвращена"
        await money.add(s, wd.user_id, wd.amount, "withdraw_refund", f"wd:{wd.id}")
        events.add(s, f"wd:{wd.id}", "cancelled", f"xRocket отключён, вывод не отправлялся: {money.usdt(wd.amount)} "
                   "USDT возвращены на баланс — пользователь выведет их в сети TON", wd.user_id, notice=True)
    open_wds = await s.scalar(select(func.count(Withdrawal.id)).where(
        Withdrawal.method != "ton", Withdrawal.status.in_(("pending", "unknown", "sent"))))
    open_deps = (await s.scalars(select(Deposit).where(Deposit.tx_hash.is_(None),
                                                       Deposit.status.in_(("new", "active"))))).all()
    for dep in open_deps:
        dep.status = "expired"
        events.add(s, f"dep:{dep.id}", "expired", "xRocket отключён: счёт закрыт без проверки оплаты", dep.user_id)
    if open_wds or open_deps:
        await events.alert_once(s, "app:ton", "legacy", f"xRocket отключён. Проверьте в приложении xRocket вручную: "
                                f"выводов с неизвестным итогом — {open_wds} (решение — в карточке вывода), открытых "
                                f"счетов пополнения — {len(open_deps)} (оплаченные зачислите корректировкой баланса)",
                                minutes=60 * 24 * 365)
    if await s.get(Setting, "xrocket_token"):
        await s.delete(await s.get(Setting, "xrocket_token"))  # a secret of a service that is no longer used
    await s.commit()
    return refunded


if __name__ == "__main__":  # python -m bot.services.ton seed | address
    import secrets
    import sys
    if sys.argv[1:] == ["seed"]:
        print(secrets.token_hex(32))
    elif sys.argv[1:] == ["address"]:
        print(friendly(hot_address()) if enabled() else "TON_SEED is not set")
    else:
        print("usage: python -m bot.services.ton seed | address")
