import aiohttp
import asyncio
import time
from c_log import UnifiedLogger

logger = UnifiedLogger("tg")

# Минимальный интервал между сообщениями в секундах
MIN_SEND_INTERVAL = 0.25

class TelegramSender:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = str(chat_id)
        self.base_url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        self._session: aiohttp.ClientSession | None = None
        
        # Инструменты для контроля лимитов
        self._lock = asyncio.Lock()
        self._last_send_time = 0

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def send_message(self, text: str):
        if not self.token or not self.chat_id: 
            return

        async with self._lock:
            # 1. Считаем, сколько времени прошло с последней отправки
            elapsed = time.monotonic() - self._last_send_time
            
            # 2. Если прошло меньше 0.5 сек, "спим" оставшееся время
            if elapsed < MIN_SEND_INTERVAL:
                await asyncio.sleep(MIN_SEND_INTERVAL - elapsed)

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
                    
                    # Если словили 429 (Too Many Requests) от самого Telegram
                    if resp.status == 429:
                        # Можно добавить дополнительную логику обработки Retry-After
                        pass

            except Exception as e:
                logger.error(f"TG Send Error: {e}")
            finally:
                # 3. Обновляем время последней отправки (даже если была ошибка)
                self._last_send_time = time.monotonic()

    async def aclose(self):
        if self._session and not self._session.closed:
            await self._session.close()