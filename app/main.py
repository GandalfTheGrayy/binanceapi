from fastapi import FastAPI, Request, WebSocket, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from .database import init_db, SessionLocal
from .routers import webhook
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
from .config import get_settings
from .services.binance_client import BinanceFuturesClient
from .services.telegram import TelegramNotifier
# Telegram polling kaldırıldı - bot sadece mesaj gönderecek
# from .services.telegram_commands import init_command_handler, start_polling_loop, stop_polling_loop
from . import models, schemas
from .state import runtime
from .services.ws_manager import ws_manager
from .services.webhook_worker import webhook_worker_layer1, webhook_worker_layer2
import socket
from .services.order_sizing import get_symbol_filters, round_step
from .services.risk_manager import check_early_losses
import os
import httpx
import asyncio
import websockets
import io
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

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
	# Queue sistemini kullan
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
    """Her saat çalışan bot durumu mesajı (scheduler thread'inde çalışır)"""
    async def _send():
        settings = get_settings()
        notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
        try:
            timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            await notifier.send_message(f"✅ Bot çalışıyor - {timestamp}")
        except Exception as e:
            print(f"[Heartbeat] Mesaj gönderilemedi: {e}")
        finally:
            await notifier.close()
    
    try:
        asyncio.run(_send())
    except Exception as e:
        print(f"[Heartbeat] Job error: {e}")


def hourly_pnl_job():
    """Saatlik PnL raporu (scheduler thread'inde çalışır) - Layer bazlı"""
    
    async def _run_job():
        db = SessionLocal()
        client = None
        notifier = None
        
        try:
            settings = get_settings()
            notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)

            # Try to fetch real balances and positions from Binance
            wallet = 0.0
            available = 0.0
            binance_positions = []
            mark_prices = {}  # symbol -> mark_price
            
            if settings.binance_api_key and settings.binance_api_secret and not settings.dry_run:
                try:
                    async with BinanceFuturesClient(settings.binance_api_key, settings.binance_api_secret, settings.binance_base_url) as client:
                        acct = await client.account_usdt_balances()
                        binance_positions = await client.positions()
                        wallet = acct.get("wallet", 0.0)
                        available = acct.get("available", 0.0)
                        
                        # Mark price'ları kaydet
                        for pos in binance_positions:
                            symbol = pos.get("symbol")
                            mark_price = float(pos.get("markPrice", 0.0))
                            if symbol and mark_price > 0:
                                mark_prices[symbol] = mark_price
                except Exception as e:
                    print(f"[HourlyPnL] Binance veri çekme hatası: {e}")

            # Get previous data for PnL calcs
            last_snap = db.query(models.BalanceSnapshot).order_by(models.BalanceSnapshot.id.desc()).first()
            used = last_snap.used_allocation_usd if last_snap else 0.0
            if wallet == 0.0:
                wallet = last_snap.total_wallet_balance if last_snap else 100000.0
            if available == 0.0:
                available = wallet - used

            first_snap = db.query(models.BalanceSnapshot).order_by(models.BalanceSnapshot.id.asc()).first()
            pnl_total = (wallet - first_snap.total_wallet_balance) if first_snap else 0.0
            pnl_1h = (wallet - last_snap.total_wallet_balance) if last_snap else 0.0

            # ========== LAYER BAZLI POZİSYON ANALİZİ ==========
            layer_data = {"layer1": {"positions": [], "pnl": 0.0, "cost": 0.0}, 
                          "layer2": {"positions": [], "pnl": 0.0, "cost": 0.0}}
            
            for endpoint in ["layer1", "layer2"]:
                endpoint_positions = db.query(models.EndpointPosition).filter_by(endpoint=endpoint).all()
                endpoint_config = db.query(models.EndpointConfig).filter_by(endpoint=endpoint).first()
                leverage = endpoint_config.leverage if endpoint_config else 5
                
                for ep_pos in endpoint_positions:
                    if ep_pos.qty == 0:
                        continue
                    
                    symbol = ep_pos.symbol
                    side = ep_pos.side
                    qty = ep_pos.qty
                    entry_price = ep_pos.entry_price or 0
                    mark_price = mark_prices.get(symbol, entry_price)  # Binance'den mark price
                    
                    # PnL hesapla
                    if side == "LONG":
                        unrealized_pnl = (mark_price - entry_price) * qty
                    else:  # SHORT
                        unrealized_pnl = (entry_price - mark_price) * qty
                    
                    # Maliyet (marjin)
                    cost = (entry_price * qty) / leverage if leverage > 0 else 0
                    roe_pct = (unrealized_pnl / cost) * 100 if cost > 0 else 0.0
                    
                    layer_data[endpoint]["positions"].append({
                        "symbol": symbol,
                        "side": side,
                        "qty": qty,
                        "entry_price": entry_price,
                        "mark_price": mark_price,
                        "pnl": unrealized_pnl,
                        "cost": cost,
                        "roe_pct": roe_pct,
                        "leverage": leverage
                    })
                    layer_data[endpoint]["pnl"] += unrealized_pnl
                    layer_data[endpoint]["cost"] += cost

            # Toplam hesapla
            total_layer_pnl = layer_data["layer1"]["pnl"] + layer_data["layer2"]["pnl"]
            total_layer_cost = layer_data["layer1"]["cost"] + layer_data["layer2"]["cost"]
            
            # ========== MESAJ OLUŞTUR ==========
            msg = (
                f"📊 <b>Saatlik Rapor</b>\n\n"
                f"💵 <b>Wallet:</b> {wallet:.2f} USDT\n"
                f"💰 <b>Available:</b> {available:.2f} USDT\n"
                f"🔒 <b>Used:</b> {used:.2f} USDT\n"
                f"📈 <b>PNL 1h:</b> {pnl_1h:.2f} USDT\n"
                f"📈 <b>PNL Total:</b> {pnl_total:.2f} USDT\n"
            )
            
            # Layer 1 Pozisyonları
            msg += f"\n{'='*30}\n"
            msg += f"🔵 <b>LAYER 1 (/tradingview)</b>\n"
            if layer_data["layer1"]["positions"]:
                for pos in layer_data["layer1"]["positions"]:
                    pnl_emoji = "🟢" if pos["pnl"] >= 0 else "🔴"
                    msg += (
                        f"\n<b>{pos['symbol']}</b> ({pos['side']})\n"
                        f"Giriş: {pos['entry_price']:.4f} | Mark: {pos['mark_price']:.4f}\n"
                        f"Miktar: {pos['qty']:.6f} | Lev: {pos['leverage']}x\n"
                        f"{pnl_emoji} Kâr: ${pos['pnl']:.2f} ({pos['roe_pct']:.1f}%)\n"
                    )
                l1_pnl = layer_data["layer1"]["pnl"]
                l1_emoji = "🟢" if l1_pnl >= 0 else "🔴"
                msg += f"\n{l1_emoji} <b>Layer1 Toplam:</b> ${l1_pnl:.2f}\n"
            else:
                msg += "📭 Açık pozisyon yok\n"
            
            # Layer 2 Pozisyonları
            msg += f"\n{'='*30}\n"
            msg += f"🟣 <b>LAYER 2 (/signal2)</b>\n"
            if layer_data["layer2"]["positions"]:
                for pos in layer_data["layer2"]["positions"]:
                    pnl_emoji = "🟢" if pos["pnl"] >= 0 else "🔴"
                    msg += (
                        f"\n<b>{pos['symbol']}</b> ({pos['side']})\n"
                        f"Giriş: {pos['entry_price']:.4f} | Mark: {pos['mark_price']:.4f}\n"
                        f"Miktar: {pos['qty']:.6f} | Lev: {pos['leverage']}x\n"
                        f"{pnl_emoji} Kâr: ${pos['pnl']:.2f} ({pos['roe_pct']:.1f}%)\n"
                    )
                l2_pnl = layer_data["layer2"]["pnl"]
                l2_emoji = "🟢" if l2_pnl >= 0 else "🔴"
                msg += f"\n{l2_emoji} <b>Layer2 Toplam:</b> ${l2_pnl:.2f}\n"
            else:
                msg += "📭 Açık pozisyon yok\n"
            
            # Genel Toplam
            msg += f"\n{'='*30}\n"
            total_emoji = "🟢" if total_layer_pnl >= 0 else "🔴"
            estimated_balance = wallet + total_layer_pnl
            msg += (
                f"💎 <b>TOPLAM</b>\n"
                f"{total_emoji} <b>Unrealized PnL:</b> ${total_layer_pnl:.2f}\n"
                f"💰 <b>Şu an kapanırsa:</b> {estimated_balance:.2f} USDT\n"
            )

            # ========== SNAPSHOT KAYDET ==========
            # Ana snapshot
            db.add(models.BalanceSnapshot(
                total_wallet_balance=wallet,
                available_balance=available,
                used_allocation_usd=used,
                total_equity=estimated_balance,
                unrealized_pnl=total_layer_pnl,
                note=f"Hourly snapshot {datetime.utcnow().isoformat()}Z",
            ))
            
            # Layer bazlı snapshot'lar
            for endpoint in ["layer1", "layer2"]:
                db.add(models.LayerSnapshot(
                    endpoint=endpoint,
                    unrealized_pnl=layer_data[endpoint]["pnl"],
                    total_cost=layer_data[endpoint]["cost"],
                    position_count=len(layer_data[endpoint]["positions"])
                ))
            
            db.commit()

            # ========== GRAFİKLER OLUŞTUR ==========
            graph_bio = None
            try:
                end_time = datetime.utcnow()
                start_time = end_time - timedelta(hours=24)
                
                # Son 24 saat için layer snapshot'larını çek
                layer1_snaps = db.query(models.LayerSnapshot).filter(
                    models.LayerSnapshot.endpoint == "layer1",
                    models.LayerSnapshot.created_at >= start_time
                ).order_by(models.LayerSnapshot.created_at.asc()).all()
                
                layer2_snaps = db.query(models.LayerSnapshot).filter(
                    models.LayerSnapshot.endpoint == "layer2",
                    models.LayerSnapshot.created_at >= start_time
                ).order_by(models.LayerSnapshot.created_at.asc()).all()
                
                # Ana balance snapshot'larını çek
                balance_snaps = db.query(models.BalanceSnapshot).filter(
                    models.BalanceSnapshot.created_at >= start_time
                ).order_by(models.BalanceSnapshot.created_at.asc()).all()
                
                has_layer_data = len(layer1_snaps) > 1 or len(layer2_snaps) > 1
                has_balance_data = len(balance_snaps) > 1
                
                if has_layer_data or has_balance_data:
                    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
                    fig.suptitle('Son 24 Saat Raporu', fontsize=14, fontweight='bold')
                    
                    # Sol üst: Layer 1 PnL
                    ax1 = axes[0, 0]
                    if len(layer1_snaps) > 1:
                        dates1 = [s.created_at.strftime("%H:%M") for s in layer1_snaps]
                        pnls1 = [s.unrealized_pnl for s in layer1_snaps]
                        colors1 = ['green' if p >= 0 else 'red' for p in pnls1]
                        ax1.bar(range(len(dates1)), pnls1, color=colors1, alpha=0.7)
                        ax1.axhline(y=0, color='gray', linestyle='--', linewidth=0.5)
                        ax1.set_title('Layer 1 PnL', fontweight='bold', color='blue')
                        ax1.set_ylabel('USDT')
                        if len(dates1) > 8:
                            step = len(dates1) // 8
                            ax1.set_xticks(range(0, len(dates1), step))
                            ax1.set_xticklabels(dates1[::step], rotation=45, fontsize=8)
                        else:
                            ax1.set_xticks(range(len(dates1)))
                            ax1.set_xticklabels(dates1, rotation=45, fontsize=8)
                    else:
                        ax1.text(0.5, 0.5, 'Veri yok', ha='center', va='center', transform=ax1.transAxes)
                        ax1.set_title('Layer 1 PnL', fontweight='bold', color='blue')
                    ax1.grid(True, alpha=0.3)
                    
                    # Sağ üst: Layer 2 PnL
                    ax2 = axes[0, 1]
                    if len(layer2_snaps) > 1:
                        dates2 = [s.created_at.strftime("%H:%M") for s in layer2_snaps]
                        pnls2 = [s.unrealized_pnl for s in layer2_snaps]
                        colors2 = ['green' if p >= 0 else 'red' for p in pnls2]
                        ax2.bar(range(len(dates2)), pnls2, color=colors2, alpha=0.7)
                        ax2.axhline(y=0, color='gray', linestyle='--', linewidth=0.5)
                        ax2.set_title('Layer 2 PnL', fontweight='bold', color='purple')
                        ax2.set_ylabel('USDT')
                        if len(dates2) > 8:
                            step = len(dates2) // 8
                            ax2.set_xticks(range(0, len(dates2), step))
                            ax2.set_xticklabels(dates2[::step], rotation=45, fontsize=8)
                        else:
                            ax2.set_xticks(range(len(dates2)))
                            ax2.set_xticklabels(dates2, rotation=45, fontsize=8)
                    else:
                        ax2.text(0.5, 0.5, 'Veri yok', ha='center', va='center', transform=ax2.transAxes)
                        ax2.set_title('Layer 2 PnL', fontweight='bold', color='purple')
                    ax2.grid(True, alpha=0.3)
                    
                    # Sol alt: Toplam Unrealized PnL
                    ax3 = axes[1, 0]
                    if has_balance_data:
                        dates3 = [s.created_at.strftime("%H:%M") for s in balance_snaps]
                        total_pnls = [s.unrealized_pnl if s.unrealized_pnl else 0 for s in balance_snaps]
                        colors3 = ['green' if p >= 0 else 'red' for p in total_pnls]
                        ax3.bar(range(len(dates3)), total_pnls, color=colors3, alpha=0.7)
                        ax3.axhline(y=0, color='gray', linestyle='--', linewidth=0.5)
                        ax3.set_title('Toplam Unrealized PnL', fontweight='bold')
                        ax3.set_ylabel('USDT')
                        if len(dates3) > 8:
                            step = len(dates3) // 8
                            ax3.set_xticks(range(0, len(dates3), step))
                            ax3.set_xticklabels(dates3[::step], rotation=45, fontsize=8)
                        else:
                            ax3.set_xticks(range(len(dates3)))
                            ax3.set_xticklabels(dates3, rotation=45, fontsize=8)
                    else:
                        ax3.text(0.5, 0.5, 'Veri yok', ha='center', va='center', transform=ax3.transAxes)
                        ax3.set_title('Toplam Unrealized PnL', fontweight='bold')
                    ax3.grid(True, alpha=0.3)
                    
                    # Sağ alt: Equity (Şu an kapanırsa)
                    ax4 = axes[1, 1]
                    if has_balance_data:
                        dates4 = [s.created_at.strftime("%H:%M") for s in balance_snaps]
                        equities = [s.total_equity if s.total_equity else s.total_wallet_balance for s in balance_snaps]
                        ax4.plot(range(len(dates4)), equities, marker='o', linestyle='-', color='gold', markersize=4)
                        ax4.fill_between(range(len(dates4)), equities, alpha=0.3, color='gold')
                        ax4.set_title('Tahmini Bakiye (Şu an kapanırsa)', fontweight='bold')
                        ax4.set_ylabel('USDT')
                        if len(dates4) > 8:
                            step = len(dates4) // 8
                            ax4.set_xticks(range(0, len(dates4), step))
                            ax4.set_xticklabels(dates4[::step], rotation=45, fontsize=8)
                        else:
                            ax4.set_xticks(range(len(dates4)))
                            ax4.set_xticklabels(dates4, rotation=45, fontsize=8)
                    else:
                        ax4.text(0.5, 0.5, 'Veri yok', ha='center', va='center', transform=ax4.transAxes)
                        ax4.set_title('Tahmini Bakiye (Şu an kapanırsa)', fontweight='bold')
                    ax4.grid(True, alpha=0.3)
                    
                    plt.tight_layout()
                    
                    graph_bio = io.BytesIO()
                    plt.savefig(graph_bio, format='png', dpi=100)
                    graph_bio.seek(0)
                    plt.close()
                    
            except Exception as e:
                print(f"[HourlyPnL] Grafik oluşturma hatası: {e}")
                import traceback
                traceback.print_exc()

            # ========== TELEGRAM'A GÖNDER ==========
            try:
                if graph_bio:
                    await notifier.send_photo(graph_bio, caption=msg)
                else:
                    await notifier.send_message(msg)
            except Exception as e:
                print(f"[HourlyPnL] Telegram gönderme hatası: {e}")

        except Exception as e:
            print(f"[HourlyPnL] Job error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            if notifier:
                await notifier.close()
            db.close()
    
    try:
        asyncio.run(_run_job())
    except Exception as e:
        print(f"[HourlyPnL] asyncio.run error: {e}")


@app.on_event("startup")
def on_startup():
    init_db()
    # initialize runtime config: try DB first, fallback to .env settings
    settings = get_settings()
    db = SessionLocal()
    try:
        if not runtime.load_from_db(db):
            # DB'de ayar yoksa veya hata olursa .env'den yükle
            print("[Startup] DB'de runtime ayarı bulunamadı, .env'den yükleniyor...")
            runtime.reset_from_settings(settings)
    finally:
        db.close()
    app.state.public_base_url = settings.public_base_url or ""
    
    global scheduler
    scheduler = BackgroundScheduler()
    # Her saat bot durumu mesajı
    scheduler.add_job(heartbeat_job, "interval", hours=1, id="heartbeat_job", replace_existing=True)
    # Saatlik PnL raporu
    scheduler.add_job(hourly_pnl_job, "interval", hours=1, id="hourly_pnl_job", replace_existing=True)
    # Erken zarar kesme kontrolü (Her 15 dk) - Şimdilik pasif
    # scheduler.add_job(check_early_losses, "interval", minutes=15, id="check_early_losses_job", replace_existing=True)
    
    # Telegram polling kaldırıldı - bot sadece mesaj gönderecek
    # if settings.telegram_bot_token and settings.telegram_chat_id:
    #     init_command_handler(settings.telegram_bot_token, settings.telegram_chat_id)
    #     print("[Startup] Telegram komut handler'ı başlatıldı (polling lifespan'da başlayacak)")
    
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
async def on_shutdown():
    global scheduler
    # Webhook worker'ları durdur
    try:
        await webhook_worker_layer1.stop()
        print("[Shutdown] Webhook worker Layer1 durduruldu")
    except Exception as e:
        print(f"[Shutdown] Webhook worker Layer1 durdurma hatası: {e}")
    
    try:
        await webhook_worker_layer2.stop()
        print("[Shutdown] Webhook worker Layer2 durduruldu")
    except Exception as e:
        print(f"[Shutdown] Webhook worker Layer2 durdurma hatası: {e}")
    
    # Telegram polling kaldırıldı - bot sadece mesaj gönderecek
    # await stop_polling_loop()
    if scheduler:
        scheduler.shutdown(wait=False)


# Async startup işlemleri (event loop hazır olduğunda)
@app.on_event("startup")
async def on_startup_async():
    settings = get_settings()
    
    # Webhook worker'ları başlat (Layer1 ve Layer2)
    try:
        await webhook_worker_layer1.start()
        print("[Startup] Webhook worker Layer1 başlatıldı")
    except Exception as e:
        print(f"[Startup] Webhook worker Layer1 başlatma hatası: {e}")
    
    try:
        await webhook_worker_layer2.start()
        print("[Startup] Webhook worker Layer2 başlatıldı")
    except Exception as e:
        print(f"[Startup] Webhook worker Layer2 başlatma hatası: {e}")
    
    # Ensure One-way mode on startup when live
    if settings.binance_api_key and settings.binance_api_secret and not settings.dry_run:
        try:
            async with BinanceFuturesClient(settings.binance_api_key, settings.binance_api_secret, settings.binance_base_url) as client:
                try:
                    pm = await client.position_mode()
                    if bool(pm.get("dualSidePosition")):
                        resp = await client.set_position_mode(dual=False)
                        # Log to DB
                        db = SessionLocal()
                        try:
                            _log_binance_call(db, "POST", "/fapi/v1/positionSide/dual", client, response_data=resp)
                        except Exception:
                            pass
                        finally:
                            db.close()
                        print("[Startup] Position mode One-way olarak ayarlandı")
                except Exception as e:
                    # Log error but do not prevent startup
                    db = SessionLocal()
                    try:
                        _log_binance_call(db, "POST", "/fapi/v1/positionSide/dual", client, error=str(e))
                    except Exception:
                        pass
                    finally:
                        db.close()
                    print(f"[Startup] Position mode check error: {e}")
        except Exception as e:
            print(f"[Startup] Binance client error: {e}")
    
    # Bot başlatıldığında hemen test mesajı gönder
    if settings.telegram_bot_token and settings.telegram_chat_id:
        notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
        try:
            await notifier.send_message("🚀 Bot başlatıldı ve çalışıyor!\n\n📡 Aktif Endpoint'ler:\n- Layer1: /webhook/tradingview\n- Layer2: /webhook/signal2")
            print("[Startup] Telegram başlangıç mesajı gönderildi")
        except Exception as e:
            print(f"[Startup] Telegram test mesajı gönderilemedi: {e}")
        finally:
            await notifier.close()
        
        # İlk raporu gönder (5 saniye bekle, servislerin hazır olmasını sağla)
        async def send_initial_report():
            await asyncio.sleep(5)
            print("[Startup] İlk rapor gönderiliyor...")
            try:
                # hourly_pnl_job'u thread'de çalıştır (senkron fonksiyon)
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    executor.submit(hourly_pnl_job)
                print("[Startup] İlk rapor gönderildi")
            except Exception as e:
                print(f"[Startup] İlk rapor gönderme hatası: {e}")
        
        # Arka planda çalıştır
        asyncio.create_task(send_initial_report())

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
        if settings.binance_api_key and settings.binance_api_secret and not settings.dry_run:
            async with BinanceFuturesClient(settings.binance_api_key, settings.binance_api_secret, settings.binance_base_url) as client:
                try:
                    positions = await client.positions()
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
    # Save to database for persistence
    db = SessionLocal()
    try:
        runtime.save_to_db(db)
    finally:
        db.close()
    return data

# Add admin alias to fix 403/404 issues with frontend
@app.get("/api/admin/runtime")
async def get_admin_runtime_config():
    return runtime.to_dict()

@app.post("/api/admin/runtime")
async def set_admin_runtime_config(payload: dict):
    runtime.set_from_dict(payload)
    # Save to database for persistence
    db = SessionLocal()
    try:
        runtime.save_to_db(db)
    finally:
        db.close()
    return {"success": True, "data": runtime.to_dict()}


# ===== ENDPOINT CONFIG API'leri =====

@app.get("/api/endpoint-config/{endpoint}")
async def get_endpoint_config(endpoint: str):
    """Endpoint config'ini getir (DB öncelikli, yoksa .env'den)"""
    db = SessionLocal()
    try:
        config = db.query(models.EndpointConfig).filter_by(endpoint=endpoint).first()
        if config:
            return {
                "success": True,
                "data": {
                    "endpoint": config.endpoint,
                    "trade_amount_usd": config.trade_amount_usd,
                    "multiplier": config.multiplier,
                    "leverage": config.leverage,
                    "enabled": config.enabled,
                    "created_at": str(config.created_at) if config.created_at else None,
                    "updated_at": str(config.updated_at) if config.updated_at else None,
                }
            }
        else:
            # .env'den varsayılan değerleri döndür
            settings = get_settings()
            env_config = settings.get_endpoint_config(endpoint)
            return {
                "success": True,
                "data": {
                    "endpoint": endpoint,
                    "trade_amount_usd": env_config["trade_amount_usd"],
                    "multiplier": env_config["multiplier"],
                    "leverage": env_config["leverage"],
                    "enabled": True,
                    "source": "env_defaults"
                }
            }
    finally:
        db.close()


@app.post("/api/endpoint-config/{endpoint}")
async def set_endpoint_config(endpoint: str, payload: dict):
    """Endpoint config'ini güncelle veya oluştur"""
    db = SessionLocal()
    try:
        config = db.query(models.EndpointConfig).filter_by(endpoint=endpoint).first()
        if not config:
            # .env'den varsayılan değerlerle oluştur
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
        
        # Payload'dan gelen değerleri güncelle
        if "trade_amount_usd" in payload:
            config.trade_amount_usd = float(payload["trade_amount_usd"])
        if "multiplier" in payload:
            config.multiplier = float(payload["multiplier"])
        if "leverage" in payload:
            config.leverage = int(payload["leverage"])
        if "enabled" in payload:
            config.enabled = bool(payload["enabled"])
        
        db.commit()
        db.refresh(config)
        
        return {
            "success": True,
            "message": f"{endpoint} config güncellendi",
            "data": {
                "endpoint": config.endpoint,
                "trade_amount_usd": config.trade_amount_usd,
                "multiplier": config.multiplier,
                "leverage": config.leverage,
                "enabled": config.enabled,
            }
        }
    finally:
        db.close()


@app.get("/api/endpoint-configs")
async def get_all_endpoint_configs():
    """Tüm endpoint config'lerini getir"""
    db = SessionLocal()
    try:
        configs = db.query(models.EndpointConfig).all()
        settings = get_settings()
        
        # Her iki endpoint için config hazırla
        result = {}
        for ep in ["layer1", "layer2"]:
            config = next((c for c in configs if c.endpoint == ep), None)
            if config:
                result[ep] = {
                    "endpoint": config.endpoint,
                    "trade_amount_usd": config.trade_amount_usd,
                    "multiplier": config.multiplier,
                    "leverage": config.leverage,
                    "enabled": config.enabled,
                    "source": "db"
                }
            else:
                env_config = settings.get_endpoint_config(ep)
                result[ep] = {
                    "endpoint": ep,
                    "trade_amount_usd": env_config["trade_amount_usd"],
                    "multiplier": env_config["multiplier"],
                    "leverage": env_config["leverage"],
                    "enabled": True,
                    "source": "env_defaults"
                }
        
        return {"success": True, "data": result}
    finally:
        db.close()


# ===== ENDPOINT POSITIONS API'leri =====

@app.get("/api/endpoint-positions/{endpoint}")
async def get_endpoint_positions(endpoint: str):
    """Endpoint'e ait tüm pozisyonları getir"""
    db = SessionLocal()
    try:
        positions = db.query(models.EndpointPosition).filter_by(endpoint=endpoint).all()
        return {
            "success": True,
            "data": [
                {
                    "id": p.id,
                    "endpoint": p.endpoint,
                    "symbol": p.symbol,
                    "side": p.side,
                    "qty": p.qty,
                    "entry_price": p.entry_price,
                    "updated_at": str(p.updated_at) if p.updated_at else None,
                }
                for p in positions
            ]
        }
    finally:
        db.close()


@app.get("/api/endpoint-positions")
async def get_all_endpoint_positions():
    """Tüm endpoint pozisyonlarını getir"""
    db = SessionLocal()
    try:
        positions = db.query(models.EndpointPosition).all()
        result = {"layer1": [], "layer2": []}
        for p in positions:
            if p.endpoint in result:
                result[p.endpoint].append({
                    "id": p.id,
                    "symbol": p.symbol,
                    "side": p.side,
                    "qty": p.qty,
                    "entry_price": p.entry_price,
                    "updated_at": str(p.updated_at) if p.updated_at else None,
                })
        return {"success": True, "data": result}
    finally:
        db.close()


@app.delete("/api/endpoint-positions/{endpoint}/{symbol}")
async def delete_endpoint_position(endpoint: str, symbol: str):
    """Belirli bir endpoint pozisyonunu sil (pozisyon sıfırla)"""
    db = SessionLocal()
    try:
        position = db.query(models.EndpointPosition).filter_by(
            endpoint=endpoint,
            symbol=symbol
        ).first()
        if position:
            db.delete(position)
            db.commit()
            return {"success": True, "message": f"{endpoint}/{symbol} pozisyonu silindi"}
        else:
            return {"success": False, "message": "Pozisyon bulunamadı"}
    finally:
        db.close()


@app.delete("/api/endpoint-positions/{endpoint}")
async def delete_all_endpoint_positions(endpoint: str):
    """Endpoint'e ait tüm pozisyonları sil"""
    db = SessionLocal()
    try:
        count = db.query(models.EndpointPosition).filter_by(endpoint=endpoint).delete()
        db.commit()
        return {"success": True, "message": f"{endpoint} için {count} pozisyon silindi"}
    finally:
        db.close()


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
    
    async with BinanceFuturesClient(
        api_key=settings.binance_api_key,
        api_secret=settings.binance_api_secret,
        base_url=settings.binance_base_url
    ) as client:
        try:
            result = await client.test_connectivity()
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
    
    async with BinanceFuturesClient(
        api_key=settings.binance_api_key,
        api_secret=settings.binance_api_secret,
        base_url=settings.binance_base_url
    ) as client:
        try:
            balances = await client.account_usdt_balances()
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
    
    async with BinanceFuturesClient(
        api_key=settings.binance_api_key,
        api_secret=settings.binance_api_secret,
        base_url=settings.binance_base_url
    ) as client:
        try:
            positions = await client.positions()
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
    async with BinanceFuturesClient(
        api_key=settings.binance_api_key,
        api_secret=settings.binance_api_secret,
        base_url=settings.binance_base_url
    ) as client:
        try:
            price = await client.ticker_price(symbol.upper())
            return {"success": True, "symbol": symbol.upper(), "price": price}
        except Exception as e:
            return {"success": False, "error": str(e)}

# Alias: support query-style access as used by some frontends
@app.get("/api/binance/price")
async def get_binance_price_query(symbol: str):
    # Doğrudan bir coroutine döndürmek yerine, sonucunu bekleyip (await) döndürüyoruz
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
        async with BinanceFuturesClient(
            api_key=settings.binance_api_key,
            api_secret=settings.binance_api_secret,
            base_url=settings.binance_base_url
        ) as client:
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
            ex_info = await client.exchange_info()
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
                pm = await client.position_mode()
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
                    resp1 = await client.set_leverage(symbol, leverage)
                    _log_binance_call(db, "POST", "/fapi/v1/leverage", client, response_data=resp1)
                    
                    # Market emri ver (yuvarlanmış qty ile)
                    order_response = await client.place_market_order(symbol, side, qty_rounded, position_side=position_side)
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
    
    notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
    try:
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        result = await notifier.send_message(f"🧪 Test Mesajı - {timestamp}")
        
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
    finally:
        await notifier.close()


@app.get("/api/telegram/test")
async def test_telegram_get():
    """GET ile de test edilebilmesi için"""
    return await test_telegram()

# Catch-all proxy: bilinmeyen yolları Streamlit'e yönlendir (dosyanın sonunda tanımlı)
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def proxy_all(path: str, request: Request):
    # Backend'in açık yolları öncelikle eşleşecektir; geriye kalan her şeyi UI'ya aktar
    return await _proxy_streamlit(path, request)
