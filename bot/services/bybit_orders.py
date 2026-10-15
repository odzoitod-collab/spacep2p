"""Durable Bybit workflow. The missing advertisement-entry capability is explicitly gated.

Bybit acts as the existing operator; the established merchant collateral/commission ledger is unchanged.
One account may service one order at a time. Pausing admission never stops monitoring existing orders.
"""
import asyncio
from datetime import timedelta
from io import BytesIO

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import config
from bot.models import BybitAccount, BybitAction, BybitOrder, Deal, Session, User, now
from bot.services import audit, deals, events, money, orders
from bot.services.bybit import (BybitClient, BybitError, advertisement_id, extract_requisites,
                                gross_quantity, message_content, validate_order)

TERMINAL = ("completed", "cancelled")
clients: dict[str, BybitClient] = {}
_cycle_lock = asyncio.Lock()


def record(s: AsyncSession, job: BybitOrder, action: str, result: str = "ok", code: str | None = None,
           **details):
    s.add(BybitAction(deal_id=job.deal_id, account_name=job.account_name, order_id=job.order_id,
                      action=action, result=result, code=code, details=details))
    job.updated_at = now()
    if result == "error":
        job.error_code = code
    events.add(s, f"deal:{job.deal_id}", "bybit_" + action[:26],
               f"Bybit · аккаунт {job.account_name or 'не назначен'} · ордер {job.order_id or 'не открыт'} · "
               f"{action}: {result}" + (f" ({code})" if code else ""), alert=result == "error",
               notice=result != "error")


async def setup():
    await close()
    if not config.bybit_enabled:
        return
    async with Session() as s:
        for credential in config.bybit_accounts:
            account = await s.get(BybitAccount, credential.name)
            if account is None:
                s.add(BybitAccount(name=credential.name, uid=credential.uid))
            elif account.uid != credential.uid:
                raise ValueError("Bybit UID of a registered account cannot be changed")
            clients[credential.name] = BybitClient(credential, config.bybit_testnet)
        await s.commit()


async def close():
    for client in clients.values():
        await client.close()
    clients.clear()


async def reserve_account(s: AsyncSession) -> BybitAccount | None:
    """Round-robin admission, under row locks. The caller reserves by assigning a job before commit."""
    names = list(clients)
    available = select(BybitAccount).where(
        BybitAccount.enabled, BybitAccount.name.in_(names),
        (BybitAccount.paused_until.is_(None) | (BybitAccount.paused_until <= now())),
        ~exists().where(BybitOrder.account_name == BybitAccount.name, ~BybitOrder.stage.in_(TERMINAL)),
    ).order_by(BybitAccount.last_used_at.asc().nullsfirst(), BybitAccount.name).with_for_update(skip_locked=True)
    account = await s.scalar(available.limit(1))
    if account:
        account.last_used_at = now()
    return account


async def submit_link(s: AsyncSession, deal_id: int, merchant: User, link: str) -> BybitOrder:
    if not config.bybit_enabled:
        raise deals.DealError("Подключение Bybit выключено")
    ad_id = advertisement_id(link)
    d = await s.get(Deal, deal_id, with_for_update=True, populate_existing=True)
    if not d or not d.is_order or d.status != "assigned" or d.seller_id != merchant.id:
        raise deals.DealError("Заявка уже не у вас")
    if deals.aware(d.expires_at) <= now():
        raise deals.DealError("Время на выдачу реквизитов вышло")
    job = await s.get(BybitOrder, deal_id)
    if job:
        raise deals.DealError("Ссылка Bybit для этой заявки уже принята")
    job = BybitOrder(deal_id=deal_id, ad_id=ad_id, expected_quantity=gross_quantity(d))
    s.add(job)
    await s.flush()
    record(s, job, "link_received", ad_id=ad_id, amount_rub=str(d.amount_rub),
           gross_usdt=str(job.expected_quantity), buyer_usdt=str(d.buyer_credit))
    # Do not guess an undocumented buy endpoint. No account or external order is consumed here.
    record(s, job, "entry_unavailable", "error", "entry_not_verified")
    return job


async def bind_existing(s: AsyncSession, deal_id: int, account_name: str, order_id: str, actor_id: int):
    """Explicit admin diagnostic operation until advertisement entry has a verified adapter.

    Read-only exchange call; strict identity/amount checks before binding. Never settle an old unrelated order.
    """
    if actor_id not in config.admin_ids or account_name not in clients:
        raise deals.DealError("Действие недоступно")
    d = await s.get(Deal, deal_id, with_for_update=True, populate_existing=True)
    job = await s.get(BybitOrder, deal_id, with_for_update=True)
    if not d or d.status != "assigned" or not job or job.stage != "awaiting_entry" or job.order_id:
        raise deals.DealError("Заявка уже изменилась")
    account = await s.get(BybitAccount, account_name, with_for_update=True)
    if not account or not account.enabled or (account.paused_until and deals.aware(account.paused_until) > now()):
        raise deals.DealError("Аккаунт недоступен для новых ордеров")
    busy = await s.scalar(select(BybitOrder.deal_id).where(BybitOrder.account_name == account_name,
                                                        ~BybitOrder.stage.in_(TERMINAL)))
    if busy is not None:
        raise deals.DealError("На аккаунте уже есть активный ордер")
    if await s.scalar(select(BybitOrder.deal_id).where(BybitOrder.order_id == order_id)) is not None:
        raise deals.DealError("Ордер уже привязан")
    detail = await clients[account_name].order(order_id)
    validate_order(detail, d, job.ad_id, account.uid, order_id)
    if int(detail["status"]) != 10 or int(detail["createDate"]) < int(deals.aware(job.created_at).timestamp() * 1000) - 60000:
        raise BybitError("order_not_new_unpaid")
    # Another old/manual order with the same counterparty would contaminate the session history.
    pending = await clients[account_name].request("/v5/p2p/order/pending/simplifyList", {"page": 1, "size": 30})
    if int(pending.get("count", 0)) > 30:
        raise BybitError("too_many_pending_orders")
    if any(str(row.get("id")) != order_id for row in pending.get("items", [])):
        raise BybitError("account_has_other_orders")
    job.account_name, job.order_id, job.stage = account_name, order_id, "requisites"
    account.last_used_at = now()
    record(s, job, "order_bound", actor_id=actor_id)
    audit.log(s, actor_id, "bybit_bind", str(deal_id), f"{account_name} / {order_id}")


async def held(s: AsyncSession, deal_id: int) -> bool:
    job = await s.get(BybitOrder, deal_id)
    return bool(job and job.order_id and job.stage not in TERMINAL)


async def review(s: AsyncSession, job: BybitOrder, code: str):
    if job.stage != "review" or job.error_code != code:
        if job.stage != "review":
            job.resume_stage = job.stage
        job.stage = "review"
        record(s, job, "review", "error", code)


async def resume(s: AsyncSession, deal_id: int, actor_id: int):
    if actor_id not in config.admin_ids:
        raise deals.DealError("Нет доступа")
    await s.get(Deal, deal_id, with_for_update=True)
    job = await s.get(BybitOrder, deal_id, with_for_update=True)
    if not job or job.stage != "review" or job.resume_stage not in ("requisites", "receipt", "waiting_release"):
        raise deals.DealError("Нет этапа, который можно продолжить")
    # All exchange identity, amount and mutation markers are checked again by the worker.
    # Resume NEVER clears send_attempted_at / paid_attempted_at or overrides received amounts.
    job.stage, job.resume_stage, job.error_code = job.resume_stage, None, None
    record(s, job, "resume", actor_id=actor_id)
    audit.log(s, actor_id, "bybit_resume", str(deal_id), "Markers preserved")


async def receipt_attached(s: AsyncSession, d: Deal, reused: bool = False, late: bool = False):
    job = await s.get(BybitOrder, d.id)
    if not job or not job.order_id:
        return
    if reused or late:
        await review(s, job, "receipt_reused" if reused else "late_receipt")
    elif job.stage not in ("requisites", "receipt"):
        await review(s, job, "unexpected_receipt")
    else:
        job.stage = "receipt"
        record(s, job, "receipt_accepted")


async def process(bot, deal_id: int):
    from bot.handlers.deal import push

    async with Session() as s:
        d = await s.get(Deal, deal_id, with_for_update=True)
        job = await s.get(BybitOrder, deal_id, with_for_update=True)
        if not d or not job or job.stage in TERMINAL:
            return
        if not job.order_id:
            if d.status != "assigned":
                job.stage = "cancelled"
                record(s, job, "entry_abandoned")
                await s.commit()
            return
        client = clients.get(job.account_name)
        if not client:
            await review(s, job, "credentials_missing")
            await s.commit()
            return
        try:
            detail = await client.order(job.order_id)
            account = await s.get(BybitAccount, job.account_name)
            quantity = validate_order(detail, d, job.ad_id, account.uid, job.order_id)
            status = int(detail["status"])
            job.updated_at = now()
            s.add(BybitAction(deal_id=job.deal_id, account_name=job.account_name, order_id=job.order_id,
                              action="order_checked", result="ok", details={"status": status}))
            if status in (40, 80):
                if d.receipt_file_id or job.paid_attempted_at:
                    await review(s, job, "cancelled_after_receipt")
                else:
                    job.stage = "cancelled"
                    if d.status == "assigned":
                        await orders.cancel(s, d.id)
                    elif d.status == "waiting_payment":
                        await deals.cancel(s, d.id, ("waiting_payment",))
                    elif d.status == "expired" and d.funds_held:
                        await deals.release_hold(s, d.id)
                    record(s, job, "cancel_verified", reason="exchange_cancelled")
                await s.commit()
                return
            if job.stage == "review":
                # Continue status checks, but never send or settle until reviewed.
                await s.commit()
                return
            if status == 50:
                if not job.receipt_sent or not job.paid_attempted_at or d.status != "paid":
                    await review(s, job, "completion_without_payment_proof")
                else:
                    job.stage = "settling"
                    done = await deals.complete(s, d.id, frm=("paid",), bybit_verified=True)
                    if not done:
                        await review(s, job, "local_status_mismatch")
                    else:
                        job.stage, job.received_quantity = "completed", quantity
                        account.acquired_usdt += quantity
                        record(s, job, "settled", received_usdt=str(quantity), buyer_usdt=str(d.buyer_credit),
                               platform_usdt=str(d.platform_fee))
                await s.commit()
                if job.stage == "completed":
                    await push(bot, s, d.buyer_id, d,
                               f"Сделка #{d.id} завершена — {money.usdt(d.buyer_credit)} USDT на балансе")
                    await push(bot, s, d.seller_id, d, f"Bybit подтвердил завершение сделки #{d.id}")
                    await s.commit()
                return
            if status not in (10, 20):
                await review(s, job, f"exchange_status_{status}")
                await s.commit()
                return
            if d.status not in ("assigned", "waiting_payment", "paid"):
                await review(s, job, "local_status_mismatch")
                await s.commit()
                return
            if not job.session_id:
                job.session_id = await client.session(detail["targetUserMaskId"])
                record(s, job, "chat_connected")
                await s.commit()
            if job.stage == "requisites":
                if d.status != "assigned" or status != 10:
                    raise BybitError("requisites_status_mismatch")
                rows = await client.messages(job.session_id, int(detail["createDate"]))
                s.add(BybitAction(deal_id=job.deal_id, account_name=job.account_name, order_id=job.order_id,
                                  action="chat_read", result="ok", details={"message_count": len(rows)}))
                requisites = extract_requisites(detail, rows)
                if not requisites:
                    if deals.aware(d.expires_at) <= now():
                        await review(s, job, "requisites_timeout")
                        await s.commit()
                    else:
                        await s.commit()
                    return
                remaining = int(detail.get("transferLastSeconds", "0"))
                # Do not give an expired or very short-lived set of payment details to the buyer.
                if remaining < 120 or deals.aware(d.expires_at) <= now():
                    raise BybitError("payment_window_too_short")
                merchant = await s.get(User, d.seller_id)
                await orders.give_requisites(s, d.id, merchant, requisites.kind, requisites.bank,
                                            requisites.number, requisites.holder, orders.pay_choices()[0],
                                            bybit_verified=True)
                d.expires_at = min(deals.aware(d.expires_at), now() + timedelta(seconds=remaining - 30))
                job.payment_id, job.payment_type = requisites.payment_id, requisites.payment_type
                job.stage = "receipt"
                record(s, job, "requisites_given", kind=requisites.kind, bank=requisites.bank,
                       number_tail=requisites.number[-4:])
                await s.commit()
                await push(bot, s, d.buyer_id, d, "Реквизиты получены. Переведите точную сумму и прикрепите PDF-чек")
                await s.commit()
                return
            if job.stage == "receipt" and d.status == "paid":
                if not job.payment_id or not job.payment_type or not d.receipt_file_id:
                    raise BybitError("missing_payment_data")
                if not job.receipt_url:
                    record(s, job, "upload_started", "started")
                    await s.commit()
                    file = await bot.download(d.receipt_file_id, destination=BytesIO())
                    job.receipt_url = await client.upload_pdf(file.getvalue(), f"receipt-{d.id}.pdf")
                    record(s, job, "receipt_uploaded")
                    await s.commit()
                if not job.receipt_sent:
                    if job.send_attempted_at:
                        rows = await client.messages(job.session_id, int(detail["createDate"]))
                        s.add(BybitAction(deal_id=job.deal_id, account_name=job.account_name, order_id=job.order_id,
                                          action="send_reconciled", result="ok", details={"message_count": len(rows)}))
                        # Both sender and the precise uploaded file must match; never use seller PDFs.
                        found = any(row.get("sendUserNickName") == detail.get("nickName")
                                    and row.get("contentType", "").lower() == "pdf"
                                    and message_content(row) == job.receipt_url for row in rows)
                        if not found:
                            raise BybitError("receipt_send_unknown")
                    else:
                        job.send_attempted_at = now()
                        record(s, job, "send_started", "started")
                        await s.commit()
                        await client.send_pdf(job.session_id, job.order_id, job.receipt_url)
                    job.receipt_sent = True
                    record(s, job, "receipt_sent")
                    await s.commit()
                if status == 10:
                    if job.paid_attempted_at:
                        # No blind replay after a timeout or restart between send and acknowledgment.
                        raise BybitError("mark_paid_unknown")
                    job.paid_attempted_at = now()
                    record(s, job, "pay_started", "started")
                    await s.commit()
                    await client.mark_paid(job.order_id, job.payment_id, job.payment_type)
                elif not job.paid_attempted_at:
                    raise BybitError("payment_marked_outside_bot")
                job.stage = "waiting_release"
                record(s, job, "payment_marked")
                await s.commit()
            elif job.stage == "waiting_release" and status == 10:
                raise BybitError("payment_status_regressed")
            await s.commit()
        except BybitError as error:
            # Error text/raw replies may contain chat/payment data; only a bounded code is recorded.
            s.add(BybitAction(deal_id=job.deal_id, account_name=job.account_name, order_id=job.order_id,
                              action="api_error", result="error", code=error.code,
                              details={"uncertain": error.uncertain}))
            await review(s, job, error.code)
            if error.code in ("10003", "10004", "10005", "http_401", "http_403"):
                account = await s.get(BybitAccount, job.account_name)
                if account:
                    account.enabled = False
            if error.code in ("10006", "http_429"):
                account = await s.get(BybitAccount, job.account_name)
                if account:
                    account.paused_until = now() + timedelta(minutes=1)
            await s.commit()


async def cycle(bot):
    if not config.bybit_enabled:
        return
    async with _cycle_lock:
        async with Session() as s:
            ids = list((await s.scalars(select(BybitOrder.deal_id).where(
                ~BybitOrder.stage.in_(TERMINAL)).order_by(BybitOrder.order_id.is_(None),
                                                        BybitOrder.updated_at, BybitOrder.deal_id).limit(50))).all())
        for deal_id in ids:
            try:
                await process(bot, deal_id)
            except Exception as error:
                # Roll back failed work; durably alert without exception text that may contain secrets.
                async with Session() as s:
                    job = await s.get(BybitOrder, deal_id)
                    if job:
                        await review(s, job, "worker_" + type(error).__name__[:40])
                        await s.commit()
