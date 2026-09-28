import asyncio
from decimal import Decimal

import httpx

from bot.services.xrocket import XRocket, XRocketError


def test_pay_api_paths_and_string_amounts():
    async def scenario():
        seen = []

        def respond(req):
            seen.append(req)
            if req.url.path == "/api/v1/invoices":
                return httpx.Response(201, json={"id": "inv", "links": {"telegramBotLink": "https://t.me/x"}})
            if req.url.path == "/api/v1/cheques":
                if req.method == "DELETE":
                    return httpx.Response(200)
                return httpx.Response(201, json={"chequeId": "ch", "links": {"telegramBotLink": "https://t.me/x"}})
            if req.url.path == "/api/v1/invoice/payments":
                return httpx.Response(200, json={"items": [], "pagination": {"next": None}})
            return httpx.Response(200, json={"id": "inv", "status": "paid"})

        api = XRocket("secret", "https://pay.api.xrocket.exchange")
        await api._http.aclose()
        api._http = httpx.AsyncClient(base_url="https://pay.api.xrocket.exchange",
                                      transport=httpx.MockTransport(respond),
                                      headers={"Authorization": "Bearer secret"})
        await api.create_invoice(Decimal("1.250000"), "dep-1", "deposit")
        await api.get_invoice_by_client("dep-1")
        await api.get_invoice_payments("inv")
        await api.create_cheque(Decimal("1.25"), "wd-1", 123, "withdraw")
        await api.get_cheque_by_client("wd-1")
        await api.delete_cheque_by_client("wd-1")
        assert [(r.method, r.url.path) for r in seen] == [
            ("POST", "/api/v1/invoices"), ("GET", "/api/v1/invoice"),
            ("GET", "/api/v1/invoice/payments"), ("POST", "/api/v1/cheques"),
            ("GET", "/api/v1/cheque"), ("DELETE", "/api/v1/cheques"),
        ]
        assert b'"priceAmount":"1.250000"' in seen[0].content
        assert b'"amount":"1.25"' in seen[3].content
        assert b'"targetType":"telegram_user_id"' in seen[3].content
        assert seen[0].headers["Authorization"] == "Bearer secret"
        await api.close()

    asyncio.run(scenario())


def test_malformed_cheque_response_is_unknown_outcome():
    async def scenario():
        api = XRocket("secret", "https://pay.api.xrocket.exchange")
        await api._http.aclose()
        api._http = httpx.AsyncClient(base_url="https://pay.api.xrocket.exchange",
                                      transport=httpx.MockTransport(lambda req: httpx.Response(201, json={})))
        try:
            await api.create_cheque(Decimal(1), "wd-1", 123, "withdraw")
        except XRocketError as e:
            assert e.code == "invalid_response"
        else:
            assert False, "Missing cheque ID must not be recorded as a completed withdrawal"
        await api.close()

    asyncio.run(scenario())


def test_http_errors_classified_by_status():
    """500 internal_error / 429 / network: operation may have happened -> never refund automatically."""
    async def scenario():
        responses = iter([
            httpx.Response(500, json={"type": "/api/problems/internal_error", "title": "Internal"}),
            httpx.Response(429, json={"type": "/api/problems/rate_limit_exceeded"}),
            httpx.Response(400, json={"type": "/api/problems/amount_more_than_app_balance"}),
            httpx.Response(502, text="<html>bad gateway</html>"),
        ])
        api = XRocket("secret", "https://pay.api.xrocket.exchange")
        await api._http.aclose()
        api._http = httpx.AsyncClient(base_url="https://pay.api.xrocket.exchange",
                                      transport=httpx.MockTransport(lambda req: next(responses)))
        outcomes = []
        for _ in range(4):
            try:
                await api.create_cheque(Decimal(1), "wd-1", 123, "withdraw")
            except XRocketError as e:
                outcomes.append((e.code, e.status, e.uncertain))
        assert outcomes == [("internal_error", 500, True), ("rate_limit_exceeded", 429, True),
                            ("amount_more_than_app_balance", 400, False), ("502", 502, True)]
        assert XRocketError("network", "timeout").uncertain
        await api.close()

    asyncio.run(scenario())
