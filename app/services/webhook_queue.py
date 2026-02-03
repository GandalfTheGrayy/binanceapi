import asyncio
from typing import Optional, Dict, Any, List
from datetime import datetime
from sqlalchemy.orm import Session
from ..database import SessionLocal
from .. import models
from ..config import get_settings
from ..services.telegram import TelegramNotifier


class WebhookQueue:
	"""Webhook isteklerini queue'da tutan ve DB'ye yazan servis."""
	
	def __init__(self, endpoint: str = "layer1"):
		self.endpoint = endpoint
		self.queue: asyncio.Queue = asyncio.Queue()
	
	def _write_to_db_sync(self, symbol: str, signal: str, price: Optional[float], payload: Dict[str, Any]) -> Optional[int]:
		"""
		DB'ye senkron olarak yazar ve db_id döndürür.
		Returns: db_id (int) veya None (hata durumunda)
		"""
		try:
			db = SessionLocal()
			try:
				evt = models.WebhookEvent(
					endpoint=self.endpoint,
					symbol=symbol,
					signal=signal,
					price=price,
					payload=payload,
					status="pending",
					retry_count=0,
				)
				db.add(evt)
				db.commit()
				db.refresh(evt)
				return evt.id
			except Exception as e:
				db.rollback()
				print(f"[WebhookQueue] DB yazma hatası: {e}")
				return None
			finally:
				db.close()
		except Exception as e:
			print(f"[WebhookQueue] DB bağlantı hatası: {e}")
			return None
	
	async def enqueue(
		self,
		payload: Dict[str, Any],
		client_ip: str,
		symbol: str,
		signal: str,
		price: Optional[float] = None
	) -> Dict[str, Any]:
		"""
		Webhook isteğini önce DB'ye yazar, sonra queue'ya ekler.
		Returns: Queue item dict with queue_id and db_id
		"""
		settings = get_settings()
		notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
		endpoint_label = "Layer1" if self.endpoint == "layer1" else "Layer2"
		
		try:
			# 1. Telegram'a bildir: Webhook isteği geldi
			try:
				queue_content = await self._get_queue_content()
				msg_lines = [
					f"🔔 Webhook İsteği Geldi [{endpoint_label}]",
					f"IP: {client_ip}",
					f"Symbol: {symbol}",
					f"Signal: {signal.upper()}",
					f"Endpoint: {self.endpoint}",
					f"Timestamp: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}",
					"",
					f"📋 Mevcut {endpoint_label} Queue İçeriği:",
				]
				if queue_content:
					msg_lines.append(f"({len(queue_content)} istek)")
					for idx, item in enumerate(queue_content[:10], 1):
						msg_lines.append(f"{idx}. {item['symbol']} - {item['signal']} - {item['created_at'].strftime('%H:%M:%S')}")
					if len(queue_content) > 10:
						msg_lines.append(f"... ve {len(queue_content) - 10} istek daha")
				else:
					msg_lines.append("(Queue boş)")
				await notifier.send_message("\n".join(msg_lines))
			except Exception as e:
				print(f"[WebhookQueue] Telegram bildirim hatası (webhook geldi): {e}")
			
			# 2. ÖNCE DB'ye yaz (SENKRON) - db_id al
			db_id = self._write_to_db_sync(symbol, signal, price, payload)
			
			if db_id is None:
				# DB yazma başarısız - hata döndür
				try:
					await notifier.send_message(f"❌ DB Yazma Başarısız [{endpoint_label}]\nSymbol: {symbol}\nSignal: {signal}\n\nWebhook reddedildi.")
				except Exception:
					pass
				raise Exception("DB'ye yazılamadı, webhook reddedildi")
			
			# 3. Queue item oluştur (db_id ile birlikte)
			queue_id = id(payload)
			queue_item = {
				"queue_id": queue_id,
				"db_id": db_id,  # KRİTİK: db_id eklendi
				"endpoint": self.endpoint,
				"payload": payload,
				"client_ip": client_ip,
				"symbol": symbol,
				"signal": signal,
				"price": price,
				"created_at": datetime.utcnow(),
			}
			
			# 4. Memory queue'ya ekle (FIFO garantisi)
			await self.queue.put(queue_item)
			
			# 5. Telegram'a bildir: Queue'ya ve DB'ye eklendi
			try:
				queue_size = self.queue.qsize()
				msg_lines = [
					f"✅ Queue ve DB'ye Eklendi [{endpoint_label}]",
					f"Symbol: {symbol}",
					f"Signal: {signal.upper()}",
					f"DB ID: {db_id}",
					f"📊 Mevcut Queue: {queue_size} istek bekliyor",
				]
				await notifier.send_message("\n".join(msg_lines))
			except Exception as e:
				print(f"[WebhookQueue] Telegram bildirim hatası (queue eklendi): {e}")
			
		finally:
			await notifier.close()
		
		return queue_item
	
	async def get(self) -> Optional[Dict[str, Any]]:
		"""Memory queue'dan istek al (FIFO garantisi - ilk eklenen ilk çıkar)."""
		try:
			return await asyncio.wait_for(self.queue.get(), timeout=1.0)
		except asyncio.TimeoutError:
			return None
	
	def get_nowait(self) -> Optional[Dict[str, Any]]:
		"""Memory queue'dan istek al (non-blocking)."""
		try:
			return self.queue.get_nowait()
		except asyncio.QueueEmpty:
			return None
	
	async def get_from_db(self, limit: int = 10) -> List[models.WebhookEvent]:
		"""DB'den bu endpoint'e ait pending istekleri getir (FIFO - ORDER BY id ASC)."""
		db = SessionLocal()
		try:
			return db.query(models.WebhookEvent)\
				.filter(models.WebhookEvent.status == "pending")\
				.filter(models.WebhookEvent.endpoint == self.endpoint)\
				.order_by(models.WebhookEvent.id.asc())\
				.limit(limit)\
				.all()
		finally:
			db.close()
	
	async def get_queue_status(self) -> Dict[str, Any]:
		"""Queue durumunu döndür (sayı, içerik listesi)."""
		items = await self._get_queue_content()
		return {
			"count": len(items),
			"items": items,
		}
	
	async def get_db_queue_status(self) -> Dict[str, Any]:
		"""Bu endpoint'e ait DB queue durumunu döndür (sayı, içerik listesi)."""
		db = SessionLocal()
		try:
			items = db.query(models.WebhookEvent)\
				.filter(models.WebhookEvent.status == "pending")\
				.filter(models.WebhookEvent.endpoint == self.endpoint)\
				.order_by(models.WebhookEvent.id.asc())\
				.all()
			return {
				"count": len(items),
				"items": items,
			}
		finally:
			db.close()
	
	async def _get_queue_content(self) -> List[Dict[str, Any]]:
		"""Memory queue içeriğini döndür (snapshot)."""
		items = []
		# Queue'yu geçici olarak boşalt, içeriği al, sonra geri ekle (FIFO sırası korunur)
		temp_items = []
		try:
			while True:
				try:
					item = self.queue.get_nowait()
					temp_items.append(item)
					items.append(item)
				except asyncio.QueueEmpty:
					break
			# Geri ekle (FIFO sırası korunur)
			for item in temp_items:
				await self.queue.put(item)
		except Exception as e:
			print(f"[WebhookQueue] Queue içerik alma hatası: {e}")
			# Hata durumunda geri eklemeyi dene
			for item in temp_items:
				try:
					await self.queue.put(item)
				except Exception:
					pass
		return items
	
	def _format_queue_content(self, items: List[Dict[str, Any]]) -> str:
		"""Queue içeriğini Telegram mesajı formatında döndür."""
		if not items:
			return "(Queue boş)"
		lines = []
		for idx, item in enumerate(items[:20], 1):  # İlk 20'yi göster
			lines.append(f"{idx}. {item['symbol']} - {item['signal']} - {item['created_at'].strftime('%H:%M:%S')}")
		if len(items) > 20:
			lines.append(f"... ve {len(items) - 20} istek daha")
		return "\n".join(lines)


# Global queue instances - her endpoint için ayrı queue
webhook_queue_layer1 = WebhookQueue(endpoint="layer1")
webhook_queue_layer2 = WebhookQueue(endpoint="layer2")

# Geriye uyumluluk için (mevcut kodun çalışmasını sağlamak)
webhook_queue = webhook_queue_layer1


def get_webhook_queue(endpoint: str) -> WebhookQueue:
	"""Endpoint'e göre uygun queue'yu döndür."""
	if endpoint == "layer2":
		return webhook_queue_layer2
	return webhook_queue_layer1

