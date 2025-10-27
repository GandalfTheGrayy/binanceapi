import os
import json
import requests
import streamlit as st
from typing import Dict, Any
import pandas as pd
from dotenv import load_dotenv
import sys
from urllib.parse import parse_qs

# Streamlit config en başta olmalı
st.set_page_config(page_title="Binance Futures Bot", page_icon="📈", layout="wide")

# .env yükle
load_dotenv()

# ============================================================================
# WEBHOOK HANDLER - TradingView webhook'larını handle eder
# ============================================================================

def handle_webhook():
    """TradingView webhook'unu handle et"""
    try:
        # URL parametrelerini kontrol et (yeni API kullan)
        query_params = st.query_params
        
        # Webhook endpoint kontrolü
        if "webhook" in query_params and query_params["webhook"] == "tradingview":
            st.write("🔔 **TradingView Webhook Alındı**")
            
            # Webhook body'sini simüle et (gerçek webhook'ta POST body gelir)
            # Streamlit'te POST body'yi direkt alamayız, bu yüzden query params kullanıyoruz
            webhook_data = {
                "signal": query_params.get("signal", "AL"),
                "symbol": query_params.get("symbol", "BTCUSDT"), 
                "price": float(query_params.get("price", "0")) if query_params.get("price", "0") != "0" else None
            }
            
            st.json(webhook_data)
            
            # Binance order işlemini simüle et
            st.success("✅ Webhook işlendi! (Simülasyon)")
            st.info("💡 Gerçek webhook için POST request kullanın")
            
            # Normal UI'yi gösterme
            st.stop()
            
    except Exception as e:
        st.error(f"Webhook hatası: {e}")
        st.stop()

# Webhook handler'ı çalıştır
handle_webhook()

# Bu uygulama mevcut FastAPI backend'inizi kullanır.
# .env dosyasındaki PORT ve base URL'lere göre otomatik bağlanır.
# Render deployment için BACKEND_URL environment variable'ını kullanır.

DEFAULT_BASE = os.getenv("DEFAULT_BASE", "http://127.0.0.1:8000")
BACKEND_URL = os.getenv("BACKEND_URL", "").strip()
FRONTEND_BACKEND_URL = os.getenv("FRONTEND_BACKEND_URL", "").strip()
PORT = os.getenv("PORT", "8000").strip()
# BACKEND_URL boşsa ya da $PORT placeholder içeriyorsa, container içi 127.0.0.1:<PORT> kullan
if (not BACKEND_URL) or ("$PORT" in BACKEND_URL) or ("localhost:$PORT" in BACKEND_URL):
    BASE_URL = f"http://127.0.0.1:{PORT}"
else:
    BASE_URL = (BACKEND_URL or FRONTEND_BACKEND_URL or DEFAULT_BASE).rstrip("/")

# HTTP timeout to avoid UI freeze on unreachable backend
TIMEOUT = float(os.getenv("FRONTEND_HTTP_TIMEOUT", "5"))

# Global tema ve stil (daha modern görünüm için)
st.markdown(
    """
    <style>
    /* Genel arka plan ve tipografi */
    .stApp { background: linear-gradient(180deg, #0f172a 0%, #111827 100%); color: #e5e7eb; }
    .stMarkdown, .stText, .stCaption { color: #e5e7eb !important; }

    /* Başlık */
    .stApp header { background: transparent; }

    /* Kart benzeri bölümler */
    .block-container { padding-top: 2rem; }
    .stTabs [data-baseweb="tab"] { color: #cbd5e1; }
    .stTabs [data-baseweb="tab"]:hover { color: #fff; }
    .stTabs [aria-selected="true"] { background: #1f2937; color: #fff; border-radius: 8px; }

    /* Metric kartları */
    div[data-testid="stMetricDelta"] svg { fill: #34d399; }
    div[data-testid="stMetric"] { background: #1f2937; border: 1px solid #374151; border-radius: 12px; padding: 16px; }

    /* Dataframe */
    .stDataFrame { background: #0b1220; }

    /* Butonlar */
    div.stButton > button:first-child { background-color: #2563eb; color: white; border: 1px solid #1d4ed8; }
    div.stButton > button:first-child:hover { background-color: #1d4ed8; }

    /* Uyarı kutuları */
    .stAlert { border-radius: 10px; }

    /* Sidebar kartları */
    .sidebar-card { background: #1f2937; border: 1px solid #374151; border-radius: 12px; padding: 12px; margin-bottom: 10px; }
    .status-badge { display: inline-block; padding: 4px 10px; border-radius: 999px; font-weight: 600; font-size: 0.85rem; }
    .status-ok { background: #065f46; color: #d1fae5; border: 1px solid #064e3b; }
    .status-bad { background: #7f1d1d; color: #fee2e2; border: 1px solid #7f1d1d; }
    .status-warn { background: #b45309; color: #ffedd5; border: 1px solid #b45309; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("📈 Binance Futures Bot — Streamlit Arayüz")

# Health check

def backend_alive() -> bool:
    try:
        r = requests.get(f"{BASE_URL}/api/ping2", timeout=min(TIMEOUT, 2.5))
        return r.status_code == 200
    except Exception:
        return False

# Yardımcılar

def get_json(path: str) -> Any:
    try:
        r = requests.get(f"{BASE_URL}{path}", timeout=TIMEOUT)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            # JSON parse edilemedi — ham yanıtı döndür
            return {
                "success": False,
                "status": r.status_code,
                "content_type": r.headers.get("content-type"),
                "raw": (r.text or "")[:1000],
            }
    except Exception as e:
        st.error(f"GET {path} hata: {e}")
        return {"success": False, "error": str(e)}


def post_json(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        r = requests.post(f"{BASE_URL}{path}", json=payload, timeout=TIMEOUT)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {
                "success": False,
                "status": r.status_code,
                "content_type": r.headers.get("content-type"),
                "raw": (r.text or "")[:1000],
            }
    except Exception as e:
        st.error(f"POST {path} hata: {e}")
        return {"success": False, "error": str(e)}

# Sidebar: canlı durum ve hızlı aksiyonlar
with st.sidebar:
    st.header("Durum ve Hızlı Aksiyonlar")
    alive = backend_alive()
    # .env’den oku
    dry_run = os.getenv("DRY_RUN", "false").strip().lower() == "true"
    bbase = os.getenv("BINANCE_BASE_URL", "—")

    st.markdown("<div class='sidebar-card'>", unsafe_allow_html=True)
    st.subheader("Backend")
    st.caption(f"URL: {BASE_URL}")
    st.markdown(
        f"<span class='status-badge {'status-ok' if alive else 'status-bad'}'>{'ONLINE' if alive else 'OFFLINE'}</span>",
        unsafe_allow_html=True,
    )
    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("<div class='sidebar-card'>", unsafe_allow_html=True)
    st.subheader("Çalışma Modu")
    badge_cls = "status-warn" if dry_run else "status-ok"
    badge_txt = "DRY_RUN (Simülasyon)" if dry_run else "Gerçek Mod"
    st.markdown(f"<span class='status-badge {badge_cls}'>{badge_txt}</span>", unsafe_allow_html=True)
    st.caption(f"Binance Base URL: {bbase}")
    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("<div class='sidebar-card'>", unsafe_allow_html=True)
    st.subheader("Hızlı İşlemler")
    cols = st.columns(2)
    with cols[0]:
        if st.button("Ping"):
            st.write(get_json("/api/binance/test-connectivity"))
    with cols[1]:
        if st.button("Bakiye"):
            st.write(get_json("/api/binance/account"))
    if st.button("Pozisyonlar"):
        st.write(get_json("/api/binance/positions"))
    st.markdown("</div>", unsafe_allow_html=True)

# Basit normalizer

def _normalize_value(v: Any):
    try:
        if v is None:
            return ""
        if isinstance(v, set):
            v = list(v)
        if isinstance(v, (dict, list, tuple)):
            return json.dumps(v, ensure_ascii=False)
        return v
    except Exception:
        return str(v)


def to_df(items: Any):
    try:
        if not items:
            return pd.DataFrame()
        if isinstance(items, dict):
            items = [items]
        rows = []
        for row in items:
            if isinstance(row, dict):
                clean = {k: _normalize_value(v) for k, v in row.items()}
                rows.append(clean)
            else:
                rows.append({"value": _normalize_value(row)})
        df = pd.DataFrame(rows)
        # Tüm kolonları tekrar normalize ederek karma tipleri temizle
        for c in df.columns:
            df[c] = df[c].map(_normalize_value)
        return df
    except Exception:
        return pd.DataFrame()

# Sekmeler
sec_dashboard, sec_binance, sec_runtime, sec_webhook, sec_simulator = st.tabs([
    "Dashboard",
    "Binance",
    "Runtime Ayarları", 
    "Webhook Test",
    "Webhook Simülatörü",
])

with sec_dashboard:
    st.subheader("Özet ve Kayıtlar")
    if not backend_alive():
        st.error("Backend erişilemiyor. Lütfen sunucunun çalıştığını ve BASE_URL ayarının doğru olduğunu kontrol edin.")
    else:
        col1, col2, col3 = st.columns(3)
        with col1:
            snaps = get_json("/api/snapshots") or []
            if snaps and isinstance(snaps, list) and len(snaps) > 0:
                last = snaps[-1]
                st.metric("Wallet (USDT)", f"{last.get('total_wallet_balance', 0):.2f}")
                st.metric("Available (USDT)", f"{last.get('available_balance', 0):.2f}")
                st.metric("Used (USDT)", f"{last.get('used_allocation_usd', 0):.2f}")
                # Mini grafik: wallet/available/used zaman serisi
                try:
                    df = pd.DataFrame(snaps)
                    if "created_at" in df.columns:
                        df["created_at"] = pd.to_datetime(df["created_at"])  # zaman ekseni
                        df = df.set_index("created_at")
                        st.line_chart(df[["total_wallet_balance", "available_balance", "used_allocation_usd"]])
                except Exception:
                    pass
            else:
                st.info("Henüz bakiye verisi yok")
        with col2:
            logs = get_json("/api/logs?limit=25") or []
            st.write("Son Binance API çağrıları")
            try:
                df_logs = to_df(logs)
                if "created_at" in df_logs.columns:
                    try:
                        df_logs["created_at"] = pd.to_datetime(df_logs["created_at"])
                    except Exception:
                        pass
                st.dataframe(df_logs, use_container_width=True)
            except Exception:
                st.json(logs)
        with col3:
            webhooks = get_json("/api/webhooks?limit=25") or []
            st.write("Son Webhook Olayları")
            try:
                st.dataframe(to_df(webhooks), use_container_width=True)
            except Exception:
                st.json(webhooks)

with sec_binance:
    st.subheader("Binance Hesap ve Pozisyonlar")
    cols = st.columns(2)
    with cols[0]:
        if st.button("Hesap Bakiyesini Getir"):
            data = get_json("/api/binance/account")
            st.json(data)
    with cols[1]:
        if st.button("Pozisyonları Getir"):
            data = get_json("/api/binance/positions")
            st.json(data)

with sec_runtime:
    st.subheader("Çalışma Zamanı Ayarları")
    current = get_json("/api/admin/runtime") or {}
    st.write("Geçerli değerler:")
    st.json(current)

    with st.form("runtime_form"):
        st.write("Aşağıdan değerleri güncelleyebilirsiniz (boş bırakırsanız değişmez)")
        leverage_policy = st.selectbox(
            "Kaldıraç Politikası",
            ["", "auto", "webhook", "per_symbol", "default"],
            index=0,
            help="Boş: değişiklik yapma"
        )
        default_leverage = st.number_input("Varsayılan Kaldıraç", min_value=1, max_value=125, value=int(current.get("default_leverage", 5)))
        leverage_per_symbol_str = st.text_input("Sembole Göre Kaldıraç (örn. BTCUSDT:7,ETHUSDT:5)")
        allocation_cap_usd = st.number_input("Toplam USD Limit (cap)", min_value=0.0, value=float(current.get("allocation_cap_usd") or 0.0))
        per_trade_pct = st.number_input("Her İşlem %", min_value=0.0, max_value=100.0, value=float(current.get("per_trade_pct", 10.0)))
        submitted = st.form_submit_button("Kaydet")
        if submitted:
            payload = {}
            if leverage_policy:
                payload["leverage_policy"] = leverage_policy
            payload["default_leverage"] = int(default_leverage)
            if leverage_per_symbol_str:
                payload["leverage_per_symbol_str"] = leverage_per_symbol_str
            payload["allocation_cap_usd"] = float(allocation_cap_usd)
            payload["per_trade_pct"] = float(per_trade_pct)
            res = post_json("/api/admin/runtime", payload)
            if res.get("success"):
                st.success("Güncellendi")
            else:
                st.error(res)

# Whitelist'i .env'den oku (CSV veya JSON dizi)
def symbols_whitelist():
    raw = os.getenv("SYMBOLS_WHITELIST", "BTCUSDT,ETHUSDT").strip()
    if not raw:
        return ["BTCUSDT", "ETHUSDT"]
    try:
        loaded = json.loads(raw)
        if isinstance(loaded, list):
            return [str(x).strip().upper() for x in loaded if str(x).strip()]
    except Exception:
        pass
    return [part.strip().upper() for part in raw.split(",") if part.strip()]

with sec_webhook:
    st.subheader("TradingView Webhook Testi")
    with st.form("webhook_form"):
        options = symbols_whitelist()
        default_idx = options.index("BTCUSDT") if "BTCUSDT" in options else 0
        symbol = st.selectbox("Sembol", options, index=default_idx)
        signal = st.selectbox("Sinyal", ["AL", "SAT", "BUY", "SELL", "LONG", "SHORT"], index=0)
        price = st.number_input("Fiyat", min_value=0.0, value=65000.0, step=0.5)
        leverage = st.number_input("Leverage (opsiyonel)", min_value=0, max_value=125, value=0)
        note = st.text_input("Not (opsiyonel)", "Test sinyali")
        submitted = st.form_submit_button("Gönder")
        if submitted:
            payload = {
                "symbol": symbol,
                "signal": signal,
                "price": float(price),
                "note": note,
            }
            if leverage > 0:
                payload["leverage"] = int(leverage)
            res = post_json("/webhook/tradingview", payload)
            st.json(res)

with sec_simulator:
    st.subheader("Webhook Simülatörü")
    st.write("USD miktarı girerek anlık fiyat üzerinden coin miktarını hesapla ve testnette pozisyon aç")
    
    # Test/Gerçek mod seçeneği
    st.markdown("---")
    mode = st.radio(
        "İşlem Modu",
        ["Test (Webhook Simülasyonu)", "Gerçek (Direkt Emir)"],
        index=0,
        key="operation_mode",
        help="Test modu webhook simülasyonu yapar, Gerçek mod direkt Binance'e emir gönderir"
    )
    
    if mode == "Gerçek (Direkt Emir)":
        st.warning("⚠️ GERÇEK MOD: Bu mod gerçek Binance hesabınızda pozisyon açacaktır!")
    
    st.markdown("---")
    
    col1, col2 = st.columns([1, 2])
    
    with col1:
        options = symbols_whitelist()
        default_idx = options.index("BTCUSDT") if "BTCUSDT" in options else 0
        symbol = st.selectbox("Sembol", options, index=default_idx, key="sim_symbol")
        signal = st.selectbox("Sinyal", ["AL (Long)", "SAT (Short)"], index=0, key="sim_signal")
        usd_amount = st.number_input("USD Miktarı", min_value=1.0, value=100.0, step=1.0, key="sim_usd")
        kaldırac = st.number_input("Kaldıraç (opsiyonel)", min_value=0, max_value=125, value=5, key="sim_leverage")
        
        # Anlık fiyatı otomatik çek ve miktarı hesapla (butonsuz)
        price_data = get_json(f"/api/binance/price?symbol={symbol}")
        if price_data and price_data.get("success"):
            current_price = float(price_data["price"])
            coin_qty = usd_amount / current_price if current_price > 0 else 0.0
            st.session_state["current_price"] = current_price
            st.session_state["coin_qty"] = coin_qty
            st.info(f"💰 {usd_amount} USD = {coin_qty:.6f} {symbol.replace('USDT', '')} (Anlık fiyat: ${current_price:.4f})")
        else:
            st.session_state["current_price"] = None
            st.session_state["coin_qty"] = None
            st.warning("Anlık fiyat alınamadı, lütfen backend'i kontrol edin.")
        
        # İşlem butonları
        st.markdown("---")
        
        # Mod seçimine göre buton metni ve endpoint
        is_real_mode = mode == "Gerçek (Direkt Emir)"
        button_text = "🔥 GERÇEK EMİR OLUŞTUR" if is_real_mode else "🚀 Simüle Et"
        
        # Gerçek emir onay kutusu (güvenlik)
        real_confirm = st.checkbox("Gerçek emir göndermeyi onaylıyorum", value=False) if is_real_mode else True
        
        # Buton stillendirme
        if is_real_mode:
            st.markdown(
                """
                <style>
                div.stButton > button:first-child {
                    background-color: #ff4444;
                    color: white;
                    border: 2px solid #cc0000;
                    font-weight: bold;
                }
                div.stButton > button:first-child:hover {
                    background-color: #cc0000;
                    border: 2px solid #990000;
                }
                </style>
                """,
                unsafe_allow_html=True,
            )
        
        if st.button(button_text, key="execute_order", disabled=(is_real_mode and not real_confirm)):
            # Signal'i API format'ına çevir
            api_signal = "AL" if "AL" in signal else "SAT"
            
            current_price = st.session_state.get("current_price")
            coin_qty = st.session_state.get("coin_qty")
            if not current_price or not coin_qty:
                st.error("Fiyat alınamadı veya coin miktarı hesaplanamadı.")
            else:
                payload = {
                    "symbol": symbol,
                    "signal": api_signal,
                    "price": current_price,
                    "qty": coin_qty,
                    "note": f"Simülatör: {usd_amount} USD -> {coin_qty:.6f} coin",
                }
                
                if kaldırac > 0:
                    payload["leverage"] = int(kaldırac)
                
                # Mod seçimine göre endpoint belirleme
                if is_real_mode:
                    endpoint = "/api/binance/create-order"
                    success_msg = "✅ Gerçek emir başarıyla oluşturuldu! Binance hesabınızda pozisyon açıldı."
                    error_prefix = "❌ Gerçek emir hatası:"
                else:
                    endpoint = "/webhook/tradingview"
                    success_msg = "✅ Webhook başarıyla gönderildi! Pozisyon testnette açılmalı."
                    error_prefix = "❌ Webhook hatası:"
                
                # API çağrısı
                with st.spinner("İşlem gönderiliyor..."):
                    res = post_json(endpoint, payload)
                
                if res.get("success"):
                    if is_real_mode:
                        resp_obj = res.get("response") or {}
                        is_dry = bool(resp_obj.get("dry_run"))
                        if is_dry:
                            st.info("ℹ️ DRY_RUN açık: Emir simüle edildi, Binance'e gönderilmedi.")
                            st.json(resp_obj)
                        elif res.get("order_id"):
                            st.success(success_msg)
                            st.info(f"📋 Emir ID: {res.get('order_id')}")
                            st.balloons()
                        else:
                            st.warning("⚠️ Emir başarılı döndü ancak Binance orderId gelmedi. DRY_RUN kapalı olduğundan emin olun ve lot/braket sınırlarını kontrol edin.")
                            st.json(resp_obj)
                    else:
                        st.success(success_msg)
                        st.balloons()
                else:
                    st.error(f"{error_prefix} {res.get('error', 'Bilinmeyen hata')}")

    with col2:
        # Başlık mod seçimine göre değişir
        result_title = "Gerçek Emir Önizleme" if mode == "Gerçek (Direkt Emir)" else "Simülasyon Sonucu"
        st.subheader(result_title)
        
        current_price = st.session_state.get("current_price")
        coin_qty = st.session_state.get("coin_qty")
        if current_price and coin_qty:
            # Özet tablosu
            st.write("**Hesaplama Sonucu:**")
            summary_data = {
                "Parametre": ["Sembol", "USD Miktarı", "Anlık Fiyat", "Hesaplanan Coin Miktarı", "Sinyal", "Kaldıraç"],
                "Değer": [
                    symbol,
                    f"${usd_amount:.2f}",
                    f"${current_price:.4f}",
                    f"{coin_qty:.6f} {symbol.replace('USDT', '')}",
                    signal,
                    f"{kaldırac}x" if kaldırac > 0 else "Varsayılan"
                ]
            }
            
            st.table(pd.DataFrame(summary_data))
            
            # Gönderilecek JSON preview - mod seçimine göre başlık
            payload_title = "**Gönderilecek Emir Payload:**" if mode == "Gerçek (Direkt Emir)" else "**Gönderilecek Webhook Payload:**"
            st.write(payload_title)
            preview_payload = {
                "symbol": symbol,
                "signal": "AL" if "AL" in signal else "SAT",
                "price": current_price,
                "qty": coin_qty,
                "note": f"Simülatör: {usd_amount} USD -> {coin_qty:.6f} coin",
            }
            if kaldırac > 0:
                preview_payload["leverage"] = int(kaldırac)
            
            st.json(preview_payload)
            
            # Gerçek mod uyarısı
            if mode == "Gerçek (Direkt Emir)":
                st.error("⚠️ DİKKAT: Bu emir gerçek Binance hesabınızda işlem yapacaktır!")
        else:
            st.info("Anlık fiyat alınamadı veya henüz hesaplama yapılamadı.")

st.caption(f"Backend: {BASE_URL} | Health: {'OK' if backend_alive() else 'UNREACHABLE'}")

# Webhook URL bilgisi
st.sidebar.markdown("---")
st.sidebar.markdown("### 🔗 Webhook URL")
st.sidebar.code("https://binance-api-app.onrender.com/?webhook=tradingview")
st.sidebar.caption("✅ Ana domain üzerinden webhook")
st.sidebar.markdown("**Test URL örneği:**")
st.sidebar.code("https://binance-api-app.onrender.com/?webhook=tradingview&signal=AL&symbol=BTCUSDT&price=60000")