# ============================================================
# FILE: CORE/executor.py
# ROLE: Шаблоны отправки ордеров на биржу, защита от гонок потоков...
# ============================================================
from __future__ import annotations

import asyncio
import time
import pytz
from typing import Dict, Any, TYPE_CHECKING, Optional
from decimal import Decimal, ROUND_DOWN
from dotenv import load_dotenv    

from c_log import UnifiedLogger
from consts import TIME_ZONE
from CORE._utils import Reporters
from CORE.models_fsm import ActivePosition

if TYPE_CHECKING:
    from CORE.orchestrator import TradingBot

load_dotenv()
logger = UnifiedLogger(name="bot")
TZ = pytz.timezone(TIME_ZONE)


def round_step(value: float, step: float) -> float:
    """Округление до шага цены или лота."""
    if not step or step <= 0:
        return value
    val_d = Decimal(str(value))
    step_d = Decimal(str(step))
    rounded = (val_d / step_d).quantize(Decimal("1"), rounding=ROUND_DOWN) * step_d
    return float(rounded)


class OrderExecutor:
    """
    Прокладка между оркестратором и сетевым адаптером. 
    Содержит шаблоны рутинных операций (Place -> Wait -> Cancel -> Evaluate).
    """
    def __init__(self, tb: "TradingBot"):
        self.tb = tb
        self.client = tb.private_client
        self.cfg = tb.cfg
        
        # Тайминги
        self.entry_timeout = self.cfg.get("entry", {}).get("entry_timeout_sec", 0.5)
        self.exit_timeout = self.cfg.get("exit", {}).get("exit_timeout_sec", 0.5)
        self.interf_timeout = self.cfg.get("exit", {}).get("interference", {}).get("interference_timeout_sec", 0.1)
        
        # Ретраи
        self.max_entry_retries = self.cfg.get("entry", {}).get("max_place_order_retries", 1)
        self.max_exit_retries = self.cfg.get("exit", {}).get("max_place_order_retries", 1)
        
        # Риски
        risk = self.cfg.get("risk", {})
        self.notional_limit = risk.get("notional_limit", 25.0)
        self.min_exchange_notional = risk.get("min_exchange_notional", 5.0)
        self.margin_over_size_pct = risk.get("margin_over_size_pct", 1.0)
        self.leverage = risk.get("leverage", {}).get("val", 10)

    def _get_lock(self, pos_key: str) -> asyncio.Lock:
        """Получает или создает лок для конкретной позиции из оркестратора."""
        if pos_key not in self.tb.active_positions_locker:
            self.tb.active_positions_locker[pos_key] = asyncio.Lock()
        return self.tb.active_positions_locker[pos_key]

    async def cancel_all_orders(self, symbol: str) -> bool:
        """Тотальная отмена всех ордеров по монете (Panic Button)."""
        try:
            resp = await self.client.cancel_all_orders(symbol)
            if resp.get("code") == 0:
                logger.info(f"[{symbol}] 🗑 Тотальная отмена ордеров выполнена.")
                return True
            return False
        except Exception as e:
            logger.error(f"[{symbol}] Ошибка тотальной отмены: {e}")
            return False

    async def execute_cancel(self, symbol: str, pos_side: str, order_id: str) -> bool:
        """Прокладка отмены ордера."""
        if not order_id: return False
        try:
            resp = await self.client.cancel_order(symbol, order_id, pos_side)
            if resp.get("code") == 0:
                return True
            
            # Если ордер уже исполнился, биржа вернет ошибку, это нормально для HFT.
            err_msg = str(resp.get("msg", "")).lower()
            if "filled" not in err_msg and "not found" not in err_msg:
                logger.debug(f"[{symbol}] Не удалось отменить {order_id}: {resp}")
            return False
            
        except Exception as e:
            logger.debug(f"[{symbol}] Exception при отмене {order_id}: {e}")
            return False

    async def interf_bought(self, symbol: str, pos_key: str, qty: float, order_price: float) -> Optional[float]:
        """Скупка мелких помех в стакане."""
        spec = self.tb.symbol_specs.get(symbol)
        if not spec: return None

        price = round_step(order_price, spec.tick_size)
        req_qty = round_step(qty, spec.lot_size)
        if req_qty <= 0: return None

        # Запоминаем объемы до выстрела
        async with self._get_lock(pos_key):
            pos = self.tb.state.active_positions.get(pos_key)
            if not pos: return None
            pos_side_raw = pos.side
            old_qty = pos.current_qty

        side = "Buy" if pos_side_raw == "LONG" else "Sell"
        phemex_pos_side = "Long" if pos_side_raw == "LONG" else "Short"

        try:
            resp = await self.client.place_limit_order(symbol, side, req_qty, price, phemex_pos_side)
            if resp.get("code") == 0:
                order_id = resp.get("data", {}).get("orderID")
                
                await asyncio.sleep(self.interf_timeout)
                
                if order_id:
                    await self.execute_cancel(symbol, phemex_pos_side, order_id)
                
                # Проверяем, сколько налило
                async with self._get_lock(pos_key):
                    if pos:
                        new_qty = pos.current_qty
                        filled = max(0.0, new_qty - old_qty)
                        if filled > 0:
                            pos.interf_comulative_qty += filled
                            logger.info(f"[{pos_key}] 🧹 Скуплена помеха: {filled} лотов по {price}")
                        return filled
            else:
                logger.debug(f"[{pos_key}] Отказ при скупке помехи: {resp}")
        except Exception as e:
            logger.debug(f"[{pos_key}] Ошибка скупки помех: {e}")
            
        return None

    async def execute_entry(self, symbol: str, pos_key: str, signal: dict) -> bool:
        """Комплексная постановка лимитки на вход с учетом рисков."""
        spec = self.tb.symbol_specs.get(symbol)
        if not spec: return False

        price = round_step(signal["price"], spec.tick_size)
        
        row_vol_asset = signal.get("row_vol_asset", 0.0)
        target_usdt = row_vol_asset * price * (1 + self.margin_over_size_pct / 100.0)
        
        max_notional = self.notional_limit * self.leverage
        target_usdt = min(target_usdt, max_notional)
        target_usdt = max(target_usdt, self.min_exchange_notional)
        
        qty = round_step(target_usdt / price, spec.lot_size)
        min_notional_asset = max(spec.lot_size, self.min_exchange_notional / price)
        
        if qty < min_notional_asset: return False

        # --- СОЗДАНИЕ ПОЗИЦИИ В СТЕЙТЕ ---
        async with self._get_lock(pos_key):
            if pos_key not in self.tb.state.active_positions:
                self.tb.state.active_positions[pos_key] = ActivePosition(
                    symbol=symbol,
                    side=signal["side"],
                    pending_qty=qty,
                    min_notional_asset=min_notional_asset,
                    init_ask1=signal.get("init_ask1", price),
                    init_bid1=signal.get("init_bid1", price),
                    base_target_price_100=signal.get("base_target_price_100", price)
                )

        side = "Buy" if signal["side"] == "LONG" else "Sell"
        phemex_pos_side = "Long" if signal["side"] == "LONG" else "Short"

        for attempt in range(max(1, self.max_entry_retries)):
            try:
                resp = await self.client.place_limit_order(symbol, side, qty, price, phemex_pos_side)
                if resp.get("code") == 0:
                    order_id = resp.get("data", {}).get("orderID")
                    
                    await asyncio.sleep(self.entry_timeout)
                    
                    if order_id:
                        await self.execute_cancel(symbol, phemex_pos_side, order_id)
                    
                    # Проверяем, налило ли хоть что-то
                    async with self._get_lock(pos_key):
                        pos = self.tb.state.active_positions.get(pos_key)
                        if pos and pos.current_qty >= min_notional_asset:
                            logger.info(f"[{pos_key}] ✅ Вход выполнен. Цена: {price}, Объем: {pos.current_qty}")
                            if self.tb.tg:
                                msg = Reporters.entry_signal(symbol, signal, signal.get("b_price", 0), signal.get("p_price", 0))
                                asyncio.create_task(self.tb.tg.send_message(msg))
                            return True
                        else:
                            logger.warning(f"[{pos_key}] ⚠️ Ордер не налило за {self.entry_timeout}с.")
                            return False
                else:
                    logger.warning(f"[{pos_key}] ❌ Ошибка постановки входа: {resp}")
            except Exception as e:
                logger.error(f"[{pos_key}] Исключение при execute_entry: {e}")
            
            await asyncio.sleep(0.2)
            
        # Карантин при полном провале
        quarantine_hours = self.cfg.get("entry", {}).get("quarantine", {}).get("quarantine_hours", 1)
        if str(quarantine_hours).lower() != "inf":
            self.tb.state.quarantine_until[symbol] = time.time() + (float(quarantine_hours) * 3600)
            
        # Отправляем в мусорку пустую позицию
        async with self._get_lock(pos_key):
            pos = self.tb.state.active_positions.get(pos_key)
            if pos and pos.current_qty < min_notional_asset:
                pos.is_closed_by_exchange = True

        return False

    async def execute_exit(self, symbol: str, pos_key: str, order_price: float, timeout_sec: float) -> bool:
        """Постановка лимитки на выход (Average, Extrime, Breakeven)."""
        spec = self.tb.symbol_specs.get(symbol)
        if not spec: return False

        # Читаем актуальное количество позиции
        async with self._get_lock(pos_key):
            pos = self.tb.state.active_positions.get(pos_key)
            if not pos or pos.current_qty <= 0:
                return False
            qty = pos.current_qty
            pos_side_raw = pos.side

        price = round_step(order_price, spec.tick_size)
        qty = round_step(qty, spec.lot_size)
        if qty <= 0: return False

        side = "Sell" if pos_side_raw == "LONG" else "Buy"
        phemex_pos_side = "Long" if pos_side_raw == "LONG" else "Short"

        for attempt in range(max(1, self.max_exit_retries)):
            try:
                resp = await self.client.place_limit_order(symbol, side, qty, price, phemex_pos_side)
                if resp.get("code") == 0:
                    order_id = resp.get("data", {}).get("orderID")
                    
                    # ИДЕМПОТЕНТНОСТЬ: Фиксируем цену, чтобы Game Loop не спамил эту же команду
                    async with self._get_lock(pos_key):
                        if pos:
                            pos.close_order_id = order_id
                            pos.current_close_price = price 

                    # Ждем таймаут (hunting_timeout_sec)
                    await asyncio.sleep(timeout_sec)
                    
                    # Отменяем непролитый остаток, чтобы переставить на следующем тике
                    if order_id:
                        await self.execute_cancel(symbol, phemex_pos_side, order_id)
                        
                    # Зачищаем следы ордера в стейте, открывая шлюз для Game Loop
                    async with self._get_lock(pos_key):
                        if pos:
                            pos.close_order_id = ""
                            pos.current_close_price = 0.0

                    return True
                else:
                    logger.warning(f"[{pos_key}] ❌ Ошибка выхода: {resp}")
            except Exception as e:
                logger.error(f"[{pos_key}] Исключение execute_exit: {e}")
            
            await asyncio.sleep(0.2)
            
        return False