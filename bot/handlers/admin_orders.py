"""Admin panel: order merchants — applications (approve / reject with a reason), suspend / resume, open requests.
Merchants have no limits and no mode: every request goes to all of them, each picks Bybit order or balance."""
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.admins import IsAdmin
from bot.emoji import back, btn, kb, pe
from bot.models import Deal, OrderMerchant, User, now
from bot.services import audit, events, money, orders, settings
from bot.ui import alink, at, card, cf, esc, notify, quote, show, title, ulink, verdict, warn

router = Router()
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

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
    decided = await s.get(User, m.admin_id) if m.admin_id else None
    await show(bot, admin, "\n".join([
        title(pe("key"), f"Ордерный мерчант {alink('om', m.user_id, f'#{m.user_id}')}") + f" · {STATUS[m.status]}",
        "",
        card(cf("Кто", ulink(u) + (" · <b>ЗАБАНЕН</b>" if u.is_banned else ""), icon="profile"),
             cf("Анкета", f"источник: {esc(m.source)}", f"скорость выдачи: {esc(m.speed)}", f"банки: {esc(m.banks)}",
                icon="list"),
             cf("Работа", f"курс {money.fmt(settings.dec('order_rate'))} ₽ · заявки приходят все, режим — при взятии",
                f"баланс {money.usdt(u.balance)} USDT · в работе сейчас {money.fmt(await orders.open_rub(s, u.id))} ₽",
                f"выполнено {done_n} на {money.fmt(Decimal(done_rub))} ₽ (Bybit-ордером {bybit_n}) · споров {disputes}",
                icon="stats"),
             cf("Репутация", orders.rep_line(*await orders.reputation(s, m.user_id)), icon="star"),
             cf("Пропуски реквизитов", f"{m.strikes} из {settings.get('strike_limit')} подряд" if m.strikes else "",
                f"<b>пауза до {at(m.sleep_until, 'dt')}</b>" if orders.asleep(m) else "", icon="warn"),
             cf("Решение", f"анкета {at(m.created_at, 'dt')}"
                + (f" · решение {at(m.decided_at, 'dt')} · {ulink(decided, m.admin_id)}" if m.decided_at else ""),
                icon="clock"),
             cf("О себе", esc(m.about), icon="info") if m.about else "",
             cf("Причина отказа", f"<i>{esc(m.reason)}</i>", icon="cross") if m.reason else ""),
    ]) + note, kb(
        [btn("Одобрить", f"aom:ok:{m.user_id}", "ok", style="success"),
         btn("Отклонить", f"aom:no:{m.user_id}", "cross", style="danger")] if m.status == "pending" else None,
        btn("Снять паузу и пропуски", f"aom:wake:{m.user_id}", "ok", style="success")
        if orders.asleep(m) or m.strikes else None,
        btn("Приостановить", f"aom:st:{m.user_id}:0", "pause", style="danger") if m.status == "approved" else None,
        btn("Возобновить", f"aom:st:{m.user_id}:1", "ok", style="success") if m.status == "suspended" else None,
        btn("История", f"aev:om:{m.user_id}", "list"),
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
    await merchant_card(bot, s, user, m, c, "\n\n" + verdict("ok", "Одобрено", user))


@router.callback_query(F.data.regexp(r"^aom:no:(\d+)$"))
async def cb_reject_ask(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    uid = int(c.data.split(":")[2])
    await state.set_state(AdmOrders.reason)
    await state.set_data({"om": uid})
    await show(bot, user, f"{pe('cross')} <b>Отказ ордерному мерчанту</b>\n\n"
                          + quote("Напишите причину следующим сообщением (5–500 символов) — её увидит заявитель."),
               kb(back(f"aom:{uid}", "Отмена")), c)


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
    await merchant_card(bot, s, user, om, note="\n\n" + verdict("cross", "Отклонено", user))


@router.callback_query(F.data.regexp(r"^aom:wake:(\d+)$"))
async def cb_wake(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    m = await s.get(OrderMerchant, int(c.data.split(":")[2]), with_for_update=True, populate_existing=True)
    if not m:
        return await c.answer("Анкета не найдена", show_alert=True)
    m.strikes, m.sleep_until = 0, None
    audit.log(s, user.id, "om_wake", f"om:{m.user_id}")
    events.add(s, f"om:{m.user_id}", "wake", f"Пауза и пропуски сняты ({user.name})", m.user_id, notice=True)
    await s.commit()
    await notify(bot, m.user_id, f"{pe('ok')} Администрация сняла паузу: заявки снова приходят.")
    await merchant_card(bot, s, user, m, c, "\n\n" + verdict("ok", "Пауза снята", user))


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
    await merchant_card(bot, s, user, m, c, "\n\n" + verdict("ok" if on == "1" else "pause", what.capitalize(), user))

