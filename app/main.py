from fastapi import FastAPI, Request, WebSocket, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from .database import init_db, SessionLocal
from .routers import webhook
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
from .config import get_settings
from .services.binance_client import BinanceFuturesClient
from .services.telegram import TelegramNotifier
from . import models, schemas
from .state import runtime
from .services.ws_manager import ws_manager
import socket
from .services.order_sizing import get_symbol_filters, round_step
import os
import httpx
import asyncio
import websockets

app = FastAPI(title="SerdarBorsa Webhook -> Binance Futures")

# CORS middleware - Frontend'in backend'e erişebilmesi için
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Render'da frontend URL'i otomatik değişebilir
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static and templates
# Not: Streamlit kendi statik dosyalarını "/static" altında sunar; çakışmayı önlemek için
# uygulamanın kendi statiklerini "/assets" altında sunuyoruz.
app.mount("/assets", StaticFiles(directory="static"), name="assets")
templates = Jinja2Templates(directory="templates")

# Routers
app.include_router(webhook.router)

# Quick debug endpoints close to the top to verify live routes
@app.get("/api/ping2")
async def ping2():
	return {"ok": True, "ts": datetime.utcnow().isoformat()}

@app.get("/api/debug/routes2")
async def debug_routes2():
	return [getattr(r, "path", None) for r in app.routes]

scheduler: BackgroundScheduler | None = None


# Küçük yardımcı
def _log_binance_call(db: SessionLocal, method: str, path: str, client: BinanceFuturesClient, response_data=None, error: str | None = None):
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
	try:
		db.add(log)
		db.commit()
	except Exception:
		pass


def _lan_ip() -> str:
	try:
		# More reliable LAN IP detection
		s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
		s.connect(("8.8.8.8", 80))
		ip = s.getsockname()[0]
		s.close()
		return ip
	except Exception:
		return socket.gethostbyname(socket.gethostname())


def resolve_base_urls():
	settings = get_settings()
	lan_ip = _lan_ip()
	return {
		"public_base_url": settings.public_base_url or "",
		"lan_base_url": f"http://{lan_ip}:{settings.port}",
		"local_base_url": f"http://127.0.0.1:{settings.port}",
	}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
	await ws_manager.connect(ws)
	try:
		while True:
			# Keep alive or echo if needed
			await ws.receive_text()
	except Exception:
		pass
	finally:
		ws_manager.disconnect(ws)


def hourly_pnl_job():
	db = SessionLocal()
	try:
		settings = get_settings()
		client = BinanceFuturesClient(settings.binance_api_key, settings.binance_api_secret, settings.binance_base_url)
		notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)

		# Try to fetch real balances, fallback to last snapshot
		wallet = 0.0
		available = 0.0
		try:
			if settings.binance_api_key and settings.binance_api_secret and not settings.dry_run:
				acct = client.account_usdt_balances()
				wallet = acct.get("wallet", 0.0)
				available = acct.get("available", 0.0)
		except Exception:
			pass

		last_snap = db.query(models.BalanceSnapshot).order_by(models.BalanceSnapshot.id.desc()).first()
		used = last_snap.used_allocation_usd if last_snap else 0.0
		if wallet == 0.0:
			wallet = last_snap.total_wallet_balance if last_snap else 100000.0
		if available == 0.0:
			available = wallet - used

		db.add(models.BalanceSnapshot(
			total_wallet_balance=wallet,
			available_balance=available,
			used_allocation_usd=used,
			note=f"Hourly snapshot {datetime.utcnow().isoformat()}Z",
		))
		db.commit()

		# Simple PnL estimate: change of wallet vs first snapshot in DB
		first_snap = db.query(models.BalanceSnapshot).order_by(models.BalanceSnapshot.id.asc()).first()
		pnl_total = (wallet - first_snap.total_wallet_balance) if first_snap else 0.0
		# Hourly pnl approx: diff vs last snapshot
		prev_snap = db.query(models.BalanceSnapshot).order_by(models.BalanceSnapshot.id.desc()).offset(1).first()
		pnl_1h = (wallet - (prev_snap.total_wallet_balance if prev_snap else wallet))

		notifier.send_message(
			f"Saatlik Rapor\nWallet: {wallet:.2f} USDT\nAvailable: {available:.2f} USDT\nUsed: {used:.2f} USDT\nPNL 1h: {pnl_1h:.2f} USDT\nPNL Total: {pnl_total:.2f} USDT"
		)
	finally:
		db.close()


@app.on_event("startup")
def on_startup():
	init_db()
	# initialize runtime config from .env settings and expose public url
	settings = get_settings()
	runtime.reset_from_settings(settings)
	app.state.public_base_url = settings.public_base_url or ""
	# Ensure One-way mode on startup when live
	if settings.binance_api_key and settings.binance_api_secret and not settings.dry_run:
		client = BinanceFuturesClient(settings.binance_api_key, settings.binance_api_secret, settings.binance_base_url)
		try:
			pm = client.position_mode()
			if bool(pm.get("dualSidePosition")):
				resp = client.set_position_mode(dual=False)
				# Log to DB
				db = SessionLocal()
				try:
					_log_binance_call(db, "POST", "/fapi/v1/positionSide/dual", client, response_data=resp)
				except Exception:
					pass
				finally:
					db.close()
		except Exception as e:
			# Log error but do not prevent startup
			db = SessionLocal()
			try:
				_log_binance_call(db, "POST", "/fapi/v1/positionSide/dual", client, error=str(e))
			except Exception:
				pass
			finally:
				db.close()
	global scheduler
	scheduler = BackgroundScheduler()
	scheduler.add_job(hourly_pnl_job, "interval", hours=1, id="hourly_pnl_job", replace_existing=True)
	scheduler.start()

	# Debug: print registered routes and openapi paths on startup
	try:
		paths = [getattr(r, "path", None) for r in app.routes]
		print("[Startup] Registered routes:", paths)
		openapi_paths = list((app.openapi() or {}).get("paths", {}).keys())
		print("[Startup] OpenAPI paths:", openapi_paths)
		print("[Startup] Has /api/binance/price?", "/api/binance/price" in openapi_paths, "/api/binance/price" in paths)
		print("[Startup] Has /api/debug/routes?", "/api/debug/routes" in openapi_paths, "/api/debug/routes" in paths)
		print("[Startup] Has /api/debug/routes2?", "/api/debug/routes2" in openapi_paths, "/api/debug/routes2" in paths)
		print("[Startup] Has /api/ping2?", "/api/ping2" in openapi_paths, "/api/ping2" in paths)
	except Exception as e:
		print("[Startup] Route/OpenAPI debug error:", e)


@app.on_event("shutdown")
def on_shutdown():
	global scheduler
	if scheduler:
		scheduler.shutdown(wait=False)

# Streamlit proxy — tek servis altında UI'yı aynı domain üzerinden sunmak için
STREAMLIT_INTERNAL_URL = os.getenv("STREAMLIT_INTERNAL_URL", "http://127.0.0.1:8501").rstrip("/")

async def _proxy_streamlit(path: str, request: Request) -> Response:
    url = f"{STREAMLIT_INTERNAL_URL}/{path}" if path else STREAMLIT_INTERNAL_URL
    # İstek gövdesini ve header'larını forward et
    body = await request.body()
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.request(request.method, url, content=body, headers=dict(request.headers))
    # Hop-by-hop header'ları çıkar
    excluded = {"transfer-encoding", "content-encoding", "connection"}
    headers = {k: v for k, v in resp.headers.items() if k.lower() not in excluded}
    # Tarayıcı cache'ini agresif şekilde kapat — UI yüklenmesini engelleyebilecek 304/etag davranışını azaltır
    headers["Cache-Control"] = "no-store"
    headers["Pragma"] = "no-cache"
    # Content-Type'ı koru
    media_type = resp.headers.get("content-type")
    return Response(content=resp.content, status_code=resp.status_code, headers=headers, media_type=media_type)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # Ana sayfayı Streamlit'e proxy'le
    return await _proxy_streamlit("", request)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
	db = SessionLocal()
	settings = get_settings()
	positions = []
	urls = resolve_base_urls()
	try:
		webhooks = db.query(models.WebhookEvent).order_by(models.WebhookEvent.id.desc()).limit(50).all()
		orders = db.query(models.OrderRecord).order_by(models.OrderRecord.id.desc()).limit(50).all()
		snap = db.query(models.BalanceSnapshot).order_by(models.BalanceSnapshot.id.desc()).first()
		# Try positions if live
		try:
			if settings.binance_api_key and settings.binance_api_secret and not settings.dry_run:
				client = BinanceFuturesClient(settings.binance_api_key, settings.binance_api_secret, settings.binance_base_url)
				positions = client.positions()
		except Exception:
			positions = []
		return templates.TemplateResponse(
			"dashboard.html",
			{
				"request": request,
				"webhooks": webhooks,
				"orders": orders,
				"snapshot": snap,
				"whitelist": settings.get_symbols_whitelist(),
				"allocation_pct": settings.allocation_pct,
				"positions": positions,
				"local_base_url": urls["local_base_url"],
				"lan_base_url": urls["lan_base_url"],
				"public_base_url": urls["public_base_url"],
				"runtime": runtime.to_dict(),
			},
		)
	finally:
		db.close()

# Catch-all proxy: bilinmeyen yolları Streamlit'e yönlendir
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def proxy_all(path: str, request: Request):
    # Backend'in açık yolları öncelikle eşleşecektir; geriye kalan her şeyi UI'ya aktar
    return await _proxy_streamlit(path, request)


# WebSocket köprüsü: Streamlit'in `/_stcore/stream` kanalını iç porttan dışa aktar
def _ws_internal_url(path: str, query: str = "") -> str:
    base = STREAMLIT_INTERNAL_URL
    if base.startswith("https"):
        scheme = "wss"
    else:
        scheme = "ws"
    url = f"{scheme}://{base.split('://', 1)[1]}/{path.lstrip('/')}"
    if query:
        url += f"?{query}"
    return url


@app.websocket("/_stcore/stream")
async def proxy_streamlit_ws(websocket: WebSocket):
    # İstemci bağlantısını kabul et
    await websocket.accept()
    upstream_url = _ws_internal_url("/_stcore/stream", query=str(websocket.url.query or ""))

    # İstemciden gelen Cookie ve Origin bilgilerini upstream'e ilet
    cookie = websocket.headers.get("cookie")
    origin = None
    host = websocket.headers.get("host")
    # Origin'i public host üzerinden ayarla (Streamlit genelde Origin kontrolü yapmıyor ama güvenli)
    if host:
        # Not: Render tarafında https olabilir; origin zorunlu değil, bu yüzden None bırakmak daha güvenli olabilir
        origin = None

    extra_headers = []
    if cookie:
        extra_headers.append(("Cookie", cookie))

    try:
        async with websockets.connect(upstream_url, extra_headers=extra_headers, origin=origin) as upstream:
            async def client_to_upstream():
                try:
                    while True:
                        msg = await websocket.receive()
                        if msg.get("type") == "websocket.close":
                            try:
                                await upstream.close()
                            except Exception:
                                pass
                            break
                        data_text = msg.get("text")
                        data_bytes = msg.get("bytes")
                        if data_text is not None:
                            await upstream.send(data_text)
                        elif data_bytes is not None:
                            await upstream.send(data_bytes)
                except Exception:
                    # Bağlantı kesildi
                    pass

            async def upstream_to_client():
                try:
                    while True:
                        data = await upstream.recv()
                        if isinstance(data, (bytes, bytearray)):
                            await websocket.send_bytes(data)
                        else:
                            await websocket.send_text(str(data))
                except Exception:
                    # Upstream kapandı
                    pass

            t1 = asyncio.create_task(client_to_upstream())
            t2 = asyncio.create_task(upstream_to_client())
            await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
    except Exception:
        # Upstream bağlantı kurulamadı ise 403 ile kapat
        try:
            await websocket.close(code=1008)
        except Exception:
            pass

@app.get("/api/snapshots")
async def api_snapshots(limit: int = 200):
	db = SessionLocal()
	try:
		rows = (
			db.query(models.BalanceSnapshot)
			.order_by(models.BalanceSnapshot.id.asc())
			.limit(limit)
			.all()
		)
		data = [
			{
				"ts": r.created_at.isoformat() if r.created_at else None,
				"wallet": r.total_wallet_balance,
				"available": r.available_balance,
				"used": r.used_allocation_usd,
			}
			for r in rows
		]
		return JSONResponse(content=data)
	finally:
		db.close()


# Admin API for runtime config
@app.get("/api/admin/runtime")
async def get_runtime_config():
	settings = get_settings()
	# Merge runtime with settings defaults
	data = runtime.to_dict()
	if data.get("default_leverage") is None:
		data["default_leverage"] = settings.default_leverage
	if not data.get("leverage_policy"):
		data["leverage_policy"] = settings.leverage_policy
	if not data.get("leverage_per_symbol"):
		data["leverage_per_symbol"] = settings.leverage_map()
	if data.get("per_trade_pct") is None:
		data["per_trade_pct"] = settings.per_trade_pct
	# allocation_cap_usd may remain None/0 if not set at runtime
	return data


@app.post("/api/admin/runtime")
async def set_runtime_config(payload: dict):
	# Allow updating both leverage and risk runtime fields via one endpoint
	runtime.set_from_dict(payload)
	return {"success": True, "message": "Runtime config updated"}


# Admin API for reset used allocation
@app.post("/api/admin/reset-used")
async def reset_used_allocation():
	"""Used allocation'ı 0'a çeken yeni bir snapshot ekle (admin)."""
	db = SessionLocal()
	try:
		# Mevcut son snapshot'ı referans al
		last_snap = db.query(models.BalanceSnapshot).order_by(models.BalanceSnapshot.id.desc()).first()
		wallet = last_snap.total_wallet_balance if last_snap else 100000.0
		# Yeni snapshot: used=0, available=wallet
		db.add(models.BalanceSnapshot(
			total_wallet_balance=wallet,
			available_balance=wallet,
			used_allocation_usd=0.0,
			note="Admin reset used to 0"
		))
		db.commit()
		return {"ok": True, "message": "Used allocation 0'a çekildi", "wallet": wallet}
	finally:
		db.close()


# Binance Test API endpoints
@app.get("/api/binance/test-connectivity")
async def test_binance_connectivity():
	"""Test Binance API connectivity"""
	settings = get_settings()
	if not settings.binance_api_key or not settings.binance_api_secret:
		return {"success": False, "error": "API key veya secret tanımlanmamış"}
	
	try:
		client = BinanceFuturesClient(
			api_key=settings.binance_api_key,
			api_secret=settings.binance_api_secret,
			base_url=settings.binance_base_url
		)
		result = client.test_connectivity()
		# Log
		db = SessionLocal()
		try:
			_log_binance_call(db, "GET", "/fapi/v1/ping", client, response_data=result)
		finally:
			db.close()
		return {"success": True, "data": result, "message": "Bağlantı başarılı"}
	except Exception as e:
		# Debug bilgilerini ve log'u ekle
		db = SessionLocal()
		try:
			_log_binance_call(db, "GET", "/fapi/v1/ping", client, error=str(e))
		finally:
			db.close()
		debug_info = client.get_last_request_debug() if hasattr(client, 'get_last_request_debug') else None
		return {"success": False, "error": str(e), "debug_info": debug_info}


@app.get("/api/binance/account")
async def get_binance_account():
	"""Get Binance account balance"""
	settings = get_settings()
	if not settings.binance_api_key or not settings.binance_api_secret:
		return {"success": False, "error": "API key veya secret tanımlanmamış"}
	
	try:
		client = BinanceFuturesClient(
			api_key=settings.binance_api_key,
			api_secret=settings.binance_api_secret,
			base_url=settings.binance_base_url
		)
		balances = client.account_usdt_balances()
		# Log
		db = SessionLocal()
		try:
			_log_binance_call(db, "GET", "/fapi/v2/balance", client, response_data=balances)
		finally:
			db.close()
		return {"success": True, "data": balances, "message": "Hesap bilgileri alındı"}
	except Exception as e:
		# Debug bilgilerini ve log'u ekle
		db = SessionLocal()
		try:
			_log_binance_call(db, "GET", "/fapi/v2/balance", client, error=str(e))
		finally:
			db.close()
		debug_info = client.get_last_request_debug() if hasattr(client, 'get_last_request_debug') else None
		return {"success": False, "error": str(e), "debug_info": debug_info}


@app.get("/api/binance/positions")
async def get_binance_positions():
	"""Get Binance positions"""
	settings = get_settings()
	if not settings.binance_api_key or not settings.binance_api_secret:
		return {"success": False, "error": "API key veya secret tanımlanmamış"}
	
	try:
		client = BinanceFuturesClient(
			api_key=settings.binance_api_key,
			api_secret=settings.binance_api_secret,
			base_url=settings.binance_base_url
		)
		positions = client.positions()
		# Log
		db = SessionLocal()
		try:
			_log_binance_call(db, "GET", "/fapi/v2/positionRisk", client, response_data=positions)
		finally:
			db.close()
		return {"success": True, "data": positions, "message": f"{len(positions)} pozisyon bulundu"}
	except Exception as e:
		# Debug bilgilerini ve log'u ekle
		db = SessionLocal()
		try:
			_log_binance_call(db, "GET", "/fapi/v2/positionRisk", client, error=str(e))
		finally:
			db.close()
		debug_info = client.get_last_request_debug() if hasattr(client, 'get_last_request_debug') else None
		return {"success": False, "error": str(e), "debug_info": debug_info}

@app.get("/api/binance/price")
async def get_binance_price(symbol: str):
    """Get latest price for given futures symbol"""
    settings = get_settings()
    client = None
    try:
        client = BinanceFuturesClient(
            api_key=settings.binance_api_key or "",
            api_secret=settings.binance_api_secret or "",
            base_url=settings.binance_base_url
        )
        px = client.ticker_price(symbol)
        # Log
        db = SessionLocal()
        try:
            _log_binance_call(db, "GET", "/fapi/v1/ticker/price", client, response_data={"symbol": symbol, "price": px})
        finally:
            db.close()
        return {"success": True, "symbol": symbol, "price": px}
    except Exception as e:
        # Debug bilgilerini ve log'u ekle
        db = SessionLocal()
        try:
            _log_binance_call(db, "GET", "/fapi/v1/ticker/price", client if client else None, error=str(e))
        finally:
            db.close()
        debug_info = client.get_last_request_debug() if (client and hasattr(client, 'get_last_request_debug')) else None
        return {"success": False, "error": str(e), "debug_info": debug_info}


# Logs API for dashboard
@app.get("/api/logs")
async def api_logs(limit: int = 50):
    db = SessionLocal()
    try:
        rows = (
            db.query(models.BinanceAPILog)
            .order_by(models.BinanceAPILog.id.desc())
            .limit(limit)
            .all()
        )
        data = [
            {
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "method": r.method,
                "path": r.path,
                "status_code": r.status_code,
                "request_params": r.request_params,
                "response": r.response,
                "error": r.error,
            }
            for r in rows
        ]
        return JSONResponse(content=data)
    finally:
        db.close()


@app.get("/api/webhooks")
async def api_webhooks(limit: int = 50):
	"""Son webhook kayıtları (detaylı)"""
	db = SessionLocal()
	try:
		rows = (
			db.query(models.WebhookEvent)
			.order_by(models.WebhookEvent.id.desc())
			.limit(limit)
			.all()
		)
		data = [
			{
				"id": r.id,
				"created_at": r.created_at.isoformat() if r.created_at else None,
				"symbol": r.symbol,
				"signal": r.signal,
				"price": r.price,
				"payload": r.payload,
			}
			for r in rows
		]
		return JSONResponse(content=data)
	finally:
		db.close()


@app.get("/api/orders")
async def api_orders(limit: int = 50):
	"""Son emir kayıtları (detaylı)"""
	db = SessionLocal()
	try:
		rows = (
			db.query(models.OrderRecord)
			.order_by(models.OrderRecord.id.desc())
			.limit(limit)
			.all()
		)
		data = [
			{
				"id": r.id,
				"created_at": r.created_at.isoformat() if r.created_at else None,
				"binance_order_id": r.binance_order_id,
				"symbol": r.symbol,
				"side": r.side,
				"position_side": r.position_side,
				"leverage": r.leverage,
				"qty": r.qty,
				"price": r.price,
				"status": r.status,
				"response": r.response,
			}
			for r in rows
		]
		return JSONResponse(content=data)
	finally:
		db.close()


@app.post("/api/binance/create-order")
async def create_binance_order(payload: schemas.TradingViewWebhook):
	"""Direkt emir oluşturma endpoint'i (webhook dışında)"""
	settings = get_settings()
	if not settings.binance_api_key or not settings.binance_api_secret:
		return {"success": False, "error": "API key veya secret tanımlanmamış"}
	
	db = SessionLocal()
	try:
		client = BinanceFuturesClient(
			api_key=settings.binance_api_key,
			api_secret=settings.binance_api_secret,
			base_url=settings.binance_base_url
		)
		
		# Symbol kontrolü
		symbol = payload.symbol
		if not symbol:
			return {"success": False, "error": "Symbol gerekli"}
		
		# Signal kontrolü ve dönüştürme
		signal = payload.signal.upper()
		if signal in ("AL", "BUY", "LONG"):
			side = "BUY"
		elif signal in ("SAT", "SELL", "SHORT"):
			side = "SELL"
		else:
			return {"success": False, "error": "Desteklenmeyen sinyal"}
		
		# Whitelist kontrolü
		whitelist = settings.get_symbols_whitelist()
		if symbol.upper() not in whitelist:
			return {"success": False, "error": "Symbol izin listesinde değil"}
		
		# Qty ve price kontrolü
		if not payload.qty or payload.qty <= 0:
			return {"success": False, "error": "Geçerli bir miktar (qty) gerekli"}
		
		if not payload.price or payload.price <= 0:
			return {"success": False, "error": "Geçerli bir fiyat gerekli"}
		
		# Exchange info ile lot doğrulaması ve qty yuvarlama
		ex_info = client.exchange_info()
		filters = get_symbol_filters(ex_info, symbol)
		step = float(filters.get("stepSize", 0.0) or 0.0)
		min_qty = float(filters.get("minQty", 0.0) or 0.0)
		qty_rounded = round_step(float(payload.qty), step if step > 0 else 0.0001)
		if qty_rounded < min_qty or qty_rounded <= 0:
			return {"success": False, "error": f"Miktar minQty altında. minQty={min_qty}, stepSize={step}, gelen={payload.qty}"}
		
		# Leverage ayarla
		leverage = payload.leverage or 1
		
		# Hedge modu ise positionSide belirle
		position_side = None
		try:
			pm = client.position_mode()
			if bool(pm.get("dualSidePosition")):
				position_side = "LONG" if side == "BUY" else "SHORT"
		except Exception:
			position_side = None
		
		if settings.dry_run:
			# Dry run modu — Binance'e emir gönderme
			order_response = {
				"dry_run": True,
				"symbol": symbol,
				"side": side,
				"qty": qty_rounded,
				"leverage": leverage,
				"price": payload.price,
				"position_side": position_side,
				"note": "Direkt emir (dry run)"
			}
		else:
			# Gerçek emir
			try:
				# Leverage ayarla
				resp1 = client.set_leverage(symbol, leverage)
				_log_binance_call(db, "POST", "/fapi/v1/leverage", client, response_data=resp1)
				
				# Market emri ver (yuvarlanmış qty ile)
				order_response = client.place_market_order(symbol, side, qty_rounded, position_side=position_side)
				_log_binance_call(db, "POST", "/fapi/v1/order", client, response_data=order_response)
				
				# orderId yoksa uyarı olarak döndür
				if order_response.get("orderId") is None:
					return {"success": False, "error": "Binance response içinde orderId yok; emir yerleşmemiş olabilir", "response": order_response}
			except Exception as e:
				# Hata durumunda log
				_log_binance_call(db, "POST", "/fapi/v1/order", client, error=str(e))
				return {"success": False, "error": f"Emir başarısız: {str(e)}"}
		
		# Emir kaydını veritabanına kaydet
		order = models.OrderRecord(
			symbol=symbol,
			side=side,
			position_side=position_side,
			leverage=leverage,
			qty=qty_rounded,
			price=payload.price,
			status=str(order_response.get("status", "NEW")),
			binance_order_id=str(order_response.get("orderId")) if order_response.get("orderId") is not None else None,
			response=order_response,
		)
		db.add(order)
		db.commit()
		
		return {
			"success": True,
			"message": "Dry-run: emir simüle edildi" if settings.dry_run else "Gerçek emir başarıyla oluşturuldu",
			"order_id": order.binance_order_id,
			"response": order_response
		}
		
	except Exception as e:
		return {"success": False, "error": str(e)}
	finally:
		db.close()


@app.get("/api/debug/routes")
async def debug_routes():
    return [getattr(r, "path", None) for r in app.routes]
