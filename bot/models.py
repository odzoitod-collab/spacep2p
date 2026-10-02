from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import JSON, BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Numeric, String, Text, false, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

USDT = Numeric(20, 6)
RUB = Numeric(14, 2)


def now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {datetime: DateTime(timezone=True)}


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("balance >= 0", name="ck_users_balance_nonneg"),
                      CheckConstraint("frozen >= 0", name="ck_users_frozen_nonneg"))
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # telegram id
    username: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128), default="")
    balance: Mapped[Decimal] = mapped_column(USDT, default=Decimal(0))
    frozen: Mapped[Decimal] = mapped_column(USDT, default=Decimal(0))
    is_banned: Mapped[bool] = mapped_column(default=False)
    is_online: Mapped[bool] = mapped_column(default=False)
    last_seen: Mapped[datetime] = mapped_column(default=now)
    ui_msg_id: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(default=now)
    quiet: Mapped[bool] = mapped_column(default=False, server_default=false())  # deal notifications without sound
    # personal static-card merchant rate set by an admin; None = the general seller_pct.
    # Order merchants have no percent: they work at the fixed order_rate.
    pct_static: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    # personal terms of this user as a buyer (static card and order requisites alike), set by an admin;
    # None = the general rate / platform_pct. Like an API client's own terms.
    buy_rate: Mapped[Decimal | None] = mapped_column(RUB)
    buy_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    team_id: Mapped[int | None] = mapped_column(index=True)  # the team he joined by its leader's link
    # new | pending | approved | rejected: with signup_review on, a new user fills an application (Signup) and uses
    # the bot after an admin approves it; users from before the review existed are approved
    access: Mapped[str] = mapped_column(String(10), default="new", server_default="approved")


class Card(Base):
    __tablename__ = "cards"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(8))  # card | sbp
    bank: Mapped[str] = mapped_column(String(64))
    requisites: Mapped[str] = mapped_column(String(64))
    holder: Mapped[str] = mapped_column(String(128))
    min_rub: Mapped[Decimal] = mapped_column(RUB)
    max_rub: Mapped[Decimal] = mapped_column(RUB)
    daily_limit_rub: Mapped[Decimal | None] = mapped_column(RUB)  # bank's daily incoming limit; None = none
    is_active: Mapped[bool] = mapped_column(default=False)
    is_banned: Mapped[bool] = mapped_column(default=False)
    is_deleted: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=now)


class Deal(Base):
    __tablename__ = "deals"
    __table_args__ = (Index("ix_deals_buyer_status", "buyer_id", "status"),
                      Index("ix_deals_seller_status", "seller_id", "status"),
                      Index("ux_deals_api_external", "api_client_id", "external_id", unique=True))
    id: Mapped[int] = mapped_column(primary_key=True)
    buyer_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    # an order-requisites request has no seller and no card until a merchant takes it and gives requisites
    seller_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    card_id: Mapped[int | None] = mapped_column(ForeignKey("cards.id"), index=True)
    amount_rub: Mapped[Decimal] = mapped_column(RUB)
    rate: Mapped[Decimal] = mapped_column(RUB)
    seller_pct: Mapped[Decimal] = mapped_column(Numeric(6, 3))
    platform_pct: Mapped[Decimal] = mapped_column(Numeric(6, 3))
    seller_debit: Mapped[Decimal] = mapped_column(USDT)
    buyer_credit: Mapped[Decimal] = mapped_column(USDT)
    platform_fee: Mapped[Decimal] = mapped_column(USDT)
    # [searching -> assigned [-> checking] ->] waiting_payment -> paid -> completed | dispute -> completed/cancelled ;
    # expired ; cancelled. searching/assigned/checking: order requisites — expires_at is the search / requisites /
    # operator check deadline there (checking: a Bybit order link waits for an operator, see services/orders.py).
    status: Mapped[str] = mapped_column(String(20), index=True, default="waiting_payment")
    receipt_file_id: Mapped[str | None] = mapped_column(String(256))
    receipt_unique_id: Mapped[str | None] = mapped_column(String(64), index=True)  # same file in two deals = red flag
    expires_at: Mapped[datetime]
    paid_at: Mapped[datetime | None]  # receipt uploaded
    reminded: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    dispute_reason: Mapped[str | None] = mapped_column(String(20))  # see handlers.deal.REASONS
    dispute_amount_rub: Mapped[Decimal | None] = mapped_column(RUB)
    # evidence: [[kind, file_id_or_text, role], ...]; kind: video|photo|document|text, role: buyer|seller
    dispute_files: Mapped[list] = mapped_column(JSON, default=list)
    seller_msg_id: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(default=now)
    closed_at: Mapped[datetime | None]
    # why a deal ended: confirmed | buyer_cancel | expired | admin_void | ban_void |
    # dispute_buyer | dispute_actual | dispute_seller (see handlers.deal.CLOSE_REASONS)
    close_reason: Mapped[str | None] = mapped_column(String(20))
    # expired deal whose seller funds are still frozen until hold_until: a late receipt stays covered
    funds_held: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    hold_until: Mapped[datetime | None]
    resolution: Mapped[str | None] = mapped_column(Text)  # admin's comment to both parties on a dispute verdict
    # order opened through the API: buyer_id is the token owner; external_id is the client's idempotency key
    api_client_id: Mapped[int | None] = mapped_column(ForeignKey("api_clients.id"), index=True)
    external_id: Mapped[str | None] = mapped_column(String(64))
    api_notified: Mapped[str | None] = mapped_column(String(20))  # last status queued as a webhook
    is_order: Mapped[bool] = mapped_column(default=False, server_default=false())  # order requisites, not a static card
    sender_bank: Mapped[str | None] = mapped_column(String(40))  # order requisites: the buyer's bank
    # order requisites: the fixed RUB/USDT rate of the merchant side (seller_debit = amount_rub / merchant_rate);
    # None = a static card deal priced by seller_pct
    merchant_rate: Mapped[Decimal | None] = mapped_column(RUB)
    # the merchant works through a Bybit P2P order: nothing of his is frozen, an operator gives the requisites of the
    # order and checks the payment; the platform credits the buyer (USDT arrive on the operator's Bybit account)
    via_bybit: Mapped[bool] = mapped_column(default=False, server_default=false())
    bybit_url: Mapped[str | None] = mapped_column(String(300), index=True)
    operator_id: Mapped[int | None] = mapped_column(BigInteger)
    # the buyer's side rate when it differs from `rate` (an API client or a buyer with own terms); `rate` stays the
    # merchant side rate, so the merchant's terms and income never depend on the client
    buyer_rate: Mapped[Decimal | None] = mapped_column(RUB)
    # the merchant's team: its leader got team_fee USDT out of the platform's fee when the deal completed
    team_id: Mapped[int | None]
    team_fee: Mapped[Decimal] = mapped_column(USDT, default=Decimal(0), server_default=text("0"))


class Deposit(Base):
    __tablename__ = "deposits"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    invoice_id: Mapped[str | None] = mapped_column(String(64))
    amount: Mapped[Decimal] = mapped_column(USDT)
    credit: Mapped[Decimal] = mapped_column(USDT)
    link: Mapped[str | None] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(16), index=True, default="new")  # new|active|paid|expired
    created_at: Mapped[datetime] = mapped_column(default=now)
    # by address: an open-amount xRocket invoice and its payment address in `network`; None = invoice link
    network: Mapped[str | None] = mapped_column(String(8))
    address: Mapped[str | None] = mapped_column(String(128))
    expires_at: Mapped[datetime | None]
    # deposit: credited to the balance minus deposit_fee; debt: an operator repays his debt (no fee)
    purpose: Mapped[str] = mapped_column(String(8), default="deposit", server_default="deposit")


class Withdrawal(Base):
    __tablename__ = "withdrawals"
    __table_args__ = (Index("ix_withdrawals_request_id", "request_id", unique=True),)  # the name migration 1 used
    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[str | None] = mapped_column(String(36))
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(USDT)  # debited from balance
    fee: Mapped[Decimal] = mapped_column(USDT)
    cheque_id: Mapped[str | None] = mapped_column(String(64))
    link: Mapped[str | None] = mapped_column(String(256))
    # cheque (xrocket): pending|unknown|done|failed. chain: pending -> sent -> done | failed; unknown = no answer
    status: Mapped[str] = mapped_column(String(16), default="pending")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=now)
    # xrocket: personal cheque; chain: xRocket pays USDT to an external address in `network`
    method: Mapped[str] = mapped_column(String(8), default="xrocket", server_default="xrocket")
    address: Mapped[str | None] = mapped_column(String(128))  # chain: recipient
    memo: Mapped[str | None] = mapped_column(String(120))  # chain: comment an exchange may require (TON)
    tx_hash: Mapped[str | None] = mapped_column(String(128))
    sent_at: Mapped[datetime | None]
    network: Mapped[str | None] = mapped_column(String(8))  # chain: TON | TRX | ETH | BSC | SOL ...
    net_fee: Mapped[Decimal] = mapped_column(USDT, default=Decimal(0), server_default=text("0"))  # xRocket's part of fee


class Ledger(Base):
    __tablename__ = "ledger"
    __table_args__ = (Index("ix_ledger_user_id_id", "user_id", "id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)  # None = platform
    delta: Mapped[Decimal] = mapped_column(USDT)  # change of total holdings (available + frozen)
    frozen_delta: Mapped[Decimal] = mapped_column(USDT, default=Decimal(0), server_default=text("0"))
    kind: Mapped[str] = mapped_column(String(24))
    ref: Mapped[str] = mapped_column(String(64), default="")
    note: Mapped[str | None] = mapped_column(Text)  # reason of manual adjustments
    created_at: Mapped[datetime] = mapped_column(default=now)


class Audit(Base):
    """Admin actions and user support requests."""
    __tablename__ = "audit"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int] = mapped_column(BigInteger, index=True)
    action: Mapped[str] = mapped_column(String(32))
    target: Mapped[str] = mapped_column(String(64), default="")
    details: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=now)


class Adjustment(Base):
    """Manual balance change: draft -> (pending approval) -> done | cancelled | failed."""
    __tablename__ = "adjustments"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    admin_id: Mapped[int] = mapped_column(BigInteger)
    approved_by: Mapped[int | None] = mapped_column(BigInteger)
    delta: Mapped[Decimal] = mapped_column(USDT)
    reason: Mapped[str] = mapped_column(String(20))
    comment: Mapped[str] = mapped_column(Text, default="")
    balance_before: Mapped[Decimal | None] = mapped_column(USDT)
    balance_after: Mapped[Decimal | None] = mapped_column(USDT)
    status: Mapped[str] = mapped_column(String(16), index=True, default="draft")
    created_at: Mapped[datetime] = mapped_column(default=now)
    done_at: Mapped[datetime | None]


class Event(Base):
    """History of an operation (ref = wd:1, dep:2, deal:3, user:4, card:5, adj:6, ticket:7).
    alert=True rows are an outbox: stored with the change, delivered to admins by a retrying task."""
    __tablename__ = "events"
    __table_args__ = (Index("ix_events_ref_kind_created", "ref", "kind", "created_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    ref: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(Text)
    alert: Mapped[bool] = mapped_column(default=False)
    notice: Mapped[bool] = mapped_column(default=False, server_default=false())  # routine, not a problem
    sent_at: Mapped[datetime | None]
    attempts: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(default=now)


class Ticket(Base):
    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    deal_id: Mapped[int | None]
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(12), index=True, default="open")  # open|answered|closed
    answer: Mapped[str | None] = mapped_column(Text)
    answered_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime] = mapped_column(default=now)


class FsmState(Base):
    """Dialog state (what the bot is waiting for) survives restarts."""
    __tablename__ = "fsm_state"
    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    state: Mapped[str | None] = mapped_column(String(128))
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)


class OrderMerchant(Base):
    """Merchant who gives requisites on request (order requisites). The row is also the application:
    pending -> approved | rejected; approved -> suspended by an admin. An approved merchant gets every request
    (no amount limits, no on/off switch) and picks how to work each one when he takes it: a Bybit order link or
    his own balance (deal.via_bybit)."""
    __tablename__ = "order_merchants"
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(10), index=True, default="pending")
    source: Mapped[str] = mapped_column(String(200))  # where the merchant gets requisites / orders
    speed: Mapped[str] = mapped_column(String(40))  # how fast a card can be given
    banks: Mapped[str] = mapped_column(String(200), default="")
    about: Mapped[str] = mapped_column(Text, default="")
    pay_minutes: Mapped[int] = mapped_column(default=15, server_default=text("15"))  # default payment window given
    admin_id: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=now)
    decided_at: Mapped[datetime | None]


class OrderOffer(Base):
    """A request shown somewhere with a button. kind: merchant (his private chat, «Взять»), chat (a post in the
    community or a team chat, user_id = the chat id, a link into the bot), operator (a Bybit order waiting for an
    operator, «Принять ордер»). declined: the merchant gave the request up and is not offered it again."""
    __tablename__ = "order_offers"
    id: Mapped[int] = mapped_column(primary_key=True)
    deal_id: Mapped[int] = mapped_column(ForeignKey("deals.id"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    msg_id: Mapped[int | None]
    declined: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=now)
    kind: Mapped[str] = mapped_column(String(8), default="merchant", server_default="merchant")


class Operator(Base):
    """Operator of Bybit-order deals, added by an admin (OPERATOR_IDS in .env still work). He enters the merchant's
    Bybit order, gives its requisites and confirms the payment: the order's USDT arrive on his Bybit account, so each
    confirmed deal adds its seller_debit to his debt; he repays it with an xRocket invoice or from his balance."""
    __tablename__ = "operators"
    __table_args__ = (CheckConstraint("debt >= 0", name="ck_operators_debt_nonneg"),)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), primary_key=True)
    active: Mapped[bool] = mapped_column(default=True)
    debt: Mapped[Decimal] = mapped_column(USDT, default=Decimal(0))
    added_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(default=now)


class Signup(Base):
    """Application of a new user: who he is (P2P seller or buyer), daily turnover, a screenshot (sellers).
    pending -> approved | rejected; posted in the log chat's «Заявки на вход» topic with the decision buttons."""
    __tablename__ = "signups"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(8))  # seller | buyer
    turnover: Mapped[str] = mapped_column(String(60))
    proof: Mapped[str | None] = mapped_column(String(256))  # screenshot: "photo:<file_id>" or a document file_id
    status: Mapped[str] = mapped_column(String(10), index=True, default="pending")
    admin_id: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=now)
    decided_at: Mapped[datetime | None]


class Team(Base):
    """A team of merchants around a leader. The row is also the application: pending -> approved | rejected;
    approved -> suspended. Users who start the bot by the leader's link join it; requests are posted in its chat;
    the leader gets pct (None = the team_pct setting) of every completed deal of a member."""
    __tablename__ = "teams"
    id: Mapped[int] = mapped_column(primary_key=True)
    leader_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), unique=True)
    name: Mapped[str] = mapped_column(String(64))
    about: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(10), index=True, default="pending")
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    admin_id: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=now)
    decided_at: Mapped[datetime | None]


class ApiApplication(Base):
    """Request for API access, reviewed by an admin: pending -> approved | rejected."""
    __tablename__ = "api_applications"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    project: Mapped[str] = mapped_column(String(100))
    url: Mapped[str] = mapped_column(String(200))
    traffic: Mapped[str] = mapped_column(String(200))  # where buyers come from
    volume: Mapped[str] = mapped_column(String(100))  # expected monthly RUB volume, as the applicant wrote it
    about: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(10), index=True, default="pending")
    admin_id: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=now)
    decided_at: Mapped[datetime | None]


class ApiClient(Base):
    """Approved API access of one user. The token itself is never stored: only its SHA-256."""
    __tablename__ = "api_clients"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), unique=True)
    project: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(10), default="active")  # active | suspended
    token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    token_hint: Mapped[str | None] = mapped_column(String(8))  # last characters, to recognise the token
    token_at: Mapped[datetime | None]
    webhook_url: Mapped[str | None] = mapped_column(String(300))
    webhook_secret: Mapped[str] = mapped_column(String(64))
    min_rub: Mapped[Decimal] = mapped_column(RUB, default=Decimal(500))
    max_rub: Mapped[Decimal] = mapped_column(RUB, default=Decimal(100000))
    daily_rub: Mapped[Decimal] = mapped_column(RUB, default=Decimal(1000000))
    max_open: Mapped[int] = mapped_column(default=10)  # orders waiting for payment at the same time
    rps: Mapped[int] = mapped_column(default=5)  # requests per second
    created_at: Mapped[datetime] = mapped_column(default=now)
    # the client's own terms for every order (static card or order requisites): RUB per USDT and the platform's
    # percent; None = the general rate / platform_pct. 100 and 7% -> 10 000 RUB = 93 USDT to the client
    rate: Mapped[Decimal | None] = mapped_column(RUB)
    pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))


class ApiEvent(Base):
    """Webhook outbox: one row per order status change, retried with backoff until the client answers 2xx."""
    __tablename__ = "api_events"
    __table_args__ = (Index("ix_api_events_due", "delivered_at", "next_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("api_clients.id"), index=True)
    deal_id: Mapped[int] = mapped_column(ForeignKey("deals.id"))
    status: Mapped[str] = mapped_column(String(20))
    attempts: Mapped[int] = mapped_column(default=0)
    next_at: Mapped[datetime] = mapped_column(default=now)
    delivered_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=now)


class LogMessage(Base):
    """The log chat's card of one operation (ref): edited in place as its status changes."""
    __tablename__ = "log_messages"
    __table_args__ = (Index("ux_log_messages_chat_ref", "chat_id", "ref", unique=True),)
    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    ref: Mapped[str] = mapped_column(String(32))
    thread_id: Mapped[int | None]
    msg_id: Mapped[int]
    updated_at: Mapped[datetime] = mapped_column(default=now)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


engine = None
Session: async_sessionmaker[AsyncSession] = async_sessionmaker(expire_on_commit=False)
INSTANCE_LOCK = 7_212_001  # pg advisory lock key: one bot process per database

# Versioned PostgreSQL migrations for databases created by earlier releases.
# Each step is idempotent; applied versions are stored in schema_version.
MIGRATIONS: list[tuple[int, list[str]]] = [
    (1, [
        "ALTER TABLE withdrawals ADD COLUMN IF NOT EXISTS request_id VARCHAR(36)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_withdrawals_request_id ON withdrawals (request_id)",
    ]),
    (2, [
        "ALTER TABLE deals ADD COLUMN IF NOT EXISTS paid_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE deals ADD COLUMN IF NOT EXISTS reminded BOOLEAN NOT NULL DEFAULT false",
        "UPDATE deals SET paid_at = created_at WHERE paid_at IS NULL AND status IN ('paid', 'dispute')",
        "ALTER TABLE ledger ADD COLUMN IF NOT EXISTS note TEXT",
        """CREATE TABLE IF NOT EXISTS audit (
            id SERIAL PRIMARY KEY,
            actor_id BIGINT NOT NULL,
            action VARCHAR(32) NOT NULL,
            target VARCHAR(64) NOT NULL,
            details TEXT NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS ix_audit_actor_id ON audit (actor_id)",
        "CREATE INDEX IF NOT EXISTS ix_deals_status_paid_at ON deals (status, paid_at)",
    ]),
    (3, [
        "ALTER TABLE deals ADD COLUMN IF NOT EXISTS close_reason VARCHAR(20)",
        """UPDATE deals SET close_reason = CASE
            WHEN status = 'expired' THEN 'expired'
            WHEN status = 'void' THEN 'admin_void'
            WHEN status = 'cancelled' AND receipt_file_id IS NOT NULL THEN 'dispute_seller'
            WHEN status = 'cancelled' THEN 'buyer_cancel'
            WHEN status = 'completed' AND dispute_reason IS NOT NULL THEN 'dispute_buyer'
            WHEN status = 'completed' THEN 'confirmed' END
           WHERE close_reason IS NULL AND status NOT IN ('waiting_payment', 'paid', 'dispute')""",
        "ALTER TABLE ledger ADD COLUMN IF NOT EXISTS frozen_delta NUMERIC(20, 6) NOT NULL DEFAULT 0",
        "UPDATE ledger SET frozen_delta = delta WHERE kind = 'deal_sell'",
        # freezes were not journaled before: one opening row per user makes sum(frozen_delta) = frozen
        """INSERT INTO ledger (user_id, delta, frozen_delta, kind, ref, created_at)
           SELECT u.id, 0, u.frozen - COALESCE(l.f, 0), 'migration', 'm3', now()
           FROM users u LEFT JOIN (SELECT user_id, sum(frozen_delta) AS f FROM ledger GROUP BY user_id) l
             ON l.user_id = u.id
           WHERE u.frozen - COALESCE(l.f, 0) <> 0""",
        """CREATE TABLE IF NOT EXISTS adjustments (
            id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL, admin_id BIGINT NOT NULL, approved_by BIGINT,
            delta NUMERIC(20, 6) NOT NULL, reason VARCHAR(20) NOT NULL, comment TEXT NOT NULL,
            balance_before NUMERIC(20, 6), balance_after NUMERIC(20, 6), status VARCHAR(16) NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL, done_at TIMESTAMP WITH TIME ZONE)""",
        "CREATE INDEX IF NOT EXISTS ix_adjustments_user_id ON adjustments (user_id)",
        "CREATE INDEX IF NOT EXISTS ix_adjustments_status ON adjustments (status)",
        """CREATE TABLE IF NOT EXISTS events (
            id SERIAL PRIMARY KEY, ref VARCHAR(32) NOT NULL, user_id BIGINT, kind VARCHAR(32) NOT NULL,
            text TEXT NOT NULL, alert BOOLEAN NOT NULL, sent_at TIMESTAMP WITH TIME ZONE,
            attempts INTEGER NOT NULL, created_at TIMESTAMP WITH TIME ZONE NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS ix_events_ref ON events (ref)",
        "CREATE INDEX IF NOT EXISTS ix_events_user_id ON events (user_id)",
        "CREATE INDEX IF NOT EXISTS ix_events_outbox ON events (id) WHERE alert AND sent_at IS NULL",
        """CREATE TABLE IF NOT EXISTS tickets (
            id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL, deal_id INTEGER, text TEXT NOT NULL,
            status VARCHAR(12) NOT NULL, answer TEXT, answered_by BIGINT,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL, updated_at TIMESTAMP WITH TIME ZONE NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS ix_tickets_user_id ON tickets (user_id)",
        "CREATE INDEX IF NOT EXISTS ix_tickets_status ON tickets (status)",
    ]),
    (4, [
        "ALTER TABLE cards ADD COLUMN IF NOT EXISTS daily_limit_rub NUMERIC(14, 2)",
        "CREATE INDEX IF NOT EXISTS ix_deals_card_created ON deals (card_id, created_at)",
    ]),
    (5, [
        "ALTER TABLE deals ADD COLUMN IF NOT EXISTS funds_held BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE deals ADD COLUMN IF NOT EXISTS hold_until TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS notice BOOLEAN NOT NULL DEFAULT false",
        """CREATE TABLE IF NOT EXISTS fsm_state (
            key VARCHAR(160) PRIMARY KEY, state VARCHAR(128), data JSON NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL)""",
    ]),
    (6, [
        "ALTER TABLE deals ADD COLUMN IF NOT EXISTS resolution TEXT",
    ]),
    (7, [
        "ALTER TABLE withdrawals ADD COLUMN IF NOT EXISTS method VARCHAR(8) NOT NULL DEFAULT 'xrocket'",
        "ALTER TABLE withdrawals ADD COLUMN IF NOT EXISTS address VARCHAR(70)",
        "ALTER TABLE withdrawals ADD COLUMN IF NOT EXISTS memo VARCHAR(120)",
        "ALTER TABLE withdrawals ADD COLUMN IF NOT EXISTS tx_hash VARCHAR(64)",
        "ALTER TABLE withdrawals ADD COLUMN IF NOT EXISTS sent_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE deals ADD COLUMN IF NOT EXISTS receipt_unique_id VARCHAR(64)",
        "CREATE INDEX IF NOT EXISTS ix_deals_receipt_unique_id ON deals (receipt_unique_id)",
    ]),
    (8, [
        # API tables come from create_all; deals reference api_clients, so it must exist before the FK
        """CREATE TABLE IF NOT EXISTS api_clients (
            id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL UNIQUE REFERENCES users (id),
            project VARCHAR(100) NOT NULL, status VARCHAR(10) NOT NULL, token_hash VARCHAR(64) UNIQUE,
            token_hint VARCHAR(8), token_at TIMESTAMP WITH TIME ZONE, webhook_url VARCHAR(300),
            webhook_secret VARCHAR(64) NOT NULL, min_rub NUMERIC(14, 2) NOT NULL, max_rub NUMERIC(14, 2) NOT NULL,
            daily_rub NUMERIC(14, 2) NOT NULL, max_open INTEGER NOT NULL, rps INTEGER NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL)""",
        "ALTER TABLE deals ADD COLUMN IF NOT EXISTS api_client_id INTEGER REFERENCES api_clients (id)",
        "ALTER TABLE deals ADD COLUMN IF NOT EXISTS external_id VARCHAR(64)",
        "ALTER TABLE deals ADD COLUMN IF NOT EXISTS api_notified VARCHAR(20)",
        "CREATE INDEX IF NOT EXISTS ix_deals_api_client_id ON deals (api_client_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_deals_api_external ON deals (api_client_id, external_id)",
        "CREATE INDEX IF NOT EXISTS ix_deals_buyer_status ON deals (buyer_id, status)",
        "CREATE INDEX IF NOT EXISTS ix_deals_seller_status ON deals (seller_id, status)",
        "CREATE INDEX IF NOT EXISTS ix_ledger_user_id_id ON ledger (user_id, id)",
        "CREATE INDEX IF NOT EXISTS ix_events_ref_kind_created ON events (ref, kind, created_at)",
        # balances can never go negative, whatever code runs; NOT VALID: old rows are not re-checked at startup
        """DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_users_balance_nonneg') THEN
                ALTER TABLE users ADD CONSTRAINT ck_users_balance_nonneg CHECK (balance >= 0) NOT VALID;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_users_frozen_nonneg') THEN
                ALTER TABLE users ADD CONSTRAINT ck_users_frozen_nonneg CHECK (frozen >= 0) NOT VALID;
            END IF;
        END $$""",
    ]),
    (9, [
        # order requisites: order_merchants / order_offers come from create_all
        "ALTER TABLE deals ALTER COLUMN seller_id DROP NOT NULL",
        "ALTER TABLE deals ALTER COLUMN card_id DROP NOT NULL",
        "ALTER TABLE deals ADD COLUMN IF NOT EXISTS is_order BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE deals ADD COLUMN IF NOT EXISTS sender_bank VARCHAR(40)",
    ]),
    (10, [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS quiet BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE IF EXISTS order_merchants ADD COLUMN IF NOT EXISTS pay_minutes INTEGER NOT NULL DEFAULT 15",
    ]),
    (11, [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS pct_static NUMERIC(6, 3)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS pct_order NUMERIC(6, 3)",
    ]),
    (12, [
        # order merchants work at a fixed rate (order_rate), optionally through Bybit P2P orders
        "ALTER TABLE deals ADD COLUMN IF NOT EXISTS merchant_rate NUMERIC(14, 2)",
        "ALTER TABLE deals ADD COLUMN IF NOT EXISTS via_bybit BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE deals ADD COLUMN IF NOT EXISTS bybit_url VARCHAR(300)",
        "ALTER TABLE deals ADD COLUMN IF NOT EXISTS operator_id BIGINT",
        "CREATE INDEX IF NOT EXISTS ix_deals_bybit_url ON deals (bybit_url)",
        # merchants approved before keep working through their balance; new ones start with Bybit orders
        "ALTER TABLE IF EXISTS order_merchants ADD COLUMN IF NOT EXISTS mode VARCHAR(8) NOT NULL DEFAULT 'balance'",
        "ALTER TABLE IF EXISTS order_merchants ALTER COLUMN mode SET DEFAULT 'bybit'",
        "ALTER TABLE users DROP COLUMN IF EXISTS pct_order",
    ]),
    (13, [
        # USDT in every network through xRocket: the bot's own TON wallets are gone (old ton_* tables are kept
        # untouched as history); API clients get their own terms
        "ALTER TABLE deposits ADD COLUMN IF NOT EXISTS network VARCHAR(8)",
        "ALTER TABLE deposits ADD COLUMN IF NOT EXISTS address VARCHAR(128)",
        "ALTER TABLE deposits ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE withdrawals ADD COLUMN IF NOT EXISTS network VARCHAR(8)",
        "ALTER TABLE withdrawals ADD COLUMN IF NOT EXISTS net_fee NUMERIC(20, 6) NOT NULL DEFAULT 0",
        "ALTER TABLE withdrawals ALTER COLUMN address TYPE VARCHAR(128)",
        "ALTER TABLE withdrawals ALTER COLUMN tx_hash TYPE VARCHAR(128)",
        "UPDATE withdrawals SET method = 'chain', network = 'TON' WHERE method = 'ton'",
        "ALTER TABLE IF EXISTS api_clients ADD COLUMN IF NOT EXISTS rate NUMERIC(14, 2)",
        "ALTER TABLE IF EXISTS api_clients ADD COLUMN IF NOT EXISTS pct NUMERIC(6, 3)",
        "ALTER TABLE deals ADD COLUMN IF NOT EXISTS buyer_rate NUMERIC(14, 2)",
    ]),
    (14, [
        # order merchants get every request and choose Bybit order / balance per request: no limits, no switch;
        # operators and teams come from create_all; personal buyer terms; operator debt repaid by invoice
        "ALTER TABLE IF EXISTS order_merchants DROP COLUMN IF EXISTS min_rub",
        "ALTER TABLE IF EXISTS order_merchants DROP COLUMN IF EXISTS max_rub",
        "ALTER TABLE IF EXISTS order_merchants DROP COLUMN IF EXISTS max_open_rub",
        "ALTER TABLE IF EXISTS order_merchants DROP COLUMN IF EXISTS accepting",
        "ALTER TABLE IF EXISTS order_merchants DROP COLUMN IF EXISTS mode",
        "ALTER TABLE IF EXISTS order_offers ADD COLUMN IF NOT EXISTS kind VARCHAR(8) NOT NULL DEFAULT 'merchant'",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS buy_rate NUMERIC(14, 2)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS buy_pct NUMERIC(6, 3)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS team_id INTEGER",
        "CREATE INDEX IF NOT EXISTS ix_users_team_id ON users (team_id)",
        "ALTER TABLE deals ADD COLUMN IF NOT EXISTS team_id INTEGER",
        "ALTER TABLE deals ADD COLUMN IF NOT EXISTS team_fee NUMERIC(20, 6) NOT NULL DEFAULT 0",
        "ALTER TABLE deposits ADD COLUMN IF NOT EXISTS purpose VARCHAR(8) NOT NULL DEFAULT 'deposit'",
    ]),
    (15, [
        # entry by application: everyone already in the bot keeps working; signups come from create_all
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS access VARCHAR(10) NOT NULL DEFAULT 'approved'",
    ]),
]


async def migrate(conn) -> list[int]:
    """Apply pending migrations under a transaction-level advisory lock. Returns applied versions."""
    await conn.execute(text("SELECT pg_advisory_xact_lock(7212002)"))
    await conn.execute(text("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, "
                            "applied_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now())"))
    done = set((await conn.execute(text("SELECT version FROM schema_version"))).scalars())
    applied = []
    for version, steps in MIGRATIONS:
        if version in done:
            continue
        for sql in steps:
            await conn.execute(text(sql))
        await conn.execute(text("INSERT INTO schema_version (version) VALUES (:v)"), {"v": version})
        applied.append(version)
    return applied


async def init_db(url: str) -> None:
    global engine
    pg = url.startswith("postgresql")
    # Pool fits concurrent updates plus background jobs; a lock wait fails after 15 s instead of hanging
    # a user's update forever (the error handler answers, background jobs retry on their next run).
    engine = create_async_engine(url, pool_pre_ping=True, **(dict(
        pool_size=10, max_overflow=20, pool_timeout=15, connect_args={"server_settings": {"lock_timeout": "15s"}},
    ) if pg else {}))
    Session.configure(bind=engine)
    async with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            # Existing tables are upgraded by explicit ALTERs first; create_all then only adds
            # tables that do not exist yet (fresh database).
            await conn.execute(text("SELECT pg_advisory_xact_lock(7212002)"))
            if (await conn.scalar(text("SELECT to_regclass('deals')"))) is not None:
                await migrate(conn)
            await conn.run_sync(Base.metadata.create_all)
            await migrate(conn)
        else:  # SQLite: unit tests only
            await conn.run_sync(Base.metadata.create_all)
