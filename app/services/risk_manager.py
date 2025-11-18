from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app import models
from app.config import get_settings
from app.services.binance_client import BinanceFuturesClient
from app.services.telegram import TelegramNotifier
import logging

# Logging setup
logger = logging.getLogger(__name__)

def check_early_losses():
    """
    Checks open positions every 15 minutes.
    If a position is opened within the last 60 minutes:
    - Checks for profit/loss status.
    - If in loss: Closes the position completely (market order).
    - Sends a detailed report to Telegram for ALL checked positions (profit or loss).
    """
    settings = get_settings()
    
    # Check if API keys are set
    if not settings.binance_api_key or not settings.binance_api_secret:
        logger.warning("Binance API keys not set. Skipping risk check.")
        return

    client = BinanceFuturesClient(
        settings.binance_api_key, 
        settings.binance_api_secret, 
        settings.binance_base_url
    )
    notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
    
    db: Session = SessionLocal()
    try:
        # 1. Get all open positions
        positions = client.positions() 
        
        if not positions:
            return

        current_time = datetime.utcnow()
        report_lines = []
        
        for pos in positions:
            symbol = pos.get("symbol")
            position_amt = float(pos.get("positionAmt", 0.0))
            entry_price = float(pos.get("entryPrice", 0.0))
            mark_price = float(pos.get("markPrice", 0.0))
            
            if position_amt == 0:
                continue
                
            # Determine side
            side = "LONG" if position_amt > 0 else "SHORT"
            
            # 2. Find the latest order for this symbol in DB to determine "open time"
            last_order = db.query(models.OrderRecord)\
                .filter(models.OrderRecord.symbol == symbol)\
                .order_by(models.OrderRecord.created_at.desc())\
                .first()
            
            if not last_order:
                continue
            
            # 3. Time Check: Is it within 1 hour?
            order_time = last_order.created_at
            if order_time.tzinfo is None:
                order_time = order_time.replace(tzinfo=None)
            
            # current_time is naive UTC
            if order_time.tzinfo:
                order_time = order_time.replace(tzinfo=None)

            time_diff = current_time - order_time
            minutes_passed = int(time_diff.total_seconds() // 60)
            
            # If older than 60 minutes, skip from this specific check/report
            if time_diff > timedelta(minutes=60):
                continue
            
            # 4. Price Check & Action Logic
            is_in_loss = False
            if side == "LONG":
                if mark_price < entry_price:
                    is_in_loss = True
            else: # SHORT
                if mark_price > entry_price:
                    is_in_loss = True
            
            status_str = "ZARAR" if is_in_loss else "KÂR"
            action_str = "DEVAM 🟢"
            
            if is_in_loss:
                # 5. Close Position
                close_side = "SELL" if side == "LONG" else "BUY"
                close_qty = abs(position_amt)
                
                logger.info(f"Closing losing position for {symbol}. Entry: {entry_price}, Mark: {mark_price}, TimeDiff: {minutes_passed}m")
                
                try:
                    # Determine Position Side (Hedge Mode check)
                    pos_mode_side = pos.get("positionSide")
                    target_pos_side = None
                    if pos_mode_side in ["LONG", "SHORT"]:
                        target_pos_side = pos_mode_side
                    
                    # Perform Close
                    if not settings.dry_run:
                        client.place_market_order(
                            symbol=symbol,
                            side=close_side,
                            quantity=close_qty,
                            position_side=target_pos_side
                        )
                        action_str = "KAPATILDI 🔴"
                    else:
                        action_str = "KAPATILDI (DRY RUN) 🔴"

                except Exception as e:
                    logger.error(f"Error closing position {symbol}: {e}")
                    action_str = f"HATA ⚠️ ({str(e)})"

            # 6. Prepare Report Line
            line = (
                f"Coin: <b>{symbol}</b>\n"
                f"Zaman: {minutes_passed} dk önce\n"
                f"Giriş: {entry_price}\n"
                f"Güncel: {mark_price}\n"
                f"Durum: {status_str}\n"
                f"Aksiyon: {action_str}\n"
            )
            report_lines.append(line)

        # 7. Send Consolidated Report
        if report_lines:
            header = "🔍 <b>15dk Periyodik Kontrol Raporu</b>\n\n"
            full_msg = header + "\n------------------\n".join(report_lines)
            notifier.send_message(full_msg)

    except Exception as e:
        logger.error(f"Error in check_early_losses: {e}")
    finally:
        db.close()
