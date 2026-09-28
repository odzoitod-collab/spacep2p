"""Strait Pay API for services: application form, then the owner's console (token, webhook, limits, orders)."""
import secrets
from datetime import timedelta

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import config
from bot.emoji import back, btn, kb, pe
from bot.models import ApiApplication, ApiClient, Deal, User, now
from bot.services import api, deals, events, money
from bot.ui import BRAND, at, esc, ok, quote, show, title, warn

router = Router()
TRAFFIC = ["Сайт / интернет-магазин", "Telegram-бот", "Мобильное приложение", "Обменник / платёжный сервис"]
VOLUME = ["до 1 млн ₽", "1–5 млн ₽", "5–20 млн ₽", "более 20 млн ₽"]
REAPPLY_AFTER = timedelta(hours=24)


class ApiForm(StatesGroup):
    project = State()
    url = State()
    traffic = State()
    volume = State()
    about = State()


class ApiEdit(StatesGroup):
    webhook = State()


def docs_url() -> str:
    return f"{config.api_url}/docs"


# ---------- entry ----------

@router.callback_query(F.data == "api")
async def cb_api(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    await api_screen(bot, s, user, c)


async def api_screen(bot: Bot, s: AsyncSession, user: User, src=None, note: str = ""):
    client = await s.scalar(select(ApiClient).where(ApiClient.user_id == user.id))
    if client:
        return await console(bot, s, user, client, src, note)
    app = await s.scalar(select(ApiApplication).where(ApiApplication.user_id == user.id)
                         .order_by(ApiApplication.id.desc()).limit(1))
    intro = [
        title(pe("key"), f"{BRAND} API"),
        "",
        "Принимайте оплату рублями, получайте USDT: ваш сервис открывает заказ на нужную сумму, показывает "
        "покупателю реквизиты продавца, отправляет PDF-чек и получает статус «успешно» — USDT зачисляются на ваш "
        "баланс на тех же условиях, что и при обычной покупке.",
        quote(f"{pe('swap')} Заказ на любую доступную сумму · реквизиты мерчанта в ответе",
              f"{pe('doc')} Загрузка PDF-чека · статусы и вебхуки с подписью HMAC",
              f"{pe('wallet')} Баланс и вывод — в Кошельке: xRocket или USDT в сети TON"),
    ]
    if app and app.status == "pending":
        lines = intro + ["", f"{pe('clock')} <b>Заявка «{esc(app.project)}» на рассмотрении</b> с {at(app.created_at, 'dt')}. "
                             "Ответ придёт в этот чат."]
        markup = kb(btn("Документация API", icon="doc", url=docs_url()), back("menu", "В меню"))
    else:
        if app and app.status == "rejected":
            intro += ["", f"{pe('cross')} Прошлая заявка отклонена"
                      + (f": <i>{esc(app.reason)}</i>" if app.reason else "") + "."]
        wait = app and app.status == "rejected" and now() - deals.aware(app.decided_at) < REAPPLY_AFTER
        lines = intro + ["", f"Подать заявку снова можно после {at(deals.aware(app.decided_at) + REAPPLY_AFTER, 'dt')}."
                         if wait else "Доступ выдаётся по заявке: расскажите о проекте, трафике и объёме."]
        markup = kb(None if wait else btn("Подать заявку", "api:apply", "pencil", style="primary"),
                    btn("Документация API", icon="doc", url=docs_url()), back("menu", "В меню"))
    await show(bot, user, "\n".join(lines) + note, markup, src)


# ---------- application form ----------

def _step(n: int, text: str) -> str:
    return f"{title(pe('pencil'), 'Заявка на API')} · шаг {n} из 5\n\n{text}"


@router.callback_query(F.data == "api:apply")
async def cb_apply(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    pending = await s.scalar(select(ApiApplication.id).where(ApiApplication.user_id == user.id,
                                                             ApiApplication.status == "pending"))
    if pending or await s.scalar(select(ApiClient.id).where(ApiClient.user_id == user.id)):
        return await api_screen(bot, s, user, c)
    await state.set_state(ApiForm.project)
    await state.set_data({})
    await show(bot, user, _step(1, "Как называется ваш проект или компания?"), kb(back("api", "Отмена")), c)


@router.message(ApiForm.project, F.text)
async def msg_project(m: Message, bot: Bot, user: User, state: FSMContext):
    v = " ".join(m.text.split())
    if not 2 <= len(v) <= 100:
        return await show(bot, user, _step(1, "Как называется ваш проект?") + warn("От 2 до 100 символов"),
                          kb(back("api", "Отмена")))
    await state.update_data(project=v)
    await state.set_state(ApiForm.url)
    await show(bot, user, _step(2, "Ссылка на проект: сайт, Telegram-бот или канал (например, https://shop.ru или "
                                   "@shop_bot)."), kb(back("api", "Отмена")))


@router.message(ApiForm.url, F.text)
async def msg_url(m: Message, bot: Bot, user: User, state: FSMContext):
    v = m.text.strip()
    if not 3 <= len(v) <= 200 or " " in v:
        return await show(bot, user, _step(2, "Ссылка на проект:") + warn("Одна ссылка без пробелов, до 200 символов"),
                          kb(back("api", "Отмена")))
    await state.update_data(url=v)
    await state.set_state(ApiForm.traffic)
    await show(bot, user, _step(3, "Откуда приходят покупатели? Выберите или напишите своими словами."), kb(
        *[btn(t, f"api:t:{i}", "people") for i, t in enumerate(TRAFFIC)], back("api", "Отмена")))


async def _ask_volume(bot, user, state: FSMContext, traffic: str, src=None):
    await state.update_data(traffic=traffic)
    await state.set_state(ApiForm.volume)
    await show(bot, user, _step(4, "Ожидаемый оборот в месяц, ₽? Выберите или напишите цифрой."),
               kb(*[btn(v, f"api:v:{i}", "stats") for i, v in enumerate(VOLUME)], back("api", "Отмена")), src)


@router.callback_query(ApiForm.traffic, F.data.regexp(r"^api:t:(\d)$"))
async def cb_traffic(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    i = int(c.data.split(":")[2])
    if i >= len(TRAFFIC):
        return await c.answer()
    await _ask_volume(bot, user, state, TRAFFIC[i], c)


@router.message(ApiForm.traffic, F.text)
async def msg_traffic(m: Message, bot: Bot, user: User, state: FSMContext):
    v = " ".join(m.text.split())
    if not 3 <= len(v) <= 200:
        return await show(bot, user, _step(3, "Откуда приходят покупатели?") + warn("От 3 до 200 символов"),
                          kb(back("api", "Отмена")))
    await _ask_volume(bot, user, state, v)


async def _ask_about(bot, user, state: FSMContext, volume: str, src=None):
    await state.update_data(volume=volume)
    await state.set_state(ApiForm.about)
    await show(bot, user, _step(5, "Коротко о проекте: что продаёте, средний чек, есть ли опыт с P2P. "
                                   "Можно пропустить."),
               kb(btn("Пропустить", "api:skip", "next"), back("api", "Отмена")), src)


@router.callback_query(ApiForm.volume, F.data.regexp(r"^api:v:(\d)$"))
async def cb_volume(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    i = int(c.data.split(":")[2])
    if i >= len(VOLUME):
        return await c.answer()
    await _ask_about(bot, user, state, VOLUME[i], c)


@router.message(ApiForm.volume, F.text)
async def msg_volume(m: Message, bot: Bot, user: User, state: FSMContext):
    v = " ".join(m.text.split())
    if not 1 <= len(v) <= 100:
        return await show(bot, user, _step(4, "Оборот в месяц, ₽:") + warn("До 100 символов"), kb(back("api", "Отмена")))
    await _ask_about(bot, user, state, v)


@router.callback_query(ApiForm.about, F.data == "api:skip")
async def cb_skip(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await _submit(bot, s, user, state, "", c)


@router.message(ApiForm.about, F.text)
async def msg_about(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    v = m.text.strip()
    if len(v) > 1000:
        return await show(bot, user, _step(5, "Коротко о проекте:") + warn("До 1000 символов"),
                          kb(btn("Пропустить", "api:skip", "next"), back("api", "Отмена")))
    await _submit(bot, s, user, state, v)


async def _submit(bot, s: AsyncSession, user: User, state: FSMContext, about: str, src=None):
    data = await state.get_data()
    await state.clear()
    if await s.scalar(select(ApiApplication.id).where(ApiApplication.user_id == user.id,
                                                      ApiApplication.status == "pending")):
        return await api_screen(bot, s, user, src)
    app = ApiApplication(user_id=user.id, project=data["project"], url=data["url"], traffic=data["traffic"],
                         volume=data["volume"], about=about)
    s.add(app)
    await s.flush()
    events.add(s, f"apa:{app.id}", "submitted", f"Заявка на API: {app.project} · {app.url} · {app.traffic} · "
               f"{app.volume}", user.id, alert=True)
    await api_screen(bot, s, user, src, ok("Заявка отправлена. Обычно рассматриваем в течение суток."))


# ---------- console of an approved client ----------

async def console(bot: Bot, s: AsyncSession, user: User, client: ApiClient, src=None, note: str = ""):
    used = await api.usage(s, client)
    total, success = (await s.execute(select(func.count(Deal.id), func.count(Deal.id).filter(
        Deal.status == "completed")).where(Deal.api_client_id == client.id))).one()
    active = client.status == "active"
    await show(bot, user, "\n".join([
        title(pe("key"), f"{BRAND} API · {esc(client.project)}"),
        f"{pe('ok') if active else pe('pause')} <b>{'Доступ активен' if active else 'Доступ приостановлен'}</b>"
        + ("" if active else " — напишите в поддержку"),
        "",
        quote(f"{pe('search')} Base URL: <code>{config.api_url}/v1</code>",
              f"{pe('key')} Токен: " + (f"<code>{api.TOKEN_PREFIX}…{client.token_hint}</code> · выпущен "
                                        f"{at(client.token_at, 'dt')}" if client.token_hash else "<b>не выпущен</b>"),
              f"{pe('bell')} Вебхук: " + (f"<code>{esc(client.webhook_url)}</code>" if client.webhook_url
                                         else "не задан — статусы только по запросу GET /v1/orders/{id}")),
        title(pe("filter"), "Лимиты"),
        quote(f"{pe('ruble')} Заказ: от {money.fmt(client.min_rub)} до {money.fmt(client.max_rub)} ₽",
              f"{pe('clock')} В сутки: {money.fmt(used['today_rub'])} из {money.fmt(client.daily_rub)} ₽",
              f"{pe('fire')} Неоплаченных заказов: {used['open_orders']} из {client.max_open}",
              f"{pe('stats')} Запросов в секунду: {client.rps}"),
        title(pe("wallet"), "Баланс API"),
        quote(f"{pe('dollar')} Доступно: <b>{money.usdt(user.balance)} USDT</b>"
              + (f" · заморожено {money.usdt(user.frozen)}" if user.frozen else ""),
              f"{pe('ok')} Заказов: {total}, успешных: {success}"),
        "USDT по успешным заказам приходят на этот баланс; вывести — в «Кошелёк».",
    ]) + note, kb(
        btn("Перевыпустить токен" if client.token_hash else "Выпустить токен", "api:tok", "key",
            style=None if client.token_hash else "primary") if active else None,
        [btn("Вебхук", "api:wh", "bell"), btn("Секрет вебхука", "api:sec", "lock")],
        [btn("Заказы API", "api:orders", "list"), btn("Документация", icon="doc", url=docs_url())],
        [btn("Кошелёк", "w", "wallet"), back("menu", "В меню")],
    ), src)


async def _client(s: AsyncSession, user: User) -> ApiClient | None:
    return await s.scalar(select(ApiClient).where(ApiClient.user_id == user.id))


@router.callback_query(F.data == "api:tok")
async def cb_token_ask(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    client = await _client(s, user)
    if not client or client.status != "active":
        return await api_screen(bot, s, user, c)
    if not client.token_hash:
        return await _issue(bot, s, user, client, c)
    await show(bot, user, "\n".join([
        f"{pe('warn')} <b>Перевыпустить токен?</b>",
        "",
        f"Старый токен <code>…{client.token_hint}</code> перестанет работать сразу — обновите его на сервере.",
    ]), kb([btn("Перевыпустить", "api:tok2", "key", style="danger"), back("api", "Отмена")]), c)


@router.callback_query(F.data == "api:tok2")
async def cb_token(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    client = await _client(s, user)
    if not client or client.status != "active":
        return await api_screen(bot, s, user, c)
    await _issue(bot, s, user, client, c)


async def _issue(bot, s: AsyncSession, user: User, client: ApiClient, src):
    token, client.token_hash, client.token_hint = api.new_token()
    client.token_at = now()
    events.add(s, f"apc:{client.id}", "token", f"Токен выпущен (…{client.token_hint})", user.id)
    await show(bot, user, "\n".join([
        title(pe("key"), "Ваш API-токен"),
        "",
        f"<code>{token}</code>",
        "",
        f"{pe('lock')} <b>Сохраните его сейчас</b> — повторно показать нельзя, мы храним только отпечаток. "
        "Передавайте в заголовке <code>Authorization: Bearer …</code>, только с сервера, никогда из браузера.",
        "После «Я сохранил» токен исчезнет с экрана.",
    ]), kb(btn("Я сохранил", "api", "ok", style="success")), src)


@router.callback_query(F.data == "api:sec")
async def cb_secret(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    client = await _client(s, user)
    if not client:
        return await api_screen(bot, s, user, c)
    await show(bot, user, "\n".join([
        title(pe("lock"), "Секрет вебхука"),
        "",
        f"<code>{client.webhook_secret}</code>",
        "",
        "Каждый вебхук подписан: <code>X-Strait-Signature: sha256=HMAC_SHA256(секрет, timestamp + \".\" + тело)</code>. "
        "Проверяйте подпись и время — пример в документации.",
    ]), kb(btn("Новый секрет", "api:sec2", "refresh"), back("api", "API")), c)


@router.callback_query(F.data == "api:sec2")
async def cb_secret_new(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    client = await _client(s, user)
    if client:
        client.webhook_secret = secrets.token_hex(32)
        events.add(s, f"apc:{client.id}", "secret", "Секрет вебхука перевыпущен", user.id)
    await cb_secret(c, bot, s, user)


@router.callback_query(F.data == "api:wh")
async def cb_webhook(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    client = await _client(s, user)
    if not client:
        return await api_screen(bot, s, user, c)
    await state.set_state(ApiEdit.webhook)
    await show(bot, user, _webhook_text(client), _webhook_kb(client), c)


def _webhook_text(client: ApiClient, err: str = "") -> str:
    return "\n".join([
        title(pe("bell"), "Вебхук"),
        "",
        f"Сейчас: <code>{esc(client.webhook_url)}</code>" if client.webhook_url else "Сейчас: не задан",
        "",
        "Отправьте URL вашего сервера (только https://). На него придёт POST при каждой смене статуса заказа; "
        "ответьте 2xx, иначе повторим с растущей паузой до суток. «-» — отключить.",
    ]) + (warn(err) if err else "")


def _webhook_kb(client: ApiClient):
    return kb(btn("Тест вебхука", "api:whtest", "refresh") if client.webhook_url else None,
              back("api", "Назад"))


@router.message(ApiEdit.webhook, F.text)
async def msg_webhook(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    client = await _client(s, user)
    if not client:
        await state.clear()
        return await api_screen(bot, s, user)
    url = m.text.strip()
    if url != "-" and (problem := await api.url_problem(url)):
        return await show(bot, user, _webhook_text(client, problem), _webhook_kb(client))
    await state.clear()
    client.webhook_url = None if url == "-" else url
    events.add(s, f"apc:{client.id}", "webhook", f"Вебхук: {client.webhook_url or 'отключён'}", user.id)
    await console(bot, s, user, client, note=ok("Вебхук сохранён" if client.webhook_url else "Вебхук отключён"))


@router.callback_query(F.data == "api:whtest")
async def cb_webhook_test(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    client = await _client(s, user)
    if not client or not client.webhook_url:
        return await c.answer("Сначала задайте URL вебхука", show_alert=True)
    delivered, what = await api.post(client, api.payload(0, "test", {}, kind="test"), 0)
    await c.answer(("Доставлен: " if delivered else "Не доставлен: ") + what, show_alert=True)


@router.callback_query(F.data == "api:orders")
async def cb_orders(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    client = await _client(s, user)
    if not client:
        return await api_screen(bot, s, user, c)
    rows = (await s.scalars(select(Deal).where(Deal.api_client_id == client.id)
                            .order_by(Deal.id.desc()).limit(15))).all()
    lines = [f"#{d.id} · {money.fmt(d.amount_rub)} ₽ → {money.usdt(d.buyer_credit)} USDT · "
             f"<code>{api.STATUS[d.status]}</code>" + (f" · {esc(d.external_id)}" if d.external_id else "")
             for d in rows]
    await show(bot, user, title(pe("list"), "Заказы API") + "\n\n" + (
        "Последние 15. Статус — как в API.\n" + quote(*lines) if rows else "Заказов пока нет."),
        kb(back("api", "API")), c)

