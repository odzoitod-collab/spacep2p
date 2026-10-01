import re
from datetime import timedelta
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.emoji import back, btn, kb, pe
from bot.models import Card, Deal, User, now
from bot.services import deals, events, money, settings
from bot.ui import esc, manual, ok, quote, show, title, warn

router = Router()

BANKS = ["Сбербанк", "Т-Банк", "Альфа-Банк", "ВТБ", "Райффайзен", "Озон Банк", "Газпромбанк", "Совкомбанк"]


class AddCard(StatesGroup):
    bank = State()
    requisites = State()
    holder = State()
    min = State()
    max = State()
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
    busy = await deals.busy_cards(s, user.id)
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
        *[btn(("🟢 " if vis[c.id][0] else "⏸ ") + card_label(c), f"cd:{c.id}") for c in cards],
        btn("Добавить карту", "sl:add", "plus", style=None if cards else "primary"),
        [btn(f"В работе ({len(todo)})" if todo else "В работе", "sl:work", "fire"), btn("Статистика", "sl:st", "stats")],
        btn("Настройки", "sl:cfg", "settings"),
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


@router.callback_query(F.data == "sl")
async def cb_seller(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    await seller_menu(bot, s, user, c)


def _shift_note(user: User, cards: list[Card], busy: dict, used: dict) -> str:
    if not any(deals.card_visibility(cd, user, busy.get(cd.id), used.get(cd.id, Decimal(0)))[0] for cd in cards):
        return warn("Вы на смене, но ни одна карта сейчас не видна покупателям — откройте карту и посмотрите причину.")
    return ok("Вы на смене. Новые сделки придут уведомлением.")


@router.callback_query(F.data.regexp(r"^sl:on:([01])$"))
async def cb_toggle_online(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    user.is_online = c.data.endswith("1")  # explicit target state: a double tap cannot flip it back
    if user.is_online:
        cards = await _cards(s, user.id)
        note = _shift_note(user, cards, await deals.busy_cards(s, user.id),
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
    busy = (await deals.busy_cards(s, user.id)).get(card.id)
    used = (await deals.used_today(s, [card.id]))[card.id]
    visible, why = deals.card_visibility(card, user, busy, used)
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
        f"{pe('fire')} Сейчас по карте идёт сделка #{busy}: реквизиты менять нельзя до её завершения." if busy
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
    if field in LOCKED and (await deals.busy_cards(s, user.id)).get(card.id):
        await state.clear()
        return await card_screen(bot, s, user, card, src, warn("По карте началась сделка — изменение не сохранено."))
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
    if err:
        return await show(bot, user, _edit_text(card, field, err), _edit_kb(card, field), src)
    await state.clear()
    if field == "min":
        card.min_rub = value
    elif field == "max":
        card.max_rub = value
    elif field == "daily":
        card.daily_limit_rub = value or None
    elif field == "bank":
        card.bank = value
    elif field == "holder":
        card.holder = value
    else:
        card.requisites = value
    await card_screen(bot, s, user, card, src, ok(f"{FIELDS[field][0]}: сохранено. Открытые сделки не меняются."))


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
                holder=data["holder"], min_rub=Decimal(data["min"]), max_rub=Decimal(data["max"]), is_active=True)
    s.add(card)
    await s.flush()
    events.add(s, f"card:{card.id}", "added", f"Новая карта {card.bank} {mask(card)}, "
               f"{money.fmt(card.min_rub)}–{money.fmt(card.max_rub)} ₽", user.id, notice=True)
    was_online = user.is_online
    user.is_online = True  # a new card is meant to work right away
    await card_screen(bot, s, user, card, c, note=ok("Реквизиты сохранены и включены"
                                                     + ("" if was_online else ", вы вышли на смену")
                                                     + ". Лимит банка на входящие за день — кнопка «Лимит в день»."))
