from __future__ import annotations

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from CORE.models import ActivePosition
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

        ask1 = depth.asks[0][0] if depth.asks else 0.0
        bid1 = depth.bids[0][0] if depth.bids else 0.0
        if not ask1 or not bid1: return None

        is_negative = False
        if pos.side == "LONG":
            spread = (ask1 - pos.init_ask1) / pos.init_ask1 * 100
            if spread <= self.negative_spread_pct: is_negative = True
        else:
            spread = (pos.init_bid1 - bid1) / pos.init_bid1 * 100
            if spread <= self.negative_spread_pct: is_negative = True

        if is_negative:
            # СТРОГО ПО ТЗ: Устойчивость к реконнектам.
            # Если бот "спал" из-за лагов более 5 секунд, мы прибавляем не всё время простоя, 
            # а только 1 секунду, чтобы избежать ложного мгновенного триггера EXTRIME MODE.
            delta = now - pos.last_negative_check_ts
            if delta > 5.0: 
                delta = 1.0 
                
            pos.negative_duration_sec += delta
            
            if pos.negative_duration_sec >= self.negative_ttl:
                return {"action": "TRIGGER_EXTRIME", "reason": "NEGATIVE_SCENARIO"}
        else:
            pos.negative_duration_sec = 0.0

        pos.last_negative_check_ts = now
        return None