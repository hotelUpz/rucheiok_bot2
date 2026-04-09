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

    def analyze(self, depth: DepthTop, pos: ActivePosition, now: float) -> str | None:
        if not self.enable: return None

        if pos.in_breakeven_mode or pos.in_extrime_mode:
            return None
        
        # Во время стабилизации мы "в безопасности", поэтому тащим якорь за собой
        if (now - pos.opened_at) < self.stab_neg: 
            pos.last_negative_check_ts = now
            return None

        is_negative = check_is_negative(pos, depth, self.negative_spread_pct)

        if is_negative:
            # Мы в просадке! Якорь перестал обновляться.
            # Считаем чистое абсолютное время с момента, как всё стало плохо.
            if (now - pos.last_negative_check_ts) >= self.negative_ttl:
                return "NEGATIVE_TIMEOUT"
        else:
            # Мы в плюсе (или спред нулевой). Подтягиваем якорь к текущему моменту.
            pos.last_negative_check_ts = now

        return None