"""Admin panel: order merchants — applications (approve / reject with a reason), suspend / resume, open requests.
Merchants have no limits and no mode: every request goes to all of them, each picks Bybit order or balance."""
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import config
from bot.emoji import back, btn, kb, pe
from bot.models import Deal, OrderMerchant, User, now
from bot.services import audit, events, money, orders, settings
from bot.ui import at, esc, notify, ok, quote, show, title, warn

router = Router()
router.message.filter(F.from_user.id.in_(config.admin_ids))
router.callback_query.filter(F.from_user.id.in_(config.admin_ids))

STATUS = {"pending": "на рассмотрении", "approved": "работает", "rejected": "отклонена", "suspended": "приостановлен"}


class AdmOrders(StatesGroup):
    reason = State()


@router.callback_query(F.data == "aoml")
async def cb_home(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    rows = (await s.scalars(select(OrderMerchant).order_by(
        (OrderMerchant.status == "pending").desc(), OrderMerchant.created_at.desc()).limit(30))).all()
    searching = await s.scalar(select(func.count(Deal.id)).where(Deal.status.in_(orders.REQUEST)))
    working = sum(1 for m in rows if m.status == "approved")
    await show(bot, user, "\n".join([
        title(pe("key"), "Ордерные мерчанты"),
        quote(f"• Работают (получают все заявки): <b>{working}</b>",
              f"• Заявок ищут реквизиты: <b>{searching}</b>",
              f"• Курс ордерного мерчанта: <b>{money.fmt(settings.dec('order_rate'))} ₽</b>"),
        "Анкеты на рассмотрении — сверху.",
    ]), kb(*[btn(f"{STATUS[m.status]} · {m.user_id} · {m.banks[:24]}",
                 f"aom:{m.user_id}", "pencil" if m.status == "pending" else "key",
                 style="primary" if m.status == "pending" else None) for m in rows],
           btn("Заявки в поиске", "adl:search", "search") if searching else None,
           back("a", "Админ-панель")), c)


async def merchant_card(bot: Bot, s: AsyncSession, admin: User, m: OrderMerchant, src=None, note: str = ""):
    u = await s.get(User, m.user_id)
    done_n, done_rub = (await s.execute(select(func.count(Deal.id), func.coalesce(func.sum(Deal.amount_rub), 0)).where(
        Deal.seller_id == m.user_id, Deal.is_order, Deal.status == "completed"))).one()
    disputes = await s.scalar(select(func.count(Deal.id)).where(Deal.seller_id == m.user_id, Deal.is_order,
                                                                Deal.dispute_reason.is_not(None)))
    bybit_n = await s.scalar(select(func.count(Deal.id)).where(Deal.seller_id == m.user_id, Deal.via_bybit,
                                                               Deal.status == "completed"))
    await show(bot, admin, "\n".join([
        title(pe("key"), f"Ордерный мерчант · {STATUS[m.status]}"),
        "<b>Кто</b>",
        quote(f"• {esc(u.name or '—')} @{esc(u.username or '—')} (<code>{u.id}</code>)"
              + (" · ЗАБАНЕН" if u.is_banned else ""),
              f"• Источник: {esc(m.source)}",
              f"• Скорость выдачи: {esc(m.speed)}",
              f"• Банки: {esc(m.banks)}"),
        "<b>Работа</b>",
        quote(f"• Курс: {money.fmt(settings.dec('order_rate'))} ₽ · заявки приходят все, режим выбирает при взятии",
              f"• Баланс: {money.usdt(u.balance)} USDT · в работе сейчас {money.fmt(await orders.open_rub(s, u.id))} ₽",
              f"• Выполнено: {done_n} на {money.fmt(Decimal(done_rub))} ₽ (Bybit-ордером {bybit_n}) · споров {disputes}",
              f"• Анкета {at(m.created_at, 'dt')}"
              + (f" · решение {at(m.decided_at, 'dt')} (<code>{m.admin_id}</code>)" if m.decided_at else "")),
        f"<b>О себе</b>\n{esc(m.about)}" if m.about else "",
        f"Причина отказа: <i>{esc(m.reason)}</i>" if m.reason else "",
    ]) + note, kb(
        [btn("Одобрить", f"aom:ok:{m.user_id}", "ok", style="success"),
         btn("Отклонить", f"aom:no:{m.user_id}", "cross", style="danger")] if m.status == "pending" else None,
        btn("Приостановить", f"aom:st:{m.user_id}:0", "pause", style="danger") if m.status == "approved" else None,
        btn("Возобновить", f"aom:st:{m.user_id}:1", "ok", style="success") if m.status == "suspended" else None,
        [btn("Профиль", f"auv:{m.user_id}", "profile"), btn("История", f"aev:om:{m.user_id}", "list")],
        back("aoml", "Ордерные мерчанты")), src)


@router.callback_query(F.data.regexp(r"^aom:(\d+)$"))
async def cb_merchant(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    m = await s.get(OrderMerchant, int(c.data.split(":")[1]))
    if not m:
        return await c.answer("Анкета не найдена", show_alert=True)
    await merchant_card(bot, s, user, m, c)


@router.callback_query(F.data.regexp(r"^aom:ok:(\d+)$"))
async def cb_approve(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    m = await s.get(OrderMerchant, int(c.data.split(":")[2]), with_for_update=True, populate_existing=True)
    if not m or m.status != "pending":
        return await c.answer("Анкета уже рассмотрена", show_alert=True)
    m.status, m.admin_id, m.decided_at = "approved", user.id, now()
    audit.log(s, user.id, "om_approve", f"om:{m.user_id}")
    events.add(s, f"om:{m.user_id}", "approved", f"Анкета одобрена ({user.name})", m.user_id, alert=True)
    await s.commit()
    await notify(bot, m.user_id, "\n".join([
        f"{pe('ok')} <b>Вы — ордерный мерчант Strait Pay</b>",
        "• Все заявки покупателей приходят в этот чат и в чаты сообщества",
        "• «Взять · Bybit-ордер» — баланс не нужен, присылаете ссылку на ордер",
        "• «Взять · с баланса» — замораживаем ваши USDT, реквизиты выдаёте сами"]),
                 kb(btn("Открыть кабинет", "om", "key", style="success"), back("x", "Скрыть", "cross")))
    await merchant_card(bot, s, user, m, c, ok("Одобрена, мерчант уведомлён"))


@router.callback_query(F.data.regexp(r"^aom:no:(\d+)$"))
async def cb_reject_ask(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    uid = int(c.data.split(":")[2])
    await state.set_state(AdmOrders.reason)
    await state.set_data({"om": uid})
    await show(bot, user, f"{title(pe('cross'), 'Отказ ордерному мерчанту')}\n\nНапишите причину (5–500 символов) — "
                          "её увидит заявитель.", kb(back(f"aom:{uid}", "Отмена")), c)


@router.message(AdmOrders.reason, F.text)
async def msg_reject(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    uid = (await state.get_data()).get("om")
    reason = " ".join(m.text.split())
    if not 5 <= len(reason) <= 500:
        return await show(bot, user, title(pe("cross"), "Причина отказа") + "\n\nНапишите причину ещё раз."
                          + warn("От 5 до 500 символов"), kb(back(f"aom:{uid}", "Отмена")))
    await state.clear()
    om = await s.get(OrderMerchant, uid, with_for_update=True, populate_existing=True)
    if not om or om.status != "pending":
        return await show(bot, user, warn("Анкета уже рассмотрена"), kb(back("aoml", "Ордерные мерчанты")))
    om.status, om.admin_id, om.decided_at, om.reason = "rejected", user.id, now(), reason
    audit.log(s, user.id, "om_reject", f"om:{uid}", reason)
    events.add(s, f"om:{uid}", "rejected", f"Анкета отклонена ({user.name}): {reason}", uid, alert=True)
    await s.commit()
    await notify(bot, uid, f"{pe('cross')} <b>Анкета ордерного мерчанта отклонена.</b>\nПричина: {esc(reason)}")
    await merchant_card(bot, s, user, om, note=ok("Отклонена, заявитель уведомлён"))


@router.callback_query(F.data.regexp(r"^aom:st:(\d+):([01])$"))
async def cb_status(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    _, _, uid, on = c.data.split(":")
    m = await s.get(OrderMerchant, int(uid), with_for_update=True, populate_existing=True)
    if not m or m.status not in ("approved", "suspended"):
        return await c.answer()
    m.status = "approved" if on == "1" else "suspended"
    what = "возобновлён" if on == "1" else "приостановлен"
    audit.log(s, user.id, "om_status", f"om:{uid}", what)
    events.add(s, f"om:{uid}", "status", f"Ордерный мерчант {what} ({user.name})", m.user_id, alert=True)
    await s.commit()
    await notify(bot, m.user_id, f"{pe('key')} Доступ ордерного мерчанта {what} администрацией."
                 + (" Взятые заявки завершите как обычно." if on == "0" else ""))
    await merchant_card(bot, s, user, m, c, ok(what.capitalize()))

