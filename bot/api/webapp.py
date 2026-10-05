"""Strait Pay mini app (Telegram Web App): the page at /app and its JSON API at /app/api/*.

Sign-in is Telegram's own: every request carries the app's initData (header X-Telegram-Init-Data), signed with the
bot token — no passwords, no cookies. The user is the bot's user with the same ban, entry and community rules, and
every action goes through the same functions as the bot's buttons (wallet, deals, cards, chat): the money rules are
one for both. The page itself is static (bot/webapp/), it reads everything from this API."""
import hashlib
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile
from aiogram.utils.web_app import safe_parse_webapp_init_data
from aiohttp import web
from sqlalchemy import exists, func, or_, select

from bot.config import config
from bot.models import Card, Deal, DealMessage, Deposit, Ledger, Operator, OrderMerchant, Session, User, Withdrawal, now
from bot.services import admins, api, deals, events, money, operators, settings, teams, xrocket

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent / "webapp"
FILES = {"app.css": "text/css", "app.js": "application/javascript"}
VERSION = hashlib.sha256(b"".join((ROOT / n).read_bytes() for n in FILES if (ROOT / n).is_file())).hexdigest()[:10]
SESSION_HOURS = 24  # initData older than this is refused: the app is reopened from Telegram
BOT = web.AppKey("bot", Bot)
S = web.RequestKey("app_session", object)
USER = web.RequestKey("app_user", object)
limiter = api.RateLimiter()
CSP = ("default-src 'self'; script-src 'self' https://telegram.org; style-src 'self' 'unsafe-inline' "
       "https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src 'self' data: blob: https://t.me "
       "https://*.telegram.org https://*.t.me; connect-src 'self'")


class AppError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def fail(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"error": {"code": code, "message": message}}, status=status)


def user_of(init_data: str) -> int:
    """The Telegram user id of a valid, fresh initData; AppError otherwise."""
    try:
        data = safe_parse_webapp_init_data(config.bot_token, init_data)
    except ValueError:
        raise AppError(401, "unauthorized", "Откройте приложение из Telegram")
    if data.user is None:
        raise AppError(401, "unauthorized", "Откройте приложение из Telegram")
    if now() - deals.aware(data.auth_date) > timedelta(hours=SESSION_HOURS):
        raise AppError(401, "expired", "Сессия устарела — закройте и откройте приложение заново")
    return data.user.id


async def admitted(s, user: User | None) -> None:
    """The bot's own gates: registered, not banned, let in, in the community (admins and operators pass)."""
    from bot.handlers.community import missing
    from bot.middlewares import let_in
    if user is None:
        raise AppError(403, "start_bot", "Сначала откройте бота и нажмите «Запустить»")
    admin = admins.is_admin(user.id)
    if user.is_banned and not admin:
        raise AppError(403, "banned", "Аккаунт заблокирован. Напишите в поддержку")
    if not let_in(user, user.id):
        raise AppError(403, "signup", "Заявка на вход ещё не одобрена — дождитесь ответа в боте")
    if not admin and missing(user) and not await operators.is_operator(s, user.id):
        raise AppError(403, "join", "Вступите в чат и канал Strait Pay в боте — и приложение откроется")


@web.middleware
async def guard(request: web.Request, handler):
    """/app/api/*: Telegram sign-in, the bot's gates, a rate limit, one DB session committed after the handler."""
    if not request.path.startswith("/app/api/"):
        return await handler(request)
    try:
        uid = user_of(request.headers.get("X-Telegram-Init-Data", ""))
        if not limiter.allow(f"app{uid}", 15):
            raise AppError(429, "rate_limited", "Слишком часто — подождите секунду")
        async with Session() as s:
            user = await s.get(User, uid)
            await admitted(s, user)
            request[S], request[USER] = s, user
            if user.last_seen is None or now() - deals.aware(user.last_seen) >= timedelta(minutes=1):
                user.last_seen = now()
            response = await handler(request)
            await s.commit()
        events.kick()
        return response
    except AppError as e:
        return fail(e.status, e.code, e.message)
    except web.HTTPException:
        raise
    except Exception:  # noqa: BLE001 - never leak internals
        log.exception("app %s %s failed", request.method, request.path)
        return fail(500, "internal", "Что-то пошло не так. Попробуйте ещё раз")


def ctx(request: web.Request):
    return request[S], request[USER], request.app[BOT]


def num(v) -> str | None:
    """A Decimal as a plain string without trailing zeros ("1500", "97.0225"): the page formats it."""
    return None if v is None else format(Decimal(v).normalize(), "f")


def iso(dt: datetime | None) -> str | None:
    return deals.aware(dt).isoformat() if dt else None


async def body(request: web.Request) -> dict:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        raise AppError(400, "bad_json", "Неверный запрос")
    if not isinstance(data, dict):
        raise AppError(400, "bad_json", "Неверный запрос")
    return data


def parse_amount(raw, places: int = 2) -> Decimal | None:
    try:
        v = Decimal(str(raw).replace(" ", "").replace(",", "."))
    except Exception:  # noqa: BLE001
        return None
    if not v.is_finite() or not 0 < v < Decimal("100000000") or v.as_tuple().exponent < -places:
        return None
    return v


async def bot_name(bot: Bot) -> str:
    from bot import ui
    if not ui.BOT:
        try:
            ui.BOT = (await bot.me()).username or ""
        except TelegramAPIError:
            return ""
    return ui.BOT


# ---------- the page ----------

async def page(request: web.Request) -> web.Response:
    html = (ROOT / "index.html").read_text(encoding="utf-8").replace("{{v}}", VERSION)
    return web.Response(text=html, content_type="text/html", charset="utf-8",
                        headers={"Cache-Control": "no-cache", "Content-Security-Policy": CSP,
                                 "Referrer-Policy": "no-referrer"})


async def asset(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    if name not in FILES or not (ROOT / name).is_file():
        raise web.HTTPNotFound()
    return web.Response(body=(ROOT / name).read_bytes(), content_type=FILES[name],
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


# ---------- the account ----------

async def roles(s, user: User) -> dict:
    om = await s.get(OrderMerchant, user.id)
    team = await teams.of_user(s, user)
    has_cards = await s.scalar(select(exists().where(Card.user_id == user.id, ~Card.is_deleted)))
    return {
        "admin": admins.is_admin(user.id),
        "operator": await operators.is_operator(s, user.id),
        "merchant": om.status if om else None,
        "seller": bool(has_cards),
        "team": {"name": team.name, "leader": team.leader_id == user.id} if team else None,
    }


async def me(request: web.Request) -> web.Response:
    from bot.handlers.start import manager_url
    s, user, bot = ctx(request)
    rate, pct = settings.buyer_terms(user)
    active = (await s.scalars(select(Deal).where(mine(user.id), Deal.status.in_(deals.OPEN)))).all()
    op = await s.get(Operator, user.id)
    name = await bot_name(bot)
    return web.json_response({
        "user": {"id": user.id, "name": user.name, "username": user.username, "since": iso(user.created_at),
                 "quiet": user.quiet, "online": user.is_online},
        "roles": await roles(s, user),
        "balance": {"available": num(user.balance), "frozen": num(user.frozen),
                    "withdrawable": num(money.withdrawable(user)), "team": num(user.team_balance),
                    "debt": num(op.debt) if op and op.debt else None},
        "rate": {"rate": num(rate), "pct": num(pct), "own": settings.has_terms(user),
                 "example_rub": "10000", "example_usdt": num(deals.buyer_preview(Decimal(10000), user).buyer_credit)},
        "counts": {"open": len(active), "action": sum(deal_json(d, None, user.id, full=False)["action"] for d in active)},
        "links": {"manager": manager_url(), "bot": f"https://t.me/{name}" if name else None,
                  "channel": settings.raw("channel_link") if settings.get("channel_id") else None,
                  "chat": bool(settings.get("chat_id")), "docs": settings.get("docs_url") or None,
                  "manager_nick": settings.get("manager") or settings.get("support") or None},
    })


async def chat_invite(request: web.Request) -> web.Response:
    """A personal one-time link into the community chat — the same as the bot's «Чат» button."""
    from bot.handlers.community import channel_link, personal_invite
    s, user, bot = ctx(request)
    link, problem = await personal_invite(bot, s, user)
    if link is None:
        raise AppError(409, "no_chat", problem)
    return web.json_response({"chat": link, "channel": await channel_link(bot, s)})


async def save_settings(request: web.Request) -> web.Response:
    s, user, _ = ctx(request)
    data = await body(request)
    if isinstance(data.get("quiet"), bool):
        user.quiet = data["quiet"]
    return web.json_response({"quiet": user.quiet})


async def stats(request: web.Request) -> web.Response:
    s, user, _ = ctx(request)
    periods = []
    for key, since in (("today", deals.day_start()), ("week", now() - timedelta(days=7)),
                       ("month", now() - timedelta(days=30)), ("all", None)):
        st = await deals.seller_stats(s, user.id, since)
        periods.append({"key": key, "n": st["n"], "rub": num(st["rub"]), "income": num(st["income"].quantize(money.Q)),
                        "avg": num(st["avg"].quantize(Decimal("0.01"))), "success": st["success"],
                        "confirm_min": st["confirm_min"], "disputes": st["disputes"]})
    days = await deals.income_by_day(s, user.id, 14)
    bought = (await s.execute(select(func.count(Deal.id), func.coalesce(func.sum(Deal.amount_rub), 0),
                                     func.coalesce(func.sum(Deal.buyer_credit), 0)).where(
        Deal.buyer_id == user.id, Deal.status == "completed", Deal.api_client_id.is_(None)))).one()
    return web.json_response({
        "seller": periods,
        "days": [{"day": d.isoformat(), "n": n, "income": num(inc.quantize(money.Q))} for d, n, inc in reversed(days)],
        "buyer": {"n": bought[0], "rub": num(Decimal(bought[1])), "usdt": num(Decimal(bought[2]))},
    })


# ---------- deals ----------

def role_of(d: Deal, uid: int) -> str:
    if uid == d.buyer_id:
        return "buyer"
    if d.via_bybit and (uid == d.operator_id or d.status == "checking" and d.operator_id is None
                        and uid not in (d.buyer_id, d.seller_id)):
        return "operator"
    return "seller" if uid == d.seller_id else "admin"


def steps(d: Deal) -> list[dict]:
    """The way of a deal: done / now / next, with what the current step is waiting for."""
    from bot.handlers.orders import _done, _steps
    if d.is_order:
        way = _steps(d)
        done = _done(d)
    else:
        way = [("Сделка создана", ""), ("Покупатель оплатил", "ждём перевод и чек"),
               ("Оплата подтверждена", "продавец проверяет поступление")]
        done = {"waiting_payment": 1, "paid": 2, "dispute": 2, "completed": 3}.get(d.status, 1)
    closed = d.status not in deals.OPEN
    return [{"title": t, "state": "done" if i < done else "now" if i == done and not closed else "next",
             "hint": hint if i == done and not closed else ""} for i, (t, hint) in enumerate(way)]


def actions(d: Deal, uid: int) -> list[str]:
    from bot.handlers.relay import chat_people
    out, buyer = [], uid == d.buyer_id
    if buyer and d.status in ("searching", "assigned", "checking"):
        out.append("cancel_request")
    if buyer and d.status == "waiting_payment":
        out += ["receipt", "cancel"]
    if buyer and d.status == "expired" and (until := deals.late_deadline(d)) and now() < until:
        out.append("late_receipt")
    if not buyer and d.status == "paid" and deals.checker(d) == uid:
        out += ["confirm", "dispute_bot"]
    if buyer and d.status == "paid" and (at := deals.buyer_dispute_at(d)) and now() >= at:
        out.append("dispute_bot")
    if d.status == "assigned" and uid == d.seller_id and not d.via_bybit:
        out.append("give_bot")
    if d.status == "assigned" and uid == d.seller_id and d.via_bybit:
        out.append("link_bot")
    if d.via_bybit and d.status == "checking" and d.operator_id is None and uid not in (d.buyer_id, d.seller_id):
        out.append("accept")  # shown only to operators: the endpoint checks it
    if d.via_bybit and d.operator_id == uid and d.status == "checking":
        out += ["give"] + (["pass_on", "no_requisites"] if d.bybit_url and d.seller_id else [])
    if deals.held(d) and d.operator_id == uid:
        out += (["recreate"] if d.bybit_url and d.seller_id else []) + ["close"]
    if uid in chat_people(d) or admins.is_admin(uid):
        out.append("chat")
    return out


def deal_json(d: Deal, card: Card | None, uid: int, full: bool = True) -> dict:
    from bot.handlers.deal import CLOSE_REASONS, status_of
    role = role_of(d, uid)
    buyer = role == "buyer"
    action = (buyer and d.status == "waiting_payment" or d.status == "paid" and deals.checker(d) == uid
              or d.status == "assigned" and uid == d.seller_id)
    out = {
        "id": d.id, "role": role, "status": d.status, "status_text": status_of(d, uid)[1],
        "kind": "order" if d.is_order else "card", "bybit": d.via_bybit,
        "amount_rub": num(d.amount_rub), "usdt": num(d.buyer_credit if buyer or role == "admin" else d.seller_debit),
        "created_at": iso(d.created_at), "action": bool(action),
    }
    if not full:
        return out
    show_req = card is not None and (not buyer or d.status in ("waiting_payment", "paid", "dispute"))
    out.update({
        "rate": num(d.buyer_rate or d.rate if buyer else d.merchant_rate or d.rate),
        "expires_at": iso(d.expires_at) if d.status in deals.UNPAID and not deals.held(d) else None,
        "held": deals.held(d),
        "paid_at": iso(d.paid_at), "closed_at": iso(d.closed_at),
        "close_reason": CLOSE_REASONS.get(d.close_reason or "", None),
        "sender_bank": d.sender_bank,
        "income": num((d.amount_rub / d.rate - d.seller_debit).quantize(money.Q))
        if role == "seller" and not d.is_order else None,
        "requisites": {"bank": card.bank, "type": card.kind, "number": card.requisites, "holder": card.holder or None}
        if show_req else None,
        "receipt": bool(d.receipt_file_id),
        "bybit_url": d.bybit_url if role in ("operator", "admin") or (d.via_bybit and d.status == "checking"
                                                                      and d.operator_id is None) else None,
        "steps": steps(d), "actions": actions(d, uid),
        "dispute_at": iso(deals.buyer_dispute_at(d)) if buyer and d.status == "paid" else None,
    })
    return out


def mine(uid: int):
    """Deals this user takes part in (an API order is the service's own: not listed for its owner)."""
    return or_(Deal.seller_id == uid, Deal.operator_id == uid,
               (Deal.buyer_id == uid) & Deal.api_client_id.is_(None))


async def deal_list(request: web.Request) -> web.Response:
    s, user, _ = ctx(request)
    scope = request.query.get("scope", "active")
    q = select(Deal).where(mine(user.id))
    if scope == "active":
        q = q.where(Deal.status.in_(deals.OPEN))
    else:
        q = q.where(Deal.status.not_in(deals.OPEN))
    if (before := request.query.get("before", "")).isdigit():
        q = q.where(Deal.id < int(before))
    rows = (await s.scalars(q.order_by(Deal.id.desc()).limit(30))).all()
    return web.json_response({"deals": [deal_json(d, None, user.id, full=False) for d in rows]})


async def deal_of(request: web.Request) -> Deal:
    s, user, _ = ctx(request)
    try:
        did = int(request.match_info["id"])
    except ValueError:
        raise AppError(404, "not_found", "Сделка не найдена")
    d = await s.get(Deal, did, populate_existing=True)
    free = (d is not None and d.via_bybit and d.status == "checking" and d.operator_id is None
            and await operators.is_operator(s, user.id))  # an order waiting for an operator: any operator sees it
    if d is None or (user.id not in (d.buyer_id, d.seller_id, d.operator_id) and not admins.is_admin(user.id)
                     and not free) \
            or (d.api_client_id and user.id == d.buyer_id and user.id not in (d.seller_id, d.operator_id)):
        raise AppError(404, "not_found", "Сделка не найдена")
    return d


async def deal_response(s, d: Deal, uid: int) -> web.Response:
    card = await s.get(Card, d.card_id) if d.card_id else None
    return web.json_response({"deal": deal_json(d, card, uid)})


async def deal_get(request: web.Request) -> web.Response:
    s, user, _ = ctx(request)
    return await deal_response(s, await deal_of(request), user.id)


async def deal_cancel(request: web.Request) -> web.Response:
    from bot.handlers.deal import after_buyer_cancel, buyer_cancel
    from bot.handlers.orders import cancel_request, request_cancelled
    s, user, bot = ctx(request)
    d = await deal_of(request)
    if d.buyer_id != user.id:
        raise AppError(403, "forbidden", "Отменить может только покупатель")
    if d.status in ("searching", "assigned", "checking"):
        res = await cancel_request(s, d.id)
        if res is None:
            raise AppError(409, "too_late", "Реквизиты уже выданы — обновите сделку")
        await request_cancelled(bot, s, res)
    else:
        res = await buyer_cancel(s, user, d)
        if res is None:
            raise AppError(409, "too_late", "Сделку уже нельзя отменить")
        await after_buyer_cancel(bot, s, res)
    return await deal_response(s, res, user.id)


async def deal_confirm(request: web.Request) -> web.Response:
    from bot.handlers.deal import after_confirm, seller_confirm
    s, user, bot = ctx(request)
    d = await deal_of(request)
    if deals.checker(d) != user.id or d.status != "paid":
        raise AppError(409, "not_allowed", "Подтвердить нельзя: статус сделки изменился")
    res = await seller_confirm(s, user, d)
    if res is None:
        raise AppError(409, "not_allowed", "Сделка уже изменена")
    await after_confirm(bot, s, res)
    return await deal_response(s, res, user.id)


def receipt_kind(data: bytes) -> str | None:
    if data[:1024].find(b"%PDF-") >= 0:
        return "pdf"
    if settings.get("receipt_images") == "1" and (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"):
        return "image"
    return None


async def deal_receipt(request: web.Request) -> web.Response:
    """The buyer's PDF from the app: Telegram gives it a file id through a copy in his own chat with the bot, then
    the receipt goes exactly the way it goes from the bot."""
    from bot.handlers.deal import MAX_FILE, accept_receipt, send_to_seller
    s, user, bot = ctx(request)
    d = await deal_of(request)
    if d.buyer_id != user.id or d.status not in ("waiting_payment", "expired"):
        raise AppError(409, "not_allowed", "Чек сейчас не принимается — обновите сделку")
    data, name = b"", "check.pdf"
    if request.content_type.startswith("multipart/"):
        async for part in await request.multipart():
            if part.name == "file":
                name = (part.filename or name)[:80]
                data = await part.read(decode=False)
                break
    if not data:
        raise AppError(422, "file_required", "Выберите файл чека")
    if len(data) > MAX_FILE:
        raise AppError(413, "too_large", "Файл больше 20 МБ")
    kind = receipt_kind(data)
    if kind is None:
        raise AppError(422, "not_pdf", "Нужен PDF-чек из приложения банка"
                       + (" или его фото" if settings.get("receipt_images") == "1" else ""))
    caption = f"Чек по сделке #{d.id} — отправлен из приложения"
    try:
        if kind == "pdf":
            m = await bot.send_document(user.id, BufferedInputFile(data, name if name.lower().endswith(".pdf")
                                                                  else "check.pdf"), caption=caption)
            fid = m.document.file_id
        else:
            m = await bot.send_photo(user.id, BufferedInputFile(data, "check.jpg"), caption=caption)
            fid = "photo:" + m.photo[-1].file_id
    except TelegramAPIError:
        raise AppError(409, "chat_closed", "Откройте чат с ботом и нажмите «Запустить» — чек хранится через него")
    paid, late, reused, err = await accept_receipt(s, d, user, fid, "sha256:" + hashlib.sha256(data).hexdigest()[:57])
    if not paid:
        raise AppError(409, "rejected", err)
    await send_to_seller(bot, s, paid, late, reused)
    return await deal_response(s, paid, user.id)


async def chat_get(request: web.Request) -> web.Response:
    from bot.handlers.relay import chat_people, chat_role
    s, user, _ = ctx(request)
    d = await deal_of(request)
    if user.id not in chat_people(d) and not admins.is_admin(user.id):
        raise AppError(403, "forbidden", "Чат недоступен")
    q = select(DealMessage).where(DealMessage.deal_id == d.id)
    if (after := request.query.get("after", "")).isdigit():
        q = q.where(DealMessage.id > int(after))
    rows = list(reversed((await s.scalars(q.order_by(DealMessage.id.desc()).limit(80))).all()))
    return web.json_response({
        "members": list(dict.fromkeys(chat_role(d, uid) for uid in chat_people(d))) + ["Администрация"],
        "open": d.status in deals.OPEN or d.status == "expired",
        "messages": [{"id": r.id, "role": r.role, "mine": r.sender_id == user.id, "text": r.text,
                      "at": iso(r.created_at)} for r in rows],
    })


async def chat_post(request: web.Request) -> web.Response:
    from bot.handlers.relay import chat_people, post
    s, user, bot = ctx(request)
    d = await deal_of(request)
    if user.id not in chat_people(d) and not admins.is_admin(user.id):
        raise AppError(403, "forbidden", "Чат недоступен")
    text = (await body(request)).get("text")
    err = await post(bot, s, user, d, text.strip() if isinstance(text, str) else "")
    if err:
        raise AppError(422, "not_sent", err)
    return await chat_get(request)


# ---------- the operator ----------

async def operator_only(request: web.Request) -> None:
    s, user, _ = ctx(request)
    if not await operators.is_operator(s, user.id):
        raise AppError(403, "not_operator", "Это действие — для операторов")


async def operator_cabinet(request: web.Request) -> web.Response:
    s, user, _ = ctx(request)
    op = await s.get(Operator, user.id)
    if not await operators.is_operator(s, user.id) and not (op and op.debt):
        raise AppError(403, "not_operator", "Кабинет — для операторов")
    working = (await s.scalars(select(Deal).where(Deal.operator_id == user.id, Deal.via_bybit, Deal.status.in_(
        ("assigned", "checking", "waiting_payment", "paid", "dispute"))).order_by(Deal.id))).all()
    free = (await s.scalars(select(Deal).where(Deal.status == "checking", Deal.operator_id.is_(None), Deal.via_bybit,
                                               Deal.seller_id != user.id, Deal.buyer_id != user.id)
                            .order_by(Deal.id).limit(20))).all()
    return web.json_response({"debt": num(op.debt if op else 0),
                              "working": [deal_json(d, None, user.id, full=False) for d in working],
                              "free": [deal_json(d, None, user.id, full=False) for d in free]})


async def operator_act(request: web.Request) -> web.Response:
    """accept · requisites · recreate · close · pass_on · no_requisites — the bot's operator buttons."""
    from bot.handlers import orders as order_handlers
    from bot.services.orders import pay_choices
    await operator_only(request)
    s, user, bot = ctx(request)
    did, act = int(request.match_info["id"]), request.match_info["act"]
    try:
        if act == "accept":
            d = await order_handlers.accept_order(bot, s, user, did)
        elif act == "requisites":
            text = (await body(request)).get("text")
            got, err = order_handlers.parse_requisites(text if isinstance(text, str) else "")
            if not got:
                raise AppError(422, "bad_requisites", err)
            kind, number, bank, holder = got
            d = await order_handlers.give_now(bot, s, user, did, kind, bank, number, holder, pay_choices()[0])
        elif act == "recreate":
            d = await order_handlers.recreate_order(bot, s, user, did)
        elif act == "close":
            d = await order_handlers.close_order(bot, s, user, did)
        elif act in ("pass_on", "no_requisites"):
            d = await order_handlers.pass_on(bot, s, user, did, act == "no_requisites")
            await order_handlers._send_rating(bot, user, *d.rating, d)
        else:
            raise AppError(404, "not_found", "Нет такого действия")
    except deals.DealError as e:
        raise AppError(409, e.code or "deal_error", str(e))
    return await deal_response(s, d, user.id)


# ---------- buying ----------

async def buy_quote(request: web.Request) -> web.Response:
    from bot.handlers.market import order_range, pick_card
    s, user, _ = ctx(request)
    amount = parse_amount(request.query.get("amount"))
    if amount is None:
        raise AppError(422, "bad_amount", "Введите сумму в рублях")
    card = await pick_card(s, user, amount)
    lo, hi = order_range()
    if card is None and not lo <= amount <= hi:
        raise AppError(409, "no_offer", f"Сейчас нет реквизитов на {money.fmt(amount)} ₽. Под точную сумму — от "
                                        f"{money.fmt(lo)} до {money.fmt(hi)} ₽")
    rate, pct = settings.buyer_terms(user)
    q = deals.buyer_preview(amount, user)
    return web.json_response({
        "amount_rub": num(amount), "rate": num(rate), "pct": num(pct), "usdt": num(q.usdt),
        "fee": num(q.usdt - q.buyer_credit), "credit": num(q.buyer_credit),
        "mode": "card" if card else "request", "card_id": card.id if card else None,
        "bank": card.bank if card else None, "type": card.kind if card else None,
        "minutes": settings.num("deal_minutes") if card else settings.num("order_pay_minutes"),
        "search_minutes": None if card else settings.num("order_search_minutes"),
    })


async def buy(request: web.Request) -> web.Response:
    from bot.handlers.deal import on_deal_created
    from bot.handlers.market import open_card_deal
    from bot.handlers.orders import broadcast, open_request
    s, user, bot = ctx(request)
    data = await body(request)
    amount, credit = parse_amount(data.get("amount_rub")), parse_amount(data.get("credit"), 6)
    if amount is None or credit is None:
        raise AppError(422, "bad_amount", "Проверьте сумму")
    try:
        if isinstance(data.get("card_id"), int):
            d = await open_card_deal(s, user, data["card_id"], amount, credit)
            await on_deal_created(bot, s, d)
        else:
            d = await open_request(s, user, amount, None, credit)
            await broadcast(bot, s, d)
    except deals.DealError as e:
        raise AppError(409, e.code or "deal_error", str(e))
    return await deal_response(s, d, user.id)


# ---------- wallet ----------

def deposit_json(dep: Deposit) -> dict:
    return {"id": dep.id, "status": dep.status, "network": dep.network,
            "network_name": xrocket.net_name(dep.network) if dep.network else None, "address": dep.address,
            "link": dep.link, "amount": num(dep.amount) if dep.amount else None,
            "credit": num(dep.credit) if dep.credit else None, "expires_at": iso(dep.expires_at),
            "created_at": iso(dep.created_at)}


def withdrawal_json(wd: Withdrawal) -> dict:
    from bot.handlers.wallet import WD_STATUS
    return {"id": wd.id, "status": wd.status, "status_text": WD_STATUS.get(wd.status, wd.status),
            "method": wd.method, "network": xrocket.net_name(wd.network) if wd.network else None,
            "address": wd.address, "amount": num(wd.amount), "receive": num(wd.amount - wd.fee),
            "link": wd.link if wd.method == "xrocket" else None, "created_at": iso(wd.created_at)}


async def wallet(request: web.Request) -> web.Response:
    from bot.handlers.wallet import fee_pct, lock_note, withdraw_terms
    s, user, _ = ctx(request)
    nets = await xrocket.networks()
    pending = (await s.scalars(select(Deposit).where(Deposit.user_id == user.id, Deposit.status == "active",
                                                     Deposit.purpose == "deposit")
                               .order_by(Deposit.id.desc()).limit(3))).all()
    moving = (await s.scalars(select(Withdrawal).where(Withdrawal.user_id == user.id, Withdrawal.status.in_(
        ("queued", "pending", "unknown", "sent"))).order_by(Withdrawal.id))).all()
    last = {}
    for wd in (await s.scalars(select(Withdrawal).where(Withdrawal.user_id == user.id, Withdrawal.method == "chain")
                               .order_by(Withdrawal.id.desc()).limit(20))).all():
        last.setdefault(wd.network, wd.address)
    return web.json_response({
        "balance": {"available": num(user.balance), "frozen": num(user.frozen),
                    "withdrawable": num(money.withdrawable(user)), "team": num(user.team_balance)},
        "lock_note": lock_note(user) or None,
        "networks": [{"code": n, "name": xrocket.net_name(n), "last_address": last.get(n)} for n in nets],
        "deposit": {"min": settings.get("deposit_min"), "fee": fee_pct()},
        "withdraw": {"min": settings.get("withdraw_min"), "chain_min": settings.get("chain_withdraw_min"),
                     "cheque_terms": withdraw_terms("xrocket"), "chain_terms": withdraw_terms("chain")},
        "pending_deposits": [deposit_json(d) for d in pending],
        "withdrawals": [withdrawal_json(w) for w in moving],
    })


async def history(request: web.Request) -> web.Response:
    from bot.handlers.wallet import KINDS, ref_label
    s, user, _ = ctx(request)
    q = select(Ledger).where(Ledger.user_id == user.id)
    if (before := request.query.get("before", "")).isdigit():
        q = q.where(Ledger.id < int(before))
    rows = (await s.scalars(q.order_by(Ledger.id.desc()).limit(40))).all()
    return web.json_response({"items": [{
        "id": r.id, "kind": r.kind, "title": f"{KINDS.get(r.kind, r.kind)} {ref_label(r.ref)}".strip(),
        "delta": num(r.delta - r.frozen_delta), "frozen": num(r.frozen_delta) if r.frozen_delta else None,
        "ref": r.ref, "note": r.note, "at": iso(r.created_at)} for r in rows]})


async def deposit_new(request: web.Request) -> web.Response:
    from bot.handlers.wallet import new_address_deposit, new_invoice_deposit
    s, user, _ = ctx(request)
    data = await body(request)
    if data.get("network"):
        dep, err = await new_address_deposit(s, user, str(data["network"])[:5])
    else:
        dep, err = await new_invoice_deposit(s, user, parse_amount(data.get("amount"), 6))
    if dep is None:
        raise AppError(409, "deposit_failed", err)
    return web.json_response({"deposit": deposit_json(dep)})


async def own_deposit(request: web.Request) -> Deposit:
    s, user, _ = ctx(request)
    dep = await s.get(Deposit, int(request.match_info["id"]), populate_existing=True)
    if dep is None or dep.user_id != user.id:
        raise AppError(404, "not_found", "Пополнение не найдено")
    return dep


async def deposit_get(request: web.Request) -> web.Response:
    from bot.handlers.wallet import check_deposit
    s, user, _ = ctx(request)
    dep = await own_deposit(request)
    if dep.status in ("active", "new") and request.query.get("check") == "1":
        try:
            await check_deposit(s, dep)
        except xrocket.XRocketError as e:
            raise AppError(502, "check_failed", f"Не удалось проверить: {e.human}. Проверим автоматически")
        await s.refresh(dep)
        await s.refresh(user)
    return web.json_response({"deposit": deposit_json(dep), "available": num(user.balance)})


async def deposit_cancel(request: web.Request) -> web.Response:
    s, user, _ = ctx(request)
    dep = await own_deposit(request)
    dep = await s.get(Deposit, dep.id, with_for_update=True, populate_existing=True)
    if dep.status != "active":
        raise AppError(409, "closed", "Пополнение уже оплачено или закрыто")
    dep.status = "cancelled"
    events.add(s, f"dep:{dep.id}", "cancelled", "Пользователь отменил пополнение в приложении", user.id, notice=True)
    return web.json_response({"deposit": deposit_json(dep)})


async def withdraw_terms_of(method: str, network: str | None, amount: Decimal | None):
    """(fee, network fee, xRocket minimum) of a withdrawal."""
    from bot.handlers.wallet import chain_quota, withdraw_fee
    if method == "xrocket":
        return (withdraw_fee(amount, "xrocket") if amount else Decimal(0)), Decimal(0), Decimal(0)
    if network not in await xrocket.networks():
        raise AppError(409, "network_off", "Сеть сейчас недоступна")
    nf, xmin = await chain_quota(network)
    return (withdraw_fee(amount, "chain", nf) if amount else Decimal(0)), nf, xmin


async def withdraw_quote(request: web.Request) -> web.Response:
    from bot.handlers.wallet import withdraw_problem, withdraw_terms
    s, user, _ = ctx(request)
    method = "chain" if request.query.get("method") == "chain" else "xrocket"
    network = request.query.get("network")
    amount = parse_amount(request.query.get("amount"), 6) if request.query.get("amount") else None
    fee, nf, xmin = await withdraw_terms_of(method, network, amount)
    return web.json_response({
        "terms": withdraw_terms(method, nf), "fee": num(fee) if amount else None,
        "receive": num(amount - fee) if amount and amount > fee else None,
        "max": num(money.withdrawable(user)),
        "error": (withdraw_problem(user, amount, method, fee, xmin) or None) if amount else None,
    })


async def withdraw(request: web.Request) -> web.Response:
    from bot.handlers.wallet import _check_address, debit_withdrawal, notify_withdrawal, pay_or_queue, withdraw_problem
    s, user, bot = ctx(request)
    data = await body(request)
    method = "chain" if data.get("method") == "chain" else "xrocket"
    amount = parse_amount(data.get("amount"), 6)
    try:
        request_id = str(UUID(str(data.get("request_id"))))
    except ValueError:
        raise AppError(422, "bad_request", "Начните вывод заново")
    network = str(data.get("network") or "")[:5] if method == "chain" else None
    fee, nf, xmin = await withdraw_terms_of(method, network, amount)
    if err := withdraw_problem(user, amount, method, fee, xmin):
        raise AppError(422, "bad_amount", err)
    if data.get("fee") is not None and parse_amount(data.get("fee"), 6) != fee:
        raise AppError(409, "fee_changed", "Комиссия изменилась — проверьте сумму ещё раз")
    wd = Withdrawal(user_id=user.id, amount=amount, fee=fee, request_id=request_id)
    if method == "chain":
        addr, err = await _check_address(s, network, data.get("address"))
        if not addr:
            raise AppError(422, "bad_address", err)
        memo = data.get("memo") or None
        if memo is not None and (network != "TON" or not isinstance(memo, str) or not 1 <= len(memo.strip()) <= 120
                                 or not memo.strip().isprintable()):
            raise AppError(422, "bad_memo", "Memo — до 120 символов и только для TON")
        wd.method, wd.network, wd.address, wd.net_fee = "chain", network, addr, nf
        wd.memo = memo.strip() if memo else None
        what = (f"Запрос вывода в сети {xrocket.net_name(network)} (приложение): списано {money.usdt(amount)} USDT, к "
                f"отправке {money.usdt(amount - fee)} на {addr}" + (f", memo {wd.memo}" if wd.memo else ""))
    else:
        what = (f"Запрос вывода чеком (приложение): списано {money.usdt(amount)} USDT, чек "
                f"{money.usdt(amount - fee)}")
    if err := await debit_withdrawal(s, user, wd, what):
        raise AppError(409, "not_debited", err)
    result = await pay_or_queue(s, wd)
    await s.commit()
    await s.refresh(user)
    if result == "done":
        await notify_withdrawal(bot, wd, "done")
    net = money.usdt(wd.amount - wd.fee)
    message = {
        "done": f"Чек на {net} USDT — в чате с ботом: активируйте его, и USDT придут в @xRocket" if method == "xrocket"
        else f"Вывод #{wd.id} выполнен: {net} USDT отправлены",
        "sent": f"Вывод #{wd.id} принят: {net} USDT уйдут в течение нескольких минут",
        "queued": f"Вывод #{wd.id} в очереди: {net} USDT отправим автоматически, обычно в течение часа",
        "unknown": f"Вывод #{wd.id} на проверке: сумма удержана, сверим автоматически",
    }.get(result) or f"Вывод не выполнен: {result}. Средства возвращены на баланс"
    return web.json_response({"result": result, "ok": result in ("done", "sent", "queued", "unknown"),
                              "message": message, "withdrawal": withdrawal_json(wd), "available": num(user.balance)})


async def withdraw_cancel(request: web.Request) -> web.Response:
    s, user, _ = ctx(request)
    wd = await s.get(Withdrawal, int(request.match_info["id"]), with_for_update=True, populate_existing=True)
    if wd is None or wd.user_id != user.id or wd.status != "queued":
        raise AppError(409, "too_late", "Вывод уже отправляется — отменить нельзя")
    wd.status = "cancelled"
    await money.add(s, user.id, wd.amount, "withdraw_refund", f"wd:{wd.id}")
    events.add(s, f"wd:{wd.id}", "cancelled", f"Пользователь отменил вывод из очереди в приложении, "
               f"{money.usdt(wd.amount)} USDT возвращены", user.id, notice=True)
    return web.json_response({"withdrawal": withdrawal_json(wd)})


# ---------- cards ----------

async def cards_json(s, user: User) -> dict:
    from bot.handlers.seller import _cards, mask
    cards = await _cards(s, user.id)
    busy = await deals.busy_cards(s, user.id, full=True)
    used = await deals.used_today(s, [c.id for c in cards])
    totals = dict((cid, (n, rub)) for cid, n, rub in (await s.execute(
        select(Deal.card_id, func.count(Deal.id), func.coalesce(func.sum(Deal.amount_rub), 0)).where(
            Deal.card_id.in_([c.id for c in cards] or [0]), Deal.status == "completed").group_by(Deal.card_id))).all())
    out = []
    for c in cards:
        visible, why = deals.card_visibility(c, user, busy.get(c.id), used[c.id])
        n, rub = totals.get(c.id, (0, 0))
        out.append({"id": c.id, "type": c.kind, "bank": c.bank, "mask": mask(c), "number": c.requisites,
                    "holder": c.holder, "min": num(c.min_rub), "max": num(c.max_rub),
                    "daily": num(c.daily_limit_rub) if c.daily_limit_rub else None, "used_today": num(used[c.id]),
                    "active": c.is_active, "banned": c.is_banned, "visible": visible, "why": why,
                    "busy_deal": busy.get(c.id), "deals": n, "turnover": num(Decimal(rub))})
    return {"online": user.is_online, "auto_off": settings.num("online_minutes") or None,
            "pct": num(settings.merchant_pct(user)),
            "cap_rub": num(money.max_rub(user.balance, settings.dec("rate"), settings.merchant_pct(user))
                           .quantize(Decimal("1"))),
            "cards": out}


async def cards(request: web.Request) -> web.Response:
    s, user, _ = ctx(request)
    return web.json_response(await cards_json(s, user))


async def shift(request: web.Request) -> web.Response:
    s, user, _ = ctx(request)
    user.is_online = bool((await body(request)).get("online"))
    return web.json_response(await cards_json(s, user))


async def card_update(request: web.Request) -> web.Response:
    from bot.handlers.seller import _cards, check_field, own_card, set_field
    s, user, _ = ctx(request)
    card = await own_card(s, user, request.match_info["id"])
    if card is None or card.is_banned:
        raise AppError(404, "not_found", "Карта недоступна")
    data = await body(request)
    from bot.handlers.seller import parse_rub
    raise_both = (parse_rub(str(data.get("min") or "")) or 0) > card.max_rub  # a higher max goes first then
    for field in ("max", "min", "daily", "bank", "holder") if raise_both else ("min", "max", "daily", "bank", "holder"):
        if field in data and data[field] is not None:
            value, err = await check_field(s, user, card, field, str(data[field]))
            if err:
                raise AppError(422, f"bad_{field}", err)
            set_field(card, field, value)
    if (data.get("active") or data.get("solo")) and (problem := deals.flow_problem(card, user)):
        raise AppError(409, "no_flow", f"Нельзя поставить в поток: {problem}")
    if "active" in data:
        card.is_active = bool(data["active"])
        if card.is_active:
            user.is_online = True  # "in the flow" means buyers can see it: the shift starts
    if data.get("solo"):
        card.is_active, user.is_online = True, True
        for other in await _cards(s, user.id):
            if other.id != card.id:
                other.is_active = False
    events.add(s, f"card:{card.id}", "app_edit", "Карта изменена в приложении: " + ", ".join(sorted(data))[:200],
               user.id)
    return web.json_response(await cards_json(s, user))


async def card_add(request: web.Request) -> web.Response:
    import re

    from bot.handlers.seller import check_bank, check_holder, check_requisites, mask, parse_rub, requisites_taken
    s, user, _ = ctx(request)
    data = await body(request)
    digits = re.sub(r"\D", "", str(data.get("number") or ""))
    kind = "card" if len(digits) >= 16 else "sbp"
    number, err = check_requisites(kind, digits)
    if not number:
        raise AppError(422, "bad_number", err)
    if err := await requisites_taken(s, number, user):
        raise AppError(409, "taken", err)
    bank, holder = check_bank(str(data.get("bank") or "")), check_holder(str(data.get("holder") or ""))
    if not bank:
        raise AppError(422, "bad_bank", "Название банка — от 2 до 40 символов")
    if not holder:
        raise AppError(422, "bad_holder", "ФИО получателя буквами, минимум 2 слова")
    lo, hi = parse_rub(str(data.get("min") or "")), parse_rub(str(data.get("max") or ""))
    if lo is None or hi is None or hi < lo:
        raise AppError(422, "bad_range", "Минимум и максимум — числа, максимум не меньше минимума")
    card = Card(user_id=user.id, kind=kind, bank=bank, requisites=number, holder=holder, min_rub=lo, max_rub=hi)
    problem = deals.flow_problem(card, user)
    card.is_active = not problem  # in the flow only with a balance behind it
    s.add(card)
    await s.flush()
    events.add(s, f"card:{card.id}", "added", f"Новая карта {card.bank} {mask(card)}, {money.fmt(lo)}–{money.fmt(hi)} ₽ "
               "(приложение)", user.id, notice=True)
    if not problem:
        user.is_online = True  # a new card is meant to work right away
    out = await cards_json(s, user)
    out["notice"] = f"Карта сохранена, но не в потоке: {problem}" if problem else "Карта сохранена и в потоке"
    return web.json_response(out)


# ---------- guides ----------

async def guides(request: web.Request) -> web.Response:
    from bot.guides import GUIDES
    return web.json_response({"guides": [{"slug": slug, "title": t, "about": a} for slug, t, a in GUIDES if slug]})


async def guide(request: web.Request) -> web.Response:
    from bot.api import docs
    from bot.guides import GUIDES
    slug = request.match_info["slug"]
    path = docs.GUIDES_DIR / f"{slug}.md"
    known = {g[0]: g[1] for g in GUIDES if g[0]}
    if slug not in known or not path.is_file():
        raise AppError(404, "not_found", "Нет такой инструкции")
    return web.json_response({"title": known[slug], "html": docs.render(path.read_text(encoding="utf-8"))})


def setup(app: web.Application, bot: Bot) -> None:
    app[BOT] = bot
    app.middlewares.append(guard)
    r = app.router
    r.add_get("/app", page)
    r.add_get("/app/", page)
    r.add_get("/app/{name:app\\.(css|js)}", asset)
    r.add_get("/app/api/me", me)
    r.add_post("/app/api/settings", save_settings)
    r.add_post("/app/api/chat-invite", chat_invite)
    r.add_get("/app/api/stats", stats)
    r.add_get("/app/api/deals", deal_list)
    r.add_get("/app/api/deals/{id:\\d+}", deal_get)
    r.add_post("/app/api/deals/{id:\\d+}/cancel", deal_cancel)
    r.add_post("/app/api/deals/{id:\\d+}/confirm", deal_confirm)
    r.add_post("/app/api/deals/{id:\\d+}/receipt", deal_receipt)
    r.add_get("/app/api/deals/{id:\\d+}/chat", chat_get)
    r.add_post("/app/api/deals/{id:\\d+}/chat", chat_post)
    r.add_get("/app/api/operator", operator_cabinet)
    r.add_post("/app/api/deals/{id:\\d+}/{act:accept|requisites|recreate|close|pass_on|no_requisites}", operator_act)
    r.add_get("/app/api/buy/quote", buy_quote)
    r.add_post("/app/api/buy", buy)
    r.add_get("/app/api/wallet", wallet)
    r.add_get("/app/api/history", history)
    r.add_post("/app/api/deposits", deposit_new)
    r.add_get("/app/api/deposits/{id:\\d+}", deposit_get)
    r.add_post("/app/api/deposits/{id:\\d+}/cancel", deposit_cancel)
    r.add_get("/app/api/withdraw/quote", withdraw_quote)
    r.add_post("/app/api/withdraw", withdraw)
    r.add_post("/app/api/withdrawals/{id:\\d+}/cancel", withdraw_cancel)
    r.add_get("/app/api/cards", cards)
    r.add_post("/app/api/cards", card_add)
    r.add_post("/app/api/cards/{id:\\d+}", card_update)
    r.add_post("/app/api/shift", shift)
    r.add_get("/app/api/guides", guides)
    r.add_get("/app/api/guides/{slug:[a-z]+}", guide)
