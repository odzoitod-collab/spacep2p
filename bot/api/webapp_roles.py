"""Mini app: the working roles — order merchant, operator, admin — and what a deal needs around them: taking a
request, the Bybit link, disputes with evidence and files, the admin's verdict, the operator's score of the merchant;
plus the user's avatar and the page's own error log. Every action is the bot's own function (handlers.orders, deal,
admin), so the bot and the app follow one set of rules and tell the same people the same things."""
import asyncio
import base64
import logging
import time
from datetime import timedelta
from decimal import Decimal

from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile
from aiohttp import web
from sqlalchemy import func, select

from bot.api.webapp import AppError, body, ctx, deal_json, deal_of, deal_response, iso, num, own_purchase
from bot.models import (Adjustment, Deal, Ledger, MerchantRating, Operator, OrderMerchant, Signup, Ticket,
                        User, Withdrawal, now)
from bot.services import admins, bsc, deals, money, operators, orders, settings

log = logging.getLogger(__name__)
UPLOAD_MB = 10  # one evidence file from the app (bigger videos: through the bot)
MAX_FILES = 5  # files in one upload


# ---------- what a deal screen needs besides deal_json ----------

async def enrich(s, d: Deal, uid: int, who: dict, out: dict) -> None:
    """Role-specific parts of a deal for the app: how to take a request, how long to give for payment, the
    operator's pending score, the parties and verdicts for an admin, the files of a dispute."""
    acts = out.get("actions", [])
    if "take" in acts:
        out["role"] = "offer"  # a free request as a merchant sees it
        m = await s.get(OrderMerchant, uid)
        u = await s.get(User, uid)
        rep, _ = await orders.reputation(s, uid)
        out["take"] = {
            "bybit_problem": orders.fit_problem(m, u, d, True) or orders.bybit_problem(rep, d),
            "balance_problem": orders.fit_problem(m, u, d, False),
            "debit": num(d.seller_debit), "balance": num(u.balance), "rate": num(d.merchant_rate),
            "link_minutes": settings.num("order_link_minutes"), "take_minutes": settings.num("order_take_minutes")}
    if "give" in acts:
        from bot.handlers.orders import _default_minutes
        held = d.via_bybit and d.status == "checking"
        out["give"] = {"choices": [] if held else orders.pay_choices(),
                       "default": None if held else await _default_minutes(s, (await s.get(User, uid)))}
    if d.operator_id == uid:
        rating = await s.scalar(select(MerchantRating).where(
            MerchantRating.deal_id == d.id, MerchantRating.operator_id == uid, MerchantRating.score.is_(None)))
        out["rating_id"] = rating.id if rating else None
    if d.receipt_file_id and "receipt_view" in acts:
        out["receipt_kind"] = "photo" if d.receipt_file_id.startswith("photo:") else "pdf"
    if who.get("admin"):
        from bot.handlers.admin import VERDICTS, verdict_allowed
        from bot.handlers.deal import verdict_effects
        parties = []
        for role, pid in (("Покупатель", d.buyer_id), ("Мерчант", d.seller_id), ("Оператор", d.operator_id)):
            if pid:
                p = await s.get(User, pid)
                parties.append({"role": role, "id": pid, "name": p.name if p else "", "username": p.username if p else None})
        out["parties"] = parties
        out["verdicts"] = [{"code": v, "title": VERDICTS[v], "effects": verdict_effects(d, v)}
                           for v in "bsacn" if verdict_allowed(d, v)]
        out["files"] = files_of(d)


def files_of(d: Deal) -> list[dict]:
    """The receipt (n = "r") and every piece of dispute evidence (n = its index): kind, side, text."""
    out = []
    if d.receipt_file_id:
        out.append({"n": "r", "kind": "photo" if d.receipt_file_id.startswith("photo:") else "document",
                    "role": "buyer", "title": "Чек покупателя"})
    for i, f in enumerate(d.dispute_files or []):
        kind, value = f[0], f[1]
        role = f[2] if len(f) > 2 else "seller"
        out.append({"n": i, "kind": kind, "role": role, "text": value if kind == "text" else None,
                    "title": {"video": "Видео", "photo": "Фото", "document": "Файл", "text": "Пояснение"}.get(kind, kind)
                    + (" покупателя" if role == "buyer" else " продавца")})
    return out


# ---------- order merchant: take a request, its Bybit link, give it up ----------

async def merchant(request: web.Request) -> web.Response:
    """The order merchant's cabinet: status, terms, reputation, requests in work and free ones."""
    s, user, _ = ctx(request)
    m = await s.get(OrderMerchant, user.id)
    terms = {"rate": num(settings.dec("order_rate")), "link_minutes": settings.num("order_link_minutes"),
             "take_minutes": settings.num("order_take_minutes"), "strike_limit": settings.num("strike_limit")}
    if m is None or m.status in ("pending", "rejected"):
        return web.json_response({"status": m.status if m else None, "reason": m.reason if m else None,
                                  "terms": terms, "offers": [], "working": []})
    rep, rated = await orders.reputation(s, user.id)
    stats = {}
    for key, since in (("today", deals.day_start()), ("week", now() - timedelta(days=7)), ("all", None)):
        st = await deals.seller_stats(s, user.id, since, order=True)
        stats[key] = {"n": st["n"], "rub": num(st["rub"]), "income": num(st["income"].quantize(money.Q)),
                      "success": st["success"]}
    working = (await s.scalars(select(Deal).where(Deal.seller_id == user.id, Deal.is_order,
                                                  Deal.status.in_(deals.FUNDED)).order_by(Deal.id))).all()
    active = m.status == "approved" and not orders.asleep(m) and not m.offline
    free = (await s.scalars(select(Deal).where(Deal.status == "searching", Deal.buyer_id != user.id,
                                               Deal.expires_at >= now()).order_by(Deal.id).limit(30))).all() \
        if active else []
    return web.json_response({
        "status": m.status, "asleep": orders.asleep(m), "sleep_until": iso(m.sleep_until), "strikes": m.strikes,
        "online": not m.offline,
        "pay_minutes": m.pay_minutes, "pay_choices": orders.pay_choices(), "terms": terms,
        "reputation": {"score": num(rep.quantize(Decimal("0.1"))) if rep is not None else None, "rated": rated,
                       "line": orders.rep_line(rep, rated)},
        "balance": num(user.balance), "cover_rub": num(money.max_rub_fixed(user.balance, settings.dec("order_rate"))),
        "stats": stats,
        "working": [deal_json(d, None, user.id, full=False) | {"via_bybit": d.via_bybit} for d in working],
        "offers": [deal_json(d, None, user.id, full=False) | {"debit": num(d.seller_debit),
                                                               "expires_at": iso(d.expires_at),
                                                               "sender_bank": d.sender_bank} for d in free],
    })


async def merchant_settings(request: web.Request) -> web.Response:
    s, user, _ = ctx(request)
    m = await s.get(OrderMerchant, user.id)
    data = await body(request)
    if m is None or m.status != "approved":
        raise AppError(403, "not_merchant", "Только для ордерных мерчантов")
    if "online" in data:  # leaves the line or comes back: requests stop / start reaching him
        if not isinstance(data["online"], bool):
            raise AppError(422, "bad_request", "online: true или false")
        m.offline = not data["online"]
        return await merchant(request)
    minutes = data.get("pay_minutes")
    if not isinstance(minutes, int) or minutes not in orders.pay_choices():
        raise AppError(422, "bad_minutes", "Выберите время из списка")
    m.pay_minutes = minutes
    return await merchant(request)


async def take(request: web.Request) -> web.Response:
    from bot.handlers.orders import take_request
    s, user, bot = ctx(request)
    d = await deal_of_request(request)
    mode = (await body(request)).get("mode")
    if mode not in ("bybit", "balance"):
        raise AppError(422, "bad_mode", "Выберите: Bybit-ордер или с баланса")
    try:
        d = await take_request(bot, s, user, d.id, mode == "bybit")
    except deals.DealError as e:
        raise AppError(409, e.code or "deal_error", str(e))
    return await deal_response(s, d, user.id)


async def link(request: web.Request) -> web.Response:
    from bot.handlers.orders import send_link
    s, user, bot = ctx(request)
    d = await deal_of(request)
    url = orders.bybit_link((await body(request)).get("url") or "")
    if not url:
        raise AppError(422, "bad_link", "Нужна ссылка на ордер Bybit: https://www.bybit.com/… — скопируйте её в "
                                        "приложении Bybit")
    if not (d.status == "assigned" and d.seller_id == user.id and d.via_bybit):
        raise AppError(409, "not_yours", "Заявка уже не у вас — обновите сделку")
    try:
        d = await send_link(bot, s, user, d.id, url)
    except deals.DealError as e:
        raise AppError(409, e.code or "deal_error", str(e))
    return await deal_response(s, d, user.id)


async def drop(request: web.Request) -> web.Response:
    from bot.handlers.orders import drop_request
    s, user, bot = ctx(request)
    d = await deal_of(request)
    d = await s.get(Deal, d.id, populate_existing=True)
    if not (d.status == "assigned" and d.seller_id == user.id):
        raise AppError(409, "not_yours", "Заявка уже не у вас — обновите сделку")
    d = await drop_request(bot, s, user, d)
    return await deal_response(s, d, user.id)


async def deal_of_request(request: web.Request) -> Deal:
    """A request a merchant may look at to take it: any searching request (deal_of shows only one's own deals)."""
    s, user, _ = ctx(request)
    d = await s.get(Deal, int(request.match_info["id"]), populate_existing=True)
    if d is None or not d.is_order or d.status != "searching" or d.buyer_id == user.id:
        raise AppError(409, "taken", "Заявку уже взяли или она закрыта")
    return d


async def request_view(request: web.Request) -> web.Response:
    """A free request as a merchant sees it before taking it."""
    s, user, _ = ctx(request)
    try:
        d = await deal_of_request(request)
    except AppError:  # taken meanwhile: the merchant who took it (or anyone who may) gets the deal itself
        d = await s.get(Deal, int(request.match_info["id"]))
        if d is None or d.status == "searching":
            raise
        d = await deal_of(request)
    return await deal_response(s, d, user.id)


# ---------- disputes and evidence ----------

def file_kind(data: bytes) -> str | None:
    if data[:1024].find(b"%PDF-") >= 0:
        return "document"
    if data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n":
        return "photo"
    if data[4:8] == b"ftyp" or data[:4] == b"\x1aE\xdf\xa3":
        return "video"
    return None


async def uploads(request: web.Request) -> tuple[dict, list[tuple[bytes, str]]]:
    """Form fields and files of a multipart request."""
    if request.content_type == "application/x-www-form-urlencoded":
        return {k: str(v)[:1000] for k, v in (await request.post()).items()}, []
    if not request.content_type.startswith("multipart/"):
        return await body(request), []
    fields, files = {}, []
    async for part in await request.multipart():
        if part.name == "file":
            if len(files) >= MAX_FILES:
                raise AppError(413, "too_many", f"Не более {MAX_FILES} файлов за раз")
            data = await part.read(decode=False)
            if len(data) > UPLOAD_MB * 1024 * 1024:
                raise AppError(413, "too_large", f"Файл больше {UPLOAD_MB} МБ — отправьте его в боте")
            if data:
                files.append((data, (part.filename or "file")[:80]))
        elif part.name:
            fields[part.name] = (await part.text())[:1000]
    return fields, files


async def to_telegram(bot, uid: int, did: int, data: bytes, name: str) -> list:
    """A file from the app becomes an evidence item: sent into the user's own chat with the bot, its file id kept."""
    kind = file_kind(data)
    if kind is None:
        raise AppError(422, "bad_file", "Нужно видео, фото (JPG, PNG) или PDF")
    caption = f"Доказательство по сделке #{did} — из приложения"
    try:
        if kind == "photo":
            m = await bot.send_photo(uid, BufferedInputFile(data, "proof.jpg"), caption=caption)
            return ["photo", m.photo[-1].file_id]
        if kind == "video":
            m = await bot.send_video(uid, BufferedInputFile(data, name if "." in name else "proof.mp4"), caption=caption)
            return ["video", (m.video or m.document).file_id]
        m = await bot.send_document(uid, BufferedInputFile(data, name if name.lower().endswith(".pdf") else "proof.pdf"),
                                    caption=caption)
        return ["document", m.document.file_id]
    except TelegramAPIError:
        raise AppError(409, "chat_closed", "Откройте чат с ботом и нажмите «Запустить» — файлы хранятся через него")


async def dispute(request: web.Request) -> web.Response:
    """Open a dispute from the app. The seller (or the operator of a Bybit order): reason not_received |
    wrong_amount (+ amount), with proof files; the operator's proof settles it at once. The buyer: when the seller
    is silent too long, with his evidence."""
    from bot.handlers.deal import REASONS, log as deal_log, push, settle_by_operator
    s, user, bot = ctx(request)
    d = await deal_of(request)
    buyer = own_purchase(d, user.id)
    if buyer and not ((at := deals.buyer_dispute_at(d)) and now() >= at):
        raise AppError(409, "not_allowed", "Спор пока недоступен — обновите сделку")
    if not buyer and (deals.checker(d) != user.id or d.status != "paid"):
        raise AppError(409, "not_allowed", "Спор недоступен: статус сделки изменился")
    fields, files = await uploads(request)
    reason = fields.get("reason")
    if not buyer and reason not in ("not_received", "wrong_amount"):
        raise AppError(422, "bad_reason", "Выберите: деньги не пришли или пришла другая сумма")
    if not buyer and not files:
        raise AppError(422, "proof_required", "Приложите доказательство: видео из банка или выписку")
    text = (fields.get("text") or "").strip()
    items = [await to_telegram(bot, user.id, d.id, data, name) for data, name in files]
    if text:
        items.append(["text", text[:1000]])
    if buyer:
        res = await deals.buyer_dispute(s, d.id, user.id)
        if res is None:
            raise AppError(409, "not_allowed", "Спор пока недоступен — обновите сделку")
        for item in items:
            res = await deals.add_evidence(s, res.id, user.id, item)
        deal_log(s, res, "dispute", f"Покупатель открыл спор (приложение): продавец не подтверждает, "
                 f"{money.fmt(res.amount_rub)} ₽, доказательств {len(items)}", alert=True)
        await s.commit()
        for uid in deals.sellers(res):
            await push(bot, s, uid, res, f"Покупатель открыл спор по сделке #{res.id}")
        return await deal_response(s, res, user.id)
    amount = None
    if reason == "wrong_amount":
        from bot.api.webapp import parse_amount
        amount = parse_amount(fields.get("amount"))
        if amount is None:
            raise AppError(422, "bad_amount", "Укажите сумму, которая фактически пришла")
    res = await deals.open_dispute(s, d.id, user.id, reason, [[*i, "seller"] for i in items], amount)
    if res is None:
        raise AppError(409, "not_allowed", "Статус сделки уже изменился, спор не открыт")
    deal_log(s, res, "dispute", f"Спор{' оператора' if res.via_bybit else ''} (приложение): {REASONS[reason]}"
             + (f", пришло {money.fmt(amount)} ₽" if amount else ""), alert=True)
    await s.commit()
    if res.via_bybit:
        settled, _ = await settle_by_operator(bot, s, user, res)
        return await deal_response(s, settled or await s.get(Deal, d.id, populate_existing=True), user.id)
    await push(bot, s, res.buyer_id, res, f"Продавец открыл спор по сделке #{res.id}. Добавьте доказательства оплаты")
    return await deal_response(s, res, user.id)


async def evidence(request: web.Request) -> web.Response:
    from bot.handlers.deal import log as deal_log
    s, user, bot = ctx(request)
    d = await deal_of(request)
    if d.status != "dispute":
        raise AppError(409, "closed", "Спор уже закрыт")
    fields, files = await uploads(request)
    items = [await to_telegram(bot, user.id, d.id, data, name) for data, name in files]
    if (fields.get("text") or "").strip():
        items.append(["text", fields["text"].strip()[:1000]])
    if not items:
        raise AppError(422, "empty", "Приложите файл или напишите пояснение")
    try:
        for item in items:
            d = await deals.add_evidence(s, d.id, user.id, item)
    except deals.DealError as e:
        raise AppError(409, "limit", str(e))
    role = "покупатель" if user.id == d.buyer_id else "оператор" if user.id == d.operator_id else "продавец"
    deal_log(s, d, "evidence", f"Новые доказательства ({role}, приложение): {', '.join(i[0] for i in items)}",
             notice=True)
    return await deal_response(s, d, user.id)


def _file_item(d: Deal, n: str) -> tuple[str, str]:
    """(kind, file id or text) of the receipt (n = "r") or a piece of evidence."""
    if n == "r":
        if not d.receipt_file_id:
            raise AppError(404, "not_found", "Файла нет")
        fid = d.receipt_file_id
        return ("photo", fid[6:]) if fid.startswith("photo:") else ("document", fid)
    files = d.dispute_files or []
    if not n.isdigit() or int(n) >= len(files):
        raise AppError(404, "not_found", "Файла нет")
    f = files[int(n)]
    return f[0], f[1]


async def may_see_file(s, d: Deal, uid: int, n: str) -> bool:
    if admins.is_admin(uid):
        return True
    if n == "r":
        return deals.checker(d) == uid or uid == d.buyer_id
    f = (d.dispute_files or [])[int(n)] if n.isdigit() and int(n) < len(d.dispute_files or []) else None
    role = (f[2] if f and len(f) > 2 else "seller") if f else None
    return (role == "buyer" and uid == d.buyer_id) or (role == "seller" and uid in (d.seller_id, d.operator_id))


async def file_get(request: web.Request) -> web.Response:
    """A photo, a video or a PDF of a deal, streamed from Telegram for the page (other documents go to the chat)."""
    s, user, bot = ctx(request)
    d = await deal_of(request)
    n = request.match_info["n"]
    if not await may_see_file(s, d, user.id, n):
        raise AppError(403, "forbidden", "Файл недоступен")
    kind, fid = _file_item(d, n)
    if kind not in ("photo", "video", "document"):
        raise AppError(415, "send", "Этот файл откроется в чате с ботом")
    try:
        buf = await bot.download(fid)
    except TelegramAPIError:
        raise AppError(502, "too_big", "Файл не загрузить в приложение — отправим его в чат")
    data = buf.read()
    if kind == "document":
        if b"%PDF-" not in data[:1024]:
            raise AppError(415, "send", "Этот файл откроется в чате с ботом")
        ctype = "application/pdf"
    else:
        ctype = "image/jpeg" if kind == "photo" else "video/mp4"
    return web.Response(body=data, content_type=ctype, headers={"Cache-Control": "private, max-age=3600"})


async def file_send(request: web.Request) -> web.Response:
    """Any file of a deal into the user's own chat with the bot (PDFs, big videos)."""
    from bot.handlers.deal import send_receipt
    s, user, bot = ctx(request)
    d = await deal_of(request)
    n = request.match_info["n"]
    if not await may_see_file(s, d, user.id, n):
        raise AppError(403, "forbidden", "Файл недоступен")
    kind, value = _file_item(d, n)
    caption = f"Сделка #{d.id}: " + ("чек покупателя" if n == "r" else "материал спора")
    try:
        if n == "r":
            await send_receipt(bot, user.id, d.receipt_file_id, caption)
        elif kind == "video":
            await bot.send_video(user.id, value, caption=caption)
        elif kind == "photo":
            await bot.send_photo(user.id, value, caption=caption)
        elif kind == "document":
            await bot.send_document(user.id, value, caption=caption)
        else:
            await bot.send_message(user.id, f"{caption}:\n{value}")
    except TelegramAPIError:
        raise AppError(409, "chat_closed", "Откройте чат с ботом и нажмите «Запустить»")
    return web.json_response({"ok": True})


# ---------- admin ----------

async def admin_only(request: web.Request) -> None:
    _, user, _ = ctx(request)
    if not admins.is_admin(user.id):
        raise AppError(403, "not_admin", "Только для администраторов")


async def resolve(request: web.Request) -> web.Response:
    from bot.handlers.admin import apply_verdict
    await admin_only(request)
    s, user, bot = ctx(request)
    d = await deal_of(request)
    data = await body(request)
    verdict = data.get("verdict")
    comment = " ".join(str(data.get("comment") or "").split())
    if comment and not 5 <= len(comment) <= 500:
        raise AppError(422, "bad_comment", "Комментарий — от 5 до 500 символов")
    res, error = await apply_verdict(bot, s, user, d.id, verdict if isinstance(verdict, str) else "", comment)
    if res is None:
        raise AppError(409, "not_allowed", error)
    return await deal_response(s, res, user.id)


async def admin_home(request: web.Request) -> web.Response:
    """The admin's desk: what waits for a decision, the money, the last day."""
    await admin_only(request)
    s, user, _ = ctx(request)
    day = now() - timedelta(hours=24)

    async def count(*where) -> int:
        return await s.scalar(select(func.count()).where(*where))
    disputes = (await s.scalars(select(Deal).where(Deal.status == "dispute").order_by(Deal.paid_at).limit(30))).all()
    slow_edge = now() - timedelta(minutes=settings.num("confirm_minutes"))
    slow = (await s.scalars(select(Deal).where(Deal.status == "paid", Deal.paid_at < slow_edge)
                            .order_by(Deal.paid_at).limit(20))).all()
    unknown = (await s.scalars(select(Withdrawal).where(Withdrawal.status.in_(("unknown", "pending")))
                               .order_by(Withdrawal.id).limit(20))).all()
    done24, volume24 = (await s.execute(select(func.count(Deal.id), func.coalesce(func.sum(Deal.amount_rub), 0))
                                        .where(Deal.status == "completed", Deal.closed_at > day))).one()
    income24 = await s.scalar(select(func.coalesce(func.sum(Ledger.delta), 0))
                              .where(Ledger.user_id.is_(None), Ledger.created_at > day))
    held = await s.scalar(select(func.coalesce(func.sum(User.balance + User.frozen + User.team_balance), 0)))
    hot, unswept = None, await bsc.unswept(s) if bsc.ready() else Decimal(0)
    if bsc.ready():
        try:
            d = await asyncio.wait_for(bsc.desk(s), 4)
            hot = {"usdt": num(d.usdt), "bnb": num(d.bnb.quantize(Decimal("0.0001"))), "address": bsc.hot,
                   "low": d.bnb < Decimal("0.003")}
        except Exception:  # noqa: BLE001 - shown as "no answer"
            hot = {"error": True}
    return web.json_response({
        "counts": {"disputes": len(disputes), "slow": len(slow), "unknown_wd": len(unknown),
                   "queued_wd": await count(Withdrawal.status == "queued", Withdrawal.method == "bsc"),
                   "free_orders": await count(Deal.status == "checking", Deal.operator_id.is_(None)),
                   "searching": await count(Deal.status == "searching"),
                   "open": await count(Deal.status.in_(deals.OPEN)),
                   "tickets": await count(Ticket.status == "open"), "signups": await count(Signup.status == "pending"),
                   "merchants": await count(OrderMerchant.status == "pending"),
                   "adjustments": await count(Adjustment.status == "pending")},
        "day": {"deals": done24, "rub": num(Decimal(volume24)), "income": num(Decimal(income24))},
        "money": {"users": num(Decimal(held)), "op_debt": num(await operators.total_debt(s)), "hot": hot,
                  "unswept": num(unswept)},
        "disputes": [deal_json(d, None, user.id, full=False) | {"paid_at": iso(d.paid_at)} for d in disputes],
        "slow": [deal_json(d, None, user.id, full=False) | {"paid_at": iso(d.paid_at)} for d in slow],
        "withdrawals": [{"id": w.id, "user_id": w.user_id, "amount": num(w.amount - w.fee), "address": w.address,
                         "status": w.status, "created_at": iso(w.created_at)} for w in unknown],
    })


async def admin_deals(request: web.Request) -> web.Response:
    """Every deal for an admin: by filter (dispute / paid / open / closed / all) or by number."""
    await admin_only(request)
    s, user, _ = ctx(request)
    q = select(Deal)
    f = request.query.get("filter", "open")
    if (num_q := request.query.get("q", "").strip().lstrip("#")).isdigit():
        q = q.where((Deal.id == int(num_q)) | (Deal.buyer_id == int(num_q)) | (Deal.seller_id == int(num_q)))
    elif f == "dispute":
        q = q.where(Deal.status == "dispute")
    elif f == "paid":
        q = q.where(Deal.status == "paid")
    elif f == "open":
        q = q.where(Deal.status.in_(deals.OPEN))
    elif f == "closed":
        q = q.where(Deal.status.not_in(deals.OPEN))
    if (before := request.query.get("before", "")).isdigit():
        q = q.where(Deal.id < int(before))
    rows = (await s.scalars(q.order_by(Deal.id.desc()).limit(30))).all()
    return web.json_response({"deals": [deal_json(d, None, user.id, full=False) for d in rows]})


# ---------- operator ----------

async def rate(request: web.Request) -> web.Response:
    from bot.handlers.orders import rate_merchant
    s, user, _ = ctx(request)
    score = (await body(request)).get("score")
    done, text = await rate_merchant(s, user, int(request.match_info["id"]), score if isinstance(score, int) else 0)
    if not done:
        raise AppError(409, "not_allowed", text)
    return web.json_response({"ok": True, "message": text})


async def repay(request: web.Request) -> web.Response:
    """The operator repays his debt from his balance — like «Погасить с баланса» in the bot."""
    s, user, _ = ctx(request)
    u = await money.lock(s, user.id)  # user row first, like deal completion does
    op = await s.get(Operator, user.id)
    if op is None:
        raise AppError(409, "no_debt", "Долга нет")
    op = await operators.row(s, user.id, lock=True)
    amount = min(op.debt, u.balance)
    if amount <= 0:
        raise AppError(409, "nothing", "Нечем гасить: нет долга или баланса")
    await money.add(s, user.id, -amount, "debt_repay", f"op:{user.id}")
    await operators.repay(s, user.id, amount, "с баланса (приложение)")
    return web.json_response({"ok": True, "repaid": num(amount), "debt": num(op.debt), "balance": num(u.balance)})


async def ratings_of(s, uid: int) -> list[dict]:
    rows = (await s.scalars(select(MerchantRating).where(MerchantRating.operator_id == uid,
                                                         MerchantRating.score.is_(None))
                            .order_by(MerchantRating.id.desc()).limit(10))).all()
    return [{"id": r.id, "deal_id": r.deal_id, "gave": r.gave} for r in rows]


# ---------- the person ----------

_avatars: dict[int, tuple[float, str | None]] = {}
AVATAR_TTL = 6 * 3600


async def avatar(request: web.Request) -> web.Response:
    """The user's Telegram profile photo as a data URL: initData has photo_url only for some users and clients."""
    _, user, bot = ctx(request)
    hit = _avatars.get(user.id)
    if hit and time.time() - hit[0] < AVATAR_TTL:
        return web.json_response({"url": hit[1]})
    data = None
    try:
        photos = await bot.get_user_profile_photos(user.id, limit=1)
        if photos.total_count and photos.photos:
            sizes = photos.photos[0]
            pick = min((p for p in sizes if p.width >= 160), key=lambda p: p.width, default=sizes[-1])
            buf = await bot.download(pick.file_id)
            data = "data:image/jpeg;base64," + base64.b64encode(buf.read()).decode()
    except (TelegramAPIError, AttributeError, TypeError):
        data = None
    if len(_avatars) > 5000:
        _avatars.clear()
    _avatars[user.id] = (time.time(), data)
    return web.json_response({"url": data})


_reported: dict[str, float] = {}


async def client_log(request: web.Request) -> web.Response:
    """An error of the page itself: into the bot's log (and its admin chat topic), once per 10 min per message."""
    _, user, _ = ctx(request)
    data = await body(request)
    message = str(data.get("message") or "")[:300]
    key = message[:120]
    if message and time.time() - _reported.get(key, 0) > 600:
        _reported[key] = time.time()
        log.error("mini app error: %s · %s · user %s · %s\n%s", message, str(data.get("path") or "")[:80], user.id,
                  str(data.get("ua") or "")[:160], str(data.get("stack") or "")[:1500])
    return web.json_response({"ok": True})


# ---------- a team: the leader's cabinet, a member's view ----------

async def team_view(request: web.Request) -> web.Response:
    from bot.ui import deep_link
    from bot.services import teams
    s, user, bot = ctx(request)
    team = await teams.of_user(s, user)
    if team is None:
        raise AppError(404, "no_team", "Вы не в команде")
    leader = team.leader_id == user.id
    out = {"id": team.id, "name": team.name, "status": team.status, "leader": leader,
           "pct": num(teams.pct(team)), "members": await teams.members(s, team)}
    if leader:
        out["link"] = await deep_link(bot, f"t{team.id}")
        out["balance"] = num(user.team_balance)
        out["chat"] = bool(team.chat_id)
        for key, since in (("today", deals.day_start()), ("week", now() - timedelta(days=7)), ("all", None)):
            n, rub, fee = await teams.stats(s, team, since)
            out[key] = {"n": n, "rub": num(rub), "income": num(fee.quantize(money.Q))}
        rows = (await s.scalars(select(User).where(User.team_id == team.id, User.id != team.leader_id)
                                .order_by(User.created_at.desc()).limit(50))).all()
        done = await deals.completed_count(s, [u.id for u in rows])
        out["list"] = [{"id": u.id, "name": u.name, "username": u.username, "deals": done[u.id],
                        "online": u.is_online, "since": iso(u.created_at)} for u in rows]
    else:
        lead = await s.get(User, team.leader_id)
        out["leader_name"] = lead.name if lead else None
    return web.json_response(out)


async def team_out(request: web.Request) -> web.Response:
    """The leader's team balance -> his main balance."""
    from bot.services import events, teams
    s, user, _ = ctx(request)
    team = await teams.led_by(s, user.id)
    if team is None:
        raise AppError(403, "not_leader", "Только для тимлида")
    u = await money.lock(s, user.id)
    amount = u.team_balance
    if amount <= 0:
        raise AppError(409, "empty", "Командный баланс пуст")
    await money.team_to_balance(s, u.id, amount)
    events.add(s, f"team:{team.id}", "team_out", f"Тимлид перевёл {money.usdt(amount)} USDT с командного баланса "
               "на основной (приложение)", u.id, notice=True)
    return web.json_response({"moved": num(amount), "balance": num(u.balance)})


# ---------- admin: the cash desk, finance, people, applications, withdrawals ----------

async def admin_desk(request: web.Request) -> web.Response:
    await admin_only(request)
    s, user, _ = ctx(request)
    if not bsc.ready():
        return web.json_response({"on": False, "error": bsc.error or "запускается"})
    queue = (await s.scalars(select(Withdrawal).where(Withdrawal.method == "bsc", Withdrawal.status.in_(
        ("queued", "sending", "sent"))).order_by(Withdrawal.id).limit(30))).all()
    out = {"on": True, "address": bsc.hot, "explorer": bsc.address_url(bsc.hot), "owner": admins.is_owner(user.id),
           "queue": [{"id": w.id, "user_id": w.user_id, "amount": num(w.amount - w.fee), "address": w.address,
                      "status": w.status, "signed": bool(w.transfer_id), "created_at": iso(w.created_at)}
                     for w in queue]}
    try:
        d = await asyncio.wait_for(bsc.desk(s), 8)
        out |= {"usdt": num(d.usdt), "bnb": num(d.bnb.quantize(Decimal("0.00001"))), "low": d.bnb < Decimal("0.003"),
                "cold": num(d.cold) if d.cold is not None else None, "unswept": num(d.unswept),
                "total": num(d.total), "queued": num(d.queued), "pending": d.pending}
    except Exception as e:  # noqa: BLE001 - the screen opens anyway
        out["chain_error"] = str(e)[:200]
    return web.json_response(out)


async def admin_desk_key(request: web.Request) -> web.Response:
    """The seed: owners only, to their private chat with the bot (never into the page), deleted after 2 minutes."""
    from bot.handlers.admin_bsc import KEY_TTL, send_key
    await admin_only(request)
    s, user, bot = ctx(request)
    if err := await send_key(bot, s, user.id):
        raise AppError(403 if "владельц" in err else 409, "key", err)
    return web.json_response({"sent": True, "minutes": KEY_TTL // 60})


async def admin_finance(request: web.Request) -> web.Response:
    from bot.services import finance
    await admin_only(request)
    s, _, _ = ctx(request)
    sn = await finance.snapshot(s)
    return web.json_response({
        "hot": num(sn.hot), "bnb": num(sn.bnb), "cold": num(sn.cold), "unswept": num(sn.unswept),
        "assets": num(sn.assets), "users": num(sn.users_available), "frozen": num(sn.users_frozen),
        "team": num(sn.users_team), "unpaid": num(sn.unpaid), "unpaid_n": sn.unpaid_n,
        "liabilities": num(sn.liabilities), "free": num(sn.free), "op_debt": num(sn.op_debt),
        "profit": {k: num(v) for k, v in sn.profit.items()},
        "volume": {k: {"n": v[0], "rub": num(v[1])} for k, v in sn.volume.items()},
        "users_n": sn.users, "online": sn.online})


def _user_json(u: User) -> dict:
    return {"id": u.id, "name": u.name, "username": u.username, "balance": num(u.balance), "frozen": num(u.frozen),
            "banned": u.is_banned, "online": u.is_online, "since": iso(u.created_at), "seen": iso(u.last_seen)}


async def admin_users(request: web.Request) -> web.Response:
    """Search by ID, @username or a part of the name; empty — the latest who were active."""
    await admin_only(request)
    s, _, _ = ctx(request)
    q = (request.query.get("q") or "").strip().lstrip("@")
    stmt = select(User)
    if q.isdigit():
        stmt = stmt.where(User.id == int(q))
    elif q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(func.lower(User.username).like(like) | func.lower(User.name).like(like))
    rows = (await s.scalars(stmt.order_by(User.last_seen.desc()).limit(30))).all()
    return web.json_response({"users": [_user_json(u) for u in rows]})


async def admin_user(request: web.Request) -> web.Response:
    from bot.api.webapp import roles
    await admin_only(request)
    s, admin, _ = ctx(request)
    u = await s.get(User, int(request.match_info["id"]), populate_existing=True)
    if u is None:
        raise AppError(404, "not_found", "Пользователь не найден")
    op = await s.get(Operator, u.id)
    done = await s.scalar(select(func.count(Deal.id)).where((Deal.buyer_id == u.id) | (Deal.seller_id == u.id),
                                                            Deal.status == "completed"))
    opened = await s.scalar(select(func.count(Deal.id)).where((Deal.buyer_id == u.id) | (Deal.seller_id == u.id),
                                                              Deal.status.in_(deals.OPEN)))
    return web.json_response(_user_json(u) | {
        "roles": await roles(s, u), "admin": admins.is_admin(u.id), "team_balance": num(u.team_balance),
        "deposit_lock": num(u.deposit_lock), "debt": num(op.debt) if op and op.debt else None,
        "rating": num(u.rating), "rating_lines": await orders.rep_text(s, u.id),
        "deals": {"done": done, "open": opened}, "owner": admins.is_owner(admin.id)})


async def admin_user_act(request: web.Request) -> web.Response:
    """{"action": "ban" | "unban" | "rating" | "balance", "value": ...} — the bot's own rules: a granted admin's
    big or own adjustment waits for a second admin, an owner's goes through."""
    from bot.handlers.admin import set_ban
    from bot.handlers.admin_balance import _change, _notice
    from bot.handlers.admin_orders import set_rating
    from bot.ui import notify
    await admin_only(request)
    s, admin, bot = ctx(request)
    uid = int(request.match_info["id"])
    u = await s.get(User, uid)
    if u is None:
        raise AppError(404, "not_found", "Пользователь не найден")
    data = await body(request)
    action, value = data.get("action"), data.get("value")
    if action in ("ban", "unban"):
        target, result = await set_ban(bot, s, admin, uid, action == "ban")
        if target is None:
            raise AppError(409, "not_allowed", result)
        return web.json_response({"message": result})
    if action == "rating":
        if value in (None, "", "-"):
            rating = None
        else:
            try:
                rating = Decimal(str(value).replace(",", ".")).quantize(Decimal("0.1"))
            except ArithmeticError:
                rating = Decimal(-1)
            if not Decimal(1) <= rating <= Decimal(10):
                raise AppError(422, "bad_rating", "Рейтинг — число от 1 до 10")
        return web.json_response({"message": "Готово: " + await set_rating(bot, s, admin, u, rating)})
    if action == "balance":
        try:
            delta = Decimal(str(value).replace(",", ".").replace(" ", "").replace("−", "-")).quantize(money.Q)
        except ArithmeticError:
            raise AppError(422, "bad_amount", "Сумма: +10 или -5")
        if not delta or abs(delta) >= Decimal(10_000_000):
            raise AppError(422, "bad_amount", "Сумма: +10 или -5")
        comment = str(data.get("comment") or "")[:200]
        result, a = await _change(s, admin, uid, delta, comment)
        await s.commit()
        if result == "done":
            await notify(bot, uid, _notice(a))
            return web.json_response({"message": f"Проведено: {money.usdt(a.balance_before)} → "
                                                 f"{money.usdt(a.balance_after)} USDT"})
        if result == "pending":
            return web.json_response({"message": "Ждёт подтверждения второго администратора (в боте)"})
        raise AppError(409, "failed", "Не хватает доступного баланса — ничего не списано")
    raise AppError(422, "bad_action", "Неизвестное действие")


async def admin_signups(request: web.Request) -> web.Response:
    from bot.handlers.signup import ROLES
    await admin_only(request)
    s, _, _ = ctx(request)
    rows = (await s.execute(select(Signup, User).join(User, User.id == Signup.user_id).where(
        Signup.status == "pending").order_by(Signup.id).limit(30))).all()
    return web.json_response({"signups": [{
        "id": su.id, "user_id": u.id, "name": u.name, "username": u.username, "role": ROLES.get(su.role, su.role),
        "turnover": su.turnover, "proof": bool(su.proof), "created_at": iso(su.created_at)} for su, u in rows]})


async def admin_signup_act(request: web.Request) -> web.Response:
    from bot.handlers.signup import decide
    await admin_only(request)
    s, admin, bot = ctx(request)
    data = await body(request)
    su, message = await decide(bot, s, admin, int(request.match_info["id"]), bool(data.get("approve")),
                               str(data.get("reason") or "")[:300] or None)
    if su is None:
        raise AppError(404, "not_found", message)
    return web.json_response({"message": message})


async def admin_withdrawal_act(request: web.Request) -> web.Response:
    """An owner settles a withdrawal of a network that is gone: done | refund."""
    from bot.handlers.admin_ops import decidable, settle
    await admin_only(request)
    s, admin, bot = ctx(request)
    if not admins.is_owner(admin.id):
        raise AppError(403, "not_owner", "Только владельцы")
    action = (await body(request)).get("action")
    if action not in ("done", "refund"):
        raise AppError(422, "bad_action", "done или refund")
    wd = await s.get(Withdrawal, int(request.match_info["id"]), with_for_update=True, populate_existing=True)
    if wd is None or not decidable(wd):
        raise AppError(409, "already", "Вывод уже обработан")
    result = await settle(bot, s, admin, wd, action)
    return web.json_response({"message": "Выполнение подтверждено" if result == "done" else "Средства возвращены"})


def setup(r) -> None:
    r.add_get("/app/api/merchant", merchant)
    r.add_post("/app/api/merchant", merchant_settings)
    r.add_get("/app/api/requests/{id:\\d+}", request_view)
    r.add_post("/app/api/deals/{id:\\d+}/take", take)
    r.add_post("/app/api/deals/{id:\\d+}/link", link)
    r.add_post("/app/api/deals/{id:\\d+}/drop", drop)
    r.add_post("/app/api/deals/{id:\\d+}/dispute", dispute)
    r.add_post("/app/api/deals/{id:\\d+}/evidence", evidence)
    r.add_get("/app/api/deals/{id:\\d+}/files/{n:r|\\d+}", file_get)
    r.add_post("/app/api/deals/{id:\\d+}/files/{n:r|\\d+}/send", file_send)
    r.add_post("/app/api/deals/{id:\\d+}/resolve", resolve)
    r.add_get("/app/api/admin", admin_home)
    r.add_get("/app/api/admin/deals", admin_deals)
    r.add_post("/app/api/ratings/{id:\\d+}", rate)
    r.add_post("/app/api/operator/repay", repay)
    r.add_get("/app/api/avatar", avatar)
    r.add_post("/app/api/log", client_log)
    r.add_get("/app/api/team", team_view)
    r.add_post("/app/api/team/out", team_out)
    r.add_get("/app/api/admin/desk", admin_desk)
    r.add_post("/app/api/admin/desk/key", admin_desk_key)
    r.add_get("/app/api/admin/finance", admin_finance)
    r.add_get("/app/api/admin/users", admin_users)
    r.add_get("/app/api/admin/users/{id:\\d+}", admin_user)
    r.add_post("/app/api/admin/users/{id:\\d+}", admin_user_act)
    r.add_get("/app/api/admin/signups", admin_signups)
    r.add_post("/app/api/admin/signups/{id:\\d+}", admin_signup_act)
    r.add_post("/app/api/admin/withdrawals/{id:\\d+}", admin_withdrawal_act)
