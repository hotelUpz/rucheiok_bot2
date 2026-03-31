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
        
        # [СКАЛЬПЕЛЬ 3]: Игнорируем PENDING_HTTP, так как это не настоящий ID ордера
        to_cancel = [oid for oid in (entry_id, interf_id, pos.close_order_id) if oid and oid != "PENDING_HTTP"]
        for oid in to_cancel:
            try: await self.tb.private_client.cancel_order(sym, oid, pos_side)
            except Exception: pass
            
        self.tb.state.active_positions.pop(pos_key, None)
        asyncio.create_task(self.tb.state.save())
        logger.info(f"✅ Внешнее закрытие #{pos_key}: состояние сброшено, сторона свободна.")
        if self.tb.tg:
            asyncio.create_task(self.tb.tg.send_message(f"⚠️ Внешнее/ручное закрытие\n#{pos_key}\nPrice: {close_price}"))

    async def _handle_entry_order(self, symbol: str, pos_key: str, ord_info: Dict[str, Any], order_id: str, status: str, pos_side: str, cum_qty: float) -> None:
        saved_id = self.tb.state.pending_entry_orders.get(pos_key)
        # [ХИРУРГИЯ 2]: Если статус PENDING_HTTP, значит вебсокет обогнал REST. Забираем ID!
        if saved_id == "PENDING_HTTP":
            self.tb.state.pending_entry_orders[pos_key] = order_id
        elif saved_id != order_id:
            return # Чужой или старый ордер
            
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
                pos.entry_finalized = cum_qty > 0

            if cum_qty > 0 and pos:
                if status == "Filled": logger.info(f"✅ [{pos_key}] Вход исполнен полностью. Объем: {cum_qty}")
                else: logger.info(f"✅ [{pos_key}] Вход подтвержден (частично). Ордер снят. Объем: {cum_qty}")
            else:
                logger.warning(f"🗑 [{pos_key}] Входной ордер снят без налива. Удаляем кэш.")
                self.tb.state.active_positions.pop(pos_key, None)
                
            await self.tb.state.save()

    async def _handle_close_order(self, symbol: str, pos_key: str, order_id: str, status: str, pos_side: str, price_rp: float, cum_qty: float, exec_qty: float) -> None:
        pos = self.tb.state.active_positions.get(pos_key)
        if not pos: return
        
        semantic = "ЗАКРЫТИЕ ЛОНГА" if pos.side == "LONG" else "ЗАКРЫТИЕ ШОРТА"

        # 1. Мгновенно учитываем налив (от любого ордера - старого или нового)
        if status in ("Filled", "PartiallyFilled") and exec_qty > 0:
            pos.qty = max(0.0, pos.qty - exec_qty)
            logger.debug(f"[{pos_key}] Учет исполнения (актуального или запоздалого): -{exec_qty}. Остаток: {pos.qty}")

        # 2. ЗОЛОТОЕ ПРАВИЛО: Если объем иссяк — фиксируем финал цикла!
        # Неважно, какой статус или чей это был ордер. Нет объема - нет позиции.
        if pos.qty <= 0 or status == "Filled":
            is_loss = False
            if price_rp > 0:
                if pos.side == "LONG" and price_rp < pos.entry_price: is_loss = True
                if pos.side == "SHORT" and price_rp > pos.entry_price: is_loss = True

            pnl_str = f"+{price_rp}" if not is_loss else str(price_rp)
            logger.info(f"✅ [{pos_key}] {semantic} ПОЛНОСТЬЮ ЗАВЕРШЕНО.")
            
            if self.tb.tg:
                asyncio.create_task(self.tb.tg.send_message(
                    f"✅ <b>Тейк-профит исполнен! ({semantic})</b>\nМонета: #{symbol}\n"
                    f"PnL: {pnl_str}. Цена выхода: {pos.current_close_price}"
                ))
            
            self._process_pnl(symbol, pos_key, is_loss, pos.current_close_price)
            return

        # ---- НИЖЕ ЛОГИКА ТОЛЬКО ДЛЯ АКТУАЛЬНОГО ТЕКУЩЕГО ОРДЕРА ----
        if pos.close_order_id != order_id:
            return # Старый ордер, просто учли его объем выше и забыли

        # 3. Отмена остатков при частично наливе
        if status == "PartiallyFilled":
            if cum_qty > 0: asyncio.create_task(self._cancel_close_remainder_once(symbol, pos_key, order_id, pos_side, pos))
            return

        # 4. Очистка завершенных ордеров
        if status in ("Canceled", "Rejected", "Deactivated"):
            pos.close_order_id = None 
            pos.close_cancel_requested = False
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

        elif status in ("Canceled", "Rejected", "Deactivated"):
            self.tb.state.pending_interference_orders.pop(pos_key, None)
            
            # Штрафуем только за явные реджекты биржи (чтобы не долбиться в стену)
            if status == "Rejected" and pos:
                if price_rp > 0: pos.failed_interference_prices[str(price_rp)] = pos.failed_interference_prices.get(str(price_rp), 0) + 1
                pos.place_order_fails += 1
                asyncio.create_task(self.tb.executor._handle_order_fail(symbol, pos_key, pos))
                
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
            order_id = str(ord_info.get("orderID", ""))
            
            pos_key = None
            pos_side_raw = ord_info.get("posSide", ord_info.get("side", ""))

            # 1. ЖЕЛЕЗОБЕТОННЫЙ ПОИСК (Прямое совпадение ID в стейте)
            for pk in (f"{symbol}_LONG", f"{symbol}_SHORT"):
                if order_id == self.tb.state.pending_entry_orders.get(pk): pos_key = pk; break
                if order_id == self.tb.state.pending_interference_orders.get(pk): pos_key = pk; break
                if pk in self.tb.state.active_positions and self.tb.state.active_positions[pk].close_order_id == order_id:
                    pos_key = pk; break

            # 2. ФОЛБЭК ДЛЯ ЗАПОЗДАЛЫХ ОРДЕРОВ (Если Phemex прислал Hedge-направление)
            if not pos_key and pos_side_raw in ("Long", "Short"):
                pos_key = f"{symbol}_{pos_side_raw.upper()}"
                if pos_key not in self.tb.state.active_positions: pos_key = None

            # 3. ФОЛБЭК ДЛЯ ONE-WAY MODE (Направления нет, ищем кто сейчас торгуется)
            if not pos_key:
                l_exists = f"{symbol}_LONG" in self.tb.state.active_positions
                s_exists = f"{symbol}_SHORT" in self.tb.state.active_positions
                if l_exists and not s_exists: pos_key = f"{symbol}_LONG"
                elif s_exists and not l_exists: pos_key = f"{symbol}_SHORT"

            if not pos_key: continue

            status = ord_info.get("ordStatus", "")
            exec_qty = float(ord_info.get("execQty", 0))
            cum_qty = float(ord_info.get("cumQtyRq", ord_info.get("cumQty", exec_qty)))
            price_rp = float(ord_info.get("execPriceRp") or ord_info.get("priceRp") or ord_info.get("price") or 0.0)

            if status not in ("New", "Untriggered", "Triggered"):
                logger.debug(f"📝 [WS ORD] {pos_key} | ID: {order_id[:8]}... | Status: {status} | execQty: {exec_qty} | cumQty: {cum_qty}")

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