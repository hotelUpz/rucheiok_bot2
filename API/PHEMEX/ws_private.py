# ============================================================
# FILE: API/PHEMEX/ws_private.py
# ROLE: Мгновенный приватный WebSocket (Prod Version)
# ============================================================

import asyncio
import time
import json
import hmac
import hashlib
import aiohttp
from typing import Callable, Awaitable, Any, Dict
from c_log import UnifiedLogger

logger = UnifiedLogger("api")

class PhemexPrivateWS:
    WS_URL = "wss://ws.phemex.com"

    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self._stop = asyncio.Event()
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ping_task = None

    def _generate_signature(self, expiry: int) -> str:
        message = f"{self.api_key}{expiry}"
        return hmac.new(self.api_secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()

    async def _ping_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while not self._stop.is_set() and not ws.closed:
            await asyncio.sleep(15.0)
            try: await ws.send_str(json.dumps({"id": 0, "method": "server.ping", "params": []}))
            except Exception: break

    async def aclose(self):
        self._stop.set()
        if self._ping_task: self._ping_task.cancel()
        if self._ws and not self._ws.closed: await self._ws.close()
        if self._session and not self._session.closed: await self._session.close()

    # async def run(self, on_message: Callable[[Dict[str, Any]], Awaitable[None]]):
    #     self._stop.clear()
    async def run(self, on_message: Callable[[Dict[str, Any]], Awaitable[None]], on_subscribe: Callable[[], Awaitable[None]] = None):
        self._stop.clear()
        self._session = aiohttp.ClientSession()
        backoff = 1.0

        while not self._stop.is_set():
            try:
                self._ws = await self._session.ws_connect(self.WS_URL, autoping=False, max_msg_size=0)
                logger.info("🔐 Private WS Подключен. Отправляем авторизацию...")
                self._ping_task = asyncio.create_task(self._ping_loop(self._ws))
                
                expiry = int(time.time() + 60)
                await self._ws.send_str(json.dumps({
                    "id": 1001, 
                    "method": "user.auth", 
                    "params": ["API", self.api_key, self._generate_signature(expiry), expiry]
                }))
                
                auth_passed = False

                async for msg in self._ws:
                    if self._stop.is_set(): break
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            data = json.loads(msg.data)
                            
                            # Ответ на пинги биржи
                            if data.get("method") == "server.ping":
                                await self._ws.send_str(json.dumps({"id": data.get("id", 0), "result": "pong"}))
                                continue

                            msg_id = data.get("id")
                            if msg_id == 1001:
                                if data.get("error"):
                                    logger.error(f"❌ Ошибка авторизации: {data['error']}")
                                    break 
                                else:
                                    logger.info("✅ Авторизация успешна! Подписываемся на канал (aop_p.subscribe)...")
                                    auth_passed = True
                                    await self._ws.send_str(json.dumps({"id": 1002, "method": "aop_p.subscribe", "params": []}))
                                continue
                            
                            if not auth_passed: continue

                            # if msg_id == 1002:
                            #     if not data.get("error"):
                            #         logger.info("🎯 Подписка ПРИНЯТА БИРЖЕЙ! Канал USDT-фьючерсов открыт.")
                            #     else:
                            #         logger.error(f"❌ Ошибка подписки: {data['error']}")
                            #     continue

                            if msg_id == 1002:
                                if not data.get("error"):
                                    logger.info("🎯 Подписка ПРИНЯТА БИРЖЕЙ! Канал USDT-фьючерсов открыт.")
                                    # 2. Добавляем вызов коллбэка в виде независимой таски:
                                    if on_subscribe:
                                        asyncio.create_task(on_subscribe())
                                else:
                                    logger.error(f"❌ Ошибка подписки: {data['error']}")
                                continue

                            # Передаем реальные сделки (incremental/snapshot) в обработчик
                            await on_message(data)
                            
                        except json.JSONDecodeError: continue
                    elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED): break
            except asyncio.CancelledError: break
            except Exception as e:
                logger.error(f"Private WS Переподключение: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                if self._ping_task: self._ping_task.cancel()
                if self._ws and not self._ws.closed: await self._ws.close()