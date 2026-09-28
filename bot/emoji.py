"""Premium emoji (https://github.com/Zulut30/premium-telegram-emoji) and keyboard helpers."""
from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup

from bot.config import config

E: dict[str, tuple[str, str]] = {
    "wallet": ("5769403330761593044", "👛"),
    "card": ("5927169041595634481", "💳"),
    "sbp": ("5206636501860893075", "💳"),
    "dollar": ("5409048419211682843", "💵"),
    "ruble": ("5231449120635370684", "💰"),  # fallback must be a real emoji, not ₽
    "percent": ("5229064374403998351", "💯"),  # not %: Telegram rejects the entity
    "ok": ("5206607081334906820", "✅"),
    "cross": ("5210952531676504517", "❌"),
    "ban": ("5260293700088511294", "🚫"),
    "lock": ("5296369303661067030", "🔒"),
    "settings": ("5341715473882955310", "⚙️"),
    "profile": ("5879770735999717115", "👤"),
    "people": ("5942877472163892475", "👥"),
    "stats": ("5231200819986047254", "📊"),
    "info": ("5323442290708985472", "ℹ️"),
    "support": ("5884510167986343350", "💬"),
    "back": ("5875082500023258804", "↩️"),
    "search": ("5874960879434338403", "🔍"),
    "refresh": ("5375338737028841420", "🔄"),
    "clock": ("5440621591387980068", "⏳"),
    "warn": ("5447644880824181073", "⚠️"),
    "clip": ("5305265301917549162", "📎"),
    "video": ("6005986106703613755", "🎬"),
    "swap": ("5874986954180791957", "📶"),
    "up": ("5449683594425410231", "📈"),
    "down": ("5447183459602669338", "📉"),
    "plus": ("5397916757333654639", "➕"),
    "trash": ("5879896690210639947", "🗑"),
    "pencil": ("5395444784611480792", "✏️"),
    "shop": ("5983399041197675256", "🏪"),
    "edu": ("5992157823838984339", "🎓"),
    "live": ("4927197721900614739", "🔴"),
    "flag": ("5460755126761312667", "🚩"),
    "filter": ("5875033614705495771", "🎛"),
    "list": ("5877597667231534929", "📋"),
    "bell": ("5458603043203327669", "🔔"),
    "next": ("5875506366050734240", "➡️"),
    "prev": ("5877536313623711363", "⬅️"),
    "fire": ("5424972470023104089", "🔥"),
    "pause": ("5359543311897998264", "⏸"),
    "doc": ("5877485980901971030", "📊"),
    "key": ("6005570495603282482", "🔑"),
    "bank": ("5924776903725551803", "🏦"),
    "star": ("5438496463044752972", "⭐"),
}


def pe(name: str) -> str:
    """Inline icon. Depends on EMOJI_MODE: premium custom emoji, plain emoji or nothing."""
    eid, fb = E[name]
    if config.emoji_mode == "premium":
        return f'<tg-emoji emoji-id="{eid}">{fb}</tg-emoji>'
    return fb if config.emoji_mode == "plain" else ""


def btn(text: str, cb: str | None = None, icon: str | None = None, url: str | None = None,
        style: str | None = None, copy: str | None = None, inline: str | None = None) -> InlineKeyboardButton:
    """style: 'success' | 'danger' | 'primary' (Bot API 9.4). copy: text copied to the clipboard on tap.
    inline: opens inline search in this chat with the query prefilled (e.g. "сделки ")."""
    if len(text) > 64:  # Telegram shows long buttons cut anyway; keep the start readable
        text = text[:63] + "…"
    return InlineKeyboardButton(
        text=text, callback_data=cb if url is None and copy is None and inline is None else None, url=url,
        copy_text=CopyTextButton(text=copy) if copy else None, switch_inline_query_current_chat=inline,
        # icons only on the highlighted (coloured) buttons: the main action of a screen stands out, the rest is text
        icon_custom_emoji_id=E[icon][0] if icon and style and config.emoji_mode == "premium" else None, style=style,
    )


class NavButton(InlineKeyboardButton):
    """Navigation (back / menu / cancel / hide): always alone in the last row."""


SMALL = 20  # a label up to this length fits half the width; longer ones keep the whole row


def _small(b: InlineKeyboardButton) -> bool:
    return b.style is None and len(b.text) <= SMALL


def kb(*rows: list[InlineKeyboardButton] | InlineKeyboardButton | None) -> InlineKeyboardMarkup:
    """One layout everywhere: the first row is the main action; after it short plain buttons flow two per row,
    long or coloured ones take the whole row; no row is wider than two; one navigation button closes the keyboard
    in a row of its own (if several are given, the first one wins)."""
    out, nav, pending = [], [], None
    for i, r in enumerate(x for x in rows if x is not None):
        r = [b for b in (r if isinstance(r, list) else [r]) if b is not None]
        nav += [b for b in r if isinstance(b, NavButton)]
        r = [b for b in r if not isinstance(b, NavButton)]
        if not r:
            continue
        if out and all(_small(b) for b in r):  # short plain buttons flow into pairs across rows
            for b in r:
                if pending is not None:  # a lone small button waits for its pair
                    out[pending].append(b)
                    pending = None
                else:
                    pending = len(out)
                    out.append([b])
            continue
        pending = None
        out += [r[j:j + 2] for j in range(0, len(r), 2)]
    if nav:
        out.append(nav[:1])
    return InlineKeyboardMarkup(inline_keyboard=out)


def back(cb: str = "menu", text: str = "Назад", icon: str = "back") -> InlineKeyboardButton:
    if len(text) > 64:
        text = text[:63] + "…"
    return NavButton(text=text, callback_data=cb)  # plain text: navigation is recognised by its place
