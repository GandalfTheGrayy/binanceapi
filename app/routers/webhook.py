from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from typing import Dict, Any
import httpx
import math

from .. import schemas, models
from ..database import SessionLocal
from ..config import get_settings
from ..services.binance_client import BinanceFuturesClient
from ..services.order_sizing import compute_quantity, get_symbol_filters, round_step
from ..services.telegram import TelegramNotifier
from ..state import runtime
from ..services.ws_manager import ws_manager
from ..services.symbols import normalize_tv_symbol
from ..services.webhook_queue import webhook_queue_layer1, webhook_queue_layer2, get_webhook_queue

router = APIRouter(prefix="/webhook", tags=["webhook"])


def get_db():
	db = SessionLocal()
	try:
		yield db
	finally:
		db.close()


def get_client_ip(request: Request) -> str:
	"""Extract client IP address from request, handling proxies."""
	# Proxy arkasındaysa (X-Forwarded-For)
	forwarded = request.headers.get("X-Forwarded-For")
	if forwarded:
		return forwarded.split(",")[0].strip()
	
	# X-Real-IP header'ı
	real_ip = request.headers.get("X-Real-IP")
	if real_ip:
		return real_ip
	
	# Direkt bağlantı
	return request.client.host if request.client else "unknown"


def _log_binance_call(db: Session, method: str, path: str, client: BinanceFuturesClient, response_data: Any | None = None, error: str | None = None):
	debug = client.get_last_request_debug() if hasattr(client, 'get_last_request_debug') else None
	status = client.get_last_status_code() if hasattr(client, 'get_last_status_code') else None
	log = models.BinanceAPILog(
		method=method,
		path=path,
		url=(debug or {}).get('url') if debug else None,
		request_params=(debug or {}).get('params') if debug else None,
		status_code=status,
		response=response_data if error is None else None,
		error=error,
	)
	db.add(log)
	# Not committing here; rely on caller to commit alongside other records


def get_or_create_endpoint_config(db: Session, endpoint: str) -> models.EndpointConfig:
	"""Endpoint config'i DB'den al veya oluştur."""
	config = db.query(models.EndpointConfig).filter_by(endpoint=endpoint).first()
	if not config:
		# .env'den varsayılan değerleri al
		settings = get_settings()
		env_config = settings.get_endpoint_config(endpoint)
		config = models.EndpointConfig(
			endpoint=endpoint,
			trade_amount_usd=env_config["trade_amount_usd"],
			multiplier=env_config["multiplier"],
			leverage=env_config["leverage"],
			enabled=True,
		)
		db.add(config)
		db.commit()
		db.refresh(config)
	return config


def get_endpoint_position(db: Session, endpoint: str, symbol: str) -> models.EndpointPosition | None:
	"""Endpoint'in belirli bir coin için pozisyonunu al."""
	return db.query(models.EndpointPosition).filter_by(
		endpoint=endpoint,
		symbol=symbol
	).first()


def update_endpoint_position(db: Session, endpoint: str, symbol: str, side: str, qty: float, entry_price: float | None = None):
	"""Endpoint pozisyonunu güncelle veya oluştur."""
	position = get_endpoint_position(db, endpoint, symbol)
	
	if qty == 0:
		# Pozisyon kapatıldı, kaydı sil veya sıfırla
		if position:
			db.delete(position)
		return None
	
	if position:
		# Mevcut pozisyonu güncelle
		position.side = side
		position.qty = qty
		if entry_price:
			position.entry_price = entry_price
	else:
		# Yeni pozisyon oluştur
		position = models.EndpointPosition(
			endpoint=endpoint,
			symbol=symbol,
			side=side,
			qty=qty,
			entry_price=entry_price,
		)
		db.add(position)
	
	return position


@router.post("/tradingview", response_model=schemas.OrderResult)
async def handle_tradingview(
	payload: schemas.TradingViewWebhook,
	request: Request,
	db: Session = Depends(get_db)
):
	"""
	Layer 1 Webhook endpoint: İsteği queue'ya ekler ve hemen 200 OK döner.
	İşleme worker tarafından yapılacak.
	"""
	return await _enqueue_webhook(payload, request, endpoint="layer1")


@router.post("/signal2", response_model=schemas.OrderResult)
async def handle_signal2(
	payload: schemas.TradingViewWebhook,
	request: Request,
	db: Session = Depends(get_db)
):
	"""
	Layer 2 Webhook endpoint: İsteği queue'ya ekler ve hemen 200 OK döner.
	İşleme worker tarafından yapılacak.
	"""
	return await _enqueue_webhook(payload, request, endpoint="layer2")


async def _enqueue_webhook(
	payload: schemas.TradingViewWebhook,
	request: Request,
	endpoint: str = "layer1"
) -> Dict[str, Any]:
	"""Webhook isteğini ilgili endpoint queue'suna ekler."""
	settings = get_settings()
	
	# Fill symbol from ticker if missing
	symbol = payload.symbol or payload.ticker or payload.ticker_upper or payload.tickerid
	if not symbol:
		raise HTTPException(status_code=400, detail="Symbol or ticker required")
	symbol = normalize_tv_symbol(symbol)

	# Get client IP
	client_ip = get_client_ip(request)

	# İlgili queue'yu al
	queue = get_webhook_queue(endpoint)

	# Queue'ya ekle (memory-first, çok hızlı)
	try:
		queue_item = await queue.enqueue(
			payload=payload.dict(),
			client_ip=client_ip,
			symbol=symbol,
			signal=payload.signal,
			price=payload.price
		)
	except Exception as e:
		# Queue'ya ekleme başarısız olsa bile 200 OK dön (TradingView tekrar göndermesin)
		print(f"[Webhook-{endpoint}] Queue'ya ekleme hatası: {e}")
		# Yine de 200 OK dön
		return {
			"success": True,
			"message": f"Webhook alındı ({endpoint}), işleniyor...",
			"order_id": None,
			"response": None,
		}

	# Hemen 200 OK dön (<10ms)
	return {
		"success": True,
		"message": f"Webhook alındı ({endpoint}), queue'ya eklendi, işleniyor...",
		"order_id": None,
		"response": {"queue_id": queue_item.get("queue_id"), "endpoint": endpoint},
	}


async def process_webhook_request(queue_item: Dict[str, Any]) -> Dict[str, Any]:
	"""
	Webhook isteğini işle (worker tarafından çağrılır).
	Returns: {"success": bool, "order_id": str, "response": dict, "error": str}
	"""
	settings = get_settings()
	db = SessionLocal()
	notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
	db_id = queue_item.get("db_id")
	
	def update_webhook_status(status: str):
		"""DB'deki webhook event status'unu güncelle."""
		nonlocal db_id
		if db_id:
			try:
				evt = db.query(models.WebhookEvent).filter_by(id=db_id).first()
				if evt:
					evt.status = status
					db.commit()
			except Exception as e:
				print(f"[Webhook] Status güncelleme hatası: {e}")
	
	try:
		symbol = queue_item["symbol"]
		signal = queue_item["signal"]
		payload_dict = queue_item.get("payload", {})
		price = queue_item.get("price")
		endpoint = queue_item.get("endpoint", "layer1")
		endpoint_label = "Layer1" if endpoint == "layer1" else "Layer2"
		
		# Payload'dan TradingViewWebhook oluştur
		payload = schemas.TradingViewWebhook(**payload_dict)
		
		# DB'de status'u processing yap
		update_webhook_status("processing")
		
		# Broadcast to WS
		if db_id:
			evt = db.query(models.WebhookEvent).filter_by(id=db_id).first()
			if evt:
				await ws_manager.broadcast_json({
					"type": "webhook_event",
					"data": {
						"id": evt.id,
						"endpoint": endpoint,
						"symbol": evt.symbol,
						"signal": evt.signal,
						"price": evt.price,
						"created_at": str(evt.created_at),
					}
				})
		
		# Normalize signal
		signal_upper = signal.upper()
		if signal_upper in ("AL", "BUY", "LONG"):
			side = "BUY"
			new_position_side = "LONG"
		elif signal_upper in ("SAT", "SELL", "SHORT"):
			side = "SELL"
			new_position_side = "SHORT"
		else:
			update_webhook_status("failed")
			return {
				"success": False,
				"error": "Unsupported signal",
				"order_id": None,
				"response": None,
			}
		
		# Endpoint config'i al (DB öncelikli, yoksa .env'den)
		endpoint_config = get_or_create_endpoint_config(db, endpoint)
		
		if not endpoint_config.enabled:
			update_webhook_status("failed")
			return {
				"success": False,
				"error": f"{endpoint_label} endpoint devre dışı",
				"order_id": None,
				"response": None,
			}
		
		# Bu endpoint'in bu coin için mevcut pozisyonunu kontrol et (DB'den)
		db_position = get_endpoint_position(db, endpoint, symbol)
		
		# Aynı yönde pozisyon var mı kontrolü
		if db_position and db_position.qty > 0:
			if db_position.side == new_position_side:
				try:
					skip_msg = [
						f"⛔ İşlem Yapılmadı [{endpoint_label}]: Aynı Yönde Pozisyon İsteği",
						"",
						f"Symbol: {symbol}",
						f"İstek: {side}",
						f"Mevcut DB Pozisyon: {db_position.side} {db_position.qty}",
						"",
						"Aynı yönde açık pozisyon olduğu için yeni işlem açılmadı."
					]
					await notifier.send_message("\n".join(skip_msg))
				except Exception:
					pass
				
				update_webhook_status("completed")
				
				return {
					"success": True,
					"message": f"İşlem yapılmadı: {symbol} üzerinde zaten aynı yönde ({db_position.side}) pozisyon var ({endpoint_label}).",
					"order_id": None,
					"response": {"db_position": {"side": db_position.side, "qty": db_position.qty}},
				}
		
		async with BinanceFuturesClient(
			api_key=settings.binance_api_key,
			api_secret=settings.binance_api_secret,
			base_url=settings.binance_base_url,
		) as client:
			# Exchange info
			try:
				ex_info = await client.exchange_info()
				_log_binance_call(db, "GET", "/fapi/v1/exchangeInfo", client, response_data=ex_info)
			except Exception as e:
				_log_binance_call(db, "GET", "/fapi/v1/exchangeInfo", client, error=str(e))
				update_webhook_status("failed")
				return {
					"success": False,
					"error": f"exchangeInfo hatası: {e}",
					"order_id": None,
					"response": None,
				}
			
			# Position mode
			try:
				pmode = await client.position_mode()
				_log_binance_call(db, "GET", "/fapi/v1/positionSide/dual", client, response_data=pmode)
				dual_mode = bool(pmode.get("dualSidePosition"))
			except Exception as e:
				_log_binance_call(db, "GET", "/fapi/v1/positionSide/dual", client, error=str(e))
				dual_mode = False
			
			# Force One-way if not dry-run
			force_msg = None
			if not settings.dry_run and dual_mode:
				try:
					resp_mode = await client.set_position_mode(dual=False)
					_log_binance_call(db, "POST", "/fapi/v1/positionSide/dual", client, response_data=resp_mode)
					dual_mode = False
					force_msg = "Pozisyon modu One-way olarak ayarlandı."
				except Exception as e:
					_log_binance_call(db, "POST", "/fapi/v1/positionSide/dual", client, error=str(e))
					update_webhook_status("failed")
					return {
						"success": False,
						"error": f"Pozisyon modu One-way'a çekilemedi: {e}",
						"order_id": None,
						"response": None,
					}
			
			# Get balance
			available_balance = 100000.0
			balance_before = 100000.0
			if settings.binance_api_key and settings.binance_api_secret and not settings.dry_run:
				try:
					acct = await client.account_usdt_balances()
					_log_binance_call(db, "GET", "/fapi/v2/balance", client, response_data=acct)
					available_balance = acct.get("available", 100000.0)
					balance_before = available_balance
				except Exception as e:
					_log_binance_call(db, "GET", "/fapi/v2/balance", client, error=str(e))
					update_webhook_status("failed")
					return {
						"success": False,
						"error": f"Balance alınamadı: {e}",
						"order_id": None,
						"response": None,
					}
			
			# Leverage - endpoint config'den al
			leverage = endpoint_config.leverage or settings.default_leverage or 1
			
			# Get price
			try:
				current_price = await client.ticker_price(symbol)
				_log_binance_call(db, "GET", "/fapi/v1/ticker/price", client, response_data={"symbol": symbol, "price": current_price})
				if current_price <= 0:
					raise ValueError("Geçersiz fiyat")
			except Exception as e:
				_log_binance_call(db, "GET", "/fapi/v1/ticker/price", client, error=str(e))
				update_webhook_status("failed")
				return {
					"success": False,
					"error": f"Fiyat bilgisi alınamadı: {e}",
					"order_id": None,
					"response": None,
				}
			
			# Quantity calculation - endpoint config'den al
			filters = get_symbol_filters(ex_info, symbol)
			step = filters["stepSize"] or 0.0001
			
			# Endpoint config'den trade amount ve multiplier al
			trade_amount_usdt = endpoint_config.trade_amount_usd * endpoint_config.multiplier
			
			lev = max(1, int(leverage or 1))
			notional = trade_amount_usdt * lev
			base_qty = notional / current_price
			base_qty = round_step(base_qty, step)
			
			step_str = "{:.8f}".format(step).rstrip('0')
			precision = 0
			if "." in step_str:
				precision = len(step_str.split(".")[1])
			
			formatted_qty = "{:.{p}f}".format(base_qty, p=precision)
			order_qty = float(formatted_qty)
			
			if order_qty <= 0 or order_qty < filters["minQty"]:
				update_webhook_status("failed")
				return {
					"success": False,
					"error": "Hesaplanan quantity minimum lot size'dan küçük",
					"order_id": None,
					"response": None,
				}
			
			# ===== TERS POZİSYON KAPATMA (DB'DEN MİKTAR AL) =====
			closed_position_msg = None
			position_side = None
			if dual_mode:
				position_side = "LONG" if side == "BUY" else "SHORT"
			
		# DB'den bu endpoint'in ters pozisyonunu kontrol et
			opposite_detected = False
			if db_position and db_position.qty > 0:
				# Ters yönde pozisyon var mı?
				if side == "BUY" and db_position.side == "SHORT":
					opposite_detected = True
				elif side == "SELL" and db_position.side == "LONG":
					opposite_detected = True
			
			if opposite_detected:
				close_qty = db_position.qty  # DB'deki miktar
				# Mevcut pozisyonu kapatmak için gereken taraf:
				# SHORT kapatmak için BUY, LONG kapatmak için SELL
				close_side = "BUY" if db_position.side == "SHORT" else "SELL"
				
				if not settings.dry_run:
					try:
						close_resp = await client.place_market_order(
							symbol, 
							close_side, 
							close_qty, 
							position_side=db_position.side if dual_mode else None,
							reduce_only=True
						)
						_log_binance_call(db, "POST", "/fapi/v1/order (close)", client, response_data=close_resp)
						closed_position_msg = f"[{endpoint_label}] Ters pozisyon kapatıldı: {close_side} {close_qty} (eski: {db_position.side})"
						
						# DB'deki pozisyonu sıfırla
						update_endpoint_position(db, endpoint, symbol, "", 0, None)
						db.commit()
					except Exception as e:
						_log_binance_call(db, "POST", "/fapi/v1/order (close)", client, error=str(e))
						# Hata olsa bile devam et, belki Binance'de pozisyon yoktur
				else:
					closed_position_msg = f"[{endpoint_label}][DRY_RUN] Ters pozisyon kapatılacaktı: {close_side} {close_qty} (eski: {db_position.side})"
					# Dry run'da da DB'yi güncelle
					update_endpoint_position(db, endpoint, symbol, "", 0, None)
					db.commit()
			
			# Bracket check
			bracket_warn = None
			try:
				risks = await client.position_risk([symbol])
				_log_binance_call(db, "GET", "/fapi/v2/positionRisk", client, response_data=risks)
				entry = None
				if dual_mode:
					desired_side = "LONG" if side == "BUY" else "SHORT"
					for r in risks:
						if r.get("symbol") == symbol and r.get("positionSide", "BOTH") == desired_side:
							entry = r
							break
				else:
					for r in risks:
						if r.get("symbol") == symbol:
							entry = r
							break
				if entry:
					max_notional = float(entry.get("maxNotionalValue") or 0.0)
					new_notional = order_qty * current_price
					if new_notional > max_notional and max_notional > 0:
						allowed_qty = (max_notional / current_price) if current_price > 0 else 0.0
						allowed_qty = round_step(allowed_qty, step)
						formatted_allowed = "{:.{p}f}".format(allowed_qty, p=precision)
						order_qty = float(formatted_allowed)
						bracket_warn = f"Qty braket ile sınırlandı: maxNotional={max_notional}, price={current_price}, allowed_qty={order_qty}"
			except Exception as e:
				_log_binance_call(db, "GET", "/fapi/v2/positionRisk", client, error=str(e))
			
			if (not settings.dry_run) and order_qty <= 0:
				update_webhook_status("failed")
				return {
					"success": False,
					"error": "Mevcut kaldıraç seviyesinde izin verilen maksimum pozisyon sınırı nedeniyle yeni pozisyon açılamıyor (maxNotional).",
					"order_id": None,
					"response": None,
				}
			
			# Place order
			order_response: Dict[str, Any]
			if settings.dry_run:
				order_response = {
					"dry_run": True,
					"endpoint": endpoint,
					"symbol": symbol,
					"side": side,
					"qty": order_qty,
					"leverage": leverage,
					"price": current_price,
					"position_side": position_side,
					"note": force_msg,
					"available_balance": available_balance,
					"trade_amount_usdt": trade_amount_usdt,
					"closed_position_msg": closed_position_msg,
				}
			else:
				try:
					resp1 = await client.set_leverage(symbol, leverage)
					_log_binance_call(db, "POST", "/fapi/v1/leverage", client, response_data=resp1)
				except Exception as e:
					extra = None
					if isinstance(e, httpx.HTTPStatusError):
						try:
							extra = e.response.json()
						except Exception:
							extra = e.response.text
					err_msg = f"Leverage ayarlanamadı: {e}" + (f" | Binance: {extra}" if extra else "")
					_log_binance_call(db, "POST", "/fapi/v1/leverage", client, error=err_msg)
					update_webhook_status("failed")
					return {
						"success": False,
						"error": err_msg,
						"order_id": None,
						"response": None,
					}
				
				try:
					resp_margin = await client.set_margin_type(symbol, "ISOLATED")
					_log_binance_call(db, "POST", "/fapi/v1/marginType", client, response_data=resp_margin)
				except Exception as e:
					extra = None
					if isinstance(e, httpx.HTTPStatusError):
						try:
							extra = e.response.json()
							if extra.get("code") == -4046:
								pass
							else:
								raise
						except Exception:
							extra = e.response.text
							raise
					else:
						_log_binance_call(db, "POST", "/fapi/v1/marginType", client, error=str(e))
				
				try:
					order_response = await client.place_market_order(symbol, side, order_qty, position_side=position_side)
					_log_binance_call(db, "POST", "/fapi/v1/order", client, response_data=order_response)
				except Exception as e:
					extra = None
					if isinstance(e, httpx.HTTPStatusError):
						try:
							extra = e.response.json()
						except Exception:
							extra = e.response.text
					err_msg = f"Emir başarısız: {e}" + (f" | Binance: {extra}" if extra else "")
					_log_binance_call(db, "POST", "/fapi/v1/order", client, error=err_msg)
					update_webhook_status("failed")
					return {
						"success": False,
						"error": err_msg,
						"order_id": None,
						"response": None,
					}
			
			# ===== DB'DE POZİSYON MİKTARINI GÜNCELLE =====
			update_endpoint_position(db, endpoint, symbol, new_position_side, order_qty, current_price)
			
			# Save order record
			order = models.OrderRecord(
				endpoint=endpoint,
				symbol=symbol,
				side=side,
				position_side=position_side,
				leverage=leverage,
				qty=order_qty,
				price=current_price,
				status=str(order_response.get("status", "NEW")),
				binance_order_id=str(order_response.get("orderId")) if order_response.get("orderId") is not None else None,
				response=order_response,
			)
			db.add(order)
			
			# Balance after
			balance_after = balance_before
			if settings.binance_api_key and settings.binance_api_secret and not settings.dry_run:
				try:
					acct_after = await client.account_usdt_balances()
					_log_binance_call(db, "GET", "/fapi/v2/balance (after)", client, response_data=acct_after)
					balance_after = acct_after.get("available", balance_before)
				except Exception as e:
					_log_binance_call(db, "GET", "/fapi/v2/balance (after)", client, error=str(e))
			
			# Balance snapshot
			margin_used = trade_amount_usdt
			snap = models.BalanceSnapshot(
				total_wallet_balance=balance_after + margin_used,
				available_balance=balance_after,
				used_allocation_usd=margin_used,
				note=f"[{endpoint_label}] Trade: {symbol} {side} qty={order_qty} margin={margin_used:.2f} USDT",
			)
			db.add(snap)
			db.commit()
			
			# ===== BAŞARI KONTROLÜ: Binance Order ID var mı? =====
			binance_order_id = order.binance_order_id
			
			if binance_order_id and binance_order_id != "None":
				# Başarılı - Binance Order ID geldi
				update_webhook_status("completed")
				return {
					"success": True,
					"order_id": binance_order_id,
					"response": order_response,
					"error": None,
				}
			elif settings.dry_run:
				# DRY_RUN modunda Order ID gelmez, yine de başarılı sayılır
				update_webhook_status("completed")
				return {
					"success": True,
					"order_id": None,
					"response": order_response,
					"error": None,
				}
			else:
				# Binance Order ID gelmedi - başarısız
				update_webhook_status("failed")
				return {
					"success": False,
					"order_id": None,
					"response": order_response,
					"error": "Binance Order ID alınamadı",
				}
	finally:
		await notifier.close()
		db.close()
