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

# ============================================================
# FILE: CORE/ws_handler.py
# ROLE: Мгновенная событийная обработка WebSocket (Изолированная логика)
# ============================================================

import asyncio
import time
from typing import Dict, Any
from c_log import UnifiedLogger

logger = UnifiedLogger("core")


class PrivateWSHandler:
    def __init__(self, tb):
        self.tb = tb

    async def _cancel_entry_remainder_once(self, symbol: str, pos_key: str, order_id: str, pos_side: str, pos) -> None:
        if getattr(pos, "entry_cancel_requested", False): return
        pos.entry_cancel_requested = True
        await self.tb.state.save()
        try:
            await self.tb.private_client.cancel_order(symbol, order_id, pos_side)
            logger.info(f"🧹 [{pos_key}] Запрошена отмена остатка входной лимитки #{order_id[:8]}...")
        except Exception as e:
            logger.debug(f"[{pos_key}] Ошибка отмены остатка входа: {e}")

    async def _cancel_interference_remainder_once(self, symbol: str, pos_key: str, order_id: str, pos_side: str, pos) -> None:
        if getattr(pos, "interference_cancel_requested", False): return
        pos.interference_cancel_requested = True
        await self.tb.state.save()
        try:
            await self.tb.private_client.cancel_order(symbol, order_id, pos_side)
            logger.info(f"🧹 [{pos_key}] Запрошена отмена остатка скупки помехи #{order_id[:8]}...")
        except Exception as e:
            logger.debug(f"[{pos_key}] Ошибка отмены остатка помехи: {e}")

    async def _cancel_close_remainder_once(self, symbol: str, pos_key: str, order_id: str, pos_side: str, pos) -> None:
        if getattr(pos, "close_cancel_requested", False): return
        pos.close_cancel_requested = True
        await self.tb.state.save()
        try:
            await self.tb.private_client.cancel_order(symbol, order_id, pos_side)
            logger.info(f"🧹 [{pos_key}] Запрошена отмена остатка закрывающей лимитки #{order_id[:8]}...")
        except Exception as e:
            logger.debug(f"[{pos_key}] Ошибка отмены остатка закрывающей лимитки: {e}")

    async def _bootstrap_initial_tp_if_needed(self, symbol: str) -> None:
        pass 

    async def _finalize_external_close(self, sym: str, pos_key: str, pos, close_price: float) -> None:
        entry_id = self.tb.state.pending_entry_orders.pop(pos_key, None)
        interf_id = self.tb.state.pending_interference_orders.pop(pos_key, None)
        
        pos_side = "Long" if pos.side == "LONG" else "Short"
        
        # [ФИКС Hedge-Mode]: Изолированно отменяем ТОЛЬКО ордера закрытой стороны!
        to_cancel = [oid for oid in (entry_id, interf_id, pos.close_order_id) if oid]
        for oid in to_cancel:
            try: await self.tb.private_client.cancel_order(sym, oid, pos_side)
            except Exception: pass
            
        self.tb.state.active_positions.pop(pos_key, None)
        asyncio.create_task(self.tb.state.save())
        logger.info(f"✅ Внешнее закрытие #{pos_key}: состояние сброшено, сторона свободна.")
        if self.tb.tg:
            asyncio.create_task(self.tb.tg.send_message(f"⚠️ Внешнее/ручное закрытие\n#{pos_key}\nPrice: {close_price}"))

    async def _handle_entry_order(self, symbol: str, pos_key: str, ord_info: Dict[str, Any], order_id: str, status: str, pos_side: str, cum_qty: float) -> None:
        if self.tb.state.pending_entry_orders.get(pos_key) != order_id: return
        pos = self.tb.state.active_positions.get(pos_key)
        semantic = "ОТКРЫТЬ ЛОНГ" if pos and pos.side == "LONG" else "ОТКРЫТЬ ШОРТ"

        if pos and cum_qty > 0:
            first_fill = pos.qty <= 0
            pos.qty = cum_qty
            if first_fill: pos.opened_at = time.time()
            pos.entry_finalized = True
            logger.info(f"🟢 [ВХОД: {semantic}] {pos_key}. Статус: {status}, Налито: {cum_qty}")

        if status == "PartiallyFilled" and pos and cum_qty > 0:
            asyncio.create_task(self._cancel_entry_remainder_once(symbol, pos_key, order_id, pos_side, pos))
            return

        if status in ("Filled", "Canceled", "Rejected", "Deactivated"):
            self.tb.state.pending_entry_orders.pop(pos_key, None)
            if pos:
                was_requested = pos.entry_cancel_requested
                pos.entry_cancel_requested = False
                pos.entry_finalized = cum_qty > 0

            if cum_qty > 0 and pos:
                if status == "Filled": logger.info(f"✅ [{pos_key}] Вход исполнен полностью. Объем: {cum_qty}")
                else: logger.info(f"✅ [{pos_key}] Вход подтвержден (частично). Остаток снят. Объем: {cum_qty}")
            else:
                if not was_requested: logger.warning(f"🗑 [{pos_key}] Входной ордер отменен биржей. Удаляем кэш.")
                self.tb.state.active_positions.pop(pos_key, None)
            await self.tb.state.save()

    async def _handle_interference_order(self, symbol: str, pos_key: str, order_id: str, status: str, pos_side: str, exec_qty: float, cum_qty: float, price_rp: float) -> None:
        if self.tb.state.pending_interference_orders.get(pos_key) != order_id: return
        pos = self.tb.state.active_positions.get(pos_key)
        
        if status in ("Filled", "PartiallyFilled") and pos and cum_qty > 0:
            added_qty = cum_qty - pos.interf_bought_qty if pos.interf_bought_qty < cum_qty else exec_qty
            if added_qty > 0:
                pos.qty += added_qty
                pos.interf_bought_qty += added_qty
                logger.info(f"🛒 [ИНТЕРФЕРЕНЦИЯ] {pos_key}. Долито: {added_qty}. Новый общий объем: {pos.qty}")

        if status == "PartiallyFilled" and pos and cum_qty > 0:
            asyncio.create_task(self._cancel_interference_remainder_once(symbol, pos_key, order_id, pos_side, pos))
            return

        if status == "Filled":
            self.tb.state.pending_interference_orders.pop(pos_key, None)
            if pos: pos.interference_cancel_requested = False
            await self.tb.state.save()
            asyncio.create_task(self.tb.executor.update_tp_after_interference(symbol, pos_key))

        elif status in ("Canceled", "Rejected", "Deactivated"):
            self.tb.state.pending_interference_orders.pop(pos_key, None)
            if pos:
                was_requested = pos.interference_cancel_requested
                pos.interference_cancel_requested = False
                
                if was_requested and pos.interf_bought_qty > 0:
                    asyncio.create_task(self.tb.executor.update_tp_after_interference(symbol, pos_key))
                
                if not was_requested:
                    if price_rp > 0: pos.failed_interference_prices[str(price_rp)] = pos.failed_interference_prices.get(str(price_rp), 0) + 1
                    pos.place_order_fails += 1
                    asyncio.create_task(self.tb.executor._handle_order_fail(symbol, pos_key, pos))
            await self.tb.state.save()

    async def _handle_close_order(self, symbol: str, pos_key: str, order_id: str, status: str, pos_side: str, price_rp: float, cum_qty: float, exec_qty: float) -> None:
        pos = self.tb.state.active_positions.get(pos_key)
        if not pos or pos.close_order_id != order_id: return
        semantic = "ЗАКРЫТИЕ ЛОНГА" if pos.side == "LONG" else "ЗАКРЫТИЕ ШОРТА"

        if status in ("Filled", "PartiallyFilled") and exec_qty > 0:
            pos.qty = max(0.0, pos.qty - exec_qty)
            logger.debug(f"[{pos_key}] Учет исполнения: -{exec_qty}. Остаток: {pos.qty}")

        if status == "PartiallyFilled":
            if cum_qty > 0: asyncio.create_task(self._cancel_close_remainder_once(symbol, pos_key, order_id, pos_side, pos))
            return

        if status == "Filled" or pos.qty <= 0:
            self.tb.state.active_positions.pop(pos_key, None)
            logger.info(f"✅ [{pos_key}] {semantic} ПОЛНОСТЬЮ ЗАВЕРШЕНО.")
            if self.tb.tg:
                pnl_str = f"+{price_rp}" if price_rp > 0 else str(price_rp)
                asyncio.create_task(self.tb.tg.send_message(
                    f"✅ <b>Тейк-профит исполнен! ({semantic})</b>\nМонета: #{symbol}\n"
                    f"PnL: {pnl_str}. Цена выхода: {pos.current_close_price}"
                ))
            await self.tb.state.save()
            return

        if status in ("Canceled", "Rejected", "Deactivated"):
            pos.close_order_id = None
            pos.close_cancel_requested = False
            if pos.qty <= 0: self.tb.state.active_positions.pop(pos_key, None)
            await self.tb.state.save()

    async def handle_message(self, payload: Dict[str, Any]):
        if "id" in payload or "index_market24h" in payload: return

        orders = payload.get("orders_p", payload.get("orders", []))
        positions_ws = payload.get("positions_p", payload.get("positions", []))
        if not orders and not positions_ws: return

        external_fills = {}

        for ord_info in orders:
            symbol = ord_info.get("symbol")
            if not symbol: continue

            # ГЕНЕРАЦИЯ КЛЮЧА ИЗ ВЕБСОКЕТА
            pos_side_raw = ord_info.get("posSide", ord_info.get("side", ""))
            side_ws = "LONG" if pos_side_raw in ("Long", "Buy", "long") else "SHORT"
            pos_key = f"{symbol}_{side_ws}"

            is_our_order = (pos_key in self.tb.state.active_positions or pos_key in self.tb.state.pending_entry_orders or pos_key in self.tb.state.pending_interference_orders)
            if not is_our_order: continue

            order_id = str(ord_info.get("orderID", ""))
            status = ord_info.get("ordStatus", "")
            exec_qty = float(ord_info.get("execQty", 0))
            cum_qty = float(ord_info.get("cumQtyRq", ord_info.get("cumQty", exec_qty)))
            price_rp = float(ord_info.get("execPriceRp") or ord_info.get("priceRp") or ord_info.get("price") or 0.0)

            if status not in ("New", "Untriggered", "Triggered"):
                logger.debug(f"📝 [WS ORD] {pos_key} | ID: {order_id[:8]}... | Status: {status} | execQty: {exec_qty} | cumQty: {cum_qty} | Price: {price_rp}")

            if status == "Filled": external_fills[pos_key] = price_rp

            if pos_key in self.tb.state.pending_entry_orders:
                await self._handle_entry_order(symbol, pos_key, ord_info, order_id, status, pos_side_raw, cum_qty)
                continue

            if pos_key in self.tb.state.pending_interference_orders:
                await self._handle_interference_order(symbol, pos_key, order_id, status, pos_side_raw, exec_qty, cum_qty, price_rp)
                continue

            if pos_key in self.tb.state.active_positions:
                await self._handle_close_order(symbol, pos_key, order_id, status, pos_side_raw, price_rp, cum_qty, exec_qty)

        for p in positions_ws:
            sym = p.get("symbol")
            if not sym: continue
            
            pos_side_ws = p.get("posSide", p.get("side", ""))
            side_ws = "LONG" if pos_side_ws in ("Long", "Buy", "long") else "SHORT"
            pos_key = f"{sym}_{side_ws}"

            if pos_key not in self.tb.state.active_positions: continue
            if "sizeRq" not in p and "size" not in p: continue

            real_size = abs(float(p.get("sizeRq", p.get("size", 0))))
            pos = self.tb.state.active_positions[pos_key]

            if real_size == 0:
                close_price = external_fills.get(pos_key) or self.tb.phemex_prices.get(sym, pos.current_close_price)
                if close_price <= 0: close_price = pos.entry_price
                logger.warning(f"⚠️ [{pos_key}] ВНЕШНЕЕ ЗАКРЫТИЕ! Сторона {side_ws} обнулилась.")
                await self._finalize_external_close(sym, pos_key, pos, close_price)
            elif pos.qty != real_size and pos_key not in self.tb.state.pending_entry_orders:
                logger.debug(f"🔄 [{pos_key}] Объем синхронизирован: Было {pos.qty} -> Стало {real_size}")
                pos.qty = real_size

    def _process_pnl(self, sym: str, pos_key: str, is_loss: bool, close_price: float):
        # Карантин остается глобальным для всего СИМВОЛА
        if is_loss:
            self.tb.state.consecutive_fails[sym] = self.tb.state.consecutive_fails.get(sym, 0) + 1
            q_cfg = self.tb.cfg.get("risk", {}).get("quarantine", {})
            max_fails = q_cfg.get("max_consecutive_fails")
            if max_fails is not None and self.tb.state.consecutive_fails[sym] >= max_fails:
                q_hours = q_cfg.get("quarantine_hours", 24)
                if str(q_hours).lower() == "inf":
                    self.tb.state.quarantine_until[sym] = float("inf")
                    self.tb.black_list.append(sym)
                    q_msg = "Навсегда (BlackList)"
                else:
                    self.tb.state.quarantine_until[sym] = time.time() + float(q_hours) * 3600
                    q_msg = f"на {q_hours} ч."
                logger.warning(f"🚨 КАРАНТИН #{sym}: {q_msg} (Failures: {self.tb.state.consecutive_fails[sym]})")
                if self.tb.tg: asyncio.create_task(self.tb.tg.send_message(f"☣️ <b>Карантин</b>\nМонета: #{sym}\nСрок: {q_msg}"))
        else:
            self.tb.state.consecutive_fails[sym] = 0

        self.tb.state.pending_entry_orders.pop(pos_key, None)
        self.tb.state.pending_interference_orders.pop(pos_key, None)
        self.tb.state.active_positions.pop(pos_key, None)
        asyncio.create_task(self.tb.state.save())

        logger.info(f"✅ Цикл #{pos_key} завершен. Сторона свободна.")
        if self.tb.tg:
            asyncio.create_task(self.tb.tg.send_message(f"🔴 Выход \n#{pos_key}\nPrice: {close_price}"))