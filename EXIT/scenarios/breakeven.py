from __future__ import annotations

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from CORE.models_fsm import ActivePosition


class PositionTTLClose:
    def __init__(self, cfg: dict, active_positions_locker):
        self.cfg = cfg
        self.enable = cfg["enable"] 
        self.position_ttl = cfg["position_ttl"]
        self.to_entry_orientation = float(cfg["to_entry_orientation"])
        self.breakeven_wait_sec = float(cfg["breakeven_wait_sec"])
        self.active_positions_locker = active_positions_locker

    def build_target_price(self, pos: ActivePosition) -> float:
        orient_pct = self.to_entry_orientation
        if orient_pct == 0.0: return pos.avg_price
        if pos.side == "LONG":
            return pos.avg_price * (1 + orient_pct / 100.0)
        return pos.avg_price * (1 - orient_pct / 100.0)

    async def scen_ttl_analyze(self, pos: ActivePosition, now: float) -> str | None: 
        if self.position_ttl in ("inf", None): return None
        # if pos.exit_status == "EXTREME": return None # -- не обязательно так как детерминированно оркестратором.
        
        # 1. Если время жизни вышло, но мы еще не начали 'охоту'
        if pos.breakeven_start_ts == 0 and (now - pos.opened_at) >= self.position_ttl: 
            return "BREAKEVEN"

        # 2. Если мы уже 'охотимся' (лимитка стоит), но время ожидания БУ вышло
        if pos.breakeven_start_ts > 0:
            if pos.current_qty > 0.0 and now - pos.breakeven_start_ts >= self.breakeven_wait_sec:
                return "BREAKEVEN_EXTRIME"
        return None