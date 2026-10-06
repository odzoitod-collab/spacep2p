"""The info channel (settings: channel_id): the bot lays it out itself.

«Оформить канал» posts, in order: a greeting under the banner, the contents (pinned) with a link to every post, the
modes of work, then a post per topic — start, buying, selling on your own card, order requisites, operators, teams,
wallet, rules. Numbers (rate, fees, deadlines) come from the settings at the moment of publishing; pressing it again
edits the same posts in place (and re-creates one an admin deleted), so the channel stays current.
Channels do not show custom emoji from bots: posts use plain emoji. The bot must be an admin of the channel with the
right to post, edit and pin.
"""
import logging
from contextlib import suppress

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot import ui
from bot.emoji import back, btn, kb, pe
from bot.handlers.community import channel_id, channel_link
from bot.handlers.wallet import withdraw_terms
from bot.models import Setting, User
from bot.services import audit, money, settings
from bot.services.admins import IsAdmin
from bot.ui import BRAND, cf, deep_link, esc, field, ok, paced, quote, show, title, warn
from bot.ui import card as fields

log = logging.getLogger(__name__)
router = Router()
router.callback_query.filter(IsAdmin())


def _s(key: str) -> str:
    return settings.get(key)


def bot_tag() -> str:
    return f"@{ui.BOT}" if ui.BOT else "бота Strait Pay"


def welcome() -> str:
    return "\n".join([
        f"💱 <b>{BRAND} — обмен USDT ⇄ RUB прямо в Telegram</b>",
        "",
        "<blockquote>Покупайте USDT за рубли переводом на карту и продавайте USDT на свою карту — с защитой "
        "сделки: деньги продавца заморожены, пока вы не получите своё.</blockquote>",
        "",
        "🛡 <b>Безопасно</b> — заморозка, чеки, споры решает администрация",
        "⚡️ <b>Быстро</b> — сделка за 15–30 минут, реквизиты бот подбирает сам",
        f"💰 <b>Выгодно</b> — курс {money.fmt(settings.dec('rate'))} ₽, мерчанты зарабатывают с каждой сделки",
        "👛 <b>Удобно</b> — личный адрес пополнения USDT в сети TON, вывод на любой кошелёк или биржу",
        "",
        f"👉 <b>Начать: {bot_tag()}</b> → «Старт» → короткая заявка на вход.",
        "📌 Всё по порядку — в закреплённом «Содержании».",
    ])


def start3() -> str:
    """The first thing for someone who came from an ad: three steps and where to click."""
    return "\n".join([
        "🚀 <b>Начните здесь: 3 шага</b>",
        "",
        f"<b>1. Откройте {bot_tag()}</b> и нажмите «Старт».",
        "<b>2. Заявка на вход</b> — выберите роль (покупатель или продавец) и оборот. Ответ — обычно в течение "
        "нескольких часов.",
        "<b>3. Вступите в чат и этот канал</b> — бот даст ссылку и откроет главное меню.",
        "",
        "<b>Что дальше — по приоритету:</b>",
        "① Хотите купить USDT → пост «Как купить USDT»",
        "② Есть карта и USDT → «Продажа на свою карту»: доход с каждой сделки",
        "③ Работаете с P2P на Bybit → «Ордерные реквизиты»: заявки под сумму без баланса в боте",
        "④ Есть люди → «Команды и тимлиды»: процент с каждой сделки команды",
        "",
        "<blockquote>Вопросы — в чате сообщества или в боте: «Помощь».</blockquote>",
    ])


def modes() -> str:
    rate, pp = settings.dec("rate"), settings.dec("platform_pct")
    return "\n".join([
        "🧭 <b>Режимы работы</b>",
        "",
        "<blockquote>Выберите свой — в каждом разделе ниже подробная инструкция.</blockquote>",
        "",
        "💸 <b>Покупатель</b>",
        f"Вводите сумму в рублях, бот подбирает реквизиты, вы переводите и прикладываете PDF-чек. Курс "
        f"{money.fmt(rate)} ₽ за USDT, комиссия {money.fmt(pp, 3)}%.",
        "",
        "💳 <b>Мерчант на своей карте</b>",
        f"Пополняете USDT, добавляете карту, выходите на смену — покупатели переводят вам рубли. Доход "
        f"{_s('seller_pct')}% с каждой сделки.",
        "",
        "🧾 <b>Ордерный мерчант</b>",
        f"Берёте заявки под точную сумму по курсу {money.fmt(settings.dec('order_rate'))} ₽. Два способа: "
        "Bybit-ордер (баланс не нужен) или с баланса бота.",
        "",
        "🧑‍💻 <b>Оператор</b>",
        "Принимает Bybit-ордера мерчантов, выдаёт покупателю реквизиты и подтверждает оплату.",
        "",
        "🫂 <b>Тимлид</b>",
        f"Собирает свою команду по реферальной ссылке и получает {_s('team_pct')}% со сделок участников.",
    ])


def start() -> str:
    return "\n".join([
        "🚪 <b>Как начать</b>",
        "",
        "<b>1. Заявка на вход.</b> Нажмите «Старт» в боте, выберите роль (продавец или покупатель) и оборот в день. "
        "Продавцы прикладывают скриншот баланса — его видит только администрация.",
        "",
        "<b>2. Сообщество.</b> После одобрения вступите в чат и подпишитесь на этот канал — бот даст личную "
        "ссылку и откроет главное меню.",
        "",
        "<b>3. Главное меню.</b> Кошелёк, «RUB ⇄ USDT» (купить), «USDT ⇄ RUB» (продать), ордерные реквизиты, "
        "команда и помощь — всё в одном сообщении, оно меняется на месте.",
        "",
        "<blockquote>Пришли по ссылке тимлида? Вы сразу в его команде.</blockquote>",
    ])


def buy() -> str:
    return "\n".join([
        "💸 <b>Как купить USDT</b>",
        "",
        "<b>1.</b> «RUB ⇄ USDT» → сумма в рублях. Бот покажет, сколько USDT придёт.",
        "<b>2.</b> Бот подберёт реквизиты: карта продавца или ордерные реквизиты под вашу сумму.",
        f"<b>3.</b> Переведите <b>ровно</b> эту сумму одним платежом за {_s('deal_minutes')} мин. Комментарий не пишите.",
        "<b>4.</b> Приложите PDF-чек из банка. Продавец подтверждает — USDT на балансе.",
        "",
        f"<blockquote>Продавец молчит {_s('confirm_minutes')} мин после чека — открывайте спор: администрация "
        "решит по чеку и выписке. Деньги продавца всё это время заморожены.</blockquote>",
    ])


def sell() -> str:
    return "\n".join([
        "💳 <b>Продажа на свою карту</b>",
        "",
        "<b>1.</b> Пополните кошелёк USDT — из баланса замораживается сумма каждой сделки.",
        "<b>2.</b> «USDT ⇄ RUB» → добавьте карту или СБП: банк, номер, ФИО, суммы от и до.",
        "<b>3.</b> Выйдите на смену — карты видны покупателям.",
        "<b>4.</b> Пришёл чек — проверьте поступление в банке и подтвердите. USDT уходят покупателю.",
        "",
        f"<blockquote>Доход — {_s('seller_pct')}% с каждой сделки. Смена закончится сама после "
        f"{settings.human('online_minutes')} без действий.</blockquote>",
    ])


def orders() -> str:
    return "\n".join([
        "🧾 <b>Ордерные реквизиты</b>",
        "",
        f"Ордерный мерчант берёт заявки покупателей под точную сумму и продаёт по фиксированному курсу "
        f"<b>{money.fmt(settings.dec('order_rate'))} ₽</b>. Анкета — в боте, «Ордерные реквизиты».",
        "",
        "<b>Bybit-ордер</b> — баланс в боте не нужен:",
        f"• «Взять» → за <b>{_s('order_link_minutes')} мин</b> пришлите ссылку на свой ордер на Bybit P2P. "
        "Нет ссылки — заявка не засчитана и уходит другим.",
        "• Оператор заходит в ордер и выдаёт покупателю реквизиты.",
        f"• В ордере нет реквизитов — это пропуск. <b>{_s('strike_limit')} пропуска подряд — пауза "
        f"{settings.human('strike_sleep_hours')}</b>, заявки не приходят.",
        f"• Оператор оценивает каждый ордер от 1 до 10. После {_s('rep_min_count')} оценок — репутация: ниже "
        f"{_s('rep_mid')} Bybit-заявки до {money.fmt(settings.dec('rep_mid_max_rub'))} ₽, ниже {_s('rep_low')} — "
        "только с баланса.",
        "",
        "<b>С баланса</b> — замораживаем ваши USDT, реквизиты выдаёте сами, оплату подтверждаете сами.",
        "",
        "<blockquote>Берите только те заявки, под которые у вас точно есть реквизиты.</blockquote>",
    ])


def operators() -> str:
    return "\n".join([
        "🧑‍💻 <b>Операторы</b>",
        "",
        "Оператор работает с Bybit-ордерами мерчантов:",
        "• «Принять ордер» — первый принявший получает ссылку, у остальных ордер пропадает;",
        "• заходит в ордер, выдаёт покупателю реквизиты и подтверждает оплату;",
        "• реквизитов в ордере нет — «Проблема с ордером» → «Мерчант не дал реквизиты»: заявка уходит другим;",
        "• после ордера оценивает мерчанта от 1 до 10 — из этого складывается его репутация.",
        "",
        "<blockquote>USDT ордера приходят оператору на Bybit — это его долг перед площадкой, он гасит его "
        "переводом USDT (TON) на свой адрес погашения или с баланса в боте.</blockquote>",
    ])


def teams() -> str:
    return "\n".join([
        "🫂 <b>Команды и тимлиды</b>",
        "",
        f"Тимлид получает <b>{_s('team_pct')}%</b> со сделок каждого участника своей команды.",
        "",
        "• «Команда» → «Создать команду» — заявку одобряет администрация;",
        "• своя реферальная ссылка: кто пришёл по ней — в команде;",
        "• свой чат: бот публикует в нём все заявки покупателей;",
        "• доход копится на <b>командном балансе</b> → «На основной баланс» → вывод в крипту из «Кошелька».",
    ])


def wallet() -> str:
    return "\n".join([
        "👛 <b>Кошелёк: пополнение и вывод</b>",
        "",
        "<b>Пополнение</b> — комиссия " + f"{_s('deposit_fee')}%:",
        "• у каждого свой постоянный адрес USDT в сети TON — переводите с биржи или кошелька;",
        "• зачисление автоматически за 1–2 минуты после подтверждения в сети, memo не нужен.",
        "",
        "<b>Вывод</b>:",
        f"• на любой TON-кошелёк или биржу (с memo) — {withdraw_terms()};",
        "• отправляется автоматически, ссылка на транзакцию приходит в чат.",
        "",
        "<blockquote>Если у сервиса временно не хватает USDT, вывод встаёт в очередь и уходит автоматически — "
        "место в очереди видно в «Кошельке», до отправки его можно отменить.</blockquote>",
    ])


def rules() -> str:
    return "\n".join([
        "🛡 <b>Правила и безопасность</b>",
        "",
        "• Общение по сделке — только через бота. Ссылки и @юзернеймы в сообщениях запрещены.",
        "• Переводите рубли только на реквизиты из сделки и только после её создания.",
        "• Чек — PDF из приложения банка. Поддельный чек — бан и спор не в вашу пользу.",
        "• Обмен в обход бота — бан без возврата к работе.",
        "",
        "<blockquote>Спор решает администрация по чеку, выписке и материалам сторон. Решение и комментарий "
        "придут обеим сторонам.</blockquote>",
    ])


# key (stable: stored message ids depend on it), title in the contents, body
# key (stable: stored message ids depend on it), title in the contents, body. The order is the priority for people
# who come from an ad: what it is, how to start, buying, earning, then the details.
POSTS = [("welcome", "Что такое Strait Pay", welcome), ("contents", "Содержание", None),
         ("start3", "Начните здесь: 3 шага", start3), ("buy", "Как купить USDT", buy),
         ("sell", "Продажа на свою карту", sell), ("orders", "Ордерные реквизиты", orders),
         ("teams", "Команды и тимлиды", teams), ("wallet", "Кошелёк", wallet), ("modes", "Все режимы работы", modes),
         ("start", "Вход и роли", start), ("operators", "Операторы", operators), ("rules", "Правила", rules)]


def post_url(chat: int, username: str | None, mid: int) -> str:
    return f"https://t.me/{username}/{mid}" if username else f"https://t.me/c/{str(chat).removeprefix('-100')}/{mid}"


def contents(chat: int, username: str | None, ids: dict[str, int]) -> str:
    lines = ["📌 <b>Содержание</b>", "",
             f"<blockquote>Новичок? Сначала «Начните здесь», потом свой раздел. Бот — {bot_tag()}</blockquote>", ""]
    n = 0
    for key, name, body in POSTS:
        if body is None or key == "welcome":
            continue
        n += 1
        lines.append(f"{n}. <a href=\"{post_url(chat, username, ids[key])}\">{name}</a>" if key in ids
                     else f"{n}. {name}")
    return "\n".join(lines)


async def _send_or_edit(bot: Bot, chat: int, mid: int | None, text: str, markup, banner: bool) -> tuple[int, str]:
    """(message id, "new" / "updated" / "same")."""
    if mid:
        edit = (lambda t: bot.edit_message_caption(chat_id=chat, message_id=mid, caption=t, reply_markup=markup)) \
            if banner else \
            (lambda t: bot.edit_message_text(text=t, chat_id=chat, message_id=mid, reply_markup=markup,
                                             disable_web_page_preview=True))
        try:
            await paced(lambda: edit(text))
            return mid, "updated"
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return mid, "same"
            # deleted by an admin: post it again
    if banner and ui.banner_path():
        async with ui._upload:
            m = await ui.send_banner(bot, chat, text, markup)
    else:
        m = await paced(lambda: bot.send_message(chat, text, reply_markup=markup, disable_web_page_preview=True,
                                                 disable_notification=True))
    return m.message_id, "new"


async def publish(bot: Bot, s: AsyncSession) -> dict[str, int]:
    """Post or update every post of the channel; the contents get the links and are pinned. Commits.
    Returns how many posts are new / updated / the same."""
    chat = channel_id()
    info = await bot.get_chat(chat)
    username = getattr(info, "username", None)
    markup = kb(btn("Открыть бота", url=await deep_link(bot, "menu"), style="success"))
    ids, done = {}, {"new": 0, "updated": 0, "same": 0}
    for key, _, body in POSTS:
        row = await s.get(Setting, f"chpost:{key}")
        old = int(row.value) if row else None
        text = contents(chat, username, ids) if body is None else body()
        mid, what = await _send_or_edit(bot, chat, old, text, None if body is None else markup,
                                        banner=key == "welcome" and ui.visible_len(text) <= ui.CAPTION_LIMIT)
        ids[key] = mid
        done[what] += 1
        await s.merge(Setting(key=f"chpost:{key}", value=str(mid)))
        await s.commit()
    # the contents were posted before the sections existed: now with a link to each
    mid, what = await _send_or_edit(bot, chat, ids["contents"], contents(chat, username, ids), None, banner=False)
    with suppress(TelegramAPIError):
        await bot.pin_chat_message(chat, ids["contents"], disable_notification=True)
    return done


# ---------- autoposts: a promo every channel_autopost_hours, in turn ----------

def _cta() -> str:
    return f"👉 <b>{bot_tag()}</b> — откройте и нажмите «Старт»"


PROMOS = [
    lambda: "\n".join([
        "📊 <b>Курсы Strait Pay сегодня</b>", "",
        f"💸 Покупка: <b>1 USDT = {money.fmt(settings.dec('rate'))} ₽</b> · комиссия {money.fmt(settings.dec('platform_pct'), 3)}%",
        f"💳 Мерчант на своей карте: <b>{money.fmt(settings.dec('seller_pct'), 3)}%</b> с каждой сделки",
        f"🧾 Ордерный мерчант: <b>{money.fmt(settings.dec('order_rate'))} ₽</b> за USDT", "", _cta()]),
    lambda: "\n".join([
        "🛡 <b>Почему с нами безопасно</b>", "",
        "• деньги продавца заморожены до конца сделки — уйти с вашими нельзя",
        "• оплата подтверждается чеком, спор решает администрация по выписке",
        "• общение только в чате сделки, ссылки и чужие контакты не проходят",
        "• вход по заявке: в боте только проверенные люди", "", _cta()]),
    lambda: "\n".join([
        "💳 <b>Зарабатывайте на своей карте</b>", "",
        f"Пополните USDT, добавьте карту, выйдите на смену — покупатели переводят вам рубли, вы подтверждаете и "
        f"получаете <b>{money.fmt(settings.dec('seller_pct'), 3)}%</b> с каждой сделки.",
        "Смена — когда удобно, лимиты по карте — ваши.", "", _cta()]),
    lambda: "\n".join([
        "🧾 <b>Работаете на Bybit P2P? Берите заявки под сумму</b>", "",
        f"Заявки покупателей приходят всем ордерным мерчантам. Взяли — за {settings.get('order_link_minutes')} мин "
        "пришлите ссылку на свой Bybit-ордер, оператор выдаст покупателю реквизиты. Баланс в боте не нужен.",
        f"Курс для ордера — <b>{money.fmt(settings.dec('order_rate'))} ₽</b> за USDT.", "", _cta()]),
    lambda: "\n".join([
        "🫂 <b>Соберите команду — получайте процент</b>", "",
        f"Тимлид получает <b>{money.fmt(settings.dec('team_pct'), 3)}%</b> со сделок каждого участника: своя "
        "реферальная ссылка, свой чат с заявками, командный баланс с выводом в крипту.", "", _cta()]),
    lambda: "\n".join([
        "⚡️ <b>Купить USDT за 15 минут</b>", "",
        "1. Сумма в рублях — бот покажет, сколько USDT придёт",
        "2. Перевод на реквизиты из сделки",
        "3. PDF-чек из банка",
        "4. Продавец подтверждает — USDT на балансе, вывод на любой TON-кошелёк", "", _cta()]),
    lambda: "\n".join([
        "👛 <b>Вывод USDT куда удобно</b>", "",
        "USDT в сети TON: свой адрес пополнения прямо в боте, вывод на любой кошелёк или биржу — автоматически, с "
        "ссылкой на транзакцию.", "", _cta()]),
]


async def post_promo(bot: Bot, s: AsyncSession) -> int:
    """The next promo of the rotation into the channel. Commits. Returns its number (1-based)."""
    from bot.models import now
    chat = channel_id()
    row = await s.get(Setting, "chpromo")
    i = (int(row.value.split(":")[0]) if row else -1) + 1
    i %= len(PROMOS)
    text = PROMOS[i]()
    markup = kb(btn("Открыть бота", url=await deep_link(bot, "menu"), style="success"))
    if i == 0 and ui.banner_path() and ui.visible_len(text) <= ui.CAPTION_LIMIT:  # the rates go under the banner
        async with ui._upload:
            await ui.send_banner(bot, chat, text, markup, silent=False)
    else:
        await paced(lambda: bot.send_message(chat, text, reply_markup=markup, disable_web_page_preview=True))
    await s.merge(Setting(key="chpromo", value=f"{i}:{now().isoformat()}"))
    await s.commit()
    return i + 1


async def autopost(bot: Bot, s: AsyncSession) -> bool:
    """A promo if channel_autopost_hours passed since the last one. True if posted."""
    from datetime import datetime, timedelta
    from bot.models import now
    hours = settings.num("channel_autopost_hours")
    if not channel_id() or not hours:
        return False
    row = await s.get(Setting, "chpromo")
    if row and now() - datetime.fromisoformat(row.value.split(":", 1)[1]) < timedelta(hours=hours):
        return False
    try:
        await post_promo(bot, s)
    except TelegramAPIError as e:
        log.warning("channel autopost: %s", e)
        return False
    return True


# ---------- admin: «Инфо-канал» ----------

async def channel_screen(bot: Bot, s: AsyncSession, admin: User, src=None, note: str = ""):
    chat = channel_id()
    posted = sum([await s.get(Setting, f"chpost:{key}") is not None for key, _, _ in POSTS])
    state = cf("Статус", "не задан — задайте ID канала (бот — админ канала с правом публиковать)", icon="info")
    if chat:
        try:
            info = await bot.get_chat(chat)
            me = await bot.get_chat_member(chat, (await bot.me()).id)
            can = getattr(me, "can_post_messages", None) is not False and me.status == "administrator"
            state = fields(cf("Канал", f"<b>{esc(info.title or str(chat))}</b> · <code>{chat}</code>", icon="bell"),
                           cf("Права бота", "публиковать — да" if can else "<b>нет прав на публикацию</b>", icon="lock"))
        except TelegramAPIError as e:
            state = cf("Канал", f"<code>{chat}</code> — бот его не видит: {esc(str(e)[:100])}", icon="warn")
    link = await channel_link(bot, s) if chat else None
    await show(bot, admin, "\n".join([
        title(pe("bell"), "Инфо-канал"),
        "",
        state,
        field("Постов опубликовано", f"{posted} из {len(POSTS)}"),
        field("Вступление обязательно", settings.human("join_required")),
        field("Автопосты", f"раз в {settings.human('channel_autopost_hours')} · {len(PROMOS)} промо по кругу"
              if settings.num("channel_autopost_hours") else "выключены"),
        "",
        quote("«Оформить канал» публикует приветствие, содержание (закреп) и пост по каждому разделу: режимы работы, "
              "вход, покупка, продажа, ордера, операторы, команды, кошелёк, правила. Нажмёте снова — посты "
              "обновятся на месте с текущими курсом и комиссиями."),
    ]) + note, kb(
        btn("Оформить канал" if posted < len(POSTS) else "Обновить посты", "achn:pub", "refresh", style="success")
        if chat else None,
        btn("Опубликовать промо сейчас", "achn:promo", "bell") if chat else None,
        btn("Автопосты: частота", "acc:channel_autopost_hours", "clock"),
        btn("Открыть канал", url=link) if link else None,
        btn("Изменить ID канала" if chat else "Задать ID канала", "acc:channel_id", "pencil"),
        back("ach", "Чат и рассылка")), src)


@router.callback_query(F.data == "achn")
async def cb_channel(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    await channel_screen(bot, s, user, c)


@router.callback_query(F.data == "achn:promo")
async def cb_promo(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    if not channel_id():
        return await c.answer("Сначала задайте ID канала", show_alert=True)
    try:
        n = await post_promo(bot, s)
    except TelegramAPIError as e:
        return await channel_screen(bot, s, user, c, warn(f"Не получилось: {esc(str(e)[:150])}"))
    await channel_screen(bot, s, user, c, ok(f"Опубликовано промо {n} из {len(PROMOS)}"))


@router.callback_query(F.data == "achn:pub")
async def cb_publish(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    if not channel_id():
        return await c.answer("Сначала задайте ID канала", show_alert=True)
    await c.answer("Публикуем…")
    try:
        done = await publish(bot, s)
    except TelegramAPIError as e:
        log.warning("channel publish: %s", e)
        return await channel_screen(bot, s, user, c, warn(f"Не получилось: {esc(str(e)[:150])}. Бот должен быть "
                                                          "админом канала с правом публиковать и закреплять."))
    audit.log(s, user.id, "channel_publish", "", f"new {done['new']}, updated {done['updated']}")
    await s.commit()
    await channel_screen(bot, s, user, c, ok(f"Канал оформлен: новых постов {done['new']}, обновлено "
                                             f"{done['updated']}, без изменений {done['same']}"))
