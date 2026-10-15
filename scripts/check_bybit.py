"""Read-only connection check. Never opens, pays, cancels or finishes an order.

Run from the project root: python -m scripts.check_bybit account-1
Uses locally configured BYBIT_ACCOUNTS; never prints keys or raw API replies.
"""
import argparse
import asyncio

from bot.config import config
from bot.services.bybit import BybitClient, BybitError


async def check(name: str) -> int:
    credentials = next((account for account in config.bybit_accounts if account.name == name), None)
    if credentials is None:
        print("Аккаунт с таким именем не настроен в BYBIT_ACCOUNTS.")
        return 1
    client = BybitClient(credentials, config.bybit_testnet)
    try:
        response = await client.request("/v5/p2p/order/pending/simplifyList", {"page": 1, "size": 1})
        print(f"{name}: чтение P2P-ордеров доступно; активных ордеров — {response.get('count', 'не указано')}.")
        print("Вход по объявлению, чтение чата и права на отметку оплаты этой проверкой не подтверждены.")
        return 0
    except BybitError as error:
        print(f"{name}: проверка не прошла, код {error.code}.")
        return 1
    finally:
        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("account", help="Name from BYBIT_ACCOUNTS")
    raise SystemExit(asyncio.run(check(parser.parse_args().account)))
