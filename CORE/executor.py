# ============================================================
# FILE: CORE/executor.py
# ROLE: Шаблоны отправки ордеров на биржу, защита от гонок потоков
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
    if not step or step <= 0: return value
    val_d = Decimal(str(value))
    step_d = Decimal(str(step))
    rounded = (val_d / step_d).quantize(Decimal("1"), rounding=ROUND_DOWN) * step_d
    return float(rounded)


class OrderExecutor:
    def __init__(self, tb: "TradingBot"):
        self.tb = tb
        self.client = tb.private_client
        self.cfg = tb.cfg
        
        self.entry_timeout = self.cfg.get("entry", {}).get("entry_timeout_sec", 0.5)
        self.exit_timeout = self.cfg.get("exit", {}).get("exit_timeout_sec", 0.5)
        self.interf_timeout = self.cfg.get("exit", {}).get("interference", {}).get("interference_timeout_sec", 0.1)
        
        self.RETRY_PAUSE_SEC = 0.05    
        self.WS_SYNC_PAUSE_SEC = 0.05  
        
        self.max_entry_retries = self.cfg.get("entry", {}).get("max_place_order_retries", 1)
        self.max_exit_retries = self.cfg.get("exit", {}).get("max_place_order_retries", 1)
        
        risk = self.cfg.get("risk", {})
        self.notional_limit = float(risk.get("notional_limit", 25.0))
        self.margin_over_size_pct = float(risk.get("margin_over_size_pct", 1.0))
        self.leverage = risk.get("leverage", {}).get("val", 10)

    async def cancel_all_orders(self, symbol: str) -> bool:
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
        if not order_id: return False
        try:
            resp = await self.client.cancel_order(symbol, order_id, pos_side)
            if resp.get("code") == 0: return True
            err_msg = str(resp.get("msg", "")).lower()
            if "filled" not in err_msg and "not found" not in err_msg:
                logger.debug(f"[{symbol}] Не удалось отменить {order_id}: {resp}")
            return False
        except Exception as e:
            logger.debug(f"[{symbol}] Exception при отмене {order_id}: {e}")
            return False

    async def interf_bought(self, symbol: str, pos_key: str, qty: float, order_price: float) -> Optional[float]:
        try:
            spec = self.tb.symbol_specs.get(symbol)
            if not spec: return None

            price = round_step(order_price, spec.tick_size)
            req_qty = round_step(qty, spec.lot_size)
            if req_qty < spec.lot_size: return None

            async with self.tb._get_lock(pos_key):
                pos = self.tb.state.active_positions.get(pos_key)
                if not pos: return None
                pos_side_raw = pos.side

            side = "Buy" if pos_side_raw == "LONG" else "Sell"
            phemex_pos_side = "Long" if pos_side_raw == "LONG" else "Short"

            resp = await self.client.place_limit_order(symbol, side, req_qty, price, phemex_pos_side)
            if resp.get("code") == 0:
                order_id = resp.get("data", {}).get("orderID")
                await asyncio.sleep(self.interf_timeout)
                if order_id: await self.execute_cancel(symbol, phemex_pos_side, order_id)
                return req_qty 
            else:
                logger.debug(f"[{pos_key}] Отказ скупки помехи: {resp}")
        except Exception as e:
            logger.debug(f"[{pos_key}] Ошибка скупки помех: {e}")
        finally:
            async with self.tb._get_lock(pos_key):
                pos = self.tb.state.active_positions.get(pos_key)
                if pos: pos.interf_in_flight = False
        return None

    async def execute_entry(self, symbol: str, pos_key: str, signal: dict) -> bool:
        try:
            spec = self.tb.symbol_specs.get(symbol)
            if not spec: return False

            price = round_step(signal["price"], spec.tick_size)
            row_vol_asset = signal.get("row_vol_asset", 0.0)
            
            target_usdt = row_vol_asset * price * (1 + self.margin_over_size_pct / 100.0)
            target_usdt = min(target_usdt, self.notional_limit)
            
            qty = round_step(target_usdt / price, spec.lot_size)
            
            # ТОТАЛЬНАЯ ПРИВЯЗКА К ФИЗИКЕ: Ордер не может быть меньше минимального лота биржи
            if qty < spec.lot_size: 
                logger.warning(f"[{pos_key}] Qty {qty} меньше биржевого шага лота {spec.lot_size}. Пропуск.")
                return False

            async with self.tb._get_lock(pos_key):
                if pos_key not in self.tb.state.active_positions:
                    self.tb.state.active_positions[pos_key] = ActivePosition(
                        symbol=symbol, side=signal["side"], pending_qty=qty,
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
                            await asyncio.sleep(self.WS_SYNC_PAUSE_SEC)
                        
                        async with self.tb._get_lock(pos_key):
                            pos = self.tb.state.active_positions.get(pos_key)
                            if pos and pos.current_qty > 0: 
                                logger.info(f"[{pos_key}] ✅ Вход выполнен. Цена: {price}, Объем: {pos.current_qty}")
                                if self.tb.tg:
                                    msg = Reporters.entry_signal(symbol, signal, signal.get("b_price", 0), signal.get("p_price", 0))
                                    asyncio.create_task(self.tb.tg.send_message(msg))
                                
                                self.tb.state.consecutive_fails[symbol] = 0
                                asyncio.create_task(self.tb.state.save()) # Сохраняем успешный вход на диск
                                return True
                    else:
                        logger.warning(f"[{pos_key}] ❌ Ошибка входа: {resp}")
                except Exception as e:
                    logger.error(f"[{pos_key}] Исключение execute_entry: {e}")
                
                await asyncio.sleep(self.RETRY_PAUSE_SEC)
                
            self.tb.apply_entry_quarantine(symbol)
                
            async with self.tb._get_lock(pos_key):
                pos = self.tb.state.active_positions.get(pos_key)
                if pos and pos.current_qty <= 0:
                    pos.is_closed_by_exchange = True 

            return False
            
        finally:
            self.tb.state.pending_entry_orders.pop(pos_key, None)

    async def execute_exit(self, symbol: str, pos_key: str, order_price: float, timeout_sec: float) -> bool:
        try:
            spec = self.tb.symbol_specs.get(symbol)
            if not spec: return False

            async with self.tb._get_lock(pos_key):
                pos = self.tb.state.active_positions.get(pos_key)
                if not pos or pos.current_qty <= 0: return False
                qty = pos.current_qty
                pos_side_raw = pos.side

            price = round_step(order_price, spec.tick_size)
            qty = round_step(qty, spec.lot_size)
            if qty < spec.lot_size: return False

            side = "Sell" if pos_side_raw == "LONG" else "Buy"
            phemex_pos_side = "Long" if pos_side_raw == "LONG" else "Short"

            for attempt in range(max(1, self.max_exit_retries)):
                try:
                    resp = await self.client.place_limit_order(symbol, side, qty, price, phemex_pos_side)
                    if resp.get("code") == 0:
                        order_id = resp.get("data", {}).get("orderID")
                        async with self.tb._get_lock(pos_key):
                            if pos: pos.close_order_id = order_id
                            
                        await asyncio.sleep(timeout_sec)
                        if order_id: await self.execute_cancel(symbol, phemex_pos_side, order_id)
                        return True
                    else:
                        logger.warning(f"[{pos_key}] ❌ Ошибка выхода: {resp}")
                except Exception as e:
                    logger.error(f"[{pos_key}] Исключение execute_exit: {e}")
                
                await asyncio.sleep(self.RETRY_PAUSE_SEC)
                
            return False
            
        finally:
            async with self.tb._get_lock(pos_key):
                pos = self.tb.state.active_positions.get(pos_key)
                if pos:
                    pos.current_close_price = 0.0
                    pos.close_order_id = ""