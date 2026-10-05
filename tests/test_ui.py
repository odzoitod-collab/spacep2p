"""Every screen of every role fits Telegram limits, is valid HTML and works without premium emoji."""
import re

from bot.config import config
from tests.harness import plain, cb, msg
from tests.test_scenarios import ADMIN, BUYER, PDF, SELLER, create_deal, ready

EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿⬀-⯿️]")


async def tour(b):
    await ready(b)
    d = await create_deal(b)
    await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, document=PDF))
    for data in ("menu", "buy:0", "tm", "op", "om", "deals", "w", "w:h", "w:dep", "w:wd", "info", "sup",
                 f"dl:{d.id}"):
        await b.run(cb(BUYER, data))
    for data in ("menu", "sl", "cd:1", "ce:min:1", "ce:daily:1", "ce:bank:1", "cd:del:1", f"dl:{d.id}", f"dl:ok:{d.id}", f"dl:ds:{d.id}",
                 f"dl:dr:{d.id}:not_received", "sl:add", "sl:add:sbp"):
        await b.run(cb(SELLER, data))
    for data in ("a", "as", "as:tutorial", "au", f"auv:{SELLER}", f"auh:{SELLER}", f"aud:{SELLER}", f"auc:{SELLER}",
                 f"aub:{SELLER}:1", f"aum:{SELLER}:+", f"amsg:{SELLER}", "ac:0", "acv:1", "ad", "adl:dispute",
                 "adl:slow", "adl:open", "adl:all", f"adv:{d.id}", f"ar:{d.id}:b", f"ar:{d.id}:s", "al", "aa"):
        await b.run(cb(ADMIN, data))


def test_all_screens_within_limits(go):
    go(tour)  # the `go` fixture validates HTML, text/caption length, callback_data and button sizes


def test_text_only_mode_has_no_emoji(go):
    async def fn(b):
        config.emoji_mode = "none"
        try:
            await tour(b)
        finally:
            config.emoji_mode = "premium"
        for t in b.session.texts():
            assert "tg-emoji" not in t
            assert not EMOJI.search(t.replace("⇄", "")), t[:200]
            assert not re.search(r"(?m)^ ", t), t[:200]
        for m in b.session.calls:
            for row in getattr(getattr(m, "reply_markup", None), "inline_keyboard", []) or []:
                assert all(btn.icon_custom_emoji_id is None for btn in row)
    go(fn)


def test_card_style_helpers():
    from bot.ui import cf, clean, quote, section
    assert quote("• Баланс: <b>5 USDT</b>", "• просто строка") == \
        "<blockquote><b>Баланс:</b> <b>5 USDT</b>\nпросто строка</blockquote>"  # no list mixes both looks
    assert quote("• один", "• два") == "<blockquote>• один\n• два</blockquote>"  # a plain list keeps its bullets
    assert quote("• 14:05 · событие") == "<blockquote>• 14:05 · событие</blockquote>"  # a time is not a label
    assert cf("Сумма", "10 ₽") == "<blockquote><b>Сумма:</b></blockquote>\n<b><code> ╰</code></b>  10 ₽"
    assert cf("Сумма", "a", "", "b").split("\n")[1:] == ["<b><code>├</code></b>  a", "<b><code>╰</code></b>  b"]
    assert cf("Пусто", "", "") == ""
    text = clean("\n".join(["T", cf("Кто", "x", icon="profile"), section("bell", "Раздел"), quote("• x: y")]))
    assert text.count("<tg-emoji") == 2  # card labels and section headers keep their icons


def long_texts(b):
    from bot.ui import CAPTION_LIMIT, visible_len
    return [(m.chat_id, visible_len(t), t) for m in b.session.calls
            if (t := getattr(m, "text", None) or getattr(m, "caption", None)) and visible_len(t) > CAPTION_LIMIT
            and type(m).__name__ in ("SendAnimation", "EditMessageCaption", "SendMessage", "EditMessageText")]


def test_every_screen_fits_under_the_banner(go):
    """A screen longer than a caption cannot keep the banner and is re-sent as text: every screen of the tour fits."""
    from bot.ui import CAPTION_LIMIT, visible_len

    async def fn(b):
        await tour(b)
        await b.run(cb(BUYER, "info:g"), cb(ADMIN, "acm"), cb(ADMIN, "aadm"), cb(ADMIN, "atml"))
        long = [(m.chat_id, visible_len(t)) for m in b.session.calls
                if type(m).__name__ in ("SendAnimation", "EditMessageCaption", "SendMessage", "EditMessageText")
                and (t := getattr(m, "text", None) or getattr(m, "caption", None)) and m.chat_id in (BUYER, SELLER, ADMIN)
                and visible_len(t) > CAPTION_LIMIT]
        assert not long, [(c, n, plain(t)[:300]) for c, n, t in long_texts(b)] if long else long
    go(fn)
