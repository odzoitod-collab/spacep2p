"""Strait Pay merchant API over HTTP (aiohttp, runs in the bot process). Reference: docs/API.md."""
import asyncio
import hashlib
import re
import logging
from decimal import Decimal, InvalidOperation
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile
from aiohttp import web
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from bot.api import docs as guides_site
from bot.config import config
from bot.handlers.deal import accept_receipt, log as deal_log, on_deal_created, push, send_to_seller
from bot.models import Card, Deal, OrderMerchant, Session, User
from bot.services import api, deals, events, money, orders, settings

log = logging.getLogger(__name__)
DOCS = Path(__file__).resolve().parent.parent.parent / "docs" / "API.md"
LIMIT_CODES = {"amount_limit", "open_limit", "daily_limit"}
PAYER_CODES = {"payer_blocked": 403, "payer_paused": 429}  # the client's own customer: blocked or on a pause
PAYER_ID = re.compile(r"[A-Za-z0-9_.:@-]{1,64}")
limiter = api.RateLimiter()
BOT = web.AppKey("bot", Bot)
SESSION = web.RequestKey("session", object)
CLIENT = web.RequestKey("client", object)
OWNER = web.RequestKey("owner", object)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def error(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"error": {"code": code, "message": message}}, status=status)


@web.middleware
async def guard(request: web.Request, handler):
    """JSON errors everywhere; Bearer token, client status and rate limit for /v1; one DB session per request."""
    try:
        if not request.path.startswith("/v1/"):
            return await handler(request)
        auth = request.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""
        async with Session() as s:
            client = await api.client_by_token(s, token) if token else None
            if client is None:
                return error(401, "unauthorized", "Missing or invalid token: Authorization: Bearer sp_live_…")
            owner = await s.get(User, client.user_id)
            if client.status != "active" or owner.is_banned:
                return error(403, "suspended", "API access is suspended. Contact Strait Pay support")
            if not limiter.allow(client.id, client.rps):
                return error(429, "rate_limited", f"Too many requests: limit {client.rps} per second")
            request[SESSION], request[CLIENT], request[OWNER] = s, client, owner
            return await handler(request)
    except ApiError as e:
        return error(e.status, e.code, e.message)
    except web.HTTPException:
        raise
    except Exception:  # noqa: BLE001 - never leak internals, always answer JSON
        log.exception("api %s %s failed", request.method, request.path)
        return error(500, "internal_error", "Internal error. Retry later; the operation was not applied")


# ---------- helpers ----------

def ctx(request: web.Request):
    return request[SESSION], request[CLIENT], request[OWNER], request.app[BOT]


def parse_rub(raw) -> Decimal:
    try:
        v = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise ApiError(422, "invalid_amount", "amount_rub must be a number, e.g. \"5000\" or \"5000.50\"")
    if not v.is_finite() or v <= 0 or v >= Decimal("100000000") or v.as_tuple().exponent < -2:
        raise ApiError(422, "invalid_amount", "amount_rub must be positive with at most 2 decimals")
    return v


async def body_json(request: web.Request) -> dict:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        raise ApiError(400, "invalid_json", "Request body must be a JSON object")
    if not isinstance(data, dict):
        raise ApiError(400, "invalid_json", "Request body must be a JSON object")
    return data


async def own_order(request: web.Request) -> Deal:
    s, client, *_ = ctx(request)
    try:
        oid = int(request.match_info["id"])
    except ValueError:
        raise ApiError(404, "not_found", "Order not found")
    d = await s.get(Deal, oid, populate_existing=True)
    if d is None or d.api_client_id != client.id:
        raise ApiError(404, "not_found", "Order not found")
    return d


async def order_response(s, d: Deal, status: int = 200) -> web.Response:
    return web.json_response(api.order_json(d, await s.get(Card, d.card_id) if d.card_id else None), status=status)


# ---------- public ----------

async def index(request: web.Request) -> web.Response:
    return web.json_response({"name": "Strait Pay API", "version": "v1", "docs": f"{config.api_url}/docs"})


async def docs(request: web.Request) -> web.Response:
    """The API reference and the guides as HTML pages (bot/api/docs.py); /docs.md is the raw API reference."""
    page = guides_site.guide(request.match_info.get("slug", ""))
    if page is None:
        raise web.HTTPNotFound(text="Нет такой инструкции. Все инструкции: /docs/help")
    return web.Response(text=page, content_type="text/html", charset="utf-8")


async def docs_static(request: web.Request) -> web.Response:
    found = guides_site.static(request.match_info["name"])
    if found is None:
        raise web.HTTPNotFound()
    return web.Response(body=found[0], content_type=found[1], headers={"Cache-Control": "public, max-age=86400"})


async def docs_raw(request: web.Request) -> web.Response:
    return web.Response(text=DOCS.read_text(encoding="utf-8"), content_type="text/markdown", charset="utf-8")


# ---------- account ----------

async def me(request: web.Request) -> web.Response:
    s, client, owner, _ = ctx(request)
    used = await api.usage(s, client)
    return web.json_response({
        "project": client.project, "status": client.status, "token_hint": client.token_hint,
        "webhook_url": client.webhook_url,
        "limits": {"min_order_rub": str(client.min_rub), "max_order_rub": str(client.max_rub),
                   "daily_rub": str(client.daily_rub), "max_open_orders": client.max_open,
                   "requests_per_second": client.rps},
        "usage": {"open_orders": used["open_orders"], "today_rub": str(used["today_rub"])},
        "balance": {"available_usdt": str(owner.balance), "frozen_usdt": str(owner.frozen),
                    "withdrawable_usdt": str(money.withdrawable(owner))},
    })


async def balance(request: web.Request) -> web.Response:
    _, _, owner, _ = ctx(request)
    return web.json_response({"currency": "USDT", "available": str(owner.balance), "frozen": str(owner.frozen),
                              "withdrawable": str(money.withdrawable(owner))})


async def rates(request: web.Request) -> web.Response:
    return web.json_response(api.rates(request[CLIENT]))


async def liquidity(request: web.Request) -> web.Response:
    """Amounts that can be ordered right now within the client's limits (no seller identities)."""
    s, client, owner, _ = ctx(request)
    rows = await deals.market(s, owner.id, None, None, None)
    ranges = []
    for card, _, lo, hi in rows:
        lo, hi = max(lo, client.min_rub), min(hi, client.max_rub)
        if lo <= hi:
            ranges.append({"min_rub": str(lo), "max_rub": str(hi), "bank": card.bank, "type": card.kind})
    # order merchants get every request, whatever the amount: any amount in the order range can be requested
    om = len([m for m in (await s.scalars(select(OrderMerchant).where(
        OrderMerchant.status == "approved", OrderMerchant.user_id != owner.id))).all() if not orders.asleep(m)])
    lo = max(settings.dec("order_min_rub"), client.min_rub).quantize(Decimal("0.01"))
    hi = min(settings.dec("order_max_rub"), client.max_rub).quantize(Decimal("0.01"))
    on = bool(om) and lo <= hi
    return web.json_response({
        "available": bool(ranges), "offers": len(ranges),
        "min_rub": str(min(Decimal(r["min_rub"]) for r in ranges)) if ranges else None,
        "max_rub": str(max(Decimal(r["max_rub"]) for r in ranges)) if ranges else None,
        "ranges": ranges,
        "order_requisites": {"available": on, "merchants": om, "min_rub": str(lo) if on else None,
                             "max_rub": str(hi) if on else None},
    })


# ---------- orders ----------

async def create_order(request: web.Request) -> web.Response:
    s, client, owner, bot = ctx(request)
    data = await body_json(request)
    amount = parse_rub(data.get("amount_rub"))
    ext = data.get("external_id")
    if ext is not None and (not isinstance(ext, str) or not 1 <= len(ext) <= 64):
        raise ApiError(422, "invalid_external_id", "external_id must be a string of 1–64 characters")
    bank, kind = data.get("bank"), data.get("type")
    if kind not in (None, "card", "sbp"):
        raise ApiError(422, "invalid_type", "type must be \"card\" or \"sbp\"")
    payer = data.get("payer_id")
    if payer is not None and (not isinstance(payer, str) or not PAYER_ID.fullmatch(payer)):
        raise ApiError(422, "invalid_payer_id", "payer_id: your customer's id, 1–64 of A–Z a–z 0–9 _ . : @ -")
    use_orders = data.get("order_requisites", True)
    sender_bank = data.get("sender_bank")
    if not isinstance(use_orders, bool) or (sender_bank is not None and (not isinstance(sender_bank, str)
                                                                         or not 2 <= len(sender_bank) <= 40)):
        raise ApiError(422, "invalid_field", "order_requisites must be boolean, sender_bank a string of 2–40 chars")
    if ext and (d := await s.scalar(select(Deal).where(Deal.api_client_id == client.id, Deal.external_id == ext))):
        return await order_response(s, d)  # idempotent retry: the same order, nothing new is created
    if not client.min_rub <= amount <= client.max_rub:
        raise ApiError(422, "amount_limit", f"amount_rub must be between {client.min_rub} and {client.max_rub}")
    offers = await deals.market(s, owner.id, amount, bank, kind)
    stats = await deals.completed_count(s, list({seller.id for _, seller, _, _ in offers}))
    offers.sort(key=lambda o: (-stats[o[1].id], o[0].id))  # the most reliable merchants first
    last = "No merchant can take this amount right now. See GET /v1/liquidity"
    for card, *_ in offers[:5]:
        try:
            d = await deals.create(s, owner, card.id, amount, client=client, external_id=ext, payer_id=payer)
        except deals.DealError as e:
            await s.rollback()
            await s.refresh(owner)
            if e.code in PAYER_CODES:
                raise ApiError(PAYER_CODES[e.code], e.code, str(e))
            if e.code in LIMIT_CODES:
                raise ApiError(409 if e.code == "open_limit" else 422, e.code, str(e))
            last = str(e)
            continue  # the card was taken meanwhile: try the next merchant
        d.api_notified = d.status  # the response itself reports the new order
        events.add(s, f"deal:{d.id}", "created", f"API {client.project}: {money.fmt(amount)} ₽ → "
                   f"{money.usdt(d.buyer_credit)} USDT, продавец {d.seller_id}", notice=True)
        try:
            await s.commit()
        except IntegrityError:  # the same external_id created by a parallel request
            await s.rollback()
            d = await s.scalar(select(Deal).where(Deal.api_client_id == client.id, Deal.external_id == ext))
            return await order_response(s, d)
        await on_deal_created(bot, s, d)
        await s.commit()
        return await order_response(s, d, 201)
    if use_orders:  # no static card fits: order merchants are asked to give requisites for this exact amount
        return await request_order(request, amount, sender_bank or bank, ext, payer)
    raise ApiError(409, "no_liquidity", last)


async def request_order(request: web.Request, amount: Decimal, sender_bank: str | None, ext: str | None,
                        payer: str | None = None):
    from bot.handlers.orders import broadcast
    s, client, owner, bot = ctx(request)
    try:
        d = await orders.create_request(s, owner, amount, sender_bank, client=client, external_id=ext, payer_id=payer)
    except deals.DealError as e:
        await s.rollback()
        await s.refresh(owner)
        if e.code in PAYER_CODES:
            raise ApiError(PAYER_CODES[e.code], e.code, str(e))
        if e.code in LIMIT_CODES:
            raise ApiError(409 if e.code == "open_limit" else 422, e.code, str(e))
        raise ApiError(409 if e.code != "order_range" else 422, "no_liquidity" if e.code != "order_range"
                       else "order_range", str(e))
    d.api_notified = d.status
    deal_log(s, d, "created", f"API {client.project}: заявка на ордерные реквизиты {money.fmt(amount)} ₽", notice=True)
    try:
        await s.commit()
    except IntegrityError:
        await s.rollback()
        d = await s.scalar(select(Deal).where(Deal.api_client_id == client.id, Deal.external_id == ext))
        return await order_response(s, d)
    await broadcast(bot, s, d)
    return await order_response(s, d, 202)


WAIT_MAX = 30  # seconds a long poll may hold


async def get_order(request: web.Request) -> web.Response:
    """GET /v1/orders/{id}[?wait=N&since=<status or detail>]: with `wait` the answer comes as soon as the order's
    status or detail differs from `since` (or after N ≤ 30 s) — no need to poll every second."""
    s = request[SESSION]
    d = await own_order(request)
    try:
        wait = min(max(int(request.query.get("wait", 0)), 0), WAIT_MAX)
    except ValueError:
        raise ApiError(422, "invalid_query", "wait must be an integer number of seconds (0–30)")
    since = request.query.get("since") or None
    if wait and since is not None:
        until = asyncio.get_running_loop().time() + wait
        oid = d.id
        while d is not None and since in (api.STATUS[d.status], api.detail(d)) \
                and asyncio.get_running_loop().time() < until:
            await asyncio.sleep(1)
            async with Session() as fresh:  # a short session per look: the request's own one holds no snapshot
                d = await fresh.get(Deal, oid)
        d = await s.get(Deal, oid, populate_existing=True)
        if d is None:
            raise ApiError(404, "not_found", "Order not found")
    return await order_response(s, d)


async def order_history(request: web.Request) -> web.Response:
    s = request[SESSION]
    d = await own_order(request)
    return web.json_response({"id": d.id, "status": api.STATUS[d.status], "history": await api.history(s, d)})


async def list_orders(request: web.Request) -> web.Response:
    s, client, *_ = ctx(request)
    q = select(Deal).where(Deal.api_client_id == client.id)
    if ext := request.query.get("external_id"):
        q = q.where(Deal.external_id == ext)
    if status := request.query.get("status"):
        internal = [k for k, v in api.STATUS.items() if v == status]
        if not internal:
            raise ApiError(422, "invalid_status", f"Unknown status {status!r}")
        q = q.where(Deal.status.in_(internal))
    try:
        limit = min(max(int(request.query.get("limit", 50)), 1), 100)
        before = int(request.query["before_id"]) if "before_id" in request.query else None
    except ValueError:
        raise ApiError(422, "invalid_query", "limit and before_id must be integers")
    if before:
        q = q.where(Deal.id < before)
    rows = (await s.scalars(q.order_by(Deal.id.desc()).limit(limit))).all()
    cards = {c.id: c for c in (await s.scalars(select(Card).where(Card.id.in_({d.card_id for d in rows})))).all()}
    return web.json_response({"orders": [api.order_json(d, cards.get(d.card_id)) for d in rows]})


def _kind(data: bytes) -> str | None:
    if data[:1024].find(b"%PDF-") >= 0:
        return "pdf"
    if settings.get("receipt_images") == "1" and (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"):
        return "image"
    return None


async def upload_receipt(request: web.Request) -> web.Response:
    s, client, owner, bot = ctx(request)
    d = await own_order(request)
    if d.status not in ("waiting_payment", "expired"):
        raise ApiError(409, "invalid_state", f"Receipt is not accepted in status {api.STATUS[d.status]}")
    data, name = b"", "receipt.pdf"
    if request.content_type.startswith("multipart/"):
        reader = await request.multipart()
        async for part in reader:
            if part.name == "file":
                name = (part.filename or name)[:80]
                data = await part.read(decode=False)
                break
    else:
        data = await request.read()
    if not data:
        raise ApiError(422, "file_required", "Send the receipt as multipart field \"file\" or as the raw body")
    if len(data) > config.api_receipt_mb * 1024 * 1024:
        raise ApiError(413, "file_too_large", f"Receipt must be at most {config.api_receipt_mb} MB")
    kind = _kind(data)
    if kind is None:
        raise ApiError(422, "not_a_receipt", "Only PDF receipts from the bank app are accepted"
                       + (" (or JPEG/PNG)" if settings.get("receipt_images") == "1" else ""))
    caption = f"Чек по API-заказу #{d.id} ({client.project}) — копия для вас"
    try:  # Telegram gives the file an id through a message: the copy goes to the token owner's own chat
        if kind == "pdf":
            m = await bot.send_document(owner.id, BufferedInputFile(data, name if name.lower().endswith(".pdf")
                                                                   else "receipt.pdf"), caption=caption)
            fid = m.document.file_id
        else:
            m = await bot.send_photo(owner.id, BufferedInputFile(data, "receipt.jpg"), caption=caption)
            fid = "photo:" + m.photo[-1].file_id
    except TelegramAPIError:
        raise ApiError(409, "owner_chat_unavailable", "Open the Strait Pay bot with the token owner's account and "
                                                      "press /start: receipts are stored through that chat")
    unique = "sha256:" + hashlib.sha256(data).hexdigest()[:57]
    paid, late, reused, err = await accept_receipt(s, d, owner, fid, unique)
    if not paid:
        raise ApiError(409, "receipt_rejected", err)
    await send_to_seller(bot, s, paid, late, reused)
    await s.commit()
    return await order_response(s, paid)


async def cancel_order(request: web.Request) -> web.Response:
    s, client, owner, bot = ctx(request)
    d = await own_order(request)
    if d.status in orders.REQUEST:
        from bot.handlers.orders import request_cancelled
        res = await orders.cancel(s, d.id)
        if res is None:
            raise ApiError(409, "invalid_state", "The order changed meanwhile, retry")
        deal_log(s, res, "cancelled", f"API {client.project} отменил заявку на реквизиты", notice=True)
        await s.commit()
        await request_cancelled(bot, s, res)
        return await order_response(s, res)
    res = await deals.cancel(s, d.id, ("waiting_payment",))
    if res is None:
        await s.rollback()
        raise ApiError(409, "invalid_state", "Only an order awaiting payment can be cancelled")
    deal_log(s, res, "cancelled", f"API {client.project} отменил заказ на {money.fmt(res.amount_rub)} ₽", notice=True)
    await s.commit()
    for uid in deals.sellers(res):
        await push(bot, s, uid, res, f"Покупатель отменил сделку #{res.id}")
    return await order_response(s, res)


async def dispute_order(request: web.Request) -> web.Response:
    s, client, owner, bot = ctx(request)
    d = await own_order(request)
    res = await deals.buyer_dispute(s, d.id, owner.id)
    if res is None:
        raise ApiError(409, "dispute_not_available", "A dispute can be opened only in status verifying after "
                                                     "dispute_available_at")
    deal_log(s, res, "dispute", f"API {client.project} открыл спор: продавец не подтверждает, "
                                f"{money.fmt(res.amount_rub)} ₽", alert=True)
    await s.commit()
    for uid in deals.sellers(res):
        await push(bot, s, uid, res, f"Покупатель открыл спор по сделке #{res.id}")
    return await order_response(s, res)


async def webhook_test(request: web.Request) -> web.Response:
    s, client, *_ = ctx(request)
    if not client.webhook_url:
        raise ApiError(409, "webhook_not_set", "Set the webhook URL in the bot: API → Webhook")
    delivered, what = await api.post(client, api.payload(0, "test", {}, kind="test"), 0)
    return web.json_response({"delivered": delivered, "result": what})


def build_app(bot: Bot) -> web.Application:
    app = web.Application(middlewares=[guard], client_max_size=(config.api_receipt_mb + 1) * 1024 * 1024)
    app[BOT] = bot
    from bot.api import webapp
    webapp.setup(app, bot)  # the mini app: /app and /app/api/*
    app.router.add_get("/", index)
    app.router.add_get("/docs", docs)
    app.router.add_get("/docs.md", docs_raw)
    app.router.add_get("/docs/static/{name}", docs_static)
    app.router.add_get("/docs/{slug:[a-z]+}", docs)
    app.router.add_get("/v1/me", me)
    app.router.add_get("/v1/balance", balance)
    app.router.add_get("/v1/rates", rates)
    app.router.add_get("/v1/liquidity", liquidity)
    app.router.add_post("/v1/orders", create_order)
    app.router.add_get("/v1/orders", list_orders)
    app.router.add_get("/v1/orders/{id}", get_order)
    app.router.add_get("/v1/orders/{id}/history", order_history)
    app.router.add_post("/v1/orders/{id}/receipt", upload_receipt)
    app.router.add_post("/v1/orders/{id}/cancel", cancel_order)
    app.router.add_post("/v1/orders/{id}/dispute", dispute_order)
    app.router.add_post("/v1/webhook/test", webhook_test)
    return app
