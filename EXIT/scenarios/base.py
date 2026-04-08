from __future__ import annotations

from typing import TYPE_CHECKING
from c_log import UnifiedLogger

if TYPE_CHECKING:
    from CORE.models_fsm import ActivePosition
    from API.PHEMEX.stakan import DepthTop

logger = UnifiedLogger("average")

class AverageScenario:
    """
    Основной сценарий охоты за TP.
    """
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.enable = cfg.get("enable", True)
        self.stab_base = cfg.get("stabilization_ttl", 2)
        self.shift_ttl = cfg.get("shift_ttl", 60)
        self.shift_demotion = cfg.get("shift_demotion", 0.2)
        self.min_target_rate = cfg.get("min_target_rate", 0.3)

    def _calc_virtual_tp(self, pos: ActivePosition) -> float:
        """Вычисляет виртуальный TP без сайд-эффектов."""
        if pos.side == "LONG":
            return pos.entry_price + (pos.base_target_price_100 - pos.entry_price) * pos.current_target_rate # для хедж позиции будет путаница. Нужно использовать отдельные структуры для Лонга и для Шорта.
        return pos.entry_price - (pos.entry_price - pos.base_target_price_100) * pos.current_target_rate # то же.

    def analyze(self, depth: DepthTop, pos: ActivePosition, now: float) -> float | None:

        if pos.current_qty <= 0.0: return None # возможно избыточно.
        
        if pos.in_breakeven_mode or pos.in_extrime_mode:
            return None

        time_in_pos = now - pos.opened_at

        # Ждем стабилизации после входа
        if not pos.in_base_mode and time_in_pos >= self.stab_base:
            logger.info(
                f"[{pos.symbol}] ✅ Average: стабилизация завершена. "
            )
            pos.in_base_mode = True
            pos.last_shift_ts = now
        else:
            if not pos.in_base_mode: return None

        virtual_tp = self._calc_virtual_tp(pos)

        # 2. ЛОГИКА СДВИГА (Shift) И КОМПРОМИССОВ
        if now - pos.last_shift_ts >= self.shift_ttl:
            if pos.current_target_rate > self.min_target_rate:

                old_rate = pos.current_target_rate
                old_virtual_tp = virtual_tp

                new_rate = pos.current_target_rate - self.shift_demotion
                pos.current_target_rate = max(new_rate, self.min_target_rate)
                pos.last_shift_ts = now
                # pos.negative_duration_sec = 0.0 # вряд ли это здесь уместно. В тех задании перепишу.

                # Пересчитываем virtual_tp после понижения планки (только локально)
                virtual_tp = self._calc_virtual_tp(pos)

                logger.info(
                    f"[{pos.symbol}] 📉 Average: СДВИГ ТП. "
                    f"Rate: {old_rate:.3f} → {pos.current_target_rate:.3f} | "
                    f"Виртуальный TP: {old_virtual_tp:.8g} → {virtual_tp:.8g}"
                )

        # 3. АКТИВНЫЙ ХАНТИНГ (Поиск уровня с максимальным объемом у/выше virtual_tp)
        ideal_target_price = None
        max_vol = -1.0

        if pos.side == "LONG":
            for price, vol in depth.bids:
                if price >= virtual_tp:
                    if vol > max_vol:
                        max_vol = vol
                        ideal_target_price = price
                # else:
                #     break # не уверен в правильности сортировки данных. Пока без break
        else:
            for price, vol in depth.asks:
                if price <= virtual_tp:
                    if vol > max_vol:
                        max_vol = vol
                        ideal_target_price = price
                # else:
                #     break # не уверен в правильности сортировки данных. Пока без break

        return ideal_target_price