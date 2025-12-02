from typing import Any, Dict, Optional, List
import time
import hmac
import hashlib
from urllib.parse import urlencode
import httpx


class BinanceFuturesClient:
	def __init__(self, api_key: str, api_secret: str, base_url: str):
		self.api_key = api_key
		self.api_secret = api_secret  # Keep as string, encode when needed
		self.base_url = base_url.rstrip("/")
		self._client = httpx.AsyncClient(base_url=self.base_url, timeout=20.0)
		self._last_request_debug = None
		self._last_status_code: Optional[int] = None
		self._time_offset_ms: int = 0
		self._time_synced: bool = False

	async def close(self):
		await self._client.aclose()

	async def __aenter__(self):
		return self

	async def __aexit__(self, exc_type, exc_val, exc_tb):
		await self.close()

	def _headers(self) -> Dict[str, str]:
		return {"X-MBX-APIKEY": self.api_key}

	def _timestamp(self) -> int:
		return int(time.time() * 1000) + self._time_offset_ms

	def _sign(self, params: Dict[str, Any]) -> str:
		query = urlencode(params, doseq=True)
		return hmac.new(self.api_secret.encode('utf-8'), query.encode('utf-8'), hashlib.sha256).hexdigest()

	async def _signed_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> httpx.Response:
		params = params.copy() if params else {}
		await self._ensure_time_sync()
		params.setdefault("timestamp", self._timestamp())
		params.setdefault("recvWindow", 10000)
		sig = self._sign(params)
		params["signature"] = sig
		
		# Debug bilgilerini sakla (headerları maskele)
		full_url = f"{self.base_url}{path}"
		query_string = urlencode({k: v for k, v in params.items() if k != 'signature'}, doseq=True)
		self._last_request_debug = {
			"url": full_url,
			"headers": {"X-MBX-APIKEY": "***"},
			"params": {**params, "signature": "***"},
			"signature": "***",
			"api_secret": "***",
			"query_string": query_string,
			"signature_input": query_string
		}
		
		resp = await self._client.get(path, params=params, headers=self._headers())
		self._last_status_code = resp.status_code
		return resp

	async def _signed_post(self, path: str, params: Optional[Dict[str, Any]] = None) -> httpx.Response:
		params = params.copy() if params else {}
		await self._ensure_time_sync()
		params.setdefault("timestamp", self._timestamp())
		params.setdefault("recvWindow", 10000)
		sig = self._sign(params)
		params["signature"] = sig
		
		# Debug bilgilerini sakla (headerları maskele)
		full_url = f"{self.base_url}{path}"
		query_string = urlencode({k: v for k, v in params.items() if k != 'signature'}, doseq=True)
		self._last_request_debug = {
			"url": full_url,
			"headers": {"X-MBX-APIKEY": "***"},
			"params": {**params, "signature": "***"},
			"signature": "***",
			"api_secret": "***",
			"query_string": query_string,
			"signature_input": query_string
		}
		
		resp = await self._client.post(path, data=params, headers=self._headers())
		self._last_status_code = resp.status_code
		return resp

	async def test_connectivity(self) -> Dict[str, Any]:
		resp = await self._client.get("/fapi/v1/ping")
		self._last_status_code = resp.status_code
		resp.raise_for_status()
		return resp.json() if resp.text else {}

	async def server_time(self) -> int:
		"""Return server time in milliseconds."""
		resp = await self._client.get("/fapi/v1/time")
		self._last_status_code = resp.status_code
		resp.raise_for_status()
		data = resp.json()
		return int(data.get("serverTime", 0))

	async def _ensure_time_sync(self) -> None:
		"""Sync local offset to Binance server time once to avoid timestamp 400s."""
		if self._time_synced:
			return
		try:
			server_ts = await self.server_time()
			local_ts = int(time.time() * 1000)
			self._time_offset_ms = server_ts - local_ts
			self._time_synced = True
		except Exception:
			# If server time fetch fails, continue without sync
			self._time_offset_ms = 0
			self._time_synced = True

	async def exchange_info(self) -> Dict[str, Any]:
		resp = await self._client.get("/fapi/v1/exchangeInfo")
		self._last_status_code = resp.status_code
		resp.raise_for_status()
		return resp.json()

	async def account_usdt_balances(self) -> Dict[str, float]:
		"""Return wallet and available USDT balances for USDT-M futures."""
		try:
			resp = await self._signed_get("/fapi/v2/balance")
			self._last_status_code = resp.status_code
			resp.raise_for_status()
			assets = resp.json()
			for a in assets:
				if a.get("asset") == "USDT":
					# walletBalance: total wallet; availableBalance: free balance
					return {
						"wallet": float(a.get("balance", a.get("walletBalance", 0.0))),
						"available": float(a.get("availableBalance", 0.0)),
					}
			return {"wallet": 0.0, "available": 0.0}
		except httpx.HTTPStatusError:
			# Some Testnet environments return 400 for v2; fallback to v3
			resp2 = await self._signed_get("/fapi/v3/account")
			self._last_status_code = resp2.status_code
			resp2.raise_for_status()
			data = resp2.json()
			assets = data.get("assets") or []
			for a in assets:
				if a.get("asset") == "USDT":
					return {
						"wallet": float(a.get("walletBalance", 0.0)),
						"available": float(a.get("availableBalance", 0.0)),
					}
			# Fallback to top-level totals if assets list missing
			return {
				"wallet": float(data.get("totalWalletBalance", 0.0)),
				"available": float(data.get("availableBalance", 0.0)),
			}

	async def set_leverage(self, symbol: str, leverage: int) -> Dict[str, Any]:
		resp = await self._signed_post("/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})
		resp.raise_for_status()
		return resp.json()

	async def set_margin_type(self, symbol: str, margin_type: str) -> Dict[str, Any]:
		"""Set margin type for a symbol. margin_type: 'ISOLATED' or 'CROSSED'"""
		resp = await self._signed_post("/fapi/v1/marginType", {"symbol": symbol, "marginType": margin_type})
		resp.raise_for_status()
		return resp.json()

	async def place_market_order(self, symbol: str, side: str, quantity: float, position_side: Optional[str] = None, reduce_only: bool = False) -> Dict[str, Any]:
		params: Dict[str, Any] = {
			"symbol": symbol,
			"side": side,
			"type": "MARKET",
			"quantity": f"{quantity}",
		}
		if position_side:
			params["positionSide"] = position_side
		if reduce_only:
			params["reduceOnly"] = "true"
		resp = await self._signed_post("/fapi/v1/order", params)
		resp.raise_for_status()
		return resp.json()

	async def positions(self, symbols: Optional[List[str]] = None) -> List[Dict[str, Any]]:
		"""Return current positions; if symbols provided, filter accordingly."""
		resp = await self._signed_get("/fapi/v2/positionRisk")
		resp.raise_for_status()
		data = resp.json()
		result: List[Dict[str, Any]] = []
		for p in data:
			if symbols and p.get("symbol") not in symbols:
				continue
			try:
				amt = float(p.get("positionAmt", 0))
				if abs(amt) > 0:
					result.append(p)
			except Exception:
				continue
		return result

	async def position_mode(self) -> Dict[str, Any]:
		"""Return position mode info: {"dualSidePosition": bool}"""
		resp = await self._signed_get("/fapi/v1/positionSide/dual")
		resp.raise_for_status()
		return resp.json()

	async def set_position_mode(self, dual: bool) -> Dict[str, Any]:
		"""Set position mode. dual=True => Hedge (dual-side), dual=False => One-way."""
		# Binance expects a string 'true'/'false'
		val = "true" if dual else "false"
		resp = await self._signed_post("/fapi/v1/positionSide/dual", {"dualSidePosition": val})
		resp.raise_for_status()
		return resp.json()

	async def position_risk(self, symbols: Optional[List[str]] = None) -> List[Dict[str, Any]]:
		"""Return raw position risk entries (including zero positions) to read maxNotionalValue etc."""
		resp = await self._signed_get("/fapi/v2/positionRisk")
		resp.raise_for_status()
		data = resp.json()
		if symbols:
			return [p for p in data if p.get("symbol") in symbols]
		return data

	def get_last_request_debug(self) -> Optional[Dict[str, Any]]:
		"""Son request'in debug bilgilerini döndür (maskeleme uygulanmış)"""
		return self._last_request_debug

	def get_last_status_code(self) -> Optional[int]:
		return self._last_status_code

	# Yeni: halka açık fiyat (ticker) endpointi
	async def ticker_price(self, symbol: str) -> float:
		"""Return latest price for a symbol from /fapi/v1/ticker/price"""
		resp = await self._client.get("/fapi/v1/ticker/price", params={"symbol": symbol})
		self._last_status_code = resp.status_code
		resp.raise_for_status()
		data = resp.json()
		price_val = data.get("price")
		try:
			return float(price_val)
		except Exception:
			return 0.0
