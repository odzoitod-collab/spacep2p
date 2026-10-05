from contextlib import suppress
from datetime import timedelta

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import guides
from bot.emoji import back, btn, kb, pe
from bot.models import Operator, OrderMerchant, Ticket, User, now
from bot.services import deals, events, money, operators, settings
from bot.handlers.wallet import withdraw_terms
from bot.ui import BRAND, esc, field, manual, ok, quote, section, show, title, warn

router = Router()
TICKETS_PER_HOUR = 5


class Support(StatesGroup):
    text = State()


def support_url() -> str | None:
    sup = settings.get("support")
    return f"https://t.me/{sup}" if sup else None


def manager_url() -> str | None:
    """The manager for questions (settings: manager); the support account if no manager is set."""
    nick = settings.get("manager") or settings.get("support")
    return f"https://t.me/{nick}" if nick else None


async def main_menu(bot: Bot, s: AsyncSession, user: User, is_admin: bool, src=None, note: str = "") -> None:
    open_ = await deals.open_deals_of(s, user.id)
    active = open_[0] if open_ else None
    todo = await deals.seller_todo(s, user.id)
    need_check = [d for d in todo if d.status == "paid"]
    om = await s.get(OrderMerchant, user.id)
    op = await s.get(Operator, user.id)
    is_op = await operators.is_operator(s, user.id) or bool(op and op.debt)
    rate, pct = settings.buyer_terms(user)
    lines = [
        title(pe("shop"), BRAND),
        quote(f"• Баланс: <b>{money.usdt(user.balance)} USDT</b>"
              + (f" · в сделках {money.usdt(user.frozen)}" if user.frozen else ""),
              f"• Курс: 1 USDT = <b>{money.fmt(rate)} ₽</b> · комиссия {money.fmt(pct, 3)}%"
              + (" · ваши условия" if settings.has_terms(user) else ""),
              f"• Долг оператора: <b>{money.usdt(op.debt)} USDT</b>" if op and op.debt else ""),
    ]
    if not active and not todo and user.balance == 0 and user.frozen == 0:  # a newcomer: one line per section
        lines += [f"{pe('info')} <b>RUB ⇄ USDT</b> — купить за рубли · <b>USDT ⇄ RUB</b> — продавать на свою "
                  f"карту. Подробно — в {manual('инструкции')}."]
    await show(bot, user, "\n".join(lines) + note, kb(
        *[btn(f"Сделка #{d.id}: {deal_hint(d)}", f"dl:{d.id}", "fire", style="primary") for d in open_[:3]],
        btn(f"Ещё открытых сделок: {len(open_) - 3}", inline="сделки ") if len(open_) > 3 else None,
        btn(f"Проверить оплату ({len(need_check)})", f"dl:{need_check[0].id}", "bell", style="danger")
        if need_check else None,
        btn(f"Кошелёк · {money.usdt(user.balance)} USDT", "w", style="primary"),
        [btn("RUB ⇄ USDT", "buy:0", style="success"), btn("USDT ⇄ RUB", "sl", style="danger")],
        btn("Ордерный кабинет" if om and om.status in ("approved", "suspended", "pending") else "Ордерные реквизиты",
            "om", wide=True),
        btn("Оператор" + (f" · долг {money.usdt(op.debt)} USDT" if op and op.debt else ""), "op", wide=True)
        if is_op else None,
        [btn("Мои сделки", inline="сделки "), btn("Помощь", "info")],
        [btn("Команда", "tm"), btn(f"{BRAND} API", "api")],
        [btn("Чат", "chat") if settings.get("chat_id") else None,
         btn("Канал", url=settings.raw("channel_link")) if settings.get("channel_id") and settings.raw("channel_link")
         else None],
        btn("Вопросы менеджеру", url=manager_url()) if manager_url() else None,
        btn("Админ-панель", "a", wide=True) if is_admin else None,
    ), src)


def deal_hint(d) -> str:
    return {"searching": "ищем реквизиты", "assigned": "мерчант выдаёт реквизиты",
            "checking": "проверяем реквизиты", "waiting_payment": "ждём ваш перевод",
            "paid": "чек у продавца", "dispute": "спор"}[d.status]


@router.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, is_admin: bool, state: FSMContext):
    await state.clear()
    await main_menu(bot, s, user, is_admin, c)


@router.callback_query(F.data == "x")
async def cb_close(c: CallbackQuery):
    with suppress(TelegramAPIError):
        await c.message.delete()
    await c.answer()


@router.callback_query(F.data == "info")
async def cb_info(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    await state.set_state(None)
    await info_screen(bot, user, c)


async def info_screen(bot: Bot, user: User, src=None):
    """«Помощь»: how it works and the terms — one message that fits under the banner; the guides open in the same
    message (info:g)."""
    url = support_url()
    rate, pct = settings.buyer_terms(user)
    await show(bot, user, "\n".join([
        title(pe("info"), f"Помощь · {BRAND}"),
        "",
        section("edu", "Как это работает"),
        settings.get("tutorial"),
        "",
        section("percent", "Условия"),
        field("Курс", f"<b>{money.fmt(rate)} ₽</b> · комиссия {money.fmt(pct, 3)}%"),
        field("Мерчанту", f"{settings.get('seller_pct')}% по карте · ордер по {money.fmt(settings.dec('order_rate'))} ₽"),
        field("Сделка", f"оплата {settings.get('deal_minutes')} мин · спор через {settings.get('confirm_minutes')} мин"),
        field("Кошелёк", f"пополнение {settings.get('deposit_fee')}% · вывод {withdraw_terms('xrocket')}"),
        field("Тимлиду", f"{settings.get('team_pct')}% со сделок команды"),
        "",
        quote(f"Вопросы — менеджеру @{esc(settings.get('manager') or settings.get('support'))}: номер сделки и ваш ID "
              f"<code>{user.id}</code>." if manager_url() else f"Ваш ID: <code>{user.id}</code>."),
    ]), kb(btn("Инструкции", "info:g", "edu", style="primary"),
           [btn("Менеджер", url=manager_url()) if manager_url() else None,
            btn("Поддержка", url=url) if url and url != manager_url() else None],
           back("menu", "В меню")), src)


@router.callback_query(F.data == "info:g")
async def cb_guides(c: CallbackQuery, bot: Bot, user: User):
    """The guides: a link per topic, in the same message as «Помощь»."""
    await show(bot, user, "\n".join([
        title(pe("edu"), "Инструкции"),
        "",
        *guides.lines(),
        "",
        quote(f"Памятка продавца — в {manual('отдельной статье')}." if settings.get("manual_url")
              else "Нажмите на название — откроется статья."),
    ]), kb([btn("Все инструкции", url=guides.index_url()) if guides.index_url() else None,
            btn("Памятка продавца", url=settings.get("manual_url")) if settings.get("manual_url") else None],
           back("info", "Помощь")), c)


@router.callback_query(F.data == "sup")
async def cb_support(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await support_screen(bot, s, user, state, c)


async def support_screen(bot: Bot, s: AsyncSession, user: User, state: FSMContext, src=None):
    await state.set_state(Support.text)
    active = await deals.open_deal_of(s, user.id)
    await show(bot, user, "\n".join([
        title(pe("support"), "Сообщение в поддержку"),
        "",
        "Опишите проблему одним сообщением (до 1000 символов): что произошло, номер сделки или вывода, сумма.",
        f"Открытая сделка #{active.id} будет приложена автоматически." if active else "",
        "Ответ придёт сюда, в этот чат.",
    ]), kb(back("info", "Отмена")), src)


@router.message(Support.text)
async def msg_support(m: Message, bot: Bot, s: AsyncSession, user: User, is_admin: bool, state: FSMContext):
    text = (m.text or m.caption or "").strip()
    if not 5 <= len(text) <= 1000:
        return await show(bot, user, title(pe("support"), "Сообщение в поддержку") + "\n\nОпишите проблему текстом."
                          + warn("Нужен текст от 5 до 1000 символов"), kb(back("info", "Отмена")))
    await state.set_state(None)
    recent = await s.scalar(select(func.count(Ticket.id)).where(
        Ticket.user_id == user.id, Ticket.created_at > now() - timedelta(hours=1)))
    if recent >= TICKETS_PER_HOUR:
        return await main_menu(bot, s, user, is_admin, note=warn("Вы уже отправили несколько сообщений — дождитесь ответа."))
    active = await deals.open_deal_of(s, user.id)
    t = Ticket(user_id=user.id, deal_id=active.id if active else None, text=text)
    s.add(t)
    await s.flush()
    # stored first, delivered to admins by the outbox task (retried if Telegram is unavailable)
    events.add(s, f"ticket:{t.id}", "ticket", f"Обращение: {text[:120]}", user.id, alert=True)
    await main_menu(bot, s, user, is_admin, note=ok(f"Обращение #{t.id} принято. Ответ придёт в этот чат."))
