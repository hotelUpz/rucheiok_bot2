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

logger = UnifiedLogger("ws")


@dataclass
class ActivePosition:
    """
    ROLE: Data structures for trading state.
    Экземпляры строго изолированы по композитным ключам (SYMBOL_SIDE).
    """
    symbol: str             
    side: str               
    
    # --- State Flags ---
    in_pending: bool = False             
    in_position: bool = False            
    in_base_mode: bool = False           
    in_breakeven_mode: bool = False      
    in_extrime_mode: bool = False        
    interference_disabled: bool = False  
    
    # --- Pricing ---
    entry_price: float = 0.0             
    pending_price: float = 0.0           
    avg_price: float = 0.0               
    current_close_price: float = 0.0     # КРИТИЧНО: Текущая цена стоящего лимитного ордера на выход
    
    # --- Quantities ---
    pending_qty: float = 0.0             
    current_qty: float = 0.0             
    interf_comulative_qty: float = 0.0 
    
    # --- Signal Initialization ---
    init_ask1: float = 0.0
    init_bid1: float = 0.0
    base_target_price_100: float = 0.0   
    
    # --- Tracking & Hunting ---
    current_target_rate: float = 1.0     
    close_order_id: str = ""             
    
    # --- Timestamps ---
    opened_at: float = field(default_factory=time.time)
    last_shift_ts: float = 0.0
    last_negative_check_ts: float = 0.0
    breakeven_start_ts: float = 0.0
    last_extrime_try_ts: float = 0.0
    
    # --- Counters ---
    extrime_retries_count: int = 0
    
    # --- ЗАКОММЕНТИРОВАННЫЙ МУСОР (DEPRECATED) ---
    # in_signal: bool = False
    # failed_interference_prices: Dict[str, int] = field(default_factory=dict)

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
        # Используем глобальный локер оркестратора
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
        pos_side_raw = o.get("posSide", o.get("side", ""))
        
        if not symbol or not pos_side_raw: return
            
        pos_side = "LONG" if pos_side_raw.lower() in ("long", "buy") else "SHORT"
        pos_key = f"{symbol}_{pos_side}"
        ord_status = str(o.get("ordStatus", "")).upper()
        
        async with self._get_lock(pos_key):
            pos: ActivePosition = self.state.active_positions.get(pos_key)
            if not pos: return

            if ord_status in ("NEW", "UNTRIGGERED"):
                pos.in_pending = True
            elif ord_status in ("FILLED", "PARTIALLYFILLED"):
                if not pos.in_position:
                    pos.opened_at = time.time()
                    pos.entry_price = self._safe_float(o.get("priceRp", o.get("price")))
                
                pos.in_position = True
                if ord_status == "FILLED":
                    pos.in_pending = False
            elif ord_status in ("CANCELED", "REJECTED"):
                pos.in_pending = False

    async def _handle_position_update(self, p: Dict[str, Any]):
        symbol = p.get("symbol")
        pos_side_raw = p.get("posSide", p.get("side", ""))
        
        if not symbol or not pos_side_raw: return
            
        pos_side = "LONG" if pos_side_raw.lower() in ("long", "buy") else "SHORT"
        pos_key = f"{symbol}_{pos_side}"
        
        async with self._get_lock(pos_key):
            pos: ActivePosition = self.state.active_positions.get(pos_key)
            if not pos: return
                
            size = self._safe_float(p.get("sizeRq", p.get("size")))
            avg_price = self._safe_float(p.get("avgEntryPriceRp", p.get("avgEntryPrice")))

            if size > 0:
                pos.current_qty = size
                if avg_price > 0:
                    pos.avg_price = avg_price
                pos.in_position = True
            else:
                # В HFT оставлять призраки позиций опасно. Жесткое удаление — самый надежный способ.
                logger.info(f"[{pos_key}] 🛑 Позиция закрыта. Стейт очищен.")
                self.state.active_positions.pop(pos_key, None)
                # """TODO: не уверен что можно и нужно так жестко сбрасываать позицию. если и так, нужен будет минимальный слепок для отчетности в тг и логи. Удалять жестко не стоит, возможно ресетить, прогоняя через начальный bootstrap. В противном случае непонятно как контролровать логику отправки."""