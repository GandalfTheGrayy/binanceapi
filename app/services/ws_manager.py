from typing import List
from fastapi import WebSocket


class WSManager:
	def __init__(self) -> None:
		self.active: List[WebSocket] = []

	async def connect(self, ws: WebSocket) -> None:
		await ws.accept()
		self.active.append(ws)

	def disconnect(self, ws: WebSocket) -> None:
		if ws in self.active:
			self.active.remove(ws)

	async def broadcast_json(self, data) -> None:
		dead = []
		for ws in self.active:
			try:
				await ws.send_json(data)
			except Exception:
				dead.append(ws)
		for ws in dead:
			self.disconnect(ws)


ws_manager = WSManager()
