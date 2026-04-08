from __future__ import annotations

from typing import TYPE_CHECKING

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

    # и get_top_bid_ask и check_is_negative можно и нужно вынести во внешнюю утилиту ибо используются в разных модулях.
    
    @staticmethod
    def get_top_bid_ask(depth: DepthTop) -> tuple[float, float]:
        """Быстрое извлечение лучших цен (bid1, ask1) из списков стакана."""
        ask1 = depth.asks[0][0] if depth.asks else 0.0
        bid1 = depth.bids[0][0] if depth.bids else 0.0
        return bid1, ask1

    def check_is_negative(self, pos: ActivePosition, depth: DepthTop, negative_spread_pct: float) -> bool:
        """Хелпер для проверки: находится ли позиция в просадке (ПНЛ <= порога)."""
        bid1, ask1 = self.get_top_bid_ask(depth)
        if not ask1 or not bid1: 
            return False

        if pos.side == "LONG":
            spread = (ask1 - pos.init_ask1) / pos.init_ask1 * 100
            return spread <= negative_spread_pct
        else:
            spread = (pos.init_bid1 - bid1) / pos.init_bid1 * 100
            return spread <= negative_spread_pct

    def analyze(self, depth: DepthTop, pos: ActivePosition, now: float) -> str | None:
        if not self.enable: return None

        if pos.in_breakeven_mode or pos.in_extrime_mode:
            return None
        
        # Во время стабилизации мы "в безопасности", поэтому тащим якорь за собой
        if (now - pos.opened_at) < self.stab_neg: 
            pos.last_negative_check_ts = now
            return None

        is_negative = self.check_is_negative(pos, depth, self.negative_spread_pct)

        if is_negative:
            # Мы в просадке! Якорь перестал обновляться.
            # Считаем чистое абсолютное время с момента, как всё стало плохо.
            if (now - pos.last_negative_check_ts) >= self.negative_ttl:
                return "NEGATIVE_TIMEOUT"
        else:
            # Мы в плюсе (или спред нулевой). Подтягиваем якорь к текущему моменту.
            pos.last_negative_check_ts = now

        return None