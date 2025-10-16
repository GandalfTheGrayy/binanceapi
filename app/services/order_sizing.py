from typing import Dict, Any, Tuple
import math


def get_symbol_filters(exchange_info: Dict[str, Any], symbol: str) -> Dict[str, Any]:
	for s in exchange_info.get("symbols", []):
		if s.get("symbol") == symbol:
			filters = {f["filterType"]: f for f in s.get("filters", [])}
			return {
				"minQty": float(filters.get("LOT_SIZE", {}).get("minQty", 0.0)),
				"stepSize": float(filters.get("LOT_SIZE", {}).get("stepSize", 0.0)),
				"tickSize": float(filters.get("PRICE_FILTER", {}).get("tickSize", 0.0)),
			}
	raise ValueError(f"Symbol not found in exchangeInfo: {symbol}")


def round_step(value: float, step: float) -> float:
	if step == 0:
		return value
	return math.floor(value / step) * step


def compute_quantity(
	symbol: str,
	price: float,
	leverage: int,
	allocation_cap_usd: float,
	used_allocation_usd: float,
	per_trade_pct: float,
	exchange_info: Dict[str, Any],
) -> Tuple[float, float, float]:
	"""
	Returns (qty, margin_usd, notional_usd)
	- margin_usd = allocation_cap_usd * per_trade_pct/100, limited by remaining allocation
	- notional_usd = margin_usd * leverage
	- qty = notional_usd / price, rounded to step size and >= minQty
	"""
	filters = get_symbol_filters(exchange_info, symbol)
	remaining = max(0.0, allocation_cap_usd - used_allocation_usd)
	if remaining <= 0:
		raise ValueError("Allocation limit reached")

	# Her işlemde kullanılacak marj: cap'in per_trade_pct kadarı
	trade_margin = allocation_cap_usd * (per_trade_pct / 100.0)
	trade_margin = min(trade_margin, remaining)

	notional = trade_margin * max(1, leverage)
	qty_raw = notional / price
	qty = round_step(qty_raw, filters["stepSize"] or 0.0001)

	if qty <= 0 or qty < filters["minQty"]:
		raise ValueError("Calculated quantity below minimum lot size")

	return qty, trade_margin, notional
