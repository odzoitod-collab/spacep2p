"""Merchant advertisement link and admin account/workflow diagnostics."""
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import config
from bot.emoji import back, btn, kb, pe
from bot.models import BybitAccount, BybitAction, BybitOrder, Deal, User
from bot.services import audit, bybit_orders, deals, money
from bot.services.bybit import BybitError, gross_quantity
from bot.ui import at, esc, show, title, warn

router = Router()


class BybitInput(StatesGroup):
    link = State()
    binding = State()


@router.callback_query(F.data.regexp(r"^orq:bybit:(\d+)$"))
async def request_link(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    d = await s.get(Deal, int(c.data.split(":")[-1]))
    if not config.bybit_enabled or not d or d.seller_id != user.id or d.status != "assigned":
        return await c.answer("Действие недоступно", show_alert=True)
    await state.set_state(BybitInput.link)
    await state.update_data(bybit_deal=d.id)
    await show(bot, user, f"{title(pe('link'), f'Bybit · заявка #{d.id}')}\n\n"
               f"Сумма покупателя: <b>{money.fmt(d.amount_rub)} ₽</b>\n"
               f"Объём до комиссии: <b>{money.usdt(gross_quantity(d))} USDT</b>\n\n"
               "Пришлите ссылку на объявление Bybit. Вход по объявлению пока проходит настройку; "
               "передача ссылки сама по себе не открывает ордер.",
               kb(back(f"orq:give:{d.id}", "Назад")), c)


@router.message(BybitInput.link, F.text)
async def receive_link(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    did = (await state.get_data()).get("bybit_deal")
    try:
        job = await bybit_orders.submit_link(s, did, user, m.text)
    except (deals.DealError, BybitError, ValueError) as error:
        await s.rollback()
        await s.refresh(user)
        text = "Нужна HTTPS-ссылка на объявление Bybit с его номером." if isinstance(error, (BybitError, ValueError)) else str(error)
        return await show(bot, user, warn(text), kb(back(f"orq:give:{did}", "Назад")))
    await s.commit()
    await state.clear()
    await show(bot, user, f"{title(pe('link'), f'Bybit · заявка #{job.deal_id}')}\n\n"
               "Ссылка сохранена. Автоматический вход по объявлению ещё не подключён. "
               "Ордер Bybit не открыт, уведомление отправлено администрации.",
               kb(back(f"orq:give:{did}", "К заявке")))


async def panel(bot: Bot, s: AsyncSession, user: User, src=None):
    accounts = (await s.scalars(select(BybitAccount).order_by(BybitAccount.name))).all()
    jobs = (await s.scalars(select(BybitOrder).where(~BybitOrder.stage.in_(bybit_orders.TERMINAL))
                           .order_by(BybitOrder.deal_id.desc()).limit(15))).all()
    lines = [title(pe("settings"), "Bybit"), "",
             "Подключение включено" if config.bybit_enabled else "Подключение выключено",
             "Автоматический вход и отмена по объявлению: ещё не подключены.", ""]
    buttons = []
    for account in accounts:
        status = "приём включён" if account.enabled else "приём на паузе"
        if account.name not in bybit_orders.clients:
            status = "ключ не подключён"
        pause = f" · ограничение до {at(account.paused_until)}" if account.paused_until else ""
        lines.append(f"<b>{esc(account.name)}</b> · {status}{pause}")
        buttons.append(btn(f"{'Пауза' if account.enabled else 'Включить'} · {account.name}",
                           f"bb:account:{account.name}", "settings"))
    if not accounts:
        lines.append("Аккаунты пока не настроены. Ключи добавляются в конфигурацию сервера.")
    if jobs:
        lines.extend(["", "<b>Активные заявки</b>"])
    for job in jobs:
        lines.append(f"#{job.deal_id} · {esc(job.stage)} · {esc(job.account_name or 'без аккаунта')}"
                     + (f" · {esc(job.error_code)}" if job.error_code else ""))
        buttons.append(btn(f"Заявка #{job.deal_id}", f"bb:job:{job.deal_id}", "list"))
    await show(bot, user, "\n".join(lines), kb(*buttons, back("a", "В админ-панель")), src)


@router.callback_query(F.data == "bb")
async def open_panel(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    if user.id not in config.admin_ids:
        return await c.answer("Нет доступа", show_alert=True)
    await panel(bot, s, user, c)


@router.callback_query(F.data.startswith("bb:account:"))
async def toggle_account(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    if user.id not in config.admin_ids:
        return await c.answer("Нет доступа", show_alert=True)
    account = await s.get(BybitAccount, c.data.split(":")[-1], with_for_update=True)
    if not account:
        return await c.answer("Аккаунт не найден", show_alert=True)
    account.enabled = not account.enabled
    audit.log(s, user.id, "bybit_account", account.name, f"enabled={account.enabled}")
    await s.commit()
    await panel(bot, s, user, c)


async def job_screen(bot: Bot, s: AsyncSession, user: User, did: int, src=None):
    job = await s.get(BybitOrder, did)
    if not job:
        return await show(bot, user, warn("Заявка Bybit не найдена"), kb(back("bb", "Назад")), src)
    actions = (await s.scalars(select(BybitAction).where(BybitAction.deal_id == did)
                              .order_by(BybitAction.id.desc()).limit(15))).all()
    lines = [title(pe("list"), f"Bybit · заявка #{did}"), "",
             f"Аккаунт: {esc(job.account_name or 'не назначен')}", f"Ордер: {esc(job.order_id or 'не открыт')}",
             f"Состояние: {esc(job.stage)}", "", "<b>Последние действия</b>"]
    for action in reversed(actions):
        lines.append(f"{at(action.created_at)} · {esc(action.action)} · {esc(action.result)}"
                     + (f" · {esc(action.code)}" if action.code else ""))
    buttons = []
    if job.stage == "awaiting_entry":
        buttons.append(btn("Привязать открытый ордер для проверки", f"bb:bind:{did}", "link"))
    if job.stage == "review" and job.resume_stage in ("requisites", "receipt", "waiting_release"):
        buttons.append(btn("Проверить и продолжить", f"bb:resume:{did}", "refresh"))
    # No button that clears send/payment markers or force-credits a buyer.
    await show(bot, user, "\n".join(lines), kb(*buttons, back("bb", "Назад")), src)


@router.callback_query(F.data.regexp(r"^bb:job:(\d+)$"))
async def open_job(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    if user.id not in config.admin_ids:
        return await c.answer("Нет доступа", show_alert=True)
    await job_screen(bot, s, user, int(c.data.split(":")[-1]), c)


@router.callback_query(F.data.regexp(r"^bb:resume:(\d+)$"))
async def resume_job(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    if user.id not in config.admin_ids:
        return await c.answer("Нет доступа", show_alert=True)
    did = int(c.data.split(":")[-1])
    try:
        await bybit_orders.resume(s, did, user.id)
    except deals.DealError as error:
        return await c.answer(str(error), show_alert=True)
    await s.commit()
    await job_screen(bot, s, user, did, c)


@router.callback_query(F.data.regexp(r"^bb:bind:(\d+)$"))
async def ask_binding(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    if user.id not in config.admin_ids:
        return await c.answer("Нет доступа", show_alert=True)
    did = int(c.data.split(":")[-1])
    await state.set_state(BybitInput.binding)
    await state.update_data(bybit_deal=did)
    await show(bot, user, f"{title(pe('link'), f'Проверка Bybit · заявка #{did}')}\n\n"
               "Укажите имя настроенного аккаунта и номер уже открытого неоплаченного ордера через пробел. "
               "После проверки бот начнёт получать реквизиты и обрабатывать оплату. "
               "Объявление, рублёвая сумма и объём USDT должны совпадать с заявкой.",
               kb(back(f"bb:job:{did}", "Назад")), c)


@router.message(BybitInput.binding, F.text)
async def receive_binding(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    if user.id not in config.admin_ids:
        await state.clear()
        return
    did = (await state.get_data()).get("bybit_deal")
    parts = m.text.strip().split()
    if len(parts) != 2 or not parts[1].isdigit() or len(parts[1]) > 32:
        return await show(bot, user, warn("Нужно имя аккаунта и номер ордера через пробел."),
                          kb(back(f"bb:job:{did}", "Назад")))
    try:
        await bybit_orders.bind_existing(s, did, parts[0], parts[1], user.id)
    except (BybitError, deals.DealError) as error:
        await s.rollback()
        await s.refresh(user)
        job = await s.get(BybitOrder, did)
        if job:
            bybit_orders.record(s, job, "bind_rejected", "error",
                                error.code if isinstance(error, BybitError) else "local_validation",
                                actor_id=user.id)
            await s.commit()
        return await show(bot, user, warn(str(error)), kb(back(f"bb:job:{did}", "Назад")))
    await s.commit()
    await state.clear()
    await job_screen(bot, s, user, did)
