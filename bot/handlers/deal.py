import io
from contextlib import suppress
from datetime import timedelta
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InputMediaDocument, InputMediaPhoto, InputMediaVideo, Message
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.emoji import back, btn, kb, pe
from bot.models import Card, Deal, User, now
from bot.handlers.seller import mask, parse_rub
from bot.services import audit, deals, events, money, settings
from bot.ui import at, clean, esc, notify, ok, person, quote, show, title, warn

router = Router()

# status -> (icon, short label). One vocabulary for buyers, sellers and admins.
STATUS = {
    "searching": ("search", "Ищем реквизиты"),
    "assigned": ("clock", "Мерчант выдаёт реквизиты"),
    "checking": ("search", "Проверка ордера"),
    "waiting_payment": ("clock", "Ждём перевод"),
    "paid": ("doc", "Чек у продавца"),
    "dispute": ("flag", "Спор"),
    "completed": ("ok", "Завершена"),
    "cancelled": ("cross", "Отменена"),
    "expired": ("cross", "Время вышло"),
    "void": ("ban", "Отменена администрацией"),
}
CLOSE_REASONS = {
    "no_merchant": "реквизиты под сумму не нашлись",
    "confirmed": "продавец подтвердил",
    "buyer_cancel": "отменил покупатель",
    "expired": "истёк срок оплаты",
    "admin_void": "отменил администратор",
    "ban_void": "участник заблокирован",
    "dispute_buyer": "спор: в пользу покупателя",
    "dispute_actual": "спор: по фактической сумме",
    "dispute_seller": "спор: в пользу продавца",
}
REASONS = {
    "not_received": "продавец: деньги не пришли",
    "wrong_amount": "продавец: пришла другая сумма",
    "seller_timeout": "продавец не ответил вовремя",
    "buyer_no_confirm": "покупатель: продавец не подтверждает",
    "seller_banned": "продавец заблокирован",
}
OPERATOR_REASONS = {"not_received": "оплата не поступила", "wrong_amount": "пришла другая сумма"}
HOW_PDF = ("В приложении банка откройте этот перевод → «Чек» или «Поделиться» → «PDF» "
           "и отправьте файл сюда.")
MAX_FILE = 20 * 1024 * 1024
PAGE = 8


class Receipt(StatesGroup):
    wait = State()


class Dispute(StatesGroup):
    amount = State()
    video = State()
    statement = State()
    proof = State()  # operator of a Bybit order: one proof settles the dispute


class Evidence(StatesGroup):
    wait = State()


def _left(d: Deal) -> float:
    return (deals.aware(d.expires_at) - now()).total_seconds()


def minutes_left(d: Deal) -> int:
    return max(0, int(_left(d) // 60))


def kind_label(card: Card) -> str:
    return "СБП" if card.kind == "sbp" else "карта"


def exact(v: Decimal) -> str:
    """Amount to type into a banking app: no spaces, kopecks only when present."""
    return str(int(v)) if v == int(v) else f"{v:.2f}"


def _money_block(d: Deal, buyer: bool) -> str:
    done, back_ = d.status == "completed", d.status in ("cancelled", "expired", "void") and not d.funds_held
    if buyer:
        return quote(
            f"{pe('ruble')} Сумма перевода: <b>{money.fmt(d.amount_rub)} ₽</b>",
            f"{pe('swap')} Курс: {money.fmt(d.buyer_rate or d.rate)} ₽ · комиссия {money.fmt(d.platform_pct, 3)}%",
            "" if back_ else f"{pe('dollar')} {'Получено' if done else 'Вы получите'}: "
                             f"<b>{money.usdt(d.buyer_credit)} USDT</b>",
        )
    if d.via_bybit:  # nothing is frozen: the USDT go through the Bybit order
        return quote(
            f"{pe('ruble')} Сумма перевода: <b>{money.fmt(d.amount_rub)} ₽</b>",
            f"{pe('shop')} Bybit-ордер: <b>{money.usdt(d.seller_debit)} USDT</b> по {money.fmt(d.merchant_rate)} ₽"
            + (f" · {_link(d)}" if d.bybit_url else ""),
        )
    frozen = (f"{pe('lock')} {'Списано с вас' if done else 'Возвращено из заморозки' if back_ else 'Удерживается' if d.funds_held else 'Заморожено у вас'}: "
              f"<b>{money.usdt(d.seller_debit)} USDT</b>")
    if d.merchant_rate is not None:  # order requisites: a fixed rate, no percent
        return quote(f"{pe('ruble')} Сумма перевода: <b>{money.fmt(d.amount_rub)} ₽</b>", frozen,
                     f"{pe('swap')} Ваш курс: <b>{money.fmt(d.merchant_rate)} ₽</b> за USDT")
    return quote(
        f"{pe('ruble')} Сумма перевода: <b>{money.fmt(d.amount_rub)} ₽</b>",
        frozen,
        f"{pe('up')} Ваш доход: <b>+{money.usdt(d.amount_rub / d.rate - d.seller_debit)} USDT</b>" if not back_ else "",
    )


def _link(d: Deal) -> str:
    return f'<a href="{esc(d.bybit_url)}">ордер</a>'


STAGES = {"searching": (1, "подбор реквизитов"), "assigned": (1, "подбор реквизитов"),
          "checking": (1, "проверка реквизитов"), "waiting_payment": (1, "перевод и чек"),
          "paid": (2, "проверка поступления")}


def deal_text(d: Deal, card: Card, viewer_id: int, note: str = "") -> str:
    icon, label = STATUS[d.status]
    buyer = viewer_id == d.buyer_id
    operator = not buyer and d.via_bybit and viewer_id == d.operator_id
    merchant = not buyer and not operator  # the seller; in a Bybit order he only watches after giving the link
    checks = not buyer and viewer_id == deals.checker(d)
    if d.status == "paid" and checks:
        label = "Ждёт вашей проверки"
    stage = STAGES.get(d.status)
    lines = [
        title(pe("fire"), f"Сделка #{d.id} · {'покупка' if buyer else 'продажа'} USDT"),
        f"{pe(icon)} <b>{label}</b>" + (f" · этап {stage[0]} из 3: {stage[1]}" if stage else ""),
        "",
        _money_block(d, buyer),
        "",
    ]
    late = d.paid_at is not None and deals.aware(d.paid_at) > deals.aware(d.expires_at)
    st = d.status
    if st == "searching":
        lines += [f"{pe('search')} Ищем ордерного мерчанта под <b>{money.fmt(d.amount_rub)} ₽</b>"
                  + (f" · перевод из {esc(d.sender_bank)}" if d.sender_bank else "") + f", до {at(d.expires_at)}.",
                  "Обычно это несколько минут. Реквизиты придут уведомлением — до этого ничего не переводите."]
    elif st == "assigned" and buyer:
        lines += [f"{pe('clock')} Мерчант взял заявку и выдаёт реквизиты (до {at(d.expires_at)}). Ничего не "
                  "переводите, пока реквизиты не появятся здесь."]
    elif st == "assigned" and d.via_bybit:
        lines += [f"{pe('fire')} <b>Вы взяли заявку.</b> Пришлите ссылку на ордер Bybit P2P до <b>{at(d.expires_at)}</b>"
                  " — иначе заявка уйдёт другим мерчантам.",
                  f"{pe('dollar')} Ордер: <b>{money.fmt(d.amount_rub)} ₽</b> = <b>{money.usdt(d.seller_debit)} USDT</b> "
                  f"по {money.fmt(d.merchant_rate)} ₽. Баланс в боте не нужен."]
    elif st == "checking" and buyer:
        lines += [f"{pe('search')} Ордер под вашу сумму найден — оператор проверяет его и выдаст реквизиты "
                  f"(до {at(d.expires_at)}). Ничего не переводите, пока реквизиты не появятся здесь."]
    elif st == "checking" and operator:
        lines += [f"{pe('shop')} Ордер {_link(d)}: зайдите на <b>{money.usdt(d.seller_debit)} USDT</b> и выдайте "
                  f"покупателю реквизиты до <b>{at(d.expires_at)}</b>."]
    elif st == "checking":
        lines += [f"{pe('clock')} Ссылка на ордер у оператора: он проверит ордер и выдаст покупателю реквизиты "
                  f"до {at(d.expires_at)}. От вас пока ничего не нужно."]
    elif st == "assigned":
        lines += [f"{pe('fire')} <b>Вы взяли заявку.</b> Выдайте реквизиты до <b>{at(d.expires_at)}</b> — иначе "
                  "заявка уйдёт другим мерчантам, а заморозка снимется.",
                  f"{pe('bank')} Покупатель переводит" + (f" из <b>{esc(d.sender_bank)}</b>" if d.sender_bank else "")
                  + f" ровно {money.fmt(d.amount_rub)} ₽.",
                  f"{pe('up')} Ваш доход: <b>+{money.usdt(d.amount_rub / d.rate - d.seller_debit)} USDT</b>"]
    elif st == "waiting_payment" and buyer:
        lines += [
            title(pe("card"), "Реквизиты для перевода"),
            quote(
                f"{pe('bank')} {esc(card.bank)} · {kind_label(card)}",
                f"{pe('key')} <code>{esc(card.requisites)}</code>",
                f"{pe('profile')} {esc(card.holder)}",
                f"{pe('ruble')} Ровно: <code>{exact(d.amount_rub)}</code> ₽",
            ),
            f"{pe('clock')} Оплатите до <b>{at(d.expires_at)}</b> (осталось {minutes_left(d)} мин).",
            f"1. Переведите <b>ровно {money.fmt(d.amount_rub)} ₽</b> одним платежом на реквизиты выше. "
            "Комментарий к переводу не пишите.",
            "2. Скачайте в банке PDF-чек этого перевода и нажмите «Прикрепить PDF-чек».",
            "Передумали или не получается перевести — отмените сделку <b>до</b> перевода.",
        ]
    elif st == "waiting_payment":
        lines += [f"{pe('clock')} Покупатель переводит на {esc(card.bank)} {mask(card)} до <b>{at(d.expires_at)}</b>.",
                  ("Чек получит оператор и проверит оплату в ордере — от вас ничего не нужно." if merchant
                   else "Пока ничего делать не нужно: когда покупатель пришлёт чек, придёт уведомление с кнопками.")
                  if d.via_bybit else
                  "Пока ничего делать не нужно: когда покупатель пришлёт чек, придёт уведомление с кнопками.",
                  "" if d.via_bybit else f"{pe('lock')} {money.usdt(d.seller_debit)} USDT заморожены под эту сделку."]
    elif st == "paid" and buyer:
        lines.append(f"{pe('doc')} Чек отправлен продавцу {at(d.paid_at)}. Он проверяет поступление в банке — "
                     "после подтверждения USDT сразу придут на баланс, мы сообщим.")
        open_at = deals.buyer_dispute_at(d)
        if open_at and now() < open_at:
            lines.append(f"Если продавец не ответит до <b>{at(open_at)}</b>, вы сможете открыть спор.")
        else:
            lines.append("Продавец долго не отвечает — можно открыть спор, решит администрация.")
    elif st == "paid" and not checks:
        lines.append(f"{pe('doc')} Чек у оператора {at(d.paid_at)}: он проверяет оплату в ордере Bybit и подтвердит "
                     "сделку. От вас ничего не нужно.")
    elif st == "paid" and operator:
        lines += [
            f"{pe('warn')} <b>Проверьте оплату в ордере Bybit</b> {_link(d)} — не по чеку:",
            f"1. Покупатель перевёл <b>{money.fmt(d.amount_rub)} ₽</b> на {esc(card.bank)} {mask(card)} после "
            f"{at(d.created_at)}. Отметьте оплату в ордере и дождитесь, пока продавец отпустит "
            f"<b>{money.usdt(d.seller_debit)} USDT</b>.",
            f"2. USDT пришли — «Подтвердить», покупатель получит {money.usdt(d.buyer_credit)} USDT.",
            "3. Оплаты нет или сумма другая — «Спор»: пришлите видео из ЛК или выписку, спор решится сразу.",
        ]
    elif st == "paid":
        lines += [
            f"{pe('warn')} <b>Проверьте поступление в приложении банка</b> — не по чеку, а по истории операций:",
            f"1. Найдите входящий перевод <b>{money.fmt(d.amount_rub)} ₽</b> на {esc(card.bank)} {mask(card)} "
            f"после {at(d.created_at)}.",
            f"2. Пришло ровно {money.fmt(d.amount_rub)} ₽ — «Деньги пришли», покупатель получит USDT.",
            "3. Денег нет или сумма другая — «Не пришли», решит администрация.",
            "Чек можно подделать: подтверждайте только по факту зачисления.",
        ]
        if late:
            lines.append(f"{pe('clock')} Чек загружен после окончания срока оплаты.")
        auto = deals.aware(d.paid_at) + timedelta(minutes=settings.num("escalate_minutes")) if d.paid_at else None
        if auto:
            lines.append(f"Без ответа до {at(auto, 'dt')} сделка уйдёт в спор автоматически.")
    elif st == "dispute":
        n = len(d.dispute_files or [])
        lines += [
            f"{pe('flag')} Причина: {REASONS.get(d.dispute_reason, '—')}"
            + (f", пришло {money.fmt(d.dispute_amount_rub)} ₽" if d.dispute_amount_rub else ""),
            "" if d.via_bybit else
            f"{pe('lock')} {money.usdt(d.seller_debit)} USDT продавца заморожены до решения администрации.",
            f"Материалов в споре: <b>{n}</b>. "
            + ("Помогут видео из банка со списанием и выписка." if buyer
               else "Поможет видео истории поступлений за время сделки."),
            "Что дальше: администрация сверит чек и материалы обеих сторон и примет решение — "
            "оно придёт уведомлением. " + ("Писать продавцу" if buyer else "Писать покупателю")
            + " напрямую не нужно.",
        ]
    elif st == "completed":
        lines.append(f"{pe('ok')} {money.usdt(d.buyer_credit)} USDT зачислены на ваш баланс." if buyer else
                     f"{pe('ok')} Сделка проведена: {money.usdt(d.seller_debit)} USDT по Bybit-ордеру." if d.via_bybit else
                     f"{pe('ok')} {money.usdt(d.seller_debit)} USDT списаны из заморозки, доход начислен.")
    elif st == "cancelled":
        lines.append("С вас ничего не списано." if buyer else "Сделка отменена, средства снова доступны.")
    elif st == "void":
        lines.append("Не переводите по этой сделке деньги. Уже перевели — напишите в поддержку, указав номер сделки."
                     if buyer else "Средства снова доступны.")
    elif st == "expired":
        until = deals.late_deadline(d)
        if buyer and until and now() < until:
            lines += ["Время на оплату вышло, с вас ничего не списано.",
                      f"{pe('warn')} <b>Уже перевели деньги?</b> Загрузите чек до {at(until, 'dt')} — "
                      "сделка вернётся продавцу на проверку."]
        else:
            lines.append("Время на оплату вышло." if buyer else "Покупатель не оплатил вовремя, средства снова доступны.")
    if st == "expired" and not buyer and d.funds_held and d.hold_until:
        lines[-1] = (f"{pe('lock')} Покупатель не оплатил вовремя. Залог {money.usdt(d.seller_debit)} USDT "
                     f"удерживается до <b>{at(d.hold_until)}</b>: если покупатель всё-таки перевёл и пришлёт чек, "
                     "сделка вернётся вам на проверку. Проверьте поступления в банке.")
    if d.close_reason in CLOSE_REASONS:
        lines.append(f"Итог: {CLOSE_REASONS[d.close_reason]}.")
    if d.resolution:
        lines.append(f"{pe('support')} Комментарий администрации: <i>{esc(d.resolution)}</i>")
    return "\n".join(lines) + ("\n" + note if note else "")


def deal_brief(d: Deal, card: Card, viewer_id: int, head: str) -> str:
    """Short text for notifications with documents (caption limit 1024)."""
    buyer = viewer_id == d.buyer_id
    amount = (f"{money.fmt(d.amount_rub)} ₽ → {money.usdt(d.buyer_credit)} USDT" if buyer
              else f"{money.fmt(d.amount_rub)} ₽ на {esc(card.bank)} {mask(card)}")
    return f"{pe('bell')} <b>{head}</b>\n{pe(STATUS[d.status][0])} Сделка #{d.id}: {amount}"


def deal_kb(d: Deal, viewer_id: int, card: Card | None = None):
    rows = []
    buyer = viewer_id == d.buyer_id
    checks = not buyer and viewer_id == deals.checker(d)
    if d.status in ("searching", "assigned", "checking") and buyer:
        rows += [[btn("Обновить", f"dl:{d.id}", "refresh"), btn("Отменить заявку", f"orb:cn:{d.id}", "cross",
                                                                   style="danger")]]
    elif d.status == "assigned":
        rows += [btn("Прислать ссылку на ордер" if d.via_bybit else "Выдать реквизиты", f"orq:give:{d.id}",
                     "shop" if d.via_bybit else "key", style="success"),
                 btn("Отказаться", f"orq:drop:{d.id}", "cross")]
    elif d.status == "checking":
        rows.append(btn("Выдать реквизиты", f"orq:give:{d.id}", "key", style="success") if checks
                    else btn("Обновить", f"dl:{d.id}", "refresh"))
    elif buyer and d.status == "waiting_payment":
        rows += [[btn("Скопировать " + ("телефон" if card.kind == "sbp" else "номер"), icon="key",
                      copy=card.requisites) if card else None,
                  btn(f"Скопировать {exact(d.amount_rub)} ₽", icon="ruble", copy=exact(d.amount_rub))],
                 btn("Прикрепить PDF-чек", f"dl:rc:{d.id}", "clip", style="success"),
                 [btn("Обновить", f"dl:{d.id}", "refresh"), btn("Отменить сделку", f"dl:cn:{d.id}", "cross", style="danger")]]
    elif buyer and d.status == "paid":
        open_at = deals.buyer_dispute_at(d)
        rows.append(btn("Обновить", f"dl:{d.id}", "refresh"))
        if open_at and now() >= open_at:
            rows.append(btn("Открыть спор", f"dl:bd:{d.id}", "flag", style="danger"))
    elif buyer and d.status == "expired" and (until := deals.late_deadline(d)) and now() < until:
        rows.append(btn("Я перевёл — чек", f"dl:late:{d.id}", "clip", style="primary"))
    elif checks and d.status == "paid":
        rows += [btn("Показать чек", f"dl:pdf:{d.id}", "doc"),
                 btn("Подтвердить" if d.via_bybit else "Деньги пришли", f"dl:ok:{d.id}", "ok", style="success"),
                 btn("Спор" if d.via_bybit else "Не пришли", f"dl:ds:{d.id}", "flag", style="danger")]
    elif d.status == "paid":
        rows += [btn("Показать чек", f"dl:pdf:{d.id}", "doc"), btn("Обновить", f"dl:{d.id}", "refresh")]
    elif d.status == "dispute":
        rows.append(btn("Добавить файл", f"dl:ev:{d.id}", "clip", style="primary"))
        if d.receipt_file_id:
            rows.append(btn("Показать чек", f"dl:pdf:{d.id}", "doc"))
    elif d.status == "waiting_payment":
        rows.append(btn("Обновить", f"dl:{d.id}", "refresh"))
    rows.append([btn("Чат сделки", f"dch:{d.id}", "support"), btn("Мои сделки", inline="сделки ")])
    rows.append(back("menu", "В меню"))
    return kb(*rows)


async def card_of(s: AsyncSession, d: Deal) -> Card | None:
    """The requisites of a deal; an order request has none until the merchant gives them."""
    return await s.get(Card, d.card_id) if d.card_id else None


async def deal_screen(bot: Bot, s: AsyncSession, user: User, d: Deal, src=None, note: str = ""):
    card = await card_of(s, d)
    await show(bot, user, deal_text(d, card, user.id, note), deal_kb(d, user.id, card), src)


async def push(bot: Bot, s: AsyncSession, uid: int, d: Deal, head: str) -> bool:
    """Notification with the deal's actions. False if the user did not receive it.
    The buyer of an API order is a service: it learns about changes by webhook, not by chat messages."""
    if uid is None or (d.api_client_id is not None and uid == d.buyer_id):
        return True
    card = await card_of(s, d)
    who = await s.get(User, uid)
    return await notify(bot, uid, f"{pe('bell')} <b>{head}</b>\n\n" + deal_text(d, card, uid), deal_kb(d, uid, card),
                        silent=bool(who and who.quiet)) is not None


def log(s: AsyncSession, d: Deal, kind: str, text: str, alert: bool = False, notice: bool = False) -> None:
    events.add(s, f"deal:{d.id}", kind, text, alert=alert, notice=notice)


async def on_deal_created(bot: Bot, s: AsyncSession, d: Deal):
    """Called after the deal is committed; the middleware commits the events added here."""
    if not await push(bot, s, d.seller_id, d, f"Новая сделка на {money.fmt(d.amount_rub)} ₽. Ждём перевод покупателя"):
        log(s, d, "notify_failed", f"Продавец {d.seller_id} не получил уведомление о новой сделке", alert=True)


async def _deal(s: AsyncSession, user: User, deal_id: str | int) -> Deal | None:
    d = await s.get(Deal, int(deal_id), populate_existing=True)
    return d if d and user.id in (d.buyer_id, d.seller_id, d.operator_id) else None


@router.callback_query(F.data.regexp(r"^deals(?::(\d+))?$"))
async def cb_deals(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.set_state(None)
    await deals_screen(bot, s, user, int(c.data.split(":")[1]) if ":" in c.data else 0, c)


async def deals_screen(bot: Bot, s: AsyncSession, user: User, page: int = 0, src=None):
    mine = or_((Deal.buyer_id == user.id) & deals.personal(), Deal.seller_id == user.id)
    total = await s.scalar(select(func.count(Deal.id)).where(mine))
    rows = (await s.scalars(select(Deal).where(mine).order_by(
        Deal.status.in_(deals.OPEN).desc(), Deal.id.desc()).offset(page * PAGE).limit(PAGE))).all()
    pages = max(1, (total + PAGE - 1) // PAGE)
    text = title(pe("list"), "Мои сделки") + "\n\n" + (
        f"Всего: <b>{total}</b>. Сначала открытые. ↓ — покупка, ↑ — продажа." if rows else
        f"{pe('info')} Сделок пока нет — они появятся здесь после покупки или продажи.")
    nav = []
    if page > 0:
        nav.append(btn(f"{page}/{pages}", f"deals:{page - 1}", "prev"))
    if page < pages - 1:
        nav.append(btn(f"{page + 2}/{pages}", f"deals:{page + 1}", "next"))
    await show(bot, user, text, kb(
        *[btn(f"{'↓' if d.buyer_id == user.id else '↑'} #{d.id} · {money.fmt(d.amount_rub)} ₽ · {STATUS[d.status][1]}",
              f"dl:{d.id}", STATUS[d.status][0]) for d in rows],
        nav,
        back("menu", "В меню"),
    ), src)


@router.callback_query(F.data.regexp(r"^dl:(\d+)$"))
async def cb_deal(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.set_state(None)
    d = await _deal(s, user, c.data.split(":")[1])
    if not d:
        return await c.answer("Сделка не найдена", show_alert=True)
    await deal_screen(bot, s, user, d, c)


# ---------- buyer: receipt ----------

def images_allowed() -> bool:
    return settings.get("receipt_images") == "1"


def receipt_problem(m: Message) -> str | None:
    doc = m.document
    if m.photo:
        return None if images_allowed() else "Это фото, а нужен PDF-файл чека. " + HOW_PDF
    if doc and images_allowed() and (doc.mime_type or "").startswith("image/"):
        return None if (doc.file_size or 0) <= MAX_FILE else "Файл больше 20 МБ."
    if not doc:
        return "Отправьте чек файлом PDF. " + HOW_PDF
    if doc.mime_type != "application/pdf" and not (doc.file_name or "").lower().endswith(".pdf"):
        return f"Файл «{esc((doc.file_name or 'без имени')[:40])}» — не PDF. " + HOW_PDF
    if (doc.file_size or 0) > MAX_FILE:
        return "Файл больше 20 МБ. Скачайте в банке обычный PDF-чек (обычно до 1 МБ)."
    return None


def receipt_unique(m: Message) -> str:
    """Same bytes = same file_unique_id, whoever sends it and however it is renamed."""
    return (m.photo[-1] if m.photo else m.document).file_unique_id


def receipt_id(m: Message) -> str:
    """Stored file reference; photos are prefixed so they are re-sent with sendPhoto."""
    return f"photo:{m.photo[-1].file_id}" if m.photo else m.document.file_id


async def not_a_pdf(bot: Bot, m: Message) -> bool:
    """A renamed text or image file is not a bank receipt: check the PDF signature.
    If Telegram cannot give the file right now, do not block the buyer (the seller still checks the bank)."""
    doc = m.document
    if m.photo or not doc or (doc.mime_type or "").startswith("image/"):
        return False
    try:
        buf = await bot.download(doc.file_id, destination=io.BytesIO())
    except Exception:  # noqa: BLE001 - network/Telegram problems must not cost the buyer the deal
        return False
    return b"%PDF-" not in buf.getvalue()[:1024]


async def send_receipt(bot: Bot, chat: int, fid: str, caption: str, markup=None, silent: bool = False,
                       thread: int | None = None):
    if fid.startswith("photo:"):
        return await bot.send_photo(chat, fid[6:], caption=caption, reply_markup=markup, disable_notification=silent,
                                    message_thread_id=thread)
    return await bot.send_document(chat, fid, caption=caption, reply_markup=markup, disable_notification=silent,
                                   message_thread_id=thread)


def _receipt_prompt(d: Deal, err: str = "") -> str:
    lines = [title(pe("clip"), f"Чек по сделке #{d.id}"), "",
             f"Отправьте <b>PDF-файл</b>{' или фото' if images_allowed() else ''} чека о переводе "
             f"<b>{money.fmt(d.amount_rub)} ₽</b>.", HOW_PDF]
    if d.status == "waiting_payment":
        lines.append(f"{pe('clock')} Успейте до {at(d.expires_at)}.")
    return "\n".join(lines) + (warn(err) if err else "")


async def _ask_receipt(c: CallbackQuery, bot, s, user, state, statuses: tuple[str, ...]):
    d = await _deal(s, user, c.data.split(":")[2])
    if not d or d.buyer_id != user.id or d.status not in statuses:
        return await c.answer("Действие недоступно: статус сделки изменился", show_alert=True)
    await state.set_state(Receipt.wait)
    await state.update_data(deal_id=d.id)
    await show(bot, user, _receipt_prompt(d), kb(back(f"dl:{d.id}", "Назад к сделке")), c)


@router.callback_query(F.data.regexp(r"^dl:rc:(\d+)$"))
async def cb_receipt(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await _ask_receipt(c, bot, s, user, state, ("waiting_payment",))


@router.callback_query(F.data.regexp(r"^dl:late:(\d+)$"))
async def cb_late(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await _ask_receipt(c, bot, s, user, state, ("expired",))


@router.message(Receipt.wait)
async def msg_receipt(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    d = await _deal(s, user, (await state.get_data())["deal_id"])
    if not d or d.status not in ("waiting_payment", "expired"):
        await state.set_state(None)
        return await deal_screen(bot, s, user, d) if d else None
    await process_receipt(bot, s, user, d, m, state)


async def process_receipt(bot: Bot, s: AsyncSession, user: User, d: Deal, m: Message, state: FSMContext):
    problem = receipt_problem(m)
    if not problem and await not_a_pdf(bot, m):
        problem = "Файл не похож на PDF-чек банка (возможно, переименован). " + HOW_PDF
    if problem:
        await state.set_state(Receipt.wait)
        await state.update_data(deal_id=d.id)
        return await show(bot, user, _receipt_prompt(d, problem), kb(back(f"dl:{d.id}", "Назад к сделке")))
    await state.set_state(None)
    paid, late, reused, error = await accept_receipt(s, d, user, receipt_id(m), receipt_unique(m))
    if not paid:
        return await deal_screen(bot, s, user, await s.get(Deal, d.id, populate_existing=True), note=warn(error))
    await deal_screen(bot, s, user, paid, note=ok("Чек отправлен продавцу. Ждите подтверждения."))
    await send_to_seller(bot, s, paid, late, reused)


async def accept_receipt(s: AsyncSession, d: Deal, buyer: User, fid: str, unique: str
                         ) -> tuple[Deal | None, bool, int | None, str]:
    """Attach a receipt to a deal of this buyer, in time or late. Commits.
    Returns (paid deal or None, late, id of another deal with the same file, error text for the buyer)."""
    did, late, paid = d.id, False, None
    if d.status == "waiting_payment" and _left(d) > 0:
        paid = await deals.mark_paid(s, did, buyer.id, fid)
        if paid is None:  # expired by the background task this very second: handle it as a late receipt
            d = await s.get(Deal, did, populate_existing=True)
    if paid is None and d.status in ("waiting_payment", "expired"):
        late = True
        if d.status == "waiting_payment":  # timer ran out, background task has not closed it yet
            await deals.expire(s, did)
        try:
            paid = await deals.reopen_late(s, did, buyer.id, fid)
        except deals.DealError as e:
            await s.rollback()
            await s.refresh(buyer)  # rollback expires every loaded object
            if await deals.expire(s, did):
                await s.commit()  # keep the expiry itself
            d = await s.get(Deal, did, populate_existing=True)
            if e.code == "no_funds":
                d.receipt_file_id = fid  # keep the receipt for the admin
                log(s, d, "late_no_funds", f"Поздний чек: у продавца нет средств на {money.fmt(d.amount_rub)} ₽, "
                                           "нужна ручная проверка", alert=True)
                await s.commit()
            return None, late, None, str(e)
    if not paid:
        await s.rollback()
        await s.refresh(buyer)
        return None, late, None, "Статус сделки уже изменился, чек не принят."
    paid.receipt_unique_id = unique
    reused = await s.scalar(select(Deal.id).where(Deal.receipt_unique_id == unique, Deal.id != paid.id)
                            .order_by(Deal.id).limit(1))
    log(s, paid, "paid", f"Покупатель {person(buyer)} загрузил чек на {money.fmt(paid.amount_rub)} ₽"
        + (" после срока оплаты" if late else "") + (" (через API)" if paid.api_client_id else "")
        + ", ждём продавца", notice=True)
    if reused:
        log(s, paid, "receipt_reused", f"Этот же файл чека уже присылали по сделке #{reused} — возможна подделка",
            alert=True)
    await s.commit()
    return paid, late, reused, ""


async def send_to_seller(bot: Bot, s: AsyncSession, paid: Deal, late: bool, reused: int | None) -> None:
    """The receipt goes to whoever checks the payment, with the buttons. In a Bybit order that is the operator;
    the merchant gets a plain copy without buttons."""
    card = await card_of(s, paid)
    head = ("Покупатель прислал чек" + (" после окончания срока" if late else "") + " — проверьте поступление"
            + (f". ВНИМАНИЕ: этот файл уже присылали по сделке #{reused}, сверяйте только выписку банка" if reused else ""))
    for uid in deals.sellers(paid):
        checks = uid == deals.checker(paid)
        who = await s.get(User, uid)
        try:
            await send_receipt(bot, uid, paid.receipt_file_id,
                               clean(deal_brief(paid, card, uid, head if checks else
                                                "Покупатель прислал чек — оплату проверяет оператор")),
                               deal_kb(paid, uid) if checks else kb(back("x", "Скрыть", "cross")),
                               silent=bool(who and who.quiet))
        except TelegramAPIError:
            if checks:
                log(s, paid, "notify_failed", f"{'Оператор' if paid.via_bybit else 'Продавец'} {uid} не получил чек "
                    "(бот заблокирован или ошибка Telegram)", alert=True)


# ---------- buyer: cancel / dispute ----------

@router.callback_query(F.data.regexp(r"^dl:cn:(\d+)$"))
async def cb_cancel(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    d = await _deal(s, user, c.data.split(":")[2])
    if not d or d.buyer_id != user.id or d.status != "waiting_payment":
        return await c.answer("Сделку уже нельзя отменить", show_alert=True)
    await show(bot, user, "\n".join([
        f"{pe('warn')} <b>Отменить сделку #{d.id}?</b>",
        "",
        quote(f"Уже перевели {money.fmt(d.amount_rub)} ₽? <b>Не отменяйте</b> — вернитесь и прикрепите чек.",
              "После отмены продавец не будет проверять перевод, а деньги вернуть будет сложнее."),
    ]), kb([btn("Да, отменить", f"dl:cn2:{d.id}", "cross", style="danger"), back(f"dl:{d.id}", "Не отменять", "back")]), c)


@router.callback_query(F.data.regexp(r"^dl:cn2:(\d+)$"))
async def cb_cancel2(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    d = await _deal(s, user, c.data.split(":")[2])
    if not d or d.buyer_id != user.id:
        return await c.answer("Действие недоступно", show_alert=True)
    d = await deals.cancel(s, d.id, ("waiting_payment",))
    if not d:
        return await c.answer("Сделку уже нельзя отменить", show_alert=True)
    log(s, d, "cancelled", f"Покупатель {person(user)} отменил сделку на {money.fmt(d.amount_rub)} ₽", notice=True)
    await s.commit()
    await deal_screen(bot, s, user, d, c, note=ok("Сделка отменена"))
    for uid in deals.sellers(d):
        await push(bot, s, uid, d, f"Покупатель отменил сделку #{d.id}")


@router.callback_query(F.data.regexp(r"^dl:bd:(\d+)$"))
async def cb_buyer_dispute(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    d = await _deal(s, user, c.data.split(":")[2])
    at_ = deals.buyer_dispute_at(d) if d and d.buyer_id == user.id else None
    if not at_ or now() < at_:
        return await c.answer("Спор пока недоступен", show_alert=True)
    await show(bot, user, "\n".join([
        f"{pe('flag')} <b>Открыть спор по сделке #{d.id}?</b>",
        "",
        quote("Сделку проверит администрация: сверит ваш чек с данными продавца.",
              "USDT продавца останутся заморожены до решения."),
        "После открытия добавьте доказательства: видео или выписку из банка о списании.",
    ]), kb([btn("Открыть спор", f"dl:bd2:{d.id}", "flag", style="danger"), back(f"dl:{d.id}", "Назад", "back")]), c)


@router.callback_query(F.data.regexp(r"^dl:bd2:(\d+)$"))
async def cb_buyer_dispute2(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    d = await deals.buyer_dispute(s, int(c.data.split(":")[2]), user.id)
    if not d:
        return await c.answer("Спор недоступен: статус сделки изменился", show_alert=True)
    log(s, d, "dispute", f"Покупатель открыл спор: продавец не подтверждает, {money.fmt(d.amount_rub)} ₽", alert=True)
    await s.commit()
    # straight to evidence: the admin decides faster with the buyer's proof of payment
    await state.set_state(Evidence.wait)
    await state.update_data(deal_id=d.id)
    await show(bot, user, _evidence_text(d, ok("Спор открыт. Пришлите видео или выписку из банка о списании "
                                               "и коротко опишите, когда и откуда переводили.")),
               kb(back(f"dl:{d.id}", "Готово")), c)
    for uid in deals.sellers(d):
        await push(bot, s, uid, d, f"Покупатель открыл спор по сделке #{d.id}")


# ---------- seller ----------

@router.callback_query(F.data.regexp(r"^dl:pdf:(\d+)$"))
async def cb_pdf(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    d = await _deal(s, user, c.data.split(":")[2])
    if d and d.receipt_file_id:
        with suppress(TelegramAPIError):
            await send_receipt(bot, user.id, d.receipt_file_id, f"Чек покупателя по сделке #{d.id}",
                               kb(back("x", "Скрыть", "cross")))
    await c.answer()


@router.callback_query(F.data.regexp(r"^dl:ok:(\d+)$"))
async def cb_confirm(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    d = await _deal(s, user, c.data.split(":")[2])
    if not d or deals.checker(d) != user.id or d.status != "paid":
        return await c.answer("Действие недоступно: статус сделки изменился", show_alert=True)
    card = await s.get(Card, d.card_id)
    await show(bot, user, "\n".join([
        f"{pe('warn')} <b>Подтвердить сделку #{d.id}?</b>",
        "",
        quote(f"{pe('ruble')} Покупатель оплатил ордер <b>{money.fmt(d.amount_rub)} ₽</b>, продавец на Bybit отпустил "
              f"вам <b>{money.usdt(d.seller_debit)} USDT</b>.",
              f"{pe('dollar')} Покупателю уйдёт <b>{money.usdt(d.buyer_credit)} USDT</b> с баланса площадки.")
        if d.via_bybit else
        quote(f"{pe('ruble')} На {esc(card.bank)} {mask(card)} поступило <b>{money.fmt(d.amount_rub)} ₽</b> — "
              "вы видите это в истории операций банка.",
              f"{pe('dollar')} Покупателю уйдёт <b>{money.usdt(d.buyer_credit)} USDT</b>, с вас — "
              f"{money.usdt(d.seller_debit)} USDT из заморозки."),
        f"{pe('lock')} <b>Отменить подтверждение будет нельзя.</b> Если денег нет или сумма другая — "
        f"вернитесь и нажмите «{'Спор' if d.via_bybit else 'Не пришли'}».",
    ]), kb(btn("Показать чек", f"dl:pdf:{d.id}", "doc"),
           [btn("Да, деньги пришли", f"dl:ok2:{d.id}", "ok", style="success"), back(f"dl:{d.id}", "Назад", "back")]), c)


@router.callback_query(F.data.regexp(r"^dl:ok2:(\d+)$"))
async def cb_confirm2(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    d = await _deal(s, user, c.data.split(":")[2])
    if not d or deals.checker(d) != user.id:
        return await c.answer("Действие недоступно", show_alert=True)
    d = await deals.complete(s, d.id, frm=("paid",))
    if not d:
        return await c.answer("Сделка уже изменена", show_alert=True)
    log(s, d, "completed", f"Подтвердил {'оператор' if d.via_bybit else 'продавец'} {person(user)}: "
                           f"{money.fmt(d.amount_rub)} ₽, "
                           f"покупателю {money.usdt(d.buyer_credit)} USDT, площадке {money.usdt(d.platform_fee)} USDT",
        notice=True)
    await s.commit()
    await deal_screen(bot, s, user, d, c, note=ok("Сделка завершена"))
    await push(bot, s, d.buyer_id, d, f"Сделка #{d.id} завершена — {money.usdt(d.buyer_credit)} USDT на балансе")
    if d.via_bybit:
        await push(bot, s, d.seller_id, d, f"Оператор подтвердил оплату по вашему ордеру, сделка #{d.id} завершена")


@router.callback_query(F.data.regexp(r"^dl:ds:(\d+)$"))
async def cb_dispute(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    d = await _deal(s, user, c.data.split(":")[2])
    if not d or deals.checker(d) != user.id or d.status != "paid":
        return await c.answer("Действие недоступно: статус сделки изменился", show_alert=True)
    await show(bot, user, "\n".join([
        title(pe("flag"), f"Спор по сделке #{d.id}"),
        "",
        "Что случилось? Дальше пришлите доказательство: видео из личного кабинета банка или Bybit, или выписку. "
        "Вы оператор этой сделки — спор решится сразу в вашу пользу." if d.via_bybit else
        "Что случилось? Дальше попросим доказательства из банка: видео из приложения "
        "и (если деньги не пришли) выписку.",
        "" if d.via_bybit else f"До решения администрации {money.usdt(d.seller_debit)} USDT останутся заморожены.",
    ]), kb(
        btn("Деньги не пришли", f"dl:dr:{d.id}:not_received", "cross"),
        btn("Другая сумма", f"dl:dr:{d.id}:wrong_amount", "ruble"),
        back(f"dl:{d.id}"),
    ), c)


@router.callback_query(F.data.regexp(r"^dl:dr:(\d+):(not_received|wrong_amount)$"))
async def cb_dispute_reason(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    _, _, deal_id, reason = c.data.split(":")
    d = await _deal(s, user, deal_id)
    if not d or deals.checker(d) != user.id or d.status != "paid":
        return await c.answer("Действие недоступно", show_alert=True)
    op = d.via_bybit
    await state.update_data(deal_id=d.id, reason=reason, files=[], amount=None, op=op)
    if reason == "wrong_amount":
        await state.set_state(Dispute.amount)
        ask = "Шаг 1/2. Отправьте сумму в ₽, которая <b>фактически</b> пришла:"
    elif op:
        await state.set_state(Dispute.proof)
        ask = (f"{pe('video')} Отправьте доказательство, что оплаты нет: <b>видео из личного кабинета</b> банка или "
               "Bybit либо <b>выписку</b> (PDF или фото). Спор решится сразу.")
    else:
        await state.set_state(Dispute.video)
        ask = f"Шаг 1/2. {pe('video')} Отправьте <b>видео из приложения банка</b> — запись экрана с историей операций за время сделки."
    await show(bot, user, f"{title(pe('flag'), f'Спор по сделке #{d.id}')}\n\n{ask}", kb(back(f"dl:{d.id}", "Отмена")), c)


@router.message(Dispute.amount, F.text)
async def msg_dispute_amount(m: Message, bot: Bot, user: User, state: FSMContext):
    data = await state.get_data()
    v = parse_rub(m.text)
    if v is None:
        return await show(bot, user, f"{title(pe('flag'), 'Спор')}\n\nОтправьте фактически полученную сумму в ₽."
                          + warn("Нужно число, например 4500"), kb(back(f"dl:{data['deal_id']}", "Отмена")))
    await state.update_data(amount=str(v))
    await state.set_state(Dispute.proof if data.get("op") else Dispute.video)
    await show(bot, user, f"{title(pe('flag'), 'Спор')}\n\nПолучено: <b>{money.fmt(v)} ₽</b>\n\n"
                          f"Шаг 2/2. {pe('video')} Отправьте <b>видео из банка</b>"
                          + (" или выписку" if data.get("op") else "") + ", где видно это поступление:",
               kb(back(f"dl:{data['deal_id']}", "Отмена")))


def _video_id(m: Message) -> str | None:
    if m.video:
        return m.video.file_id
    if m.document and (m.document.mime_type or "").startswith("video/"):
        return m.document.file_id
    return None


@router.message(Dispute.video)
async def msg_dispute_video(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    data = await state.get_data()
    fid = _video_id(m)
    if not fid:
        return await show(bot, user, f"{title(pe('flag'), 'Спор')}\n\nОтправьте видео из приложения банка."
                          + warn("Нужен видеофайл — запись экрана"), kb(back(f"dl:{data['deal_id']}", "Отмена")))
    files = data["files"] + [["video", fid, "seller"]]
    await state.update_data(files=files)
    if data["reason"] == "not_received":
        await state.set_state(Dispute.statement)
        return await show(bot, user, f"{title(pe('flag'), 'Спор')}\n\nШаг 2/2. {pe('doc')} Отправьте "
                                     "<b>выписку из банка</b> за время сделки (PDF или фото):",
                          kb(back(f"dl:{data['deal_id']}", "Отмена")))
    await submit_dispute(bot, s, user, state)


@router.message(Dispute.statement)
async def msg_dispute_statement(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    data = await state.get_data()
    if m.document:
        item = ["document", m.document.file_id, "seller"]
    elif m.photo:
        item = ["photo", m.photo[-1].file_id, "seller"]
    else:
        return await show(bot, user, f"{title(pe('flag'), 'Спор')}\n\nОтправьте выписку из банка."
                          + warn("Нужен файл или фото"), kb(back(f"dl:{data['deal_id']}", "Отмена")))
    await state.update_data(files=data["files"] + [item])
    await submit_dispute(bot, s, user, state)


@router.message(Dispute.proof)
async def msg_dispute_proof(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    data = await state.get_data()
    if fid := _video_id(m):
        item = ["video", fid, "seller"]
    elif m.document:
        item = ["document", m.document.file_id, "seller"]
    elif m.photo:
        item = ["photo", m.photo[-1].file_id, "seller"]
    else:
        return await show(bot, user, f"{title(pe('flag'), 'Спор')}\n\nОтправьте видео из личного кабинета или выписку."
                          + warn("Нужен видеофайл, PDF или фото"), kb(back(f"dl:{data['deal_id']}", "Отмена")))
    await state.update_data(files=data["files"] + [item])
    await submit_dispute(bot, s, user, state)


async def submit_dispute(bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    data = await state.get_data()
    await state.set_state(None)
    amount = Decimal(data["amount"]) if data.get("amount") else None
    d = await deals.open_dispute(s, data["deal_id"], user.id, data["reason"], data["files"], amount)
    if not d:
        d = await _deal(s, user, data["deal_id"])
        return await deal_screen(bot, s, user, d, note=warn("Статус сделки уже изменился, спор не открыт."))
    log(s, d, "dispute", f"Спор{' оператора' if d.via_bybit else ''}: {REASONS[d.dispute_reason]}"
        + (f", пришло {money.fmt(d.dispute_amount_rub)} ₽" if d.dispute_amount_rub else ""), alert=True)
    await s.commit()
    if d.via_bybit:
        return await operator_verdict(bot, s, user, d)
    await deal_screen(bot, s, user, d, note=ok("Спор открыт. Администрация рассмотрит его."))
    await push(bot, s, d.buyer_id, d, f"Продавец открыл спор по сделке #{d.id}. Добавьте доказательства оплаты")


async def operator_verdict(bot: Bot, s: AsyncSession, user: User, d: Deal):
    """The operator of a Bybit order acts as an admin: his proof from the bank settles the dispute at once —
    no payment: cancelled; another amount: completed by the amount actually received."""
    what = OPERATOR_REASONS[d.dispute_reason]
    try:
        if d.dispute_reason == "wrong_amount":
            res = await deals.complete(s, d.id, frm=("dispute",), actual_rub=d.dispute_amount_rub,
                                       reason="dispute_actual")
            what += f": проведено по {money.fmt(d.dispute_amount_rub)} ₽"
        else:
            res = await deals.cancel(s, d.id, ("dispute",), "cancelled", "dispute_seller")
    except deals.DealError as e:
        await s.rollback()
        await s.refresh(user)
        res, what = None, str(e)
    if res is None:
        d = await s.get(Deal, d.id, populate_existing=True)
        return await deal_screen(bot, s, user, d, note=warn(f"Спор передан администрации: {what}"))
    res.resolution = f"Оператор подтвердил доказательствами из банка: {what}."
    audit.log(s, user.id, "resolve", f"deal:{res.id}", f"оператор: {what}")
    log(s, res, "resolved", f"Спор решён оператором {user.id} автоматически: {what}", alert=True)
    await s.commit()
    await deal_screen(bot, s, user, res, note=ok(f"Спор решён в вашу пользу: {what}."))
    head = f"Спор по сделке #{res.id} решён: {what}"
    await push(bot, s, res.buyer_id, res, head)
    await push(bot, s, res.seller_id, res, head)


# ---------- evidence (both sides, while in dispute) ----------

@router.callback_query(F.data.regexp(r"^dl:ev:(\d+)$"))
async def cb_evidence(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    d = await _deal(s, user, c.data.split(":")[2])
    if not d or d.status != "dispute":
        return await c.answer("Спор уже закрыт", show_alert=True)
    await state.set_state(Evidence.wait)
    await state.update_data(deal_id=d.id)
    await show(bot, user, _evidence_text(d), kb(back(f"dl:{d.id}", "Готово")), c)


def _evidence_text(d: Deal, note: str = "") -> str:
    return "\n".join([
        title(pe("clip"), f"Доказательства по спору #{d.id}"),
        "",
        "Отправляйте по одному: видео, фото, PDF или текст с пояснением (до 1000 символов).",
        f"Уже в споре: <b>{len(d.dispute_files or [])}</b> из {deals.MAX_EVIDENCE}. Когда закончите — «Готово».",
    ]) + note


@router.message(Evidence.wait)
async def msg_evidence(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    did = (await state.get_data())["deal_id"]
    if m.video:
        item = ["video", m.video.file_id]
    elif m.photo:
        item = ["photo", m.photo[-1].file_id]
    elif m.document:
        item = ["document", m.document.file_id]
    elif m.text and len(m.text) <= 1000:
        item = ["text", m.text]
    else:
        d = await _deal(s, user, did)
        return await show(bot, user, _evidence_text(d, warn("Нужен файл, фото, видео или текст до 1000 символов")),
                          kb(back(f"dl:{did}", "Готово")))
    try:
        d = await deals.add_evidence(s, did, user.id, item)
    except deals.DealError as e:
        await state.set_state(None)
        d = await _deal(s, user, did)
        return await deal_screen(bot, s, user, d, note=warn(str(e)))
    role = "покупатель" if user.id == d.buyer_id else "оператор" if user.id == d.operator_id else "продавец"
    log(s, d, "evidence", f"Новое доказательство ({role}): {item[0]}", notice=True)
    await s.commit()
    await show(bot, user, _evidence_text(d, ok("Добавлено. Можно отправить ещё")), kb(back(f"dl:{did}", "Готово")))


# ---------- admin-facing dispute card (used by admin handlers and background tasks) ----------

async def send_files(bot: Bot, chat: int, d: Deal, thread: int | None = None):
    """Receipt and dispute evidence for an admin (in his private chat or right in the admin chat's topic): photos /
    videos and documents as albums (up to 10), texts as one message — a dispute with 20 files is a few messages."""
    cap = f"Сделка #{d.id}"
    t = {"message_thread_id": thread}
    if d.receipt_file_id:
        await send_receipt(bot, chat, d.receipt_file_id, f"{cap}: чек покупателя", thread=thread)
    names = {"video": "видео", "photo": "фото", "document": "файл"}
    who = {"buyer": "покупатель", "seller": "продавец"}
    items = [(f[0], f[1], who.get(f[2] if len(f) > 2 else "seller", "")) for f in d.dispute_files or []]
    texts = [f"<b>{w}:</b> {esc(v)}" for k, v, w in items if k == "text"]
    if texts:
        await bot.send_message(chat, f"<b>{cap}: пояснения сторон</b>\n" + "\n\n".join(texts)[:3900], **t)
    visual = [(k, v, w) for k, v, w in items if k in ("photo", "video")]
    docs = [(k, v, w) for k, v, w in items if k == "document"]
    for group in (visual, docs):
        for i in range(0, len(group), 10):
            chunk = group[i:i + 10]
            if len(chunk) == 1:
                k, v, w = chunk[0]
                send = {"video": bot.send_video, "photo": bot.send_photo}.get(k, bot.send_document)
                await send(chat, v, caption=f"{cap}: {names[k]} — {w}", **t)
                continue
            media = [{"photo": InputMediaPhoto, "video": InputMediaVideo}.get(k, InputMediaDocument)(
                media=v, caption=f"{cap}: {names[k]} — {w}") for k, v, w in chunk]
            await bot.send_media_group(chat, media, **t)


def verdict_effects(d: Deal, verdict: str) -> list[str]:
    """What each admin decision does with money, in plain words."""
    if verdict == "c" and (d.status == "searching" or (d.via_bybit and d.status != "waiting_payment")):
        return ["Заявка на реквизиты закрывается. Заморозки нет."]
    if verdict in ("s", "c"):
        return ["Сделка отменяется, заморозки нет (Bybit-ордер)." if d.via_bybit else
                f"Сделка отменяется, продавцу возвращаются {money.usdt(d.seller_debit)} USDT из заморозки.",
                "Покупатель USDT не получает."]
    amount = d.dispute_amount_rub if verdict == "a" else d.amount_rub
    q = deals.requote(d, amount)
    if d.via_bybit:
        return [f"Сделка проводится на <b>{money.fmt(amount)} ₽</b>.",
                f"Покупателю +{money.usdt(q.buyer_credit)} USDT с баланса площадки: {money.usdt(q.seller_debit)} USDT "
                "по ордеру получены оператором на Bybit.",
                f"Площадке +{money.usdt(q.platform_fee)} USDT."]
    return [f"Сделка проводится на <b>{money.fmt(amount)} ₽</b>.",
            f"Покупателю +{money.usdt(q.buyer_credit)} USDT, у продавца списывается {money.usdt(q.seller_debit)} USDT"
            + (f" (заморожено {money.usdt(d.seller_debit)})" if q.seller_debit != d.seller_debit else " из заморозки") + ".",
            f"Площадке +{money.usdt(q.platform_fee)} USDT."]


async def dispute_text(s: AsyncSession, d: Deal) -> str:
    card = await card_of(s, d)
    buyer = await s.get(User, d.buyer_id)
    seller = await s.get(User, d.seller_id) if d.seller_id else None
    stats = await deals.completed_count(s, [buyer.id] + ([seller.id] if seller else []))

    def who(u: User | None) -> str:
        if u is None:
            return "ещё не назначен (ордерная заявка)"
        flags = " · ЗАБАНЕН" if u.is_banned else ""
        return f"{esc(u.name)} (@{esc(u.username or '—')}, <code>{u.id}</code>) · сделок: {stats[u.id]}{flags}"

    icon, label = STATUS[d.status]
    files = d.dispute_files or []
    by = {r: sum(1 for f in files if (f[2] if len(f) > 2 else "seller") == r) for r in ("buyer", "seller")}
    times = [f"создана {at(d.created_at, 'dt')}", f"оплатить до {at(d.expires_at, 't')}"]
    if d.paid_at:
        times.append(f"чек {at(d.paid_at, 'dt')}")
    if d.closed_at:
        times.append(f"закрыта {at(d.closed_at, 'dt')}")
    lines = [
        title(pe("flag"), f"Сделка #{d.id}"),
        f"{pe(icon)} <b>{label}</b>",
        "",
        quote(
            f"{pe('profile')} Покупатель: {who(buyer)}",
            f"{pe('profile')} Продавец: {who(seller)}",
            f"{pe('card')} {esc(card.bank)} <code>{esc(card.requisites)}</code> · {esc(card.holder)}"
            + (" · ордерные реквизиты" if d.is_order else "") if card else
            f"{pe('card')} Реквизиты ещё не выданы" + (f" · перевод из {esc(d.sender_bank)}" if d.sender_bank else ""),
            f"{pe('ruble')} Сумма: <b>{money.fmt(d.amount_rub)} ₽</b> · курс {money.fmt(d.rate)}",
            f"{pe('shop')} Bybit-ордер: {_link(d) if d.bybit_url else 'ссылки ещё нет'} · "
            f"{money.usdt(d.seller_debit)} USDT по {money.fmt(d.merchant_rate)} ₽ · оператор "
            + (f"<code>{d.operator_id}</code>" if d.operator_id else "не назначен") if d.via_bybit else
            f"{pe('lock')} Заморожено у продавца: <b>{money.usdt(d.seller_debit)} USDT</b>"
            + (f" · курс {money.fmt(d.merchant_rate)} ₽" if d.merchant_rate else ""),
            f"{pe('dollar')} Покупателю к выдаче: <b>{money.usdt(d.buyer_credit)} USDT</b>",
            f"{pe('clock')} " + " · ".join(times),
            f"{pe('clip')} Чек: {'есть' if d.receipt_file_id else 'нет'} · материалов: покупатель {by['buyer']}, продавец {by['seller']}",
        ),
    ]
    if d.close_reason:
        lines.append(f"{pe(icon)} Итог: <b>{CLOSE_REASONS.get(d.close_reason, d.close_reason)}</b>")
    if d.dispute_reason:
        lines.append(f"{pe('warn')} Причина: <b>{REASONS.get(d.dispute_reason, d.dispute_reason)}</b>"
                     + (f" — пришло <b>{money.fmt(d.dispute_amount_rub)} ₽</b>" if d.dispute_amount_rub else ""))
    return "\n".join(lines)


def dispute_kb(d: Deal, *extra):
    seller = d.seller_id is not None
    operator = d.operator_id if d.via_bybit else None
    people = [[btn("Покупатель", f"auv:{d.buyer_id}", "profile"),
               btn("Мерчант", f"auv:{d.seller_id}", "profile") if seller else None],
              btn("Оператор", f"auv:{operator}", "profile") if operator else None,
              [btn("Написать покупателю", f"dm:{d.id}:{d.buyer_id}", "support") if not d.api_client_id else None,
               btn("Написать мерчанту", f"dm:{d.id}:{d.seller_id}", "support") if seller else None],
              btn("Написать оператору", f"dm:{d.id}:{operator}", "support") if operator else None]
    files = btn("Чек и файлы", f"af:{d.id}", "clip")
    if d.status in deals.UNPAID:
        return kb(btn("Отменить сделку", f"ar:{d.id}:c", "cross", style="danger"), *people, *extra)
    if d.status not in ("paid", "dispute"):
        return kb(files, *people, *extra)
    actual = None
    if d.dispute_amount_rub is not None and d.dispute_amount_rub != d.amount_rub:
        actual = btn(f"Провести на {money.fmt(d.dispute_amount_rub)} ₽", f"ar:{d.id}:a", "ruble", style="primary")
    return kb(
        files,
        btn("За покупателя", f"ar:{d.id}:b", "ok", style="success"),
        actual,
        btn("За продавца", f"ar:{d.id}:s", "cross", style="danger"),
        *people,
        *extra,
    )
