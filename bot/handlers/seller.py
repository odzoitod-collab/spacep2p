import re
from contextlib import suppress
from datetime import timedelta
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.emoji import back, btn, kb, pe
from bot.models import Card, Deal, User, now
from bot.services import bsc, deals, events, money, settings
from bot.ui import app_btn, esc, manual, ok, quote, section, show, title, warn

router = Router()

BANKS = ["Сбербанк", "Т-Банк", "Альфа-Банк", "ВТБ", "Райффайзен", "Озон Банк", "Газпромбанк", "Совкомбанк"]


class AddCard(StatesGroup):
    bank = State()
    requisites = State()
    holder = State()
    min = State()
    max = State()
    confirm = State()


class CreateSellDeal(StatesGroup):
    amount = State()
    bank = State()
    requisites = State()
    holder = State()
    confirm = State()


def mask(card: Card) -> str:
    r = card.requisites
    return f"•• {r[-4:]}" if card.kind == "card" else f"{r[:2]} ••• {r[-4:]}"


def card_label(card: Card) -> str:
    return f"{card.bank} {mask(card)} · {money.fmt(card.min_rub)}–{money.fmt(card.max_rub)} ₽"


def card_icon(card: Card) -> str:
    return "ban" if card.is_banned else "ok" if card.is_active else "pause"


def luhn(num: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(num)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def parse_rub(raw: str | None) -> Decimal | None:
    raw = (raw or "").replace(" ", "").replace(",", ".")
    try:
        v = Decimal(raw)
    except Exception:
        return None
    if not v.is_finite() or not 0 < v < Decimal("100000000"):
        return None
    amount = v.quantize(Decimal("0.01"))
    return amount if amount > 0 else None


async def _cards(s: AsyncSession, uid: int) -> list[Card]:
    return list((await s.scalars(select(Card).where(Card.user_id == uid, ~Card.is_deleted).order_by(Card.id))).all())


async def seller_menu(bot: Bot, s: AsyncSession, user: User, src=None, note: str = "") -> None:
    cards = await _cards(s, user.id)
    busy = await deals.busy_cards(s, user.id, full=True)
    used = await deals.used_today(s, [c.id for c in cards])
    todo = await deals.seller_todo(s, user.id)
    need_check = [d for d in todo if d.status == "paid" and not d.via_bybit]  # a Bybit order is the operator's
    cap = money.max_rub(user.balance, settings.dec("rate"), settings.merchant_pct(user))
    vis = {c.id: deals.card_visibility(c, user, busy.get(c.id), used[c.id]) for c in cards}
    visible = sum(v[0] for v in vis.values())
    status = (f"{pe('live')} <b>На смене</b> · покупатели видят карт: <b>{visible} из {len(cards)}</b>" if user.is_online
              else f"{pe('pause')} <b>Не на смене</b> — покупатели ваши карты не видят")
    today = await deals.seller_stats(s, user.id, deals.day_start())
    lines = [
        title(pe("card"), "USDT ⇄ RUB · панель мерчанта"),
        status,
        quote(
            f"Сегодня: <b>{today['n']}</b> · {money.fmt(today['rub'])} ₽ · <b>+{money.usdt(today['income'])} USDT</b>"
            if today["n"] else "",
            f"Доступно: <b>{money.usdt(user.balance)} USDT</b> — сделка до {money.fmt(cap)} ₽",
            f"В сделках: {money.usdt(user.frozen)} USDT" if user.frozen else "",
            f"Ваш доход: <b>{money.fmt(settings.merchant_pct(user), 3)}%</b> с каждой сделки"
            + (" (личная ставка)" if user.pct_static is not None else ""),
            f"Открытых сделок: <b>{len(todo)}</b>" + (f" · ждут проверки: <b>{len(need_check)}</b>" if need_check else ""),
        ),
    ]
    dep_enabled = settings.get("insurance_deposit_enabled") != "0"
    dep_amount = settings.dec("insurance_deposit_amount") or Decimal(1500)
    if dep_enabled and user.balance < dep_amount:
        lines.append(f"{pe('warn')} Требуется страховой депозит <b>{money.usdt(dep_amount)} USDT</b>. "
                     "Нажмите «Страховой депозит» или напишите «Оплатить» для пополнения.")
    if not cards:
        lines.append(f"{pe('info')} Добавьте карту или СБП — сразу выйдете на смену и попадёте в список покупателей.")
    elif user.balance <= 0:
        lines.append(f"{pe('warn')} Нет свободного баланса — пополните кошелёк, иначе карты не видны.")
    elif user.is_online and not visible:
        lines.append(f"{pe('warn')} Ни одна карта не видна покупателям — откройте карту, там причина.")
    lines.append(f"{pe('info')} У каждой карты в списке видно, показывается ли она покупателям. Подробно — в "
                 f"{manual('инструкции')}. "
                 + (f"Смена закончится сама через {settings.get('online_minutes')} мин без действий."
                    if settings.num("online_minutes") else "Смена длится, пока вы её не завершите."))
    await show(bot, user, "\n".join(lines) + note, kb(
        btn(f"Проверить оплату ({len(need_check)})", f"dl:{need_check[0].id}", "bell", style="danger")
        if need_check else None,
        btn("Уйти со смены", "sl:on:0", style="danger") if user.is_online
        else btn("Выйти на смену", "sl:on:1", "live", style="success") if cards else None,
        btn("Создать сделку на продажу", "sl:create_deal", "plus", style="primary"),
        *[btn(("🟢 " if vis[c.id][0] else "⏸ ") + card_label(c), f"cd:{c.id}") for c in cards],
        btn("Добавить карту", "sl:add", "plus", style=None if cards else "primary"),
        app_btn("Карты в приложении", "cards"),
        [btn(f"В работе ({len(todo)})" if todo else "В работе", "sl:work", "fire"), btn("Статистика", "sl:st", "stats")],
        [btn("Страховой депозит", "sl:deposit", "lock"), btn("Настройки", "sl:cfg", "settings")]
        if dep_enabled else btn("Настройки", "sl:cfg", "settings"),
        back("menu", "В меню"),
    ), src)


def _period(name: str, st: dict) -> list[str]:
    if not st["n"]:
        return [f"<b>{name}</b> · сделок нет"]
    return [f"<b>{name}</b>", quote(
        f"{pe('ok')} Сделок: <b>{st['n']}</b>" + (f" (ордерных {st['n_order']})" if st["n_order"] else "")
        + f" на <b>{money.fmt(st['rub'])} ₽</b> · средний чек {money.fmt(st['avg'])} ₽",
        f"{pe('up')} Доход: <b>+{money.usdt(st['income'])} USDT</b>",
        f"{pe('stats')} Успешных: {st['success']}%" if st["success"] is not None else "",
        f"{pe('clock')} Подтверждаете в среднем за {st['confirm_min']} мин" if st["confirm_min"] is not None else "",
        f"{pe('flag')} Споров: {st['disputes']}" if st["disputes"] else "")]


@router.callback_query(F.data == "sl:st")
async def cb_seller_stats(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    blocks = []
    for name, since in (("Сегодня", deals.day_start()), ("7 дней", now() - timedelta(days=7)),
                        ("30 дней", now() - timedelta(days=30)), ("Всё время", None)):
        blocks += _period(name, await deals.seller_stats(s, user.id, since))
    days = await deals.income_by_day(s, user.id, 7)
    await show(bot, user, "\n".join([
        title(pe("stats"), "Статистика и доход"),
        "",
        *blocks,
        "",
        title(pe("up"), "Доход по дням"),
        quote(*[f"{d:%d.%m} · {n} сд. · <b>+{money.usdt(inc)} USDT</b>" if n else f"{d:%d.%m} · —" for d, n, inc in days]),
        "Успешные — завершённые из всех закрытых. Быстрое подтверждение = меньше споров и больше заказов.",
    ]), kb(btn("Сделки в работе", "sl:work", "fire"), back("sl", "Панель мерчанта")), c)


@router.callback_query(F.data == "sl:work")
async def cb_seller_work(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    rows = await deals.seller_todo(s, user.id)
    labels = {"assigned": "выдать реквизиты", "checking": "ордер у оператора", "waiting_payment": "ждём перевод",
              "paid": "проверьте поступление", "dispute": "спор"}
    await show(bot, user, title(pe("fire"), "Сделки в работе") + "\n\n" + (
        "Сначала те, где нужно ваше действие." if rows else f"{pe('ok')} Открытых сделок нет."), kb(
        *[btn(f"#{d.id} · {money.fmt(d.amount_rub)} ₽ · "
              + ("у оператора" if d.via_bybit and d.status in ("checking", "paid") else labels.get(d.status, d.status))
              + (" · ордер" if d.is_order else ""), f"dl:{d.id}", "bell" if d.status in ("paid", "assigned") else "fire",
              style="danger" if d.status in ("paid", "assigned") else None) for d in rows],
        back("sl", "Панель мерчанта")), c)


@router.callback_query(F.data.in_({"sl:cfg", "sl:quiet"}))
async def cb_seller_settings(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    note = ""
    if c.data == "sl:quiet":
        user.quiet = not user.quiet
        note = ok("Уведомления о сделках будут приходить без звука" if user.quiet else "Звук уведомлений включён")
    auto = settings.num("online_minutes")
    await show(bot, user, "\n".join([
        title(pe("settings"), "Настройки мерчанта"),
        "",
        quote(f"{pe('bell')} Уведомления о сделках: <b>{'без звука' if user.quiet else 'со звуком'}</b>",
              f"{pe('pause')} Смена завершается сама после {auto} мин без действий (за 5 мин придёт напоминание)"
              if auto else f"{pe('live')} Смена длится, пока вы её не завершите"),
        f"{pe('info')} Суммы и лимит в день задаются у каждой карты.",
    ]) + note, kb(
        btn("Включить звук" if user.quiet else "Уведомления без звука", "sl:quiet", "bell"),
        btn("Выключить все карты", "sl:alloff", "pause"),
        back("sl", "Панель мерчанта")), c)


async def open_seller(bot: Bot, s: AsyncSession, user: User, src=None) -> None:
    dep_enabled = settings.get("insurance_deposit_enabled") != "0"
    dep_amount = settings.dec("insurance_deposit_amount") or Decimal(1500)
    if not dep_enabled or user.balance >= dep_amount:
        return await seller_menu(bot, s, user, src)
    cards = await _cards(s, user.id)
    today = await deals.seller_stats(s, user.id, deals.day_start())
    if cards and (today["n"] > 0 or user.is_online):
        await seller_menu(bot, s, user, src)
    else:
        await seller_deposit_screen(bot, s, user, src)


async def seller_deposit_screen(bot: Bot, s: AsyncSession, user: User, src=None, note: str = "") -> None:
    cards = await _cards(s, user.id)
    dep_amount = settings.dec("insurance_deposit_amount") or Decimal(1500)
    lines = [
        title(pe("lock"), "Страховой депозит · Продажа USDT"),
        f"{pe('info')} Для входа в панель мерчанта и продажи USDT на карту требуется страховой депозит.",
        quote(
            f"• Сумма депозита: <b>{money.usdt(dep_amount)} USDT</b> (${money.fmt(dep_amount)})",
            "• Сеть депозита: <b>BNB Smart Chain (BEP-20)</b>",
            "• Назначение: <b>Обеспечение безопасности P2P-сделок</b>",
        ),
        "Для продажи USDT за рубли (работы в качестве мерчанта и добавления карт) "
        f"необходимо внести страховой депозит в размере <b>{money.usdt(dep_amount)} USDT</b> (${money.fmt(dep_amount)}).\n\n"
        "Депозит замораживается на вашем балансе в качестве гарантии и защищает участников "
        "P2P-сделок от неисполнения обязательств.\n\n"
        "Вы также можете сразу создать сделку на продажу USDT с указанием суммы "
        "и реквизитов вашей карты.\n\n"
        "Чтобы получить персональный адрес для оплаты, нажмите кнопку <b>«Оплатить»</b> ниже "
        "или напишите в чат сообщение <b>«Оплатить»</b>.\n\n"
        f"Подробно — в {manual('инструкции')}.",
    ]
    await show(bot, user, "\n".join(lines) + note, kb(
        btn("Оплатить", "sl:dep_pay", "card", style="success"),
        btn("Создать сделку на продажу", "sl:create_deal", "plus", style="primary"),
        btn("Панель мерчанта", "sl:panel", "card") if cards else None,
        back("menu", "В меню"),
    ), src)


async def seller_deposit_pay_screen(bot: Bot, s: AsyncSession, user: User, src=None, note: str = "") -> None:
    if not bsc.ready():
        return await seller_deposit_screen(bot, s, user, src, warn(bsc.OFF))
    dep_amount = settings.dec("insurance_deposit_amount") or Decimal(1500)
    addr = await bsc.deposit_address(s, user.id)
    lines = [
        title(pe("plus"), "Оплата страхового депозита · BEP-20"),
        quote(
            f"• Сумма к оплате: <b>{money.usdt(dep_amount)} USDT</b>",
            "• Сеть: <b>BNB Smart Chain (BEP-20)</b>",
            "• Монета: <b>USDT</b>",
        ),
        section("key", "Адрес BEP-20 для оплаты"),
        f"<code>{addr}</code>",
        "",
        quote("Переводите только <b>USDT в сети BNB Smart Chain (BEP-20)</b> — другая монета или сеть не зачислится. "
              "На бирже или в кошельке выбирайте сеть «BSC (BEP20)»."),
        f"Адрес закреплён за вашим аккаунтом. После перевода <b>{money.usdt(dep_amount)} USDT</b> депозит зачислится автоматически "
        "в течение нескольких секунд после подтверждения транзакции в сети BSC.\n\n"
        "После пополнения вы можете выставить сделку на продажу нужной суммы USDT на свои реквизиты.",
    ]
    await show(bot, user, "\n".join(lines) + note, kb(
        btn("Скопировать адрес", icon="key", copy=addr, style="primary"),
        btn("Создать сделку на продажу", "sl:create_deal", "plus"),
        [btn("Адрес в BscScan", icon="search", url=bsc.address_url(addr)), app_btn("В приложении", "deposit")],
        back("sl", "Назад к условиям"),
    ), src)


@router.callback_query(F.data == "sl")
async def cb_seller(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    await open_seller(bot, s, user, c)


@router.callback_query(F.data.in_({"sl:deposit", "sl:dep_info"}))
async def cb_seller_deposit_info(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    await seller_deposit_screen(bot, s, user, c)


@router.callback_query(F.data == "sl:dep_pay")
async def cb_seller_deposit_pay(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    await seller_deposit_pay_screen(bot, s, user, c)


@router.callback_query(F.data.in_({"sl:panel", "sl:menu"}))
async def cb_seller_panel(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    await seller_menu(bot, s, user, c)


@router.message(F.text.regexp(r"(?i)^(оплатить|оплата|pay)(?:\s.*)?$"))
async def msg_pay_deposit(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    with suppress(TelegramAPIError):
        await m.delete()
    await seller_deposit_pay_screen(bot, s, user)


@router.message(F.text.regexp(r"(?i)^(продать|продать\s+юсдт|продать\s+usdt|продать\s+юсдт\s+в\s+руб)(?:\s.*)?$"))
async def msg_sell_trigger(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    with suppress(TelegramAPIError):
        await m.delete()
    await open_seller(bot, s, user)


def _shift_note(user: User, cards: list[Card], busy: dict, used: dict) -> str:
    if not any(deals.card_visibility(cd, user, busy.get(cd.id), used.get(cd.id, Decimal(0)))[0] for cd in cards):
        return warn("Вы на смене, но ни одна карта сейчас не видна покупателям — откройте карту и посмотрите причину.")
    return ok("Вы на смене. Новые сделки придут уведомлением.")


@router.callback_query(F.data.regexp(r"^sl:on:([01])$"))
async def cb_toggle_online(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    user.is_online = c.data.endswith("1")  # explicit target state: a double tap cannot flip it back
    if user.is_online:
        cards = await _cards(s, user.id)
        note = _shift_note(user, cards, await deals.busy_cards(s, user.id, full=True),
                           await deals.used_today(s, [cd.id for cd in cards]))
    else:
        note = ok("Смена завершена, карты скрыты. Открытые сделки продолжаются — завершите их.")
    await seller_menu(bot, s, user, c, note)


@router.callback_query(F.data == "sl:alloff")
async def cb_all_off(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    for card in await _cards(s, user.id):
        card.is_active = False
    await seller_menu(bot, s, user, c, ok("Все карты выключены. Открытые сделки продолжаются."))


async def own_card(s: AsyncSession, user: User, card_id: str) -> Card | None:
    card = await s.get(Card, int(card_id))
    return card if card and card.user_id == user.id and not card.is_deleted else None


async def card_screen(bot: Bot, s: AsyncSession, user: User, card: Card, src=None, note: str = "") -> None:
    full = (await deals.busy_cards(s, user.id, full=True)).get(card.id)
    busy = (await deals.busy_cards(s, user.id)).get(card.id)  # any open deal: the requisites stay as they are
    load = (await deals.card_load(s, [card.id]))[card.id]
    used = (await deals.used_today(s, [card.id]))[card.id]
    visible, why = deals.card_visibility(card, user, full, used)
    total_n, total_rub = (await s.execute(select(func.count(Deal.id), func.coalesce(func.sum(Deal.amount_rub), 0))
                                          .where(Deal.card_id == card.id, Deal.status == "completed"))).one()
    state = (f"{pe('ban')} заблокирована администрацией" if card.is_banned
             else f"{pe('ok')} включена" if card.is_active else f"{pe('pause')} выключена")
    daily = (f"{money.fmt(used)} из {money.fmt(card.daily_limit_rub)} ₽" if card.daily_limit_rub is not None
             else f"{money.fmt(used)} ₽, дневной лимит не задан")
    text = "\n".join([
        title(pe("sbp" if card.kind == "sbp" else "card"),
              f"{esc(card.bank)} · {'СБП' if card.kind == 'sbp' else 'карта'} {mask(card)}"),
        f"{pe('ok' if visible else 'pause')} <b>{'Видна' if visible else 'Не видна'} покупателям</b>"
        + (f" · {why}" if visible else f": {why}"),
        "",
        quote(
            f"{pe('bank')} Банк: <b>{esc(card.bank)}</b>",
            f"{pe('key')} {'Номер карты' if card.kind == 'card' else 'Телефон СБП'}: <code>{esc(card.requisites)}</code>",
            f"{pe('profile')} Получатель: <b>{esc(card.holder)}</b>",
            f"{pe('ruble')} Сумма одной сделки: <b>{money.fmt(card.min_rub)} – {money.fmt(card.max_rub)} ₽</b>",
            f"{pe('clock')} Сегодня принято: <b>{daily}</b>",
            f"{pe('stats')} Всего завершено: <b>{total_n}</b> сделок на <b>{money.fmt(Decimal(total_rub))} ₽</b>",
            f"Состояние: {state}" + ("" if user.is_online else " · вы не на смене"),
        ),
        f"{pe('fire')} По карте идёт сделок: <b>{len(load)}</b> из {settings.get('card_parallel')} — суммы "
        f"{', '.join(money.fmt(a) for a in sorted(load))} ₽. Сверяйте переводы по сумме; реквизиты менять нельзя, "
        "пока сделки идут." if busy
        else "Реквизиты увидит только покупатель, создавший сделку." if not card.is_banned
        else "Разблокировать карту может только администрация — напишите в поддержку.",
    ]) + note
    toggle = None
    if not card.is_banned:
        toggle = (btn("Выключить карту", f"cd:off:{card.id}", "pause") if card.is_active
                  else btn("Включить карту", f"cd:on:{card.id}", "ok", style="success"))
    others_on = card.is_active and await s.scalar(select(exists().where(
        Card.user_id == user.id, Card.id != card.id, Card.is_active, ~Card.is_deleted)))
    await show(bot, user, text, kb(
        btn(f"Открыть сделку #{busy}", f"dl:{busy}", "fire", style="primary") if busy else None,
        toggle,
        btn("Выйти на смену", f"cd:shift:{card.id}", "live", style="success")
        if card.is_active and not user.is_online and not card.is_banned else None,
        btn("Только эта карта", f"cd:solo:{card.id}", "star") if others_on else None,
        [btn("Минимум", f"ce:min:{card.id}", "down"), btn("Максимум", f"ce:max:{card.id}", "up"),
         btn("Лимит в день", f"ce:daily:{card.id}", "clock")],
        [btn("Банк", f"ce:bank:{card.id}", "bank"), btn("Получатель", f"ce:holder:{card.id}", "profile"),
         btn("Номер" if card.kind == "card" else "Телефон", f"ce:req:{card.id}", "key")],
        btn("Удалить карту", f"cd:del:{card.id}", "trash"),
        back("sl", "Панель мерчанта"),
    ), src)


@router.callback_query(F.data.regexp(r"^cd:(\d+)$"))
async def cb_card(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    card = await own_card(s, user, c.data.split(":")[1])
    if card:
        await card_screen(bot, s, user, card, c)
    else:
        await seller_menu(bot, s, user, c)


@router.callback_query(F.data.regexp(r"^cd:(on|off|shift|solo):(\d+)$"))
async def cb_card_toggle(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    _, act, cid = c.data.split(":")
    card = await own_card(s, user, cid)
    if not card or card.is_banned:
        return await c.answer("Карта недоступна", show_alert=True)
    if act != "off" and (problem := deals.flow_problem(card, user)):
        return await c.answer(f"Нельзя поставить в поток: {problem}", show_alert=True)
    if act == "off":
        card.is_active = False
        return await card_screen(bot, s, user, card, c, ok("Карта выключена и скрыта. Открытая сделка по ней продолжится."))
    card.is_active = True
    if act == "solo":
        for other in await _cards(s, user.id):
            if other.id != card.id:
                other.is_active = False
    was_online, user.is_online = user.is_online, True  # "in work" means buyers can see it: start the shift
    msg = {"on": "Карта включена", "shift": "Вы на смене", "solo": "В работе только эта карта"}[act]
    await card_screen(bot, s, user, card, c, ok(msg + ("" if was_online else " — вы вышли на смену")))


@router.callback_query(F.data.regexp(r"^cd:del:(\d+)$"))
async def cb_card_del(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    card = await own_card(s, user, c.data.split(":")[2])
    if not card:
        return await seller_menu(bot, s, user, c)
    await show(bot, user, f"{pe('warn')} <b>Удалить {esc(card_label(card))}?</b>\n\n"
                          "Карта исчезнет из списка. История сделок сохранится. Отменить удаление нельзя — "
                          "реквизиты можно будет добавить заново.", kb(
        btn("Удалить", f"cd:del2:{card.id}", "trash", style="danger"), back(f"cd:{card.id}", "Отмена", "back"),
    ), c)


@router.callback_query(F.data.regexp(r"^cd:del2:(\d+)$"))
async def cb_card_del2(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    card = await own_card(s, user, c.data.split(":")[2])
    if card:
        card = await s.get(Card, card.id, with_for_update=True, populate_existing=True)
        if card.is_deleted:
            return await seller_menu(bot, s, user, c)
        busy = await s.scalar(select(exists().where(Deal.card_id == card.id, Deal.status.in_(deals.OPEN))))
        if busy:
            return await c.answer("По карте есть открытая сделка — удалите после её завершения", show_alert=True)
        card.is_deleted = True
        card.is_active = False
    await seller_menu(bot, s, user, c, ok("Карта удалена"))


# ---------- validation shared by "add" and "edit" ----------

def check_requisites(kind: str, raw: str) -> tuple[str | None, str]:
    digits = re.sub(r"\D", "", raw)
    if kind == "card":
        if 16 <= len(digits) <= 19 and luhn(digits):
            return digits, ""
        return None, "Номер карты не прошёл проверку — проверьте цифры"
    if len(digits) == 11 and digits[0] in "78":
        digits = "7" + digits[1:]
    if len(digits) == 11 and digits[0] == "7":
        return "+" + digits, ""
    return None, "Нужен российский номер: +7 и 10 цифр"


async def requisites_taken(s: AsyncSession, value: str, user: User, except_id: int | None = None) -> str:
    q = select(Card.user_id).where(Card.requisites == value, ~Card.is_deleted)
    if except_id:
        q = q.where(Card.id != except_id)
    owner = await s.scalar(q.limit(1))
    if owner is None:
        return ""
    # one requisite = one seller: duplicates cause parallel deals on one card and are a fraud signal
    return ("Эти реквизиты уже добавлены — найдите их в «Мои карты»" if owner == user.id
            else "Эти реквизиты уже используются другим продавцом. Если это ваша карта — напишите в поддержку")


def check_holder(raw: str) -> str | None:
    fio = " ".join(raw.split())
    # "Иванов Иван", "Иван Иванович И." — as banks show the recipient
    ok_ = 5 <= len(fio) <= 100 and len(fio.split()) >= 2 and all(
        p.replace("-", "").rstrip(".").isalpha() for p in fio.split())
    return fio if ok_ else None


def check_bank(raw: str) -> str | None:
    bank = " ".join(raw.split())
    return bank if 2 <= len(bank) <= 40 else None


# ---------- edit one field of a card ----------

FIELDS = {
    "min": ("Минимум одной сделки", "Отправьте минимальную сумму одной сделки в ₽, например <code>1000</code>."),
    "max": ("Максимум одной сделки", "Отправьте максимальную сумму одной сделки в ₽, например <code>50000</code>."),
    "daily": ("Лимит в день", "Отправьте, сколько ₽ карта может принять за день (лимит банка), например "
                              "<code>300000</code>. <code>0</code> — без лимита. Сброс в 00:00 МСК."),
    "bank": ("Банк", "Выберите банк или отправьте название сообщением."),
    "holder": ("Получатель", "Отправьте ФИО получателя так, как его показывает банк при переводе."),
    "req": ("Реквизиты", ""),
}
LOCKED = ("bank", "holder", "req")  # the buyer of an open deal sees them


class CardEdit(StatesGroup):
    value = State()


def _edit_text(card: Card, field: str, err: str = "") -> str:
    name, hint = FIELDS[field]
    current = {"min": f"{money.fmt(card.min_rub)} ₽", "max": f"{money.fmt(card.max_rub)} ₽",
               "daily": f"{money.fmt(card.daily_limit_rub)} ₽" if card.daily_limit_rub is not None else "не задан",
               "bank": esc(card.bank), "holder": esc(card.holder), "req": f"<code>{esc(card.requisites)}</code>"}[field]
    if field == "req":
        hint = ("Отправьте новый номер карты (16–19 цифр)." if card.kind == "card"
                else "Отправьте новый номер телефона для СБП (+7…).")
    return "\n".join([title(pe("pencil"), f"{name} · {esc(card.bank)} {mask(card)}"), "",
                      f"Сейчас: <b>{current}</b>", "", hint]) + (warn(err) if err else "")


def _edit_kb(card: Card, field: str):
    rows = []
    if field == "bank":
        rows = [[btn(b, f"ceb:{card.id}:{i + j}", "bank") for j, b in enumerate(BANKS[i:i + 2])]
                for i in range(0, len(BANKS), 2)]
    return kb(*rows, back(f"cd:{card.id}", "Отмена", "cross"))


@router.callback_query(F.data.regexp(r"^ce:(min|max|daily|bank|holder|req):(\d+)$"))
async def cb_edit(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    _, field, cid = c.data.split(":")
    card = await own_card(s, user, cid)
    if not card:
        return await seller_menu(bot, s, user, c)
    busy = (await deals.busy_cards(s, user.id)).get(card.id)
    if field in LOCKED and busy:
        return await c.answer(f"По карте идёт сделка #{busy}: покупатель видит эти реквизиты. "
                              "Изменить можно после её завершения.", show_alert=True)
    await state.set_state(CardEdit.value)
    await state.update_data(card_id=card.id, field=field)
    await show(bot, user, _edit_text(card, field), _edit_kb(card, field), c)


async def _save(bot, s, user, state, card: Card, field: str, raw: str, src=None):
    value, err = await check_field(s, user, card, field, raw)
    if err == BUSY:
        await state.clear()
        return await card_screen(bot, s, user, card, src, warn(err))
    if err:
        return await show(bot, user, _edit_text(card, field, err), _edit_kb(card, field), src)
    await state.clear()
    set_field(card, field, value)
    await card_screen(bot, s, user, card, src, ok(f"{FIELDS[field][0]}: сохранено. Открытые сделки не меняются."))


BUSY = "По карте началась сделка — изменение не сохранено."


async def check_field(s: AsyncSession, user: User, card: Card, field: str, raw: str):
    """(value, "") or (None, why) for one field of a card — the bot and the mini app alike."""
    if field in LOCKED and (await deals.busy_cards(s, user.id)).get(card.id):
        return None, BUSY
    err, value = "", None
    if field in ("min", "max", "daily"):
        v = Decimal(0) if field == "daily" and raw.strip() == "0" else parse_rub(raw)
        if v is None:
            err = "Нужно число, например 5000"
        elif field == "min" and v > card.max_rub:
            err = f"Минимум не может быть больше максимума ({money.fmt(card.max_rub)} ₽)"
        elif field == "max" and v < card.min_rub:
            err = f"Максимум не может быть меньше минимума ({money.fmt(card.min_rub)} ₽)"
        elif field == "daily" and v and v < card.min_rub:
            err = f"Дневной лимит меньше минимума сделки ({money.fmt(card.min_rub)} ₽) — карта не будет видна"
        value = v
    elif field == "bank":
        value = check_bank(raw)
        err = "" if value else "Название банка — от 2 до 40 символов"
    elif field == "holder":
        value = check_holder(raw)
        err = "" if value else "Укажите ФИО буквами, минимум 2 слова"
    else:
        value, err = check_requisites(card.kind, raw)
        if value and value != card.requisites:
            err = await requisites_taken(s, value, user, except_id=card.id)
    return (None, err) if err else (value, "")


def set_field(card: Card, field: str, value) -> None:
    attr = {"min": "min_rub", "max": "max_rub", "daily": "daily_limit_rub", "bank": "bank", "holder": "holder",
            "req": "requisites"}[field]
    setattr(card, attr, value or None if field == "daily" else value)


@router.message(CardEdit.value, F.text)
async def msg_edit(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    data = await state.get_data()
    card = await own_card(s, user, data["card_id"])
    if not card:
        await state.clear()
        return await seller_menu(bot, s, user)
    await _save(bot, s, user, state, card, data["field"], m.text)


@router.callback_query(CardEdit.value, F.data.regexp(r"^ceb:(\d+):(\d+)$"))
async def cb_edit_bank(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    _, cid, idx = c.data.split(":")
    card = await own_card(s, user, cid)
    if not card or int(idx) >= len(BANKS):
        return await c.answer("Банк недоступен", show_alert=True)
    await _save(bot, s, user, state, card, "bank", BANKS[int(idx)], c)


# ---------- add card ----------

def _step(n: int, text: str) -> str:
    return f"{title(pe('plus'), 'Новые реквизиты')} · шаг {n} из 6\n\n{text}"


async def _ask(bot, user, state: FSMContext, text: str, markup=None, src=None, err: str = ""):
    await show(bot, user, text + (warn(err) if err else ""), markup or kb(back("sl", "Отмена", "cross")), src)


@router.callback_query(F.data == "sl:add")
async def cb_add(c: CallbackQuery, bot: Bot, user: User):
    await show(bot, user, _step(1, "Куда покупатели будут переводить рубли?\n\n"
                                   "<b>Карта</b> — перевод по номеру карты. <b>СБП</b> — перевод по номеру телефона.\n"
                                   f"Все шаги и настройки карты — в {manual('инструкции')}."), kb(
        [btn("Банковская карта", "sl:add:card", "card"), btn("СБП по телефону", "sl:add:sbp", "sbp")],
        back("sl", "Отмена", "cross"),
    ), c)


def _bank_kb():
    rows = [[btn(b, f"sl:bank:{i + j}", "bank") for j, b in enumerate(BANKS[i:i + 2])]
            for i in range(0, len(BANKS), 2)]
    return kb(*rows, back("sl", "Отмена", "cross"))


@router.callback_query(F.data.regexp(r"^sl:add:(card|sbp)$"))
async def cb_add_kind(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    await state.set_state(AddCard.bank)
    await state.update_data(kind=c.data.split(":")[2])
    await _ask(bot, user, state, _step(2, "Выберите банк или отправьте название сообщением:"), _bank_kb(), c)


async def _after_bank(bot, user, state: FSMContext, bank: str, src=None):
    await state.update_data(bank=bank)
    await state.set_state(AddCard.requisites)
    kind = (await state.get_data())["kind"]
    ask = ("Отправьте номер карты — 16–19 цифр, можно с пробелами:" if kind == "card"
           else "Отправьте номер телефона, привязанный к СБП, например <code>+79001234567</code>:")
    await _ask(bot, user, state, _step(3, f"Банк: <b>{esc(bank)}</b>\n\n{ask}"), src=src)


@router.callback_query(AddCard.bank, F.data.startswith("sl:bank:"))
async def cb_bank(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    idx = c.data.split(":")[2]
    if not idx.isdigit() or int(idx) >= len(BANKS):
        return await c.answer("Банк недоступен", show_alert=True)
    await _after_bank(bot, user, state, BANKS[int(idx)], c)


@router.message(AddCard.bank, F.text)
async def msg_bank(m: Message, bot: Bot, user: User, state: FSMContext):
    bank = check_bank(m.text)
    if not bank:
        return await _ask(bot, user, state, _step(2, "Выберите банк:"), _bank_kb(), err="Название 2–40 символов")
    await _after_bank(bot, user, state, bank)


@router.message(AddCard.requisites, F.text)
async def msg_requisites(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    data = await state.get_data()
    value, err = check_requisites(data["kind"], m.text)
    if not value:
        return await _ask(bot, user, state, _step(3, "Отправьте реквизиты ещё раз:"), err=err)
    if err := await requisites_taken(s, value, user):
        return await _ask(bot, user, state, _step(3, "Отправьте другие реквизиты:"), err=err)
    await state.update_data(requisites=value)
    await state.set_state(AddCard.holder)
    await _ask(bot, user, state, _step(4, "Отправьте ФИО получателя так, как его видит отправитель при переводе, "
                                          "например <code>Иван Иванович И.</code> — покупатель сверит его в банке:"))


@router.message(AddCard.holder, F.text)
async def msg_holder(m: Message, bot: Bot, user: User, state: FSMContext):
    fio = check_holder(m.text)
    if not fio:
        return await _ask(bot, user, state, _step(4, "Отправьте ФИО получателя:"), err="Укажите ФИО буквами, минимум 2 слова")
    await state.update_data(holder=fio)
    await state.set_state(AddCard.min)
    await _ask(bot, user, state, _step(5, "Отправьте <b>минимальную</b> сумму одной сделки в ₽, например <code>1000</code>:"))


@router.message(AddCard.min, F.text)
async def msg_min(m: Message, bot: Bot, user: User, state: FSMContext):
    v = parse_rub(m.text)
    if v is None:
        return await _ask(bot, user, state, _step(5, "Отправьте минимальную сумму в ₽:"), err="Нужно число, например 1000")
    await state.update_data(min=str(v))
    await state.set_state(AddCard.max)
    await _ask(bot, user, state, _step(6, f"Минимум: <b>{money.fmt(v)} ₽</b>\n\nОтправьте <b>максимальную</b> "
                                          "сумму одной сделки в ₽. Больше свободного баланса покупатель всё равно "
                                          "не увидит:"))


@router.message(AddCard.max, F.text)
async def msg_max(m: Message, bot: Bot, user: User, state: FSMContext):
    data = await state.get_data()
    lo, hi = Decimal(data["min"]), parse_rub(m.text)
    if hi is None or hi < lo:
        return await _ask(bot, user, state, _step(6, "Отправьте максимальную сумму в ₽:"),
                          err=f"Максимум должен быть не меньше {money.fmt(lo)} ₽")
    await state.update_data(max=str(hi))
    await state.set_state(AddCard.confirm)
    card = Card(kind=data["kind"], bank=data["bank"], requisites=data["requisites"], holder=data["holder"],
                min_rub=lo, max_rub=hi)
    await show(bot, user, "\n".join([
        title(pe("doc"), "Проверьте реквизиты"),
        "",
        quote(f"{pe('bank')} Банк: <b>{esc(card.bank)}</b>",
              f"{pe('key')} {'Номер карты' if card.kind == 'card' else 'Телефон СБП'}: <code>{esc(card.requisites)}</code>",
              f"{pe('profile')} Получатель: <b>{esc(card.holder)}</b>",
              f"{pe('ruble')} Сумма одной сделки: <b>{money.fmt(lo)} – {money.fmt(hi)} ₽</b>"),
        f"{pe('warn')} Покупатели будут переводить рубли именно сюда. Ошибка в цифрах — деньги уйдут чужому "
        "человеку, вернуть их будет сложно.",
    ]), kb(btn("Сохранить", "sl:save", "ok", style="success"),
           btn("Заполнить заново", "sl:add", "pencil"), back("sl", "Отмена", "cross")))


@router.callback_query(AddCard.confirm, F.data == "sl:save")
async def cb_save(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    if err := await requisites_taken(s, data["requisites"], user):  # added meanwhile (e.g. from another device)
        return await seller_menu(bot, s, user, c, warn(err))
    card = Card(user_id=user.id, kind=data["kind"], bank=data["bank"], requisites=data["requisites"],
                holder=data["holder"], min_rub=Decimal(data["min"]), max_rub=Decimal(data["max"]))
    problem = deals.flow_problem(card, user)
    card.is_active = not problem  # in the flow only with a balance behind it
    s.add(card)
    await s.flush()
    events.add(s, f"card:{card.id}", "added", f"Новая карта {card.bank} {mask(card)}, "
               f"{money.fmt(card.min_rub)}–{money.fmt(card.max_rub)} ₽", user.id, notice=True)
    if problem:
        return await card_screen(bot, s, user, card, c, note=warn(f"Реквизиты сохранены, но не в потоке: {problem}."))
    was_online = user.is_online
    user.is_online = True  # a new card is meant to work right away
    await card_screen(bot, s, user, card, c, note=ok("Реквизиты сохранены и включены"
                                                     + ("" if was_online else ", вы вышли на смену")
                                                     + ". Лимит банка на входящие за день — кнопка «Лимит в день»."))


# ---------- create sell deal with card & deposit ----------

def _csd_step(n: int, text: str) -> str:
    return f"{title(pe('plus'), 'Сделка на продажу USDT')} · шаг {n} из 5\n\n{text}"


def _csd_bank_kb():
    rows = [[btn(b, f"sl:csd_b:{i + j}", "bank") for j, b in enumerate(BANKS[i:i + 2])]
            for i in range(0, len(BANKS), 2)]
    return kb(*rows, back("sl", "Отмена", "cross"))


@router.callback_query(F.data == "sl:create_deal")
async def cb_create_sell_deal(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    await state.set_state(CreateSellDeal.amount)
    rate = settings.dec("rate")
    dep_enabled = settings.get("insurance_deposit_enabled") != "0"
    dep_amount = settings.dec("insurance_deposit_amount") or Decimal(1500)
    has_dep = user.balance >= dep_amount
    dep_str = (("Активен" if has_dep else f"Требуется {money.usdt(dep_amount)} USDT")
               if dep_enabled else "Не требуется")
    text = _csd_step(1, "\n".join([
        "Укажите сумму в <b>USDT</b>, которую вы хотите выставить на продажу.",
        "",
        quote(
            f"Текущий курс: <b>{money.fmt(rate)} ₽</b> за 1 USDT",
            f"Страховой депозит: <b>{dep_str}</b>",
            f"Доступно на балансе: <b>{money.usdt(user.balance)} USDT</b>",
        ),
        "Отправьте сумму USDT сообщением (например, <code>500</code> или <code>1500</code>):",
    ]))
    await show(bot, user, text, kb(back("sl", "Отмена", "cross")), c)


@router.message(F.text.regexp(r"(?i)^(создать сделку|создать заявку|продать на сумму)(?:\s.*)?$"))
async def msg_create_sell_deal_trigger(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    with suppress(TelegramAPIError):
        await m.delete()
    await state.set_state(CreateSellDeal.amount)
    rate = settings.dec("rate")
    dep_enabled = settings.get("insurance_deposit_enabled") != "0"
    dep_amount = settings.dec("insurance_deposit_amount") or Decimal(1500)
    has_dep = user.balance >= dep_amount
    dep_str = (("Активен" if has_dep else f"Требуется {money.usdt(dep_amount)} USDT")
               if dep_enabled else "Не требуется")
    text = _csd_step(1, "\n".join([
        "Укажите сумму в <b>USDT</b>, которую вы хотите выставить на продажу.",
        "",
        quote(
            f"Текущий курс: <b>{money.fmt(rate)} ₽</b> за 1 USDT",
            f"Страховой депозит: <b>{dep_str}</b>",
            f"Доступно на балансе: <b>{money.usdt(user.balance)} USDT</b>",
        ),
        "Отправьте сумму USDT сообщением (например, <code>500</code> или <code>1500</code>):",
    ]))
    await show(bot, user, text, kb(back("sl", "Отмена", "cross")))


@router.message(CreateSellDeal.amount, F.text)
async def msg_csd_amount(m: Message, bot: Bot, user: User, state: FSMContext):
    raw = m.text.replace(" ", "").replace(",", ".").replace("$", "").strip()
    try:
        val = Decimal(raw)
    except Exception:
        val = None
    if val is None or not val.is_finite() or val <= 0 or val > Decimal("10000000"):
        return await show(bot, user, _csd_step(1, "Введите корректную сумму USDT для продажи:\n\n"
                          + warn("Сумма должна быть положительным числом, например 500")),
                          kb(back("sl", "Отмена", "cross")))
    amount_usdt = val.quantize(Decimal("0.01"))
    rate = settings.dec("rate")
    amount_rub = (amount_usdt * rate).quantize(Decimal("0.01"))
    await state.update_data(amount_usdt=str(amount_usdt), amount_rub=str(amount_rub))
    await state.set_state(CreateSellDeal.bank)
    text = _csd_step(2, "\n".join([
        quote(
            f"Сумма к продаже: <b>{money.usdt(amount_usdt)} USDT</b>",
            f"Сумма к получению: <b>{money.fmt(amount_rub)} ₽</b>",
            f"Курс: <b>{money.fmt(rate)} ₽</b>",
        ),
        "Выберите банк для получения рублей на карту/СБП или отправьте его название:",
    ]))
    await show(bot, user, text, _csd_bank_kb())


async def _after_csd_bank(bot: Bot, user: User, state: FSMContext, bank: str, src=None):
    await state.update_data(bank=bank)
    await state.set_state(CreateSellDeal.requisites)
    data = await state.get_data()
    amount_usdt = Decimal(data["amount_usdt"])
    amount_rub = Decimal(data["amount_rub"])
    text = _csd_step(3, "\n".join([
        quote(
            f"Сумма: <b>{money.usdt(amount_usdt)} USDT</b> (~{money.fmt(amount_rub)} ₽)",
            f"Банк: <b>{esc(bank)}</b>",
        ),
        "Отправьте номер банковской карты (16–19 цифр) или номер телефона СБП (+7...):",
    ]))
    await show(bot, user, text, kb(back("sl", "Отмена", "cross")), src)


@router.callback_query(CreateSellDeal.bank, F.data.startswith("sl:csd_b:"))
async def cb_csd_bank(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    idx = c.data.split(":")[2]
    if not idx.isdigit() or int(idx) >= len(BANKS):
        return await c.answer("Банк недоступен", show_alert=True)
    await _after_csd_bank(bot, user, state, BANKS[int(idx)], c)


@router.message(CreateSellDeal.bank, F.text)
async def msg_csd_bank(m: Message, bot: Bot, user: User, state: FSMContext):
    bank = check_bank(m.text)
    if not bank:
        return await show(bot, user, _csd_step(2, "Выберите банк:\n\n"
                          + warn("Название банка — от 2 до 40 символов")),
                          _csd_bank_kb())
    await _after_csd_bank(bot, user, state, bank)


@router.message(CreateSellDeal.requisites, F.text)
async def msg_csd_requisites(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    raw = m.text.strip()
    digits = re.sub(r"\D", "", raw)
    kind, value, err = None, None, ""
    if len(digits) == 11 and digits[0] in "78":
        kind = "sbp"
        value = "+7" + digits[1:]
    elif 16 <= len(digits) <= 19 and luhn(digits):
        kind = "card"
        value = digits
    elif len(digits) in (16, 17, 18, 19):
        kind = "card"
        err = "Номер карты не прошёл проверку — проверьте цифры"
    else:
        err = "Отправьте 16–19 цифр номера карты или номер телефона СБП (+7...)"

    if err or not value:
        return await show(bot, user, _csd_step(3, "Отправьте реквизиты ещё раз:\n\n" + warn(err or "Неверный формат")),
                          kb(back("sl", "Отмена", "cross")))

    q = select(Card.user_id).where(Card.requisites == value, ~Card.is_deleted)
    owner = await s.scalar(q.limit(1))
    if owner is not None and owner != user.id:
        return await show(bot, user, _csd_step(3, "Отправьте другие реквизиты:\n\n"
                          + warn("Эти реквизиты уже используются другим продавцом.")),
                          kb(back("sl", "Отмена", "cross")))

    await state.update_data(kind=kind, requisites=value)
    await state.set_state(CreateSellDeal.holder)
    data = await state.get_data()
    text = _csd_step(4, "\n".join([
        quote(
            f"Банк: <b>{esc(data['bank'])}</b>",
            f"Реквизиты: <code>{esc(value)}</code> ({'карта' if kind == 'card' else 'СБП'})",
        ),
        "Отправьте ФИО получателя перевода (как видит банк, например <code>Иван Иванович И.</code>):",
    ]))
    await show(bot, user, text, kb(back("sl", "Отмена", "cross")))


@router.message(CreateSellDeal.holder, F.text)
async def msg_csd_holder(m: Message, bot: Bot, user: User, state: FSMContext):
    fio = check_holder(m.text)
    if not fio:
        return await show(bot, user, _csd_step(4, "Отправьте ФИО получателя:\n\n"
                          + warn("Укажите ФИО буквами, минимум 2 слова")),
                          kb(back("sl", "Отмена", "cross")))
    await state.update_data(holder=fio)
    await state.set_state(CreateSellDeal.confirm)
    data = await state.get_data()
    dep_enabled = settings.get("insurance_deposit_enabled") != "0"
    dep_amount = settings.dec("insurance_deposit_amount") or Decimal(1500)
    has_dep = user.balance >= dep_amount
    dep_str = ("Обеспечено страховым депозитом ($1 500)" if has_dep else f"Требуется страховой депозит ({money.usdt(dep_amount)} USDT)") if dep_enabled else "Страховой депозит отключён"

    text = "\n".join([
        title(pe("doc"), "Проверьте данные сделки на продажу"),
        "",
        quote(
            f"Сумма к продаже: <b>{money.usdt(Decimal(data['amount_usdt']))} USDT</b> (~{money.fmt(Decimal(data['amount_rub']))} ₽)",
            f"Банк: <b>{esc(data['bank'])}</b>",
            f"Реквизиты: <code>{esc(data['requisites'])}</code>",
            f"Получатель: <b>{esc(fio)}</b>",
            f"Статус депозита: <b>{dep_str}</b>",
        ),
        f"{pe('warn')} Покупатели будут переводить рубли на эти реквизиты. "
        "Проверьте правильность номера карты и ФИО перед подтверждением.",
    ])
    await show(bot, user, text, kb(
        btn("Подтвердить и опубликовать", "sl:csd_save", "ok", style="success"),
        btn("Оплатить депозит", "sl:dep_pay", "card", style="primary")
        if dep_enabled and not has_dep else None,
        back("sl", "Отмена", "cross"),
    ))


@router.callback_query(CreateSellDeal.confirm, F.data == "sl:csd_save")
async def cb_csd_save(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    if not data or "requisites" not in data:
        return await seller_menu(bot, s, user, c)

    amount_usdt = Decimal(data["amount_usdt"])
    amount_rub = Decimal(data["amount_rub"])

    q = select(Card).where(Card.user_id == user.id, Card.requisites == data["requisites"], ~Card.is_deleted)
    card = await s.scalar(q.limit(1))
    if card:
        card.bank = data["bank"]
        card.holder = data["holder"]
        card.max_rub = max(card.max_rub, amount_rub)
        card.is_active = True
    else:
        card = Card(
            user_id=user.id,
            kind=data["kind"],
            bank=data["bank"],
            requisites=data["requisites"],
            holder=data["holder"],
            min_rub=min(amount_rub, Decimal("1000")),
            max_rub=amount_rub,
            is_active=True,
        )
        s.add(card)

    user.is_online = True
    await s.flush()

    events.add(s, f"card:{card.id}", "sell_deal_created",
               f"Создана сделка на продажу {money.usdt(amount_usdt)} USDT ({money.fmt(amount_rub)} ₽)",
               user.id, notice=True)

    dep_enabled = settings.get("insurance_deposit_enabled") != "0"
    dep_amount = settings.dec("insurance_deposit_amount") or Decimal(1500)
    has_dep = user.balance >= dep_amount

    lines = [
        title(pe("ok"), "Сделка на продажу опубликована"),
        "",
        quote(
            f"Сумма: <b>{money.usdt(amount_usdt)} USDT</b> (~{money.fmt(amount_rub)} ₽)",
            f"Банк: <b>{esc(card.bank)}</b>",
            f"Реквизиты: <code>{esc(card.requisites)}</code>",
            f"Получатель: <b>{esc(card.holder)}</b>",
            f"Лимиты сделок: <b>{money.fmt(card.min_rub)} – {money.fmt(card.max_rub)} ₽</b>",
            f"Страховой депозит: <b>{'Обеспечено' if has_dep or not dep_enabled else 'Требуется оплата'}</b>",
        ),
        f"{pe('live')} <b>Вы на смене</b>. Реквизиты активны и доступны покупателям для обмена.\n"
        "Когда покупатель откроет сделку, вам придёт уведомление с деталями.",
    ]
    if dep_enabled and not has_dep:
        lines.append(f"\n{pe('warn')} Для стабильной работы не забудьте внести страховой депозит "
                     f"<b>{money.usdt(dep_amount)} USDT</b>.")

    await show(bot, user, "\n".join(lines), kb(
        btn("Панель мерчанта", "sl:panel", "card", style="primary"),
        btn("Оплатить депозит", "sl:dep_pay", "card", style="success") if dep_enabled and not has_dep else None,
        back("menu", "В главное меню"),
    ), c)
