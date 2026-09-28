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
from bot.emoji import back, btn, kb, pe
from bot.handlers.seller import parse_rub
from bot.models import ApiApplication, ApiClient, Deal, User, now
from bot.services import api, audit, deals, events, money
from bot.ui import at, esc, notify, ok, quote, show, title, warn

router = Router()
router.message.filter(F.from_user.id.in_(config.admin_ids))
router.callback_query.filter(F.from_user.id.in_(config.admin_ids))

APP_STATUS = {"pending": "на рассмотрении", "approved": "одобрена", "rejected": "отклонена"}
# field -> (title, kind): rub = RUB amount, int = whole number
LIMITS = {"min_rub": ("Минимальный заказ, ₽", "rub"), "max_rub": ("Максимальный заказ, ₽", "rub"),
          "daily_rub": ("Лимит в сутки, ₽", "rub"), "max_open": ("Неоплаченных заказов одновременно", "int"),
          "rps": ("Запросов в секунду", "int")}


class AdmApi(StatesGroup):
    reason = State()
    limit = State()


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
    await show(bot, admin, "\n".join([
        title(pe("pencil"), f"Заявка на API #{a.id} · {APP_STATUS[a.status]}"),
        "",
        quote(f"{pe('profile')} {_who(u, a.user_id)} · сделок в боте: {stats[a.user_id]}",
              f"{pe('shop')} Проект: <b>{esc(a.project)}</b>",
              f"{pe('search')} Ссылка: {esc(a.url)}",
              f"{pe('people')} Трафик: {esc(a.traffic)}",
              f"{pe('stats')} Оборот в месяц: {esc(a.volume)}",
              f"{pe('clock')} Подана {at(a.created_at, 'dt')}"
              + (f" · решение {at(a.decided_at, 'dt')} (<code>{a.admin_id}</code>)" if a.decided_at else "")),
        esc(a.about) if a.about else "",
        f"Причина отказа: <i>{esc(a.reason)}</i>" if a.reason else "",
    ]) + note, kb(
        [btn("Одобрить", f"aap:ok:{a.id}", "ok", style="success"), btn("Отклонить", f"aap:no:{a.id}", "cross",
                                                                          style="danger")]
        if a.status == "pending" else None,
        btn("Профиль", f"auv:{a.user_id}", "profile"), back("aapi", "API")), src)


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
    await client_screen(bot, s, user, client, c, ok("Одобрена. Лимиты по умолчанию — поправьте при необходимости."))


@router.callback_query(F.data.regexp(r"^aap:no:(\d+)$"))
async def cb_reject_ask(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    aid = int(c.data.split(":")[2])
    await state.set_state(AdmApi.reason)
    await state.set_data({"app": aid})
    await show(bot, user, f"{title(pe('cross'), f'Отказ по заявке #{aid}')}\n\nНапишите причину (5–500 символов) — "
                          "её увидит заявитель.", kb(back(f"aap:{aid}", "Отмена")), c)


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
    await app_screen(bot, s, user, a, note=ok("Отклонена, заявитель уведомлён"))


# ---------- clients ----------

async def client_screen(bot: Bot, s: AsyncSession, admin: User, cl: ApiClient, src=None, note: str = ""):
    u = await s.get(User, cl.user_id)
    used = await api.usage(s, cl)
    total, success, volume = (await s.execute(select(
        func.count(Deal.id), func.count(Deal.id).filter(Deal.status == "completed"),
        func.coalesce(func.sum(Deal.amount_rub).filter(Deal.status == "completed"), 0),
    ).where(Deal.api_client_id == cl.id))).one()
    await show(bot, admin, "\n".join([
        title(pe("key"), f"API-клиент #{cl.id} · {esc(cl.project)}"),
        f"{pe('ok') if cl.status == 'active' else pe('pause')} <b>{'активен' if cl.status == 'active' else 'приостановлен'}</b>",
        "",
        quote(f"{pe('profile')} Владелец: {_who(u, cl.user_id)}",
              f"{pe('key')} Токен: " + (f"…{cl.token_hint}, выпущен {at(cl.token_at, 'dt')}" if cl.token_hash else "нет"),
              f"{pe('bell')} Вебхук: {esc(cl.webhook_url or '—')}",
              f"{pe('dollar')} Баланс владельца: {money.usdt(u.balance)} USDT"),
        quote(*[f"{t}: <b>{money.fmt(getattr(cl, k)) if kind == 'rub' else getattr(cl, k)}</b>"
                for k, (t, kind) in LIMITS.items()],
              f"Сегодня: {money.fmt(used['today_rub'])} ₽ · неоплаченных сейчас: {used['open_orders']}"),
        quote(f"{pe('stats')} Заказов всего: {total}, успешных: {success} на {money.fmt(Decimal(volume))} ₽"),
    ]) + note, kb(
        *[btn(f"Изменить: {t}", f"acl:l:{cl.id}:{k}", "pencil") for k, (t, _) in LIMITS.items()],
        [btn("Приостановить", f"acl:st:{cl.id}:0", "pause", style="danger") if cl.status == "active"
         else btn("Возобновить", f"acl:st:{cl.id}:1", "ok", style="success"),
         btn("Отозвать токен", f"acl:rv:{cl.id}", "cross") if cl.token_hash else None],
        [btn("Профиль", f"auv:{cl.user_id}", "profile"), back("aapi", "API")]), src)


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
