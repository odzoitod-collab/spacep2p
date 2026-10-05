"""Single-message UI: every screen edits the user's main bot message.

Screen layout used everywhere:
    <icon> <b>Title</b>
    status line (where the user is / what happened)
    <blockquote>facts: amounts, fees, deadlines</blockquote> — "• Label: value" lines become "<b>Label:</b> value"
    next step (what to do now)
    note (result of the last action: ok / warning)

Admin chat and logs use the card style (cf): the label as a quote, the value under it on a branch:
    <blockquote><b>Label:</b></blockquote>
    <b><code> ╰</code></b>  value
In a group (the admin chat) a button edits the very message it is under, in that chat: nothing goes to the private
chat. A command or an answer an admin types there gets its screen there too (`place`).
"""
import asyncio
import functools
import html
import logging
import re
from contextlib import suppress
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message,
                           ReplyParameters)

from bot.config import config
from bot.emoji import E, NavButton, back, kb, pe
from bot.models import User

esc = html.escape
BOT = ""  # the bot's username, known after the first update (middlewares.Context): deep links in texts
# Brand canon (docs/BRAND.md): the name is always "Strait Pay" — two words, capital S and P.
BRAND = "Strait Pay"
TAGLINE = "P2P-обмен USDT ⇄ RUB с защитой сделки"
MSK = timezone(timedelta(hours=3))
MIN_GAP = 1 / 25  # seconds between bot-initiated messages: stays under Telegram's ~30 msg/s limit
_pace = asyncio.Lock()
_last_sent = 0.0


async def paced(call):
    """Send a bot-initiated message: global pacing plus one retry after Telegram's RetryAfter."""
    global _last_sent
    for attempt in range(2):
        async with _pace:
            loop = asyncio.get_running_loop()
            wait = _last_sent + MIN_GAP - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            _last_sent = loop.time()
        try:
            return await call()
        except TelegramRetryAfter as e:
            if attempt:
                raise
            await asyncio.sleep(e.retry_after)


async def deep_link(bot: Bot, payload: str) -> str:
    """t.me link that opens the bot with /start <payload> (A–Z, a–z, 0–9, _ and -, up to 64 characters)."""
    global BOT
    BOT = (await bot.me()).username or BOT
    return f"https://t.me/{BOT}?start={payload}"


def alink(kind: str, oid: int | str, text: str) -> str:
    """`text` linking to the admin card of an object in the bot (/start a-<kind>-<id>, commands.cmd_start): a click
    in the admin chat or a log opens the card in the private chat with the bot. Plain text until the bot knows its
    username."""
    return f'<a href="https://t.me/{BOT}?start=a-{kind}-{oid}">{text}</a>' if BOT else text


def ulink(u, uid: int | None = None) -> str:
    """A person in admin texts: @username · name linking to his admin card, the id to copy."""
    uid = uid if uid is not None else getattr(u, "id", None)
    if uid is None:
        return "—"
    name = " · ".join(p for p in (f"@{esc(u.username)}" if u is not None and u.username else "",
                                  esc(u.name) if u is not None and u.name else "") if p) or "без имени"
    return f"{alink('user', uid, name)} · <code>{uid}</code>"


def mark(ch: str) -> str:
    """A plain status mark (🟢, 🛡, ✓...): none in the text-only mode."""
    return "" if config.emoji_mode == "none" else ch


def mention(u) -> str:
    """Who did it (an admin's verdict): the name opens his Telegram profile."""
    return f'<a href="tg://user?id={u.id}">{esc(u.name or str(u.id))}</a>'


def doc_url(slug: str) -> str | None:
    """The guide page docs_url/<slug>; "" is the API reference itself. None if no docs site is set."""
    from bot.services import settings
    base = settings.get("docs_url").rstrip("/")
    return (f"{base}/{slug}" if slug else base) if base else None


def doc(slug: str, text: str) -> str:
    """`text` with a hidden link to a guide page (plain text if no docs site is set)."""
    url = doc_url(slug)
    return f'<a href="{html.escape(url, quote=True)}">{text}</a>' if url else text


def person(u) -> str:
    """A person in event texts (the log chat escapes them): @username · name (id)."""
    if u is None:
        return "—"
    parts = [f"@{u.username}" if u.username else "", u.name or ""]
    return " · ".join(p for p in parts if p) + f" ({u.id})" if any(parts) else str(u.id)


def manual(text: str = "инструкции") -> str:
    """`text` with the seller manual link hidden in it (settings: manual_url); plain text if no link is set."""
    from bot.services import settings
    url = settings.get("manual_url")
    return f'<a href="{html.escape(url, quote=True)}">{text}</a>' if url else text


_FIELD = re.compile(r"^• ([A-Za-zА-Яа-яЁё][^:<>•—\n]{0,39}?): (.+)$", re.S)


def field(label: str, value: str) -> str:
    return f"<b>{label}:</b> {value}"


def quote(*lines: str) -> str:
    """Facts in a quote; a "• Label: value" line becomes a field with a bold label (and then the other lines of the
    quote lose their bullets too, so a list never mixes both looks)."""
    lines = [line for line in lines if line]
    fields = [_FIELD.match(line) for line in lines]
    if any(fields):
        lines = [field(f[1], f[2]) if f else line.removeprefix("• ") for f, line in zip(fields, lines)]
    return "<blockquote>" + "\n".join(lines) + "</blockquote>"


def title(emoji: str, text: str) -> str:
    return f"{emoji} <b>{text}</b>"


def section(icon: str, text: str) -> str:
    """A section header inside a screen: its own quote, the icon stays."""
    return f"<blockquote>{pe(icon)} <b>{text}</b></blockquote>"


def cf(label: str, *values: str, icon: str | None = None) -> str:
    """Card field (admin chat, logs): the label as a quote, its values under it on a branch. "" if no values."""
    values = [v for v in values if v]
    if not values:
        return ""
    head = f"<blockquote><b>{pe(icon) + ' ' if icon else ''}{label}:</b></blockquote>"
    if len(values) == 1:
        return f"{head}\n<b><code> ╰</code></b>  {values[0]}"
    return "\n".join([head, *[f"<b><code>{'╰' if i == len(values) - 1 else '├'}</code></b>  {v}"
                              for i, v in enumerate(values)]])


def card(*parts: str) -> str:
    """Card fields one under another (empty ones dropped)."""
    return "\n".join(p for p in parts if p)


def verdict(icon: str, text: str, admin=None) -> str:
    """The decision line under a card: ✅ Одобрено · <who>."""
    return f"{pe(icon)} <b>{text}" + (f" · {mention(admin)}" if admin is not None else "") + "</b>"


def stamp(dt: datetime | None = None) -> str:
    """Italic date-time at the bottom of a card, MSK."""
    dt = dt or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"<i>{dt.astimezone(MSK):%d.%m.%Y · %H:%M} МСК</i>"


def ok(text: str) -> str:
    return f"\n{pe('ok')} <b>{text}</b>"


def warn(text: str) -> str:
    return f"\n{pe('warn')} <i>{text}</i>"


def at(dt: datetime, fmt: str = "t") -> str:
    """Time rendered by Telegram in the viewer's timezone; MSK text for old clients."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(MSK)
    fallback = (f"{local:%d.%m.%Y}" if fmt == "d" else f"{local:%d.%m %H:%M} МСК" if "d" in fmt
                else f"{local:%H:%M} МСК")
    return f'<tg-time unix="{int(dt.timestamp())}" format="{fmt}">{fallback}</tg-time>'


_TG_EMOJI = re.compile(r"<tg-emoji[^>]*>.*?</tg-emoji> ?")
log = logging.getLogger(__name__)


def entity_error(e: Exception) -> bool:
    msg = str(e).upper()
    return isinstance(e, TelegramBadRequest) and ("ENTITY" in msg or "EMOJI" in msg or "CAN'T PARSE" in msg)


def no_custom_emoji(text: str) -> str:
    return _TG_EMOJI.sub("", text)


EMOJI_PAUSE = 3600  # after a rejection, send without custom emoji for an hour instead of failing every screen
_emoji_off_until = 0.0


async def safe_text(call, text: str):
    """Send/edit; if Telegram rejects a custom emoji (bot owner without Premium, bad id), retry without them."""
    global _emoji_off_until
    if "<tg-emoji" in text and asyncio.get_running_loop().time() < _emoji_off_until:
        text = no_custom_emoji(text)
    try:
        return await paced(lambda: call(text))
    except TelegramBadRequest as e:
        if not entity_error(e) or "<tg-emoji" not in text:
            raise
        log.warning("custom emoji rejected by Telegram, sending without them for %s s: %s", EMOJI_PAUSE, e)
        _emoji_off_until = asyncio.get_running_loop().time() + EMOJI_PAUSE
        return await paced(lambda: call(no_custom_emoji(text)))


_KEEP = None  # icons allowed after the title line: result notes (✅ / ⚠️)
_FALLBACKS = None
_HEAD = re.compile(r"^<blockquote>[^\n]*<b>[^\n]*</blockquote>$")  # a one-line quote with a bold header


def _declutter(text: str) -> str:
    """Icons only where they help: the title (first line) and the result note of an action. Every other line —
    facts, steps, section headers — is plain text: a wall of icons makes a screen harder to read, not easier."""
    global _KEEP, _FALLBACKS
    if _KEEP is None:
        _KEEP = (pe("ok"), pe("warn"))
        _FALLBACKS = tuple(sorted({fb for _, fb in E.values()}, key=len, reverse=True))
    lines = text.split("\n")
    for i in range(1, len(lines)):
        line = lines[i]
        body = line.removeprefix("<blockquote>")
        if body.startswith(_KEEP) and not line.startswith("<blockquote>"):
            continue
        if _HEAD.match(line):  # a section header or a card label (section, cf): its icon is the point
            continue
        line = _TG_EMOJI.sub("", line)
        if config.emoji_mode == "plain":  # plain mode: the same icons are ordinary emoji characters
            prefix = "<blockquote>" if line.startswith("<blockquote>") else ""
            rest = line[len(prefix):]
            for fb in _FALLBACKS:
                if rest.startswith(fb + " "):
                    rest = rest[len(fb) + 1:]
                    break
            line = prefix + rest
        lines[i] = line
    return "\n".join(lines)


def clean(text: str) -> str:
    text = _declutter(text)
    if config.emoji_mode == "none":  # icons are empty: drop the spaces they leave behind
        text = re.sub(r"(?m)(^|<blockquote>|<b>) +", r"\1", text)
    return text


def carries_file(m) -> bool:
    """A message whose file must stay as it is (a receipt, dispute evidence). Our banner screens are not such:
    Telegram fills `document` for every animation too (backward compatibility), so an animation or the banner photo
    is a screen, not a file."""
    if m.animation:
        return False
    if m.photo:
        return not (_banner_id and m.photo[-1].file_id == _banner_id)
    return bool(m.document or m.video)


# (admin id, chat id, topic) of an update an admin sent in the admin chat (middlewares.Context): his screens go there
place: ContextVar[tuple[int, int, int | None] | None] = ContextVar("place", default=None)
_group_screens: dict[tuple[int, int], int] = {}  # (admin id, chat id) -> his current screen message there
# the log card (handlers.logchat) under which a button was pressed: a screen opened on it can fold back into the card
card_ref: ContextVar[str | None] = ContextVar("card_ref", default=None)


def with_collapse(markup: InlineKeyboardMarkup | None, ref: str) -> InlineKeyboardMarkup:
    """The screen's keyboard plus «Свернуть в карточку» (back to the log card), above the navigation row."""
    rows = [list(r) for r in (markup.inline_keyboard if markup else [])]
    if any((b.callback_data or "").startswith("lg:") for r in rows for b in r):
        return markup
    fold = [InlineKeyboardButton(text="Свернуть в карточку", callback_data=f"lg:{ref}"[:64])]
    if rows and len(rows[-1]) == 1 and isinstance(rows[-1][0], NavButton):
        rows.insert(len(rows) - 1, fold)
    else:
        rows.append(fold)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def group_screen(uid: int, chat: int) -> int | None:
    return _group_screens.get((uid, chat))


def forget_group_screen(uid: int, chat: int) -> None:
    """A command in the group: the answer is a new message, the old screen stays as history."""
    _group_screens.pop((uid, chat), None)


async def _in_group(bot: Bot, uid: int, chat: int, thread: int | None, text: str,
                    markup: InlineKeyboardMarkup | None, mid: int | None, reply_to: int | None = None) -> None:
    """A screen in a group: edit message `mid` (its text or, for a card with a file, its caption) or post a new one
    in the same topic. It becomes the admin's current screen in that chat."""
    if mid:
        plain = lambda t: bot.edit_message_text(text=t, chat_id=chat, message_id=mid, reply_markup=markup,  # noqa: E731
                                                disable_web_page_preview=True)
        caption = lambda t: bot.edit_message_caption(chat_id=chat, message_id=mid, caption=t,  # noqa: E731
                                                     reply_markup=markup)
        for edit in (plain, caption):
            try:
                await safe_text(edit, text)
                _group_screens[(uid, chat)] = mid
                return
            except TelegramAPIError as e:  # deleted, too old, other kind of message...: the next way or a new one
                if "not modified" in str(e):
                    _group_screens[(uid, chat)] = mid
                    return
    m = await safe_text(lambda t: bot.send_message(
        chat, t, message_thread_id=thread, reply_markup=markup, disable_web_page_preview=True,
        disable_notification=True,
        reply_parameters=ReplyParameters(message_id=reply_to, allow_sending_without_reply=True) if reply_to else None,
    ), text)
    _group_screens[(uid, chat)] = m.message_id


def pressed_in_group(c: CallbackQuery) -> bool:
    return c.message is not None and c.message.chat.type != "private"


def files_to(c: CallbackQuery) -> tuple[int, int | None]:
    """(chat, topic) for files an admin asked for with a button: the admin chat's topic where he pressed it, or his
    private chat."""
    if pressed_in_group(c):
        return c.message.chat.id, getattr(c.message, "message_thread_id", None)
    return c.from_user.id, None


async def show(bot: Bot, user: User, text: str, markup: InlineKeyboardMarkup | None = None,
               src: CallbackQuery | Message | None = None) -> None:
    """Show a screen. A button press edits the very message it was pressed in (screen or notification) — that
    message becomes the current screen; older messages stay as history. Without a button press (a command, an
    answer to a question) the current screen is edited or, if there is none, a new one is sent.
    In a group (the admin chat) everything stays in that group: see `place`."""
    text = clean(text)
    if isinstance(src, CallbackQuery) and pressed_in_group(src):
        m = src.message
        keep = isinstance(m, Message) and carries_file(m)  # a card with a receipt or screenshot stays as it is
        ref = card_ref.get()
        if ref and not keep and not ref.startswith("signup:"):
            markup = with_collapse(markup, ref)
        await _in_group(bot, user.id, m.chat.id, m.message_thread_id if isinstance(m, Message) else None, text,
                        markup, None if keep else m.message_id, reply_to=m.message_id if keep else None)
        with suppress(TelegramAPIError):
            await src.answer()
        return
    here = place.get()
    if here is not None and here[0] == user.id and src is None:
        _, chat, thread = here
        return await _in_group(bot, user.id, chat, thread, text, markup, _group_screens.get((user.id, chat)))
    if isinstance(src, CallbackQuery):
        try:
            m = src.message
            if m and m.chat.id == user.id and not carries_file(m):
                user.ui_msg_id = m.message_id
                await _render(bot, user, text, markup)
            else:  # the admin log chat, or a message carrying a file (receipt): keep it, answer below
                await _resend(bot, user, text, markup)
        except TelegramForbiddenError:  # clicked in a group, but has no private chat with the bot
            with suppress(TelegramAPIError):
                await src.answer("Откройте личный чат с ботом и нажмите /start — экраны открываются там.",
                                 show_alert=True)
            return
        with suppress(TelegramAPIError):
            await src.answer()
        return
    await _render(bot, user, text, markup)


CAPTION_LIMIT = 1024
ANIMATION = (".mp4", ".gif")  # shown by Telegram as a silent looping animation (the mp4 must have no audio track)
_banner_id: str | None = None  # Telegram file_id after the first upload
_upload = asyncio.Lock()  # the first screens of a fresh process upload the file once, not once per user


@functools.cache  # config is fixed for the process: no filesystem check on every screen
def banner_path() -> Path | None:
    p = Path(config.banner_path)
    p = p if p.is_absolute() else Path(__file__).resolve().parent.parent / p
    return p if config.banner_path and p.is_file() else None


def visible_len(text: str) -> int:
    """Length as Telegram counts it for its limits: UTF-16 units of the text without tags."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)).encode("utf-16-le")) // 2


_banner_broken = False  # Telegram refused the banner in this process: screens go as text, no endless replacing


def _with_banner(text: str) -> bool:
    """Screens carry the banner (photo or silent animation); captions are limited to 1024 characters."""
    return banner_path() is not None and not _banner_broken and visible_len(text) <= CAPTION_LIMIT


async def _render(bot: Bot, user: User, text: str, markup: InlineKeyboardMarkup | None) -> None:
    """Edit the current screen in place. A screen with the banner is edited only as a caption: a text message (a
    notification, a long screen) cannot get the banner by an edit, so it is replaced by a new banner screen — the
    first screen after «Заявка одобрена → Открыть главное меню» already has the picture. A long screen that does not
    fit a caption replaces a banner screen the same way. Replaced screens are deleted: no stale buttons above."""
    if user.ui_msg_id:
        caption = lambda t: bot.edit_message_caption(  # noqa: E731
            chat_id=user.id, message_id=user.ui_msg_id, caption=t, reply_markup=markup)
        plain = lambda t: bot.edit_message_text(  # noqa: E731
            text=t, chat_id=user.id, message_id=user.ui_msg_id, reply_markup=markup, disable_web_page_preview=True)
        for edit in ((caption,) if _with_banner(text) else (plain, caption)):
            try:
                await safe_text(edit, text)
                return
            except TelegramBadRequest as e:
                if "not modified" in str(e):
                    return
                # wrong kind of message, caption too long, deleted or too old: try the next way
        with suppress(TelegramAPIError):
            await bot.delete_message(user.id, user.ui_msg_id)
    await _resend(bot, user, text, markup)


async def _send_banner(bot: Bot, user: User, text: str, markup: InlineKeyboardMarkup | None) -> Message:
    return await send_banner(bot, user.id, text, markup)


async def send_banner(bot: Bot, chat: int, text: str, markup: InlineKeyboardMarkup | None,
                      silent: bool = True) -> Message:
    """The banner (silent animation or picture) with `text` as its caption; uploaded once, then sent by file_id.
    The caller holds _upload while the banner is not uploaded yet."""
    global _banner_id
    path = banner_path()
    animated = path.suffix.lower() in ANIMATION
    send = bot.send_animation if animated else bot.send_photo
    m = await safe_text(lambda t: send(chat, _banner_id or FSInputFile(path), caption=t, reply_markup=markup,
                                       disable_notification=silent), text)
    if not _banner_id:
        media = m.animation or m.video or m.document if animated else (m.photo[-1] if m.photo else None)
        _banner_id = media.file_id if media else None
    return m


async def _resend(bot: Bot, user: User, text: str, markup: InlineKeyboardMarkup | None) -> None:
    """A new screen at the bottom. Screens answer the user's own action, so they arrive without a sound;
    notifications (new deal, receipt, payout) keep theirs."""
    global _banner_id
    m = None
    if _with_banner(text):
        try:
            if _banner_id:
                m = await _send_banner(bot, user, text, markup)
            else:
                async with _upload:
                    m = await _send_banner(bot, user, text, markup)
        except TelegramBadRequest as e:  # banner rejected: the screen itself must still be shown
            global _banner_broken
            log.warning("banner not sent: %s", e)
            _banner_id, _banner_broken = None, True
    if m is None:
        m = await safe_text(lambda t: bot.send_message(user.id, t, reply_markup=markup, disable_notification=True,
                                                       disable_web_page_preview=True), text)
    user.ui_msg_id = m.message_id  # the previous screen stays in the chat as history


def close_kb(extra=None) -> InlineKeyboardMarkup:
    return kb(extra, back("x", "Скрыть", "cross"))


async def notify(bot: Bot, uid: int, text: str, markup: InlineKeyboardMarkup | None = None,
                 silent: bool = False) -> Message | None:
    """Separate message (notification). Returns None if it could not be delivered (blocked bot etc.) or there is
    nobody to deliver to."""
    if not uid:
        return None
    try:
        return await safe_text(lambda t: bot.send_message(
            uid, t, reply_markup=markup or close_kb(), disable_notification=silent, disable_web_page_preview=True),
            clean(text))
    except TelegramAPIError:
        return None
