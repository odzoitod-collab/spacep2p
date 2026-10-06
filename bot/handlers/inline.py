"""Inline search: «Мои сделки» and «История операций» open the search right in the chat input
(switch_inline_query_current_chat). Picking a card sends /deal N or /op N via the bot: the middleware deletes that
message and the current screen is edited into the details (commands.cmd_deal / cmd_op)."""
from aiogram import Bot, Router
from aiogram.types import InlineQuery, InlineQueryResultArticle, InlineQueryResultsButton, InputTextMessageContent
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.deal import STATUS, status_of
from bot.handlers.wallet import KINDS, ref_label, signed
from bot.models import Deal, Ledger, User
from bot.services import deals, money
from bot.ui import MSK

router = Router()
PAGE = 20
DEALS = ("сделки", "сделка", "deals")
OPS = ("операции", "история", "ops")


def _page(q: InlineQuery) -> int:
    return int(q.offset) if q.offset.isdigit() else 0


def _article(rid: str, title: str, description: str, command: str) -> InlineQueryResultArticle:
    return InlineQueryResultArticle(id=rid, title=title[:120], description=description[:250],
                                    input_message_content=InputTextMessageContent(message_text=command))


async def _deals(s: AsyncSession, user: User, words: list[str], offset: int) -> list:
    q = select(Deal).where(or_((Deal.buyer_id == user.id) & deals.personal(), Deal.seller_id == user.id))
    for w in words:  # "#15", "15", "5000", "спор", "продажа"…
        w = w.lstrip("#")
        if w.isdigit():
            q = q.where(or_(Deal.id == int(w), Deal.amount_rub == int(w)))
        elif w.startswith("прод"):
            q = q.where(Deal.seller_id == user.id)
        elif w.startswith("пок"):
            q = q.where(Deal.buyer_id == user.id)
        else:
            codes = [k for k, (_, label) in STATUS.items() if label.lower().startswith(w)]
            q = q.where(Deal.status.in_(codes or ["-"]))
    rows = (await s.scalars(q.order_by(Deal.status.in_(deals.OPEN).desc(), Deal.id.desc())
                            .offset(offset).limit(PAGE))).all()
    out = []
    for d in rows:
        buy = d.buyer_id == user.id
        out.append(_article(
            f"d{d.id}", f"{'↓' if buy else '↑'} #{d.id} · {money.fmt(d.amount_rub)} ₽ · {status_of(d, user.id)[1]}",
            f"{'Покупка' if buy else 'Продажа'} · {money.usdt(d.buyer_credit if buy else d.seller_debit)} USDT · "
            f"{deals.aware(d.created_at).astimezone(MSK):%d.%m %H:%M}" + (" · ордер" if d.is_order else ""),
            f"/deal {d.id}"))
    return out


async def _ops(s: AsyncSession, user: User, words: list[str], offset: int) -> list:
    q = select(Ledger).where(Ledger.user_id == user.id)
    for w in words:
        kinds = [k for k, label in KINDS.items() if label.lower().startswith(w)]
        q = q.where(Ledger.kind.in_(kinds or ["-"]))
    rows = (await s.scalars(q.order_by(Ledger.id.desc()).offset(offset).limit(PAGE))).all()
    return [_article(f"o{r.id}", f"{KINDS.get(r.kind, r.kind)} {signed(r.delta - r.frozen_delta or r.frozen_delta)} USDT",
                     f"{deals.aware(r.created_at).astimezone(MSK):%d.%m.%Y %H:%M} {ref_label(r.ref)}".strip(),
                     f"/op {r.id}") for r in rows]


@router.inline_query()
async def on_inline(q: InlineQuery, bot: Bot, s: AsyncSession, user: User):
    words = q.query.lower().split()
    kind, words = (words[0], words[1:]) if words and words[0] in DEALS + OPS else ("сделки", words)
    offset = _page(q)
    results = await (_ops(s, user, words, offset) if kind in OPS else _deals(s, user, words, offset))
    empty = not results and not offset
    await q.answer(results, cache_time=0, is_personal=True,
                   next_offset=str(offset + PAGE) if len(results) == PAGE else "",
                   button=InlineQueryResultsButton(
                       text="Ничего не найдено — открыть бота" if empty else
                       ("Поиск: сумма, тип операции" if kind in OPS else "Поиск: #номер, сумма, статус"),
                       start_parameter="menu"))

