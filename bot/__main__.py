import asyncio
import logging

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from sqlalchemy import text

from bot import models, tasks
from bot.app import build_dispatcher
from bot.config import config
from bot.handlers import commands, logchat
from bot.services import api, money, settings, ton


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await models.init_db(config.database_url)
    lock_conn = None
    if models.engine.dialect.name == "postgresql":
        # Two pollers conflict in Telegram and would double background jobs: allow one process per DB.
        # autocommit: the lock is session-level, the connection must not sit idle inside a transaction
        lock_conn = await (await models.engine.connect()).execution_options(isolation_level="AUTOCOMMIT")
        if not await lock_conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": models.INSTANCE_LOCK}):
            logging.error("Another bot process is already running on this database; exiting")
            await lock_conn.close()
            await models.engine.dispose()
            raise SystemExit(1)
    async with models.Session() as s:
        await settings.load(s)
        if settings.get("order_link_minutes") == "2":  # the old default: 5 minutes for the link now
            await settings.put(s, "order_link_minutes", "5")
            await s.commit()
    await ton.start()
    if ton.enabled():
        logging.info("TON wallet on %s, hot wallet %s", "testnet" if config.ton_testnet else "mainnet",
                     ton.friendly(ton.hot_address()))
    else:
        logging.warning("TON_SEED is not set: deposits and withdrawals are off (python -m bot.services.ton seed)")

    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    logging.getLogger().addHandler(logchat.ErrorTopic(bot))  # the bot's errors -> the admin chat's «Ошибки бота»
    dp = build_dispatcher()
    from bot import ui
    try:
        me = await bot.me()
        ui.BOT, ui.MAIN_APP = me.username or "", bool(getattr(me, "has_main_web_app", False))
    except Exception:  # noqa: BLE001 - known after the first update anyway
        logging.exception("getMe failed")
    async with models.Session() as s:  # leftovers of xRocket: never left hanging with users' money
        for wd in await ton.retire_legacy(s):
            await ui.notify(bot, wd.user_id, f"Вывод #{wd.id} через xRocket отменён: сервис переведён на USDT в сети TON. "
                                             f"{money.usdt(wd.amount)} USDT снова на балансе — выведите их в «Кошельке».")
    await commands.setup_commands(bot)
    try:  # the admin chat's topics in the current layout, with their pins; never blocks the start
        await logchat.ensure_topics(bot)
    except Exception:  # noqa: BLE001
        logging.exception("admin chat topics not set up")
    background = tasks.start(bot)
    runner = None
    if config.api_enabled:
        from aiohttp import web
        from bot.api.server import build_app
        runner = web.AppRunner(build_app(bot), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, config.api_host, config.api_port).start()
        logging.info("Strait Pay API on %s:%s, public %s", config.api_host, config.api_port, config.api_url)
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query", "inline_query", "chat_member"])
    finally:
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        if runner is not None:
            await runner.cleanup()
        await api.close()
        await ton.close()
        if lock_conn is not None:
            await lock_conn.close()
        await models.engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
