from __future__ import annotations
from typing import TYPE_CHECKING
from c_log import UnifiedLogger

if TYPE_CHECKING:
    from CORE.models_fsm import ActivePosition
    from API.PHEMEX.stakan import DepthTop

logger = UnifiedLogger("base")

class BaseScenario:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.enable = cfg.get("enable", True)
        self.stab_ttl = cfg.get("stabilization_ttl", 0.0)
        self.target_rate = cfg.get("target_rate", 0.7)
        self.shift_demotion = cfg.get("shift_demotion", 0.2)
        self.min_target_rate = cfg.get("min_target_rate", 0.3)
        self.shift_ttl = cfg.get("shift_ttl", 10.0)

    def analyze(self, depth: DepthTop, pos: ActivePosition, now: float) -> float | None:
        if not self.enable or pos.in_breakeven_mode or pos.in_extrime_mode:
            return None
            
        # ФИКС ЗОМБИ-ПОЗИЦИЙ: Закрываем всё что больше нуля!
        if pos.current_qty == 0: 
            return None

        if (now - pos.opened_at) < self.stab_ttl:
            return None

        if not pos.in_base_mode:
            pos.in_base_mode = True
            pos.last_shift_ts = now
            pos.current_target_rate = self.target_rate

        time_since_shift = now - pos.last_shift_ts
        if time_since_shift >= self.shift_ttl and pos.current_target_rate > self.min_target_rate:
            pos.current_target_rate = max(self.min_target_rate, pos.current_target_rate - self.shift_demotion)
            pos.last_shift_ts = now

        target_shift = pos.base_target_price_100 - pos.entry_price
        target_price = pos.entry_price + (target_shift * pos.current_target_rate)
        
        return target_price