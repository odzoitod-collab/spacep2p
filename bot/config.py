from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class BybitCredential(BaseModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,32}$")
    uid: str = Field(pattern=r"^[0-9]{1,32}$")
    api_key: SecretStr
    api_secret: SecretStr


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)  # KEY= means default

    bot_token: str
    admin_ids: list[int] = []
    database_url: str = "postgresql+asyncpg://p2p:p2p@localhost:5432/p2p"
    xrocket_token: str = ""
    xrocket_base_url: str = "https://pay.api.xrocket.exchange"
    log_chat_id: int | None = None
    log_thread_id: int | None = None  # topic id when the log chat is a forum supergroup
    # premium: custom emoji (bot owner needs Telegram Premium or a Fragment username);
    # plain: ordinary emoji only; none: no emoji at all, text-only interface.
    emoji_mode: Literal["premium", "plain", "none"] = "premium"
    # picture or silent mp4/gif animation on top of every screen (relative to the project root); empty = text only
    banner_path: str = "images/banner.mp4"
    # USDT on TON. ton_seed: 64 hex chars (32 random bytes) — every deposit wallet and the gas wallet are derived
    # from it. Empty = TON deposits off. Losing it = losing access to funds on deposit wallets; never share it.
    ton_seed: str = ""
    ton_api_key: str = ""  # toncenter.com key (@tonapibot): 10 req/s instead of 1
    ton_testnet: bool = False
    ton_usdt_master: str = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"  # Tether USD (USD₮) jetton, mainnet

    # Merchant API (HTTP, same process). Put it behind HTTPS (nginx/caddy) on your domain and set api_public_url.
    api_enabled: bool = False
    api_host: str = "127.0.0.1"
    api_port: int = 8080
    api_public_url: str = ""  # e.g. https://api.straitpay.com — shown to clients; default http://host:port
    api_receipt_mb: int = 10

    # Credentials stay outside the database and Telegram. One active order per account initially.
    bybit_enabled: bool = False
    bybit_testnet: bool = False
    bybit_accounts: list[BybitCredential] = []
    bybit_poll_seconds: int = Field(default=15, ge=5, le=300)

    @field_validator("bybit_accounts")
    @classmethod
    def _bybit_accounts(cls, values: list[BybitCredential]) -> list[BybitCredential]:
        if len({v.name for v in values}) != len(values) or len({v.uid for v in values}) != len(values):
            raise ValueError("Bybit account names and UIDs must be unique")
        if any(not v.api_key.get_secret_value() or not v.api_secret.get_secret_value() for v in values):
            raise ValueError("Bybit credentials must not be empty")
        return values

    @property
    def api_url(self) -> str:
        return (self.api_public_url or f"http://{self.api_host}:{self.api_port}").rstrip("/")

    @field_validator("ton_seed")
    @classmethod
    def _seed(cls, v: str) -> str:
        v = v.strip()
        if v and (len(v) != 64 or any(c not in "0123456789abcdefABCDEF" for c in v)):
            raise ValueError("TON_SEED must be 64 hex characters: python -c \"import secrets; print(secrets.token_hex(32))\"")
        return v

    @property
    def log_targets(self) -> list[int]:
        return [self.log_chat_id] if self.log_chat_id else self.admin_ids


config = Config()  # type: ignore[call-arg]
