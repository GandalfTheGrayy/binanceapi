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

	whitelist = settings.get_symbols_whitelist()
	if symbol.upper() not in whitelist:
		raise HTTPException(status_code=400, detail="Symbol not allowed")

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

	# Allocation tracking
	first_snap = db.query(models.BalanceSnapshot).order_by(models.BalanceSnapshot.id.asc()).first()
	last_snapshot = db.query(models.BalanceSnapshot).order_by(models.BalanceSnapshot.id.desc()).first()
	used_alloc = last_snapshot.used_allocation_usd if last_snapshot else 0.0

	# Determine initial wallet
	if not first_snap:
		initial_wallet = 100000.0
		if settings.binance_api_key and settings.binance_api_secret and not settings.dry_run:
			try:
				acct = client.account_usdt_balances()
				_log_binance_call(db, "GET", "/fapi/v2/balance", client, response_data=acct)
				initial_wallet = acct.get("wallet", initial_wallet)
			except Exception as e:
				_log_binance_call(db, "GET", "/fapi/v2/balance", client, error=str(e))
				pass
		# create first snapshot
		snap0 = models.BalanceSnapshot(
			total_wallet_balance=initial_wallet,
			available_balance=initial_wallet,
			used_allocation_usd=0.0,
			note="Initial snapshot",
		)
		db.add(snap0)
		db.commit()
		first_snap = snap0

	initial_wallet = first_snap.total_wallet_balance

	# Kullanıcı tanımlı limit (runtime) öncelikli
	allocation_cap = runtime.allocation_cap_usd if getattr(runtime, 'allocation_cap_usd', 0) else None
	if not allocation_cap:
		allocation_cap = initial_wallet * (settings.allocation_pct / 100.0)

	# Kaldıraç seçimi ve fiyat
	leverage = runtime.select_leverage(settings, symbol, payload.leverage)
	price = payload.price or 0.0
	if price <= 0:
		raise HTTPException(status_code=400, detail="Price is required for sizing in this phase")

	per_trade_pct = getattr(runtime, 'per_trade_pct', None) or settings.per_trade_pct or 10.0

	# Qty belirleme: eğer payload.qty verildiyse onu kullan, yoksa mevcut sizing ile hesapla
	filters = get_symbol_filters(ex_info, symbol)
	step = filters["stepSize"] or 0.0001
	used_direct_qty = False
	try:
		if payload.qty and payload.qty > 0:
			qty = round_step(float(payload.qty), step)
			notional = price * qty
			lev = max(1, int(leverage or 1))
			margin_usd = notional / lev
			used_direct_qty = True
		else:
			qty, margin_usd, notional = compute_quantity(
				symbol=symbol,
				price=price,
				leverage=leverage,
				allocation_cap_usd=allocation_cap,
				used_allocation_usd=used_alloc,
				per_trade_pct=per_trade_pct,
				exchange_info=ex_info,
			)
	except ValueError as e:
		remaining = max(0.0, allocation_cap - used_alloc)
		raise HTTPException(status_code=400, detail=f"{str(e)} (cap={allocation_cap}, used={used_alloc}, remaining={remaining})")

	# Flip sonrası toplam emir miktarı
	order_qty = qty
	flip_offset = 0.0
	try:
		cur_positions = client.positions([symbol])
		for p in cur_positions:
			if p.get("symbol") == symbol:
				amt = float(p.get("positionAmt", 0) or 0)
				if side == "BUY" and amt < 0:
					order_qty = qty + abs(amt)
					flip_offset = abs(amt)
				elif side == "SELL" and amt > 0:
					order_qty = qty + amt
					flip_offset = amt
				break
		# Adım hassasiyetine göre miktarı yuvarla
		order_qty = round_step(order_qty, step)
	except Exception:
		# Pozisyonlar okunamazsa sadece hesaplanan qty ile devam et
		pass

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
			existing_amt = float(entry.get("positionAmt") or 0.0)
			existing_notional_abs = abs(float(entry.get("notional") or 0.0))
			# Kapanış için gereken ofset (flip senaryosu)
			offset_to_close = 0.0
			if side == "BUY" and existing_amt < 0:
				offset_to_close = abs(existing_amt)
			elif side == "SELL" and existing_amt > 0:
				offset_to_close = existing_amt
			base_qty = max(0.0, order_qty - offset_to_close)
			baseline_notional = 0.0 if offset_to_close > 0 else existing_notional_abs
			allowed_new_notional = max(0.0, (max_notional - baseline_notional))
			allowed_base_qty = (allowed_new_notional / price) if price > 0 else 0.0
			capped_base_qty = round_step(min(base_qty, allowed_base_qty), step)
			if capped_base_qty < base_qty:
				order_qty = round_step(capped_base_qty + offset_to_close, step)
				bracket_warn = f"Qty braket ile sınırlandı: maxNotional={max_notional}, mevcut={baseline_notional}, price={price}"
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
			"sizing": ("direct_qty" if used_direct_qty else "auto"),
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

	# Update allocation usage snapshot
	new_used = used_alloc + (notional / max(1, int(leverage or 1)))
	snap = models.BalanceSnapshot(
		total_wallet_balance=first_snap.total_wallet_balance,
		available_balance=first_snap.total_wallet_balance - new_used,
		used_allocation_usd=new_used,
		note=f"Applied margin {notional / max(1, int(leverage or 1))} USDT for {symbol}",
	)
	db.add(snap)
	db.commit()

	# Bildirim
	try:
		msg_lines = [
			f"{symbol} {side} qty={order_qty} lev={leverage}",
			f"price={price} margin={(notional / max(1, int(leverage or 1)))} notional={notional}",
		]
		if position_side:
			msg_lines.append(f"positionSide={position_side}")
		if force_msg:
			msg_lines.append(force_msg)
		if bracket_warn:
			msg_lines.append(f"warning: {bracket_warn}")
		notifier.send_message("\n".join(msg_lines))
	except Exception:
		pass

	return {
		"success": True,
		"message": ("Order executed" + (f" | {bracket_warn}" if bracket_warn else "")),
		"order_id": order.binance_order_id,
		"response": order_response,
	}
