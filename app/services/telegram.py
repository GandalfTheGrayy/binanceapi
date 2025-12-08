import httpx
from typing import Optional, List, Dict, Any


class TelegramNotifier:
	def __init__(self, bot_token: str, chat_id: str):
		self.bot_token = bot_token
		self.chat_id = chat_id
		self.base_url = f"https://api.telegram.org/bot{self.bot_token}"
		self.last_error = None
		self.last_update_id = 0

	async def get_updates(self, offset: int = None, timeout: int = 10) -> List[Dict[str, Any]]:
		"""
		Telegram'dan yeni güncellemeleri çeker (long polling).
		"""
		if not self.bot_token:
			return []
		
		try:
			params = {"timeout": timeout}
			if offset is not None:
				params["offset"] = offset
			elif self.last_update_id > 0:
				params["offset"] = self.last_update_id + 1
			
			async with httpx.AsyncClient(timeout=timeout + 5) as client:
				resp = await client.get(
					f"{self.base_url}/getUpdates",
					params=params
				)
				resp.raise_for_status()
				result = resp.json()
				
				if result.get("ok") and result.get("result"):
					updates = result["result"]
					# Update last_update_id
					if updates:
						self.last_update_id = max(u.get("update_id", 0) for u in updates)
					return updates
				return []
		except Exception as e:
			print(f"[Telegram] getUpdates hatası: {e}")
			return []

	async def send_message_with_keyboard(
		self, 
		text: str, 
		keyboard: List[List[Dict[str, str]]], 
		chat_id: str = None
	) -> Optional[dict]:
		"""
		Inline keyboard butonları ile mesaj gönderir.
		keyboard format: [[{"text": "Button", "callback_data": "action"}]]
		"""
		target_chat = chat_id or self.chat_id
		if not self.bot_token or not target_chat:
			self.last_error = "Bot token veya chat ID boş"
			return None
		
		try:
			reply_markup = {
				"inline_keyboard": keyboard
			}
			
			async with httpx.AsyncClient(timeout=15.0) as client:
				resp = await client.post(
					f"{self.base_url}/sendMessage",
					json={
						"chat_id": target_chat,
						"text": text,
						"parse_mode": "HTML",
						"reply_markup": reply_markup
					}
				)
				resp.raise_for_status()
				result = resp.json()
				self.last_error = None
				return result
		except httpx.HTTPStatusError as e:
			self.last_error = f"HTTP Hatası {e.response.status_code}: {e.response.text}"
			print(f"[Telegram] Hata: {self.last_error}")
			return None
		except Exception as e:
			self.last_error = str(e)
			print(f"[Telegram] Hata: {self.last_error}")
			return None

	async def answer_callback_query(
		self, 
		callback_query_id: str, 
		text: str = None, 
		show_alert: bool = False
	) -> bool:
		"""
		Callback query'yi yanıtlar (buton tıklaması onayı).
		"""
		if not self.bot_token:
			return False
		
		try:
			data = {"callback_query_id": callback_query_id}
			if text:
				data["text"] = text
			data["show_alert"] = show_alert
			
			async with httpx.AsyncClient(timeout=10.0) as client:
				resp = await client.post(
					f"{self.base_url}/answerCallbackQuery",
					json=data
				)
				resp.raise_for_status()
				return True
		except Exception as e:
			print(f"[Telegram] answerCallbackQuery hatası: {e}")
			return False

	async def edit_message_text(
		self,
		chat_id: str,
		message_id: int,
		text: str,
		keyboard: List[List[Dict[str, str]]] = None
	) -> Optional[dict]:
		"""
		Mevcut bir mesajı düzenler.
		"""
		if not self.bot_token:
			return None
		
		try:
			data = {
				"chat_id": chat_id,
				"message_id": message_id,
				"text": text,
				"parse_mode": "HTML"
			}
			
			if keyboard:
				data["reply_markup"] = {"inline_keyboard": keyboard}
			
			async with httpx.AsyncClient(timeout=10.0) as client:
				resp = await client.post(
					f"{self.base_url}/editMessageText",
					json=data
				)
				resp.raise_for_status()
				return resp.json()
		except Exception as e:
			print(f"[Telegram] editMessageText hatası: {e}")
			return None

	async def send_message(self, text: str) -> Optional[dict]:
		if not self.bot_token or not self.chat_id:
			self.last_error = "Bot token veya chat ID boş"
			print(f"[Telegram] Hata: {self.last_error}")
			return None
		try:
			print(f"[Telegram] Mesaj gönderiliyor: {text[:50]}...")
			print(f"[Telegram] Chat ID: {self.chat_id}")
			async with httpx.AsyncClient(timeout=15.0) as client:
				resp = await client.post(
					f"{self.base_url}/sendMessage",
					json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"},
				)
				resp.raise_for_status()
				result = resp.json()
				print(f"[Telegram] Mesaj başarıyla gönderildi!")
				self.last_error = None
				return result
		except httpx.HTTPStatusError as e:
			self.last_error = f"HTTP Hatası {e.response.status_code}: {e.response.text}"
			print(f"[Telegram] Hata: {self.last_error}")
			return None
		except Exception as e:
			self.last_error = str(e)
			print(f"[Telegram] Hata: {self.last_error}")
			return None

	async def send_photo(self, photo_file, caption: str = None) -> Optional[dict]:
		"""
		Sends a photo to Telegram.
		photo_file: binary file-like object (e.g. io.BytesIO)
		"""
		if not self.bot_token or not self.chat_id:
			self.last_error = "Bot token veya chat ID boş"
			return None
		
		try:
			print(f"[Telegram] Fotoğraf gönderiliyor...")
			async with httpx.AsyncClient(timeout=30.0) as client:
				files = {'photo': ('chart.png', photo_file, 'image/png')}
				data = {'chat_id': self.chat_id}
				if caption:
					data['caption'] = caption
					data['parse_mode'] = 'HTML'
				
				resp = await client.post(
					f"{self.base_url}/sendPhoto",
					data=data,
					files=files
				)
				resp.raise_for_status()
				result = resp.json()
				print(f"[Telegram] Fotoğraf başarıyla gönderildi!")
				return result
		except httpx.HTTPStatusError as e:
			self.last_error = f"HTTP Hatası {e.response.status_code}: {e.response.text}"
			print(f"[Telegram] Hata: {self.last_error}")
			return None
		except Exception as e:
			self.last_error = str(e)
			print(f"[Telegram] Hata: {self.last_error}")
			return None
