# ============================================================
# FILE: CORE/ws_handler.py
# ROLE: Интерпретатор событий WebSocket. Контроллер инвариантов.
# ============================================================
from __future__ import annotations
import asyncio
import time
from typing import Dict, Any, TYPE_CHECKING
from c_log import UnifiedLogger
from CORE.bot_utils import Reporters

if TYPE_CHECKING:
    from CORE.bot import TradingBot
    from CORE.models import ActivePosition

logger = UnifiedLogger("ws")

class PrivateWSHandler:
    def __init__(self, tb: 'TradingBot') -> None:
        self.tb = tb

    async def _handle_partial_fill_cancellation(self, symbol: str, pos_key: str, order_id: str,
                                                pos_side: str, pos: ActivePosition, timeout_attr: str, context: str):
        """
        ИНВАРИАНТ: Даем лимитке время "высказаться", затем сносим остаток.
        """
        now = time.time()
        timeout = getattr(pos, timeout_attr, now)
        
        # Если время еще есть - ждем в фоне (не блокируя стакан)
        if timeout > now:
            await asyncio.sleep(timeout - now)
            
        # Проверяем, не исполнен ли он уже полностью за время сна
        is_still_pending = (
            order_id == self.tb.state.pending_entry_orders.get(pos_key) or
            order_id == pos.close_order_id or
            order_id == self.tb.state.pending_interference_orders.get(pos_key)
        )
        
        if is_still_pending:
            logger.info(f"🧹 [{pos_key}] Время '{context}' истекло. Сносим остаток #{order_id[:8]}...")
            await self.tb.executor.cancel_order_via_ws(symbol, order_id, pos_side)

    # ------------------ Жизненный цикл событий ------------------

    async def _process_entry(self, symbol: str, pos_key: str, order_id: str, status: str, pos_side: str, cum_qty: float) -> None:
        saved_id = self.tb.state.pending_entry_orders.get(pos_key)
        if saved_id == "PENDING_HTTP":
            self.tb.state.pending_entry_orders[pos_key] = order_id
        elif saved_id != order_id: return 
            
        pos = self.tb.state.active_positions.get(pos_key)
        if not pos: return

        if cum_qty > 0:
            first_fill = pos.qty <= 0
            pos.qty = cum_qty
            if first_fill: pos.opened_at = time.time()
            pos.entry_finalized = True
            logger.info(f"🟢 [ВХОД] {pos_key}. Статус: {status}, Налито: {cum_qty}")

        if status == "PartiallyFilled" and cum_qty > 0 and not getattr(pos, "entry_cancel_requested", False):
            pos.entry_cancel_requested = True
            asyncio.create_task(self._handle_partial_fill_cancellation(
                symbol, pos_key, order_id, pos_side, pos, "entry_active_until", "ВХОД"
            ))

        if status in ("Filled", "Canceled", "Rejected", "Deactivated"):
            self.tb.state.pending_entry_orders.pop(pos_key, None)
            if cum_qty <= 0:
                self.tb.state.active_positions.pop(pos_key, None)
            await self.tb.state.save()

    async def _process_close(self, symbol: str, pos_key: str, order_id: str, status: str, pos_side: str, price_rp: float, cum_qty: float, exec_qty: float) -> None:
        pos = self.tb.state.active_positions.get(pos_key)
        if not pos: return

        if status in ("Filled", "PartiallyFilled") and exec_qty > 0:
            pos.qty = max(0.0, pos.qty - exec_qty)

        if pos.qty <= 0 or status == "Filled":
            semantic = "ЗАКРЫТИЕ ЛОНГА" if pos.side == "LONG" else "ЗАКРЫТИЕ ШОРТА"
            logger.info(f"✅ [{pos_key}] {semantic} ЗАВЕРШЕНО.")
            if self.tb.tg:
                asyncio.create_task(self.tb.tg.send_message(Reporters.exit_success(pos_key, semantic, pos.current_close_price)))
            self.tb.executor.finalize_position_cycle(symbol, pos_key, price_rp)
            return

        if pos.close_order_id != order_id: return 

        if status == "PartiallyFilled" and cum_qty > 0 and not getattr(pos, "close_cancel_requested", False):
            pos.close_cancel_requested = True
            asyncio.create_task(self._handle_partial_fill_cancellation(
                symbol, pos_key, order_id, pos_side, pos, "hunting_active_until", "ВЫХОД"
            ))

        if status in ("Canceled", "Rejected", "Deactivated"):
            pos.close_order_id = None 
            pos.close_cancel_requested = False
            await self.tb.state.save()

    async def process_phemex_message(self, payload: Dict[str, Any]) -> None:
        """Точка входа (роутер)."""
        orders = payload.get("orders_p", payload.get("orders", []))
        
        for ord_info in orders:
            symbol = ord_info.get("symbol")
            if not symbol: continue
                
            order_id = str(ord_info.get("orderID", ""))
            pos_key = None
            pos_side_raw = ord_info.get("posSide", ord_info.get("side", ""))

            # Жесткий маппинг стейта
            for pk in (f"{symbol}_LONG", f"{symbol}_SHORT"):
                if order_id == self.tb.state.pending_entry_orders.get(pk) or \
                   order_id == self.tb.state.pending_interference_orders.get(pk) or \
                   (pk in self.tb.state.active_positions and self.tb.state.active_positions[pk].close_order_id == order_id):
                    pos_key = pk; break

            # Phemex Hedge Fallback
            if not pos_key and pos_side_raw in ("Long", "Short"):
                pk = f"{symbol}_{pos_side_raw.upper()}"
                if pk in self.tb.state.active_positions: pos_key = pk

            if not pos_key: continue

            status = ord_info.get("ordStatus", "")
            exec_qty = float(ord_info.get("execQty", 0))
            cum_qty = float(ord_info.get("cumQtyRq", ord_info.get("cumQty", exec_qty)))
            price_rp = float(ord_info.get("execPriceRp", 0) or ord_info.get("priceRp", 0) or ord_info.get("price", 0))

            if pos_key in self.tb.state.pending_entry_orders:
                await self._process_entry(symbol, pos_key, order_id, status, pos_side_raw, cum_qty)
            elif pos_key in self.tb.state.active_positions:
                await self._process_close(symbol, pos_key, order_id, status, pos_side_raw, price_rp, cum_qty, exec_qty)