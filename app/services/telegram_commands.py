"""
Telegram Command Handler - !islemler komutu için interaktif menü yönetimi
"""
import asyncio
from typing import Dict, Any, Optional
from datetime import datetime, timedelta
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
				
				# DEBUG: Gelen mesajı logla
				print(f"[TelegramCommandHandler] Mesaj alındı - chat_id: '{chat_id}', text: '{text[:50] if text else ''}'")
				
				# GÜVENLİK: Sadece .env'de tanımlı chat_id'den gelen mesajları işle
				allowed_chat_id = str(self.notifier.chat_id).strip()
				incoming_chat_id = str(chat_id).strip()
				
				print(f"[TelegramCommandHandler] Karşılaştırma - gelen: '{incoming_chat_id}' vs izinli: '{allowed_chat_id}'")
				
				if incoming_chat_id != allowed_chat_id:
					print(f"[TelegramCommandHandler] Yetkisiz chat_id: {incoming_chat_id} (izin verilen: {allowed_chat_id})")
					return
				
				# /islemler komutu kontrolü (Telegram gruplarında / ile başlayan komutlar gerekli)
				if text.strip().lower() in ["/islemler", "/islemler@" + self.notifier.bot_token.split(":")[0] if ":" in self.notifier.bot_token else ""]:
					await self.show_main_menu(chat_id)
					return
				
				# @botismi /islemler formatını da destekle
				if text.strip().lower().startswith("/islemler"):
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
			
			# GÜVENLİK: Sadece .env'de tanımlı chat_id'den gelen callback'leri işle
			allowed_chat_id = self.notifier.chat_id
			if chat_id != allowed_chat_id:
				print(f"[TelegramCommandHandler] Yetkisiz callback chat_id: {chat_id}")
				await self.notifier.answer_callback_query(query_id, "❌ Yetkisiz erişim", show_alert=True)
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
		"""Anlık raporu gönderir - Layer bazlı"""
		db = SessionLocal()
		client = None
		try:
			settings = get_settings()
			client = BinanceFuturesClient(
				settings.binance_api_key, 
				settings.binance_api_secret, 
				settings.binance_base_url
			)
			
			# Binance'den verileri çek
			wallet = 0.0
			available = 0.0
			binance_positions = []
			mark_prices = {}  # symbol -> mark_price
			
			if settings.binance_api_key and settings.binance_api_secret and not settings.dry_run:
				try:
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
					mark_price = mark_prices.get(symbol, entry_price)
					
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
			
			# ========== MESAJ OLUŞTUR ==========
			msg = (
				f"📈 <b>Anlık Rapor</b>\n\n"
				f"💵 <b>Wallet:</b> {wallet:.2f} USDT\n"
				f"💰 <b>Available:</b> {available:.2f} USDT\n"
				f"🔒 <b>Used:</b> {used:.2f} USDT\n"
				f"📊 <b>PNL 1h:</b> {pnl_1h:.2f} USDT\n"
				f"📈 <b>PNL Total:</b> {pnl_total:.2f} USDT\n"
			)
			
			# Layer 1 Pozisyonları
			msg += f"\n{'='*25}\n"
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
			msg += f"\n{'='*25}\n"
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
			msg += f"\n{'='*25}\n"
			total_emoji = "🟢" if total_layer_pnl >= 0 else "🔴"
			estimated_balance = wallet + total_layer_pnl
			msg += (
				f"💎 <b>TOPLAM</b>\n"
				f"{total_emoji} <b>Unrealized PnL:</b> ${total_layer_pnl:.2f}\n"
				f"💰 <b>Şu an kapanırsa:</b> {estimated_balance:.2f} USDT\n"
			)

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
				print(f"[TelegramCommandHandler] Grafik oluşturma hatası: {e}")
				import traceback
				traceback.print_exc()
			
			# Mesajı gönder
			if graph_bio:
				await self.notifier.send_photo(graph_bio, caption=msg)
			else:
				await self.notifier.send_message(msg)
			
		except Exception as e:
			print(f"[TelegramCommandHandler] Rapor gönderme hatası: {e}")
			await self.notifier.send_message(f"❌ Rapor oluşturulurken hata oluştu: {e}")
		finally:
			# BinanceFuturesClient'ı kapat (file descriptor leak önlemi)
			if client is not None:
				await client.close()
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
	poll_count = 0
	
	while _polling_running:
		if command_handler is None:
			await asyncio.sleep(1)
			continue
		
		try:
			updates = await command_handler.notifier.get_updates(timeout=1)
			poll_count += 1
			
			# Her 10 polling'de bir log bas (sessiz kalmasın)
			if poll_count % 10 == 0:
				print(f"[TelegramCommandHandler] Polling aktif... (#{poll_count})")
			
			if updates:
				print(f"[TelegramCommandHandler] {len(updates)} update alındı!")
				for update in updates:
					print(f"[TelegramCommandHandler] Update: {update}")
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
	global _polling_task, _polling_running, command_handler
	
	_polling_running = False
	
	if _polling_task is not None:
		_polling_task.cancel()
		try:
			await _polling_task
		except asyncio.CancelledError:
			pass
		_polling_task = None
	
	# Notifier'ın HTTP client'ını kapat (file descriptor leak önlemi)
	if command_handler is not None and command_handler.notifier is not None:
		await command_handler.notifier.close()
	
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
