"""Test bench: real dispatcher + middlewares + handlers, fake Telegram transport, fake xRocket.

Database: SQLite in memory by default; every test using `db_url` also runs on PostgreSQL when
P2P_TEST_PG is set (e.g. postgresql+asyncpg://p2p@127.0.0.1:55432/p2p_test). The PG schema is
dropped and re-created for every test.
"""
import html
import itertools
import os
import re
from datetime import datetime
from html.parser import HTMLParser

os.environ.setdefault("BOT_TOKEN", "123:abc")
os.environ["ADMIN_IDS"] = "[1, 2]"  # 2 = second admin for approvals
os.environ["LOG_CHAT_ID"] = "1"  # never use the log chat from a developer's .env
os.environ["EMOJI_MODE"] = "premium"

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
from bot.services import settings, xrocket  # noqa: E402

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


class FakeRocket:
    def __init__(self):
        self.cheques = []
        self.invoice_status = "paid"
        self.payments = [{"id": "p1", "status": "paid", "receiveAmount": "98.5", "receiveCurrency": "USDT"}]
        self.cheque_error: xrocket.XRocketError | None = None
        self.lookup: dict | xrocket.XRocketError = xrocket.XRocketError("app_cheque_not_found", status=404)
        self.withdrawals: dict[str, dict] = {}  # clientWithdrawalId -> xRocket withdrawal
        self.withdrawal_calls: list[tuple] = []
        self.withdrawal_status = "CREATED"
        self.withdrawal_error: xrocket.XRocketError | None = None
        self.invoices: list[tuple] = []  # (amount or None, client id, min payment)
        self.addresses: list[tuple] = []  # (invoice id, network)
        self.networks = ["TON", "TRX", "ETH", "BSC", "SOL"]

    async def create_invoice(self, amount, client_id, description, min_payment=None, expires_ms=3_600_000):
        self.invoices.append((amount, client_id, min_payment))
        return {"id": f"inv{len(self.invoices)}", "links": {"telegramBotLink": "https://t.me/xRocket?start=inv1"}}

    async def payment_address(self, invoice_id, network):
        self.addresses.append((invoice_id, network))
        return {"address": f"{network}addr{len(self.addresses)}" + "x" * 30, "payNetwork": network,
                "expiresAt": "2099-01-01T00:00:00Z"}

    async def usdt_networks(self):
        return self.networks

    async def get_invoice(self, invoice_id):
        return {"id": invoice_id, "status": self.invoice_status}

    async def get_invoice_by_client(self, client_id):
        return {"id": "inv1", "status": self.invoice_status, "links": {"telegramBotLink": "https://t.me/x"}}

    async def get_invoice_payments(self, invoice_id):
        return self.payments

    async def create_cheque(self, amount, client_id, tg_id, description):
        if self.cheque_error:
            raise self.cheque_error
        self.cheques.append((amount, client_id, tg_id))
        return {"chequeId": "c1", "links": {"telegramBotLink": "https://t.me/xRocket?start=c1"}}

    async def get_cheque_by_client(self, client_id):
        if isinstance(self.lookup, Exception):
            raise self.lookup
        return self.lookup

    async def delete_cheque_by_client(self, client_id):
        pass

    async def balances(self):
        return [{"asset": "USDT", "available": "1000"}]

    async def create_withdrawal(self, client_id, network, address, amount, comment):
        if self.withdrawal_error:
            raise self.withdrawal_error
        w = self.withdrawals.setdefault(client_id, {"status": self.withdrawal_status, "amount": str(amount),
                                                    "address": address, "comment": comment, "network": network})
        self.withdrawal_calls.append((client_id, network, address, amount, comment))
        return dict(w)

    async def get_withdrawal(self, client_id):
        if client_id not in self.withdrawals:
            raise xrocket.XRocketError("app_withdrawal_not_found", status=404)
        return dict(self.withdrawals[client_id])

    async def withdrawal_quota(self, network):
        return {"withdrawMinSize": "0.5", "withdrawFee": "0.1", "withdrawFeeAsset": "USDT", "precision": 6}


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
    xrocket._usdt = None
    xrocket._quotas.clear()
    xrocket._networks = None
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
        self.rocket = FakeRocket()
        xrocket.rocket = self.rocket
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
