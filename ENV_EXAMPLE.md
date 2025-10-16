# .env örneği (bu dosyayı .env olarak kopyalayıp değerleri doldurun)

# Binance Futures Testnet
BINANCE_API_KEY=your_testnet_api_key
BINANCE_API_SECRET=your_testnet_api_secret
BINANCE_BASE_URL=https://testnet.binancefuture.com

# Genel ayarlar
ALLOCATION_PCT=50
DEFAULT_LEVERAGE=5
PER_TRADE_PCT=10
SYMBOLS_WHITELIST=BTCUSDT,ETHUSDT
DRY_RUN=true

# Kaldıraç politikası
# auto: webhook üzerindeki leverage varsa onu, yoksa per_symbol -> default
# webhook: her zaman webhook'taki değeri al
# per_symbol: her sembol için LEVERAGE_PER_SYMBOL haritasını kullan
# default: her zaman DEFAULT_LEVERAGE kullan
LEVERAGE_POLICY=auto
LEVERAGE_PER_SYMBOL=BTCUSDT:7,ETHUSDT:6

# Telegram
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_chat_id

# Uygulama
PORT=8000
ENV=dev
PUBLIC_BASE_URL=https://your-public-hostname-or-ngrok-url
