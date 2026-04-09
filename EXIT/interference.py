from __future__ import annotations
from typing import TYPE_CHECKING

from EXIT.utils import check_is_negative

if TYPE_CHECKING:
    from CORE.models_fsm import ActivePosition
    from API.PHEMEX.stakan import DepthTop

class Interference:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.enable = cfg.get("enable", False)
        self.stab_ttl = cfg.get("stabilization_ttl", 2.0)
        self.usual_vol_pct = cfg.get("usual_vol_pct_to_init_size", 3) / 100
        self.max_vol_pct = cfg.get("max_vol_pct_to_init_size", 9) / 100
        self.negative_spread_pct = 0.0

    def _find_target(self, depth: DepthTop, pos: ActivePosition, allowed_remains: float) -> tuple[float, float] | None:
        if pos.side == "LONG":
            for price, vol in depth.asks:
                if price < pos.base_target_price_100 and vol <= allowed_remains: return (price, vol)
        else:
            for price, vol in depth.bids:
                if price > pos.base_target_price_100 and vol <= allowed_remains: return (price, vol)
        return None    

    def analyze(self, depth: DepthTop, pos: ActivePosition, now: float) -> tuple[float, float] | None:
        if not self.enable or pos.in_breakeven_mode or pos.in_extrime_mode:
            return None
            
        # ЗАМЕНА на min_notional
        if pos.current_qty < pos.min_notional_asset or pos.interference_disabled:
            return None

        if (now - pos.opened_at) < self.stab_ttl: return None

        # ИСПОЛЬЗУЕМ УТИЛИТУ
        if not check_is_negative(pos, depth, self.negative_spread_pct):
            return None

        allowed_remains = (pos.pending_qty * self.max_vol_pct) - pos.interf_comulative_qty
        
        # ЗАМЕНА проверки лимита на min_notional_asset
        if allowed_remains < pos.min_notional_asset: 
            pos.interference_disabled = True
            return None

        max_chunk_vol = pos.pending_qty * self.usual_vol_pct
        target = self._find_target(depth, pos, allowed_remains) 

        if target:
            price, _ = target
            buy_qty = min(max_chunk_vol, allowed_remains)
            # Финальная проверка перед выстрелом
            if buy_qty < pos.min_notional_asset: return None
            return price, buy_qty

        return None