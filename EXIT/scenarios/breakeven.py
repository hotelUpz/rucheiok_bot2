from __future__ import annotations

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from CORE.models_fsm import ActivePosition


class PositionTTLClose:
    """
    Глобальный таймаут позиции.

    Инвариант (двухэтапный):
    1. После position_ttl секунд → TRIGGER_BREAKEVEN (ExitEngine переводит цель в БУ).
    2. Если БУ-лимитка не закрыла позицию за breakeven_wait_sec → TRIGGER_EXTRIME.
    Оба шага происходят ровно по одному разу благодаря флагу pos.in_breakeven_mode.
    """
    def __init__(self, cfg: dict, active_positions_locker):
        self.cfg = cfg
        self.enable = cfg.get("enable", True)
        self.position_ttl = cfg.get("position_ttl", 600)
        self.to_entry_orientation = float(cfg.get("to_entry_orientation", 0.0))
        self.breakeven_wait_sec = float(cfg.get("breakeven_wait_sec", 0.0))
        self.active_positions_locker = active_positions_locker # стата тут может мутироваться. Норм.

    # def build_target_price(self, pos: ActivePosition) -> float:
    #     orient_pct = self.to_entry_orientation
    #     if orient_pct == 0.0: return pos.entry_price
    #     if pos.side == "LONG":
    #         return pos.entry_price * (1 + orient_pct / 100.0)
    #     return pos.entry_price * (1 - orient_pct / 100.0)

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