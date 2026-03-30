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

        # Обязательно обновляем виртуальную цель в стейте, чтобы Интерференция знала диапазон!
        pos.current_close_price = virtual_tp  

        # 1. Первичная постановка цели (Ждем стабилизации)
        if not pos.is_average_stabilized:
            if time_in_pos >= self.stab_avg:
                pos.is_average_stabilized = True
                pos.last_shift_ts = now # Таймер сдвигов стартует только сейчас
            else:
                return None
        
        # 2. Логика сдвига (Уже стабилизированы)
        if now - pos.last_shift_ts >= self.shift_ttl:
            new_rate = pos.current_target_rate - self.shift_demotion
            if pos.current_target_rate <= self.min_target_rate:
                # Если уже на минималке и прошло время -> Экстрим
                return {"action": "TRIGGER_EXTRIME", "reason": "MIN_TARGET_RATE_TIMEOUT"}
                
            pos.current_target_rate = max(new_rate, self.min_target_rate)
            pos.last_shift_ts = now
            pos.negative_duration_sec = 0.0 # Сброс негативного таймера по ТЗ
            
            # Пересчитываем TP
            if pos.side == "LONG":
                virtual_tp = pos.entry_price + (pos.base_target_price_100 - pos.entry_price) * pos.current_target_rate
            else:
                virtual_tp = pos.entry_price - (pos.entry_price - pos.base_target_price_100) * pos.current_target_rate
            pos.current_close_price = virtual_tp

        # 3. Динамический поиск объема в стакане
        # Если ордер уже висит на бирже (ждет налива), не спамим
        if pos.close_order_id:
            return None

        best_price = None
        if pos.side == "LONG":
            # Для лонга ищем объем в бидах
            for price, vol in depth.bids:
                if price >= virtual_tp:
                    best_price = price
                    break
        else:
            # Для шорта ищем объем в асках
            for price, vol in depth.asks:
                if price <= virtual_tp:
                    best_price = price
                    break

        if best_price is not None:
            return {"action": "PLACE_DYNAMIC_CLOSE", "price": best_price, "reason": "DYNAMIC_TP_HIT"}

        return None