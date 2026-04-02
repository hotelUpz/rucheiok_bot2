from __future__ import annotations

from typing import TYPE_CHECKING
if TYPE_CHECKING:
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

        # 1. ВЫЧИСЛЕНИЕ ВИРТУАЛЬНОЙ ЦЕЛИ
        if pos.side == "LONG":
            virtual_tp = pos.entry_price + (pos.base_target_price_100 - pos.entry_price) * pos.current_target_rate
        else:
            virtual_tp = pos.entry_price - (pos.entry_price - pos.base_target_price_100) * pos.current_target_rate

        pos.current_close_price = virtual_tp  

        # Ждем стабилизации после входа
        if not pos.is_average_stabilized:
            if time_in_pos >= self.stab_avg:
                pos.is_average_stabilized = True
                pos.last_shift_ts = now
            else:
                return None
        
        # 2. ЛОГИКА СДВИГА (Shift) И КОМПРОМИССОВ
        if now - pos.last_shift_ts >= self.shift_ttl:
            if pos.current_target_rate <= self.min_target_rate:
                return {"action": "TRIGGER_EXTRIME", "reason": "MIN_TARGET_RATE_TIMEOUT"}

            new_rate = pos.current_target_rate - self.shift_demotion
            pos.current_target_rate = max(new_rate, self.min_target_rate)
            pos.last_shift_ts = now
            pos.negative_duration_sec = 0.0 
            
            # Пересчитываем цель после понижения планки
            if pos.side == "LONG":
                pos.current_close_price = pos.entry_price + (pos.base_target_price_100 - pos.entry_price) * pos.current_target_rate
            else:
                pos.current_close_price = pos.entry_price - (pos.entry_price - pos.base_target_price_100) * pos.current_target_rate

            # Очищаем стакан от старых заявок при сдвиге
            if pos.close_order_id:
                return {"action": "CANCEL_CLOSE", "reason": "SHIFT_DEMOTION_TIMEOUT"}

        # if pos.close_order_id:
        #     return None

        # # 3. АКТИВНЫЙ ХАНТИНГ (Поиск уровня с максимальным объемом)
        # target_price = None
        # max_vol = -1.0

        # if pos.side == "LONG":
        #     # Для лонга: ищем БИД с максимальным объемом среди тех, что >= pos.current_close_price
        #     for price, vol in depth.bids:
        #         if price >= pos.current_close_price:
        #             if vol > max_vol:
        #                 max_vol = vol
        #                 target_price = price
        #         else:
        #             break # Биды отсортированы по убыванию цены, дальше смотреть нет смысла (цены хуже)
        # else:
        #     # Для шорта: ищем АСК с максимальным объемом среди тех, что <= pos.current_close_price
        #     for price, vol in depth.asks:
        #         if price <= pos.current_close_price:
        #             if vol > max_vol:
        #                 max_vol = vol
        #                 target_price = price
        #         else:
        #             break # Аски отсортированы по возрастанию цены, дальше смотреть нет смысла (цены хуже)

        # if target_price is not None:
        #     # Бьем прямо в самый плотный уровень
        #     return {"action": "PLACE_DYNAMIC_CLOSE", "price": target_price, "reason": "DYNAMIC_TP_HIT"}

        # return None

        # 3. АКТИВНЫЙ ХАНТИНГ (Поиск уровня с максимальным объемом)
        ideal_target_price = None
        max_vol = -1.0

        if pos.side == "LONG":
            for price, vol in depth.bids:
                if price >= pos.current_close_price:
                    if vol > max_vol:
                        max_vol = vol
                        ideal_target_price = price
                else:
                    break
        else:
            for price, vol in depth.asks:
                if price <= pos.current_close_price:
                    if vol > max_vol:
                        max_vol = vol
                        ideal_target_price = price
                else:
                    break

        if ideal_target_price is not None:
            # Если ордер уже стоит на ЭТОЙ ЖЕ цене - не суетимся, ждем (возвращаем None)
            if pos.close_order_id and abs(pos.current_close_price - ideal_target_price) < 1e-8:
                return None
                
            # Если ордера нет ИЛИ стенка объема уехала на другую цену - бьем туда!
            # (Executor сам отменит старый ордер перед постановкой этого)
            return {"action": "PLACE_DYNAMIC_CLOSE", "price": ideal_target_price, "reason": "DYNAMIC_TP_HIT"}

        return None