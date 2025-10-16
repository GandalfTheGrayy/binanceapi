from pydantic import BaseModel, Field
from typing import Optional, Any, Dict


class TradingViewWebhook(BaseModel):
    symbol: Optional[str] = None
    signal: str = Field(description="AL | SAT | BUY | SELL | LONG | SHORT")
    price: Optional[float] = None
    # Yeni: İsteğe bağlı miktar (coin adedini doğrudan göndermek için)
    qty: Optional[float] = None
    note: Optional[str] = None
    leverage: Optional[int] = None
    ts: Optional[int] = None
    meta: Optional[Dict[str, Any]] = None
    # TradingView specific fields
    ticker: Optional[str] = None
    ticker_upper: Optional[str] = None
    tickerid: Optional[str] = None


class OrderResult(BaseModel):
    success: bool
    message: str
    order_id: Optional[str] = None
    response: Optional[Dict[str, Any]] = None


class DashboardSummary(BaseModel):
    total_wallet_balance: float
    available_balance: float
    used_allocation_usd: float
    pnl_1h: float
    pnl_total: float
