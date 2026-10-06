"""Test bench: real dispatcher + middlewares + handlers, fake Telegram transport, fake BNB Smart Chain (FakeBsc).

Database: SQLite in memory by default; every test using `db_url` also runs on PostgreSQL when
P2P_TEST_PG is set (e.g. postgresql+asyncpg://p2p@127.0.0.1:55432/p2p_test). The PG schema is
dropped and re-created for every test.
"""
import asyncio
import html
import itertools
import os
import re
from datetime import datetime
from decimal import Decimal
from html.parser import HTMLParser

os.environ.setdefault("BOT_TOKEN", "123:abc")
os.environ["ADMIN_IDS"] = "[1, 2]"  # 2 = second admin for approvals
os.environ["LOG_CHAT_ID"] = "1"  # never use the log chat from a developer's .env
os.environ["EMOJI_MODE"] = "premium"
WORDS = "test test test test test test test test test test test junk"  # the well-known test seed (Hardhat)
os.environ["BSC_MNEMONIC"] = WORDS  # the BEP-20 desk is on in every test, with stable addresses
os.environ["MASTER_SECRET"] = ""
os.environ["BSC_RPC_URLS"] = ""

from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError  # noqa: E402
from aiogram.types import (Animation, CallbackQuery, Chat, ChatInviteLink, ChatMemberAdministrator, ChatMemberLeft,  # noqa: E402
                           Document,
                           File, ForumTopic, Message, MessageId, PhotoSize, Update)
from aiogram.types import User as TgUser  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from bot import models  # noqa: E402
from bot.emoji import NavButton  # noqa: E402
from bot.app import build_dispatcher  # noqa: E402
from bot import ui  # noqa: E402
from bot.api import server as api_server  # noqa: E402
from bot.handlers import logchat  # noqa: E402
from bot.services import bsc, settings  # noqa: E402
import rlp  # noqa: E402
from eth_account import Account  # noqa: E402
from eth_utils import keccak, to_checksum_address  # noqa: E402

HOT = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"  # m/44'/60'/0'/0/0 of WORDS — MetaMask «Account 1»
EXCH = "0x28C6c06298d514Db089934071355E5743bf21d60"  # an exchange that sends deposits
GWEI = 10 ** 9


def W(x) -> int:
    """USDT or BNB -> wei (18 decimals)."""
    return int(Decimal(str(x)) * 10 ** 18)

ui.MIN_GAP = 0  # no pacing delays in tests

PG = os.environ.get("P2P_TEST_PG")
ids = itertools.count(1000)
ALLOWED_TAGS = {"b", "i", "u", "s", "code", "pre", "a", "blockquote", "tg-emoji", "tg-time", "tg-spoiler"}


EMOJI_RANGES = [(0x1F000, 0x1FAFF), (0x2600, 0x27BF), (0x2B00, 0x2BFF), (0x2190, 0x21FF), (0x2300, 0x23FF),
                (0x2100, 0x214F), (0x203C, 0x203C), (0x2049, 0x2049), (0x3030, 0x3030), (0xA9, 0xAE)]


def is_emoji(s: str) -> bool:
    chars = [c for c in s if c not in "\ufe0f\u200d"]
    return bool(chars) and all(any(a <= ord(c) <= b for a, b in EMOJI_RANGES) for c in chars)


def check_entities(method) -> None:
    """Like Telegram: custom emoji must wrap a real emoji, otherwise ENTITY_TEXT_INVALID."""
    body = getattr(method, "text", None) or getattr(method, "caption", None) or ""
    for inner in re.findall(r"<tg-emoji[^>]*>(.*?)</tg-emoji>", body):
        if not is_emoji(inner):
            raise TelegramBadRequest(method=method, message="Bad Request: ENTITY_TEXT_INVALID")


class FakeSession(BaseSession):
    """Records every Bot API call; users in `blocked` behave like they blocked the bot."""

    def __init__(self):
        super().__init__()
        self.calls = []
        self.blocked: set[int] = set()
        self.fail_once: set[int] = set()  # next send/edit to this chat fails like a Telegram outage
        self.kinds: dict[int, str] = {}  # message_id -> photo | text, to mimic edit restrictions
        self.files: dict[str, bytes] = {}  # file_id -> content for bot.download(); default: a valid PDF
        self.forums: dict[int, set[int]] = {}  # forum chat id -> existing topic thread ids
        self.outsiders: set[tuple[int, int]] = set()  # (chat, user): get_chat_member says he is not there

    async def make_request(self, bot, method, timeout=None):
        chat = getattr(method, "chat_id", None)
        if chat in self.blocked:
            raise TelegramForbiddenError(method=method, message="Forbidden: bot was blocked by the user")
        check_entities(method)
        if chat in self.fail_once and type(method).__name__.startswith(("Send", "Edit")):
            self.fail_once.discard(chat)
            raise TelegramBadRequest(method=method, message="Bad Request: simulated failure")
        name = type(method).__name__
        if name == "EditMessageCaption" and len(plain(method.caption or "")) > 1024:  # rejected, never recorded
            raise TelegramBadRequest(method=method, message="Bad Request: message caption is too long")
        self.calls.append(method)
        if name == "GetChat":
            if chat in self.forums:
                return Chat(id=chat, type="supergroup", is_forum=True, title="log")
            return Chat(id=chat or 0, type="private")
        if name == "CreateForumTopic":
            tid = next(ids)
            self.forums[chat].add(tid)
            return ForumTopic(message_thread_id=tid, name=method.name, icon_color=method.icon_color or 7322096)
        thread = getattr(method, "message_thread_id", None)
        if chat in self.forums and thread is not None and thread not in self.forums[chat]:
            raise TelegramBadRequest(method=method, message="Bad Request: message thread not found")
        if name == "GetMe":
            return TgUser(id=123, is_bot=True, first_name="Strait Pay", username="straitpay_bot")
        if name == "CreateChatInviteLink":
            return ChatInviteLink(invite_link=f"https://t.me/+inv{next(ids)}", creator=TgUser(id=123, is_bot=True,
                                  first_name="bot"), creates_join_request=False, is_primary=False, is_revoked=False,
                                  name=method.name, member_limit=method.member_limit)
        if name == "GetChatMember" and (chat, method.user_id) in self.outsiders:
            return ChatMemberLeft(user=TgUser(id=method.user_id, is_bot=False, first_name="x"))
        if name == "GetChatMember":
            return ChatMemberAdministrator(user=TgUser(id=method.user_id, is_bot=True, first_name="bot"),
                                           can_be_edited=False, is_anonymous=False, can_manage_chat=True,
                                           can_delete_messages=True, can_manage_video_chats=True,
                                           can_restrict_members=True, can_promote_members=False,
                                           can_change_info=True, can_invite_users=True, can_post_stories=False,
                                           can_edit_stories=False, can_delete_stories=False, can_send_welcome_messages=False,
                                           can_pin_messages=True)
        if name == "CopyMessage":
            return MessageId(message_id=next(ids))
        if name == "GetFile":
            return File(file_id=method.file_id, file_unique_id="f", file_path=f"documents/{method.file_id}")
        mid = getattr(method, "message_id", None)
        if (name == "EditMessageText" and self.kinds.get(mid) == "photo") or \
                (name == "EditMessageCaption" and self.kinds.get(mid) == "text"):
            raise TelegramBadRequest(method=method, message="Bad Request: there is no text in the message to edit")
        if name.startswith("Send"):
            mid = next(ids)
            self.kinds[mid] = "photo" if name in ("SendPhoto", "SendAnimation") else "text"
            if name == "SendMessage" and thread is not None:
                return Message(message_id=mid, date=datetime.now(), chat=Chat(id=chat, type="supergroup"),
                               text="x", message_thread_id=thread)
            if name == "SendAnimation":  # the banner: like Telegram, an animation also carries `document`
                return banner_message(mid, chat)
            if name == "SendPhoto":
                return Message(message_id=mid, date=datetime.now(), chat=Chat(id=chat or 0, type="private"),
                               photo=[PhotoSize(file_id="banner-file-id", file_unique_id="b", width=1, height=1)])
            if name == "SendDocument":  # an uploaded file gets an id, like in Telegram
                return Message(message_id=mid, date=datetime.now(), chat=Chat(id=chat or 0, type="private"),
                               document=Document(file_id=f"up{mid}", file_unique_id=f"uu{mid}"))
            return Message(message_id=mid, date=datetime.now(), chat=Chat(id=chat or 0, type="private"), text="x")
        if name == "SendPhoto":
            return Message(message_id=next(ids), date=datetime.now(), chat=Chat(id=chat or 0, type="private"),
                           photo=[PhotoSize(file_id="banner-file-id", file_unique_id="b", width=1, height=1)])
        if name.startswith(("Send", "Edit")):
            return Message(message_id=next(ids), date=datetime.now(),
                           chat=Chat(id=chat or 0, type="private"), text="x")
        return True

    async def stream_content(self, url, *a, **k):
        yield self.files.get(url.rsplit("/", 1)[-1], b"%PDF-1.4\n% fake bank receipt\n")

    async def close(self):
        pass

    def texts(self, chat: int | None = None) -> list[str]:
        out = []
        for m in self.calls:
            t = getattr(m, "text", None) or getattr(m, "caption", None)
            if t and (chat is None or getattr(m, "chat_id", None) == chat):
                out.append(t)
        return out

    def last(self, chat: int) -> str:
        return self.texts(chat)[-1]

    def alerts(self) -> list[str]:
        return [m.text for m in self.calls if type(m).__name__ == "AnswerCallbackQuery" and m.text]

    def buttons(self, chat: int) -> list[str]:
        """callback_data of the last keyboard shown to `chat`."""
        for m in reversed(self.calls):
            if getattr(m, "chat_id", None) == chat and getattr(m, "reply_markup", None):
                return [b.callback_data or b.url for row in m.reply_markup.inline_keyboard for b in row]
        return []


class FakeBsc:
    def __init__(self):
        self.head = self.final = 10_000
        self.usdt: dict[str, int] = {}
        self.bnb: dict[str, int] = {}
        self.nonce: dict[str, int] = {}
        self.pool: dict[str, dict] = {}
        self.receipts: dict[str, dict] = {}
        self.logs: list[dict] = []
        self.gp = GWEI
        self.down = False
        self.accept = True
        self.revert = 0  # the next N token transfers revert
        self.sends: list[str] = []

    def _log(self, src, dst, wei, h, li=0):
        self.logs.append({"address": bsc.USDT.lower(), "topics": [bsc.TRANSFER_TOPIC, bsc.pad32(src), bsc.pad32(dst)],
                          "data": hex(wei), "blockNumber": hex(self.head), "logIndex": hex(li), "transactionHash": h,
                          "blockTimestamp": hex(1_700_000_000 + self.head), "removed": False})

    def pay(self, to, amount, src=EXCH) -> str:
        """A transfer from outside, in a new final block."""
        self.head += 1
        self.final = self.head
        h = "0x" + os.urandom(32).hex()
        self.usdt[to] = self.usdt.get(to, 0) + W(amount)
        self._log(src, to, W(amount), h)
        return h

    def fund(self, a, usdt=0, bnb=0):
        self.usdt[a] = self.usdt.get(a, 0) + W(usdt)
        self.bnb[a] = self.bnb.get(a, 0) + W(bnb)

    def fund_hot(self, usdt=0, bnb="0.05"):
        self.fund(HOT, usdt, bnb)

    def usdt_of(self, a) -> Decimal:
        return Decimal(self.usdt.get(a, 0)) / 10 ** 18

    def replace(self, a):
        """Somebody used the address's next nonce outside the bot: our pending transaction can never be mined."""
        self.nonce[a] = self.nonce.get(a, 0) + 1
        self.pool = {h: t for h, t in self.pool.items() if not (t["from"] == a and t["nonce"] < self.nonce[a])}

    @staticmethod
    def _decode(data: str) -> tuple[str, int]:
        assert data[2:10] == bsc.SEL_TRANSFER
        return to_checksum_address("0x" + data[34:74]), int(data[74:138], 16)

    def mine(self):
        """Everything that can go, in nonce order, into one new final block."""
        self.head += 1
        self.final = self.head
        li, moved = 0, True
        while moved:
            moved = False
            for h, t in sorted(self.pool.items(), key=lambda kv: kv[1]["nonce"]):
                if t["nonce"] != self.nonce.get(t["from"], 0) or self.bnb.get(t["from"], 0) < t["gas"] * t["gp"] + t["value"]:
                    continue
                del self.pool[h]
                moved = True
                self.nonce[t["from"]] = t["nonce"] + 1
                token = t["to"] == bsc.USDT
                self.bnb[t["from"]] -= (50_000 if token else 21_000) * t["gp"] + t["value"]
                self.bnb[t["to"]] = self.bnb.get(t["to"], 0) + t["value"]
                ok = True
                if token:
                    to, amount = self._decode(t["data"])
                    if self.revert or amount > self.usdt.get(t["from"], 0):
                        ok, self.revert = False, max(self.revert - 1, 0)
                    else:
                        self.usdt[t["from"]] -= amount
                        self.usdt[to] = self.usdt.get(to, 0) + amount
                        self._log(t["from"], to, amount, h, li)
                        li += 1
                self.receipts[h] = {"blockNumber": hex(self.head), "status": "0x1" if ok else "0x0"}

    def handle(self, method, params):
        if self.down:
            raise bsc.RpcError("node: ConnectError")
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getBlockByNumber":
            if params[0] == "finalized":
                return {"number": hex(self.final)}
            return {"number": params[0], "timestamp": hex(1_700_000_000 + int(params[0], 16))}
        if method == "eth_call":
            assert params[0]["to"] == bsc.USDT and params[0]["data"].startswith("0x" + bsc.SEL_BALANCE)
            return hex(self.usdt.get(to_checksum_address("0x" + params[0]["data"][-40:]), 0))
        if method == "eth_getBalance":
            return hex(self.bnb.get(params[0], 0))
        if method == "eth_gasPrice":
            return hex(self.gp)
        if method == "eth_getTransactionCount":
            a, tag = params
            n = self.nonce.get(a, 0)
            while tag == "pending" and any(t["from"] == a and t["nonce"] == n for t in self.pool.values()):
                n += 1
            return hex(n)
        if method == "eth_estimateGas":
            _, amount = self._decode(params[0]["data"])
            if amount > self.usdt.get(params[0]["from"], 0):
                raise bsc.RpcError("node: execution reverted: BEP20: transfer amount exceeds balance")
            return hex(50_000)
        if method == "eth_sendRawTransaction":
            raw = params[0]
            h = "0x" + keccak(hexstr=raw).hex()
            self.sends.append(h)
            if not self.accept:
                raise bsc.RpcError("node: boom")
            if h in self.pool or h in self.receipts:
                raise bsc.RpcError("node: already known")
            f = rlp.decode(bytes.fromhex(raw[2:]))
            nonce, gp, gas, to, value, data, v = (int.from_bytes(f[0], "big"), int.from_bytes(f[1], "big"),
                                                  int.from_bytes(f[2], "big"), f[3], int.from_bytes(f[4], "big"),
                                                  f[5], int.from_bytes(f[6], "big"))
            assert v in (35 + 2 * 56, 36 + 2 * 56)  # EIP-155 for chain 56, legacy gasPrice
            frm = Account.recover_transaction(raw)
            if nonce < self.nonce.get(frm, 0):
                raise bsc.RpcError("node: nonce too low")
            self.pool[h] = {"from": frm, "nonce": nonce, "gp": gp, "gas": gas, "to": to_checksum_address(to),
                            "value": value, "data": "0x" + data.hex()}
            return h
        if method == "eth_getTransactionReceipt":
            return self.receipts.get(params[0])
        if method == "eth_getLogs":
            f = params[0]
            lo, hi, targets = int(f["fromBlock"], 16), int(f["toBlock"], 16), set(f["topics"][2])
            return [x for x in self.logs if lo <= int(x["blockNumber"], 16) <= hi and x["topics"][2] in targets]
        raise AssertionError(method)


def tg(uid):
    return TgUser(id=uid, is_bot=False, first_name=f"U{uid}", username=f"u{uid}")


def msg(uid, text=None, **kw):
    return Update(update_id=next(ids), message=Message(
        message_id=next(ids), date=datetime.now(), chat=Chat(id=uid, type="private"),
        from_user=tg(uid), text=text, **kw))


def cb(uid, data):
    return Update(update_id=next(ids), callback_query=CallbackQuery(
        id=str(next(ids)), from_user=tg(uid), chat_instance="ci", data=data,
        message=Message(message_id=999, date=datetime.now(), chat=Chat(id=uid, type="private"), text="x")))


def banner_message(mid, chat):
    return Message(message_id=mid, date=datetime.now(), chat=Chat(id=chat or 0, type="private"), caption="x",
                   animation=Animation(file_id="banner-file-id", file_unique_id="b", width=1280, height=720, duration=5),
                   document=Document(file_id="banner-file-id", file_unique_id="b"))


async def cb_main(uid, data, session=None):
    """Click on the user's main (UI) message, as in real use: the screen is edited in place. With `session` the
    clicked message looks exactly like what was sent (a banner animation or a text message)."""
    async with models.Session() as s:
        mid = (await s.get(models.User, uid)).ui_msg_id
    if session is not None and session.kinds.get(mid) == "photo":
        message = banner_message(mid, uid)
    else:
        message = Message(message_id=mid, date=datetime.now(), chat=Chat(id=uid, type="private"), text="x")
    return Update(update_id=next(ids), callback_query=CallbackQuery(
        id=str(next(ids)), from_user=tg(uid), chat_instance="ci", data=data, message=message))


async def reset_db(url: str) -> None:
    ui._banner_id = None  # every test starts like a fresh process: banner not uploaded yet
    ui._emoji_off_until = 0.0
    bsc._use(WORDS)  # the desk is on; a test that needs it off calls bsc._use(None)
    bsc.error = ""
    bsc.lock = asyncio.Lock()
    bsc.wake = asyncio.Event()
    api_server.limiter._buckets.clear()
    logchat._forum.clear()
    logchat._topics.clear()
    if url.startswith("postgresql"):
        eng = create_async_engine(url)
        async with eng.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
        await eng.dispose()
    if models.engine is not None:
        await models.engine.dispose()
    await models.init_db(url)
    async with models.Session() as s:
        await settings.put(s, "signup_review", "0")  # scenarios start from approved users; test_signup turns it on
        await settings.put(s, "join_required", "0")  # the entry gate has its own tests
        await settings.put(s, "withdraw_turnover", "0")  # so has the turnover rule (tests/test_turnover.py)
        await settings.put(s, "order_first_wave", "0")  # requests to everyone at once; the waves have their own test
        await settings.put(s, "log_all", "1")  # scenarios read every step in the log; the quiet default has its own test
        await s.commit()
        await settings.load(s)


def make_dp() -> Dispatcher:
    """A fresh dispatcher = a restarted bot process (in-memory caches are lost, stored dialog state is not)."""
    return build_dispatcher()


class Bench:
    def __init__(self):
        self.session = FakeSession()
        self.bot = Bot("123:abc", session=self.session, default=DefaultBotProperties(parse_mode="HTML"))
        self.chain = FakeBsc()

        async def post(url, method, params):
            return self.chain.handle(method, params)
        bsc._post = post
        self.dp = make_dp()

    def restart(self):
        self.dp = make_dp()

    async def run(self, *updates):
        for u in updates:
            await self.dp.feed_update(self.bot, u)

    async def deliver(self) -> list[str]:
        """Run the alert outbox once; returns plain texts delivered to the log chat (admin 1)."""
        from bot import tasks
        before = len(self.session.texts(1))
        await tasks.deliver_alerts(self.bot)
        return [plain(t) for t in self.session.texts(1)[before:]]


# ---------- Telegram limits and HTML validity for everything the bot sent ----------

class _Html(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.errors, self.plain = [], [], []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED_TAGS:
            self.errors.append(f"tag <{tag}> not supported by Telegram")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.errors.append(f"unbalanced </{tag}>")

    def handle_data(self, data):
        self.plain.append(data)


def check_telegram_limits(session: FakeSession) -> None:
    for m in session.calls:
        name = type(m).__name__
        body, limit = (m.text, 4096) if hasattr(m, "text") and m.text else (getattr(m, "caption", None), 1024)
        if body and name != "AnswerCallbackQuery":
            p = _Html()
            p.feed(body)
            p.close()
            assert not p.errors and not p.stack, (p.errors, p.stack, body[:300])
            assert len("".join(p.plain)) <= limit, (name, len("".join(p.plain)), body[:200])
        if name == "AnswerCallbackQuery" and m.text:
            assert len(m.text) <= 200, m.text
        markup = getattr(m, "reply_markup", None)
        if markup is not None and hasattr(markup, "inline_keyboard"):
            flat = [b for row in markup.inline_keyboard for b in row]
            assert len(flat) <= 100
            rows = markup.inline_keyboard
            navs = [i for i, row in enumerate(rows) for b in row if isinstance(b, NavButton)]
            if navs:  # navigation: one button, alone in the last row
                assert navs == [len(rows) - 1] and len(rows[-1]) == 1, [[b.text for b in r] for r in rows]
            assert all(len(r) <= 2 for r in rows), [[b.text for b in r] for r in rows]  # one big or two small
            for b in flat:
                if b.callback_data:
                    assert len(b.callback_data.encode()) <= 64, b.callback_data
                assert 0 < len(b.text) <= 64, b.text


def plain(t: str) -> str:
    """Visible text; card fields (ui.cf) read as one line: "Label:\n ╰  value" -> "Label: value", a branch's lines
    lose their corners."""
    p = html.unescape(re.sub(r"<[^>]+>", "", t))
    p = re.sub(r":\n ╰  ", ": ", p)
    return re.sub(r"(?m)^[├╰] {2}", "", p)


async def bsc_tick(b) -> "bsc.Report":
    from bot import tasks
    return await tasks.bsc_tick(b.bot, 0)


async def deposit(b, uid: int, amount) -> str:
    """Someone sends USDT to the user's deposit address; then a tick credits it. Returns the tx hash."""
    async with models.Session() as s:
        addr = await bsc.deposit_address(s, uid)
    h = b.chain.pay(addr, amount)
    await bsc_tick(b)
    return h
