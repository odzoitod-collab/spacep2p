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
    "network_is_suspended": "вывод в сети TON временно приостановлен",
    "withdrawal_incorrect_address": "адрес не принят — проверьте, что это адрес в сети TON",
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

    async def create_invoice(self, amount: Decimal, client_id: str, description: str) -> dict:
        result = await self._req("POST", "/api/v1/invoices", json={
            "priceAmount": str(amount),
            "priceCurrency": "USDT",
            "payoutCurrency": "USDT",
            "clientInvoiceId": client_id,
            "description": description,
            "expiresIn": 3_600_000,
        })
        if not result.get("id"):
            raise XRocketError("invalid_response", "invoice id missing")
        return result

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

    async def create_withdrawal(self, client_id: str, address: str, amount: Decimal, comment: str | None) -> dict:
        """USDT in the TON network from the app balance to an external address. clientWithdrawalId makes a repeated
        request safe: xRocket executes one withdrawal per id."""
        body = {"clientWithdrawalId": client_id, "network": "TON", "address": address, "asset": "USDT",
                "amount": str(amount)}
        if comment:
            body["comment"] = comment
        result = await self._req("POST", "/api/v1/withdrawals", json=body)
        if result.get("status") not in ("CREATED", "COMPLETED", "FAIL"):
            raise XRocketError("invalid_response", "withdrawal status missing")
        return result

    async def get_withdrawal(self, client_id: str) -> dict:
        return await self._req("GET", "/api/v1/withdrawal", params={"clientWithdrawalId": client_id})

    async def deposit_address(self, amount: Decimal, client_id: str) -> str:
        """A TON address that tops up the app balance by `amount` USDT: an invoice for exactly this amount and its
        on-chain payment address (POST /api/v1/invoices, POST /api/v1/invoices/payments/address)."""
        inv = await self._req("POST", "/api/v1/invoices", json={
            "priceAmount": str(amount), "priceCurrency": "USDT", "payoutCurrency": "USDT", "payCurrencies": ["USDT"],
            "clientInvoiceId": client_id, "description": "Strait Pay: автоперевод USDT TON на баланс приложения",
            "expiresIn": 3_600_000})
        if not inv.get("id"):
            raise XRocketError("invalid_response", "invoice id missing")
        addr = await self._req("POST", "/api/v1/invoices/payments/address", params={"invoiceId": inv["id"]},
                               json={"payNetwork": "TON"})
        if not addr.get("address"):
            raise XRocketError("invalid_response", "payment address missing")
        return addr["address"]

    async def withdrawal_quota(self) -> dict:
        """Minimum and xRocket's own fee for USDT in TON: {withdrawMinSize, withdrawFee, withdrawFeeAsset}."""
        return await self._req("GET", "/api/v1/withdrawal-quotas", params={"network": "TON", "asset": "USDT"})

    async def balances(self) -> list[dict]:
        return (await self._req("GET", "/api/v1/balances"))["balances"]

    @staticmethod
    def link(obj: dict) -> str | None:
        links = obj.get("links") or {}
        return links.get("telegramBotLink") or links.get("webLink") or links.get("telegramMiniAppLink")


rocket: XRocket | None = None
_usdt: tuple[float, Decimal] | None = None  # (loop time, available USDT) of the last balance request


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
