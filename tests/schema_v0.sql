-- Schema of the first production release (before versioned migrations). Used by tests/test_postgres.py.
CREATE TABLE ledger (
	id SERIAL NOT NULL, 
	user_id BIGINT, 
	delta NUMERIC(20, 6) NOT NULL, 
	kind VARCHAR(24) NOT NULL, 
	ref VARCHAR(64) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_ledger_user_id ON ledger (user_id);
CREATE TABLE settings (
	key VARCHAR(32) NOT NULL, 
	value TEXT NOT NULL, 
	PRIMARY KEY (key)
);
CREATE TABLE users (
	id BIGSERIAL NOT NULL, 
	username VARCHAR(64), 
	name VARCHAR(128) NOT NULL, 
	balance NUMERIC(20, 6) NOT NULL, 
	frozen NUMERIC(20, 6) NOT NULL, 
	is_banned BOOLEAN NOT NULL, 
	is_online BOOLEAN NOT NULL, 
	last_seen TIMESTAMP WITH TIME ZONE NOT NULL, 
	ui_msg_id INTEGER, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);
CREATE TABLE cards (
	id SERIAL NOT NULL, 
	user_id BIGINT NOT NULL, 
	kind VARCHAR(8) NOT NULL, 
	bank VARCHAR(64) NOT NULL, 
	requisites VARCHAR(64) NOT NULL, 
	holder VARCHAR(128) NOT NULL, 
	min_rub NUMERIC(14, 2) NOT NULL, 
	max_rub NUMERIC(14, 2) NOT NULL, 
	is_active BOOLEAN NOT NULL, 
	is_banned BOOLEAN NOT NULL, 
	is_deleted BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);
CREATE INDEX ix_cards_user_id ON cards (user_id);
CREATE TABLE deposits (
	id SERIAL NOT NULL, 
	user_id BIGINT NOT NULL, 
	invoice_id VARCHAR(64), 
	amount NUMERIC(20, 6) NOT NULL, 
	credit NUMERIC(20, 6) NOT NULL, 
	link VARCHAR(256), 
	status VARCHAR(16) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);
CREATE INDEX ix_deposits_status ON deposits (status);
CREATE INDEX ix_deposits_user_id ON deposits (user_id);
CREATE TABLE withdrawals (
	id SERIAL NOT NULL, 
	user_id BIGINT NOT NULL, 
	amount NUMERIC(20, 6) NOT NULL, 
	fee NUMERIC(20, 6) NOT NULL, 
	cheque_id VARCHAR(64), 
	link VARCHAR(256), 
	status VARCHAR(16) NOT NULL, 
	error TEXT, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);
CREATE INDEX ix_withdrawals_user_id ON withdrawals (user_id);
CREATE TABLE deals (
	id SERIAL NOT NULL, 
	buyer_id BIGINT NOT NULL, 
	seller_id BIGINT NOT NULL, 
	card_id INTEGER NOT NULL, 
	amount_rub NUMERIC(14, 2) NOT NULL, 
	rate NUMERIC(14, 2) NOT NULL, 
	seller_pct NUMERIC(6, 3) NOT NULL, 
	platform_pct NUMERIC(6, 3) NOT NULL, 
	seller_debit NUMERIC(20, 6) NOT NULL, 
	buyer_credit NUMERIC(20, 6) NOT NULL, 
	platform_fee NUMERIC(20, 6) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	receipt_file_id VARCHAR(256), 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	dispute_reason VARCHAR(20), 
	dispute_amount_rub NUMERIC(14, 2), 
	dispute_files JSON NOT NULL, 
	seller_msg_id INTEGER, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	closed_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(buyer_id) REFERENCES users (id), 
	FOREIGN KEY(seller_id) REFERENCES users (id), 
	FOREIGN KEY(card_id) REFERENCES cards (id)
);
CREATE INDEX ix_deals_buyer_id ON deals (buyer_id);
CREATE INDEX ix_deals_status ON deals (status);
CREATE INDEX ix_deals_card_id ON deals (card_id);
CREATE INDEX ix_deals_seller_id ON deals (seller_id);
