"""xRocket Pay API client (current API, not Legacy).

Docs: https://docs.xrocket.exchange/api/pay/pay-api-overview
Auth: Authorization: Bearer <token>. Amounts are decimal strings. Errors are RFC 9457 problem details.
"""
import asyncio
from decimal import Decimal

import httpx

ERRORS_RU = {
    "amount_more_than_app_balance": "недостаточно средств на балансе сервиса",
    "amount_less_than_minimum": "сумма меньше минимальной",
    "target_user_not_found": "ваш аккаунт не найден в xRocket — откройте @xRocket и повторите",
    "client_id_already_taken": "операция уже создана",
    "unauthorized": "ошибка авторизации API",
    "operation_disabled": "операция временно отключена",
    "network_is_suspended": "сеть временно приостановлена",
    "withdrawal_incorrect_address": "адрес не принят — проверьте адрес и сеть",
    "withdrawal_incorrect_comment": "комментарий (memo) не принят — проверьте его",
    "withdrawal_asset_not_allowed": "вывод USDT временно недоступен",
}
# Codes after which the operation may still have happened on xRocket's side.
UNCERTAIN = {"network", "invalid_response", "client_id_already_taken", "internal_error", "rate_limit_exceeded"}


class XRocketError(Exception):
    def __init__(self, code: str, detail: str = "", status: int = 0):
        self.code = code
        self.status = status
        super().__init__(f"{code}: {detail}")

    @property
    def human(self) -> str:
        return ERRORS_RU.get(self.code, "платёжный сервис временно недоступен")

    @property
    def uncertain(self) -> bool:
        """True if the request may have been executed: never treat such a failure as final."""
        return self.code in UNCERTAIN or self.status == 0 or self.status >= 500 or self.status == 429


class XRocket:
    def __init__(self, token: str, base_url: str):
        self._http = httpx.AsyncClient(
            base_url=base_url, timeout=20, headers={"Authorization": f"Bearer {token}"}
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _req(self, method: str, path: str, **kw) -> dict:
        try:
            r = await self._http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise XRocketError("network", str(e)) from e
        if r.is_success:
            if not r.content:
                return {}
            try:
                body = r.json()
            except ValueError as e:
                raise XRocketError("invalid_response", str(e)) from e
            if not isinstance(body, dict):
                raise XRocketError("invalid_response", "expected object")
            return body
        try:
            body = r.json()
        except ValueError:
            body = {}
        code = str(body.get("type", r.status_code)).rsplit("/", 1)[-1]
        raise XRocketError(code, body.get("detail") or body.get("title") or r.text[:300], r.status_code)

    async def create_invoice(self, amount: Decimal | None, client_id: str, description: str,
                             min_payment: Decimal | None = None, expires_ms: int = 3_600_000) -> dict:
        """amount None: an open-amount invoice (one payment of at least min_payment) — used for an address deposit."""
        body = {"priceCurrency": "USDT", "payoutCurrency": "USDT", "clientInvoiceId": client_id,
                "description": description, "expiresIn": expires_ms}
        if amount is None:
            body |= {"minPayment": str(min_payment or 1), "numPayments": 1, "payCurrencies": ["USDT"]}
        else:
            body["priceAmount"] = str(amount)
        result = await self._req("POST", "/api/v1/invoices", json=body)
        if not result.get("id"):
            raise XRocketError("invalid_response", "invoice id missing")
        return result

    async def payment_address(self, invoice_id: str, network: str) -> dict:
        """On-chain address that pays the invoice: {address, payNetwork, expiresAt, minAmount}."""
        result = await self._req("POST", "/api/v1/invoices/payments/address", params={"invoiceId": invoice_id},
                                 json={"payNetwork": network})
        if not result.get("address"):
            raise XRocketError("invalid_response", "payment address missing")
        return result

    async def usdt_networks(self) -> list[str]:
        """Networks xRocket supports for USDT right now (GET /api/v1/currencies)."""
        for cur in await self._req_list("GET", "/api/v1/currencies", params={"kind": "crypto"}):
            if cur.get("code") == "USDT":
                return [n["code"] for n in cur.get("networks") or [] if n.get("code")]
        return []

    async def _req_list(self, method: str, path: str, **kw) -> list:
        try:
            r = await self._http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise XRocketError("network", str(e)) from e
        if not r.is_success:
            raise XRocketError(str(r.status_code), r.text[:300], r.status_code)
        try:
            body = r.json()
        except ValueError as e:
            raise XRocketError("invalid_response", str(e)) from e
        return body if isinstance(body, list) else body.get("data") or body.get("items") or []

    async def get_invoice(self, invoice_id: str) -> dict:
        return await self._req("GET", "/api/v1/invoice", params={"invoiceId": invoice_id})

    async def get_invoice_by_client(self, client_id: str) -> dict:
        return await self._req("GET", "/api/v1/invoice", params={"clientInvoiceId": client_id})

    async def get_invoice_payments(self, invoice_id: str) -> list[dict]:
        items = []
        cursor = None
        while True:
            params = {"invoiceId": invoice_id, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            page = await self._req("GET", "/api/v1/invoice/payments", params=params)
            items.extend(page["items"])
            new_cursor = page.get("pagination", {}).get("next")
            if not new_cursor or new_cursor == cursor:
                return items
            cursor = new_cursor

    async def create_cheque(self, amount: Decimal, client_id: str, tg_id: int, description: str) -> dict:
        result = await self._req("POST", "/api/v1/cheques", json={
            "clientChequeId": client_id,
            "asset": "USDT",
            "amount": str(amount),
            "description": description,
            "targetType": "telegram_user_id",
            "target": str(tg_id),
        })
        if not result.get("chequeId"):
            raise XRocketError("invalid_response", "cheque id missing")
        return result

    async def get_cheque_by_client(self, client_id: str) -> dict:
        return await self._req("GET", "/api/v1/cheque", params={"clientChequeId": client_id})

    async def delete_cheque_by_client(self, client_id: str) -> None:
        await self._req("DELETE", "/api/v1/cheques", params={"clientChequeId": client_id})

    async def create_withdrawal(self, client_id: str, network: str, address: str, amount: Decimal,
                                comment: str | None) -> dict:
        """USDT from the app balance to an external address in `network`. clientWithdrawalId makes a repeated
        request safe: xRocket executes one withdrawal per id."""
        body = {"clientWithdrawalId": client_id, "network": network, "address": address, "asset": "USDT",
                "amount": str(amount)}
        if comment:
            body["comment"] = comment
        result = await self._req("POST", "/api/v1/withdrawals", json=body)
        if result.get("status") not in ("CREATED", "COMPLETED", "FAIL"):
            raise XRocketError("invalid_response", "withdrawal status missing")
        return result

    async def get_withdrawal(self, client_id: str) -> dict:
        return await self._req("GET", "/api/v1/withdrawal", params={"clientWithdrawalId": client_id})

    async def withdrawal_quota(self, network: str) -> dict:
        """Minimum and xRocket's own fee for USDT in `network`: {withdrawMinSize, withdrawFee, withdrawFeeAsset}."""
        return await self._req("GET", "/api/v1/withdrawal-quotas", params={"network": network, "asset": "USDT"})

    async def balances(self) -> list[dict]:
        return (await self._req("GET", "/api/v1/balances"))["balances"]

    @staticmethod
    def link(obj: dict) -> str | None:
        links = obj.get("links") or {}
        return links.get("telegramBotLink") or links.get("webLink") or links.get("telegramMiniAppLink")


# network code -> how users know it
NETWORKS = {"TON": "TON", "TRX": "TRC-20 (Tron)", "ETH": "ERC-20 (Ethereum)", "BSC": "BEP-20 (BNB Chain)",
            "SOL": "Solana", "BTC": "Bitcoin"}
NETWORKS_TTL = 3600
_networks: tuple[float, list[str]] | None = None
QUOTA_TTL = 600
_quotas: dict[str, tuple[float, dict]] = {}


def net_name(code: str | None) -> str:
    return NETWORKS.get(code or "TON", code or "TON")


async def networks() -> list[str]:
    """USDT networks of xRocket (cached 1 h); TON if xRocket does not answer."""
    global _networks
    t = asyncio.get_running_loop().time()
    if _networks is None or t - _networks[0] > NETWORKS_TTL:
        try:
            found = await asyncio.wait_for(rocket.usdt_networks(), 5)
        except Exception:  # noqa: BLE001 - keep the last answer
            return _networks[1] if _networks else ["TON"]
        _networks = t, [n for n in found if n in NETWORKS] or ["TON"]
    return _networks[1]


async def quota(network: str) -> dict | None:
    """xRocket's minimum and own fee for USDT in `network` (cached 10 min); None if xRocket does not answer."""
    t = asyncio.get_running_loop().time()
    if network not in _quotas or t - _quotas[network][0] > QUOTA_TTL:
        try:
            _quotas[network] = t, await asyncio.wait_for(rocket.withdrawal_quota(network), 5)
        except Exception:  # noqa: BLE001 - the withdrawal request itself is the final check
            return None
    return _quotas[network][1]


def net_fee(q: dict | None) -> Decimal:
    return Decimal(str(q["withdrawFee"])) if q and q.get("withdrawFeeAsset") == "USDT" else Decimal(0)


rocket: XRocket | None = None
_usdt: tuple[float, Decimal] | None = None  # (loop time, available USDT) of the last balance request
TOKEN_KEY = "xrocket_token"  # settings: a token set in the admin panel wins over XROCKET_TOKEN from .env


def token(env_token: str) -> str:
    from bot.services import settings
    return settings.raw(TOKEN_KEY) or env_token


def hint(tok: str) -> str:
    """A token as admins see it: never in full."""
    return f"…{tok[-4:]}" if len(tok) > 8 else "задан" if tok else "не задан"


async def check_token(tok: str, base_url: str) -> Decimal:
    """Available USDT of the app behind `tok`; raises XRocketError if xRocket does not accept it."""
    client = XRocket(tok, base_url)
    try:
        bal = await asyncio.wait_for(client.balances(), 10)
    except asyncio.TimeoutError as e:
        raise XRocketError("network", "timeout") from e
    finally:
        await client.close()
    usdt = next((b for b in bal if b.get("asset") == "USDT"), None)
    return Decimal(str(usdt.get("available", "0"))) if usdt else Decimal(0)


async def switch(tok: str, base_url: str) -> None:
    """Use another token from now on: a new client, cached answers of the old app forgotten."""
    global rocket, _usdt, _networks
    old, rocket = rocket, XRocket(tok, base_url)
    _usdt, _networks = None, None
    _quotas.clear()
    if old is not None and hasattr(old, "close"):
        await old.close()


async def usdt_available(max_age: float = 0, timeout: float = 5) -> Decimal:
    """Available USDT of the app. max_age > 0 reuses a recent answer (dashboard); withdrawals ask fresh.
    Raises XRocketError or asyncio.TimeoutError if xRocket does not answer in time."""
    global _usdt
    now = asyncio.get_running_loop().time()
    if _usdt is None or now - _usdt[0] > max_age:
        bal = await asyncio.wait_for(rocket.balances(), timeout)
        usdt = next((b for b in bal if b.get("asset") == "USDT"), None)
        _usdt = now, Decimal(str(usdt.get("available", "0"))) if usdt else Decimal(0)
    return _usdt[1]
