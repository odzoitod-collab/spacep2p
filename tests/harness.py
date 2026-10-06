"""Test bench: real dispatcher + middlewares + handlers, fake Telegram transport, fake TON network.

Database: SQLite in memory by default; every test using `db_url` also runs on PostgreSQL when
P2P_TEST_PG is set (e.g. postgresql+asyncpg://p2p@127.0.0.1:55432/p2p_test). The PG schema is
dropped and re-created for every test.
"""
import asyncio
import base64
import hashlib
import html
import itertools
import os
import re
import time
from datetime import datetime
from decimal import Decimal
from html.parser import HTMLParser

os.environ.setdefault("BOT_TOKEN", "123:abc")
os.environ["ADMIN_IDS"] = "[1, 2]"  # 2 = second admin for approvals
os.environ["LOG_CHAT_ID"] = "1"  # never use the log chat from a developer's .env
os.environ["EMOJI_MODE"] = "premium"
os.environ["TON_SEED"] = "5e" * 32  # a fixed test key: every wallet address is stable across runs
os.environ["TON_TESTNET"] = "false"
os.environ["TON_API_KEY"] = ""

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
from bot.services import settings, ton  # noqa: E402

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


class FakeChain:
    """The TON network as services/ton.Chain shows it: wallets with seqno and TON, USDT jetton balances, the indexer's
    jetton transfers. broadcast() executes a message like a v4 wallet: only with the current seqno and before
    valid_until."""

    def __init__(self):
        self.seqno: dict[str, int] = {}  # wallet label -> seqno
        self.ton: dict[str, Decimal] = {}  # raw address -> TON
        self.usdt: dict[str, Decimal] = {}  # raw owner -> USDT
        self.transfers: list[dict] = []  # what Toncenter v3 /jetton/transfers knows
        self.sent: list[tuple] = []  # executed messages: (label, asset, to raw, amount, memo, query_id)
        self.messages: dict[str, dict] = {}  # boc -> signed message
        self.apply = True  # broadcast reaches the network (False: lost on the way)
        self.abort = False  # jetton transfers are aborted on chain
        self.index = True  # executed jetton transfers show up in the indexer
        self.down = False  # Toncenter does not answer
        self.n = 0

    def _check(self):
        if self.down:
            raise ton.ChainError("toncenter: ConnectError down")

    def _transfer(self, source, destination, amount, query_id=0, aborted=False, master=None) -> str:
        self.n += 1
        h = hashlib.sha256(f"tx{self.n}".encode()).digest()
        self.transfers.append({
            "source": source, "destination": destination, "amount": str(int(Decimal(amount) * ton.USDT_UNIT)),
            "jetton_master": master or ton.usdt_master(), "transaction_hash": base64.b64encode(h).decode(),
            "transaction_now": int(time.time()), "transaction_aborted": aborted, "query_id": str(query_id)})
        return h.hex()

    def pay(self, uid, amount, purpose="deposit", master=None, aborted=False) -> str:
        """Someone sends USDT to the user's personal address. Returns the transaction hash (hex)."""
        to = ton.address(f"{purpose}:{uid}")
        if not aborted and master is None:
            self.usdt[to] = self.usdt.get(to, Decimal(0)) + Decimal(amount)
        return self._transfer(ton.raw("UQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_p0p"), to, amount, aborted=aborted,
                              master=master)

    def fund_hot(self, usdt="0", gas="10"):
        hot = ton.hot_address()
        self.usdt[hot] = self.usdt.get(hot, Decimal(0)) + Decimal(usdt)
        self.ton[hot] = self.ton.get(hot, Decimal(0)) + Decimal(gas)

    async def state(self, label):
        self._check()
        return self.seqno.get(label, 0), self.ton.get(ton.address(label), Decimal(0))

    async def build(self, tr):
        boc = f"boc{tr.id}"
        self.messages[boc] = {"wallet": tr.wallet, "seqno": tr.seqno, "valid_until": tr.valid_until,
                              "asset": tr.asset, "to": tr.to_address, "amount": Decimal(tr.amount), "memo": tr.memo,
                              "query_id": tr.id}
        return boc, hashlib.sha256(boc.encode()).hexdigest()

    async def broadcast(self, boc):
        self._check()
        m = self.messages[boc]
        label, src = m["wallet"], ton.address(m["wallet"])
        if not self.apply or self.seqno.get(label, 0) != m["seqno"] or time.time() > m["valid_until"]:
            return  # a v4 wallet refuses it: nothing happens
        self.seqno[label] = m["seqno"] + 1
        fee = ton.JETTON_TON if m["asset"] == "USDT" else m["amount"]
        self.ton[src] = self.ton.get(src, Decimal(0)) - fee
        if m["asset"] == "TON":
            self.ton[m["to"]] = self.ton.get(m["to"], Decimal(0)) + m["amount"]
        else:
            aborted = self.abort or self.usdt.get(src, Decimal(0)) < m["amount"]
            if not aborted:
                self.usdt[src] -= m["amount"]
                self.usdt[m["to"]] = self.usdt.get(m["to"], Decimal(0)) + m["amount"]
            if self.index:
                self._transfer(src, m["to"], m["amount"], m["query_id"], aborted)
        self.sent.append((label, m["asset"], m["to"], m["amount"], m["memo"], m["query_id"]))

    async def incoming(self, owners, since):
        self._check()
        return [t for t in self.transfers if t["destination"] in owners and t["transaction_now"] >= since]

    async def outgoing(self, owner, since):
        self._check()
        return [t for t in self.transfers if t["source"] == owner and t["transaction_now"] >= since]

    async def usdt_balance(self, owner):
        self._check()
        return self.usdt.get(owner, Decimal(0))

    async def check(self):
        self._check()

    async def close(self):
        pass


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
    ton._hot = None
    ton.busy = asyncio.Lock()
    ton.wake = asyncio.Event()
    ton.POLL, ton.WAIT = 0, 0.01
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
        await s.commit()
        await settings.load(s)


def make_dp() -> Dispatcher:
    """A fresh dispatcher = a restarted bot process (in-memory caches are lost, stored dialog state is not)."""
    return build_dispatcher()


class Bench:
    def __init__(self):
        self.session = FakeSession()
        self.bot = Bot("123:abc", session=self.session, default=DefaultBotProperties(parse_mode="HTML"))
        self.chain = FakeChain()
        ton.chain = self.chain
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


async def ton_cycle(b) -> "ton.Report":
    from bot import tasks
    return await tasks.ton_cycle(b.bot)
