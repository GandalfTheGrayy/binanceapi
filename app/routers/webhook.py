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


@router.post("/tradingview", response_model=schemas.OrderResult)
async def handle_tradingview(
	payload: schemas.TradingViewWebhook,
	request: Request,
	db: Session = Depends(get_db)
):
	settings = get_settings()
	
	# Telegram notifier (async)
	notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)

	# Fill symbol from ticker if missing
	symbol = payload.symbol or payload.ticker or payload.ticker_upper or payload.tickerid
	if not symbol:
		await notifier.close()
		raise HTTPException(status_code=400, detail="Symbol or ticker required")
	symbol = normalize_tv_symbol(symbol)

	# Get client IP
	client_ip = get_client_ip(request)

	# Record webhook
	evt = models.WebhookEvent(symbol=symbol, signal=payload.signal, price=payload.price, payload=payload.dict())
	db.add(evt)
	db.commit()
	db.refresh(evt)

	# İlk bildirim: Webhook geldi
	try:
		initial_msg = [
			"🔔 Yeni Webhook İsteği",
			f"IP: {client_ip}",
			f"Symbol: {symbol}",
			f"Signal: {payload.signal.upper()}",
		]
		if payload.leverage:
			initial_msg.append(f"Leverage: {payload.leverage}x")
		await notifier.send_message("\n".join(initial_msg))
	except Exception:
		pass

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
		await notifier.close()
		raise HTTPException(status_code=400, detail="Unsupported signal")

	async with BinanceFuturesClient(
		api_key=settings.binance_api_key,
		api_secret=settings.binance_api_secret,
		base_url=settings.binance_base_url,
	) as client:

		# Check for existing position in the same direction
		try:
			# Sadece kontrol amaçlı pozisyon sorgula
			positions_check = await client.positions(symbols=[symbol])
			_log_binance_call(db, "GET", "/fapi/v2/positionRisk", client, response_data=positions_check)
			
			for pos in positions_check:
				if pos.get("symbol") == symbol:
					amt = float(pos.get("positionAmt", 0) or 0)
					should_skip = False
					
					# BUY isteği + LONG pozisyon (amt > 0)
					if side == "BUY" and amt > 0:
						should_skip = True
					# SELL isteği + SHORT pozisyon (amt < 0)
					elif side == "SELL" and amt < 0:
						should_skip = True
					
					if should_skip:
						# Telegram mesajı
						try:
							skip_msg = [
								"⛔ İşlem Yapılmadı: Aynı Yönde Pozisyon İsteği",
								"",
								f"Symbol: {symbol}",
								f"İstek: {side}",
								f"Mevcut Pozisyon: {amt}",
								"",
								"Aynı yönde açık pozisyon olduğu için yeni işlem açılmadı."
							]
							await notifier.send_message("\n".join(skip_msg))
						except Exception:
							pass
						
						return {
							"success": True,
							"message": f"İşlem yapılmadı: {symbol} üzerinde zaten aynı yönde ({'LONG' if amt > 0 else 'SHORT'}) pozisyon var.",
							"order_id": None,
							"response": {"positionAmt": amt}
						}
					break 
		except Exception as e:
			# Hata olursa logla ama akışı bozma, devam et
			_log_binance_call(db, "GET", "/fapi/v2/positionRisk", client, error=str(e))

		# Exchange info (logla)
		try:
			ex_info = await client.exchange_info()
			_log_binance_call(db, "GET", "/fapi/v1/exchangeInfo", client, response_data=ex_info)
		except Exception as e:
			_log_binance_call(db, "GET", "/fapi/v1/exchangeInfo", client, error=str(e))
			raise HTTPException(status_code=400, detail=f"exchangeInfo hatası: {e}")

		# Position mode (hedge vs one-way)
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
				raise HTTPException(status_code=400, detail=f"Pozisyon modu One-way'a çekilemedi: {e}")

		# Get current available balance from Binance (işlem öncesi)
		available_balance = 100000.0  # Default for dry_run
		balance_before = 100000.0
		if settings.binance_api_key and settings.binance_api_secret and not settings.dry_run:
			try:
				acct = await client.account_usdt_balances()
				_log_binance_call(db, "GET", "/fapi/v2/balance", client, response_data=acct)
				available_balance = acct.get("available", 100000.0)
				balance_before = available_balance  # İşlem öncesi bakiye
			except Exception as e:
				_log_binance_call(db, "GET", "/fapi/v2/balance", client, error=str(e))
				raise HTTPException(status_code=400, detail=f"Balance alınamadı: {e}")

		# Kaldıraç seçimi: Her zaman runtime'daki varsayılan (default) kaldıracı kullan
		leverage = int(runtime.default_leverage or settings.default_leverage or 1)
		
		try:
			price = await client.ticker_price(symbol)
			_log_binance_call(db, "GET", "/fapi/v1/ticker/price", client, response_data={"symbol": symbol, "price": price})
			if price <= 0:
				raise ValueError("Geçersiz fiyat")
		except Exception as e:
			_log_binance_call(db, "GET", "/fapi/v1/ticker/price", client, error=str(e))
			raise HTTPException(status_code=400, detail=f"Fiyat bilgisi alınamadı: {e}")

		# Quantity hesaplama: Available balance'ın %10'u veya fixed tutar
		filters = get_symbol_filters(ex_info, symbol)
		step = filters["stepSize"] or 0.0001
		
		# Eğer fixed_trade_amount ayarlıysa onu kullan, yoksa bakiyenin %10'u
		if runtime.fixed_trade_amount and runtime.fixed_trade_amount > 0:
			trade_amount_usdt = float(runtime.fixed_trade_amount)
		else:
			# Available balance'ın %10'u ile işlem yapacağız
			trade_amount_usdt = available_balance * 0.10
		
		# Kaldıraçla notional hesaplama
		lev = max(1, int(leverage or 1))
		notional = trade_amount_usdt * lev
		
		# Base quantity hesaplama
		base_qty = notional / price
		base_qty = round_step(base_qty, step)
		
		# Precision hatasını önlemek için (1.300000001 gibi) tekrar string formatlayıp float'a çevirelim
		# stepSize'ın ondalık basamak sayısını string analiziyle bul (daha güvenli)
		# "0.00100000" -> "0.001" -> precision=3
		step_str = "{:.8f}".format(step).rstrip('0')
		precision = 0
		if "." in step_str:
			precision = len(step_str.split(".")[1])
		
		# round_step zaten matematiksel yuvarlıyor ama float point hatası kalabiliyor.
		# Tekrar string format ile "temizle"
		formatted_qty = "{:.{p}f}".format(base_qty, p=precision)
		order_qty = float(formatted_qty)
		
		if order_qty <= 0 or order_qty < filters["minQty"]:
			raise HTTPException(status_code=400, detail="Hesaplanan quantity minimum lot size'dan küçük")

		# Pozisyon kontrolü: Ters yönde pozisyon varsa önce onu kapat
		closed_position_amount = 0.0
		closed_position_msg = None

		try:
			cur_positions = await client.positions([symbol])
			_log_binance_call(db, "GET", "/fapi/v2/positionRisk", client, response_data=cur_positions)
			
			for p in cur_positions:
				if p.get("symbol") == symbol:
					amt = float(p.get("positionAmt", 0) or 0)
					
					# Eğer ters yönde bir pozisyon varsa kapat
					opposite_side = None
					if side == "BUY" and amt < 0:
						opposite_side = "BUY"  # Short'u kapatmak için BUY yapılır
					elif side == "SELL" and amt > 0:
						opposite_side = "SELL" # Long'u kapatmak için SELL yapılır
						
					if opposite_side:
						close_qty = abs(amt)
						closed_position_amount = close_qty
						
						if not settings.dry_run:
							try:
								# Mevcut pozisyonu kapat (reduce_only=True)
								close_resp = await client.place_market_order(
									symbol, 
									opposite_side, 
									close_qty, 
									position_side=position_side if dual_mode else None,
									reduce_only=True
								)
								_log_binance_call(db, "POST", "/fapi/v1/order (close)", client, response_data=close_resp)
								closed_position_msg = f"Ters pozisyon kapatıldı: {opposite_side} {close_qty}"
								
								# Telegram bildirimi (Kapanış)
								try:
									await notifier.send_message(f"⚠️ Ters Pozisyon Kapatılıyor\nSymbol: {symbol}\nİşlem: {opposite_side}\nMiktar: {close_qty}\nSebep: Yeni {side} sinyali geldi.")
								except:
									pass
									
							except Exception as e:
								_log_binance_call(db, "POST", "/fapi/v1/order (close)", client, error=str(e))
								# Kapanış hatası olsa bile devam etmeye çalışabiliriz veya hata fırlatabiliriz
								# Şimdilik loglayıp devam ediyoruz, ama riskli olabilir.
								await notifier.send_message(f"❌ Ters Pozisyon Kapatılamadı: {e}")
						else:
							closed_position_msg = f"[DRY_RUN] Ters pozisyon kapatılacaktı: {opposite_side} {close_qty}"

					break
		except Exception as e:
			_log_binance_call(db, "GET", "/fapi/v2/positionRisk", client, error=str(e))
			pass

		# Kaldıraç braketi (maxNotional) kontrolü: -2027'yi önlemek için miktarı sınırla
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
				new_notional = order_qty * price
				
				# Yeni notional değeri max'ı aşıyorsa sınırla
				if new_notional > max_notional and max_notional > 0:
					allowed_qty = (max_notional / price) if price > 0 else 0.0
					# Tekrar precision uygula
					allowed_qty = round_step(allowed_qty, step)
					formatted_allowed = "{:.{p}f}".format(allowed_qty, p=precision)
					order_qty = float(formatted_allowed)
					
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
				"closed_position_msg": closed_position_msg,
			}
		else:
			try:
				resp1 = await client.set_leverage(symbol, leverage)
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
				
				# Hata durumunda Telegram bildirimi
				try:
					import json
					error_lines = [
						"❌ Leverage Ayarlanamadı",
						"",
						f"Symbol: {symbol}",
						f"Target Leverage: {leverage}",
						"",
						"⚠️ Hata Detayı:",
						f"{str(e)}",
						"",
						"📝 Binance Response (Error):",
					]
					if extra:
						try:
							formatted_extra = json.dumps(extra, indent=2)
							error_lines.append(f"<pre>{formatted_extra}</pre>")
						except:
							error_lines.append(str(extra))
					await notifier.send_message("\n".join(error_lines))
				except:
					pass

				raise HTTPException(status_code=400, detail=err_msg)
			
			# Isolated margin ayarla
			try:
				resp_margin = await client.set_margin_type(symbol, "ISOLATED")
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
				order_response = await client.place_market_order(symbol, side, order_qty, position_side=position_side)
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
				
				# Hata durumunda Telegram bildirimi
				try:
					import json
					error_lines = [
						"❌ İşlem Başarısız",
						"",
						f"Symbol: {symbol}",
						f"Side: {side}",
						f"Quantity: {order_qty}",
						"",
						"⚠️ Hata Detayı:",
						f"{str(e)}",
						"",
						"📝 Binance Response (Error):",
					]
					
					if extra:
						try:
							formatted_extra = json.dumps(extra, indent=2)
							error_lines.append(f"<pre>{formatted_extra}</pre>")
						except:
							error_lines.append(str(extra))
					
					await notifier.send_message("\n".join(error_lines))
				except:
					pass
					
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

		# İşlem sonrası bakiyeyi çek
		balance_after = balance_before  # Default
		if settings.binance_api_key and settings.binance_api_secret and not settings.dry_run:
			try:
				acct_after = await client.account_usdt_balances()
				_log_binance_call(db, "GET", "/fapi/v2/balance (after)", client, response_data=acct_after)
				balance_after = acct_after.get("available", balance_before)
			except Exception as e:
				_log_binance_call(db, "GET", "/fapi/v2/balance (after)", client, error=str(e))
				# Hata durumunda balance_before'u kullan
				pass

	# Balance snapshot (yeni mantıkla)
	margin_used = trade_amount_usdt
	snap = models.BalanceSnapshot(
		total_wallet_balance=balance_after + margin_used,  # Yaklaşık toplam
		available_balance=balance_after,
		used_allocation_usd=margin_used,
		note=f"Trade: {symbol} {side} qty={order_qty} margin={margin_used:.2f} USDT (available balance %10)",
	)
	db.add(snap)
	db.commit()

	# Detaylı bildirim: İşlem tamamlandı
	try:
		msg_lines = [
			"✅ İşlem Tamamlandı",
			"",
			"📊 İşlem Detayları:",
			f"Symbol: {symbol}",
			f"Side: {side}",
			f"Quantity: {order_qty}",
			f"Price: {price:.2f} USDT",
			f"Leverage: {leverage}x",
			f"Margin: {trade_amount_usdt:.2f} USDT",
			f"Notional: {notional:.2f} USDT",
			"",
			"💰 Bakiye Değişimi:",
			f"Önceki: {balance_before:.2f} USDT (available)",
			f"Sonraki: {balance_after:.2f} USDT (available)",
			f"Kullanılan: {margin_used:.2f} USDT",
		]
		
		if closed_position_msg:
			msg_lines.append("")
			msg_lines.append(f"⚠️ {closed_position_msg}")
		
		msg_lines.append("")
		msg_lines.append("📊 Margin Type: ISOLATED")
		
		if order.binance_order_id:
			msg_lines.append(f"Order ID: {order.binance_order_id}")
		
		if position_side:
			msg_lines.append(f"Position Side: {position_side}")
		
		if force_msg:
			msg_lines.append(f"ℹ️ {force_msg}")
		
		if bracket_warn:
			msg_lines.append(f"⚠️ {bracket_warn}")
		
		# Add raw Binance response
		msg_lines.append("")
		msg_lines.append("📝 Binance Response:")
		import json
		try:
			# Pretty print JSON
			formatted_response = json.dumps(order_response, indent=2)
			msg_lines.append(f"<pre>{formatted_response}</pre>")
		except Exception:
			msg_lines.append(str(order_response))
		
		await notifier.send_message("\n".join(msg_lines))
	except Exception:
		pass

	# Notifier'ı kapat (file descriptor leak önlemi)
	await notifier.close()

	return {
		"success": True,
		"message": ("Order executed" + (f" | {bracket_warn}" if bracket_warn else "")),
		"order_id": order.binance_order_id,
		"response": order_response,
	}
