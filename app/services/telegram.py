import httpx
from typing import Optional


class TelegramNotifier:
	def __init__(self, bot_token: str, chat_id: str):
		self.bot_token = bot_token
		self.chat_id = chat_id
		self.base_url = f"https://api.telegram.org/bot{self.bot_token}"
		self.last_error = None

	def send_message(self, text: str) -> Optional[dict]:
		if not self.bot_token or not self.chat_id:
			self.last_error = "Bot token veya chat ID boş"
			print(f"[Telegram] Hata: {self.last_error}")
			return None
		try:
			print(f"[Telegram] Mesaj gönderiliyor: {text[:50]}...")
			print(f"[Telegram] Chat ID: {self.chat_id}")
			resp = httpx.post(
				f"{self.base_url}/sendMessage",
				json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"},
				timeout=15.0,
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
