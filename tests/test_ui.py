"""Every screen of every role fits Telegram limits, is valid HTML and works without premium emoji."""
import re

from bot.config import config
from tests.harness import cb, msg
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
