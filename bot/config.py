from decimal import Decimal
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)  # KEY= means default

    bot_token: str
    admin_ids: list[int] = []
    # operators of Bybit-order deals: they get the merchant's order link, give its requisites to the buyer and
    # confirm the payment (acting as an admin in that deal). Empty = the admins.
    operator_ids: list[int] = []
    database_url: str = "postgresql+asyncpg://p2p:p2p@localhost:5432/p2p"
    # USDT BEP-20 (BNB Smart Chain): one BIP-39 seed is the whole cash desk (services/bsc.py). The bot makes it on the
    # first start and stores it encrypted with MASTER_SECRET: set MASTER_SECRET before that start, never change it.
    master_secret: str = ""
    bsc_mnemonic: str = ""  # own seed instead of the stored one (12/24 words)
    bsc_wallet_address: str = ""  # the hot wallet expected (0x…): a different derived one keeps the desk off
    bsc_rpc_urls: str = ""  # own (paid) RPC nodes, comma separated: tried first, the public ones stay as fallback
    bsc_cold_address: str = ""  # optional cold wallet: the hot wallet's surplus over bsc_hot_max_usdt goes there
    bsc_hot_max_usdt: Decimal | None = None
    log_chat_id: int | None = None
    log_thread_id: int | None = None  # topic id when the log chat is a forum supergroup
    # premium: custom emoji (bot owner needs Telegram Premium or a Fragment username);
    # plain: ordinary emoji only; none: no emoji at all, text-only interface.
    emoji_mode: Literal["premium", "plain", "none"] = "premium"
    # picture or silent mp4/gif animation on top of every screen (relative to the project root); empty = text only
    banner_path: str = "images/banner.mp4"
    # Merchant API (HTTP, same process). Put it behind HTTPS (nginx/caddy) on your domain and set api_public_url.
    api_enabled: bool = False
    api_host: str = "127.0.0.1"
    api_port: int = 8080
    api_public_url: str = ""  # e.g. https://api.straitpay.com — shown to clients; default http://host:port
    api_receipt_mb: int = 10

    @property
    def api_url(self) -> str:
        return (self.api_public_url or f"http://{self.api_host}:{self.api_port}").rstrip("/")

    @property
    def operators(self) -> list[int]:
        return self.operator_ids or self.admin_ids

    @property
    def log_targets(self) -> list[int]:
        return [self.log_chat_id] if self.log_chat_id else self.admin_ids


config = Config()  # type: ignore[call-arg]
