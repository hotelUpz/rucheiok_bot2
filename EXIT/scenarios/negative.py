from __future__ import annotations

from typing import TYPE_CHECKING
from EXIT.utils import check_is_negative

if TYPE_CHECKING:
    from CORE.models_fsm import ActivePosition
    from API.PHEMEX.stakan import DepthTop

class NegativeScenario:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.enable = cfg.get("enable", True)
        self.stab_neg = cfg.get("stabilization_ttl", 1.5)
        self.negative_spread_pct = cfg.get("negative_spread_pct", 0)
        self.negative_ttl = cfg.get("negative_ttl", 60)

    def analyze(self, depth: DepthTop, pos: ActivePosition, now: float) -> dict | None:
        if not self.enable: return None
        
        time_in_pos = now - pos.opened_at
        if time_in_pos < self.stab_neg: return None

        # --- ВМЕСТО РУЧНОГО ПОЛУЧЕНИЯ ЦЕН И РАСЧЕТА СПРЕДА ---
        is_negative = check_is_negative(pos, depth, self.negative_spread_pct)

        if is_negative:
            delta = now - pos.last_negative_check_ts
            if delta > 5.0: delta = 1.0 
                
            pos.negative_duration_sec += delta
            
            if pos.negative_duration_sec >= self.negative_ttl:
                return "NEGATIVE_SCENARIO"
        else:
            pos.negative_duration_sec = 0.0

        pos.last_negative_check_ts = now
        return None