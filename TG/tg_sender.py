# ============================================================
# FILE: TG/tg_sender.py
# ROLE: Telegram outbound message sender (fire-and-forget async)
# ============================================================
import aiohttp
import asyncio
import time
import os
from c_log import UnifiedLogger

logger = UnifiedLogger("tg_sender")

# Минимальный интервал между сообщениями в секундах
MIN_SEND_INTERVAL = 0.25

class TelegramSender:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = str(chat_id)
        self.api_url = f"https://api.telegram.org/bot{self.token}"
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
            
            # 2. Если прошло меньше 0.25 сек, "спим" оставшееся время
            if elapsed < MIN_SEND_INTERVAL:
                await asyncio.sleep(MIN_SEND_INTERVAL - elapsed)

            try:
                session = await self._get_session()
                url = f"{self.api_url}/sendMessage"
                payload = {
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "HTML"
                }
                
                async with session.post(url, json=payload, timeout=10) as resp:
                    if resp.status != 200:
                        err_txt = await resp.text()
                        logger.error(f"TG API Error [{resp.status}]: {err_txt}")

            except Exception as e:
                logger.error(f"TG Send Error: {e}")
            finally:
                self._last_send_time = time.monotonic()

    async def send_document(self, file_path: str, caption: str = ""):
        """Отправка документа (например, bot_state.json)"""
        if not self.token or not self.chat_id:
            return

        async with self._lock:
            elapsed = time.monotonic() - self._last_send_time
            if elapsed < MIN_SEND_INTERVAL:
                await asyncio.sleep(MIN_SEND_INTERVAL - elapsed)

            try:
                session = await self._get_session()
                url = f"{self.api_url}/sendDocument"
                
                with open(file_path, 'rb') as f:
                    data = aiohttp.FormData()
                    data.add_field('chat_id', self.chat_id)
                    data.add_field('caption', caption)
                    data.add_field('parse_mode', 'HTML')
                    data.add_field('document', f, filename=os.path.basename(file_path))

                    async with session.post(url, data=data, timeout=20) as resp:
                        if resp.status != 200:
                            err_txt = await resp.text()
                            logger.error(f"TG Document API Error [{resp.status}]: {err_txt}")
            except Exception as e:
                logger.error(f"TG Document Send Error: {e}")
            finally:
                self._last_send_time = time.monotonic()

    async def aclose(self):
        if self._session and not self._session.closed:
            await self._session.close()