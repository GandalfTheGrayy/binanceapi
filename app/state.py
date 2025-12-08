from typing import Dict, Optional, Any
from .config import Settings
import json


class RuntimeConfigStore:
	def __init__(self) -> None:
		self.leverage_policy: Optional[str] = None
		self.default_leverage: Optional[int] = None
		self.leverage_per_symbol: Dict[str, int] = {}
		# New runtime-controlled risk settings
		self.allocation_cap_usd: Optional[float] = None  # Sabit USD limit (toplam kullanılabilir marj üst limiti)
		self.per_trade_pct: Optional[float] = None       # Her işlem için yüzdelik (örn. 10)
		self.fixed_trade_amount: Optional[float] = None  # Sabit işlem tutarı (varsa öncelikli)

	def reset_from_settings(self, settings: Settings) -> None:
		self.leverage_policy = settings.leverage_policy
		self.default_leverage = settings.default_leverage
		self.leverage_per_symbol = settings.leverage_map()
		# Initialize new fields from settings defaults
		self.allocation_cap_usd = None  # Varsayılan olarak .env'den sabit bir USD limit yok
		self.per_trade_pct = settings.per_trade_pct
		self.fixed_trade_amount = settings.fixed_trade_amount

	def to_dict(self) -> Dict[str, object]:
		return {
			"leverage_policy": self.leverage_policy,
			"default_leverage": self.default_leverage,
			"leverage_per_symbol": self.leverage_per_symbol,
			"allocation_cap_usd": self.allocation_cap_usd,
			"per_trade_pct": self.per_trade_pct,
			"fixed_trade_amount": self.fixed_trade_amount,
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
		if "fixed_trade_amount" in data and data["fixed_trade_amount"] is not None:
			try:
				valf = float(data["fixed_trade_amount"])  # type: ignore[arg-type]
				self.fixed_trade_amount = max(0.0, valf)
			except Exception:
				pass
		if "allocation_cap_usd" in data and data["allocation_cap_usd"] is not None:
			try:
				self.allocation_cap_usd = float(data["allocation_cap_usd"])  # type: ignore[arg-type]
			except Exception:
				pass
		if "per_trade_pct" in data and data["per_trade_pct"] is not None:
			try:
				self.per_trade_pct = float(data["per_trade_pct"])  # type: ignore[arg-type]
			except Exception:
				pass
		if "leverage_per_symbol" in data and data["leverage_per_symbol"]:
			if isinstance(data["leverage_per_symbol"], dict):
				self.leverage_per_symbol = {str(k): int(v) for k, v in data["leverage_per_symbol"].items()}
			elif isinstance(data["leverage_per_symbol"], str):
				try:
					parsed = json.loads(data["leverage_per_symbol"])
					if isinstance(parsed, dict):
						self.leverage_per_symbol = {str(k): int(v) for k, v in parsed.items()}
				except Exception:
					pass

	def load_from_db(self, db_session: Any) -> bool:
		"""
		DB'den ayarları yükle. Başarılıysa True döner.
		Herhangi bir hata olursa False döner (fallback için).
		"""
		try:
			from . import models
			rows = db_session.query(models.RuntimeSettings).all()
			if not rows:
				return False
			data: Dict[str, Any] = {}
			for r in rows:
				key = r.key
				value = r.value
				# Parse values based on key type
				if key in ("fixed_trade_amount", "allocation_cap_usd", "per_trade_pct"):
					try:
						data[key] = float(value) if value else None
					except Exception:
						data[key] = None
				elif key == "default_leverage":
					try:
						data[key] = int(value) if value else None
					except Exception:
						data[key] = None
				elif key == "leverage_per_symbol":
					try:
						data[key] = json.loads(value) if value else {}
					except Exception:
						data[key] = {}
				else:
					data[key] = value
			
			# Apply loaded values
			if "leverage_policy" in data and data["leverage_policy"]:
				self.leverage_policy = str(data["leverage_policy"])
			if "default_leverage" in data and data["default_leverage"] is not None:
				self.default_leverage = data["default_leverage"]
			if "leverage_per_symbol" in data and data["leverage_per_symbol"]:
				self.leverage_per_symbol = data["leverage_per_symbol"]
			if "allocation_cap_usd" in data and data["allocation_cap_usd"] is not None:
				self.allocation_cap_usd = data["allocation_cap_usd"]
			if "per_trade_pct" in data and data["per_trade_pct"] is not None:
				self.per_trade_pct = data["per_trade_pct"]
			if "fixed_trade_amount" in data and data["fixed_trade_amount"] is not None:
				self.fixed_trade_amount = data["fixed_trade_amount"]
			
			print("[RuntimeConfigStore] Ayarlar DB'den yüklendi")
			return True
		except Exception as e:
			print(f"[RuntimeConfigStore] DB'den yükleme hatası: {e}")
			return False

	def save_to_db(self, db_session: Any) -> bool:
		"""
		Mevcut ayarları DB'ye kaydet.
		Başarılıysa True döner.
		"""
		try:
			from . import models
			settings_dict = {
				"fixed_trade_amount": str(self.fixed_trade_amount) if self.fixed_trade_amount is not None else "0",
				"allocation_cap_usd": str(self.allocation_cap_usd) if self.allocation_cap_usd is not None else "0",
				"per_trade_pct": str(self.per_trade_pct) if self.per_trade_pct is not None else "10",
				"leverage_policy": self.leverage_policy or "auto",
				"default_leverage": str(self.default_leverage) if self.default_leverage is not None else "5",
				"leverage_per_symbol": json.dumps(self.leverage_per_symbol or {}),
			}
			for key, value in settings_dict.items():
				existing = db_session.query(models.RuntimeSettings).filter_by(key=key).first()
				if existing:
					existing.value = value
				else:
					db_session.add(models.RuntimeSettings(key=key, value=value))
			db_session.commit()
			print("[RuntimeConfigStore] Ayarlar DB'ye kaydedildi")
			return True
		except Exception as e:
			print(f"[RuntimeConfigStore] DB'ye kaydetme hatası: {e}")
			try:
				db_session.rollback()
			except Exception:
				pass
			return False

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

