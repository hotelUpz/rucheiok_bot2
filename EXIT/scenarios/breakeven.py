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
    Оба шага происходят ровно по одному разу благодаря флагу pos.in_negative_mode.
    """
    def __init__(self, cfg: dict, active_positions_locker):
        self.cfg = cfg
        self.enable = cfg.get("enable", True)
        self.position_ttl = cfg.get("position_ttl", 600)
        self.to_entry_orientation = float(cfg.get("to_entry_orientation", 0.0))
        self.breakeven_wait_sec = float(cfg.get("breakeven_wait_sec", 0.0))
        self.active_positions_locker = active_positions_locker # стата тут может мутироваться. Норм.

    def build_target_price(self, pos: ActivePosition) -> float:
        orient_pct = self.to_entry_orientation
        if orient_pct == 0.0: return pos.entry_price # либо даже pos.avg_price
        if pos.side == "LONG":
            return pos.entry_price * (1 + orient_pct / 100.0)
        return pos.entry_price * (1 - orient_pct / 100.0)

    async def analyze(self, pos: ActivePosition, now: float) -> str | None: # нужно все маркеры времени по контролю состояния позиций привести к единому формату. скорее всего тип будет int то есть формат секунд.
        if self.position_ttl in ("inf", None): return None
        
        if not pos.in_negative_mode and (now - pos.opened_at) >= self.position_ttl: # 
            # async with self.active_positions_locker...: #(по ключам извлекаем нужный локер)
            pos.breakeven_start_ts = now
            pos.in_negative_mode = True

            return "BREAKEVEN_TIMEOUT"

        # Если Breakeven уже активен
        # Ждём breakeven_wait_sec и переводим в Extrime — независимо от position_ttl.
        if pos.in_negative_mode:
            if now - pos.breakeven_start_ts >= self.breakeven_wait_sec:                
                return "EXTRIME_SCENARIO"
            return None