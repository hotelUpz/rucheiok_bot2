import time
from CORE.models import ActivePosition
from API.PHEMEX.stakan import DepthTop

class AverageScenario:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.enable = cfg.get("enable", True)
        self.stab_avg = cfg.get("stabilization_ttl", 2)
        self.shift_ttl = cfg.get("shift_ttl", 60)
        self.shift_demotion = cfg.get("shift_demotion", 0.2)
        self.min_target_rate = cfg.get("min_target_rate", 0.3)

    def analyze(self, depth: DepthTop, pos: ActivePosition, now: float) -> dict | None:
        if not self.enable:
            return None

        time_in_pos = now - pos.opened_at

        # Виртуальный расчет тейк профита
        if pos.side == "LONG":
            virtual_tp = pos.entry_price + (pos.base_target_price_100 - pos.entry_price) * pos.current_target_rate
        else:
            virtual_tp = pos.entry_price - (pos.entry_price - pos.base_target_price_100) * pos.current_target_rate

        pos.current_close_price = virtual_tp  

        if not pos.is_average_stabilized:
            if time_in_pos >= self.stab_avg:
                pos.is_average_stabilized = True
                pos.last_shift_ts = now
            else:
                return None
        
        # Логика сдвига 
        if now - pos.last_shift_ts >= self.shift_ttl:
            if pos.current_target_rate <= self.min_target_rate:
                return {"action": "TRIGGER_EXTRIME", "reason": "MIN_TARGET_RATE_TIMEOUT"}

            new_rate = pos.current_target_rate - self.shift_demotion
            pos.current_target_rate = max(new_rate, self.min_target_rate)
            pos.last_shift_ts = now
            pos.negative_duration_sec = 0.0 
            
            # Пересчитываем цель после сдвига
            if pos.side == "LONG":
                pos.current_close_price = pos.entry_price + (pos.base_target_price_100 - pos.entry_price) * pos.current_target_rate
            else:
                pos.current_close_price = pos.entry_price - (pos.entry_price - pos.base_target_price_100) * pos.current_target_rate

            # Если старый ордер всё еще висит в стакане — мы обязаны его отменить для переоценки!
            if pos.close_order_id:
                return {"action": "CANCEL_CLOSE", "reason": "SHIFT_DEMOTION_TIMEOUT"}

        if pos.close_order_id:
            return None

        # Динамический поиск объема
        best_price = None
        if pos.side == "LONG":
            for price, vol in depth.bids:
                if price >= pos.current_close_price:
                    best_price = price
                    break
        else:
            for price, vol in depth.asks:
                if price <= pos.current_close_price:
                    best_price = price
                    break

        if best_price is not None:
            return {"action": "PLACE_DYNAMIC_CLOSE", "price": best_price, "reason": "DYNAMIC_TP_HIT"}

        return None