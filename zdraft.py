
# # git remote add origin git@github.com:hotelUpz/rucheiok_bot.git


# # ============================================================
# # FILE: CORE/ws_handler.py
# # ROLE: Мгновенная событийная обработка WebSocket (Изолированная логика)
# # ============================================================

# import asyncio
# import time
# from typing import Dict, Any
# from c_log import UnifiedLogger

# logger = UnifiedLogger("core")


# class PrivateWSHandler:
#     def __init__(self, tb):
#         self.tb = tb

#     async def _cancel_entry_remainder_once(self, symbol: str, order_id: str, pos_side: str, pos) -> None:
#         if getattr(pos, "entry_cancel_requested", False):
#             return
#         pos.entry_cancel_requested = True
#         await self.tb.state.save()
#         try:
#             await self.tb.private_client.cancel_order(symbol, order_id, pos_side)
#             logger.info(f"🧹 [{symbol}] Запрошена отмена остатка входной лимитки #{order_id[:8]}...")
#         except Exception as e:
#             logger.debug(f"[{symbol}] Ошибка отмены остатка входа: {e}")

#     async def _cancel_interference_remainder_once(self, symbol: str, order_id: str, pos_side: str, pos) -> None:
#         if getattr(pos, "interference_cancel_requested", False):
#             return
#         pos.interference_cancel_requested = True
#         await self.tb.state.save()
#         try:
#             await self.tb.private_client.cancel_order(symbol, order_id, pos_side)
#             logger.info(f"🧹 [{symbol}] Запрошена отмена остатка скупки помехи #{order_id[:8]}...")
#         except Exception as e:
#             logger.debug(f"[{symbol}] Ошибка отмены остатка помехи: {e}")

#     async def _cancel_close_remainder_once(self, symbol: str, order_id: str, pos_side: str, pos) -> None:
#         if getattr(pos, "close_cancel_requested", False):
#             return
#         pos.close_cancel_requested = True
#         await self.tb.state.save()
#         try:
#             await self.tb.private_client.cancel_order(symbol, order_id, pos_side)
#             logger.info(f"🧹 [{symbol}] Запрошена отмена остатка закрывающей лимитки #{order_id[:8]}...")
#         except Exception as e:
#             logger.debug(f"[{symbol}] Ошибка отмены остатка закрывающей лимитки: {e}")

#     async def _bootstrap_initial_tp_if_needed(self, symbol: str) -> None:
#         pass # Отключено: AverageScenario теперь ставит динамические цели самостоятельно

#     async def _finalize_external_close(self, sym: str, close_price: float) -> None:
#         self.tb.state.pending_entry_orders.pop(sym, None)
#         self.tb.state.pending_interference_orders.pop(sym, None)
#         self.tb.state.in_flight_orders.discard(sym)
#         self.tb.state.active_positions.pop(sym, None)
#         asyncio.create_task(self.tb.private_client.cancel_all_orders(sym))
#         asyncio.create_task(self.tb.state.save())
#         logger.info(f"✅ Внешнее закрытие #{sym}: состояние сброшено, монета снова доступна для новой итерации.")
#         if self.tb.tg:
#             asyncio.create_task(self.tb.tg.send_message(f"⚠️ Внешнее/ручное закрытие\n#{sym}\nPrice: {close_price}"))

#     async def _handle_entry_order(self, symbol: str, ord_info: Dict[str, Any], order_id: str, status: str, pos_side: str, cum_qty: float) -> None:
#         if self.tb.state.pending_entry_orders.get(symbol) != order_id:
#             return

#         pos = self.tb.state.active_positions.get(symbol)
#         if pos and cum_qty > 0:
#             first_fill = pos.qty <= 0
#             pos.qty = cum_qty
#             if first_fill:
#                 pos.opened_at = time.time()
#             pos.entry_finalized = True
#             logger.info(f"🟢 [ВХОД] {symbol}. Статус: {status}, Исполненный объем: {cum_qty}")

#         if status == "PartiallyFilled" and pos and cum_qty > 0:
#             asyncio.create_task(self._cancel_entry_remainder_once(symbol, order_id, pos_side, pos))
#             asyncio.create_task(self._bootstrap_initial_tp_if_needed(symbol))
#             return

#         if status in ("Filled", "Canceled", "Rejected", "Deactivated"):
#             self.tb.state.pending_entry_orders.pop(symbol, None)
#             if pos:
#                 was_requested = pos.entry_cancel_requested
#                 pos.entry_cancel_requested = False
#                 pos.entry_finalized = cum_qty > 0

#             if cum_qty > 0 and pos:
#                 if status == "Filled":
#                     logger.info(f"✅ [{symbol}] Вход исполнен полностью. Рабочий объем: {cum_qty}")
#                 else:
#                     logger.info(f"✅ [{symbol}] Вход подтвержден (частично). Остаток снят. Рабочий объем: {cum_qty}")
#             else:
#                 if not was_requested:
#                     logger.warning(f"🗑 [{symbol}] Входной ордер отменен/отклонен биржей. Удаляем кэш.")
#                 self.tb.state.active_positions.pop(symbol, None)

#             await self.tb.state.save()

#             if cum_qty > 0:
#                 await self._bootstrap_initial_tp_if_needed(symbol)

#     async def _handle_interference_order(self, symbol: str, order_id: str, status: str, pos_side: str, exec_qty: float, cum_qty: float, price_rp: float) -> None:
#         if self.tb.state.pending_interference_orders.get(symbol) != order_id:
#             return

#         pos = self.tb.state.active_positions.get(symbol)
        
#         # Обновляем объемы, если налило (но ТП пока не трогаем!)
#         if status in ("Filled", "PartiallyFilled") and pos and cum_qty > 0:
#             added_qty = cum_qty - pos.interf_bought_qty if pos.interf_bought_qty < cum_qty else exec_qty
#             if added_qty > 0:
#                 pos.qty += added_qty
#                 pos.interf_bought_qty += added_qty
#                 logger.info(f"🛒 [ИНТЕРФЕРЕНЦИЯ] {symbol}. Долито: {added_qty}. Новый общий объем: {pos.qty}")

#         if status == "PartiallyFilled" and pos and cum_qty > 0:
#             asyncio.create_task(self._cancel_interference_remainder_once(symbol, order_id, pos_side, pos))
#             return

#         if status == "Filled":
#             self.tb.state.pending_interference_orders.pop(symbol, None)
#             if pos:
#                 pos.interference_cancel_requested = False
#             await self.tb.state.save()
#             # ✅ Обновляем ТП строго ОДИН РАЗ, когда скупка полностью завершена
#             asyncio.create_task(self.tb.executor.update_tp_after_interference(symbol))

#         elif status in ("Canceled", "Rejected", "Deactivated"):
#             self.tb.state.pending_interference_orders.pop(symbol, None)
#             if pos:
#                 was_requested = pos.interference_cancel_requested
#                 pos.interference_cancel_requested = False
                
#                 # ✅ Если мы сами отменили хвост скупки, значит мы долили часть объема -> обновляем ТП
#                 if was_requested and pos.interf_bought_qty > 0:
#                     asyncio.create_task(self.tb.executor.update_tp_after_interference(symbol))
                
#                 if not was_requested:
#                     if price_rp > 0:
#                         pos.failed_interference_prices[str(price_rp)] = pos.failed_interference_prices.get(str(price_rp), 0) + 1
#                     pos.place_order_fails += 1
#                     asyncio.create_task(self.tb.executor._handle_order_fail(symbol, pos))
#             await self.tb.state.save()

#     async def _handle_close_order(self, symbol: str, order_id: str, status: str, pos_side: str, price_rp: float, cum_qty: float, exec_qty: float) -> None:
#         """Обработка WS-событий для закрывающих ордеров (Average, Extrime, TTL)"""
#         pos = self.tb.state.active_positions.get(symbol)
#         if not pos or pos.close_order_id != order_id:
#             return

#         # [КРИТИЧЕСКИЙ ФИКС]: Изолируем баланс от задержек WS. Сразу вычитаем исполненный объем.
#         if status in ("Filled", "PartiallyFilled") and exec_qty > 0:
#             pos.qty = max(0.0, pos.qty - exec_qty)
#             logger.debug(f"[{symbol}] Учет исполнения: -{exec_qty}. Остаток позы: {pos.qty}")

#         if status == "PartiallyFilled":
#             # Ордер частично налился, отменяем остаток, чтобы Average пересчитал стакан
#             if cum_qty > 0:
#                 asyncio.create_task(self._cancel_close_remainder_once(symbol, order_id, pos_side, pos))
#             return

#         if status == "Filled" or pos.qty <= 0:
#             # Позиция полностью закрыта
#             self.tb.state.active_positions.pop(symbol, None)
#             logger.info(f"✅ [{symbol}] Позиция ПОЛНОСТЬЮ ЗАКРЫТА по цели.")
#             if self.tb.tg:
#                 pnl_str = f"+{price_rp}" if price_rp > 0 else str(price_rp)
#                 close_price = pos.current_close_price
#                 asyncio.create_task(self.tb.tg.send_message(
#                     f"✅ <b>Тейк-профит исполнен!</b>\nМонета: #{symbol}\n"
#                     f"PnL: {pnl_str}. Цена выхода: {close_price}"
#                 ))
#             await self.tb.state.save()
#             return

#         if status in ("Canceled", "Rejected", "Deactivated"):
#             # Ордер снят (нами или биржей), освобождаем слот для AverageScenario
#             pos.close_order_id = None
#             pos.close_cancel_requested = False
            
#             # Страховка на случай рассинхрона
#             if pos.qty <= 0:
#                 self.tb.state.active_positions.pop(symbol, None)
                
#             await self.tb.state.save()
#             return

#     async def handle_message(self, payload: Dict[str, Any]):
#         if "id" in payload or "index_market24h" in payload:
#             return

#         orders = payload.get("orders_p", payload.get("orders", []))
#         positions_ws = payload.get("positions_p", payload.get("positions", []))
#         if not orders and not positions_ws:
#             return

#         external_fills = {}

#         for ord_info in orders:
#             symbol = ord_info.get("symbol")
#             if not symbol:
#                 continue

#             is_our_order = (
#                 symbol in self.tb.state.active_positions
#                 or symbol in self.tb.state.pending_entry_orders
#                 or symbol in self.tb.state.pending_interference_orders
#             )
#             if not is_our_order:
#                 continue

#             order_id = str(ord_info.get("orderID", ""))
#             status = ord_info.get("ordStatus", "")
#             pos_side = ord_info.get("posSide", ord_info.get("side", ""))
#             exec_qty = float(ord_info.get("execQty", 0))
#             cum_qty = float(ord_info.get("cumQtyRq", ord_info.get("cumQty", exec_qty)))
#             price_rp = float(ord_info.get("execPriceRp") or ord_info.get("priceRp") or ord_info.get("price") or 0.0)

#             if status not in ("New", "Untriggered", "Triggered"):
#                 logger.debug(
#                     f"📝 [WS ORD] {symbol} | ID: {order_id[:8]}... | Status: {status} | execQty: {exec_qty} | cumQty: {cum_qty} | Price: {price_rp}"
#                 )

#             if status == "Filled":
#                 external_fills[symbol] = price_rp

#             if symbol in self.tb.state.pending_entry_orders:
#                 await self._handle_entry_order(symbol, ord_info, order_id, status, pos_side, cum_qty)
#                 continue

#             if symbol in self.tb.state.pending_interference_orders:
#                 await self._handle_interference_order(symbol, order_id, status, pos_side, exec_qty, cum_qty, price_rp)
#                 continue

#             if symbol in self.tb.state.active_positions:
#                 await self._handle_close_order(symbol, order_id, status, pos_side, price_rp, cum_qty)

#         for p in positions_ws:
#             sym = p.get("symbol")
#             if not sym or sym not in self.tb.state.active_positions:
#                 continue
#             if "sizeRq" not in p and "size" not in p:
#                 continue

#             real_size = abs(float(p.get("sizeRq", p.get("size", 0))))
#             pos_side_ws = p.get("posSide", p.get("side", ""))
#             side_ws = "LONG" if pos_side_ws in ("Long", "Buy", "long") else "SHORT"

#             pos = self.tb.state.active_positions[sym]
#             if pos.side != side_ws:
#                 continue

#             if real_size == 0:
#                 close_price = external_fills.get(sym) or self.tb.phemex_prices.get(sym, pos.current_close_price)
#                 if close_price <= 0:
#                     close_price = pos.entry_price
#                 logger.warning(f"⚠️ [{sym}] ВНЕШНЕЕ ЗАКРЫТИЕ! Сторона {side_ws} обнулилась.")
#                 await self._finalize_external_close(sym, close_price)
#             elif pos.qty != real_size and sym not in self.tb.state.pending_entry_orders:
#                 logger.debug(f"🔄 [{sym}] Объем синхронизирован: Было {pos.qty} -> Стало {real_size}")
#                 pos.qty = real_size

#     def _process_pnl(self, sym: str, is_loss: bool, close_price: float):
#         if is_loss:
#             self.tb.state.consecutive_fails[sym] = self.tb.state.consecutive_fails.get(sym, 0) + 1
#             q_cfg = self.tb.cfg.get("risk", {}).get("quarantine", {})
#             max_fails = q_cfg.get("max_consecutive_fails")
#             if max_fails is not None and self.tb.state.consecutive_fails[sym] >= max_fails:
#                 q_hours = q_cfg.get("quarantine_hours", 24)
#                 if str(q_hours).lower() == "inf":
#                     self.tb.state.quarantine_until[sym] = float("inf")
#                     self.tb.black_list.append(sym)
#                     q_msg = "Навсегда (BlackList)"
#                 else:
#                     self.tb.state.quarantine_until[sym] = time.time() + float(q_hours) * 3600
#                     q_msg = f"на {q_hours} ч."
#                 logger.warning(f"🚨 КАРАНТИН #{sym}: {q_msg} (Failures: {self.tb.state.consecutive_fails[sym]})")
#                 if self.tb.tg:
#                     asyncio.create_task(self.tb.tg.send_message(f"☣️ <b>Карантин</b>\nМонета: #{sym}\nСрок: {q_msg}"))
#         else:
#             self.tb.state.consecutive_fails[sym] = 0

#         self.tb.state.pending_entry_orders.pop(sym, None)
#         self.tb.state.pending_interference_orders.pop(sym, None)
#         self.tb.state.in_flight_orders.discard(sym)
#         self.tb.state.active_positions.pop(sym, None)
#         asyncio.create_task(self.tb.state.save())

#         logger.info(f"✅ Цикл #{sym} завершен. Монета свободна.")
#         if self.tb.tg:
#             asyncio.create_task(self.tb.tg.send_message(f"🔴 Выход \n#{sym}\nPrice: {close_price}"))


# # ============================================================
# # FILE: CORE/executor.py
# # ROLE: Отправка ордеров на биржу и защита от гонок потоков (Locks)
# # ============================================================

# import asyncio
# from typing import Dict
# from c_log import UnifiedLogger
# from CORE.models import ActivePosition
# from API.PHEMEX.stakan import DepthTop
# from utils import round_step

# logger = UnifiedLogger("core")

# class OrderExecutor:
#     def __init__(self, tb):
#         self.tb = tb
#         self.locks: Dict[str, asyncio.Lock] = {}

#     def get_lock(self, symbol: str) -> asyncio.Lock:
#         if symbol not in self.locks:
#             self.locks[symbol] = asyncio.Lock()
#         return self.locks[symbol]

#     async def _handle_order_fail(self, symbol: str, pos: ActivePosition):
#         """Централизованный диспетчер аварий при постановке ордеров"""
#         max_fails = self.tb.cfg.get("app", {}).get("max_place_order_retries", 5)
        
#         if pos.place_order_fails >= max_fails:
#             if pos.qty > 0:
#                 if not pos.in_extrime_mode:
#                     logger.error(f"[{symbol}] ⚠️ Лимит ошибок постановки ({max_fails}) исчерпан! Перевод в EXTRIME MODE.")
#                     pos.in_extrime_mode = True
#                     pos.in_breakeven_mode = False
#                     pos.place_order_fails = 0 
#                     if self.tb.tg:
#                         asyncio.create_task(self.tb.tg.send_message(f"🆘 <b>Авария Ордеров</b>\n#{symbol}: Ошибки API. Включен Экстрим."))
#                 else:
#                     # [ФИКС ИНВАРИАНТА]: МЫ В РЫНКЕ! НЕ УДАЛЯЕМ СТЕЙТ! ОРЕМ И ЖДЕМ ОПЕРАТОРА.
#                     logger.error(f"[{symbol}] 🚨 КРИТИЧЕСКИЙ ОТКАЗ! Экстрим-ордера отклоняются биржей. Требуется ручное вмешательство! Позиция ({pos.qty} шт) СОХРАНЕНА.")
#                     if self.tb.tg:
#                         asyncio.create_task(self.tb.tg.send_message(f"🚨 <b>КРИТИЧЕСКАЯ АВАРИЯ</b>\n#{symbol}: Экстрим-ордера отклоняются биржей! Проверьте баланс/лимиты руками!"))
#             else:
#                 # Позиция не открылась (входная лимитка реджектнулась до исполнения). Можно удалять.
#                 logger.error(f"[{symbol}] 🗑 Ошибки API до открытия позиции (объем 0). Сбрасываем стейт.")
#                 self.tb.state.active_positions.pop(symbol, None)
                
#         await self.tb.state.save()

#     async def _cancel_current_tp(self, symbol: str, pos: ActivePosition, pos_side: str):
#         if pos.close_order_id:
#             logger.debug(f"[{symbol}] Запрос отмены старого ТП #{pos.close_order_id[:8]}...")
#             pos.close_cancel_requested = True 
#             await self.tb.state.save()
#             try:
#                 await self.tb.private_client.cancel_order(symbol, pos.close_order_id, pos_side)
#             except Exception as e:
#                 logger.debug(f"[{symbol}] ТП уже исполнен или отменен: {e}")
#                 pos.close_cancel_requested = False 
#             pos.close_order_id = None

#     async def update_tp_after_interference(self, symbol: str):
#         async with self.get_lock(symbol):
#             pos = self.tb.state.active_positions.get(symbol)
#             spec = self.tb.symbol_specs.get(symbol)
#             if not pos or not spec:
#                 return

#             pos_side = "Long" if pos.side == "LONG" else "Short"

#             # СТРОГО ПО ТЗ: Мы ТОЛЬКО отменяем старый ордер.
#             # Новый ордер с обновленным объемом выставит AverageScenario ДИНАМИЧЕСКИ, когда увидит ликвидность!
#             await self._cancel_current_tp(symbol, pos, pos_side)
#             logger.info(f"🔄 [{symbol}] Отменен старый ТП после интерференции. Передача управления AverageScenario.")

#     async def restore_tp(self, symbol: str, pos: ActivePosition):
#         async with self.get_lock(symbol):
#             if pos.close_order_id:
#                 return
#             spec = self.tb.symbol_specs.get(symbol)
#             if not spec or pos.current_close_price <= 0 or pos.qty <= 0:
#                 return

#             # [КРИТИЧЕСКИЙ ФИКС]: Защита от саботажа Watchdog-ом
#             # Если Average работает, watchdog НЕ ДОЛЖЕН ставить глухую лимитку. Average сам найдет объем!
#             avg_enabled = self.tb.cfg.get("exit", {}).get("scenarious", {}).get("average", {}).get("enable", True)
#             if avg_enabled and not pos.in_extrime_mode and not pos.in_breakeven_mode:
#                 return

#             close_side = "Sell" if pos.side == "LONG" else "Buy"
#             pos_side = "Long" if pos.side == "LONG" else "Short"

#             try:
#                 resp = await self.tb.private_client.place_limit_order(
#                     symbol=symbol, side=close_side, qty=pos.qty, price=pos.current_close_price, pos_side=pos_side
#                 )
#                 order_id = str(resp.get("data", {}).get("orderID") or resp.get("result", {}).get("orderId", ""))
#                 if order_id:
#                     pos.close_order_id = order_id
#                     pos.place_order_fails = 0
#                     pos.close_cancel_requested = False # Сброс флага
#                     await self.tb.state.save()
#                     logger.warning(f"[{symbol}] 🛡 ТП восстановлен Watchdog-ом на {pos.current_close_price}!")
#             except Exception as e:
#                 logger.error(f"[{symbol}] Ошибка Watchdog: {e}")
#                 pos.place_order_fails += 1
#                 await self._handle_order_fail(symbol, pos)

#     async def handle_exit_action(self, symbol: str, action: dict):
#         async with self.get_lock(symbol):
#             pos = self.tb.state.active_positions.get(symbol)
#             spec = self.tb.symbol_specs.get(symbol)
#             if not pos or not spec or pos.qty <= 0:
#                 return

#             act = action["action"]
#             close_side = "Sell" if pos.side == "LONG" else "Buy"
#             pos_side = "Long" if pos.side == "LONG" else "Short"

#             # ОБРАБОТКА ОТМЕНЫ ЗАВИСШИХ ДИНАМИЧЕСКИХ ОРДЕРОВ ИЗ AVERAGE
#             if act == "CANCEL_CLOSE":
#                 await self._cancel_current_tp(symbol, pos, pos_side)
#                 logger.info(f"🧹 [{symbol}] Отмена зависшего динамического ордера: {action.get('reason', '')}")
#                 return

#             if act in ("UPDATE_TARGET", "PLACE_EXTRIME_LIMIT", "PLACE_DYNAMIC_CLOSE"):
#                 if act == "UPDATE_TARGET":
#                     if "price" in action:
#                         target_price = action["price"]
#                     elif pos.side == "LONG":
#                         target_price = pos.entry_price + (pos.base_target_price_100 - pos.entry_price) * action["new_rate"]
#                     else:
#                         target_price = pos.entry_price - (pos.entry_price - pos.base_target_price_100) * action["new_rate"]
#                 else:
#                     target_price = action["price"]

#                 target_price = round_step(target_price, spec.tick_size)
#                 if target_price <= 0:
#                     return

#                 if pos.close_order_id and pos.current_close_price > 0 and abs(pos.current_close_price - target_price) < max(spec.tick_size, 1e-12):
#                     return

#                 await self._cancel_current_tp(symbol, pos, pos_side)
#                 pos.current_close_price = target_price

#                 try:
#                     resp = await self.tb.private_client.place_limit_order(
#                         symbol=symbol, side=close_side, qty=pos.qty, price=target_price, pos_side=pos_side
#                     )
#                     pos.close_order_id = str(resp.get("data", {}).get("orderID") or resp.get("result", {}).get("orderId", ""))
#                     pos.place_order_fails = 0
                    
#                     # [КРИТИЧЕСКИЙ ФИКС]: Принудительно сбрасываем флаг ожидания отмены.
#                     # Иначе поздний WS-ответ от старого ордера заблокирует отмену будущих partial fills!
#                     pos.close_cancel_requested = False

#                     reason = action.get("reason", "")
#                     log_msg = (
#                         f"[{symbol}] ACTION: {reason} | Side: {pos.side} | Qty: {pos.qty} | "
#                         f"Entry: {pos.entry_price:.6f} | Base100: {pos.base_target_price_100:.6f} | "
#                         f"New Target: {target_price:.6f}"
#                     )

#                     if reason == "INITIAL_TP":
#                         logger.info(f"🎯 ПЕРВИЧНАЯ ЦЕЛЬ: {log_msg}")
#                     elif reason == "SHIFT_DEMOTION":
#                         logger.info(f"📉 СДВИГ ЦЕЛИ: {log_msg}")
#                     elif reason == "DYNAMIC_TP_HIT":
#                         logger.info(f"⚡ ДИНАМИЧЕСКОЕ ЗАКРЫТИЕ (TAKER): {log_msg}")
#                     elif reason == "TTL_BREAKEVEN":
#                         logger.warning(f"⌛ БЕЗУБЫТОК: {log_msg}")
#                     else:
#                         logger.warning(f"🆘 EXTRIME CLOSE: {log_msg}")

#                     await self.tb.state.save()
#                 except Exception as e:
#                     logger.error(f"[{symbol}] ❌ Ошибка выставления ТП: {e}")
#                     pos.place_order_fails += 1
#                     await self._handle_order_fail(symbol, pos)

#             elif act == "BUY_OUT_INTERFERENCE":
#                 if symbol in self.tb.state.pending_interference_orders:
#                     return

#                 current_notional = pos.qty * pos.entry_price
#                 notional_limit = self.tb.cfg.get("risk", {}).get("notional_limit", 5000)
#                 available = notional_limit - current_notional
#                 if available <= 0:
#                     return

#                 interf_side = "Buy" if pos.side == "LONG" else "Sell"
#                 interf_price = round_step(action['price'], spec.tick_size)
#                 if interf_price <= 0:
#                     return

#                 max_qty = available / interf_price
#                 raw_qty = min(action['qty'], max_qty)
#                 interf_qty = round_step(raw_qty, spec.lot_size)

#                 min_notional_usdt = 5.0
#                 order_value_usdt = interf_qty * interf_price

#                 if interf_qty <= 0 or order_value_usdt < min_notional_usdt:
#                     return

#                 try:
#                     resp = await self.tb.private_client.place_limit_order(
#                         symbol=symbol, side=interf_side, qty=interf_qty, price=interf_price, pos_side=pos_side
#                     )
#                     order_id = str(resp.get("data", {}).get("orderID") or resp.get("result", {}).get("orderId", ""))
#                     if order_id:
#                         self.tb.state.pending_interference_orders[symbol] = order_id
#                         pos.interference_cancel_requested = False
#                         pos.place_order_fails = 0 # Сброс при успехе
#                         logger.info(f"🛒 [{symbol}] СКУПКА ПОМЕХИ: Отправлен ордер {interf_qty} шт. по {interf_price}")
#                         await self.tb.state.save()
#                 except Exception as e:
#                     err = str(e)
#                     if "TE_NO_ENOUGH_AVAILABLE_BALANCE" in err:
#                         pos.interference_disabled = True
#                         logger.warning(f"[{symbol}] ⛔ Скупка помех отключена до конца позиции: недостаточно доступной маржи/баланса.")
#                     else:
#                         logger.error(f"[{symbol}] ❌ Ошибка скупки помехи: {e}")
#                         # Баним уровень, чтобы не долбиться в него
#                         pos.failed_interference_prices[str(interf_price)] = pos.failed_interference_prices.get(str(interf_price), 0) + 1
                    
#                     pos.place_order_fails += 1
#                     await self._handle_order_fail(symbol, pos)

#     async def execute_entry(self, symbol: str, signal: dict, depth: DepthTop):
#         spec = self.tb.symbol_specs.get(symbol)
#         if not spec:
#             return

#         try:
#             if symbol not in self.tb.state.leverage_configured:
#                 leverage_cfg = self.tb.cfg.get("risk", {}).get("leverage")
#                 if leverage_cfg is not None:
#                     leverage_val = spec.max_leverage if str(leverage_cfg).lower() == "max" else float(leverage_cfg)
#                     try:
#                         await self.tb.private_client.set_leverage(symbol, "Merged", leverage_val, mode="hedged")
#                         logger.debug(f"[{symbol}] Плечо: {leverage_val}x")
#                     except Exception:
#                         pass
#                 self.tb.state.leverage_configured.add(symbol)

#             price = round_step(signal['price'], spec.tick_size)
#             if price <= 0:
#                 return

#             margin_size_cfg = self.tb.cfg.get("risk", {}).get("margin_size", "row")
#             if str(margin_size_cfg).lower() == "row":
#                 base_vol_usdt = signal.get("row_vol_usdt", price * signal.get("row_vol_asset", 0))
#             else:
#                 base_vol_usdt = float(margin_size_cfg)

#             margin_over_size_pct = self.tb.cfg.get("risk", {}).get("margin_over_size_pct", 1)
#             target_vol_usdt = base_vol_usdt * (1 + margin_over_size_pct / 100)

#             notional_limit = self.tb.cfg.get("risk", {}).get("notional_limit", 5000)
#             if target_vol_usdt > notional_limit:
#                 target_vol_usdt = notional_limit

#             qty = round_step(target_vol_usdt / price, spec.lot_size)
#             if qty <= 0:
#                 return

#             # СТРОГО ПО ТЗ: берем базу (ask2/bid2) переданную из ENTRY/engine
#             base_target = signal.get("base_target_price_100")
#             if not base_target: # Fallback защита
#                 spr2_pct = signal.get("spr2_pct", 0)
#                 base_target = price * (1 + (spr2_pct / 100)) if signal["side"] == "LONG" else price * (1 - (spr2_pct / 100))

#             pos_side = "Long" if signal["side"] == "LONG" else "Short"
#             side = "Buy" if signal["side"] == "LONG" else "Sell"

#             resp = await self.tb.private_client.place_limit_order(symbol=symbol, side=side, qty=qty, price=price, pos_side=pos_side)
#             order_id = str(resp.get("data", {}).get("orderID") or resp.get("result", {}).get("orderId", ""))

#             if order_id:
#                 self.tb.state.pending_entry_orders[symbol] = order_id
#                 ask1, bid1 = depth.asks[0][0], depth.bids[0][0]
#                 pos = ActivePosition(
#                     symbol=symbol,
#                     side=signal["side"],
#                     entry_price=price,
#                     qty=0.0,
#                     init_qty=qty,
#                     init_ask1=ask1,
#                     init_bid1=bid1,
#                     base_target_price_100=base_target,
#                 )
#                 self.tb.exit_engine.initialize_position_state(pos)
#                 self.tb.state.active_positions[symbol] = pos
#                 await self.tb.state.save()

#                 logger.info(f"🚀 [{symbol}] ВХОД ОТПРАВЛЕН: {signal['side']} по {price} (Плановый объем: {qty})")
#                 if self.tb.tg:
#                     await self.tb.tg.send_message(
#                         f"🟢 <b>Сигнал на вход</b>\nМонета: #{symbol}\nНаправление: {signal['side']}\nЦена: {price}\nОбъем: {qty}"
#                     )
#         except Exception as e:
#             logger.error(f"[{symbol}] ❌ Критическая ошибка входа: {e}")


# import time
# from typing import Dict, Any, Optional
# from ENTRY.pattern_math import StakanEntryPattern
# from ENTRY.funding_filter import FundingFilter
# from API.PHEMEX.stakan import DepthTop

# class EntryEngine:
#     def __init__(self, cfg: Dict[str, Any], funding_api):
#         self.cfg = cfg
#         self.phemex_cfg = cfg["pattern"]["phemex"]
#         self.binance_cfg = cfg["pattern"]["binance"]
        
#         self.pattern_math = StakanEntryPattern(self.phemex_cfg)
#         self.funding_filter = FundingFilter(cfg.get("pattern", {}).get("phemex_funding_filter", {}), funding_api)
        
#         self.target_depth = self.phemex_cfg.get("depth", 8)
#         self.pattern_ttl = self.phemex_cfg.get("pattern_ttl_sec", 0)
        
#         self.binance_enabled = self.binance_cfg.get("enable", True)
#         self.min_price_spread = abs(self.binance_cfg.get("min_price_spread_pct", 0.1))
#         self.spread_ttl = self.binance_cfg.get("spread_ttl_sec", 0)
        
#         self._pattern_first_seen: Dict[str, float] = {}
#         self._spread_first_seen: Dict[str, float] = {}

#     def analyze(self, depth: DepthTop, b_price: float, p_price: float) -> Optional[Dict[str, Any]]:
#         symbol = depth.symbol
        
#         if self.funding_filter.is_blocked(symbol):
#             return None
            
#         now = time.time()
#         bids_sliced = depth.bids[:self.target_depth]
#         asks_sliced = depth.asks[:self.target_depth]
        
#         signal = self.pattern_math.analyze(bids_sliced, asks_sliced)
#         if not signal:
#             self._pattern_first_seen.pop(symbol, None)
#             self._spread_first_seen.pop(symbol, None)
#             return None

#         # СТРОГО ПО ТЗ: Извлекаем базу для ТП из второго уровня (ask2/bid2)
#         ask2 = asks_sliced[1][0] if len(asks_sliced) > 1 else asks_sliced[0][0]
#         bid2 = bids_sliced[1][0] if len(bids_sliced) > 1 else bids_sliced[0][0]
#         signal["base_target_price_100"] = ask2 if signal["side"] == "LONG" else bid2

#         passed_binance = False
#         spread_val = 0.0
#         if self.binance_enabled:
#             if b_price and p_price:
#                 spread_pct = (b_price - p_price) / p_price * 100
#                 passed_binance = (spread_pct >= self.min_price_spread) if signal["side"] == "LONG" else (spread_pct <= -self.min_price_spread)
#                 spread_val = abs(spread_pct)
#         else:
#             passed_binance = True

#         if not passed_binance:
#             self._spread_first_seen.pop(symbol, None)
#             return None

#         if self.pattern_ttl > 0:
#             first_seen_p = self._pattern_first_seen.setdefault(symbol, now)
#             if now - first_seen_p < self.pattern_ttl: return None

#         if self.binance_enabled and self.spread_ttl > 0:
#             first_seen_s = self._spread_first_seen.setdefault(symbol, now)
#             if now - first_seen_s < self.spread_ttl: return None

#         self._pattern_first_seen.pop(symbol, None)
#         self._spread_first_seen.pop(symbol, None)
        
#         signal["b_price"] = b_price
#         signal["p_price"] = p_price
#         signal["spread"] = spread_val
#         return signal

# import time
# from CORE.models import ActivePosition
# from API.PHEMEX.stakan import DepthTop

# class AverageScenario:
#     def __init__(self, cfg: dict):
#         self.cfg = cfg
#         self.enable = cfg.get("enable", True)
#         self.stab_avg = cfg.get("stabilization_ttl", 2)
#         self.shift_ttl = cfg.get("shift_ttl", 60)
#         self.shift_demotion = cfg.get("shift_demotion", 0.2)
#         self.min_target_rate = cfg.get("min_target_rate", 0.3)

#     def analyze(self, depth: DepthTop, pos: ActivePosition, now: float) -> dict | None:
#         if not self.enable:
#             return None

#         time_in_pos = now - pos.opened_at

#         # 1. ВЫЧИСЛЕНИЕ ВИРТУАЛЬНОЙ ЦЕЛИ
#         # Математически эквивалентно логике "От обратного" (целевой спред * target_rate).
#         if pos.side == "LONG":
#             virtual_tp = pos.entry_price + (pos.base_target_price_100 - pos.entry_price) * pos.current_target_rate
#         else:
#             virtual_tp = pos.entry_price - (pos.entry_price - pos.base_target_price_100) * pos.current_target_rate

#         pos.current_close_price = virtual_tp  

#         # Ждем стабилизации после входа
#         if not pos.is_average_stabilized:
#             if time_in_pos >= self.stab_avg:
#                 pos.is_average_stabilized = True
#                 pos.last_shift_ts = now
#             else:
#                 return None
        
#         # 2. ЛОГИКА СДВИГА (Shift) И КОМПРОМИССОВ
#         if now - pos.last_shift_ts >= self.shift_ttl:
#             if pos.current_target_rate <= self.min_target_rate:
#                 return {"action": "TRIGGER_EXTRIME", "reason": "MIN_TARGET_RATE_TIMEOUT"}

#             new_rate = pos.current_target_rate - self.shift_demotion
#             pos.current_target_rate = max(new_rate, self.min_target_rate)
#             pos.last_shift_ts = now
#             pos.negative_duration_sec = 0.0 
            
#             # Пересчитываем цель после понижения планки
#             if pos.side == "LONG":
#                 pos.current_close_price = pos.entry_price + (pos.base_target_price_100 - pos.entry_price) * pos.current_target_rate
#             else:
#                 pos.current_close_price = pos.entry_price - (pos.entry_price - pos.base_target_price_100) * pos.current_target_rate

#             # Если старый кусок лимитки всё еще висит в стакане — мы обязаны его отменить,
#             # чтобы расчистить путь для новой итерации хантинга.
#             if pos.close_order_id:
#                 return {"action": "CANCEL_CLOSE", "reason": "SHIFT_DEMOTION_TIMEOUT"}

#         # Если ордер в стакане, молча ждем его судьбы от ws_handler (Полный налив или Отмена остатка)
#         if pos.close_order_id:
#             return None

#         # 3. АКТИВНЫЙ ХАНТИНГ (Парсинг стакана)
#         hit = False
#         if pos.side == "LONG":
#             # Для лонга: ищем БИДЫ, которые больше или равны нашей виртуальной цели
#             if depth.bids and depth.bids[0][0] >= pos.current_close_price:
#                 hit = True
#         else:
#             # Для шорта: ищем АСКИ, которые меньше или равны нашей виртуальной цели
#             if depth.asks and depth.asks[0][0] <= pos.current_close_price:
#                 hit = True

#         if hit:
#             # ФОКУС: Кидаем заявку ровно в НАШУ virtual_tp, а не в цену бида/аска!
#             # Биржа сработает как Price Improvement (исполнит нас об лучшую цену).
#             # А если ликвидность сбежит, мы встанем ровно на нашей границе, ничего не теряя.
#             return {"action": "PLACE_DYNAMIC_CLOSE", "price": pos.current_close_price, "reason": "DYNAMIC_TP_HIT"}

#         return None



    # async def stop(self):
    #     """
    #     СТРОГО ПО ТЗ 1.13: STOP должен останавливать новые входы, 
    #     не рушить state, корректно завершать фоновые задачи.
    #     """
    #     from c_log import UnifiedLogger
    #     logger = UnifiedLogger("core")
        
    #     logger.info("🛑 Получена команда STOP. Начинаем graceful shutdown...")
    #     self._is_running = False  # Блокирует новые входы
        
    #     # Останавливаем публичные сокеты (стаканы)
    #     if hasattr(self, 'phemex_public_ws') and self.phemex_public_ws:
    #         await self.phemex_public_ws.stop()
            
    #     # Останавливаем приватные сокеты (ордера/позиции)
    #     if hasattr(self, 'phemex_private_ws') and self.phemex_private_ws:
    #         await self.phemex_private_ws.stop()

    #     # Сохраняем стейт на диск.
    #     # ВАЖНО: Мы НЕ очищаем self.state.active_positions! 
    #     # При следующем старте Recovery восстановит контроль над позициями.
    #     await self.state.save()
    #     logger.info("✅ Graceful shutdown завершен. Стейт сохранен.")



    # async def save(self):
    #     """Потокобезопасная отправка данных на диск"""
    #     async with self._lock: # <-- ДОБАВИТЬ ЭТО
    #         state_dict = {
    #             "positions": {sym: pos.to_dict() for sym, pos in self.active_positions.items() if sym not in self.black_list},
    #             "fails": dict(self.consecutive_fails),
    #             "quarantine": dict(self.quarantine_until)
    #         }
    #         await asyncio.to_thread(self._sync_save, state_dict)

    # def load(self):
    #     """Синхронная загрузка стейта при старте"""
    #     if not os.path.exists(self.filepath): return
    #     try:
    #         with open(self.filepath, "r", encoding="utf-8") as f:
    #             data = json.load(f)
            
    #         self.consecutive_fails = data.get("fails", {})
    #         self.quarantine_until = data.get("quarantine", {})
            
    #         saved_positions = data.get("positions", {})
    #         self.active_positions.clear()
    #         for sym, pos_data in saved_positions.items():
    #             if sym in self.black_list:
    #                 continue
    #             self.active_positions[sym] = ActivePosition.from_dict(pos_data)
    #     except Exception as e:
    #         logger.error(f"Ошибка чтения стейта: {e}")