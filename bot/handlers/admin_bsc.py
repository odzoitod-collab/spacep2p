"""Admin commands of the USDT BEP-20 cash desk (services/bsc.py): /bsc_status, /bsc_key (owners), /gaz, /bscq."""
import asyncio
import logging

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.emoji import pe
from bot.models import BscPayout, BscTx, Withdrawal
from bot.services import admins, audit, bsc, money
from bot.services.admins import IsAdmin
from bot.ui import esc

log = logging.getLogger(__name__)
router = Router()
router.message.filter(IsAdmin())

KEY_TTL = 120  # seconds the seed stays in the owner's chat
_drops: set[asyncio.Task] = set()


def _off() -> str:
    return f"{pe('warn')} Касса BEP-20 выключена" + (f": {esc(bsc.error)}" if bsc.error else " (ещё запускается)")


async def _say(m: Message, text: str) -> None:
    await m.answer(text, disable_web_page_preview=True)


@router.message(Command("bsc_status"))
async def cmd_status(m: Message, s: AsyncSession):
    if not admins.is_owner(m.from_user.id):
        return await _say(m, f"{pe('lock')} Только владельцы")
    if not bsc.ready():
        return await _say(m, _off())
    try:
        d = await bsc.desk(s)
        money_lines = [f"USDT на горячем (свободно): <b>{money.usdt(d.usdt)}</b>",
                       f"BNB на газ: <b>{d.bnb:.5f}</b>",
                       f"Холодный: <b>{money.usdt(d.cold)} USDT</b>" if d.cold is not None else "Холодный: не задан",
                       f"Не собрано с адресов пополнения: {money.usdt(d.unswept)} USDT",
                       f"Касса всего: <b>{money.usdt(d.total)} USDT</b>",
                       f"Очередь выплат: {money.usdt(d.queued)} USDT · транзакций в пути: {d.pending}"]
    except (bsc.RpcError, ValueError) as e:
        money_lines = [f"{pe('warn')} Сеть не ответила: {esc(str(e)[:200])}"]
    await _say(m, "\n".join([
        f"{pe('wallet')} <b>Касса USDT BEP-20</b>",
        f"Горячий кошелёк: <code>{bsc.hot}</code>",
        *money_lines,
        f"Рабочий RPC: {esc(bsc.good_node or '—')}",
        f"Ошибка конфигурации: {esc(bsc.error)}" if bsc.error else "Конфигурация: в порядке",
        f'<a href="{bsc.address_url(bsc.hot)}">Открыть в BscScan</a>',
    ]))


async def _drop(bot: Bot, chat: int, mid: int) -> None:
    await asyncio.sleep(KEY_TTL)
    try:
        await bot.delete_message(chat, mid)
    except TelegramAPIError:
        log.warning("bsc key message %s in %s not deleted", mid, chat)


async def send_key(bot: Bot, s: AsyncSession, uid: int) -> str:
    """The seed and the hot wallet to an owner's private chat, deleted after KEY_TTL. "" or why not. Commits."""
    if not admins.is_owner(uid):
        return "Только владельцы"
    if not bsc.ready():
        return _off()
    try:
        sent = await bot.send_message(uid, "\n".join([
            f"{pe('key')} <b>Ключ кассы BEP-20 (seed)</b> — сообщение удалится через {KEY_TTL // 60} мин",
            "",
            f"<code>{esc(bsc.mnemonic())}</code>",
            "",
            f"Горячий кошелёк (MetaMask «Account 1»): <code>{bsc.hot}</code>",
            "Запишите слова офлайн. Кто знает их — распоряжается всеми деньгами кассы. С горячего кошелька в "
            "MetaMask руками не отправляйте: это займёт nonce бота. Смотреть баланс и историю — можно.",
        ]), disable_web_page_preview=True)
    except TelegramAPIError:
        return "Не смог написать вам в личку — откройте чат с ботом и повторите"
    task = asyncio.create_task(_drop(bot, uid, sent.message_id))
    _drops.add(task)
    task.add_done_callback(_drops.discard)
    audit.log(s, uid, "bsc_key", "", "seed кассы BEP-20 показан владельцу в личке")
    await s.commit()
    return ""


@router.message(Command("bsc_key"))
async def cmd_key(m: Message, bot: Bot, s: AsyncSession):
    """The seed and the hot wallet, to an owner's private chat only; deleted after 2 minutes."""
    if err := await send_key(bot, s, m.from_user.id):
        return await _say(m, f"{pe('lock')} {esc(err)}")
    if m.chat.id != m.from_user.id:
        await _say(m, f"{pe('ok')} Отправил в личку, удалится через {KEY_TTL // 60} мин")


@router.message(Command("gaz"))
async def cmd_gas(m: Message):
    if not bsc.ready():
        return await _say(m, _off())
    try:
        wei, usdt = await bsc.bnb_balance(bsc.hot), await bsc.usdt_balance(bsc.hot)
        nums = [f"BNB: <b>{bsc.bnb(wei):.5f}</b>" + (" — мало, пополните" if wei < bsc.LOW_GAS_WEI else ""),
                f"USDT: <b>{money.usdt(bsc.from_micro(bsc.wei_to_micro(usdt)))}</b>"]
    except bsc.RpcError as e:
        nums = [f"{pe('warn')} Сеть не ответила: {esc(str(e)[:200])}"]
    await _say(m, "\n".join([f"{pe('wallet')} <b>Горячий кошелёк BEP-20</b>", f"<code>{bsc.hot}</code>", *nums,
                             f'<a href="{bsc.address_url(bsc.hot)}">BscScan</a>']))


@router.message(Command("bscq"))
async def cmd_queue(m: Message, s: AsyncSession, command: CommandObject):
    args = (command.args or "").split()
    if args[:1] == ["cancel"]:
        if not admins.is_owner(m.from_user.id):
            return await _say(m, f"{pe('lock')} Снимать выплаты могут только владельцы")
        if len(args) != 2 or not args[1].lstrip("#").isdigit():
            return await _say(m, "Как: <code>/bscq cancel 12</code> — номер вывода из очереди")
        wd = await bsc.cancel_unsigned(s, int(args[1].lstrip("#")), f"владелец {m.from_user.id}")
        if wd is None:
            return await _say(m, f"{pe('warn')} Вывод не найден, не BEP-20 или уже подписан — снять нельзя")
        audit.log(s, m.from_user.id, "bsc_cancel", f"wd:{wd.id}", str(wd.amount))
        await s.commit()
        from bot.handlers.wallet import notify_withdrawal
        await notify_withdrawal(m.bot, wd, "refunded")
        return await _say(m, f"{pe('ok')} Вывод #{wd.id} снят, {money.usdt(wd.amount)} USDT возвращены пользователю")
    wds = (await s.scalars(select(Withdrawal).where(Withdrawal.method == "bsc", Withdrawal.status.in_(
        ("queued", "sending", "sent"))).order_by(Withdrawal.id).limit(30))).all()
    pays = (await s.scalars(select(BscPayout).where(BscPayout.status.in_(("queued", "sending", "pending_chain")))
                            .order_by(BscPayout.id).limit(10))).all()
    txs = (await s.scalars(select(BscTx).where(BscTx.status == "pending").order_by(BscTx.id).limit(20))).all()
    lines = [f"{pe('list')} <b>Очередь исходящих BEP-20</b>", ""]
    lines += [f"в{w.id} · {money.usdt(w.amount - w.fee)} USDT → <code>{bsc.short(w.address)}</code> · {w.status}"
              + (" · подписан" if w.transfer_id else "") for w in wds] or ["Выводов в очереди нет"]
    lines += [f"{p.kind} #{p.id} · {money.usdt(p.amount)} USDT → <code>{bsc.short(p.address)}</code> · {p.status}"
              for p in pays]
    if txs:
        lines += ["", "<b>Транзакции в пути</b>"] + [
            f"{t.kind} #{t.ref_id} · nonce {t.nonce} · <code>{t.tx_hash[:14]}…</code>" for t in txs]
    lines += ["", "Снять неподписанный вывод (владельцы): <code>/bscq cancel 12</code>"]
    await _say(m, "\n".join(lines))
