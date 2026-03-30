from CORE.models import ActivePosition
from API.PHEMEX.stakan import DepthTop

class ExtrimeClose:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.enable = cfg.get("enable", True)
        self.retry_ttl = cfg.get("retry_ttl", 3)
        self.retry_num = cfg.get("retry_num", "inf")
        self.bid_to_ask_orientation = cfg.get("bid_to_ask_orientation", 0.0)
        # increase_fraction -- % от (ask1 - bid1). В конфиге 2.5 = 2.5% = 0.025
        self.increase_fraction = cfg.get("increase_fraction", 2.5) / 100

    def analyze(self, depth: DepthTop, pos: ActivePosition, now: float) -> dict | None:
        if not self.enable or not pos.in_extrime_mode: 
            return None
        
        # 1. Каждые retry_ttl секунд вычисляется новая exit-цена.
        if now - pos.last_extrime_try_ts < self.retry_ttl: 
            return None
            
        ask1 = depth.asks[0][0] if depth.asks else 0.0
        bid1 = depth.bids[0][0] if depth.bids else 0.0
        if not ask1 or not bid1: 
            return None

        # 3. Если число попыток ограничено и лимит исчерпан: действие не ожидается. Молимся Богу.
        if str(self.retry_num).lower() != "inf" and pos.extrime_retries_count >= int(self.retry_num):
            return None

        # 2. Цена рассчитывается:
        # относительно mid = (ask1 + bid1) / 2; -- ось.
        mid = (ask1 + bid1) / 2
        spread = ask1 - bid1
        
        # bid_to_ask_orientation == 0 -- прямо в эту ось, с последующими смещениями в компромисную сторону.
        base_price = mid + (spread * self.bid_to_ask_orientation)
        
        # с нарастающим ухудшением через increase_fraction. increase_fraction -- % от (ask1 - bid1).
        shift = spread * self.increase_fraction * pos.extrime_retries_count
        
        # Ухудшаем качество выхода (для LONG смещаем цену вниз к бидам, для SHORT - вверх к аскам)
        if pos.side == "LONG": 
            target_price = base_price - shift
        else: 
            target_price = base_price + shift
            
        pos.extrime_retries_count += 1
        pos.last_extrime_try_ts = now
        
        return {
            "action": "PLACE_EXTRIME_LIMIT", 
            "price": target_price, 
            "reason": f"EXTRIME_RETRY_{pos.extrime_retries_count}"
        }
    
# import time
# from CORE.models import ActivePosition

# class PositionTTLClose:
#     def __init__(self, cfg: dict):
#         self.cfg = cfg
#         self.enable = cfg.get("enable", True)
#         self.position_ttl = cfg.get("position_ttl", 3600)
#         self.to_entry_orientation = cfg.get("to_entry_orientation", 0.0)

#     def build_target_price(self, pos: ActivePosition) -> float:
#         """
#         СТРОГО ПО ТЗ 1.10.4:
#         Расчет цели БУ (относительно entry price с процентным смещением).
#         """
#         orient_pct = self.to_entry_orientation
#         if pos.side == "LONG":
#             return pos.entry_price * (1 + orient_pct / 100.0)
#         return pos.entry_price * (1 - orient_pct / 100.0)

#     def analyze(self, pos: ActivePosition, now: float) -> dict | None:
#         if not self.enable: 
#             return None
        
#         time_in_pos = now - pos.opened_at
        
#         if time_in_pos >= self.position_ttl:
#             # Если еще не в режиме БУ и не в экстриме — переводим в БУ
#             if not pos.in_breakeven_mode and not pos.in_extrime_mode:
#                 target = self.build_target_price(pos)
#                 return {"action": "TRIGGER_BREAKEVEN", "price": target}
            
#             # Если уже в БУ, ждем окно ожидания (например, 60 сек), затем рубим в экстрим
#             if pos.in_breakeven_mode and not pos.in_extrime_mode:
#                 if now - pos.breakeven_start_ts >= 60:
#                     return {"action": "TRIGGER_EXTRIME"}
                    
#         return None