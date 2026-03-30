import aiohttp
import asyncio
from c_log import UnifiedLogger

logger = UnifiedLogger("tg")

class TelegramSender:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = str(chat_id)
        self.base_url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def send_message(self, text: str):
        if not self.token or not self.chat_id: return
        try:
            session = await self._get_session()
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML"
            }
            async with session.post(self.base_url, json=payload, timeout=10) as resp:
                if resp.status != 200:
                    err_txt = await resp.text()
                    logger.error(f"TG API Error [{resp.status}]: {err_txt}")
        except Exception as e:
            logger.error(f"TG Send Error: {e}")

    async def aclose(self):
        if self._session and not self._session.closed:
            await self._session.close()