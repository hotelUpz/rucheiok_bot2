# ============================================================
# FILE: CORE/models_fsm.py
# ROLE: FSM для ActivePosition. Интерпретатор событий WebSocket.
# ============================================================
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, fields
from typing import Dict, Any, TYPE_CHECKING
from c_log import UnifiedLogger

if TYPE_CHECKING:
    from CORE.restorator import BotState

logger = UnifiedLogger("ws_model")

@dataclass
class ActivePosition:
    symbol: str             
    side: str               
    
    in_position: bool = False            # Якорь физического присутствия в рынке!
    in_base_mode: bool = False           
    in_breakeven_mode: bool = False      
    in_extrime_mode: bool = False        
    interference_disabled: bool = False  
    is_closed_by_exchange: bool = False  
    interf_in_flight: bool = False       
    
    entry_price: float = 0.0             
    pending_price: float = 0.0           
    avg_price: float = 0.0               
    current_close_price: float = 0.0     
    realized_exit_price: float = 0.0     
    
    pending_qty: float = 0.0             
    current_qty: float = 0.0             
    interf_comulative_qty: float = 0.0 
    
    init_ask1: float = 0.0
    init_bid1: float = 0.0
    base_target_price_100: float = 0.0   
    
    current_target_rate: float = 1.0     
    close_order_id: str = ""             
    
    opened_at: float = field(default_factory=time.time)
    last_shift_ts: float = 0.0
    last_negative_check_ts: float = 0.0
    breakeven_start_ts: float = 0.0
    last_extrime_try_ts: float = 0.0
    
    extrime_retries_count: int = 0

    def to_dict(self) -> dict:
        return self.__dict__

    @classmethod
    def from_dict(cls, data: dict) -> "ActivePosition":
        valid_keys = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)


class WsInterpreter:
    def __init__(self, state: BotState, active_positions_locker: Dict[str, asyncio.Lock]):
        self.state = state
        self._locks = active_positions_locker

    def _get_lock(self, pos_key: str) -> asyncio.Lock:
        if pos_key not in self._locks:
            self._locks[pos_key] = asyncio.Lock()
        return self._locks[pos_key]

    @staticmethod
    def _safe_float(val: Any, default: float = 0.0) -> float:
        try: return float(val) if val is not None else default
        except (ValueError, TypeError): return default

    async def process_phemex_message(self, event_data: Dict[str, Any]):
        orders = event_data.get("orders") or event_data.get("order_p") or []
        positions = event_data.get("positions") or event_data.get("position_p") or []
        
        for order in orders:
            await self._handle_order_update(order)
        for pos in positions:
            await self._handle_position_update(pos)

    async def _handle_order_update(self, o: Dict[str, Any]):
        symbol = o.get("symbol")
        if not symbol: return

        pos_side_raw = str(o.get("posSide", ""))
        order_side = str(o.get("side", "")).lower()

        # ФИКС PHEMEX API: При Market Close биржа шлет posSide="None". 
        # Вычисляем сторону реверсивным путем: Sell закрывает Long, Buy закрывает Short.
        if pos_side_raw.lower() in ("none", ""):
            exec_inst = str(o.get("execInst", "")).lower()
            if "close" in exec_inst or "reduce" in exec_inst or o.get("action") == "Replace":
                pos_side = "LONG" if order_side == "sell" else "SHORT"
            else:
                return # Игнорируем маржинальные апдейты аккаунта
        else:
            pos_side = "LONG" if pos_side_raw.lower() in ("long", "buy") else "SHORT"

        pos_key = f"{symbol}_{pos_side}"
        ord_status = str(o.get("ordStatus", "")).upper()
        exec_status = str(o.get("execStatus", "")).upper()

        async with self._get_lock(pos_key):
            pos: ActivePosition = self.state.active_positions.get(pos_key)
            if not pos: return

            is_closing_order = (pos.side == "LONG" and order_side == "sell") or \
                               (pos.side == "SHORT" and order_side == "buy")

            # Ловим физический налив ордера
            if ord_status in ("FILLED", "PARTIALLYFILLED") or "FILL" in exec_status:
                
                # ФИКС ЦЕНЫ: Phemex прячет реальную цену исполнения в execPriceRp
                fill_price = self._safe_float(o.get("execPriceRp", 0.0))
                if fill_price <= 0:
                    fill_price = self._safe_float(o.get("priceRp", o.get("price", 0.0)))

                logger.debug(f"[WS_ORDERS] 🔔 Налив ордера {pos_key} | Закрывающий: {is_closing_order} | Цена: {fill_price}")

                if is_closing_order:
                    if fill_price > 0: 
                        pos.realized_exit_price = fill_price
                else:
                    if pos.entry_price == 0.0 and fill_price > 0:
                        pos.opened_at = time.time()
                        pos.entry_price = fill_price

    async def _handle_position_update(self, p: Dict[str, Any]):
        symbol = p.get("symbol")
        pos_side_raw = str(p.get("posSide", ""))

        if pos_side_raw.lower() in ("none", ""): return 
        if not symbol: return

        pos_side = "LONG" if pos_side_raw.lower() in ("long", "buy") else "SHORT"
        pos_key = f"{symbol}_{pos_side}"

        async with self._get_lock(pos_key):
            pos: ActivePosition = self.state.active_positions.get(pos_key)
            if not pos: return

            if "size" in p or "sizeRq" in p:
                size = abs(self._safe_float(p.get("sizeRq", p.get("size"))))
                avg_price = self._safe_float(p.get("avgEntryPriceRp", p.get("avgEntryPrice")))

                logger.debug(f"[WS_POS] 📊 Апдейт {pos_key} | Size: {size} | InPos: {pos.in_position}")

                if size > 0:
                    pos.current_qty = size
                    pos.in_position = True # ФИКС: Якорь, что позиция РЕАЛЬНО в рынке
                    if avg_price > 0: pos.avg_price = avg_price
                else:
                    # ФИКС МУСОРОСБОРЩИКА: 
                    # Игнорируем пустые апдейты, если позиция еще не наливалась (ждущая лимитка).
                    if getattr(pos, 'in_position', False):
                        logger.info(f"[{pos_key}] 💀 Биржа прислала size=0. Позиция закрыта.")
                        pos.is_closed_by_exchange = True
                        pos.current_qty = 0.0
                        pos.in_position = False