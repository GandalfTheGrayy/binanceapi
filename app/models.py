from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, JSON, Text
from sqlalchemy.sql import func
from .database import Base


class WebhookEvent(Base):
	__tablename__ = "webhook_events"

	id = Column(Integer, primary_key=True, index=True)
	symbol = Column(String(32), index=True)
	signal = Column(String(16), index=True)
	price = Column(Float, nullable=True)
	payload = Column(JSON)
	created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class OrderRecord(Base):
	__tablename__ = "orders"

	id = Column(Integer, primary_key=True, index=True)
	binance_order_id = Column(String(64), index=True, nullable=True)
	symbol = Column(String(32), index=True)
	side = Column(String(16), index=True)
	position_side = Column(String(16), index=True, nullable=True)
	leverage = Column(Integer, default=0)
	qty = Column(Float)
	price = Column(Float, nullable=True)
	status = Column(String(32), default="NEW")
	response = Column(JSON)
	created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class BalanceSnapshot(Base):
	__tablename__ = "balance_snapshots"

	id = Column(Integer, primary_key=True, index=True)
	total_wallet_balance = Column(Float)
	available_balance = Column(Float)
	used_allocation_usd = Column(Float, default=0)
	total_equity = Column(Float, default=0.0)  # Wallet Balance + Unrealized PnL
	unrealized_pnl = Column(Float, default=0.0)
	note = Column(Text, nullable=True)
	created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class BinanceAPILog(Base):
	__tablename__ = "binance_api_logs"

	id = Column(Integer, primary_key=True, index=True)
	method = Column(String(8), index=True)
	path = Column(String(255), index=True)
	url = Column(Text)
	request_params = Column(JSON, nullable=True)
	status_code = Column(Integer, nullable=True)
	response = Column(JSON, nullable=True)
	error = Column(Text, nullable=True)
	created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
