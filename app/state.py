from typing import Dict, Optional
from .config import Settings


class RuntimeConfigStore:
	def __init__(self) -> None:
		self.leverage_policy: Optional[str] = None
		self.default_leverage: Optional[int] = None
		self.leverage_per_symbol: Dict[str, int] = {}
		# New runtime-controlled risk settings
		self.allocation_cap_usd: Optional[float] = None  # Sabit USD limit (toplam kullanılabilir marj üst limiti)
		self.per_trade_pct: Optional[float] = None       # Her işlem için yüzdelik (örn. 10)

	def reset_from_settings(self, settings: Settings) -> None:
		self.leverage_policy = settings.leverage_policy
		self.default_leverage = settings.default_leverage
		self.leverage_per_symbol = settings.leverage_map()
		# Initialize new fields from settings defaults
		self.allocation_cap_usd = None  # Varsayılan olarak .env'den sabit bir USD limit yok
		self.per_trade_pct = settings.per_trade_pct

	def to_dict(self) -> Dict[str, object]:
		return {
			"leverage_policy": self.leverage_policy,
			"default_leverage": self.default_leverage,
			"leverage_per_symbol": self.leverage_per_symbol,
			"allocation_cap_usd": self.allocation_cap_usd,
			"per_trade_pct": self.per_trade_pct,
		}

	def set_from_dict(self, data: Dict[str, object]) -> None:
		if data is None:
			return
		if "leverage_policy" in data and data["leverage_policy"]:
			self.leverage_policy = str(data["leverage_policy"]).lower()
		if "default_leverage" in data and data["default_leverage"] is not None:
			try:
				self.default_leverage = int(data["default_leverage"])  # type: ignore[arg-type]
			except Exception:
				pass
		if "leverage_per_symbol" in data and isinstance(data["leverage_per_symbol"], dict):
			m: Dict[str, int] = {}
			for k, v in (data["leverage_per_symbol"] or {}).items():
				try:
					m[str(k).upper()] = int(v)  # type: ignore[arg-type]
				except Exception:
					continue
			self.leverage_per_symbol = m
		if "leverage_per_symbol_str" in data and isinstance(data["leverage_per_symbol_str"], str):
			m2: Dict[str, int] = {}
			for part in data["leverage_per_symbol_str"].split(","):
				pt = part.strip()
				if not pt:
					continue
				if ":" in pt:
					k, v = pt.split(":", 1)
					try:
						m2[k.strip().upper()] = int(v.strip())
					except Exception:
						continue
			self.leverage_per_symbol = m2
		# New runtime fields
		if "allocation_cap_usd" in data and data["allocation_cap_usd"] is not None:
			try:
				val = float(data["allocation_cap_usd"])  # type: ignore[arg-type]
				self.allocation_cap_usd = max(0.0, val)
			except Exception:
				pass
		if "per_trade_pct" in data and data["per_trade_pct"] is not None:
			try:
				valp = float(data["per_trade_pct"])  # type: ignore[arg-type]
				self.per_trade_pct = max(0.0, min(100.0, valp))
			except Exception:
				pass

	def select_leverage(self, settings: Settings, symbol: str, payload_leverage: Optional[int]) -> int:
		# Prefer runtime overrides; fallback to settings
		policy = (self.leverage_policy or settings.leverage_policy or "auto").lower()
		symbol = (symbol or "").upper()
		default_lev = int(self.default_leverage or settings.default_leverage)
		per_map = self.leverage_per_symbol or settings.leverage_map()
		if policy == "webhook" and payload_leverage:
			return int(payload_leverage)
		if policy == "per_symbol":
			return int(per_map.get(symbol, default_lev))
		if policy == "default":
			return default_lev
		# auto
		if payload_leverage:
			return int(payload_leverage)
		return int(per_map.get(symbol, default_lev))


runtime = RuntimeConfigStore()
