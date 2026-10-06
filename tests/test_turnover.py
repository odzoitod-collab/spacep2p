"""A deposit cannot simply go back out: only what was turned over in deals (or bought, earned) is withdrawn.
withdrawable = balance − deposit_lock; a deposit adds to the lock, USDT sold to buyers take it off."""
from decimal import Decimal as D

from bot import models
from sqlalchemy import select

from bot.models import User, Withdrawal
from bot.services import money, settings
from tests.harness import cb, msg, plain

DEST = "UQBvW8Z5huBkMJYdnfAEM5JqTNkuWX3diqYENkWsIL0XglxD"
from tests.test_scenarios import ADMIN, BUYER, PDF, SELLER, create_deal, ready


async def on():
    async with models.Session() as s:
        await settings.put(s, "withdraw_turnover", "1")
        await s.commit()


async def deposit(uid, amount):
    async with models.Session() as s:
        await money.add(s, uid, D(amount), "deposit", "dep:9")
        await s.commit()


async def user(uid):
    async with models.Session() as s:
        return await s.get(User, uid)


def test_a_deposit_is_not_withdrawn_until_it_is_turned_over(go):
    async def fn(b):
        await on()
        await ready(b)  # the seller deposited 200
        u = await user(SELLER)
        assert (u.balance, u.deposit_lock, money.withdrawable(u)) == (D(200), D(200), 0)
        await b.run(cb(SELLER, "w"))
        assert "Можно вывести: 0 USDT · ещё прокрутить 200 USDT в сделках" in plain(b.session.last(SELLER))
        await b.run(cb(SELLER, "w:out"), msg(SELLER, DEST), cb(SELLER, "w:nomemo"), msg(SELLER, "50"))
        assert "Вывести можно только 0 USDT" in plain(b.session.last(SELLER))
        assert "w:all" not in b.session.buttons(SELLER)
        async with models.Session() as s:
            assert not await s.scalar(select(Withdrawal.id))

        d = await create_deal(b)  # he sells 95 USDT to a buyer: that much of his deposit worked
        await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, document=PDF), cb(SELLER, f"dl:ok2:{d.id}"))
        u = await user(SELLER)
        assert (u.balance, u.deposit_lock, money.withdrawable(u)) == (D(105), D(105), 0)
        b_ = await user(BUYER)
        assert money.withdrawable(b_) == b_.balance == D(94)  # bought USDT go out at once
        await b.run(cb(BUYER, "w:out"), msg(BUYER, DEST), cb(BUYER, "w:nomemo"), msg(BUYER, "94"), cb(BUYER, "w:go"))
        assert (await user(BUYER)).balance == 0
    go(fn)


def test_earned_money_is_free_the_deposit_is_not_and_the_last_check_holds(go):
    async def fn(b):
        await on()
        await ready(b)
        await b.run(msg(30, "/start"))
        await deposit(30, 100)
        async with models.Session() as s:  # an admin credit: free
            await money.add(s, 30, D(30), "admin", "adj:1")
            await s.commit()
        assert money.withdrawable(await user(30)) == D(30)
        await b.run(cb(30, "w:out"), msg(30, DEST), cb(30, "w:nomemo"), msg(30, "30"))
        assert "w:go" in b.session.buttons(30)
        await deposit(30, 0.000001)  # meanwhile: the balance moves, the free part does not grow
        async with models.Session() as s:  # and something takes the free part away before he confirms
            await money.add(s, 30, D(-20), "admin", "adj:2")
            await s.commit()
        await b.run(cb(30, "w:go"))
        assert "Вывести можно только 10 USDT" in b.session.alerts()[-1]  # checked again
        async with models.Session() as s:
            assert not await s.scalar(select(Withdrawal.id))
        await b.run(cb(ADMIN, "auv:30"))
        assert "не прокручено пополнений 100" in plain(b.session.last(ADMIN))
        await b.run(cb(ADMIN, "aul:30"))
        u = await user(30)
        assert u.deposit_lock == 0 and money.withdrawable(u) == u.balance
    go(fn)


def test_debt_repaid_from_the_balance_counts_as_turned_over(go):
    async def fn(b):
        from bot.services import operators
        await on()
        await ready(b)
        await deposit(BUYER, 50)
        async with models.Session() as s:
            await operators.accrue(s, BUYER, D(20), "deal:1")
            await money.add(s, BUYER, D(-20), "debt_repay", f"op:{BUYER}")
            await s.commit()
        u = await user(BUYER)
        assert (u.balance, u.deposit_lock) == (D(30), D(30))
    go(fn)


def test_bought_usdt_go_out_at_once_even_next_to_a_locked_deposit(go):
    async def fn(b):
        await on()
        await ready(b)
        await deposit(BUYER, 100)  # his own deposit, not turned over
        d = await create_deal(b)
        await b.run(cb(BUYER, f"dl:rc:{d.id}"), msg(BUYER, document=PDF), cb(SELLER, f"dl:ok2:{d.id}"))
        u = await user(BUYER)
        assert (u.balance, u.deposit_lock, money.withdrawable(u)) == (D(194), D(100), D(94))
        await b.run(cb(BUYER, "w:out"), msg(BUYER, DEST), cb(BUYER, "w:nomemo"), msg(BUYER, "94"), cb(BUYER, "w:go"))
        assert (await user(BUYER)).balance == D(100)  # all the bought USDT left at once
    go(fn)


def test_a_ton_deposit_is_locked_until_turned_over(go):
    from bot import tasks

    async def fn(b):
        await on()
        await ready(b)
        await b.run(cb(BUYER, "w:in"))
        b.chain.pay(BUYER, "100")
        await tasks.ton_cycle(b.bot)
        u = await user(BUYER)
        assert (u.balance, u.deposit_lock, money.withdrawable(u)) == (D("98.5"), D("98.5"), 0)
    go(fn)


def test_command_menu_is_short_and_has_the_manager(go):
    from bot.handlers.commands import COMMANDS

    async def fn(b):
        assert [c for c, _ in COMMANDS] == ["start", "buy", "sell", "wallet", "deals", "help", "manager"]
        await ready(b)
        await b.run(cb(ADMIN, "as:manager"), msg(ADMIN, "@strait_manager"))
        await b.run(msg(BUYER, "/manager"))
        assert "@strait_manager" in plain(b.session.last(BUYER))
        assert "https://t.me/strait_manager" in b.session.buttons(BUYER)
        await b.run(cb(BUYER, "menu"))
        assert "https://t.me/strait_manager" in b.session.buttons(BUYER)  # «Вопросы менеджеру» in the menu
        await b.run(cb(BUYER, "info"))
        assert "Вопросы — менеджеру @strait_manager" in plain(b.session.last(BUYER))
    go(fn)
