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

    async def analyze(self, pos: ActivePosition, now: float) -> str | None:
        if self.position_ttl in ("inf", None): return None

        # Если уже улетели в экстрим — БУ не имеет значения
        if pos.exit_status == "EXTRIME": return None
        
        # Если всё штатно, проверяем не вышел ли базовый таймер
        if pos.exit_status == "NORMAL" and (now - pos.opened_at) >= self.position_ttl: 
            pos.breakeven_start_ts = now  # Засекаем начало охоты
            return "BREAKEVEN_TIMEOUT"

        # Если мы уже в фазе охоты за безубытком (HUNTING), проверяем таймер ожидания БУ
        if pos.exit_status == "HUNTING":
            if pos.current_qty > 0.0 and now - pos.breakeven_start_ts >= self.breakeven_wait_sec:
                return "EXTRIME_SCENARIO"
            return None
            
        return None