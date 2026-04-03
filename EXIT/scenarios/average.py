from __future__ import annotations

from typing import TYPE_CHECKING
from c_log import UnifiedLogger

if TYPE_CHECKING:
    from CORE.models import ActivePosition
    from API.PHEMEX.stakan import DepthTop

logger = UnifiedLogger("average")

class AverageScenario:
    """
    Основной сценарий охоты за TP.

    Инвариант:
    - Вычисляет виртуальный TP (virtual_tp) на основе current_target_rate.
    - virtual_tp используется ТОЛЬКО как локальный порог сканирования стакана.
    - pos.current_close_price НЕ перезаписывается здесь — это исключительно зона
      Executor-а (хранит цену РЕАЛЬНО стоящего ордера для идемпотентности).
    - Каждые shift_ttl секунд rate снижается на shift_demotion.
    - При достижении min_target_rate → TRIGGER_EXTRIME.
    """
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.enable = cfg.get("enable", True)
        self.stab_avg = cfg.get("stabilization_ttl", 2)
        self.shift_ttl = cfg.get("shift_ttl", 60)
        self.shift_demotion = cfg.get("shift_demotion", 0.2)
        self.min_target_rate = cfg.get("min_target_rate", 0.3)

    def _calc_virtual_tp(self, pos: ActivePosition) -> float:
        """Вычисляет виртуальный TP без сайд-эффектов."""
        if pos.side == "LONG":
            return pos.entry_price + (pos.base_target_price_100 - pos.entry_price) * pos.current_target_rate
        return pos.entry_price - (pos.entry_price - pos.base_target_price_100) * pos.current_target_rate

    def analyze(self, depth: DepthTop, pos: ActivePosition, now: float) -> dict | None:
        if not self.enable:
            return None

        time_in_pos = now - pos.opened_at

        # 1. ВЫЧИСЛЕНИЕ ВИРТУАЛЬНОЙ ЦЕЛИ (только локально, НЕ пишем в pos!)
        # ИНВАРИАНТ: pos.current_close_price принадлежит Executor-у.
        # Он хранит цену РЕАЛЬНО размещённого ордера. Перезапись здесь ломает
        # идемпотентность Executor-а → лавина cancel+replace на каждый тик.
        virtual_tp = self._calc_virtual_tp(pos)

        # Ждем стабилизации после входа
        if not pos.is_average_stabilized:
            if time_in_pos >= self.stab_avg:
                pos.is_average_stabilized = True
                pos.last_shift_ts = now
                logger.info(
                    f"[{pos.symbol}] ✅ Average: стабилизация завершена. "
                    f"Виртуальный TP: {virtual_tp}, Rate: {pos.current_target_rate}"
                )
            else:
                return None

        # 2. ЛОГИКА СДВИГА (Shift) И КОМПРОМИССОВ
        if now - pos.last_shift_ts >= self.shift_ttl:
            if pos.current_target_rate <= self.min_target_rate:
                logger.info(
                    f"[{pos.symbol}] 🔥 Average: Rate={pos.current_target_rate} <= min={self.min_target_rate}. "
                    f"TRIGGER_EXTRIME."
                )
                return {"action": "TRIGGER_EXTRIME", "reason": "MIN_TARGET_RATE_TIMEOUT"}

            old_rate = pos.current_target_rate
            old_virtual_tp = virtual_tp

            new_rate = pos.current_target_rate - self.shift_demotion
            pos.current_target_rate = max(new_rate, self.min_target_rate)
            pos.last_shift_ts = now
            pos.negative_duration_sec = 0.0

            # Пересчитываем virtual_tp после понижения планки (только локально)
            virtual_tp = self._calc_virtual_tp(pos)

            logger.info(
                f"[{pos.symbol}] 📉 Average: СДВИГ ТП. "
                f"Rate: {old_rate:.3f} → {pos.current_target_rate:.3f} | "
                f"Виртуальный TP: {old_virtual_tp:.8g} → {virtual_tp:.8g}"
            )

            # Очищаем стакан от старых заявок при сдвиге
            if pos.close_order_id:
                return {"action": "CANCEL_CLOSE", "reason": "SHIFT_DEMOTION_TIMEOUT"}

        # 3. АКТИВНЫЙ ХАНТИНГ (Поиск уровня с максимальным объемом у/выше virtual_tp)
        ideal_target_price = None
        max_vol = -1.0

        if pos.side == "LONG":
            for price, vol in depth.bids:
                if price >= virtual_tp:
                    if vol > max_vol:
                        max_vol = vol
                        ideal_target_price = price
                else:
                    break
        else:
            for price, vol in depth.asks:
                if price <= virtual_tp:
                    if vol > max_vol:
                        max_vol = vol
                        ideal_target_price = price
                else:
                    break

        if ideal_target_price is not None:
            # ИНВАРИАНТ идемпотентности: pos.current_close_price здесь = цена РЕАЛЬНО стоящего
            # ордера (выставленная Executor-ом). Если ордер уже там — не трогаем.
            if pos.close_order_id and abs(pos.current_close_price - ideal_target_price) < 1e-8:
                return None

            return {"action": "PLACE_DYNAMIC_CLOSE", "price": ideal_target_price, "reason": "DYNAMIC_TP_HIT"}

        return None