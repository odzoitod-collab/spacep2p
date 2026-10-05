"""«/balance» — every balance in the bot and full control over it (like the poker balances in SpaceTeam's /buh).

The list shows who holds money with the totals; a user's card changes his available balance at once: ±1/10/50, an
exact amount («+10», «-5», «=20») or zero. Mass actions — top up everyone holding money or everyone in the bot, take
a sum from everyone, zero everyone — go through a confirmation screen.

Every change is an ordinary manual adjustment (admin.apply_adjustment): a numbered row, the journal entry, the
history, the second admin's approval when the amount reaches «adjust_approval_usdt» (always for one's own balance).
Frozen money is never touched — it moves only through deals. A mass action writes its adjustments quietly and posts
one summary to «Действия админов» instead of two posts per user. The user gets a message with his new balance.
"""
import asyncio
import secrets
from decimal import Decimal, InvalidOperation

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.emoji import back, btn, kb, pe
from bot.handlers.wallet import parse_usdt
from bot.models import Adjustment, User
from bot.services import audit, money, settings
from bot.services.admins import IsAdmin
from bot.ui import card, cf, esc, notify, ok, quote, show, title, ulink, warn

router = Router()
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

PAGE = 15
STEPS = (1, 10, 50)
# kind: (button, what it does, asks an amount)
MASS = {"plus": ("Начислить всем с балансом", "Начисление всем, у кого есть баланс", True),
        "all": ("Начислить всем в боте", "Начисление всем пользователям бота", True),
        "minus": ("Списать всем", "Списание со всех, у кого есть баланс", True),
        "zero": ("Обнулить всех", "Обнуление доступного баланса у всех", False)}
RESULT = {"pending": ok("Ждёт подтверждения второго администратора"),
          "failed": warn("Не хватает доступного баланса — ничего не списано")}
_mass_lock = asyncio.Lock()  # one mass action at a time: a double tap must not credit everyone twice
_notifying: set[asyncio.Task] = set()  # the users' messages after a mass action go in the background


class Bal(StatesGroup):
    amount = State()  # one user's amount: +10 / -5 / =20
    mass = State()  # the amount of a mass action


def _name(u: User) -> str:
    return f"@{u.username}" if u.username else (u.name or str(u.id))


def _signed(v: Decimal) -> str:
    return ("+" if v > 0 else "") + money.usdt(v)


def _notice(a: Adjustment) -> str:
    from bot.handlers.admin import _adj_reason
    return (f"{pe('wallet')} <b>Баланс изменён администрацией: {_signed(a.delta)} USDT</b>\n"
            f"Теперь доступно: <b>{money.usdt(a.balance_after)} USDT</b>\n"
            f"Причина: {esc(_adj_reason(a))}\nОперация #{a.id}")


async def _change(s: AsyncSession, admin: User, uid: int, delta: Decimal, comment: str,
                  quiet: bool = False) -> tuple[str, Adjustment | None]:
    """One manual adjustment created and applied at once by the same admin (see admin.apply_adjustment)."""
    from bot.handlers.admin import apply_adjustment
    u = await s.get(User, uid)
    if u is None or not delta:
        return "noop", None
    a = Adjustment(user_id=uid, admin_id=admin.id, delta=delta, reason="manual", comment=comment,
                   balance_before=u.balance, balance_after=u.balance + delta)
    s.add(a)
    await s.flush()
    return await apply_adjustment(s, a.id, admin, quiet=quiet)


# ---------- the list ----------

@router.message(Command("balance"))
async def cmd_balance(m: Message, bot: Bot, s: AsyncSession, user: User, command: CommandObject, state: FSMContext):
    from bot.handlers.admin_people import find_user
    await state.clear()
    arg = (command.args or "").strip()
    if not arg:
        return await balances_screen(bot, s, user)
    u = await find_user(s, arg)
    if u is None:
        return await balances_screen(bot, s, user, note=warn(f"Пользователь {esc(arg[:40])} не найден"))
    await balance_card(bot, s, user, u)


@router.callback_query(F.data.regexp(r"^bal:p:(\d+)$"))
async def cb_list(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    await balances_screen(bot, s, user, int(c.data.split(":")[2]), c)


async def balances_screen(bot: Bot, s: AsyncSession, admin: User, page: int = 0, src=None, note: str = ""):
    holding = or_(User.balance > 0, User.frozen > 0)
    count = await s.scalar(select(func.count(User.id)).where(holding))
    users = await s.scalar(select(func.count(User.id)))
    available, frozen = (await s.execute(select(func.coalesce(func.sum(User.balance), 0),
                                                func.coalesce(func.sum(User.frozen), 0)))).one()
    pages = max(1, -(-count // PAGE))
    page = min(page, pages - 1)
    rows = (await s.scalars(select(User).where(holding).order_by(User.balance.desc(), User.id)
                            .offset(page * PAGE).limit(PAGE))).all()
    nav = [b for b in (btn("‹ Назад", f"bal:p:{page - 1}") if page else None,
                       btn("Дальше ›", f"bal:p:{page + 1}") if page + 1 < pages else None) if b]
    await show(bot, admin, "\n".join([
        title(pe("wallet"), "Балансы") + (f" · стр. {page + 1}/{pages}" if pages > 1 else ""),
        "",
        quote(f"{pe('dollar')} Доступно у всех: <b>{money.usdt(Decimal(available))} USDT</b>",
              f"{pe('lock')} Заморожено в сделках: {money.usdt(Decimal(frozen))} USDT",
              f"{pe('people')} С деньгами: <b>{count}</b> из {users}"),
        "Нажмите на человека — карточка с кнопками ±. Любой пользователь: <code>/balance ID</code> или "
        "<code>/balance @ник</code>." if rows else "<i>Ни у кого нет денег на балансе.</i>",
    ]) + note, kb(
        *[btn(f"{_name(u)} · {money.usdt(u.balance)}" + (f" · в сделках {money.usdt(u.frozen)}" if u.frozen else ""),
              f"bal:u:{u.id}", wide=True) for u in rows],
        nav or None,
        btn(MASS["plus"][0], "bal:m:plus"),
        btn(MASS["all"][0], "bal:m:all"),
        [btn(MASS["minus"][0], "bal:m:minus"), btn(MASS["zero"][0], "bal:m:zero")],
        back("a", "Админ-панель"),
    ), src)


# ---------- one user ----------

@router.callback_query(F.data.regexp(r"^bal:u:(\d+)$"))
async def cb_user(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    u = await s.get(User, int(c.data.split(":")[2]))
    if u is None:
        return await c.answer("Пользователь не найден", show_alert=True)
    await balance_card(bot, s, user, u, c)


async def balance_card(bot: Bot, s: AsyncSession, admin: User, u: User, src=None, note: str = "", asking=False):
    limit = settings.dec("adjust_approval_usdt")
    await show(bot, admin, "\n".join([
        title(pe("wallet"), f"Баланс · {u.id}"),
        "",
        card(cf("Пользователь", ulink(u), icon="profile"),
             cf("Доступно", f"<b>{money.usdt(u.balance)} USDT</b>", icon="dollar"),
             cf("Заморожено", f"{money.usdt(u.frozen)} USDT — меняется только через сделки", icon="lock")
             if u.frozen else "",
             cf("Не прокручено", f"{money.usdt(u.deposit_lock)} USDT", icon="refresh") if u.deposit_lock else ""),
        "",
        "Кнопки меняют доступный баланс сразу, пользователь получит сообщение. Свой баланс проводит только другой "
        "администратор" + (f", суммы от {money.usdt(limit)} USDT — тоже." if limit > 0 else "."),
    ]) + note, kb(back(f"bal:u:{u.id}", "Отмена", "cross")) if asking else kb(
        # the project's layout: two per row at most — so «−n / +n» pairs
        *[[btn(f"−{n}", f"bal:q:{u.id}:-{n}"), btn(f"+{n}", f"bal:q:{u.id}:{n}")] for n in STEPS],
        [btn("Своя сумма", f"bal:c:{u.id}", "pencil"), btn("Обнулить", f"bal:z:{u.id}", "trash")],
        [btn("Профиль", f"auv:{u.id}"), btn("Корректировки", f"aadj:{u.id}")],
        back("bal:p:0", "Все балансы"),
    ), src)


async def _apply_one(bot: Bot, s: AsyncSession, admin: User, uid: int, delta: Decimal, comment: str, src=None):
    result, a = await _change(s, admin, uid, delta, comment)
    await s.commit()
    if result == "done":
        await notify(bot, uid, _notice(a))
    note = (ok(f"{_signed(a.delta)} USDT · {money.usdt(a.balance_before)} → {money.usdt(a.balance_after)}")
            if result == "done" else RESULT.get(result, ok("Без изменений")))
    await balance_card(bot, s, admin, await s.get(User, uid, populate_existing=True), src, "\n" + note)


@router.callback_query(F.data.regexp(r"^bal:q:(\d+):(-?\d+)$"))
async def cb_quick(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    _, _, uid, step = c.data.split(":")
    await _apply_one(bot, s, user, int(uid), Decimal(step), "", c)


@router.callback_query(F.data.regexp(r"^bal:c:(\d+)$"))
async def cb_custom(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    u = await s.get(User, int(c.data.split(":")[2]))
    if u is None:
        return await c.answer()
    await state.set_state(Bal.amount)
    await state.set_data({"uid": u.id})
    await balance_card(bot, s, user, u, c, "\n" + quote(
        f"{pe('pencil')} Отправьте сумму: <code>+10</code> — начислить, <code>-5</code> — списать, "
        "<code>20</code> — выставить ровно."), asking=True)


def parse_change(raw: str | None) -> tuple[str, Decimal] | None:
    """«+10» / «-5» / «=20» or a bare «20» (= exactly). «0» / «=0» zero the balance."""
    t = (raw or "").strip().replace(" ", "").replace("−", "-")
    sign, num = (t[0], t[1:]) if t[:1] in ("+", "-", "=") else ("=", t)
    if sign == "=":
        try:
            if Decimal(num.replace(",", ".")) == 0:
                return "=", Decimal(0)
        except InvalidOperation:
            return None
    v = parse_usdt(num)
    return (sign, v) if v is not None else None


@router.message(Bal.amount, F.text)
async def msg_custom(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    uid = (await state.get_data()).get("uid")
    u = await s.get(User, uid) if uid else None
    if u is None:
        return await state.clear()
    change = parse_change(m.text)
    if change is None:
        return await balance_card(bot, s, user, u, None, "\n" + warn("Не понял сумму: +10, -5 или 20"), asking=True)
    await state.clear()
    sign, v = change
    delta = v - u.balance if sign == "=" else (v if sign == "+" else -v)
    await _apply_one(bot, s, user, u.id, delta, "")


@router.callback_query(F.data.regexp(r"^bal:z:(\d+)$"))
async def cb_zero_ask(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    u = await s.get(User, int(c.data.split(":")[2]))
    if u is None:
        return await c.answer()
    if not u.balance:
        return await c.answer("Доступный баланс уже 0", show_alert=True)
    await show(bot, user, "\n".join([
        f"{pe('warn')} <b>Обнулить баланс?</b>",
        "",
        card(cf("Пользователь", ulink(u), icon="profile"),
             cf("Сейчас", f"<b>{money.usdt(u.balance)} USDT</b> → 0", icon="dollar"),
             cf("Заморожено", f"{money.usdt(u.frozen)} USDT останется в сделках", icon="lock") if u.frozen else ""),
    ]), kb([btn("Обнулить", f"bal:zz:{u.id}", "trash", style="danger"), btn("Отмена", f"bal:u:{u.id}")]), c)


@router.callback_query(F.data.regexp(r"^bal:zz:(\d+)$"))
async def cb_zero(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    u = await s.get(User, int(c.data.split(":")[2]))
    if u is None:
        return await c.answer()
    await _apply_one(bot, s, user, u.id, -u.balance, "обнуление баланса", c)


# ---------- everyone ----------

def _targets(kind: str):
    q = select(User.id).order_by(User.id)
    return q.where(~User.is_banned) if kind == "all" else q.where(User.balance > 0)


def _plan(kind: str, amount: Decimal, balance: Decimal) -> Decimal:
    """What a mass action does to one user's available balance (never below zero)."""
    if kind in ("plus", "all"):
        return amount
    return -(min(amount, balance) if kind == "minus" else balance)


@router.callback_query(F.data.regexp(r"^bal:m:(plus|all|minus|zero)$"))
async def cb_mass(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    kind = c.data.split(":")[2]
    await state.clear()
    if MASS[kind][2]:
        await state.set_state(Bal.mass)
        await state.set_data({"kind": kind})
        n = await s.scalar(select(func.count()).select_from(_targets(kind).subquery()))
        return await show(bot, user, "\n".join([
            title(pe("wallet"), MASS[kind][1]),
            "",
            f"Затронет: <b>{n}</b> чел.",
            "Отправьте сумму в USDT — " + ("столько получит каждый." if kind != "minus" else
                                          "столько спишется с каждого (у кого меньше — до нуля)."),
        ]), kb(back("bal:p:0", "Отмена", "cross")), c)
    await _mass_confirm(bot, s, user, state, kind, Decimal(0), c)


@router.message(Bal.mass, F.text)
async def msg_mass(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    kind = (await state.get_data()).get("kind")
    v = parse_usdt(m.text)
    if kind not in MASS:
        return await state.clear()
    if v is None:
        return await show(bot, user, title(pe("wallet"), MASS[kind][1]) + "\n\nОтправьте сумму в USDT."
                          + warn("Нужно положительное число"), kb(back("bal:p:0", "Отмена", "cross")))
    await _mass_confirm(bot, s, user, state, kind, v)


async def _mass_confirm(bot, s, admin, state, kind: str, amount: Decimal, src=None):
    ids = (await s.scalars(_targets(kind))).all()
    balances = dict((await s.execute(select(User.id, User.balance).where(User.id.in_(ids)))).all()) if ids else {}
    total = sum((_plan(kind, amount, balances[i]) for i in ids), Decimal(0))
    nonce = secrets.token_hex(4)
    await state.set_state(None)
    await state.set_data({"kind": kind, "amount": str(amount), "nonce": nonce})
    await show(bot, admin, "\n".join([
        f"{pe('warn')} <b>{MASS[kind][1]}?</b>",
        "",
        card(cf("Затронет", f"<b>{len(ids)}</b> чел.", icon="people"),
             cf("Каждому", f"{_signed(amount)} USDT", icon="dollar") if kind in ("plus", "all") else
             cf("С каждого", f"{money.usdt(amount)} USDT, но не ниже нуля", icon="dollar") if kind == "minus" else "",
             cf("Итого", f"<b>{_signed(total)} USDT</b>", icon="wallet")),
        "",
        "Каждому придёт сообщение. Свой баланс и суммы выше порога ждут второго администратора.",
    ]), kb([btn("Подтвердить", f"bal:x:{nonce}", "ok", style="danger" if total < 0 else "success"),
            btn("Отмена", "bal:p:0")]), src)


@router.callback_query(F.data.regexp(r"^bal:x:([0-9a-f]+)$"))
async def cb_mass_run(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    async with _mass_lock:
        data = await state.get_data()
        if data.get("nonce") != c.data.split(":")[2] or data.get("kind") not in MASS:
            return await c.answer("Уже выполнено или устарело — откройте /balance заново", show_alert=True)
        await state.clear()
        kind, amount = data["kind"], Decimal(data["amount"])
        done: list[Adjustment] = []
        pending = failed = 0
        for uid in (await s.scalars(_targets(kind))).all():
            u = await s.get(User, uid, populate_existing=True)
            result, a = await _change(s, user, uid, _plan(kind, amount, u.balance), MASS[kind][1], quiet=True)
            if result == "done":
                done.append(a)
            pending += result == "pending"
            failed += result == "failed"
        total = sum((a.delta for a in done), Decimal(0))
        summary = (f"{MASS[kind][1]}: проведено {len(done)} на {_signed(total)} USDT"
                   + (f", ждут второго администратора {pending}" if pending else "")
                   + (f", отклонено {failed}" if failed else ""))
        audit.log(s, user.id, "balance_mass", f"mass:{kind}", summary)
        await s.commit()
    messages = [(a.user_id, _notice(a)) for a in done]
    if messages:
        task = asyncio.create_task(_notify_all(bot, messages))
        _notifying.add(task)
        task.add_done_callback(_notifying.discard)
    await balances_screen(bot, s, user, 0, c, "\n" + ok(summary))


async def _notify_all(bot: Bot, messages: list[tuple[int, str]]) -> None:
    for uid, text in messages:
        await notify(bot, uid, text)
