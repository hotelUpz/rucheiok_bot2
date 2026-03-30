# ============================================================
# FILE: CORE/models.py
# ROLE: Data structures for trading state
# ============================================================

import time
from dataclasses import dataclass, field

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