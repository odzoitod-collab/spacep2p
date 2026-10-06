"""Entry by application (setting signup_review = 1).

A new user does not see the bot until an admin lets him in: 1) who he is — a P2P seller or a buyer; 2) the daily
turnover he can handle; 3) a seller adds a screenshot of his balance or turnover; then he checks and sends the
application. It is posted in the log chat's «📝 Заявки на вход» topic (with the screenshot) with «Одобрить» /
«Отклонить» right under it, and is listed in «Админ-панель → Заявки на вход». Approved: a message with «Открыть главное
меню»; rejected: the reason, a new application after 24 h. Users from before (access = approved) are not affected.

Every update of a user who is not let in yet lands in `router` (its filter): nothing else of the bot is reachable.
"""
import logging
from contextlib import suppress
from datetime import timedelta

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.admins import IsAdmin
from bot.emoji import back, btn, kb, pe
from bot.handlers import logchat
from bot.models import LogMessage, Signup, Team, User, now
from bot.services import audit, deals, events, settings, teams
from bot.ui import (BRAND, TAGLINE, alink, at, card, cf, clean, doc, esc, files_to, mark, notify, ok, quote, safe_text,
                    show, stamp, title, verdict, warn)

log = logging.getLogger(__name__)
ROLES = {"seller": "P2P-продавец", "buyer": "Покупатель"}
TURNOVER = ["до 100 000 ₽", "100 000 – 500 000 ₽", "500 000 – 1 000 000 ₽", "более 1 000 000 ₽"]
REAPPLY_AFTER = timedelta(hours=24)
REJECT_DEFAULT = "заявка не подходит под условия сервиса"


class Gated(Filter):
    """The user still waits to be let in (or fills the application)."""

    async def __call__(self, event, user: User | None = None, is_admin: bool = False, s=None) -> bool:
        if user is None or is_admin or user.access == "approved" or settings.get("signup_review") != "1":
            return False
        from bot.services import operators
        return not (s is not None and await operators.is_operator(s, user.id))  # staff: let in whatever they applied


router = Router()
router.message.filter(Gated())
router.callback_query.filter(Gated())
admin_router = Router()  # decisions: in the log chat's topic or in the admin panel
admin_router.message.filter(IsAdmin())
admin_router.callback_query.filter(IsAdmin())


class SignupForm(StatesGroup):
    turnover = State()
    proof = State()


class SignupReject(StatesGroup):
    reason = State()


def _step(n: int, of: int, text: str, err: str = "") -> str:
    return f"{title(pe('pencil'), 'Заявка на вход')} · шаг {n} из {of}\n\n{text}" + (warn(err) if err else "")


async def last_signup(s: AsyncSession, uid: int) -> Signup | None:
    return await s.scalar(select(Signup).where(Signup.user_id == uid).order_by(Signup.id.desc()).limit(1))


async def gate_screen(bot: Bot, s: AsyncSession, user: User, state: FSMContext, src=None, note: str = ""):
    """Where the user is in the entry flow: welcome, waiting for the decision, or rejected."""
    su = await last_signup(s, user.id)
    if user.access == "pending" and su and su.status == "pending":
        return await show(bot, user, "\n".join([
            title(pe("clock"), f"Заявка #{su.id} на рассмотрении"),
            quote(f"• Роль: <b>{ROLES[su.role]}</b>", f"• Оборот в день: {esc(su.turnover)}",
                  f"• Подана: {at(su.created_at, 'dt')}"),
            "Администрация проверит заявку и ответит сюда — обычно в течение нескольких часов. Как только её "
            "одобрят, придёт сообщение с кнопкой «Открыть главное меню».",
            f"Пока ждёте — {doc('start', 'как устроен Strait Pay')}.",
        ]) + note, kb(btn("Обновить статус", "su:st", "refresh"), _support()), src)
    if user.access == "rejected" and su and su.status == "rejected":
        again = deals.aware(su.decided_at) + REAPPLY_AFTER if su.decided_at else now()
        can = now() >= again
        return await show(bot, user, "\n".join([
            title(pe("cross"), f"Заявка #{su.id} отклонена"),
            quote(f"• Причина: {esc(su.reason or REJECT_DEFAULT)}"),
            "Можно подать новую заявку." if can else f"Новую заявку можно подать после {at(again, 'dt')}.",
        ]) + note, kb(btn("Подать заново", "su:new", "pencil", style="success") if can else None, _support()), src)
    await state.set_state(None)
    await show(bot, user, "\n".join([
        title(pe("shop"), f"{BRAND} · вход по заявке"),
        f"{TAGLINE}.",
        quote("• Покупайте USDT за рубли — продавец под защитой сделки",
              "• Продавайте USDT на свою карту или берите заявки на реквизиты под сумму",
              "• Пополнение и вывод — USDT в сети BEP-20 (BSC)"),
        "Чтобы начать, ответьте на 2–3 вопроса. Заявку рассмотрит администрация, ответ придёт сюда.",
        f"Подробнее — {doc('start', 'как начать работу')}.",
        "",
        "<b>Шаг 1. Кто вы?</b>",
    ]) + note, kb(btn("Я P2P-продавец", "su:r:seller", "shop", style="primary"),
                  btn("Я покупатель", "su:r:buyer", "dollar", style="success")), src)


def _support():
    sup = settings.get("support")
    return btn("Поддержка", url=f"https://t.me/{sup}") if sup else None


@router.message(CommandStart())
async def cmd_start(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext,
                    command: CommandObject | None = None):
    """/start t<team> (or o<deal>_t<team>) from a team link: he joins the team right away, the entry still waits."""
    from bot.handlers.commands import START
    found = START.match((command.args or "").strip() if command else "")
    if found and found[2] and (team := await s.get(Team, int(found[2]))):
        await teams.join(s, user, team)
    await gate_screen(bot, s, user, state)


@router.callback_query(F.data.regexp(r"^su:r:(seller|buyer)$"))
async def cb_role(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    role = c.data.split(":")[2]
    await state.set_state(SignupForm.turnover)
    await state.update_data(su_role=role)
    await _ask_turnover(bot, user, role, c)


async def _ask_turnover(bot, user, role: str, src=None, err: str = ""):
    question = ("Какой оборот в день вы способны держать — сколько рублей в день принимаете на свои реквизиты?"
                if role == "seller" else "На какую сумму в день планируете покупать USDT?")
    await show(bot, user, _step(2, 3 if role == "seller" else 2,
                                f"Роль: <b>{ROLES[role]}</b>\n\n{question}\nВыберите или напишите сумму.", err),
               kb(*[btn(t, f"su:t:{i}", "ruble") for i, t in enumerate(TURNOVER)], back("su:new", "Назад")), src)


@router.callback_query(SignupForm.turnover, F.data.regexp(r"^su:t:(\d)$"))
async def cb_turnover(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    i = int(c.data.split(":")[2])
    if i >= len(TURNOVER):
        return await c.answer()
    await _after_turnover(bot, user, state, TURNOVER[i], c)


@router.message(SignupForm.turnover, F.text)
async def msg_turnover(m: Message, bot: Bot, user: User, state: FSMContext):
    v = " ".join(m.text.split())
    role = (await state.get_data()).get("su_role", "buyer")
    if not 2 <= len(v) <= 60 or v.startswith("/"):
        return await _ask_turnover(bot, user, role, err="Напишите сумму коротко, например «300 000 ₽»")
    await _after_turnover(bot, user, state, v)


async def _after_turnover(bot, user, state: FSMContext, turnover: str, src=None):
    await state.update_data(su_turnover=turnover)
    if (await state.get_data()).get("su_role") == "seller":
        await state.set_state(SignupForm.proof)
        return await _ask_proof(bot, user, turnover, src)
    await state.set_state(None)
    await _confirm(bot, user, state, src)


async def _ask_proof(bot, user, turnover: str, src=None, err: str = ""):
    await show(bot, user, _step(3, 3, "\n".join([
        f"Роль: <b>P2P-продавец</b> · оборот: <b>{esc(turnover)}</b>",
        "",
        "Пришлите <b>скриншот баланса или оборота</b> — биржа, банк или профиль P2P (Bybit, HTX и т. п.). "
        "Одно фото или файл. Скриншот видит только администрация.",
    ]), err), kb(back("su:new", "Назад")), src)


@router.message(SignupForm.proof)
async def msg_proof(m: Message, bot: Bot, user: User, state: FSMContext):
    if m.photo:
        proof = f"photo:{m.photo[-1].file_id}"
    elif m.document:
        proof = m.document.file_id
    else:
        data = await state.get_data()
        return await _ask_proof(bot, user, data.get("su_turnover", ""), err="Нужен скриншот — фото или файл")
    await state.set_state(None)
    await state.update_data(su_proof=proof)
    await _confirm(bot, user, state)


async def _confirm(bot, user, state: FSMContext, src=None):
    data = await state.get_data()
    role = data.get("su_role")
    if role not in ROLES or not data.get("su_turnover"):
        return await show(bot, user, warn("Заявка устарела — начните заново"), kb(btn("Начать", "su:new", "pencil")), src)
    seller = role == "seller"
    await show(bot, user, "\n".join([
        title(pe("doc"), "Проверьте заявку"),
        quote(f"• Роль: <b>{ROLES[role]}</b>", f"• Оборот в день: <b>{esc(data['su_turnover'])}</b>",
              "• Скриншот: прикреплён" if seller else ""),
        "После отправки заявку рассмотрит администрация — ответ придёт в этот чат.",
    ]), kb(btn("Отправить заявку", "su:go", "ok", style="success"),
           btn("Заменить скриншот", "su:pf", "clip") if seller else None, back("su:new", "Заново")), src)


@router.callback_query(F.data == "su:pf")
async def cb_reproof(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    await state.set_state(SignupForm.proof)
    await _ask_proof(bot, user, (await state.get_data()).get("su_turnover", ""), c)


@router.callback_query(F.data == "su:go")
async def cb_submit(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    data = await state.get_data()
    role, turnover, proof = data.get("su_role"), data.get("su_turnover"), data.get("su_proof")
    if role not in ROLES or not turnover or (role == "seller" and not proof):
        return await gate_screen(bot, s, user, state, c, warn("Заявка устарела — заполните заново"))
    if user.access == "pending":
        return await gate_screen(bot, s, user, state, c)
    await state.set_data({})
    su = Signup(user_id=user.id, role=role, turnover=turnover, proof=proof if role == "seller" else None)
    s.add(su)
    user.access = "pending"
    await s.flush()
    events.add(s, f"user:{user.id}", "signup", f"Заявка на вход #{su.id}: {ROLES[role]}, оборот {turnover}", user.id)
    await s.commit()
    await publish(bot, s, su)
    await gate_screen(bot, s, user, state, c, ok("Заявка отправлена"))


@router.callback_query(F.data == "su:new")
async def cb_new(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    su = await last_signup(s, user.id)
    if user.access == "pending" or (su and su.status == "rejected" and su.decided_at
                                    and now() < deals.aware(su.decided_at) + REAPPLY_AFTER):
        return await gate_screen(bot, s, user, state, c)
    if user.access == "rejected":
        user.access = "new"
    await state.set_state(None)
    await gate_screen(bot, s, user, state, c)


@router.callback_query(F.data == "su:st")
async def cb_status(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await gate_screen(bot, s, user, state, c)


@router.callback_query()
async def cb_any(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    """Any other button (an old message, the main menu) while not let in: back to the entry flow."""
    await state.set_state(None)
    await gate_screen(bot, s, user, state, c)


@router.message()
async def msg_any(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await gate_screen(bot, s, user, state)


# ---------- the application card in the log chat ----------

async def card_text(s: AsyncSession, su: Signup) -> str:
    """The application card (admin chat and panel); the decision line is drawn from the data, so every copy of the
    card shows who decided."""
    u = await s.get(User, su.user_id)
    team = await s.get(Team, u.team_id) if u and u.team_id else None
    status = {"pending": f"{mark(logchat.WAIT)} на рассмотрении", "approved": f"{mark(logchat.GOOD)} одобрена",
              "rejected": f"{mark(logchat.BAD)} отклонена"}[su.status]
    lines = [f"{pe('pencil')} <b>Заявка на вход {alink('signup', su.id, f'#{su.id}')}</b> · {status}", "",
             card(cf("Кто", await logchat.who(s, su.user_id), icon="profile"),
                  cf("Роль", f"<b>{ROLES[su.role]}</b>", icon="shop"),
                  cf("Оборот в день", f"<b>{esc(su.turnover)}</b>", icon="ruble"),
                  cf("Скриншот", "прикреплён" if su.proof else "не нужен (покупатель)", icon="clip"),
                  cf("Пришёл", f"по ссылке команды {alink('team', team.id, f'«{esc(team.name)}»')}" if team else "",
                     icon="people"),
                  cf("Причина отказа", esc(su.reason or REJECT_DEFAULT), icon="info") if su.status == "rejected" else ""),
             "", stamp(su.created_at)]
    if su.status != "pending":
        admin = await s.get(User, su.admin_id) if su.admin_id else None
        lines += ["", verdict("ok" if su.status == "approved" else "cross",
                              "Одобрено" if su.status == "approved" else "Отклонено", admin)]
    return clean("\n".join(lines))


def card_kb(su: Signup):
    if su.status != "pending":
        return None
    return kb([btn("Одобрить", f"sua:ok:{su.id}", "ok", style="success"),
               btn("Отклонить", f"sua:no:{su.id}", "cross", style="danger")])


async def publish(bot: Bot, s: AsyncSession, su: Signup) -> None:
    """Post the application (with the screenshot) in every log chat's «Заявки на вход» topic. Commits."""
    text, markup, posted = await card_text(s, su), card_kb(su), 0
    for chat in logchat.targets():
        try:
            m = await logchat.post(bot, s, chat, "signups", text, markup, silent=False, media=su.proof)
        except TelegramAPIError as e:
            log.warning("signup %s not posted to %s: %s", su.id, chat, e)
            continue
        s.add(LogMessage(chat_id=chat, ref=f"signup:{su.id}", thread_id=m.message_thread_id, msg_id=m.message_id))
        posted += 1
    if not posted:
        events.add(s, f"user:{su.user_id}", "signup_unposted", f"Заявка на вход #{su.id} не опубликована в лог-чате — "
                   "откройте «Админ-панель → Заявки на вход»", su.user_id, alert=True)
    await s.commit()


async def refresh_cards(bot: Bot, s: AsyncSession, su: Signup) -> None:
    """The decision on every posted copy: status and who decided, the buttons go."""
    text = await card_text(s, su)
    for row in (await s.scalars(select(LogMessage).where(LogMessage.ref == f"signup:{su.id}"))).all():
        edit = ((lambda t, r=row: bot.edit_message_caption(chat_id=r.chat_id, message_id=r.msg_id, caption=t))
                if su.proof else
                (lambda t, r=row: bot.edit_message_text(chat_id=r.chat_id, message_id=r.msg_id, text=t,
                                                        disable_web_page_preview=True)))
        with suppress(TelegramAPIError):
            await safe_text(edit, text)


# ---------- admin: decide (in the topic or in the panel) ----------

async def decide(bot: Bot, s: AsyncSession, admin: User, sid: int, approve: bool, reason: str | None = None
                 ) -> tuple[Signup | None, str]:
    su = await s.get(Signup, sid, with_for_update=True, populate_existing=True)
    if su is None:
        return None, "Заявка не найдена"
    if su.status != "pending":
        return su, f"Заявка уже {'одобрена' if su.status == 'approved' else 'отклонена'}"
    u = await s.get(User, su.user_id)
    su.status, su.admin_id, su.decided_at = ("approved" if approve else "rejected"), admin.id, now()
    su.reason = None if approve else (reason or REJECT_DEFAULT)
    u.access = su.status
    audit.log(s, admin.id, "signup_ok" if approve else "signup_no", f"user:{u.id}", su.reason or "")
    events.add(s, f"user:{u.id}", "signup_decided", f"Заявка на вход #{su.id} " + (
        "одобрена" if approve else f"отклонена: {su.reason}") + f" ({admin.name})", u.id, notice=True)
    await s.commit()
    if approve:
        hint = (["• «USDT ⇄ RUB» — продажа USDT на свою карту",
                 "• «Ордерные реквизиты» — заявки покупателей под точную сумму"] if su.role == "seller" else
                ["• «RUB ⇄ USDT» — введите сумму в рублях, бот сам подберёт реквизиты",
                 "• «Кошелёк» — пополнение и вывод USDT в сети BEP-20 (BSC)"])
        from bot.handlers.community import missing
        join = ("Последний шаг — вступить в чат и подписаться на инфо-канал: бот даст ссылки. "
                if missing(u) else "")
        await notify(bot, u.id, "\n".join([
            f"{pe('ok')} <b>Заявка одобрена — добро пожаловать в {BRAND}</b>",
            "",
            quote(f"• Роль: <b>{ROLES[su.role]}</b>", *hint),
            f"{join}Как всё устроено — {doc('start', 'короткая инструкция')}. Нажмите кнопку, чтобы начать.",
        ]), kb(btn("Открыть главное меню", "menu", "shop", style="success")))
    else:
        await notify(bot, u.id, "\n".join([
            f"{pe('cross')} <b>Заявка на вход #{su.id} отклонена</b>",
            quote(f"• Причина: {esc(su.reason)}"),
            "Подать новую заявку можно через 24 часа — напишите /start.",
        ]))
    await refresh_cards(bot, s, su)
    return su, "Одобрена — пользователь получил приглашение в меню" if approve else "Отклонена, пользователь уведомлён"


@admin_router.callback_query(F.data.regexp(r"^sua:(ok|no):(\d+)$"))
async def cb_decide(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    _, act, sid = c.data.split(":")
    su, result = await decide(bot, s, user, int(sid), act == "ok")
    if c.message and c.message.chat.id == user.id:  # the admin panel: the card again
        return await signup_card(bot, s, user, su, c, ok(result)) if su else await c.answer(result, show_alert=True)
    await c.answer(result, show_alert=su is None or su.status != ("approved" if act == "ok" else "rejected"))


async def signup_card(bot: Bot, s: AsyncSession, admin: User, su: Signup, src=None, note: str = ""):
    await show(bot, admin, await card_text(s, su) + note, kb(
        btn("Показать скриншот", f"asu:pf:{su.id}", "clip") if su.proof else None,
        [btn("Одобрить", f"sua:ok:{su.id}", "ok", style="success"),
         btn("Отклонить", f"sua:no:{su.id}", "cross", style="danger")] if su.status == "pending" else None,
        btn("Отклонить с причиной", f"asu:rs:{su.id}", "pencil") if su.status == "pending" else None,
        back("asu", "Заявки на вход")), src)


@admin_router.callback_query(F.data.regexp(r"^asu(?::(all))?$"))
async def cb_list(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    show_all = c.data.endswith(":all")
    q = (select(Signup).order_by(Signup.id.desc()).limit(25) if show_all
         else select(Signup).where(Signup.status == "pending").order_by(Signup.id).limit(25))
    rows = (await s.scalars(q)).all()
    users = {u.id: u for u in (await s.scalars(select(User).where(User.id.in_([r.user_id for r in rows])))).all()}
    pending = await s.scalar(select(func.count(Signup.id)).where(Signup.status == "pending"))
    await show(bot, user, "\n".join([
        title(pe("pencil"), "Заявки на вход" + (": все" if show_all else "")),
        quote(f"• Ждут решения: <b>{pending}</b>",
              f"• Вход по заявке: <b>{settings.human('signup_review')}</b> — меняется в «Настройки → Правила»"),
        "Заявки приходят и в админ-чат, в тему «Заявки на вход» — решать можно прямо там." if rows else
        f"{pe('ok')} Новых заявок нет.",
    ]), kb(*[btn(f"#{r.id} · {ROLES[r.role]} · {(users[r.user_id].name if r.user_id in users else '')[:16]} · "
                 f"{r.turnover[:14]}", f"asu:{r.id}", "profile",
                 style="primary" if r.status == "pending" else None) for r in rows],
           btn("Ждут решения" if show_all else "Все заявки", "asu" if show_all else "asu:all", "list"),
           back("a", "Админ-панель")), c)


@admin_router.callback_query(F.data.regexp(r"^asu:(\d+)$"))
async def cb_card(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    su = await s.get(Signup, int(c.data.split(":")[1]))
    if not su:
        return await c.answer("Заявка не найдена", show_alert=True)
    await signup_card(bot, s, user, su, c)


@admin_router.callback_query(F.data.regexp(r"^asu:pf:(\d+)$"))
async def cb_proof(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    su = await s.get(Signup, int(c.data.split(":")[2]))
    await c.answer()
    if su and su.proof:
        send = bot.send_photo if su.proof.startswith("photo:") else bot.send_document
        chat, topic = files_to(c)
        with suppress(TelegramAPIError):
            await send(chat, su.proof.removeprefix("photo:"), caption=f"Заявка на вход #{su.id}: скриншот",
                       reply_markup=kb(back("x", "Скрыть", "cross")), message_thread_id=topic)


@admin_router.callback_query(F.data.regexp(r"^asu:rs:(\d+)$"))
async def cb_reason(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    sid = int(c.data.split(":")[2])
    await state.set_state(SignupReject.reason)
    await state.set_data({"signup": sid})
    await show(bot, user, f"{title(pe('cross'), f'Отказ по заявке #{sid}')}\n\nНапишите причину (5–300 символов) — "
                          "её увидит пользователь.", kb(back(f"asu:{sid}", "Отмена")), c)


@admin_router.message(SignupReject.reason, F.text)
async def msg_reason(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    sid = (await state.get_data())["signup"]
    reason = " ".join(m.text.split())
    if not 5 <= len(reason) <= 300:
        return await show(bot, user, title(pe("cross"), "Причина отказа") + "\n\nНапишите причину ещё раз."
                          + warn("От 5 до 300 символов"), kb(back(f"asu:{sid}", "Отмена")))
    await state.clear()
    su, result = await decide(bot, s, user, sid, False, reason)
    if su is None:
        return await show(bot, user, warn(result), kb(back("asu", "Заявки на вход")))
    await signup_card(bot, s, user, su, note=ok(result))


@admin_router.message(Command("signups"))
async def cmd_signups(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    """Not in the command menu: a shortcut for admins."""
    await state.clear()
    rows = (await s.scalars(select(Signup).where(Signup.status == "pending").order_by(Signup.id).limit(1))).all()
    if rows:
        return await signup_card(bot, s, user, rows[0])
    await show(bot, user, f"{pe('ok')} Новых заявок нет.", kb(back("a", "Админ-панель")))
