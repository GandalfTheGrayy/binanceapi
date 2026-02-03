import asyncio
from typing import Optional, Dict, Any
from datetime import datetime
from sqlalchemy.orm import Session
from ..database import SessionLocal
from .. import models
from ..config import get_settings
from ..services.telegram import TelegramNotifier
from ..services.webhook_queue import webhook_queue_layer1, webhook_queue_layer2, get_webhook_queue
from ..services.binance_client import BinanceFuturesClient
from ..services.order_sizing import get_symbol_filters, round_step
from ..state import runtime
from ..services.ws_manager import ws_manager
from ..services.symbols import normalize_tv_symbol
import httpx
import json


class WebhookWorker:
	"""Background worker: Queue'dan webhook isteklerini alıp işler."""
	
	def __init__(self, endpoint: str = "layer1"):
		self.endpoint = endpoint
		self.running = False
		self._task: Optional[asyncio.Task] = None
		self._queue = get_webhook_queue(endpoint)
	
	@property
	def endpoint_label(self) -> str:
		return "Layer1" if self.endpoint == "layer1" else "Layer2"
	
	async def start(self):
		"""Worker'ı başlat."""
		if self.running:
			return
		self.running = True
		self._task = asyncio.create_task(self._worker_loop())
		print(f"[WebhookWorker-{self.endpoint_label}] Worker başlatıldı")
		
		# Startup'ta DB'deki pending istekleri yükle
		await self.recover_pending_webhooks()
	
	async def stop(self):
		"""Worker'ı durdur."""
		self.running = False
		if self._task:
			self._task.cancel()
			try:
				await self._task
			except asyncio.CancelledError:
				pass
		print(f"[WebhookWorker-{self.endpoint_label}] Worker durduruldu")
	
	async def recover_pending_webhooks(self):
		"""Startup'ta DB'deki bu endpoint'e ait pending istekleri memory queue'ya yükle."""
		settings = get_settings()
		notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
		
		try:
			db = SessionLocal()
			try:
				pending_events = db.query(models.WebhookEvent)\
					.filter(models.WebhookEvent.status == "pending")\
					.filter(models.WebhookEvent.endpoint == self.endpoint)\
					.order_by(models.WebhookEvent.id.asc())\
					.all()
				
				if pending_events:
					print(f"[WebhookWorker-{self.endpoint_label}] {len(pending_events)} pending istek bulundu, queue'ya yükleniyor...")
					
					for evt in pending_events:
						queue_item = {
							"queue_id": evt.id,
							"endpoint": self.endpoint,
							"payload": evt.payload or {},
							"client_ip": "unknown",
							"symbol": evt.symbol,
							"signal": evt.signal,
							"price": evt.price,
							"created_at": evt.created_at,
							"db_id": evt.id,  # DB'den geldiğini belirt
						}
						await self._queue.queue.put(queue_item)
					
					try:
						msg = [
							f"🔄 Restart Recovery [{self.endpoint_label}]",
							f"{len(pending_events)} pending istek queue'ya yüklendi",
							"",
							"📋 Yüklenen İstekler:",
						]
						for idx, evt in enumerate(pending_events[:10], 1):
							msg.append(f"{idx}. {evt.symbol} - {evt.signal} - {evt.created_at.strftime('%H:%M:%S')}")
						if len(pending_events) > 10:
							msg.append(f"... ve {len(pending_events) - 10} istek daha")
						await notifier.send_message("\n".join(msg))
					except Exception as e:
						print(f"[WebhookWorker-{self.endpoint_label}] Telegram bildirim hatası (recovery): {e}")
			finally:
				db.close()
		finally:
			await notifier.close()
	
	async def _worker_loop(self):
		"""Worker loop: Sürekli queue'yu kontrol edip işler."""
		settings = get_settings()
		
		while self.running:
			try:
				# Memory queue'dan istek al (FIFO) - sürekli bekler
				try:
					queue_item = await asyncio.wait_for(self._queue.queue.get(), timeout=1.0)
				except asyncio.TimeoutError:
					# Queue boşsa kısa bekleme ve tekrar dene
					await asyncio.sleep(0.1)
					continue
				
				if queue_item:
					# Telegram'a bildir: Queue'dan işlem alındı
					notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
					try:
						queue_status = await self._queue.get_queue_status()
						msg_lines = [
							f"🔄 Queue'dan İşlem Alındı [{self.endpoint_label}]",
							f"Symbol: {queue_item['symbol']}",
							f"Signal: {queue_item['signal'].upper()}",
							f"Queue ID: {queue_item.get('queue_id', 'N/A')}",
							f"📊 Kalan {self.endpoint_label} Queue: {queue_status['count']} istek",
							"",
							"📋 Kalan Queue İçeriği:",
						]
						if queue_status['items']:
							for idx, item in enumerate(queue_status['items'][:10], 1):
								msg_lines.append(f"{idx}. {item['symbol']} - {item['signal']} - {item['created_at'].strftime('%H:%M:%S')}")
							if len(queue_status['items']) > 10:
								msg_lines.append(f"... ve {len(queue_status['items']) - 10} istek daha")
						else:
							msg_lines.append("(Queue boş)")
						await notifier.send_message("\n".join(msg_lines))
					except Exception as e:
						print(f"[WebhookWorker-{self.endpoint_label}] Telegram bildirim hatası (işlem alındı): {e}")
					finally:
						await notifier.close()
					
					# İşle
					start_time = datetime.utcnow()
					try:
						result = await self.process_webhook(queue_item)
						
						# Telegram'a bildir: İşlem sonucu
						notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
						try:
							processing_time = (datetime.utcnow() - start_time).total_seconds()
							queue_status = await self._queue.get_queue_status()
							
							# Başarı kontrolü: Binance Order ID varsa başarılı
							order_id = result.get("order_id")
							is_dry_run = result.get("response", {}).get("dry_run", False) if result.get("response") else False
							
							if order_id and order_id != "None":
								status_icon = "✅"
								status_text = "BAŞARILI"
							elif is_dry_run:
								status_icon = "🔸"
								status_text = "DRY_RUN (Simülasyon)"
							else:
								status_icon = "❌"
								status_text = "BAŞARISIZ"
							
							msg_lines = [
								f"{status_icon} İşlem Sonucu [{self.endpoint_label}]",
								f"Symbol: {queue_item['symbol']}",
								f"Signal: {queue_item['signal'].upper()}",
								f"Durum: {status_text}",
								f"İşlem Süresi: {processing_time:.2f}s",
							]
							
							if order_id and order_id != "None":
								msg_lines.append(f"🎯 Binance Order ID: {order_id}")
							
							if result.get("error"):
								msg_lines.append("")
								msg_lines.append(f"⚠️ Hata: {result['error']}")
							
							if result.get("response"):
								msg_lines.append("")
								msg_lines.append("📝 Binance Response:")
								try:
									formatted_response = json.dumps(result['response'], indent=2)
									msg_lines.append(f"<pre>{formatted_response}</pre>")
								except:
									msg_lines.append(str(result['response']))
							
							msg_lines.append("")
							msg_lines.append(f"📊 Kalan {self.endpoint_label} Queue: {queue_status['count']} istek")
							msg_lines.append("")
							msg_lines.append("📋 Kalan Queue İçeriği:")
							if queue_status['items']:
								for idx, item in enumerate(queue_status['items'][:10], 1):
									msg_lines.append(f"{idx}. {item['symbol']} - {item['signal']} - {item['created_at'].strftime('%H:%M:%S')}")
								if len(queue_status['items']) > 10:
									msg_lines.append(f"... ve {len(queue_status['items']) - 10} istek daha")
							else:
								msg_lines.append("(Queue boş)")
							
							await notifier.send_message("\n".join(msg_lines))
						except Exception as e:
							print(f"[WebhookWorker-{self.endpoint_label}] Telegram bildirim hatası (işlem sonucu): {e}")
						finally:
							await notifier.close()
					except Exception as e:
						# İşleme hatası
						print(f"[WebhookWorker-{self.endpoint_label}] İşleme hatası: {e}")
						
						# Retry mekanizması
						retry_count = queue_item.get("retry_count", 0)
						max_retries = 3
						
						if retry_count < max_retries:
							# Retry yap
							queue_item["retry_count"] = retry_count + 1
							wait_time = 2 ** retry_count  # Exponential backoff
							await asyncio.sleep(wait_time)
							
							# Queue'ya tekrar ekle (FIFO sırası korunur)
							await self._queue.queue.put(queue_item)
							
							# Telegram'a bildir
							notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
							try:
								msg = [
									f"⚠️ Retry Yapıldı [{self.endpoint_label}]",
									f"Symbol: {queue_item['symbol']}",
									f"Signal: {queue_item['signal']}",
									f"Retry: {retry_count + 1}/{max_retries}",
									f"Hata: {str(e)}",
									f"Bekleme: {wait_time}s",
								]
								await notifier.send_message("\n".join(msg))
							except Exception:
								pass
							finally:
								await notifier.close()
						else:
							# Retry limitine ulaşıldı
							# Queue'ya tekrar ekle (FIFO sırası korunur)
							queue_item["retry_count"] = retry_count + 1
							await self._queue.queue.put(queue_item)
							
							# DB'de status'u güncelle
							if queue_item.get("db_id"):
								db = SessionLocal()
								try:
									evt = db.query(models.WebhookEvent).filter_by(id=queue_item["db_id"]).first()
									if evt:
										evt.status = "pending"
										evt.retry_count = retry_count + 1
										db.commit()
								finally:
									db.close()
							
							# Telegram'a bildir
							notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
							try:
								queue_status = await self._queue.get_queue_status()
								msg_lines = [
									f"❌ Retry Limitine Ulaşıldı [{self.endpoint_label}]",
									f"Symbol: {queue_item['symbol']}",
									f"Signal: {queue_item['signal']}",
									f"Hata: {str(e)}",
									"",
									"İstek queue'ya tekrar eklendi (FIFO sırası korunur)",
									f"📊 Mevcut {self.endpoint_label} Queue: {queue_status['count']} istek",
								]
								await notifier.send_message("\n".join(msg_lines))
							except Exception:
								pass
							finally:
								await notifier.close()
				
				# Queue boşsa kısa bekleme
				await asyncio.sleep(0.1)
			except Exception as e:
				print(f"[WebhookWorker-{self.endpoint_label}] Worker loop hatası: {e}")
				await asyncio.sleep(1)
	
	async def process_webhook(self, queue_item: Dict[str, Any]) -> Dict[str, Any]:
		"""
		Webhook isteğini işle (mevcut handle_tradingview mantığını kullanır).
		Returns: {"success": bool, "order_id": str, "response": dict, "error": str}
		"""
		# Endpoint bilgisini queue_item'a ekle (yoksa)
		if "endpoint" not in queue_item:
			queue_item["endpoint"] = self.endpoint
		
		# Import here to avoid circular dependency
		from ..routers.webhook import process_webhook_request
		return await process_webhook_request(queue_item)


# Global worker instances - her endpoint için ayrı worker
webhook_worker_layer1 = WebhookWorker(endpoint="layer1")
webhook_worker_layer2 = WebhookWorker(endpoint="layer2")

# Geriye uyumluluk için
webhook_worker = webhook_worker_layer1