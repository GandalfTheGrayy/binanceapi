"""
Telegram Command Handler - !islemler komutu için interaktif menü yönetimi
"""
import asyncio
from typing import Dict, Any, Optional
from datetime import datetime
import io
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from .telegram import TelegramNotifier
from ..config import get_settings
from ..state import runtime
from ..database import SessionLocal
from .. import models
from .binance_client import BinanceFuturesClient


class TelegramCommandHandler:
	"""Telegram bot komutlarını işleyen handler"""
	
	# Callback data için sabitler
	ACTION_REPORT = "action_report"
	ACTION_SHOW_SETTINGS = "action_show_settings"
	ACTION_CHANGE_SETTINGS = "action_change_settings"
	ACTION_CHANGE_LEVERAGE = "action_change_leverage"
	ACTION_CHANGE_TRADE_AMOUNT = "action_change_trade_amount"
	ACTION_BACK_TO_MAIN = "action_back_main"
	
	def __init__(self, notifier: TelegramNotifier):
		self.notifier = notifier
		# User conversation states: {chat_id: {"state": "...", "data": {...}}}
		self.user_states: Dict[str, Dict[str, Any]] = {}
	
	def get_user_state(self, chat_id: str) -> Dict[str, Any]:
		"""Kullanıcının konuşma durumunu döndürür."""
		if chat_id not in self.user_states:
			self.user_states[chat_id] = {"state": "none", "data": {}}
		return self.user_states[chat_id]
	
	def set_user_state(self, chat_id: str, state: str, data: Dict[str, Any] = None):
		"""Kullanıcının konuşma durumunu ayarlar."""
		self.user_states[chat_id] = {"state": state, "data": data or {}}
	
	def clear_user_state(self, chat_id: str):
		"""Kullanıcının konuşma durumunu temizler."""
		if chat_id in self.user_states:
			del self.user_states[chat_id]
	
	async def handle_update(self, update: Dict[str, Any]):
		"""Gelen Telegram güncellemesini işler."""
		try:
			# Callback query (buton tıklaması)
			if "callback_query" in update:
				await self.handle_callback_query(update["callback_query"])
				return
			
			# Normal mesaj
			if "message" in update:
				message = update["message"]
				text = message.get("text", "")
				chat_id = str(message.get("chat", {}).get("id", ""))
				
				if not chat_id:
					return
				
				# !islemler komutu kontrolü
				if text.strip().lower() == "!islemler":
					await self.show_main_menu(chat_id)
					return
				
				# Kullanıcı bir değer bekliyor mu?
				user_state = self.get_user_state(chat_id)
				if user_state["state"] == "waiting_leverage":
					await self.handle_leverage_input(chat_id, text)
					return
				elif user_state["state"] == "waiting_trade_amount":
					await self.handle_trade_amount_input(chat_id, text)
					return
					
		except Exception as e:
			print(f"[TelegramCommandHandler] Update işleme hatası: {e}")
	
	async def handle_callback_query(self, callback_query: Dict[str, Any]):
		"""Callback query'yi (buton tıklamasını) işler."""
		try:
			query_id = callback_query.get("id")
			data = callback_query.get("data", "")
			message = callback_query.get("message", {})
			chat_id = str(message.get("chat", {}).get("id", ""))
			message_id = message.get("message_id")
			
			if not chat_id or not query_id:
				return
			
			# Callback'i yanıtla (loading'i kapat)
			await self.notifier.answer_callback_query(query_id)
			
			# Aksiyona göre işlem yap
			if data == self.ACTION_REPORT:
				await self.send_report(chat_id)
			elif data == self.ACTION_SHOW_SETTINGS:
				await self.show_settings(chat_id)
			elif data == self.ACTION_CHANGE_SETTINGS:
				await self.show_settings_menu(chat_id)
			elif data == self.ACTION_CHANGE_LEVERAGE:
				await self.ask_for_leverage(chat_id)
			elif data == self.ACTION_CHANGE_TRADE_AMOUNT:
				await self.ask_for_trade_amount(chat_id)
			elif data == self.ACTION_BACK_TO_MAIN:
				await self.show_main_menu(chat_id)
				
		except Exception as e:
			print(f"[TelegramCommandHandler] Callback işleme hatası: {e}")
	
	async def show_main_menu(self, chat_id: str):
		"""Ana menüyü gösterir."""
		self.clear_user_state(chat_id)
		
		text = "📊 <b>İşlemler Menüsü</b>\n\nBir seçenek seçin:"
		
		keyboard = [
			[{"text": "📈 Rapor Ver", "callback_data": self.ACTION_REPORT}],
			[{"text": "⚙️ Çalışma Ayarlarını Göster", "callback_data": self.ACTION_SHOW_SETTINGS}],
			[{"text": "🔧 Çalışma Ayarlarını Değiştir", "callback_data": self.ACTION_CHANGE_SETTINGS}]
		]
		
		await self.notifier.send_message_with_keyboard(text, keyboard, chat_id)
	
	async def show_settings(self, chat_id: str):
		"""Çalışma ayarlarını gösterir."""
		leverage = runtime.default_leverage or 5
		fixed_amount = runtime.fixed_trade_amount or 0
		policy = runtime.leverage_policy or "auto"
		
		text = (
			"⚙️ <b>Çalışma Ayarları</b>\n\n"
			f"📊 <b>Leverage:</b> {leverage}x\n"
			f"💰 <b>Sabit İşlem Tutarı:</b> ${fixed_amount:.2f}\n"
			f"📋 <b>Kaldıraç Politikası:</b> {policy}\n"
		)
		
		keyboard = [
			[{"text": "🔙 Ana Menü", "callback_data": self.ACTION_BACK_TO_MAIN}]
		]
		
		await self.notifier.send_message_with_keyboard(text, keyboard, chat_id)
	
	async def show_settings_menu(self, chat_id: str):
		"""Ayar değiştirme menüsünü gösterir."""
		text = "🔧 <b>Hangi ayarı değiştirmek istiyorsunuz?</b>"
		
		keyboard = [
			[{"text": "📊 Leverage", "callback_data": self.ACTION_CHANGE_LEVERAGE}],
			[{"text": "💰 Sabit İşlem Tutarı", "callback_data": self.ACTION_CHANGE_TRADE_AMOUNT}],
			[{"text": "🔙 Ana Menü", "callback_data": self.ACTION_BACK_TO_MAIN}]
		]
		
		await self.notifier.send_message_with_keyboard(text, keyboard, chat_id)
	
	async def ask_for_leverage(self, chat_id: str):
		"""Yeni leverage değeri ister."""
		current = runtime.default_leverage or 5
		
		text = (
			f"📊 <b>Leverage Değiştir</b>\n\n"
			f"Mevcut değer: <b>{current}x</b>\n\n"
			f"Yeni leverage değerini yazın (1-125 arası):"
		)
		
		self.set_user_state(chat_id, "waiting_leverage")
		await self.notifier.send_message(text)
	
	async def ask_for_trade_amount(self, chat_id: str):
		"""Yeni sabit işlem tutarı ister."""
		current = runtime.fixed_trade_amount or 0
		
		text = (
			f"💰 <b>Sabit İşlem Tutarı Değiştir</b>\n\n"
			f"Mevcut değer: <b>${current:.2f}</b>\n\n"
			f"Yeni tutarı yazın (USD, 0 = devre dışı):"
		)
		
		self.set_user_state(chat_id, "waiting_trade_amount")
		await self.notifier.send_message(text)
	
	async def handle_leverage_input(self, chat_id: str, text: str):
		"""Kullanıcıdan gelen leverage değerini işler."""
		try:
			value = int(text.strip())
			if value < 1 or value > 125:
				await self.notifier.send_message("❌ Leverage 1-125 arası olmalıdır. Tekrar deneyin:")
				return
			
			# Runtime'ı güncelle
			runtime.default_leverage = value
			
			# DB'ye kaydet
			db = SessionLocal()
			try:
				runtime.save_to_db(db)
			finally:
				db.close()
			
			self.clear_user_state(chat_id)
			
			await self.notifier.send_message(f"✅ Leverage <b>{value}x</b> olarak ayarlandı!")
			await self.show_settings(chat_id)
			
		except ValueError:
			await self.notifier.send_message("❌ Geçerli bir sayı girin. Tekrar deneyin:")
	
	async def handle_trade_amount_input(self, chat_id: str, text: str):
		"""Kullanıcıdan gelen sabit işlem tutarını işler."""
		try:
			value = float(text.strip().replace(",", ".").replace("$", ""))
			if value < 0:
				await self.notifier.send_message("❌ Tutar negatif olamaz. Tekrar deneyin:")
				return
			
			# Runtime'ı güncelle
			runtime.fixed_trade_amount = value
			
			# DB'ye kaydet
			db = SessionLocal()
			try:
				runtime.save_to_db(db)
			finally:
				db.close()
			
			self.clear_user_state(chat_id)
			
			if value == 0:
				await self.notifier.send_message("✅ Sabit işlem tutarı <b>devre dışı</b> bırakıldı!")
			else:
				await self.notifier.send_message(f"✅ Sabit işlem tutarı <b>${value:.2f}</b> olarak ayarlandı!")
			
			await self.show_settings(chat_id)
			
		except ValueError:
			await self.notifier.send_message("❌ Geçerli bir tutar girin. Tekrar deneyin:")
	
	async def send_report(self, chat_id: str):
		"""Saatlik raporu gönderir (hourly_pnl_job ile aynı mantık)."""
		db = SessionLocal()
		try:
			settings = get_settings()
			client = BinanceFuturesClient(
				settings.binance_api_key, 
				settings.binance_api_secret, 
				settings.binance_base_url
			)
			
			# Verileri çek
			wallet = 0.0
			available = 0.0
			positions = []
			
			if settings.binance_api_key and settings.binance_api_secret and not settings.dry_run:
				try:
					acct = await client.account_usdt_balances()
					positions = await client.positions()
					wallet = acct.get("wallet", 0.0)
					available = acct.get("available", 0.0)
				except Exception as e:
					print(f"[TelegramCommandHandler] Binance veri çekme hatası: {e}")
			
			# Snapshot'tan veri çek
			last_snap = db.query(models.BalanceSnapshot).order_by(models.BalanceSnapshot.id.desc()).first()
			used = last_snap.used_allocation_usd if last_snap else 0.0
			
			if wallet == 0.0 and last_snap:
				wallet = last_snap.total_wallet_balance
			if available == 0.0:
				available = wallet - used
			
			# PnL hesapla
			first_snap = db.query(models.BalanceSnapshot).order_by(models.BalanceSnapshot.id.asc()).first()
			pnl_total = (wallet - first_snap.total_wallet_balance) if first_snap else 0.0
			pnl_1h = (wallet - last_snap.total_wallet_balance) if last_snap else 0.0
			
			msg = (
				f"📈 <b>Anlık Rapor</b>\n\n"
				f"💵 <b>Wallet:</b> {wallet:.2f} USDT\n"
				f"💰 <b>Available:</b> {available:.2f} USDT\n"
				f"🔒 <b>Used:</b> {used:.2f} USDT\n"
				f"📊 <b>PNL 1h:</b> {pnl_1h:.2f} USDT\n"
				f"📈 <b>PNL Total:</b> {pnl_total:.2f} USDT\n"
			)
			
			# Açık pozisyonları ekle
			total_unrealized_pnl = 0.0
			
			if positions:
				msg += "\n📊 <b>Açık Pozisyonlar:</b>\n"
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
						initial_margin = (entry_price * abs(amt)) / leverage if leverage > 0 else 0
						roe_pct = (unrealized_pnl / initial_margin) * 100 if initial_margin > 0 else 0.0
						
						total_unrealized_pnl += unrealized_pnl
						
						msg += (
							f"\n<b>{symbol}</b> ({side})\n"
							f"Giriş: {entry_price} | Mark: {mark_price}\n"
							f"Miktar: {amt} | Kâr: ${unrealized_pnl:.2f} ({roe_pct:.1f}%)\n"
						)
					except Exception as e:
						print(f"[TelegramCommandHandler] Position işleme hatası: {e}")
						continue
				
				estimated_balance = wallet + total_unrealized_pnl
				msg += f"\n💰 <b>Tahmini Toplam Bakiye:</b> {estimated_balance:.2f} USDT"
			else:
				msg += "\n📭 Açık pozisyon yok."
			
			# Grafik oluştur
			graph_bio = None
			try:
				last_snaps = db.query(models.BalanceSnapshot).order_by(models.BalanceSnapshot.id.desc()).limit(48).all()
				last_snaps.reverse()
				
				if last_snaps and len(last_snaps) > 1:
					dates = [s.created_at.strftime("%H:%M") for s in last_snaps]
					equities = [s.total_equity if s.total_equity is not None else s.total_wallet_balance for s in last_snaps]
					
					plt.figure(figsize=(10, 5))
					plt.plot(dates, equities, marker='o', linestyle='-', color='b', markersize=4)
					plt.title('Toplam Bakiye (Equity) - Son 48 Saat')
					plt.xlabel('Saat')
					plt.ylabel('USDT')
					plt.grid(True, which='both', linestyle='--', linewidth=0.5)
					
					n = len(dates)
					if n > 12:
						step = n // 12
						plt.xticks(range(0, n, step), dates[::step], rotation=45)
					else:
						plt.xticks(rotation=45)
					
					plt.tight_layout()
					
					graph_bio = io.BytesIO()
					plt.savefig(graph_bio, format='png')
					graph_bio.seek(0)
					plt.close()
			except Exception as e:
				print(f"[TelegramCommandHandler] Grafik oluşturma hatası: {e}")
			
			# Mesajı gönder
			if graph_bio:
				await self.notifier.send_photo(graph_bio, caption=msg)
			else:
				await self.notifier.send_message(msg)
			
		except Exception as e:
			print(f"[TelegramCommandHandler] Rapor gönderme hatası: {e}")
			await self.notifier.send_message(f"❌ Rapor oluşturulurken hata oluştu: {e}")
		finally:
			db.close()


# Global command handler instance (will be initialized on startup)
command_handler: Optional[TelegramCommandHandler] = None
# Background polling task
_polling_task: Optional[asyncio.Task] = None
_polling_running: bool = False


async def _polling_loop():
	"""Sürekli çalışan Telegram polling döngüsü."""
	global command_handler, _polling_running
	
	print("[TelegramCommandHandler] Polling döngüsü başladı")
	
	while _polling_running:
		if command_handler is None:
			await asyncio.sleep(1)
			continue
		
		try:
			updates = await command_handler.notifier.get_updates(timeout=1)
			for update in updates:
				try:
					await command_handler.handle_update(update)
				except Exception as e:
					print(f"[TelegramCommandHandler] Update işleme hatası: {e}")
		except Exception as e:
			print(f"[TelegramCommandHandler] Polling hatası: {e}")
		
		# Kısa bekleme
		await asyncio.sleep(0.5)
	
	print("[TelegramCommandHandler] Polling döngüsü durduruldu")


async def start_polling_loop():
	"""Background polling task'ını başlatır."""
	global _polling_task, _polling_running
	
	if _polling_task is not None and not _polling_task.done():
		print("[TelegramCommandHandler] Polling zaten çalışıyor")
		return
	
	_polling_running = True
	_polling_task = asyncio.create_task(_polling_loop())
	print("[TelegramCommandHandler] Polling task başlatıldı")


async def stop_polling_loop():
	"""Background polling task'ını durdurur."""
	global _polling_task, _polling_running
	
	_polling_running = False
	
	if _polling_task is not None:
		_polling_task.cancel()
		try:
			await _polling_task
		except asyncio.CancelledError:
			pass
		_polling_task = None
	
	print("[TelegramCommandHandler] Polling task durduruldu")


async def poll_telegram_updates():
	"""Tek seferlik polling (geriye uyumluluk için)."""
	global command_handler
	
	if command_handler is None:
		return
	
	try:
		updates = await command_handler.notifier.get_updates(timeout=1)
		for update in updates:
			await command_handler.handle_update(update)
	except Exception as e:
		print(f"[TelegramCommandHandler] Polling hatası: {e}")


def init_command_handler(bot_token: str, chat_id: str) -> TelegramCommandHandler:
	"""Command handler'ı başlatır."""
	global command_handler
	notifier = TelegramNotifier(bot_token, chat_id)
	command_handler = TelegramCommandHandler(notifier)
	return command_handler
