from __future__ import annotations
from typing import TYPE_CHECKING

from EXIT.utils import check_is_negative

if TYPE_CHECKING:
    from CORE.models_fsm import ActivePosition
    from API.PHEMEX.stakan import DepthTop


class Interference:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.enable = cfg["enable"]
        self.stab_ttl = cfg["stabilization_ttl"]
        self.usual_vol_pct = cfg["usual_vol_pct_to_init_size"] / 100.0
        self.max_vol_pct = cfg["max_vol_pct_to_init_size"] / 100.0
        self.negative_spread_pct = 0.0 #

    def _find_target(self, depth: DepthTop, pos: ActivePosition, allowed_remains: float) -> tuple[float, float] | None:
        if pos.side == "LONG":
            for price, vol in depth.asks:
                if price < pos.base_target_price_100 and vol <= allowed_remains: return (price, vol)
        else:
            for price, vol in depth.bids:
                if price > pos.base_target_price_100 and vol <= allowed_remains: return (price, vol)
        return None    

    def scen_interf_analyze(self, depth: DepthTop, pos: ActivePosition, now: float) -> tuple[float, float] | None:
        if not self.enable:
            return None

        if pos.current_qty <= 0:
            return None

        if (now - pos.opened_at) < self.stab_ttl:
            return None

        if not check_is_negative(pos, depth, self.negative_spread_pct):
            return None
        
        if not pos.max_allowed_remains:
            # pos.max_allowed_remains = pos.pending_qty * self.max_vol_pct # -- в этом есть есть логика, но \
            pos.max_allowed_remains = pos.current_qty * self.max_vol_pct # -- наверное так точнее.       

        # Фиксированный лимит (посчитан один раз при входе, не зависит от текущего pending_qty)
        allowed_remains = pos.max_allowed_remains - pos.interf_comulative_qty
        if allowed_remains <= 0.0:
            return None

        target = self._find_target(depth, pos, allowed_remains)
        if not target:
            return None

        price, _ = target

        # Размер чанка выводим из того же фиксированного лимита, а не из живого pending_qty
        max_chunk_vol = pos.max_allowed_remains * (self.usual_vol_pct / self.max_vol_pct)
        buy_qty = min(max_chunk_vol, allowed_remains)

        if buy_qty <= 0.0:
            return None

        return price, buy_qty