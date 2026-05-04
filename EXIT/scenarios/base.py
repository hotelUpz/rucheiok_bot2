# ============================================================
# FILE: EXIT/scenarios/base.py
# ============================================================
from __future__ import annotations
from typing import TYPE_CHECKING
from c_log import UnifiedLogger

if TYPE_CHECKING:
    from CORE.models_fsm import ActivePosition
    from API.PHEMEX.stakan import DepthTop

logger = UnifiedLogger("base")

class BaseScenario:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.enable = cfg["enable"] 
        self.stab_ttl = cfg["stabilization_ttl"]
        self.target_rate = cfg["target_rate"]
        self.shift_demotion = cfg["shift_demotion"]
        self.min_target_rate = cfg["min_target_rate"]
        self.shift_ttl = cfg["shift_ttl"]

    def _calc_virtual_tp(self, pos: ActivePosition) -> float:
        """Вычисляет виртуальный TP без сайд-эффектов."""
        if pos.side == "LONG":
            return pos.entry_price + (pos.base_target_price_100 - pos.entry_price) * pos.current_target_rate
        return pos.entry_price - (pos.entry_price - pos.base_target_price_100) * pos.current_target_rate

    def scen_base_analyze(self, depth: DepthTop, pos: ActivePosition, now: float) -> float | None:
        if not self.enable:
            return None   

        if pos.current_qty <= 0:
            return None

        if (now - pos.opened_at) < self.stab_ttl:
            return None

        if not pos.in_base_mode:
            pos.in_base_mode = True
            pos.last_shift_ts = now
            pos.current_target_rate = self.target_rate

        # Логика сдвига: просто упираемся в min_target_rate и стоим на нем
        time_since_shift = now - pos.last_shift_ts
        if time_since_shift >= self.shift_ttl and pos.current_target_rate > self.min_target_rate:
            old_rate = pos.current_target_rate
            pos.current_target_rate = max(self.min_target_rate, pos.current_target_rate - self.shift_demotion)
            pos.last_shift_ts = now
            logger.info(f"[{pos.symbol}] 📉 Base: СДВИГ ТП. Rate: {old_rate:.3f} → {pos.current_target_rate:.3f}")

        # 1. ВЫЧИСЛЕНИЕ ВИРТУАЛЬНОЙ ЦЕЛИ
        virtual_tp = self._calc_virtual_tp(pos)

        # 2. АКТИВНЫЙ ХАНТИНГ (Поиск уровня с максимальным объемом у/выше virtual_tp)
        ideal_target_price = None
        max_vol = -1.0

        if pos.side == "LONG":
            for price, vol in depth.bids:
                if price >= virtual_tp:
                    if vol > max_vol:
                        max_vol = vol
                        ideal_target_price = price
                # else:
                #     break
        else:
            for price, vol in depth.asks:
                if price <= virtual_tp:
                    if vol > max_vol:
                        max_vol = vol
                        ideal_target_price = price
                # else:
                #     break

        return ideal_target_price