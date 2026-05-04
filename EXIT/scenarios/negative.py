from __future__ import annotations

from typing import TYPE_CHECKING
from EXIT.utils import check_is_negative
from c_log import UnifiedLogger

if TYPE_CHECKING:
    from CORE.models_fsm import ActivePosition
    from API.PHEMEX.stakan import DepthTop


logger = UnifiedLogger("negative")


class NegativeScenario:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.enable = cfg["enable"]
        self.stab_neg = cfg["stabilization_ttl"]
        self.negative_spread_pct = cfg["negative_spread_pct"]
        self.negative_ttl = cfg["negative_ttl"]

    def scen_neg_analyze(self, depth: DepthTop, pos: ActivePosition, now: float) -> str | None:
        if not self.enable:
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