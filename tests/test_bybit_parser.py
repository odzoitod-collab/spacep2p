"""The Bybit P2P parser (bot/services/bybit.py), on its own: advertisement links, requisites from the seller's chat,
the order checked against the deal, signed requests without retrying a money action. Not wired into the bot yet."""
import asyncio
import hashlib
import hmac
import json
from decimal import Decimal as D

import httpx
import pytest

from bot.config import BybitCredential
from bot.models import Deal, now
from bot.services.bybit import (BybitClient, BybitError, advertisement_id, extract_requisites,
                                gross_quantity, validate_order)


def credential(name="first", uid="101"):
    return BybitCredential(name=name, uid=uid, api_key="test-key", api_secret="test-secret")


def detail(did="999", ad="123", *, status=10, created=None):
    return {"id": did, "itemId": ad, "side": 0, "userId": "101", "tokenId": "USDT", "currencyId": "RUB",
            "price": "100", "quantity": "100", "amount": "10000", "orderType": "ORIGIN", "status": status,
            "createDate": str(created or int(now().timestamp() * 1000)), "targetUserMaskId": "mask",
            "targetNickName": "seller", "nickName": "our-account", "sellerRealName": "Иван Иванов",
            "transferLastSeconds": "900", "takerFee": "0", "notifyTokenId": "USDT", "notifyTokenQuantity": "100",
            "paymentTermList": [{"id": "44", "paymentType": "14", "realName": "Иван Иванов"}]}


def message(text, sender="seller", ts=None, kind="str"):
    return {"id": "1", "sendUserNickName": sender, "contentType": kind,
            "createDate": str(ts or int(now().timestamp() * 1000)), "message": json.dumps({"content": text})}


@pytest.mark.parametrize("link", ["https://www.bybit.com/fiat/trade/otc/item/123",
                                  "https://www.bybit.com/fiat/trade/otc/item?itemId=123",
                                  "https://www.bybit.com/fiat/trade/otc/advertisement?advertisementId=123"])
def test_advertisement_links(link):
    assert advertisement_id(link) == "123"


@pytest.mark.parametrize("link", ["https://bybit.com.evil.test/fiat/trade/otc/item/123",
                                  "https://evil.test/?itemId=123", "http://www.bybit.com/fiat/trade/otc/item/123",
                                  "https://u:p@www.bybit.com/fiat/trade/otc/item/123",
                                  "https://www.bybit.com/fiat/trade/otc/item/123?itemId=456",
                                  "https://www.bybit.com/fiat/trade/otc/order/123"])
def test_untrusted_or_ambiguous_links_rejected(link):
    with pytest.raises(BybitError):
        advertisement_id(link)


def test_parser_card_sbp_sender_time_and_ambiguity():
    d = detail(created=10000)
    card = extract_requisites(d, [message("Сбербанк\n4111 1111 1111 1111", ts=11000)])
    assert (card.kind, card.number, card.bank, card.payment_id) == ("card", "4111111111111111", "Сбербанк", "44")
    sbp = extract_requisites(d, [message("СБП Тинькофф +7 (999) 123-45-67", ts=11000)])
    assert (sbp.kind, sbp.number, sbp.bank) == ("sbp", "+79991234567", "Т-Банк")
    assert extract_requisites(d, [message("Сбер 4111111111111111", sender="our-account", ts=11000)]) is None
    assert extract_requisites(d, [message("Сбер 4111111111111111", ts=9000)]) is None
    assert extract_requisites(d, [message("4111111111111111", ts=11000)]) is None
    assert extract_requisites(d, [message("Сбер 4111111111111112", ts=11000)]) is None  # invalid card checksum
    with pytest.raises(BybitError, match="ambiguous_requisites"):
        extract_requisites(d, [message("Сбер 4111111111111111", ts=11000),
                               message("Т-Банк 5536913812345672", ts=12000)])
    with pytest.raises(BybitError, match="ambiguous_payment_method"):
        d["paymentTermList"].append({"id": "55", "paymentType": "15"})
        extract_requisites(d, [message("Сбер 4111111111111111", ts=11000)])


def test_chat_does_not_override_different_registered_payment():
    d = detail(created=10000)
    d["paymentTermList"][0]["accountNo"] = "5536913812345672"
    with pytest.raises(BybitError, match="chat_payment_mismatch"):
        extract_requisites(d, [message("Сбер 4111111111111111", ts=11000)])


def test_crypto_input_includes_existing_platform_commission():
    d = Deal(amount_rub=D(10000), rate=D(100), buyer_credit=D(94))
    assert gross_quantity(d) == D(100)  # not 94 or 94 * 1.06
    assert validate_order(detail(), d, "123", "101", "999") == D(100)
    for field, value in (("amount", "9400"), ("quantity", "94"), ("userId", "102"),
                         ("currencyId", "USD"), ("side", 1), ("takerFee", "0.1"),
                         ("notifyTokenQuantity", "99"), ("usedCoupon", True)):
        changed = detail() | {field: value}
        with pytest.raises(BybitError):
            validate_order(changed, d, "123", "101", "999")


def test_http_signatures_multipart_errors_and_no_mutation_retries():
    async def run():
        seen = []
        def reply(request):
            body, headers = request.content, request.headers
            sign = hmac.new(b"test-secret", (headers["X-BAPI-TIMESTAMP"] + "test-key5000").encode() + body,
                            hashlib.sha256).hexdigest()
            assert headers["X-BAPI-SIGN"] == sign
            seen.append(request.url.path)
            if request.url.path.endswith("upload_file"):
                assert b"%PDF-1.4" in body and b'name="upload_file"' in body
                return httpx.Response(200, json={"ret_code": 0, "result": {"url": "/receipt.pdf"}})
            return httpx.Response(200, json={"ret_code": 0, "result": {"id": "999"}})
        client = BybitClient(credential(), transport=httpx.MockTransport(reply))
        assert (await client.order("999"))["id"] == "999"
        assert await client.upload_pdf(b"%PDF-1.4\n", "receipt.pdf") == "/receipt.pdf"
        await client.close()
        def timeout(request):
            seen.append("timeout")
            raise httpx.ReadTimeout("secret-response", request=request)
        client = BybitClient(credential(), transport=httpx.MockTransport(timeout))
        with pytest.raises(BybitError) as error:
            await client.mark_paid("999", "44", "14")
        assert error.value.uncertain and "secret-response" not in str(error.value)
        assert seen.count("timeout") == 1
        await client.close()
        for data in ({"ret_code": 10005, "ret_msg": "sensitive text"}, {"result": {}}, {"ret_code": "abc"}):
            with pytest.raises(BybitError) as error:
                BybitClient.result(httpx.Response(200, json=data), True)
            assert "sensitive" not in str(error.value)
    asyncio.run(run())
