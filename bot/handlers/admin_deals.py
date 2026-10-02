"""Admin: one card per deal (open it by its number: /deal 15, «Найти» → #15 or «Открыть» in the log chat) with
everything about it — who created it, who took it, the operator, money, requisites, deadlines, the history — and full
control: give the requisites yourself, change the amount, give more time, close it, settle it, write to anyone."""
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import config
from bot.emoji import back, btn, kb, pe
from bot.handlers.deal import CLOSE_REASONS, REASONS, STATUS, card_of, push
from bot.handlers.seller import parse_rub
from bot.models import ApiClient, Deal, Event, User
from bot.services import audit, deals, events, money, orders
from bot.ui import at, esc, notify, ok, person, quote, show, title, warn

router = Router()
router.message.filter(F.from_user.id.in_(config.admin_ids))
router.callback_query.filter(F.from_user.id.in_(config.admin_ids))
HISTORY = 8
EXTEND = 15


class AdmDeal(StatesGroup):
    amount = State()


def kind_of(d: Deal) -> str:
    return "Bybit-ордер" if d.via_bybit else "ордер с баланса мерчанта" if d.is_order else "статичная карта"


async def card_text(s: AsyncSession, d: Deal) -> str:
    card = await card_of(s, d)
    client = await s.get(ApiClient, d.api_client_id) if d.api_client_id else None
    ids = [uid for uid in (d.buyer_id, d.seller_id, d.operator_id) if uid]
    users = {u.id: u for u in (await s.scalars(select(User).where(User.id.in_(ids)))).all()}
    done = await deals.completed_count(s, ids)

    def who(uid: int | None, empty: str) -> str:
        if not uid:
            return empty
        u = users.get(uid)
        return f"{esc(person(u))} · сделок {done[uid]}" + (" · ЗАБАНЕН" if u and u.is_banned else "")

    icon, label = STATUS[d.status]
    frozen = deals.frozen(d) and d.seller_id and d.status in deals.FUNDED
    times = [f"создана {at(d.created_at, 'dt')}"]
    if d.status in deals.UNPAID:
        times.append(f"срок этапа до {at(d.expires_at, 'dt')}")
    if d.paid_at:
        times.append(f"чек {at(d.paid_at, 'dt')}")
    if d.closed_at:
        times.append(f"закрыта {at(d.closed_at, 'dt')}")
    rows = (await s.scalars(select(Event).where(Event.ref == f"deal:{d.id}").order_by(Event.id.desc())
                            .limit(HISTORY))).all()
    files = d.dispute_files or []
    lines = [
        title(pe("fire"), f"Сделка #{d.id}"),
        f"{pe(icon)} <b>{label}</b> · {kind_of(d)}" + (f" · API «{esc(client.project)}»" if client else ""),
        "<b>Участники</b>",
        quote(f"• Создал (покупатель): {who(d.buyer_id, '—')}",
              f"• Принял (мерчант): {who(d.seller_id, 'ещё никто')}",
              f"• Оператор: {who(d.operator_id, 'не назначен')}" if d.via_bybit else ""),
        "<b>Деньги</b>",
        quote(f"• Сумма: <b>{money.fmt(d.amount_rub)} ₽</b>",
              f"• Покупатель получит: <b>{money.usdt(d.buyer_credit)} USDT</b> · курс {money.fmt(d.buyer_rate or d.rate)} ₽ "
              f"· {money.fmt(d.platform_pct, 3)}%",
              f"• Мерчант отдаёт: {money.usdt(d.seller_debit)} USDT" + (
                  f" по {money.fmt(d.merchant_rate)} ₽" if d.merchant_rate else f" · {money.fmt(d.seller_pct, 3)}%")
              + (" · заморожено" if frozen else " · через Bybit-ордер" if d.via_bybit and d.bybit_url else ""),
              f"• Площадке: {money.usdt(d.platform_fee)} USDT" + (
                  f" · тимлиду {money.usdt(d.team_fee)} USDT" if d.team_fee else "")),
        "<b>Реквизиты</b>",
        quote(f"• {esc(card.bank)} · <code>{esc(card.requisites)}</code> · {esc(card.holder)}" if card
              else "• Ещё не выданы",
              f"• Банк покупателя: {esc(d.sender_bank)}" if d.sender_bank else "",
              f'• Ордер Bybit: <a href="{esc(d.bybit_url)}">{esc(d.bybit_url[:50])}</a>' if d.bybit_url else ""),
        "<b>Сроки</b>",
        quote("• " + " · ".join(times)),
    ]
    if d.dispute_reason or files or d.receipt_file_id:
        by = {r: sum(1 for f in files if (f[2] if len(f) > 2 else "seller") == r) for r in ("buyer", "seller")}
        lines += ["<b>Чек и спор</b>", quote(
            f"• Чек: {'есть' if d.receipt_file_id else 'нет'} · материалов: покупатель {by['buyer']}, продавец "
            f"{by['seller']}",
            f"• Причина спора: {REASONS.get(d.dispute_reason, d.dispute_reason)}"
            + (f" — пришло {money.fmt(d.dispute_amount_rub)} ₽" if d.dispute_amount_rub else "") if d.dispute_reason
            else "")]
    if d.close_reason:
        lines.append(f"Итог: <b>{CLOSE_REASONS.get(d.close_reason, d.close_reason)}</b>"
                     + (f" — <i>{esc(d.resolution)}</i>" if d.resolution else ""))
    if rows:
        lines += ["<b>История</b> (последние события)",
                  quote(*[f"• {at(e.created_at)} · {esc(e.text[:160])}" for e in reversed(rows)])]
    return "\n".join(lines)


def card_kb(d: Deal, *extra):
    request = d.status in orders.REQUEST
    settle = d.status in ("paid", "dispute")
    operator = d.operator_id if d.via_bybit else None
    return kb(
        btn("Выдать реквизиты самому", f"adm:give:{d.id}", "key", style="primary") if request else None,
        btn("Чек и файлы", f"af:{d.id}", "clip") if d.receipt_file_id or d.dispute_files else None,
        btn("Завершить: USDT покупателю", f"ar:{d.id}:b", "ok", style="success") if settle else None,
        btn(f"Провести на {money.fmt(d.dispute_amount_rub)} ₽", f"ar:{d.id}:a", "ruble", style="primary")
        if settle and d.dispute_amount_rub is not None and d.dispute_amount_rub != d.amount_rub else None,
        btn("Отменить: в пользу продавца", f"ar:{d.id}:s", "cross", style="danger") if settle else None,
        btn("Закрыть заявку" if request else "Отменить сделку", f"ar:{d.id}:c", "cross", style="danger")
        if d.status in deals.UNPAID else None,
        [btn("Изменить сумму", f"adm:amt:{d.id}", "pencil") if d.status in deals.OPEN else None,
         btn(f"Продлить +{EXTEND} мин", f"adm:ext:{d.id}", "clock") if d.status in deals.UNPAID else None],
        [btn("Покупатель", f"auv:{d.buyer_id}", "profile"),
         btn("Мерчант", f"auv:{d.seller_id}", "profile") if d.seller_id else None],
        btn("Оператор", f"auv:{operator}", "profile") if operator else None,
        [btn("Написать покупателю", f"dm:{d.id}:{d.buyer_id}", "support") if not d.api_client_id else None,
         btn("Написать мерчанту", f"dm:{d.id}:{d.seller_id}", "support") if d.seller_id else None],
        btn("Написать оператору", f"dm:{d.id}:{operator}", "support") if operator else None,
        [btn("Вся история", f"aev:deal:{d.id}", "list"), btn("Обновить", f"adv:{d.id}", "refresh")],
        *extra)


async def deal_view(bot: Bot, s: AsyncSession, admin: User, d: Deal, src=None, note: str = ""):
    await show(bot, admin, await card_text(s, d) + note, card_kb(d, back("ad", "Сделки")), src)


@router.callback_query(F.data.regexp(r"^adv:(\d+)$"))
async def cb_view(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.set_state(None)
    d = await s.get(Deal, int(c.data.split(":")[1]), populate_existing=True)
    if not d:
        return await c.answer("Сделка не найдена", show_alert=True)
    await deal_view(bot, s, user, d, c)


# ---------- give the requisites yourself ----------

@router.callback_query(F.data.regexp(r"^adm:give:(\d+)$"))
async def cb_give(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    from bot.handlers.orders import close_offers, give_screen
    did = int(c.data.split(":")[2])
    try:
        d, merchant, operator = await orders.admin_take(s, did, user)
    except deals.DealError as e:
        await s.rollback()
        await s.refresh(user)
        return await c.answer(str(e), show_alert=True)
    audit.log(s, user.id, "deal_take", f"deal:{d.id}")
    events.add(s, f"deal:{d.id}", "admin_take", f"Администратор {person(user)} выдаёт реквизиты сам"
               + (f", мерчант {merchant} снят" if merchant else "") + (f", оператор {operator} снят" if operator else ""),
               notice=True)
    await s.commit()
    await close_offers(bot, s, d, f"Заявку #{d.id} взяла администрация", keep=user.id)
    for uid in (merchant, operator):
        if uid:
            await notify(bot, uid, f"{pe('info')} Заявку #{d.id} забрала администрация — делать по ней ничего не нужно."
                         + (" Заморозка снята." if uid == merchant and not d.bybit_url else ""))
    await push(bot, s, d.buyer_id, d, f"Реквизиты по заявке #{d.id} готовит администрация")
    await give_screen(bot, s, user, d, state, c)


# ---------- change the amount ----------

@router.callback_query(F.data.regexp(r"^adm:amt:(\d+)$"))
async def cb_amount(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    d = await s.get(Deal, int(c.data.split(":")[2]), populate_existing=True)
    if not d or d.status not in deals.OPEN:
        return await c.answer("Сделка уже закрыта", show_alert=True)
    await state.set_state(AdmDeal.amount)
    await state.set_data({"deal": d.id})
    await show(bot, user, _amount_text(d), kb(back(f"adv:{d.id}", "Отмена")), c)


def _amount_text(d: Deal, err: str = "") -> str:
    return "\n".join([
        title(pe("pencil"), f"Сумма сделки #{d.id}"),
        quote(f"• Сейчас: <b>{money.fmt(d.amount_rub)} ₽</b> → покупателю {money.usdt(d.buyer_credit)} USDT",
              "• Пересчёт — по условиям этой сделки (курс, процент или курс ордера)",
              "• Заморозка мерчанта изменится вместе с суммой"),
        "Отправьте новую сумму в рублях. Стороны получат уведомление.",
    ]) + (warn(err) if err else "")


@router.message(AdmDeal.amount, F.text)
async def msg_amount(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    did = (await state.get_data()).get("deal", 0)
    d = await s.get(Deal, did, populate_existing=True)
    v = parse_rub(m.text)
    if d is None:
        await state.clear()
        return await show(bot, user, warn("Сделка не найдена"), kb(back("ad", "Сделки")))
    if v is None:
        return await show(bot, user, _amount_text(d, "Нужна сумма числом, например 15000"),
                          kb(back(f"adv:{d.id}", "Отмена")))
    try:
        d, old = await deals.change_amount(s, did, v)
    except deals.DealError as e:
        await s.rollback()
        await s.refresh(user)
        d = await s.get(Deal, did, populate_existing=True)
        return await show(bot, user, _amount_text(d, str(e)), kb(back(f"adv:{d.id}", "Отмена")))
    await state.clear()
    audit.log(s, user.id, "deal_amount", f"deal:{d.id}", f"{old} → {v}")
    events.add(s, f"deal:{d.id}", "amount", f"Администратор {person(user)} изменил сумму: {money.fmt(old)} → "
               f"{money.fmt(v)} ₽, покупателю {money.usdt(d.buyer_credit)} USDT", notice=True)
    await s.commit()
    await deal_view(bot, s, user, d, note=ok(f"Сумма изменена: {money.fmt(old)} → {money.fmt(v)} ₽"))
    head = f"Администрация изменила сумму сделки #{d.id}: {money.fmt(old)} → {money.fmt(v)} ₽"
    for uid in dict.fromkeys((d.buyer_id, *deals.sellers(d))):
        await push(bot, s, uid, d, head)


# ---------- more time ----------

@router.callback_query(F.data.regexp(r"^adm:ext:(\d+)$"))
async def cb_extend(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    did = int(c.data.split(":")[2])
    try:
        d = await deals.extend(s, did, EXTEND)
    except deals.DealError as e:
        return await c.answer(str(e), show_alert=True)
    audit.log(s, user.id, "deal_extend", f"deal:{d.id}", f"+{EXTEND} min")
    events.add(s, f"deal:{d.id}", "extended", f"Администратор {person(user)} продлил срок этапа до "
               f"{d.expires_at.astimezone(deals.MSK):%H:%M} МСК", notice=True)
    await s.commit()
    await deal_view(bot, s, user, d, c, ok(f"Срок продлён до {at(d.expires_at)}"))
    if d.status == "waiting_payment":
        await push(bot, s, d.buyer_id, d, f"Срок оплаты сделки #{d.id} продлён до "
                                          f"{d.expires_at.astimezone(deals.MSK):%H:%M} МСК")

