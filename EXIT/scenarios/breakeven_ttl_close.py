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
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.enable = cfg.get("enable", True)
        self.position_ttl = cfg.get("position_ttl", 600)

        self.to_entry_orientation = float(cfg.get("to_entry_orientation", 0.0))

    def build_target_price(self, pos: ActivePosition) -> float:
        orient_pct = self.to_entry_orientation
        if orient_pct == 0.0: return pos.entry_price # либо даже pos.avg_price
        if pos.side == "LONG":
            return pos.entry_price * (1 + orient_pct / 100.0)
        return pos.entry_price * (1 - orient_pct / 100.0)

    def analyze(self, pos: ActivePosition, now: float) -> str | None:
        if not self.enable:
            return None

        # Breakeven уже активен (установлен Average ИЛИ нами ранее).
        # Ждём breakeven_wait_sec и переводим в Extrime — независимо от position_ttl.
        if pos.in_breakeven_mode:
            if now - pos.breakeven_start_ts >= self.breakeven_wait_sec:
                return "BREAKEVEN_TIMEOUT"
            return None