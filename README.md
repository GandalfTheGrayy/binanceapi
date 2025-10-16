# SerdarBorsa - TradingView Webhook -> Binance Futures Testnet

TradingView webhook alarmlarını alır, Binance USDT-M Futures Testnet üzerinde LONG/SHORT işlemleri (dry-run destekli) yönetir; Telegram bildirimleri ve basit dashboard içerir.

## 🚀 Render.com'da Deployment

Bu proje Render.com'da kolayca deploy edilebilir. `render.yaml` dosyası ile otomatik deployment yapılandırması mevcuttur.

### Render'da Deploy Etme Adımları:

1. **GitHub Repository Oluştur**: Projeyi GitHub'a push edin
2. **Render'a Bağlan**: [render.com](https://render.com) hesabınızla GitHub'ı bağlayın
3. **Blueprint Deploy**: "New" > "Blueprint" seçin ve repository'nizi seçin
4. **Environment Variables**: Aşağıdaki değişkenleri Render dashboard'unda ayarlayın:
   - `BINANCE_API_KEY`: Binance API anahtarınız
   - `BINANCE_API_SECRET`: Binance API secret'ınız
   - `TELEGRAM_BOT_TOKEN`: Telegram bot token'ınız (opsiyonel)
   - `TELEGRAM_CHAT_ID`: Telegram chat ID'niz (opsiyonel)

### Render Services:
- **Backend**: FastAPI uygulaması (Port: otomatik)
- **Frontend**: Streamlit dashboard (Port: otomatik)

Deployment sonrası:
- Backend URL: `https://binance-api-backend.onrender.com`
- Frontend URL: `https://binance-api-frontend.onrender.com`

## Kurulum

1. Python 3.10+

https://d9b257a5f04f.ngrok-free.app



```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Ortam değişkenleri

`.env` oluşturun, `ENV_EXAMPLE.md` değerlerini doldurun. Teste başlarken `DRY_RUN=true` bırakın.

3. Çalıştırma

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

- Webhook: `POST /webhook/tradingview`
- Dashboard: `GET /dashboard`

## TradingView Webhook Şablonları

Webhook URL:
```
http://<HOST>:8000/webhook/tradingview
```

Mesaj gövdesi (JSON):
- AL (LONG) sinyali:
```json
{
  "signal": "AL",
  "ticker": "{{ticker}}",
  "price": {{close}},
  "leverage": 7,
  "note": "{{interval}} {{exchange}}"
}
```
- SAT (SHORT) sinyali:
```json
{
  "signal": "SAT",
  "ticker": "{{ticker}}",
  "price": {{close}},
  "leverage": 7,
  "note": "{{interval}} {{exchange}}"
}
```
Açıklamalar:
- `ticker`: TradingView değişkeni (örn. `BINANCE:BTCUSDT.P`). Sistem otomatik olarak `BTCUSDT`'ye normalize eder.
- `price`: Genelde `{{close}}` kullanılır.
- `leverage`: Göndermediğinizde politika ile belirlenir (`LEVERAGE_POLICY`).
- Dilerseniz `symbol` alanını doğrudan `BTCUSDT` olarak gönderebilirsiniz.

Whitelist:
- `.env` içindeki `SYMBOLS_WHITELIST` sembol dışında istekleri reddeder. CSV veya JSON dizi olarak girilebilir.
