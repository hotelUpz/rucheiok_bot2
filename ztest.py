# # ============================================================
# # FILE: API/PHEMEX/ws_private.py
# # ROLE: Подписка на приватные обновления Phemex (Истинный aop_p)
# # ============================================================

# import asyncio
# import time
# import json
# import hmac
# import hashlib
# import os
# import sys
# import aiohttp
# from typing import Callable, Awaitable, Any, Dict, List

# sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
# from c_log import UnifiedLogger

# logger = UnifiedLogger("ws_private")

# class PhemexPrivateWS:
#     WS_URL = "wss://ws.phemex.com"

#     # Оставили symbols_provider для обратной совместимости с bot.py
#     def __init__(self, api_key: str, api_secret: str, symbols_provider: Callable[[], List[str]] = None):
#         self.api_key = api_key
#         self.api_secret = api_secret
#         self._stop = asyncio.Event()
#         self._session: aiohttp.ClientSession | None = None
#         self._ws: aiohttp.ClientWebSocketResponse | None = None
#         self._ping_task = None

#     def _generate_signature(self, expiry: int) -> str:
#         message = f"{self.api_key}{expiry}"
#         return hmac.new(self.api_secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()

#     async def _ping_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
#         while not self._stop.is_set() and not ws.closed:
#             await asyncio.sleep(15.0)
#             try: await ws.ping()
#             except Exception: break

#     async def aclose(self):
#         self._stop.set()
#         if self._ping_task: self._ping_task.cancel()
#         if self._ws and not self._ws.closed: await self._ws.close()
#         if self._session and not self._session.closed: await self._session.close()

#     async def run(self, on_message: Callable[[Dict[str, Any]], Awaitable[None]]):
#         self._stop.clear()
#         self._session = aiohttp.ClientSession()
#         backoff = 1.0

#         while not self._stop.is_set():
#             try:
#                 self._ws = await self._session.ws_connect(self.WS_URL, autoping=False, max_msg_size=0)
#                 logger.info("🔐 Private WS Подключен. Отправляем авторизацию...")
#                 self._ping_task = asyncio.create_task(self._ping_loop(self._ws))
                
#                 expiry = int(time.time() + 60)
#                 await self._ws.send_str(json.dumps({
#                     "id": 1001, 
#                     "method": "user.auth", 
#                     "params": ["API", self.api_key, self._generate_signature(expiry), expiry]
#                 }))
                
#                 auth_passed = False

#                 async for msg in self._ws:
#                     if self._stop.is_set(): break
#                     if msg.type == aiohttp.WSMsgType.TEXT:
#                         try:
#                             data = json.loads(msg.data)
                            
#                             if data.get("method") == "server.ping":
#                                 await self._ws.send_str(json.dumps({"id": data.get("id", 0), "result": "pong"}))
#                                 continue

#                             msg_id = data.get("id")
#                             if msg_id == 1001:
#                                 if data.get("error"):
#                                     logger.error(f"❌ Ошибка авторизации: {data['error']}")
#                                     break 
#                                 else:
#                                     logger.info("✅ Авторизация успешна! Подписываемся на aop_p.subscribe...")
#                                     auth_passed = True
#                                     # ТОТ САМЫЙ ИСТИННЫЙ МЕТОД С НИЖНИМ ПОДЧЕРКИВАНИЕМ
#                                     await self._ws.send_str(json.dumps({"id": 1002, "method": "aop_p.subscribe", "params": []}))
#                                 continue
                            
#                             if not auth_passed: continue

#                             if msg_id == 1002:
#                                 if not data.get("error"):
#                                     logger.info("🎯 Подписка aop_p.subscribe ПРИНЯТА БИРЖЕЙ!")
#                                 else:
#                                     logger.error(f"❌ Ошибка подписки: {data['error']}")
#                                 continue

#                             # Передаем реальные сделки в обработчик
#                             await on_message(data)
                            
#                         except json.JSONDecodeError: continue
#                     elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED): break
#             except asyncio.CancelledError: break
#             except Exception as e:
#                 logger.error(f"Private WS Переподключение: {e}")
#                 await asyncio.sleep(backoff)
#                 backoff = min(backoff * 2, 30.0)
#             finally:
#                 if self._ping_task: self._ping_task.cancel()
#                 if self._ws and not self._ws.closed: await self._ws.close()


# # ----------------------------
# # SELF TEST (Private WS)
# # Запусти: python -m API.PHEMEX.ws_private
# # ----------------------------
# if __name__ == "__main__":
#     from dotenv import load_dotenv
#     load_dotenv()
    
#     api_key = os.getenv("API_KEY", "")
#     api_secret = os.getenv("API_SECRET", "")

#     async def _main():
#         if not api_key or not api_secret:
#             print("❌ Установите API_KEY и API_SECRET в файле .env")
#             return

#         ws = PhemexPrivateWS(api_key, api_secret)

#         async def on_msg(data):
#             if "index_market24h" in data or data.get("result") == "pong": return
            
#             print(f"\n🔔 --- ПРИВАТНЫЙ ПУШ [{time.strftime('%X')}] ---")
#             print(json.dumps(data, indent=2))

#         print("🚀 Запуск Истинного Сокета...")
#         print("💡 Иди на биржу Phemex и открой/закрой ордер USDT-фьючерса.")
        
#         task = asyncio.create_task(ws.run(on_msg))
#         try: await asyncio.sleep(3600)
#         except KeyboardInterrupt: print("\nОстановка...")
#         finally:
#             await ws.aclose()
#             await task

#     asyncio.run(_main())



        # if pos.close_order_id:
        #     return None

        # # 3. АКТИВНЫЙ ХАНТИНГ (Поиск уровня с максимальным объемом)
        # target_price = None
        # max_vol = -1.0

        # if pos.side == "LONG":
        #     # Для лонга: ищем БИД с максимальным объемом среди тех, что >= pos.current_close_price
        #     for price, vol in depth.bids:
        #         if price >= pos.current_close_price:
        #             if vol > max_vol:
        #                 max_vol = vol
        #                 target_price = price
        #         else:
        #             break # Биды отсортированы по убыванию цены, дальше смотреть нет смысла (цены хуже)
        # else:
        #     # Для шорта: ищем АСК с максимальным объемом среди тех, что <= pos.current_close_price
        #     for price, vol in depth.asks:
        #         if price <= pos.current_close_price:
        #             if vol > max_vol:
        #                 max_vol = vol
        #                 target_price = price
        #         else:
        #             break # Аски отсортированы по возрастанию цены, дальше смотреть нет смысла (цены хуже)

        # if target_price is not None:
        #     # Бьем прямо в самый плотный уровень
        #     return {"action": "PLACE_DYNAMIC_CLOSE", "price": target_price, "reason": "DYNAMIC_TP_HIT"}

        # return None