from pydantic_settings import BaseSettings
from pydantic import Field
from typing import List, Dict, Optional
import json


class Settings(BaseSettings):
	binance_api_key: str = Field(default="", alias="BINANCE_API_KEY")
	binance_api_secret: str = Field(default="", alias="BINANCE_API_SECRET")
	binance_base_url: str = Field(default="https://testnet.binancefuture.com", alias="BINANCE_BASE_URL")

	allocation_pct: float = Field(default=50, alias="ALLOCATION_PCT")
	default_leverage: int = Field(default=5, alias="DEFAULT_LEVERAGE")
	per_trade_pct: float = Field(default=10, alias="PER_TRADE_PCT")
	symbols_whitelist_raw: str = Field(default="BTCUSDT,ETHUSDT", alias="SYMBOLS_WHITELIST")
	dry_run: bool = Field(default=True, alias="DRY_RUN")

	leverage_policy: str = Field(default="auto", alias="LEVERAGE_POLICY")  # auto|webhook|per_symbol|default
	leverage_per_symbol_str: str = Field(default="", alias="LEVERAGE_PER_SYMBOL")  # e.g. BTCUSDT:7,ETHUSDT:6

	telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
	telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")

	public_base_url: str = Field(default="", alias="PUBLIC_BASE_URL")
	port: int = Field(default=8000, alias="PORT")
	env: str = Field(default="dev", alias="ENV")

	class Config:
		env_file = ".env"
		case_sensitive = False
		populate_by_name = True

	def get_symbols_whitelist(self) -> List[str]:
		val = (self.symbols_whitelist_raw or "").strip()
		if not val:
			# Frontend defaults allow BTCUSDT and ETHUSDT; keep backend consistent
			return ["BTCUSDT", "ETHUSDT"]
		# try json first
		try:
			loaded = json.loads(val)
			if isinstance(loaded, list):
				return [str(x).strip().upper() for x in loaded if str(x).strip()]
		except Exception:
			pass
		# fallback csv
		return [part.strip().upper() for part in val.split(",") if part.strip()]

	def leverage_map(self) -> Dict[str, int]:
		m: Dict[str, int] = {}
		raw = (self.leverage_per_symbol_str or "").strip()
		if not raw:
			return m
		for part in raw.split(","):
			pt = part.strip()
			if not pt:
				continue
			if ":" in pt:
				k, v = pt.split(":", 1)
				try:
					m[k.strip().upper()] = int(v.strip())
				except Exception:
					continue
		return m

	def get_leverage_for_symbol(self, symbol: str, payload_leverage: Optional[int]) -> int:
		policy = (self.leverage_policy or "auto").lower()
		symbol = (symbol or "").upper()
		if policy == "webhook" and payload_leverage:
			return int(payload_leverage)
		if policy == "per_symbol":
			return self.leverage_map().get(symbol, int(self.default_leverage))
		if policy == "default":
			return int(self.default_leverage)
		# auto
		if payload_leverage:
			return int(payload_leverage)
		return self.leverage_map().get(symbol, int(self.default_leverage))


def get_settings() -> Settings:
	return Settings()
