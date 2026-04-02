# ============================================================
# FILE: CORE/ws_handler.py
# ROLE: Мгновенная событийная обработка WebSocket (Изолированная логика)
# ============================================================
from __future__ import annotations

import asyncio
import time
from typing import Dict, Any, TYPE_CHECKING
from c_log import UnifiedLogger

if TYPE_CHECKING:
    from CORE.bot import TradingBot
    from CORE.models import ActivePosition

logger = UnifiedLogger("core")


class PrivateWSHandler:
    """Маршрутизатор приватных событий WebSocket Phemex. Обновляет состояние (стейт) без блокировок."""
    
    def __init__(self, tb: 'TradingBot') -> None:
        self.tb = tb

    async def _cancel_remainder(self, symbol: str, pos_key: str, order_id: str, pos_side: str, pos: ActivePosition, flag_attr: str, context: str) -> None:
        """Асинхронная отмена остатка ордера после частичного исполнения (PartiallyFilled)."""
        if getattr(pos, flag_attr, False): 
            return
        setattr(pos, flag_attr, True)
        await self.tb.state.save()
        try:
            await self.tb.private_client.cancel_order(symbol, order_id, pos_side)
            logger.info(f"🧹 [{pos_key}] Запрошена отмена остатка ({context}) #{order_id[:8]}...")
        except Exception as e:
            logger.debug(f"[{pos_key}] Ошибка отмены остатка ({context}): {e}")

    async def _finalize_external_close(self, sym: str, pos_key: str, pos: ActivePosition, close_price: float) -> None:
        """Полная ликвидация цикла и очистка стейта при ручном вмешательстве оператора или ликвидации."""
        entry_id: str | None = self.tb.state.pending_entry_orders.pop(pos_key, None)
        interf_id: str | None = self.tb.state.pending_interference_orders.pop(pos_key, None)
        
        pos_side: str = "Long" if pos.side == "LONG" else "Short"
        
        # Снос всех ассоциированных физических ордеров
        to_cancel = [oid for oid in (entry_id, interf_id, pos.close_order_id) if oid and oid != "PENDING_HTTP"]
        for oid in to_cancel:
            try: 
                await self.tb.private_client.cancel_order(sym, oid, pos_side)
            except Exception: 
                pass
            
        self.tb.state.active_positions.pop(pos_key, None)
        asyncio.create_task(self.tb.state.save())
        logger.info(f"✅ Внешнее закрытие #{pos_key}: состояние сброшено, сторона свободна.")
        if self.tb.tg:
            asyncio.create_task(self.tb.tg.send_message(f"⚠️ Внешнее/ручное закрытие\n#{pos_key}\nPrice: {close_price}"))

    async def _process_entry(self, symbol: str, pos_key: str, order_id: str, status: str, pos_side: str, cum_qty: float) -> None:
        """Обработка жизненного цикла открывающего ордера."""
        saved_id: str | None = self.tb.state.pending_entry_orders.get(pos_key)
        
        # Перехват быстрого вебсокета до ответа REST API
        if saved_id == "PENDING_HTTP":
            self.tb.state.pending_entry_orders[pos_key] = order_id
        elif saved_id != order_id:
            return 
            
        pos: ActivePosition | None = self.tb.state.active_positions.get(pos_key)
        if not pos: 
            return

        # Учет налитого объема
        if cum_qty > 0:
            first_fill: bool = pos.qty <= 0
            pos.qty = cum_qty
            if first_fill: 
                pos.opened_at = time.time()
            pos.entry_finalized = True
            semantic: str = "ОТКРЫТЬ ЛОНГ" if pos.side == "LONG" else "ОТКРЫТЬ ШОРТ"
            logger.info(f"🟢 [ВХОД: {semantic}] {pos_key}. Статус: {status}, Налито: {cum_qty}")

        # Триггер отмены остатка
        if status == "PartiallyFilled" and cum_qty > 0:
            asyncio.create_task(self._cancel_remainder(symbol, pos_key, order_id, pos_side, pos, "entry_cancel_requested", "ВХОД"))
            return

        # Финализация ордера (отменен/исполнен/отклонен)
        if status in ("Filled", "Canceled", "Rejected", "Deactivated"):
            self.tb.state.pending_entry_orders.pop(pos_key, None)
            pos.entry_finalized = cum_qty > 0

            if cum_qty > 0:
                if status == "Filled": 
                    logger.info(f"✅ [{pos_key}] Вход исполнен полностью. Объем: {cum_qty}")
                else: 
                    logger.info(f"✅ [{pos_key}] Вход подтвержден (частично). Ордер снят. Объем: {cum_qty}")
            else:
                logger.warning(f"🗑 [{pos_key}] Входной ордер снят без налива. Удаляем кэш.")
                self.tb.state.active_positions.pop(pos_key, None)
                
            await self.tb.state.save()

    async def _process_interference(self, symbol: str, pos_key: str, order_id: str, status: str, pos_side: str, exec_qty: float, cum_qty: float, price_rp: float) -> None:
        """Обработка ордеров скупки помех. Купленный объем плюсуется к общей позиции."""
        if self.tb.state.pending_interference_orders.get(pos_key) != order_id: 
            return
            
        pos: ActivePosition | None = self.tb.state.active_positions.get(pos_key)
        if not pos: 
            return
        
        if status in ("Filled", "PartiallyFilled") and cum_qty > 0:
            added_qty: float = cum_qty - pos.interf_bought_qty if pos.interf_bought_qty < cum_qty else exec_qty
            if added_qty > 0:
                pos.qty += added_qty
                pos.interf_bought_qty += added_qty
                logger.info(f"🛒 [ИНТЕРФЕРЕНЦИЯ] {pos_key}. Долито: {added_qty}. Новый общий объем: {pos.qty}")

        if status == "PartiallyFilled" and cum_qty > 0:
            asyncio.create_task(self._cancel_remainder(symbol, pos_key, order_id, pos_side, pos, "interference_cancel_requested", "ПОМЕХА"))
            return

        if status == "Filled":
            self.tb.state.pending_interference_orders.pop(pos_key, None)
            pos.interference_cancel_requested = False
            await self.tb.state.save()

        elif status in ("Canceled", "Rejected", "Deactivated"):
            self.tb.state.pending_interference_orders.pop(pos_key, None)
            
            if status == "Rejected":
                if price_rp > 0: 
                    pos.failed_interference_prices[str(price_rp)] = pos.failed_interference_prices.get(str(price_rp), 0) + 1
                pos.place_order_fails += 1
                asyncio.create_task(self.tb.executor._handle_order_fail(symbol, pos_key, pos))
                
            await self.tb.state.save()

    async def _process_close(self, symbol: str, pos_key: str, order_id: str, status: str, pos_side: str, price_rp: float, cum_qty: float, exec_qty: float) -> None:
        """Обработка закрывающих ордеров. Мгновенно вычитает объем и фиксирует финал цикла."""
        pos: ActivePosition | None = self.tb.state.active_positions.get(pos_key)
        if not pos: 
            return
        
        semantic: str = "ЗАКРЫТИЕ ЛОНГА" if pos.side == "LONG" else "ЗАКРЫТИЕ ШОРТА"

        # 1. Безусловный вычет исполненного объема (актуальный ордер или запоздалый налив отмененного)
        if status in ("Filled", "PartiallyFilled") and exec_qty > 0:
            pos.qty = max(0.0, pos.qty - exec_qty)
            logger.debug(f"[{pos_key}] Учет исполнения: -{exec_qty}. Остаток: {pos.qty}")

        # 2. Фиксация финала торгового цикла
        if pos.qty <= 0 or status == "Filled":
            is_loss: bool = False
            if price_rp > 0:
                if pos.side == "LONG" and price_rp < pos.entry_price: is_loss = True
                if pos.side == "SHORT" and price_rp > pos.entry_price: is_loss = True

            pnl_str: str = f"+{price_rp}" if not is_loss else str(price_rp)
            logger.info(f"✅ [{pos_key}] {semantic} ПОЛНОСТЬЮ ЗАВЕРШЕНО.")
            
            if self.tb.tg:
                asyncio.create_task(self.tb.tg.send_message(
                    f"✅ <b>Тейк-профит исполнен! ({semantic})</b>\nМонета: #{symbol}\n"
                    f"PnL: {pnl_str}. Цена выхода: {pos.current_close_price}"
                ))
            
            self._process_pnl(symbol, pos_key, is_loss, pos.current_close_price)
            return

        # ---- Игнорирование событий старых ордеров, если объем еще есть ----
        if pos.close_order_id != order_id:
            return 

        # if status == "PartiallyFilled":
        #     if cum_qty > 0: 
        #         asyncio.create_task(self._cancel_remainder(symbol, pos_key, order_id, pos_side, pos, "close_cancel_requested", "ВЫХОД"))
        #     return

        if status == "PartiallyFilled":
            if cum_qty > 0: 
                async def _delayed_cancel():
                    # Вычисляем, сколько еще осталось от hunting_timeout_sec
                    wait_time = pos.hunting_active_until - time.time()
                    if wait_time > 0:
                        await asyncio.sleep(wait_time)
                    
                    # Просыпаемся и проверяем: актуален ли еще этот ордер?
                    # Если за время сна его налили полностью (qty <= 0) 
                    # или уже отменили, то ничего не делаем.
                    if pos.close_order_id == order_id and pos.qty > 0 and not pos.close_cancel_requested:
                        await self._cancel_remainder(symbol, pos_key, order_id, pos_side, pos, "close_cancel_requested", "ВЫХОД")
                
                asyncio.create_task(_delayed_cancel())
            return

        if status in ("Canceled", "Rejected", "Deactivated"):
            pos.close_order_id = None 
            pos.close_cancel_requested = False
            await self.tb.state.save()

    async def process_phemex_message(self, payload: Dict[str, Any]) -> None:
        """Точка входа данных WebSocket. Распределяет события по ордерам и позициям."""
        if "id" in payload or "index_market24h" in payload: 
            return

        orders: list = payload.get("orders_p", payload.get("orders", []))
        positions_ws: list = payload.get("positions_p", payload.get("positions", []))
        if not orders and not positions_ws: 
            return

        external_fills: Dict[str, float] = {}

        # Обработка событий ордеров
        for ord_info in orders:
            symbol: str = ord_info.get("symbol")
            if not symbol: 
                continue
                
            order_id: str = str(ord_info.get("orderID", ""))
            pos_key: str | None = None
            pos_side_raw: str = ord_info.get("posSide", ord_info.get("side", ""))

            # Жесткий поиск по стейту
            for pk in (f"{symbol}_LONG", f"{symbol}_SHORT"):
                if order_id == self.tb.state.pending_entry_orders.get(pk): pos_key = pk; break
                if order_id == self.tb.state.pending_interference_orders.get(pk): pos_key = pk; break
                if pk in self.tb.state.active_positions and self.tb.state.active_positions[pk].close_order_id == order_id:
                    pos_key = pk; break

            # Фолбэк для Phemex Hedge
            if not pos_key and pos_side_raw in ("Long", "Short"):
                pos_key = f"{symbol}_{pos_side_raw.upper()}"
                if pos_key not in self.tb.state.active_positions: 
                    pos_key = None

            # Фолбэк для One-Way
            if not pos_key:
                l_ex = f"{symbol}_LONG" in self.tb.state.active_positions
                s_ex = f"{symbol}_SHORT" in self.tb.state.active_positions
                if l_ex and not s_ex: pos_key = f"{symbol}_LONG"
                elif s_ex and not l_ex: pos_key = f"{symbol}_SHORT"

            if not pos_key: 
                continue

            status: str = ord_info.get("ordStatus", "")
            exec_qty: float = float(ord_info.get("execQty", 0))
            cum_qty: float = float(ord_info.get("cumQtyRq", ord_info.get("cumQty", exec_qty)))
            price_rp: float = float(ord_info.get("execPriceRp") or ord_info.get("priceRp") or ord_info.get("price") or 0.0)

            if status not in ("New", "Untriggered", "Triggered"):
                logger.debug(f"📝 [WS ORD] {pos_key} | ID: {order_id[:8]}... | Status: {status} | execQty: {exec_qty} | cumQty: {cum_qty}")

            if status == "Filled": 
                external_fills[pos_key] = price_rp

            if pos_key in self.tb.state.pending_entry_orders:
                await self._process_entry(symbol, pos_key, order_id, status, pos_side_raw, cum_qty)
            elif pos_key in self.tb.state.pending_interference_orders:
                await self._process_interference(symbol, pos_key, order_id, status, pos_side_raw, exec_qty, cum_qty, price_rp)
            elif pos_key in self.tb.state.active_positions:
                await self._process_close(symbol, pos_key, order_id, status, pos_side_raw, price_rp, cum_qty, exec_qty)

        # Обработка событий позиций (страховка баланса)
        for p in positions_ws:
            sym: str = p.get("symbol")
            if not sym: 
                continue
            
            pos_side_ws: str = p.get("posSide", p.get("side", ""))
            side_ws: str = "LONG" if pos_side_ws in ("Long", "Buy", "long") else "SHORT"
            pos_key: str = f"{sym}_{side_ws}"

            if pos_key not in self.tb.state.active_positions: 
                continue
            if "sizeRq" not in p and "size" not in p: 
                continue

            real_size: float = abs(float(p.get("sizeRq", p.get("size", 0))))
            pos: ActivePosition = self.tb.state.active_positions[pos_key]

            if real_size == 0:
                close_price: float = external_fills.get(pos_key) or self.tb.phemex_prices.get(sym, pos.current_close_price)
                if close_price <= 0: close_price = pos.entry_price
                logger.warning(f"⚠️ [{pos_key}] ВНЕШНЕЕ ЗАКРЫТИЕ! Сторона {side_ws} обнулилась.")
                await self._finalize_external_close(sym, pos_key, pos, close_price)
            elif pos.qty != real_size and pos_key not in self.tb.state.pending_entry_orders:
                logger.debug(f"🔄 [{pos_key}] Объем синхронизирован: Было {pos.qty} -> Стало {real_size}")
                pos.qty = real_size

    def _process_pnl(self, sym: str, pos_key: str, is_loss: bool, close_price: float) -> None:
        """Обработка результатов торгового цикла. Зачистка хвостов и Карантин."""
        
        # [СКАЛЬПЕЛЬ]: Уничтожаем все физические ордера-хвосты (Помехи/Входы), если позиция закрылась
        pos = self.tb.state.active_positions.get(pos_key)
        if pos:
            pos_side = "Long" if pos.side == "LONG" else "Short"
            interf_id = self.tb.state.pending_interference_orders.get(pos_key)
            if interf_id and interf_id != "PENDING_HTTP":
                asyncio.create_task(self.tb.private_client.cancel_order(sym, interf_id, pos_side))
                
            entry_id = self.tb.state.pending_entry_orders.get(pos_key)
            if entry_id and entry_id != "PENDING_HTTP":
                asyncio.create_task(self.tb.private_client.cancel_order(sym, entry_id, pos_side))

        if is_loss:
            self.tb.state.consecutive_fails[sym] = self.tb.state.consecutive_fails.get(sym, 0) + 1
            q_cfg: dict = self.tb.cfg.get("risk", {}).get("quarantine", {})
            max_fails: int | None = q_cfg.get("max_consecutive_fails")
            
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
                if self.tb.tg: 
                    asyncio.create_task(self.tb.tg.send_message(f"☣️ <b>Карантин</b>\nМонета: #{sym}\nСрок: {q_msg}"))
        else:
            self.tb.state.consecutive_fails[sym] = 0

        # Очистка следов цикла из стейта
        self.tb.state.pending_entry_orders.pop(pos_key, None)
        self.tb.state.pending_interference_orders.pop(pos_key, None)
        self.tb.state.active_positions.pop(pos_key, None)
        asyncio.create_task(self.tb.state.save())

        logger.info(f"✅ Цикл #{pos_key} завершен. Сторона свободна.")
        if self.tb.tg:
            asyncio.create_task(self.tb.tg.send_message(f"🔴 Выход \n#{pos_key}\nPrice: {close_price}"))