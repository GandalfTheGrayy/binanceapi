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
from .services.risk_manager import check_early_losses
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

# Adapter: TradingView POST'ları root ("/") adresine gönderenler için
# Aynı webhook işlevini doğrudan kökte de kabul ediyoruz
@app.post("/", response_model=schemas.OrderResult)
async def root_webhook_adapter(payload: schemas.TradingViewWebhook, request: Request):
	db = SessionLocal()
	try:
		return await webhook.handle_tradingview(payload, request, db)
	finally:
		db.close()

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


def heartbeat_job():
    """Her saat çalışan bot durumu mesajı"""
    try:
        settings = get_settings()
        notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        notifier.send_message(f"✅ Bot çalışıyor - {timestamp}")
    except Exception:
        pass


def hourly_pnl_job():
    db = SessionLocal()
    try:
        settings = get_settings()
        client = BinanceFuturesClient(settings.binance_api_key, settings.binance_api_secret, settings.binance_base_url)
        notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)

        # Asenkron verileri çekmek için yardımcı fonksiyon
        async def fetch_data():
            try:
                balances_task = client.account_usdt_balances()
                positions_task = client.positions()
                return await asyncio.gather(balances_task, positions_task)
            except Exception as e:
                print(f"Data fetch error: {e}")
                return {}, []

        # Try to fetch real balances and positions
        wallet = 0.0
        available = 0.0
        positions = []
        
        if settings.binance_api_key and settings.binance_api_secret and not settings.dry_run:
            try:
                # Run async tasks in sync context
                acct, positions = asyncio.run(fetch_data())
                wallet = acct.get("wallet", 0.0)
                available = acct.get("available", 0.0)
            except Exception as e:
                print(f"Async execution error: {e}")
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

        msg = (
            f"Saatlik Rapor\n"
            f"Wallet: {wallet:.2f} USDT\n"
            f"Available: {available:.2f} USDT\n"
            f"Used: {used:.2f} USDT\n"
            f"PNL 1h: {pnl_1h:.2f} USDT\n"
            f"PNL Total: {pnl_total:.2f} USDT\n"
        )

        # Açık pozisyonları ekle
        if positions:
            msg += "\n📊 Açık Pozisyonlar:\n"
            for pos in positions:
                try:
                    symbol = pos.get("symbol")
                    amt = float(pos.get("positionAmt", 0.0))
                    entry_price = float(pos.get("entryPrice", 0.0))
                    mark_price = float(pos.get("markPrice", 0.0))
                    unrealized_pnl = float(pos.get("unRealizedProfit", 0.0))
                    leverage = float(pos.get("leverage", 1.0))
                    
                    if amt == 0:
                        continue

                    side = "LONG" if amt > 0 else "SHORT"
                    
                    # PnL Yüzdesi (ROE) hesabı
                    # ROE % = unrealized_pnl / initial_margin * 100
                    # initial_margin = (entry_price * abs(amt)) / leverage
                    # ==> ROE % = (unrealized_pnl * leverage) / (entry_price * abs(amt)) * 100
                    
                    initial_margin = (entry_price * abs(amt)) / leverage if leverage > 0 else 0
                    roe_pct = 0.0
                    if initial_margin > 0:
                        roe_pct = (unrealized_pnl / initial_margin) * 100
                    
                    # Pozisyon Maliyeti (Cost)
                    cost = initial_margin

                    msg += (
                        f"{symbol} ({side})\n"
                        f"Giriş: {entry_price} | Mark: {mark_price}\n"
                        f"Miktar: {amt} | Maliyet: ${cost:.2f}\n"
                        f"Kâr: ${unrealized_pnl:.2f} (%{roe_pct:.2f})\n"
                        f"----------------\n"
                    )
                except Exception as e:
                    print(f"Error processing position {pos.get('symbol')}: {e}")
                    continue

        notifier.send_message(msg)
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
    # Her saat bot durumu mesajı
    scheduler.add_job(heartbeat_job, "interval", hours=1, id="heartbeat_job", replace_existing=True)
    # Saatlik PnL raporu
    scheduler.add_job(hourly_pnl_job, "interval", hours=1, id="hourly_pnl_job", replace_existing=True)
    # Erken zarar kesme kontrolü (Her 15 dk) - Şimdilik pasif
    # scheduler.add_job(check_early_losses, "interval", minutes=15, id="check_early_losses_job", replace_existing=True)
    scheduler.start()
    
    # Bot başlatıldığında hemen test mesajı gönder
    try:
        notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
        notifier.send_message("🚀 Bot başlatıldı ve çalışıyor!")
        # İlk raporu da gönder
        hourly_pnl_job()
    except Exception as e:
        print(f"[Startup] Telegram test mesajı gönderilemedi: {e}")

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

# (Moved below) Catch-all proxy route is defined at the end of file


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
    # Subprotocol (ör. 'streamlit') müzakeresini koru
    proto_header = websocket.headers.get("sec-websocket-protocol")
    subprotocols = [p.strip() for p in (proto_header or "").split(",") if p.strip()]
    if subprotocols:
        await websocket.accept(subprotocol=subprotocols[0])
    else:
        await websocket.accept()

    upstream_url = _ws_internal_url("/_stcore/stream", query=str(websocket.url.query or ""))

    # İstemciden gelen Cookie bilgisini upstream'e ilet
    cookie = websocket.headers.get("cookie")
    extra_headers = []
    if cookie:
        extra_headers.append(("Cookie", cookie))

    try:
        async with websockets.connect(
            upstream_url,
            extra_headers=extra_headers,
            subprotocols=subprotocols if subprotocols else None,
            ping_interval=20,
            ping_timeout=20,
        ) as upstream:
            async def client_to_upstream():
                try:
                    while True:
                        msg = await websocket.receive()
                        typ = msg.get("type")
                        if typ == "websocket.disconnect":
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
            done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
            for p in pending:
                try:
                    p.cancel()
                except Exception:
                    pass
    except Exception:
        # Upstream bağlantı kurulamadı ise 403 ile kapat
        try:
            await websocket.close(code=1008)
        except Exception:
            pass


# API Endpoints
@app.get("/api/base-urls")
async def api_base_urls():
    return resolve_base_urls()


@app.get("/api/snapshots")
async def api_snapshots(limit: int = 200):
    db = SessionLocal()
    try:
        snapshots = db.query(models.BalanceSnapshot).order_by(models.BalanceSnapshot.id.desc()).limit(limit).all()
        data = []
        for s in snapshots:
            data.append({
                "id": s.id,
                "created_at": str(s.created_at),
                "total_wallet_balance": s.total_wallet_balance,
                "available_balance": s.available_balance,
                "used_allocation_usd": s.used_allocation_usd,
                "note": s.note,
            })
        return JSONResponse(content=data)
    finally:
        db.close()


@app.get("/api/runtime")
async def get_runtime_config():
    return runtime.to_dict()


@app.post("/api/runtime")
async def set_runtime_config(payload: dict):
    # Allow updating both leverage and risk runtime fields via one endpoint
    runtime.set_from_dict(payload)
    data = runtime.to_dict()
    # Also update the database with the new runtime config if needed
    # (For now, we just keep it in memory)
    return data

# Add admin alias to fix 403/404 issues with frontend
@app.get("/api/admin/runtime")
async def get_admin_runtime_config():
    return runtime.to_dict()

@app.post("/api/admin/runtime")
async def set_admin_runtime_config(payload: dict):
    runtime.set_from_dict(payload)
    return {"success": True, "data": runtime.to_dict()}



@app.post("/api/admin/reset-used")
async def reset_used_allocation():
    """Reset used allocation to 0 for fresh start."""
    db = SessionLocal()
    try:
        # Get the last snapshot
        last_snap = db.query(models.BalanceSnapshot).order_by(models.BalanceSnapshot.id.desc()).first()
        if last_snap:
            # Create a new snapshot with used_allocation_usd = 0
            new_snap = models.BalanceSnapshot(
                total_wallet_balance=last_snap.total_wallet_balance,
                available_balance=last_snap.total_wallet_balance,
                used_allocation_usd=0.0,
                note="Admin reset: used allocation cleared"
            )
            db.add(new_snap)
            db.commit()
            return {"success": True, "message": "Used allocation reset to 0"}
        else:
            return {"success": False, "message": "No snapshots found"}

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
        return {"success": True, "data": positions, "message": "Pozisyon bilgileri alındı"}
    except Exception as e:
        # Debug bilgilerini ve log'u ekle
        db = SessionLocal()
        try:
            _log_binance_call(db, "GET", "/fapi/v2/positionRisk", client, error=str(e))
        finally:
            db.close()
        debug_info = client.get_last_request_debug() if hasattr(client, 'get_last_request_debug') else None
        return {"success": False, "error": str(e), "debug_info": debug_info}


@app.get("/api/binance/price/{symbol}")
async def get_binance_price(symbol: str):
    """Get current price for a symbol"""
    settings = get_settings()
    try:
        client = BinanceFuturesClient(
            api_key=settings.binance_api_key,
            api_secret=settings.binance_api_secret,
            base_url=settings.binance_base_url
        )
        price = client.ticker_price(symbol.upper())
        return {"success": True, "symbol": symbol.upper(), "price": price}
    except Exception as e:
        return {"success": False, "error": str(e)}

# Alias: support query-style access as used by some frontends
@app.get("/api/binance/price")
async def get_binance_price_query(symbol: str):
    return await get_binance_price(symbol)


@app.get("/api/orders")
async def api_orders(limit: int = 50):
    db = SessionLocal()
    try:
        orders = db.query(models.OrderRecord).order_by(models.OrderRecord.id.desc()).limit(limit).all()
        data = [
            {
                "id": o.id,
                "symbol": o.symbol,
                "side": o.side,
                "position_side": o.position_side,
                "leverage": o.leverage,
                "qty": o.qty,
                "price": o.price,
                "status": o.status,
                "binance_order_id": o.binance_order_id,
                "created_at": str(o.created_at),
                "response": o.response,
            }
            for o in orders

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


@app.post("/api/telegram/test")
async def test_telegram():
    """Telegram bağlantısını test et"""
    settings = get_settings()
    
    print(f"[DEBUG] Bot Token: {settings.telegram_bot_token[:20] if settings.telegram_bot_token else 'BOŞ'}...")
    print(f"[DEBUG] Chat ID: {settings.telegram_chat_id}")
    
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return {
            "success": False, 
            "error": "TELEGRAM_BOT_TOKEN veya TELEGRAM_CHAT_ID .env dosyasında tanımlanmamış",
            "bot_token_exists": bool(settings.telegram_bot_token),
            "chat_id_exists": bool(settings.telegram_chat_id),
            "bot_token_length": len(settings.telegram_bot_token) if settings.telegram_bot_token else 0,
            "chat_id_value": settings.telegram_chat_id
        }
    
    try:
        notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        result = notifier.send_message(f"🧪 Test Mesajı - {timestamp}")
        
        if result:
            return {
                "success": True, 
                "message": "Telegram mesajı başarıyla gönderildi! Telegram uygulamanızı kontrol edin.",
                "response": result
            }
        else:
            return {
                "success": False,
                "error": notifier.last_error or "Telegram API yanıt vermedi",
                "last_error": notifier.last_error,
                "bot_token_preview": settings.telegram_bot_token[:20] + "..." if len(settings.telegram_bot_token) > 20 else settings.telegram_bot_token,
                "chat_id": settings.telegram_chat_id
            }
    except Exception as e:
        return {
            "success": False, 
            "error": str(e),
            "exception_type": type(e).__name__,
            "bot_token_preview": settings.telegram_bot_token[:20] + "..." if len(settings.telegram_bot_token) > 20 else settings.telegram_bot_token,
            "chat_id": settings.telegram_chat_id
        }


@app.get("/api/telegram/test")
async def test_telegram_get():
    """GET ile de test edilebilmesi için"""
    return await test_telegram()

# Catch-all proxy: bilinmeyen yolları Streamlit'e yönlendir (dosyanın sonunda tanımlı)
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def proxy_all(path: str, request: Request):
    # Backend'in açık yolları öncelikle eşleşecektir; geriye kalan her şeyi UI'ya aktar
    return await _proxy_streamlit(path, request)
