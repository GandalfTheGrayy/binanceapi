import httpx
from typing import Optional


class TelegramNotifier:
	def __init__(self, bot_token: str, chat_id: str):
		self.bot_token = bot_token
		self.chat_id = chat_id
		self.base_url = f"https://api.telegram.org/bot{self.bot_token}"

	def send_message(self, text: str) -> Optional[dict]:
		if not self.bot_token or not self.chat_id:
			return None
		try:
			resp = httpx.post(
				f"{self.base_url}/sendMessage",
				json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"},
				timeout=15.0,
			)
			resp.raise_for_status()
			return resp.json()
		except Exception:
			return None
