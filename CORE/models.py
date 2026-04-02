# ============================================================
# FILE: CORE/models.py
# ROLE: Data structures for trading state
# ============================================================
from __future__ import annotations

import time
import os
import json
import asyncio
from typing import Dict, Set, List
from dataclasses import dataclass, field
from c_log import UnifiedLogger

logger = UnifiedLogger("core")


@dataclass
class ActivePosition:
    symbol: str
    side: str
    entry_price: float
    qty: float
    init_qty: float
    init_ask1: float
    init_bid1: float
    base_target_price_100: float
    opened_at: float = field(default_factory=time.time)

    current_target_rate: float = 1.0
    current_close_price: float = 0.0
    close_order_id: str | None = None
    
    is_average_stabilized: bool = False
    last_shift_ts: float = 0.0
    
    last_negative_check_ts: float = field(default_factory=time.time)
    negative_duration_sec: float = 0.0
    
    in_breakeven_mode: bool = False
    breakeven_start_ts: float = 0.0
    
    in_extrime_mode: bool = False
    extrime_retries_count: int = 0
    last_extrime_try_ts: float = 0.0
    
    entry_cancel_requested: bool = False
    interference_cancel_requested: bool = False
    # НОВОЕ ПОЛЕ: Timestamp, до которого позиция находится в фазе "Охоты"
    hunting_active_until: float = 0.0
    close_cancel_requested: bool = False
    entry_finalized: bool = False
    
    interf_bought_qty: float = 0.0
    interference_disabled: bool = False
    
    # НОВЫЕ ПОЛЯ ДЛЯ ЗАЩИТЫ ОТ ЗАВИСАНИЙ
    failed_interference_prices: dict = field(default_factory=dict)
    place_order_fails: int = 0

    def to_dict(self):
        return {
            "symbol": self.symbol,
            "side": self.side,
            "entry_price": self.entry_price,
            "qty": self.qty,
            "init_qty": self.init_qty,
            "init_ask1": self.init_ask1,
            "init_bid1": self.init_bid1,
            "base_target_price_100": self.base_target_price_100,
            "opened_at": self.opened_at,
            "current_target_rate": self.current_target_rate,
            "current_close_price": self.current_close_price,
            "close_order_id": self.close_order_id,
            "is_average_stabilized": self.is_average_stabilized,
            "last_shift_ts": self.last_shift_ts,
            "last_negative_check_ts": self.last_negative_check_ts,
            "negative_duration_sec": self.negative_duration_sec,
            "in_breakeven_mode": self.in_breakeven_mode,
            "breakeven_start_ts": self.breakeven_start_ts,
            "in_extrime_mode": self.in_extrime_mode,
            "extrime_retries_count": self.extrime_retries_count,
            "last_extrime_try_ts": self.last_extrime_try_ts,
            "entry_cancel_requested": self.entry_cancel_requested,
            "interference_cancel_requested": self.interference_cancel_requested,
            "close_cancel_requested": self.close_cancel_requested,
            "entry_finalized": self.entry_finalized,
            "interf_bought_qty": self.interf_bought_qty,
            "interference_disabled": self.interference_disabled,
            "failed_interference_prices": dict(self.failed_interference_prices),
            "place_order_fails": self.place_order_fails
        }

    @classmethod
    def from_dict(cls, data: dict):
        obj = cls(
            symbol=data["symbol"],
            side=data["side"],
            entry_price=data["entry_price"],
            qty=data["qty"],
            init_qty=data["init_qty"],
            init_ask1=data["init_ask1"],
            init_bid1=data["init_bid1"],
            base_target_price_100=data["base_target_price_100"]
        )
        for key, value in data.items():
            if hasattr(obj, key):
                setattr(obj, key, value)
        return obj


class BotState:
    def __init__(self, black_list: List, filepath: str = "bot_state.json"):
        self.filepath = filepath
        self.active_positions: Dict[str, ActivePosition] = {}
        self.consecutive_fails: Dict[str, int] = {}
        self.quarantine_until: Dict[str, float] = {}
        
        self.pending_entry_orders: Dict[str, str] = {}
        self.pending_interference_orders: Dict[str, str] = {}
        self.in_flight_orders: Set[str] = set()
        self.leverage_configured: Set[str] = set()
        self._lock = asyncio.Lock()
        self.black_list = black_list

    def _sync_save(self, state_dict: dict):
        tmp_file = f"{self.filepath}.{os.getpid()}.tmp"
        for attempt in range(3):
            try:
                with open(tmp_file, "w", encoding="utf-8") as f:
                    json.dump(state_dict, f, indent=4)
                os.replace(tmp_file, self.filepath)
                return
            except Exception as e:
                if attempt == 2:
                    logger.error(f"Ошибка сохранения стейта: {e}")
                else:
                    import time as _time
                    _time.sleep(0.05 * (attempt + 1))
            finally:
                try:
                    if os.path.exists(tmp_file):
                        os.remove(tmp_file)
                except Exception:
                    pass

    async def save(self):
        """Потокобезопасная отправка данных на диск"""
        async with self._lock:
            # 🛑 ЖИРНАЯ ПРАВКА: Берем list(), чтобы зафиксировать состояние 
            # и избежать RuntimeError, если WS удалит ключ во время сохранения
            current_positions = list(self.active_positions.items())
            
            state_dict = {
                "positions": {
                    pos_key: pos.to_dict() 
                    for pos_key, pos in current_positions 
                    if pos.symbol not in self.black_list
                },
                "fails": dict(self.consecutive_fails),
                "quarantine": dict(self.quarantine_until)
            }
            await asyncio.to_thread(self._sync_save, state_dict)

    def load(self):
        """Синхронная загрузка стейта при старте"""
        if not os.path.exists(self.filepath): return
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            self.consecutive_fails = data.get("fails", {})
            self.quarantine_until = data.get("quarantine", {})
            
            saved_positions = data.get("positions", {})
            self.active_positions.clear()
            for pos_key, pos_data in saved_positions.items():
                pos = ActivePosition.from_dict(pos_data)
                if pos.symbol in self.black_list:
                    continue
                self.active_positions[pos_key] = pos
        except Exception as e:
            logger.error(f"Ошибка чтения стейта: {e}")