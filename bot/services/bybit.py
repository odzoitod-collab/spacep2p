"""Documented Bybit P2P API only. No retries of mutations inside the HTTP client.

Sources and capability gaps are recorded in docs/BYBIT.md. Credentials are never logged.
"""
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from urllib.parse import parse_qs, urlsplit

import httpx

from bot.config import BybitCredential


class BybitError(Exception):
    def __init__(self, code: str, *, uncertain: bool = False):
        self.code = str(code)[:64]
        self.uncertain = uncertain
        super().__init__(f"Bybit: {self.code}")


class BybitClient:
    def __init__(self, credentials: BybitCredential, testnet: bool = False, transport=None):
        self.credentials = credentials
        self.http = httpx.AsyncClient(
            base_url="https://api-testnet.bybit.com" if testnet else "https://api.bybit.com",
            timeout=20, follow_redirects=False, transport=transport,
        )

    async def close(self):
        await self.http.aclose()

    def headers(self, body: bytes) -> dict[str, str]:
        ts, window = str(int(time.time() * 1000)), "5000"
        key = self.credentials.api_key.get_secret_value()
        sig = hmac.new(self.credentials.api_secret.get_secret_value().encode(),
                       (ts + key + window).encode() + body, hashlib.sha256).hexdigest()
        return {"X-BAPI-API-KEY": key, "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": window,
                "X-BAPI-SIGN": sig, "X-BAPI-SIGN-TYPE": "2"}

    async def request(self, path: str, payload: dict, *, mutation: bool = False) -> dict:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        headers = self.headers(body) | {"Content-Type": "application/json"}
        try:
            response = await self.http.post(path, content=body, headers=headers)
        except httpx.HTTPError:
            raise BybitError("transport", uncertain=mutation) from None
        return self.result(response, mutation)

    @staticmethod
    def result(response: httpx.Response, mutation: bool) -> dict:
        if response.status_code != 200:
            raise BybitError(f"http_{response.status_code}", uncertain=mutation)
        try:
            data = response.json()
            code = data.get("retCode", data.get("ret_code"))
            if code is None:
                raise ValueError
            if int(code) != 0:
                raise BybitError(str(code), uncertain=mutation and int(code) in (10000, 10016))
            result = data.get("result") or {}
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (ValueError, AttributeError, TypeError):
            raise BybitError("invalid_response", uncertain=mutation) from None

    async def order(self, order_id: str) -> dict:
        return await self.request("/v5/p2p/order/info", {"orderId": order_id})

    async def session(self, user_mask_id: str) -> str:
        result = await self.request("/v5/p2p/chat/session/getSessionId", {"userMaskId": user_mask_id})
        if not result.get("sessionId"):
            raise BybitError("missing_session")
        return str(result["sessionId"])

    async def messages(self, session_id: str, since_ms: int) -> list[dict]:
        out, cursor = [], 0
        # Bound work; an incomplete history cannot be used to extract payment details.
        for _ in range(10):
            page = await self.request("/v5/p2p/chat/message/listpage_v1",
                                      {"sessionId": session_id, "lastId": cursor, "limit": 30})
            rows = page.get("messages", [])
            if not isinstance(rows, list):
                raise BybitError("invalid_messages")
            out.extend(row for row in rows if int(row.get("createDate", 0)) >= since_ms)
            if len(rows) < 30 or any(int(row.get("createDate", 0)) < since_ms for row in rows):
                return out
            next_cursor = int(rows[-1]["id"])
            if next_cursor == cursor:
                break
            cursor = next_cursor
        raise BybitError("chat_history_incomplete")

    async def upload_pdf(self, data: bytes, filename: str) -> str:
        if not data.startswith(b"%PDF-") or len(data) > 10 * 1024 * 1024:
            raise BybitError("invalid_pdf")
        # Sign precisely the multipart bytes sent, as in Bybit's official P2P SDK.
        request = self.http.build_request("POST", "/v5/p2p/oss/upload_file",
                                          files={"upload_file": (filename, data, "application/pdf")})
        body = await request.aread()
        request.headers.update(self.headers(body))
        try:
            response = await self.http.send(request)
        except httpx.HTTPError:
            raise BybitError("upload_transport", uncertain=True) from None
        result = self.result(response, True)
        if not result.get("url"):
            raise BybitError("missing_upload_url", uncertain=True)
        return str(result["url"])

    async def send_pdf(self, session_id: str, order_id: str, url: str):
        await self.request("/v5/p2p/chat/message/send_v1", {
            "sessionId": session_id, "orderId": order_id, "message": url, "contentType": "pdf",
        }, mutation=True)

    async def mark_paid(self, order_id: str, payment_id: str, payment_type: str):
        await self.request("/v5/p2p/order/pay", {
            "orderId": order_id, "paymentId": payment_id, "paymentType": payment_type,
        }, mutation=True)


def advertisement_id(link: str) -> str:
    """Parse an ID; never request arbitrary merchant URLs or follow their redirects."""
    u = urlsplit(link.strip())
    if (u.scheme != "https" or u.hostname not in ("www.bybit.com", "bybit.com", "www.bybitglobal.com")
            or u.username or u.password or u.port not in (None, 443)
            or not re.search(r"/(?:fiat/)?trade/otc/(?:item|advertisement)(?:/|$)", u.path)):
        raise BybitError("invalid_ad_link")
    candidates = [v for key in ("itemId", "id", "advertisementId") for v in parse_qs(u.query).get(key, [])]
    tail = u.path.rstrip("/").rsplit("/", 1)[-1]
    if tail.isdigit():
        candidates.append(tail)
    if len(set(candidates)) != 1 or not re.fullmatch(r"[0-9]{1,32}", candidates[0]):
        raise BybitError("missing_ad_id")
    return candidates[0]


def decimal_value(value) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise BybitError("invalid_amount") from None
    if not result.is_finite() or result < 0:
        raise BybitError("invalid_amount")
    return result


def gross_quantity(deal) -> Decimal:
    """Enter gross crypto amount, not the buyer's credit after the platform commission."""
    return (deal.amount_rub / deal.rate).quantize(Decimal("0.000001"), ROUND_DOWN)


def validate_order(detail: dict, deal, ad_id: str, uid: str, expected_order_id: str) -> Decimal:
    if (str(detail.get("id")) != expected_order_id or str(detail.get("userId")) != uid
            or str(detail.get("itemId")) != ad_id or str(detail.get("side")) != "0"
            or detail.get("tokenId") != "USDT" or detail.get("currencyId") != "RUB"
            or detail.get("orderType") != "ORIGIN" or detail.get("usedCoupon") not in (False, None)):
        raise BybitError("order_identity_mismatch")
    if decimal_value(detail.get("amount")) != deal.amount_rub:
        raise BybitError("fiat_amount_mismatch")
    quantity = decimal_value(detail.get("quantity"))
    # The current commission and merchant collateral model is preserved. Price changes require review.
    if abs(quantity - gross_quantity(deal)) > Decimal("0.000001"):
        raise BybitError("crypto_amount_mismatch")
    if decimal_value(detail.get("takerFee") or "0") != 0:
        raise BybitError("unexpected_taker_fee")
    if detail.get("notifyTokenId") not in (None, "USDT"):
        raise BybitError("received_asset_mismatch")
    if detail.get("notifyTokenQuantity") is not None and decimal_value(detail["notifyTokenQuantity"]) != quantity:
        raise BybitError("received_amount_mismatch")
    return quantity


@dataclass(frozen=True)
class Requisites:
    kind: str
    number: str
    bank: str
    holder: str
    payment_id: str
    payment_type: str


BANK_ALIASES = {
    "Сбербанк": r"сбер(?:банк)?",
    "Т-Банк": r"т[ -]?банк|тинькофф",
    "Альфа-Банк": r"альфа(?:[ -]?банк)?",
    "ВТБ": r"втб",
    "Райффайзен": r"райффайзен(?:банк)?",
    "Озон Банк": r"озон(?:[ -]?банк)?|ozon(?:[ -]?bank)?",
    "Газпромбанк": r"газпром(?:банк)?",
    "Совкомбанк": r"совком(?:банк)?",
}


def _number(raw: str) -> tuple[str, str] | None:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits[0] in "78":
        return "sbp", "+7" + digits[1:]
    if len(digits) == 10 and digits.startswith("9"):
        return "sbp", "+7" + digits
    if 16 <= len(digits) <= 19:
        total = 0
        for i, digit in enumerate(reversed(digits)):
            n = int(digit) * (2 if i % 2 else 1)
            total += n - 9 if n > 9 else n
        if total % 10:
            return None
        return "card", digits
    return None


def message_content(row: dict) -> str:
    try:
        content = json.loads(row.get("message", ""))
    except (ValueError, TypeError):
        return ""
    return str(content.get("content", "")) if isinstance(content, dict) else ""


def extract_requisites(detail: dict, messages: list[dict]) -> Requisites | None:
    """Only seller messages since creation of this order. Do not guess among multiple recipients.

    Sessions can span multiple orders with the same counterparty; the caller must fetch a complete
    time-bounded history and serialize orders on an account. Ambiguity always needs review.
    """
    nickname = detail.get("targetNickName")
    if not nickname or nickname == detail.get("nickName"):
        raise BybitError("ambiguous_sender")
    since = int(detail["createDate"])
    texts = [message_content(row) for row in messages
             if row.get("contentType", "").lower() == "str" and row.get("sendUserNickName") == nickname
             and int(row.get("createDate", 0)) >= since]
    text = "\n".join(texts)
    numbers = {_number(m.group()) for m in re.finditer(r"(?<!\d)\+?\d[\d ()-]{8,30}\d(?!\d)", text)} - {None}
    banks = {name for name, pattern in BANK_ALIASES.items()
             if re.search(r"(?<!\w)(?:" + pattern + r")(?!\w)", text, re.I)}
    if not numbers:
        return None
    if len(numbers) != 1 or len(banks) > 1:
        raise BybitError("ambiguous_requisites")
    if not banks:
        return None
    kind, number = next(iter(numbers))
    bank = next(iter(banks))
    terms = detail.get("paymentTermList") or []
    # Match the actual bank/number if present, otherwise only a single supported payment method.
    candidates = [term for term in terms if str(term.get("paymentType")) != "377"
                  and term.get("id") is not None and term.get("paymentType") is not None]
    matching = [term for term in candidates if _number(str(term.get("accountNo", ""))) == (kind, number)]
    if matching:
        candidates = matching
    if len(candidates) != 1:
        raise BybitError("ambiguous_payment_method")
    term = candidates[0]
    term_number = _number(str(term.get("accountNo", "")))
    term_banks = {name for name, pattern in BANK_ALIASES.items()
                  if re.search(r"(?<!\w)(?:" + pattern + r")(?!\w)", str(term.get("bankName", "")), re.I)}
    if (term_number and term_number != (kind, number)) or (term_banks and bank not in term_banks):
        raise BybitError("chat_payment_mismatch")
    holder = str(term.get("realName") or detail.get("sellerRealName") or "").strip()
    if not 2 <= len(holder) <= 128:
        raise BybitError("missing_holder")
    return Requisites(kind, number, bank, holder, str(term["id"]), str(term["paymentType"]))
