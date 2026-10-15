import asyncio
import hashlib
import hmac
import json
from datetime import timedelta
from decimal import Decimal as D

import httpx
import pytest
from sqlalchemy import func, select

from bot.config import BybitCredential, config
from bot.models import BybitAccount, BybitAction, BybitOrder, Deal, Ledger, User, now
from bot import models
from bot.services import bybit_orders, deals, money, orders
from bot.services.bybit import (BybitClient, BybitError, advertisement_id, extract_requisites,
                                gross_quantity, validate_order)
from tests.harness import cb, msg, plain


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


class FakeBybit:
    def __init__(self):
        self.detail = detail()
        self.chat = [message("Сбербанк\n4111 1111 1111 1111")]
        self.calls = []
        self.fail_send = False
        self.fail_paid = False

    async def order(self, order_id):
        self.calls.append("order")
        return self.detail.copy()

    async def request(self, path, payload):
        return {"count": 1, "items": [{"id": "999"}]}

    async def session(self, mask):
        return "session"

    async def messages(self, session, since):
        return self.chat

    async def upload_pdf(self, data, name):
        assert data.startswith(b"%PDF-")
        self.calls.append("upload")
        return "/receipt.pdf"

    async def send_pdf(self, session, order, url):
        self.calls.append("send")
        if self.fail_send:
            raise BybitError("transport", uncertain=True)
        self.chat.append(message(url, "our-account", kind="pdf"))

    async def mark_paid(self, order, pid, ptype):
        self.calls.append("pay")
        if self.fail_paid:
            raise BybitError("transport", uncertain=True)
        self.detail["status"] = 20


async def seed(b, monkeypatch, *, bind=True):
    monkeypatch.setattr(config, "bybit_enabled", True)
    fake = FakeBybit()
    monkeypatch.setattr(bybit_orders, "clients", {"first": fake})
    async with models.Session() as s:
        seller, buyer = User(id=7, balance=D(4), frozen=D(96)), User(id=8)
        s.add_all([seller, buyer, BybitAccount(name="first", uid="101")])
        await s.flush()
        d = Deal(buyer_id=8, seller_id=7, amount_rub=D(10000), rate=D(100), seller_pct=D(4), platform_pct=D(6),
                 seller_debit=D(96), buyer_credit=D(94), platform_fee=D(2), status="assigned", is_order=True,
                 expires_at=now() + timedelta(minutes=10))
        s.add(d)
        await s.flush()
        await bybit_orders.submit_link(s, d.id, seller, "https://www.bybit.com/fiat/trade/otc/item/123")
        if bind:
            await bybit_orders.bind_existing(s, d.id, "first", "999", 1)
        await s.commit()
        return d.id, fake


async def get_job(did):
    async with models.Session() as s:
        return await s.get(BybitOrder, did)


async def pay(did):
    async with models.Session() as s:
        d = await deals.mark_paid(s, did, 8, "test-pdf")
        assert d
        await bybit_orders.receipt_attached(s, d)
        await s.commit()


def test_bound_order_full_flow_and_settle_once(go, monkeypatch):
    async def run(b):
        did, fake = await seed(b, monkeypatch)
        await bybit_orders.process(b.bot, did)
        async with models.Session() as s:
            d = await s.get(Deal, did)
            assert d.status == "waiting_payment"
            assert await deals.complete(s, did) is None
            assert await deals.cancel(s, did, ("waiting_payment",)) is None
        await pay(did)
        async with models.Session() as s:
            assert await deals.complete(s, did) is None
        await bybit_orders.process(b.bot, did)
        assert fake.calls.count("send") == fake.calls.count("pay") == 1
        assert (await get_job(did)).stage == "waiting_release"
        fake.detail["status"] = 50
        await bybit_orders.process(b.bot, did)
        await bybit_orders.process(b.bot, did)
        async with models.Session() as s:
            assert (await s.get(Deal, did)).status == "completed"
            assert (await s.get(User, 8)).balance == D(94)
            seller = await s.get(User, 7)
            assert seller.balance == D(4) and seller.frozen == 0  # preserve the existing accounting
            assert (await s.get(BybitAccount, "first")).acquired_usdt == D(100)
            assert await s.scalar(select(func.count(Ledger.id)).where(Ledger.kind == "deal_buy")) == 1
            records = (await s.scalars(select(BybitAction))).all()
            assert any(row.action == "settled" for row in records)
            serialized = json.dumps([row.details for row in records])
            assert "4111111111111111" not in serialized and "test-secret" not in serialized
    go(run)


@pytest.mark.parametrize("mutation", ["send", "pay"])
def test_uncertain_mutations_and_restart_not_repeated(go, monkeypatch, mutation):
    async def run(b):
        did, fake = await seed(b, monkeypatch)
        await bybit_orders.process(b.bot, did)
        await pay(did)
        if mutation == "send":
            fake.fail_send = True
        else:
            fake.fail_paid = True
        await bybit_orders.process(b.bot, did)
        await bybit_orders.process(b.bot, did)
        assert fake.calls.count(mutation) == 1
        assert (await get_job(did)).stage == "review"
        async with models.Session() as s:
            assert (await s.get(User, 8)).balance == 0
            assert (await s.get(User, 7)).frozen == D(96)
    go(run)


def test_external_cancel_preserves_paid_funds(go, monkeypatch):
    async def run(b):
        did, fake = await seed(b, monkeypatch)
        await bybit_orders.process(b.bot, did)
        await pay(did)
        fake.detail["status"] = 40
        await bybit_orders.process(b.bot, did)
        assert (await get_job(did)).error_code == "cancelled_after_receipt"
        async with models.Session() as s:
            assert (await s.get(User, 7)).frozen == D(96)
            assert (await s.get(User, 8)).balance == 0
            assert await deals.cancel(s, did, ("paid", "dispute")) is None
    go(run)


def test_no_credit_for_wrong_order_or_completion_without_our_receipt(go, monkeypatch):
    async def run(b):
        did, fake = await seed(b, monkeypatch)
        fake.detail["status"] = 50
        await bybit_orders.process(b.bot, did)
        assert (await get_job(did)).stage == "review"
        async with models.Session() as s:
            assert (await s.get(User, 8)).balance == 0
    go(run)


def test_account_reservation_rotation_and_pause(go, monkeypatch):
    async def run(b):
        monkeypatch.setattr(bybit_orders, "clients", {"a": FakeBybit(), "b": FakeBybit()})
        async with models.Session() as s:
            s.add_all([BybitAccount(name="a", uid="11"), BybitAccount(name="b", uid="12")])
            await s.commit()
            assert (await bybit_orders.reserve_account(s)).name == "a"
            await s.commit()
            assert (await bybit_orders.reserve_account(s)).name == "b"
            await s.commit()
            a = await s.get(BybitAccount, "a")
            a.paused_until = now() + timedelta(hours=24)
            await s.commit()
            assert (await bybit_orders.reserve_account(s)).name == "b"
            b_account = await s.get(BybitAccount, "b")
            b_account.enabled = False
            await s.commit()
            assert await bybit_orders.reserve_account(s) is None
    go(run)


def test_entry_is_explicitly_unavailable_and_merchant_access_checked(go, monkeypatch):
    async def run(b):
        did, fake = await seed(b, monkeypatch, bind=False)
        await bybit_orders.process(b.bot, did)
        assert not fake.calls  # no invented purchase call
        job = await get_job(did)
        assert job.stage == "awaiting_entry" and job.order_id is None and job.account_name is None
        await b.run(msg(7, "/start"), cb(7, f"orq:bybit:{did}"))
        assert "пока проходит настройку" in plain(b.session.last(7))
        await b.run(msg(8, "/start"), cb(8, f"orq:bybit:{did}"))
        assert "Действие недоступно" in b.session.alerts()
    go(run)


def test_expiry_does_not_release_external_collateral(go, monkeypatch):
    async def run(b):
        did, fake = await seed(b, monkeypatch)
        await bybit_orders.process(b.bot, did)
        async with models.Session() as s:
            d = await s.get(Deal, did)
            d.expires_at = now() - timedelta(minutes=1)
            assert await deals.expire(s, did)
            await s.commit()
            assert await deals.release_hold(s, did) is None
            assert (await s.get(User, 7)).frozen == D(96)
        await bybit_orders.process(b.bot, did)
        assert (await get_job(did)).stage == "review"
    go(run)


def test_confirmed_unpaid_external_cancel_releases_once(go, monkeypatch):
    async def run(b):
        did, fake = await seed(b, monkeypatch)
        await bybit_orders.process(b.bot, did)
        fake.detail["status"] = 40
        await bybit_orders.process(b.bot, did)
        await bybit_orders.process(b.bot, did)
        async with models.Session() as s:
            assert (await s.get(Deal, did)).status == "cancelled"
            assert (await s.get(User, 7)).balance == D(100)
            assert (await s.get(User, 7)).frozen == 0
            assert await s.scalar(select(func.count(Ledger.id)).where(Ledger.kind == "unfreeze")) == 1
    go(run)


def test_restart_reconciles_sent_file_and_paid_status_without_replay(go, monkeypatch):
    async def run(b):
        did, fake = await seed(b, monkeypatch)
        await bybit_orders.process(b.bot, did)
        await pay(did)
        # Crash after exchange accepted send, before local acknowledgment.
        async with models.Session() as s:
            job = await s.get(BybitOrder, did)
            job.receipt_url = "/receipt.pdf"
            job.send_attempted_at = now()
            await s.commit()
        fake.chat.append(message("/receipt.pdf", "our-account", kind="pdf"))
        await bybit_orders.process(b.bot, did)
        assert "send" not in fake.calls and fake.calls.count("pay") == 1
        # Crash after exchange accepted paid mark, before local acknowledgment.
        async with models.Session() as s:
            job = await s.get(BybitOrder, did)
            job.stage = "receipt"
            await s.commit()
        await bybit_orders.process(b.bot, did)
        assert fake.calls.count("pay") == 1
        assert (await get_job(did)).stage == "waiting_release"
    go(run)


def test_manual_resume_preserves_uncertain_markers(go, monkeypatch):
    async def run(b):
        did, fake = await seed(b, monkeypatch)
        await bybit_orders.process(b.bot, did)
        await pay(did)
        fake.fail_paid = True
        await bybit_orders.process(b.bot, did)
        original = (await get_job(did)).paid_attempted_at
        async with models.Session() as s:
            await bybit_orders.resume(s, did, 1)
            await s.commit()
        await bybit_orders.process(b.bot, did)
        assert fake.calls.count("pay") == 1  # still status 10: no second mark-paid request
        assert (await get_job(did)).paid_attempted_at == original
        assert (await get_job(did)).stage == "review"
    go(run)


def test_parallel_bybit_completion_once_on_postgres(go, monkeypatch):
    async def run(b):
        if models.engine.dialect.name != "postgresql":
            pytest.skip("requires PostgreSQL row locks")
        did, fake = await seed(b, monkeypatch)
        await bybit_orders.process(b.bot, did)
        await pay(did)
        await bybit_orders.process(b.bot, did)
        fake.detail["status"] = 50
        await asyncio.gather(bybit_orders.process(b.bot, did), bybit_orders.process(b.bot, did))
        async with models.Session() as s:
            assert (await s.get(BybitOrder, did)).stage == "completed"
            assert (await s.get(User, 8)).balance == D(94)
            assert (await s.get(BybitAccount, "first")).acquired_usdt == D(100)
            assert await s.scalar(select(func.count(Ledger.id)).where(Ledger.kind == "deal_buy")) == 1
    go(run)
