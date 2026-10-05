"""Strait Pay merchant API: tokens, order representation, webhook outbox.

A token is shown to its owner once; only SHA-256 of it is stored. Orders are ordinary deals whose buyer is the
token owner (so a successful order credits USDT to the owner's balance on the usual terms) with api_client_id set.
Status changes reach the client as signed webhooks: a background task compares each order's status with the last
one queued (deals.api_notified), writes an ApiEvent row and delivers it with retries.
"""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import ApiClient, ApiEvent, Card, Deal, now
from bot.services import deals, settings

log = logging.getLogger(__name__)

TOKEN_PREFIX = "sp_live_"
# deal status -> API status (the vocabulary clients see; docs/API.md describes every one)
STATUS = {
    "searching": "searching_requisites",  # no static card fits: order merchants are asked for requisites
    "assigned": "merchant_assigned",  # a merchant took the request and prepares requisites (or a Bybit order)
    "checking": "requisites_check",  # Strait Pay checks the merchant's Bybit order before giving its requisites
    "waiting_payment": "awaiting_payment",  # show the requisites to the payer, wait for the receipt
    "paid": "verifying",  # receipt uploaded, the merchant checks the bank
    "dispute": "dispute",  # Strait Pay support decides
    "completed": "success",  # USDT credited to the token owner's balance
    "cancelled": "cancelled",
    "void": "cancelled",
    "expired": "expired",  # not paid in time; a late receipt may still be accepted
}
FINAL = {"success", "cancelled"}
# API status -> (step, title): a progress bar for the client's UI; finals have no step
STAGE = {"searching_requisites": (1, "Подбор реквизитов"), "merchant_assigned": (1, "Подбор реквизитов"),
         "requisites_check": (1, "Проверка реквизитов"), "awaiting_payment": (2, "Оплата"),
         "verifying": (3, "Проверка платежа"), "dispute": (3, "Спор")}
STAGES = 4
MAX_ATTEMPTS = 20  # ~1 day with the backoff below
TIMEOUT = 10


# ---------- tokens ----------

def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> tuple[str, str, str]:
    """(token shown once, sha256 stored, hint = last 4 characters)."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    return token, hash_token(token), token[-4:]


async def client_by_token(s: AsyncSession, token: str) -> ApiClient | None:
    if not token.startswith(TOKEN_PREFIX) or len(token) > 100:
        return None
    return await s.scalar(select(ApiClient).where(ApiClient.token_hash == hash_token(token)))


# ---------- orders ----------

def iso(dt: datetime | None) -> str | None:
    return deals.aware(dt).astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if dt else None


def next_action(d: Deal, status: str) -> str | None:
    """What the client should do now (docs/API.md §4)."""
    if status == "awaiting_payment":
        return "pay_and_upload_receipt"
    if status == "verifying":
        at = deals.buyer_dispute_at(d)
        return "dispute_available" if at and now() >= at else "wait"
    if status == "expired":
        until = deals.late_deadline(d)
        return "upload_late_receipt" if until and now() < until else None
    return None if status in FINAL else "wait"


# what exactly happens inside a status: the public status stays stable, `detail` tells the step
DETAIL_TEXT = {
    "searching_merchant": "Ищем ордерного мерчанта под сумму",
    "waiting_bybit_order": "Мерчант взял заявку и создаёт Bybit-ордер",
    "merchant_preparing_requisites": "Мерчант взял заявку и выдаёт реквизиты",
    "waiting_operator": "Ордер мерчанта ждёт оператора",
    "operator_checking_order": "Оператор проверяет ордер и выдаёт реквизиты",
    "awaiting_payment": "Реквизиты выданы — ждём перевод и чек",
    "verifying": "Чек у продавца — он проверяет поступление",
    "dispute": "Спор: решает поддержка Strait Pay",
    "success": "Готово: USDT зачислены на баланс",
    "cancelled": "Заказ отменён",
    "expired": "Время на оплату вышло",
}


def detail(d: Deal) -> str:
    if d.status == "searching":
        return "searching_merchant"
    if d.status == "assigned":
        return "waiting_bybit_order" if d.via_bybit else "merchant_preparing_requisites"
    if d.status == "checking":
        return "operator_checking_order" if d.operator_id else "waiting_operator"
    return STATUS[d.status]


def flow(d: Deal) -> dict:
    """How the requisites come: a merchant's own card, a merchant's balance, or a Bybit order an operator handles."""
    if not d.is_order:
        return {"type": "static_card", "via": None, "operator_assigned": False}
    return {"type": "order_requisites", "via": "bybit_order" if d.via_bybit else ("merchant_balance" if d.seller_id
                                                                                  else None),
            "operator_assigned": bool(d.operator_id)}


def order_json(d: Deal, card: Card | None) -> dict:
    status = STATUS[d.status]
    step = STAGE.get(status)
    out = {
        "id": d.id,
        "external_id": d.external_id,
        "payer_id": d.payer_id,
        "status": status,
        "amount_rub": str(d.amount_rub),
        "amount_usdt": str(d.buyer_credit),
        "rate": str(d.buyer_rate or d.rate),
        "fee_percent": str(d.platform_pct),
        "created_at": iso(d.created_at),
        "expires_at": iso(d.expires_at),
        "receipt_uploaded_at": iso(d.paid_at),
        "closed_at": iso(d.closed_at),
        "close_reason": d.close_reason,
        "requisites": None,
        "order_requisites": d.is_order,
        "detail": detail(d),
        "status_text": DETAIL_TEXT[detail(d)],
        "flow": flow(d),
        "next_action": next_action(d, status),
        "stage": {"step": step[0] if step else STAGES, "of": STAGES, "title": step[1] if step else
                  {"success": "Готово", "cancelled": "Отменён", "expired": "Время вышло"}[status]},
        "stage_deadline": iso(d.expires_at) if status in ("searching_requisites", "merchant_assigned",
                                                          "requisites_check", "awaiting_payment") else None,
    }
    if d.status in ("searching", "assigned", "checking"):
        out["search_expires_at"] = iso(d.expires_at)
    if card is not None and status in ("awaiting_payment", "verifying", "dispute"):
        out["requisites"] = {"bank": card.bank, "type": card.kind, "number": card.requisites, "holder": card.holder}
    if status == "verifying" and (at := deals.buyer_dispute_at(d)):
        out["dispute_available_at"] = iso(at)
    if status == "expired" and (until := deals.late_deadline(d)) and now() < until:
        out["late_receipt_until"] = iso(until)
    return out


async def history(s: AsyncSession, d: Deal) -> list[dict]:
    """Status changes of an API order, oldest first: the status it was created in, then every webhook queued."""
    rows = (await s.execute(select(ApiEvent.status, ApiEvent.created_at).where(ApiEvent.deal_id == d.id)
                            .order_by(ApiEvent.id))).all()
    out = [{"status": "searching_requisites" if d.is_order else "awaiting_payment", "at": iso(d.created_at)}]
    for st, at in rows:
        if STATUS[st] != out[-1]["status"]:  # assigned -> checking etc. may map to the same public status
            out.append({"status": STATUS[st], "at": iso(at)})
    return out


async def usage(s: AsyncSession, client: ApiClient) -> dict:
    waiting = await s.scalar(select(func.count(Deal.id)).where(
        Deal.api_client_id == client.id, Deal.status == "waiting_payment"))
    today = await s.scalar(select(func.coalesce(func.sum(Deal.amount_rub), 0)).where(
        Deal.api_client_id == client.id, Deal.created_at >= deals.day_start(),
        Deal.status.in_(deals.OPEN + ("completed",))))
    return {"open_orders": waiting, "today_rub": Decimal(today)}


def rates(client: ApiClient) -> dict:
    """The client's own terms: the same for orders on static cards and on order requisites."""
    from bot.services import money
    rate, pp = settings.client_terms(client)
    example = money.split(Decimal(10000), Decimal("Infinity"), rate, pp)
    return {"pair": "RUB/USDT", "rate": format(rate.normalize(), "f"), "fee_percent": format(pp.normalize(), "f"),
            "example": {"amount_rub": "10000", "amount_usdt": str(example.buyer_credit)}}


# ---------- webhooks ----------

def sign(secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def payload(event_id: int, status: str, order: dict, kind: str = "order.status") -> bytes:
    return json.dumps({"id": event_id, "type": kind, "status": status, "created_at": iso(now()), "order": order},
                      ensure_ascii=False, separators=(",", ":")).encode()


_http: httpx.AsyncClient | None = None


def http() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False,
                                  headers={"User-Agent": "StraitPay-Webhooks/1.0"})
    return _http


async def close() -> None:
    global _http
    if _http is not None:
        await _http.aclose()
        _http = None


async def url_problem(url: str) -> str:
    """Webhooks go only to public HTTPS addresses: never to the server itself or its private network (SSRF).
    Checked when the URL is saved and again before every delivery (DNS may change)."""
    parsed = httpx.URL(url) if url else None
    if parsed is None or parsed.scheme != "https" or not parsed.host or len(url) > 300:
        return "Нужен адрес https://… до 300 символов"
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parsed.host, parsed.port or 443)
    except OSError:
        return "Домен не найден"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            return "Адрес ведёт во внутреннюю сеть — укажите публичный сервер"
    return ""


async def post(client: ApiClient, body: bytes, event_id: int) -> tuple[bool, str]:
    """(delivered, what happened). Only a 2xx answer counts as delivered."""
    if problem := await url_problem(client.webhook_url or ""):
        return False, f"url: {problem}"
    ts = str(int(time.time()))
    try:
        r = await http().post(client.webhook_url, content=body, headers={
            "Content-Type": "application/json", "X-Strait-Event-Id": str(event_id), "X-Strait-Timestamp": ts,
            "X-Strait-Signature": "sha256=" + sign(client.webhook_secret, ts, body)})
    except httpx.HTTPError as e:
        return False, f"network: {type(e).__name__}"
    return r.is_success, f"HTTP {r.status_code}"


async def enqueue_changes(s: AsyncSession) -> int:
    """Queue a webhook for every API order whose status changed since the last one. Commits."""
    rows = (await s.scalars(select(Deal).where(
        Deal.api_client_id.is_not(None), (Deal.api_notified.is_(None)) | (Deal.api_notified != Deal.status))
        .order_by(Deal.id).limit(200))).all()
    for d in rows:
        s.add(ApiEvent(client_id=d.api_client_id, deal_id=d.id, status=d.status))
        d.api_notified = d.status
    await s.commit()
    return len(rows)


async def deliver(s: AsyncSession) -> None:
    """Send due webhooks; failures back off 10 s, 20 s, 40 s … up to 1 h, 20 attempts. Commits each."""
    due = (await s.scalars(select(ApiEvent).where(
        ApiEvent.delivered_at.is_(None), ApiEvent.next_at <= now(), ApiEvent.attempts < MAX_ATTEMPTS)
        .order_by(ApiEvent.id).limit(50))).all()
    for ev in due:
        client = await s.get(ApiClient, ev.client_id)
        if not client.webhook_url:
            ev.delivered_at, ev.last_error = now(), "webhook url not set"
            await s.commit()
            continue
        d = await s.get(Deal, ev.deal_id)
        card = await s.get(Card, d.card_id) if d.card_id else None
        body = payload(ev.id, STATUS[ev.status], order_json(d, card))
        ok_, what = await post(client, body, ev.id)
        ev.attempts += 1
        if ok_:
            ev.delivered_at, ev.last_error = now(), None
        else:
            ev.last_error = what
            ev.next_at = now() + timedelta(seconds=min(10 * 2 ** (ev.attempts - 1), 3600))
        await s.commit()


class RateLimiter:
    """Token bucket per client: `rps` requests per second, bursts up to 2×rps."""

    def __init__(self) -> None:
        self._buckets: dict[int, tuple[float, float]] = {}

    def allow(self, client_id: int, rps: int) -> bool:
        t = asyncio.get_running_loop().time()
        tokens, last = self._buckets.get(client_id, (2.0 * rps, t))
        tokens = min(2.0 * rps, tokens + (t - last) * rps)
        if tokens < 1:
            self._buckets[client_id] = (tokens, t)
            return False
        self._buckets[client_id] = (tokens - 1, t)
        return True
