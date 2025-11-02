from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import Dict, Any
import httpx

from .. import schemas, models
from ..database import SessionLocal
from ..config import get_settings
from ..services.binance_client import BinanceFuturesClient
from ..services.order_sizing import compute_quantity, get_symbol_filters, round_step
from ..services.telegram import TelegramNotifier
from ..state import runtime
from ..services.ws_manager import ws_manager
from ..services.symbols import normalize_tv_symbol

router = APIRouter(prefix="/webhook", tags=["webhook"])


def get_db():
	db = SessionLocal()
	try:
		yield db
	finally:
		db.close()


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


@router.post("/tradingview", response_model=schemas.OrderResult)
async def handle_tradingview(
	payload: schemas.TradingViewWebhook,
	db: Session = Depends(get_db)
):
	settings = get_settings()
	client = BinanceFuturesClient(
		api_key=settings.binance_api_key,
		api_secret=settings.binance_api_secret,
		base_url=settings.binance_base_url,
	)
	notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)

	# Fill symbol from ticker if missing
	symbol = payload.symbol or payload.ticker or payload.ticker_upper or payload.tickerid
	if not symbol:
		raise HTTPException(status_code=400, detail="Symbol or ticker required")
	symbol = normalize_tv_symbol(symbol)

	# Record webhook
	evt = models.WebhookEvent(symbol=symbol, signal=payload.signal, price=payload.price, payload=payload.dict())
	db.add(evt)
	db.commit()
	db.refresh(evt)

	# Broadcast to WS
	await ws_manager.broadcast_json({
		"type": "webhook_event",
		"data": {
			"id": evt.id,
			"symbol": evt.symbol,
			"signal": evt.signal,
			"price": evt.price,
			"created_at": str(evt.created_at),
		}
	})

	# Normalize signal
	signal = payload.signal.upper()
	if signal in ("AL", "BUY", "LONG"):
		side = "BUY"
	elif signal in ("SAT", "SELL", "SHORT"):
		side = "SELL"
	else:
		raise HTTPException(status_code=400, detail="Unsupported signal")

	# Exchange info (logla)
	try:
		ex_info = client.exchange_info()
		_log_binance_call(db, "GET", "/fapi/v1/exchangeInfo", client, response_data=ex_info)
	except Exception as e:
		_log_binance_call(db, "GET", "/fapi/v1/exchangeInfo", client, error=str(e))
		raise HTTPException(status_code=400, detail=f"exchangeInfo hatası: {e}")

	# Position mode (hedge vs one-way)
	try:
		pmode = client.position_mode()
		_log_binance_call(db, "GET", "/fapi/v1/positionSide/dual", client, response_data=pmode)
		dual_mode = bool(pmode.get("dualSidePosition"))
	except Exception as e:
		_log_binance_call(db, "GET", "/fapi/v1/positionSide/dual", client, error=str(e))
		dual_mode = False

	# Force One-way if not dry-run
	force_msg = None
	if not settings.dry_run and dual_mode:
		try:
			resp_mode = client.set_position_mode(dual=False)
			_log_binance_call(db, "POST", "/fapi/v1/positionSide/dual", client, response_data=resp_mode)
			dual_mode = False
			force_msg = "Pozisyon modu One-way olarak ayarlandı."
		except Exception as e:
			_log_binance_call(db, "POST", "/fapi/v1/positionSide/dual", client, error=str(e))
			raise HTTPException(status_code=400, detail=f"Pozisyon modu One-way'a çekilemedi: {e}")

	# Get current available balance from Binance
	available_balance = 100000.0  # Default for dry_run
	if settings.binance_api_key and settings.binance_api_secret and not settings.dry_run:
		try:
			acct = client.account_usdt_balances()
			_log_binance_call(db, "GET", "/fapi/v2/balance", client, response_data=acct)
			available_balance = acct.get("available", 100000.0)
		except Exception as e:
			_log_binance_call(db, "GET", "/fapi/v2/balance", client, error=str(e))
			raise HTTPException(status_code=400, detail=f"Balance alınamadı: {e}")

	# Kaldıraç seçimi ve fiyat (Binance'den güncel fiyat çek)
	leverage = runtime.select_leverage(settings, symbol, payload.leverage)
	try:
		price = client.ticker_price(symbol)
		_log_binance_call(db, "GET", "/fapi/v1/ticker/price", client, response_data={"symbol": symbol, "price": price})
		if price <= 0:
			raise ValueError("Geçersiz fiyat")
	except Exception as e:
		_log_binance_call(db, "GET", "/fapi/v1/ticker/price", client, error=str(e))
		raise HTTPException(status_code=400, detail=f"Fiyat bilgisi alınamadı: {e}")

	# Quantity hesaplama: Available balance'ın %10'u
	filters = get_symbol_filters(ex_info, symbol)
	step = filters["stepSize"] or 0.0001
	
	# Available balance'ın %10'u ile işlem yapacağız
	trade_amount_usdt = available_balance * 0.10
	
	# Kaldıraçla notional hesaplama
	lev = max(1, int(leverage or 1))
	notional = trade_amount_usdt * lev
	
	# Base quantity hesaplama
	base_qty = notional / price
	base_qty = round_step(base_qty, step)
	
	if base_qty <= 0 or base_qty < filters["minQty"]:
		raise HTTPException(status_code=400, detail="Hesaplanan quantity minimum lot size'dan küçük")

	# Pozisyon kontrolü: Ters yönde pozisyon varsa 2x quantity
	order_qty = base_qty
	has_opposite_position = False
	
	try:
		cur_positions = client.positions([symbol])
		_log_binance_call(db, "GET", "/fapi/v2/positionRisk", client, response_data=cur_positions)
		
		for p in cur_positions:
			if p.get("symbol") == symbol:
				amt = float(p.get("positionAmt", 0) or 0)
				# BUY isteği + SHORT pozisyon = 2x
				if side == "BUY" and amt < 0:
					has_opposite_position = True
				# SELL isteği + LONG pozisyon = 2x
				elif side == "SELL" and amt > 0:
					has_opposite_position = True
				break
	except Exception as e:
		_log_binance_call(db, "GET", "/fapi/v2/positionRisk", client, error=str(e))
		# Pozisyonlar okunamazsa 1x quantity ile devam et
		pass
	
	# Ters pozisyon varsa 2x, yoksa 1x
	if has_opposite_position:
		order_qty = base_qty * 2
	else:
		order_qty = base_qty
	
	# Adım hassasiyetine göre miktarı yuvarla
	order_qty = round_step(order_qty, step)

	# Kaldıraç braketi (maxNotional) kontrolü: -2027'yi önlemek için miktarı sınırla
	bracket_warn = None
	try:
		risks = client.position_risk([symbol])
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
			new_notional = order_qty * price
			
			# Yeni notional değeri max'ı aşıyorsa sınırla
			if new_notional > max_notional and max_notional > 0:
				allowed_qty = (max_notional / price) if price > 0 else 0.0
				order_qty = round_step(allowed_qty, step)
				bracket_warn = f"Qty braket ile sınırlandı: maxNotional={max_notional}, price={price}, allowed_qty={order_qty}"
	except Exception as e:
		_log_binance_call(db, "GET", "/fapi/v2/positionRisk", client, error=str(e))
		pass

	# Eğer braket nedeniyle yeni pozisyon açılamıyorsa (ve kapanış da yoksa), kullanıcıyı bilgilendir
	if (not settings.dry_run) and order_qty <= 0:
		raise HTTPException(status_code=400, detail="Mevcut kaldıraç seviyesinde izin verilen maksimum pozisyon sınırı nedeniyle yeni pozisyon açılamıyor (maxNotional). İşlem miktarı 0'a düşürüldü.")

	order_response: Dict[str, Any]
	# Hedge modunda uygun positionSide seçimi
	position_side = None
	if dual_mode:
		position_side = "LONG" if side == "BUY" else "SHORT"

	if settings.dry_run:
		order_response = {
			"dry_run": True,
			"symbol": symbol,
			"side": side,
			"qty": order_qty,
			"leverage": leverage,
			"price": price,
			"position_side": position_side,
			"note": force_msg,
			"available_balance": available_balance,
			"trade_amount_usdt": trade_amount_usdt,
			"has_opposite_position": has_opposite_position,
		}
	else:
		try:
			resp1 = client.set_leverage(symbol, leverage)
			_log_binance_call(db, "POST", "/fapi/v1/leverage", client, response_data=resp1)
		except Exception as e:
			# Binance hata gövdesini çıkar
			extra = None
			if isinstance(e, httpx.HTTPStatusError):
				try:
					extra = e.response.json()
				except Exception:
					extra = e.response.text
			err_msg = f"Leverage ayarlanamadı: {e}" + (f" | Binance: {extra}" if extra else "")
			_log_binance_call(db, "POST", "/fapi/v1/leverage", client, error=err_msg)
			raise HTTPException(status_code=400, detail=err_msg)
		
		# Isolated margin ayarla
		try:
			resp_margin = client.set_margin_type(symbol, "ISOLATED")
			_log_binance_call(db, "POST", "/fapi/v1/marginType", client, response_data=resp_margin)
		except Exception as e:
			# Margin type zaten ayarlanmışsa hata verebilir, devam et
			extra = None
			if isinstance(e, httpx.HTTPStatusError):
				try:
					extra = e.response.json()
					# -4046 kodu: No need to change margin type (zaten ISOLATED)
					if extra.get("code") == -4046:
						pass  # Bu normal, devam et
					else:
						raise
				except Exception:
					extra = e.response.text
					raise
			else:
				_log_binance_call(db, "POST", "/fapi/v1/marginType", client, error=str(e))
		
		try:
			order_response = client.place_market_order(symbol, side, order_qty, position_side=position_side)
			_log_binance_call(db, "POST", "/fapi/v1/order", client, response_data=order_response)
		except Exception as e:
			# Binance hata gövdesini çıkar
			extra = None
			if isinstance(e, httpx.HTTPStatusError):
				try:
					extra = e.response.json()
				except Exception:
					extra = e.response.text
			err_msg = f"Emir başarısız: {e}" + (f" | Binance: {extra}" if extra else "")
			_log_binance_call(db, "POST", "/fapi/v1/order", client, error=err_msg)
			raise HTTPException(status_code=400, detail=err_msg)

	# Save order record
	order = models.OrderRecord(
		symbol=symbol,
		side=side,
		position_side=position_side,
		leverage=leverage,
		qty=order_qty,
		price=price,
		status=str(order_response.get("status", "NEW")),
		binance_order_id=str(order_response.get("orderId")) if order_response.get("orderId") is not None else None,
		response=order_response,
	)
	db.add(order)

	# Balance snapshot (yeni mantıkla)
	margin_used = trade_amount_usdt
	snap = models.BalanceSnapshot(
		total_wallet_balance=available_balance + margin_used,  # Yaklaşık toplam
		available_balance=available_balance,
		used_allocation_usd=margin_used,
		note=f"Trade: {symbol} {side} qty={order_qty} margin={margin_used:.2f} USDT (available balance %10)",
	)
	db.add(snap)
	db.commit()

	# Bildirim
	try:
		msg_lines = [
			f"{symbol} {side} qty={order_qty} lev={leverage}",
			f"price={price} margin={trade_amount_usdt:.2f} USDT notional={notional:.2f} USDT",
			f"available_balance={available_balance:.2f} (used %10)",
		]
		if has_opposite_position:
			msg_lines.append("⚠️ Ters pozisyon var - 2x quantity kullanıldı")
		if position_side:
			msg_lines.append(f"positionSide={position_side}")
		if force_msg:
			msg_lines.append(force_msg)
		if bracket_warn:
			msg_lines.append(f"warning: {bracket_warn}")
		msg_lines.append("📊 Margin Type: ISOLATED")
		notifier.send_message("\n".join(msg_lines))
	except Exception:
		pass

	return {
		"success": True,
		"message": ("Order executed" + (f" | {bracket_warn}" if bracket_warn else "")),
		"order_id": order.binance_order_id,
		"response": order_response,
	}
