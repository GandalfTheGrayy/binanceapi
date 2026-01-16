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
	
	def __init__(self):
		self.queue: asyncio.Queue = asyncio.Queue()
		self._db_write_tasks: set = set()
	
	async def enqueue(
		self,
		payload: Dict[str, Any],
		client_ip: str,
		symbol: str,
		signal: str,
		price: Optional[float] = None
	) -> Dict[str, Any]:
		"""
		Webhook isteğini queue'ya ekler ve background task ile DB'ye yazar.
		Returns: Queue item dict with queue_id
		"""
		settings = get_settings()
		notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
		
		# Queue item oluştur
		queue_id = id(payload)  # Unique ID
		queue_item = {
			"queue_id": queue_id,
			"payload": payload,
			"client_ip": client_ip,
			"symbol": symbol,
			"signal": signal,
			"price": price,
			"created_at": datetime.utcnow(),
		}
		
		try:
			# 1. Mevcut queue içeriğini al (webhook geldi mesajı için)
			queue_content = await self._get_queue_content()
			
			# 2. Telegram'a bildir: Webhook isteği geldi
			try:
				msg_lines = [
					"🔔 Webhook İsteği Geldi",
					f"IP: {client_ip}",
					f"Symbol: {symbol}",
					f"Signal: {signal.upper()}",
					f"Timestamp: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}",
					"",
					"📋 Mevcut Queue İçeriği:",
				]
				if queue_content:
					msg_lines.append(f"({len(queue_content)} istek)")
					for idx, item in enumerate(queue_content[:10], 1):  # İlk 10'unu göster
						msg_lines.append(f"{idx}. {item['symbol']} - {item['signal']} - {item['created_at'].strftime('%H:%M:%S')}")
					if len(queue_content) > 10:
						msg_lines.append(f"... ve {len(queue_content) - 10} istek daha")
				else:
					msg_lines.append("(Queue boş)")
				await notifier.send_message("\n".join(msg_lines))
			except Exception as e:
				print(f"[WebhookQueue] Telegram bildirim hatası (webhook geldi): {e}")
			
			# 3. Memory queue'ya ekle (FIFO garantisi)
			await self.queue.put(queue_item)
			
			# 4. Telegram'a bildir: Queue'ya eklendi
			try:
				queue_size = self.queue.qsize()
				queue_content_after = await self._get_queue_content()
				msg_lines = [
					"✅ Queue'ya Eklendi",
					f"Symbol: {symbol}",
					f"Signal: {signal.upper()}",
					f"Queue ID: {queue_id}",
					f"📊 Mevcut Queue: {queue_size} istek bekliyor",
					"",
					"📋 Mevcut Queue İçeriği:",
				]
				if queue_content_after:
					for idx, item in enumerate(queue_content_after[:10], 1):
						marker = " (YENİ)" if item['queue_id'] == queue_id else ""
						msg_lines.append(f"{idx}. {item['symbol']} - {item['signal']} - {item['created_at'].strftime('%H:%M:%S')}{marker}")
					if len(queue_content_after) > 10:
						msg_lines.append(f"... ve {len(queue_content_after) - 10} istek daha")
				await notifier.send_message("\n".join(msg_lines))
			except Exception as e:
				print(f"[WebhookQueue] Telegram bildirim hatası (queue eklendi): {e}")
			
			# 5. Background task başlat (DB'ye yazma)
			task = asyncio.create_task(self._write_to_db_async(queue_item))
			self._db_write_tasks.add(task)
			task.add_done_callback(self._db_write_tasks.discard)
			
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
		"""DB'den pending istekleri getir (FIFO - ORDER BY id ASC)."""
		db = SessionLocal()
		try:
			return db.query(models.WebhookEvent)\
				.filter(models.WebhookEvent.status == "pending")\
				.order_by(models.WebhookEvent.id.asc())\
				.limit(limit)\
				.all()
		finally:
			db.close()
	
	async def _write_to_db_async(self, queue_item: Dict[str, Any], retry_count: int = 0) -> None:
		"""Background task: DB'ye yazar (retry mekanizması ile)."""
		settings = get_settings()
		notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
		max_retries = 3
		
		try:
			for attempt in range(max_retries):
				try:
					db = SessionLocal()
					try:
						# DB'ye kaydet
						evt = models.WebhookEvent(
							symbol=queue_item["symbol"],
							signal=queue_item["signal"],
							price=queue_item["price"],
							payload=queue_item["payload"],
							status="pending",
							retry_count=0,
						)
						db.add(evt)
						db.commit()
						db.refresh(evt)
						
						# Telegram'a bildir: DB'ye eklendi
						try:
							db_queue_status = await self.get_db_queue_status()
							msg_lines = [
								"💾 DB'ye Eklendi",
								f"Symbol: {queue_item['symbol']}",
								f"Signal: {queue_item['signal'].upper()}",
								f"DB ID: {evt.id}",
								f"📊 Mevcut DB Queue: {db_queue_status['count']} istek bekliyor",
								"",
								"📋 Mevcut DB Queue İçeriği:",
							]
							if db_queue_status['items']:
								for idx, item in enumerate(db_queue_status['items'][:10], 1):
									marker = " (YENİ)" if item.id == evt.id else ""
									msg_lines.append(f"{idx}. {item.symbol} - {item.signal} - {item.created_at.strftime('%H:%M:%S')} (ID: {item.id}){marker}")
								if len(db_queue_status['items']) > 10:
									msg_lines.append(f"... ve {len(db_queue_status['items']) - 10} istek daha")
							await notifier.send_message("\n".join(msg_lines))
						except Exception as e:
							print(f"[WebhookQueue] Telegram bildirim hatası (DB eklendi): {e}")
						
						return  # Başarılı
					except Exception as e:
						db.rollback()
						raise e
					finally:
						db.close()
				except Exception as e:
					if attempt < max_retries - 1:
						# Exponential backoff
						wait_time = 2 ** attempt
						await asyncio.sleep(wait_time)
						continue
					else:
						# Son deneme başarısız
						try:
							error_msg = [
								"❌ DB'ye Yazma Başarısız",
								f"Symbol: {queue_item['symbol']}",
								f"Signal: {queue_item['signal']}",
								f"Hata: {str(e)}",
								"",
								"⚠️ İstek memory queue'da bekliyor, worker işleyecek.",
							]
							await notifier.send_message("\n".join(error_msg))
						except Exception:
							pass
						print(f"[WebhookQueue] DB yazma hatası (tüm retry'lar başarısız): {e}")
		finally:
			await notifier.close()
	
	async def get_queue_status(self) -> Dict[str, Any]:
		"""Queue durumunu döndür (sayı, içerik listesi)."""
		items = await self._get_queue_content()
		return {
			"count": len(items),
			"items": items,
		}
	
	async def get_db_queue_status(self) -> Dict[str, Any]:
		"""DB queue durumunu döndür (sayı, içerik listesi)."""
		db = SessionLocal()
		try:
			items = db.query(models.WebhookEvent)\
				.filter(models.WebhookEvent.status == "pending")\
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


# Global queue instance
webhook_queue = WebhookQueue()

