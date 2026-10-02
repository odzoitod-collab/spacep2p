"""Single-message UI: every screen edits the user's main bot message.

Screen layout used everywhere:
    <icon> <b>Title</b>
    status line (where the user is / what happened)
    <blockquote>facts: amounts, fees, deadlines</blockquote>
    next step (what to do now)
    note (result of the last action: ok / warning)
"""
import asyncio
import functools
import html
import logging
import re
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardMarkup, Message

from bot.config import config
from bot.emoji import E, back, kb, pe
from bot.models import User

esc = html.escape
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
    return f"https://t.me/{(await bot.me()).username}?start={payload}"


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


def quote(*lines: str) -> str:
    return "<blockquote>" + "\n".join(line for line in lines if line) + "</blockquote>"


def title(emoji: str, text: str) -> str:
    return f"{emoji} <b>{text}</b>"


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


async def show(bot: Bot, user: User, text: str, markup: InlineKeyboardMarkup | None = None,
               src: CallbackQuery | Message | None = None) -> None:
    """Show a screen. A button press edits the very message it was pressed in (screen or notification) — that
    message becomes the current screen; older messages stay as history. Without a button press (a command, an
    answer to a question) the current screen is edited or, if there is none, a new one is sent."""
    text = clean(text)
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


def _with_banner(text: str) -> bool:
    """Screens carry the banner (photo or silent animation); captions are limited to 1024 characters."""
    return banner_path() is not None and len(html.unescape(re.sub(r"<[^>]+>", "", text))) <= CAPTION_LIMIT


async def _render(bot: Bot, user: User, text: str, markup: InlineKeyboardMarkup | None) -> None:
    """Edit the current screen in place. A screen with the banner has a caption, a notification has text: try the
    fitting edit first, then the other one; only if the message cannot be edited at all, send a new screen."""
    if user.ui_msg_id:
        caption = lambda t: bot.edit_message_caption(  # noqa: E731
            chat_id=user.id, message_id=user.ui_msg_id, caption=t, reply_markup=markup)
        plain = lambda t: bot.edit_message_text(  # noqa: E731
            text=t, chat_id=user.id, message_id=user.ui_msg_id, reply_markup=markup, disable_web_page_preview=True)
        for edit in ((caption, plain) if _with_banner(text) else (plain, caption)):
            try:
                await safe_text(edit, text)
                return
            except TelegramBadRequest as e:
                if "not modified" in str(e):
                    return
                # wrong kind of message, caption too long, deleted or too old: try the next way
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
            log.warning("banner not sent: %s", e)
            _banner_id = None
    if m is None:
        m = await safe_text(lambda t: bot.send_message(user.id, t, reply_markup=markup, disable_notification=True,
                                                       disable_web_page_preview=True), text)
    user.ui_msg_id = m.message_id  # the previous screen stays in the chat as history


def close_kb(extra=None) -> InlineKeyboardMarkup:
    return kb(extra, back("x", "Скрыть", "cross"))


async def notify(bot: Bot, uid: int, text: str, markup: InlineKeyboardMarkup | None = None,
                 silent: bool = False) -> Message | None:
    """Separate message (notification). Returns None if it could not be delivered (blocked bot etc.)."""
    try:
        return await safe_text(lambda t: bot.send_message(
            uid, t, reply_markup=markup or close_kb(), disable_notification=silent, disable_web_page_preview=True),
            clean(text))
    except TelegramAPIError:
        return None
