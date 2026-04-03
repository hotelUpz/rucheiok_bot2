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

        # Инвариант: Даем лимитке высказаться после первого налива
        if status == "PartiallyFilled" and cum_qty > 0 and not getattr(pos, "entry_cancel_requested", False):
            pos.entry_cancel_requested = True
            # Взводим таймер устояния ордера
            pos.entry_active_until = time.time() + self.tb.executor.entry_timeout_sec
            asyncio.create_task(self._handle_partial_fill_cancellation(
                symbol, pos_key, order_id, pos_side, pos, "entry_active_until", "ВХОД"
            ))

        if status in ("Filled", "Canceled", "Rejected", "Deactivated"):
            self.tb.state.pending_entry_orders.pop(pos_key, None)
            if cum_qty <= 0:
                self.tb.state.active_positions.pop(pos_key, None)
            await self.tb.state.save()

    async def _process_interference(self, symbol: str, pos_key: str, order_id: str, status: str,
                                    pos_side: str, price_rp: float, exec_qty: float) -> None:
        """
        ИНВАРИАНТ скупки интерференции: ордер интерференции ДОБАВЛЯЕТ к позиции.
        pos.qty += exec_qty (не вычитать! это не закрытие, а доливка).
        Маршрут: pending_interference_orders → этот метод (не _process_close).
        """
        pos = self.tb.state.active_positions.get(pos_key)
        if not pos:
            return

        # Фикс #1 TECH_DEBT: обновляем PENDING_HTTP → реальный order_id
        saved_id = self.tb.state.pending_interference_orders.get(pos_key)
        if saved_id == "PENDING_HTTP":
            self.tb.state.pending_interference_orders[pos_key] = order_id
        elif saved_id != order_id:
            return

        if status in ("Filled", "PartiallyFilled") and exec_qty > 0:
            pos.qty += exec_qty
            pos.interf_bought_qty += exec_qty
            logger.info(
                f"🛒 [{pos_key}] ИНТЕРФЕРЕНЦИЯ ЗАПОЛНЕНА: +{exec_qty} шт. по {price_rp}. "
                f"Куплено помех всего: {pos.interf_bought_qty:.6g}. Итого позиция: {pos.qty:.6g}"
            )

        # Частичное заполнение — взводим таймер и сносим остаток
        if status == "PartiallyFilled" and exec_qty > 0 and not pos.interference_cancel_requested:
            pos.interference_cancel_requested = True
            pos.hunting_active_until = time.time() + self.tb.executor.hunting_timeout_sec
            asyncio.create_task(self._handle_partial_fill_cancellation(
                symbol, pos_key, order_id, pos_side, pos, "hunting_active_until", "ИНТЕРФЕРЕНЦИЯ"
            ))

        if status in ("Filled", "Canceled", "Rejected", "Deactivated"):
            self.tb.state.pending_interference_orders.pop(pos_key, None)
            pos.interference_cancel_requested = False
            await self.tb.state.save()

    async def _process_close(self, symbol: str, pos_key: str, order_id: str, status: str, pos_side: str, price_rp: float, cum_qty: float, exec_qty: float) -> None:
        pos = self.tb.state.active_positions.get(pos_key)
        if not pos: return

        if status in ("Filled", "PartiallyFilled") and exec_qty > 0:
            pos.qty = max(0.0, pos.qty - exec_qty)

        if status == "Filled":
            pos.qty = 0.0

        if pos.qty <= 0:
            semantic = "ЗАКРЫТИЕ ЛОНГА" if pos.side == "LONG" else "ЗАКРЫТИЕ ШОРТА"
            # Используем реальную цену исполнения (price_rp), не виртуальный ТП
            fill_price = price_rp if price_rp > 0 else pos.current_close_price
            logger.info(f"✅ [{pos_key}] {semantic} ЗАВЕРШЕНО по {fill_price}.")
            if self.tb.tg:
                asyncio.create_task(self.tb.tg.send_message(Reporters.exit_success(pos_key, semantic, fill_price)))
            self.tb.executor.finalize_position_cycle(symbol, pos_key, price_rp)
            return

        # Фикс #2 TECH_DEBT: обновляем PENDING_HTTP → реальный order_id
        if pos.close_order_id == "PENDING_HTTP":
            pos.close_order_id = order_id
        elif pos.close_order_id != order_id:
            return

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
        """
        Точка входа (роутер WS событий).

        Маршрутизация (приоритет сверху вниз):
          1. pending_interference_orders → _process_interference (ДОБАВЛЯЕТ к позиции)
          2. pending_entry_orders        → _process_entry
          3. active_positions            → _process_close
        ВАЖНО: интерференция должна проверяться ДО active_positions, иначе
        она ошибочно попадет в _process_close и уменьшит pos.qty.
        """
        orders = payload.get("orders_p", payload.get("orders", []))

        for ord_info in orders:
            symbol = ord_info.get("symbol")
            if not symbol: continue

            order_id = str(ord_info.get("orderID", ""))
            pos_key = None
            pos_side_raw = ord_info.get("posSide", ord_info.get("side", ""))

            # Жесткий маппинг стейта
            for pk in (f"{symbol}_LONG", f"{symbol}_SHORT"):
                stored_interf = self.tb.state.pending_interference_orders.get(pk)
                stored_close = self.tb.state.active_positions[pk].close_order_id if pk in self.tb.state.active_positions else None
                if order_id == self.tb.state.pending_entry_orders.get(pk) or \
                   order_id == stored_interf or stored_interf == "PENDING_HTTP" or \
                   order_id == stored_close or stored_close == "PENDING_HTTP":
                    pos_key = pk; break

            # Phemex Hedge Fallback (фикс #3 TECH_DEBT: case-insensitive сравнение)
            if not pos_key and pos_side_raw.lower() in ("long", "short"):
                pk = f"{symbol}_{pos_side_raw.upper()}"
                if pk in self.tb.state.active_positions: pos_key = pk

            if not pos_key: continue

            status = ord_info.get("ordStatus", "")
            exec_qty = float(ord_info.get("execQty", 0))
            cum_qty = float(ord_info.get("cumQtyRq", ord_info.get("cumQty", exec_qty)))
            price_rp = float(ord_info.get("execPriceRp", 0) or ord_info.get("priceRp", 0) or ord_info.get("price", 0))

            # ИНВАРИАНТ маршрутизации: интерференция проверяется первой
            # Фикс #1 TECH_DEBT: обрабатываем и PENDING_HTTP (гонка HTTP/WS)
            interf_stored = self.tb.state.pending_interference_orders.get(pos_key)
            if interf_stored is not None and (order_id == interf_stored or interf_stored == "PENDING_HTTP"):
                await self._process_interference(symbol, pos_key, order_id, status, pos_side_raw, price_rp, exec_qty)
            elif pos_key in self.tb.state.pending_entry_orders:
                await self._process_entry(symbol, pos_key, order_id, status, pos_side_raw, cum_qty)
            elif pos_key in self.tb.state.active_positions:
                await self._process_close(symbol, pos_key, order_id, status, pos_side_raw, price_rp, cum_qty, exec_qty)