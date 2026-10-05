"""Admin panel: API applications (approve / reject) and API clients (limits, suspend, revoke token)."""
import secrets
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import config
from bot.services.admins import IsAdmin
from bot.emoji import back, btn, kb, pe
from bot.handlers.seller import parse_rub
from bot.models import ApiApplication, ApiClient, Deal, User, now
from bot.services import api, audit, deals, events, money, settings
from bot.ui import alink, at, card, cf, esc, notify, ok, quote, show, title, ulink, verdict, warn

router = Router()
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

APP_STATUS = {"pending": "на рассмотрении", "approved": "одобрена", "rejected": "отклонена"}
# field -> (title, kind): rub = RUB amount, int = whole number
LIMITS = {"min_rub": ("Минимальный заказ, ₽", "rub"), "max_rub": ("Максимальный заказ, ₽", "rub"),
          "daily_rub": ("Лимит в сутки, ₽", "rub"), "max_open": ("Неоплаченных заказов одновременно", "int"),
          "rps": ("Запросов в секунду", "int")}


TERMS = {"rate": "Курс клиента, ₽ за 1 USDT", "pct": "Процент площадки с клиента"}


class AdmApi(StatesGroup):
    reason = State()
    limit = State()
    terms = State()


def terms_lines(cl: ApiClient) -> list[str]:
    """The client's terms and what the platform keeps on 10 000 ₽ through a static card and an order merchant."""
    rate, pct = settings.client_terms(cl)
    amount = Decimal(10000)
    card = money.seller_debit(amount, settings.dec("rate"), settings.dec("seller_pct"))
    order = (amount / settings.dec("order_rate")).quantize(money.Q, "ROUND_UP")
    credit = money.split(amount, Decimal("Infinity"), rate, pct).buyer_credit
    margin = lambda debit: (f"<b>{money.usdt(debit - credit)}</b>" if debit >= credit  # noqa: E731
                            else f"🔴 <b>{money.usdt(debit - credit)}</b> — в минус, заказы не создаются")
    return [f"Курс: <b>{money.fmt(rate)} ₽</b>" + ("" if cl.rate is not None else " (общий)")
            + f" · процент: <b>{money.fmt(pct, 3)}%</b>" + ("" if cl.pct is not None else " (общий)"),
            f"10 000 ₽ → клиенту <b>{money.usdt(credit)} USDT</b> — одинаково для карт и ордеров",
            f"Площадке: карта {margin(card)} · ордер {margin(order)} USDT"]


def _who(u: User | None, uid: int) -> str:
    return f"{esc(u.name or '—')} @{esc(u.username or '—')} (<code>{uid}</code>)" if u else f"<code>{uid}</code>"


@router.callback_query(F.data == "aapi")
async def cb_home(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    pending = (await s.scalars(select(ApiApplication).where(ApiApplication.status == "pending")
                               .order_by(ApiApplication.id))).all()
    clients = (await s.scalars(select(ApiClient).order_by(ApiClient.id.desc()).limit(20))).all()
    await show(bot, user, "\n".join([
        title(pe("key"), "API для сервисов"),
        "",
        quote(f"{pe('search')} Адрес API: <code>{config.api_url}</code>" + ("" if config.api_enabled
                                                                           else " · <b>сервер выключен</b> (API_ENABLED)"),
              f"{pe('bell')} Заявок ждут решения: <b>{len(pending)}</b> · клиентов: <b>{len(clients)}</b>"),
    ]), kb(*[btn(f"Заявка #{a.id} · {a.project[:30]}", f"aap:{a.id}", "pencil", style="primary") for a in pending],
           *[btn(f"{'' if cl.status == 'active' else '⏸ '}{cl.project[:30]} · {cl.user_id}", f"acl:{cl.id}", "key")
             for cl in clients],
           btn("Все заявки", "aapl", "list"), back("a", "Админ-панель")), c)


@router.callback_query(F.data == "aapl")
async def cb_apps(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    rows = (await s.scalars(select(ApiApplication).order_by(ApiApplication.id.desc()).limit(20))).all()
    await show(bot, user, title(pe("list"), "Заявки на API") + ("" if rows else "\n\nПока нет"), kb(
        *[btn(f"#{a.id} · {a.project[:28]} · {APP_STATUS[a.status]}", f"aap:{a.id}", "pencil") for a in rows],
        back("aapi", "API")), c)


async def app_screen(bot: Bot, s: AsyncSession, admin: User, a: ApiApplication, src=None, note: str = ""):
    u = await s.get(User, a.user_id)
    stats = await deals.completed_count(s, [a.user_id])
    decided = await s.get(User, a.admin_id) if a.admin_id else None
    await show(bot, admin, "\n".join([
        title(pe("pencil"), f"Заявка на API {alink('apa', a.id, f'#{a.id}')}") + f" · {APP_STATUS[a.status]}",
        "",
        card(cf("Заявитель", f"{ulink(u, a.user_id)} · сделок в боте: {stats[a.user_id]}", icon="profile"),
             cf("Проект", f"<b>{esc(a.project)}</b>", esc(a.url), icon="shop"),
             cf("Трафик", esc(a.traffic), icon="people"),
             cf("Оборот в месяц", esc(a.volume), icon="stats"),
             cf("Подана", at(a.created_at, "dt") + (f" · решение {at(a.decided_at, 'dt')} · {ulink(decided, a.admin_id)}"
                                                    if a.decided_at else ""), icon="clock"),
             cf("О проекте", esc(a.about), icon="info") if a.about else "",
             cf("Причина отказа", f"<i>{esc(a.reason)}</i>", icon="cross") if a.reason else ""),
    ]) + note, kb(
        [btn("Одобрить", f"aap:ok:{a.id}", "ok", style="success"), btn("Отклонить", f"aap:no:{a.id}", "cross",
                                                                          style="danger")]
        if a.status == "pending" else None,
        back("aapi", "API")), src)


@router.callback_query(F.data.regexp(r"^aap:(\d+)$"))
async def cb_app(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    a = await s.get(ApiApplication, int(c.data.split(":")[1]))
    if not a:
        return await c.answer("Заявка не найдена", show_alert=True)
    await app_screen(bot, s, user, a, c)


@router.callback_query(F.data.regexp(r"^aap:ok:(\d+)$"))
async def cb_approve(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    a = await s.get(ApiApplication, int(c.data.split(":")[2]), with_for_update=True, populate_existing=True)
    if not a or a.status != "pending":
        return await c.answer("Заявка уже рассмотрена", show_alert=True)
    a.status, a.admin_id, a.decided_at = "approved", user.id, now()
    client = await s.scalar(select(ApiClient).where(ApiClient.user_id == a.user_id))
    if client is None:
        client = ApiClient(user_id=a.user_id, project=a.project, webhook_secret=secrets.token_hex(32))
        s.add(client)
    else:
        client.project, client.status = a.project, "active"
    await s.flush()
    audit.log(s, user.id, "api_approve", f"apa:{a.id}", a.project)
    events.add(s, f"apa:{a.id}", "approved", f"Заявка одобрена ({user.name}), клиент #{client.id}", a.user_id, notice=True)
    await s.commit()
    await notify(bot, a.user_id, f"{pe('ok')} <b>Заявка на Strait Pay API одобрена.</b> Откройте раздел API, "
                                 "выпустите токен и задайте вебхук.", kb(btn("Открыть API", "api", "key", style="success"),
                                                                         back("x", "Скрыть", "cross")))
    await client_screen(bot, s, user, client, c, "\n\n" + verdict("ok", "Одобрено", user)
                        + "\n" + quote("Лимиты по умолчанию — поправьте при необходимости."))


@router.callback_query(F.data.regexp(r"^aap:no:(\d+)$"))
async def cb_reject_ask(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    aid = int(c.data.split(":")[2])
    await state.set_state(AdmApi.reason)
    await state.set_data({"app": aid})
    await show(bot, user, f"{pe('cross')} <b>Отказ по заявке на API #{aid}</b>\n\n"
                          + quote("Напишите причину следующим сообщением (5–500 символов) — её увидит заявитель."),
               kb(back(f"aap:{aid}", "Отмена")), c)


@router.message(AdmApi.reason, F.text)
async def msg_reject(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    aid = (await state.get_data()).get("app")
    reason = " ".join(m.text.split())
    if not 5 <= len(reason) <= 500:
        return await show(bot, user, title(pe("cross"), "Причина отказа") + "\n\nНапишите причину ещё раз."
                          + warn("От 5 до 500 символов"), kb(back(f"aap:{aid}", "Отмена")))
    await state.clear()
    a = await s.get(ApiApplication, aid, with_for_update=True, populate_existing=True)
    if not a or a.status != "pending":
        return await show(bot, user, warn("Заявка уже рассмотрена"), kb(back("aapi", "API")))
    a.status, a.admin_id, a.decided_at, a.reason = "rejected", user.id, now(), reason
    audit.log(s, user.id, "api_reject", f"apa:{a.id}", reason)
    events.add(s, f"apa:{a.id}", "rejected", f"Отклонена ({user.name}): {reason}", a.user_id, notice=True)
    await s.commit()
    await notify(bot, a.user_id, f"{pe('cross')} <b>Заявка на Strait Pay API отклонена.</b>\nПричина: {esc(reason)}")
    await app_screen(bot, s, user, a, note="\n\n" + verdict("cross", "Отклонено", user))


# ---------- clients ----------

async def client_screen(bot: Bot, s: AsyncSession, admin: User, cl: ApiClient, src=None, note: str = ""):
    u = await s.get(User, cl.user_id)
    used = await api.usage(s, cl)
    total, success, volume = (await s.execute(select(
        func.count(Deal.id), func.count(Deal.id).filter(Deal.status == "completed"),
        func.coalesce(func.sum(Deal.amount_rub).filter(Deal.status == "completed"), 0),
    ).where(Deal.api_client_id == cl.id))).one()
    await show(bot, admin, "\n".join([
        title(pe("key"), f"API-клиент {alink('apc', cl.id, f'#{cl.id}')} · {esc(cl.project)}")
        + f" · {'активен' if cl.status == 'active' else 'приостановлен'}",
        "",
        card(cf("Владелец", f"{ulink(u, cl.user_id)} · баланс {money.usdt(u.balance)} USDT", icon="profile"),
             cf("Доступ", "токен: " + (f"…{cl.token_hint}, выпущен {at(cl.token_at, 'dt')}" if cl.token_hash else "нет"),
                f"вебхук: {esc(cl.webhook_url or '—')}", icon="key"),
             cf("Условия", *terms_lines(cl), icon="percent"),
             cf("Лимиты", *[f"{t}: <b>{money.fmt(getattr(cl, k)) if kind == 'rub' else getattr(cl, k)}</b>"
                            for k, (t, kind) in LIMITS.items()],
                f"сегодня: {money.fmt(used['today_rub'])} ₽ · неоплаченных сейчас: {used['open_orders']}", icon="lock"),
             cf("Заказы", f"всего {total}, успешных {success} на {money.fmt(Decimal(volume))} ₽", icon="stats")),
    ]) + note, kb(
        [btn("Курс клиента", f"acl:t:{cl.id}:rate", "swap"), btn("Процент клиента", f"acl:t:{cl.id}:pct", "percent")],
        *[btn(f"Изменить: {t}", f"acl:l:{cl.id}:{k}", "pencil") for k, (t, _) in LIMITS.items()],
        [btn("Приостановить", f"acl:st:{cl.id}:0", "pause", style="danger") if cl.status == "active"
         else btn("Возобновить", f"acl:st:{cl.id}:1", "ok", style="success"),
         btn("Отозвать токен", f"acl:rv:{cl.id}", "cross") if cl.token_hash else None],
        back("aapi", "API")), src)


@router.callback_query(F.data.regexp(r"^acl:(\d+)$"))
async def cb_client(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    cl = await s.get(ApiClient, int(c.data.split(":")[1]))
    if not cl:
        return await c.answer("Клиент не найден", show_alert=True)
    await client_screen(bot, s, user, cl, c)


@router.callback_query(F.data.regexp(r"^acl:(st:\d+:[01]|rv:\d+)$"))
async def cb_client_action(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    parts = c.data.split(":")
    cl = await s.get(ApiClient, int(parts[2]), with_for_update=True, populate_existing=True)
    if not cl:
        return await c.answer()
    if parts[1] == "st":
        cl.status = "active" if parts[3] == "1" else "suspended"
        what = "возобновлён" if cl.status == "active" else "приостановлен"
        msg = f"Доступ к Strait Pay API {what} администрацией."
    else:
        cl.token_hash, cl.token_hint = None, None
        what, msg = "токен отозван", "API-токен отозван администрацией. Выпустите новый в разделе API."
    audit.log(s, user.id, "api_client", f"apc:{cl.id}", what)
    events.add(s, f"apc:{cl.id}", "admin", f"{what} ({user.name})", cl.user_id, alert=True)
    await s.commit()
    await notify(bot, cl.user_id, f"{pe('key')} {msg}")
    await client_screen(bot, s, user, cl, c, ok(what.capitalize()))


@router.callback_query(F.data.regexp(r"^acl:l:(\d+):(\w+)$"))
async def cb_limit_ask(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    _, _, cid, field = c.data.split(":")
    if field not in LIMITS:
        return await c.answer()
    await state.set_state(AdmApi.limit)
    await state.set_data({"client": int(cid), "field": field})
    await show(bot, user, f"{title(pe('pencil'), LIMITS[field][0])}\n\nОтправьте новое значение числом.",
               kb(back(f"acl:{cid}", "Отмена")), c)


@router.message(AdmApi.limit, F.text)
async def msg_limit(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    data = await state.get_data()
    field, cid = data["field"], data["client"]
    cl = await s.get(ApiClient, cid)
    raw = m.text.strip()
    value = parse_rub(raw) if LIMITS[field][1] == "rub" else (int(raw) if raw.isdigit() and 1 <= int(raw) <= 1000
                                                               else None)
    lo = value if field == "min_rub" else cl.min_rub
    hi = value if field == "max_rub" else cl.max_rub
    if value is None or (field in ("min_rub", "max_rub") and lo > hi):
        return await show(bot, user, f"{title(pe('pencil'), LIMITS[field][0])}\n\nОтправьте значение ещё раз."
                          + warn("Число; для ₽ минимум не больше максимума; целые — от 1 до 1000"),
                          kb(back(f"acl:{cid}", "Отмена")))
    await state.clear()
    old = getattr(cl, field)
    setattr(cl, field, value)
    audit.log(s, user.id, "api_limit", f"apc:{cl.id}", f"{field}: {old} → {value}")
    await client_screen(bot, s, user, cl, note=ok(f"{LIMITS[field][0]}: {old} → {value}"))


@router.callback_query(F.data.regexp(r"^acl:t:(\d+):(rate|pct)$"))
async def cb_terms_ask(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    _, _, cid, field = c.data.split(":")
    cl = await s.get(ApiClient, int(cid))
    if not cl:
        return await c.answer()
    await state.set_state(AdmApi.terms)
    await state.set_data({"client": cl.id, "field": field})
    await show(bot, user, _terms_text(cl, field), kb(back(f"acl:{cid}", "Отмена")), c)


def _terms_text(cl: ApiClient, field: str, err: str = "") -> str:
    general = (f"{money.fmt(settings.dec('rate'))} ₽" if field == "rate"
               else f"{money.fmt(settings.dec('platform_pct'), 3)}%")
    return "\n".join([
        title(pe("pencil"), TERMS[field]),
        quote(*terms_lines(cl)),
        f"Отправьте значение (например, <code>{'100' if field == 'rate' else '7'}</code>) или «-» — общий "
        f"({general}). Действует на новые заказы, и по картам, и по ордерам.",
    ]) + (warn(err) if err else "")


@router.message(AdmApi.terms, F.text)
async def msg_terms(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    data = await state.get_data()
    cl = await s.get(ApiClient, data["client"])
    field, raw = data["field"], m.text.strip().replace(",", ".").replace(" ", "")
    value = None
    if raw != "-":
        try:
            value = Decimal(raw)
        except Exception:  # noqa: BLE001
            value = Decimal(-1)
        bad = (not value.is_finite() or value <= 0 or value >= 10_000_000 or value.as_tuple().exponent < -2
               if field == "rate" else not value.is_finite() or not 0 <= value < 100 or value.as_tuple().exponent < -3)
        if bad:
            return await show(bot, user, _terms_text(cl, field, "Курс — число больше 0, до 2 знаков" if field == "rate"
                                                     else "Процент от 0 до 99,999, до 3 знаков"),
                              kb(back(f"acl:{cl.id}", "Отмена")))
    await state.clear()
    old = getattr(cl, field)
    setattr(cl, field, value)
    audit.log(s, user.id, "api_terms", f"apc:{cl.id}", f"{field}: {old} → {value}")
    events.add(s, f"apc:{cl.id}", "terms", f"{TERMS[field]}: {old if old is not None else 'общий'} → "
               f"{value if value is not None else 'общий'} ({user.name})", cl.user_id, alert=True)
    rate, pct = settings.client_terms(cl)
    await notify(bot, cl.user_id, f"{pe('key')} <b>Условия Strait Pay API изменены</b>\nКурс {money.fmt(rate)} ₽ · "
                                  f"комиссия {money.fmt(pct, 3)}% — для новых заказов.")
    await client_screen(bot, s, user, cl, note=ok("Сохранено"))
