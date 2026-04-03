from __future__ import annotations
from typing import TYPE_CHECKING
from EXIT.utils import check_is_negative

if TYPE_CHECKING:
    from CORE.models import ActivePosition
    from API.PHEMEX.stakan import DepthTop

class Interference:
    """
    Скупка мелких помех в стакане на пути к TP.

    Инвариант:
    - Запускается только когда цена ещё НЕ ушла в профит (spread <= 0).
    - Суммарный объём скупки ≤ max_vol_pct * init_qty.
    - Каждый чанк ≤ avg_vol_pct * init_qty.
    - Если помеха крупнее чанка — break (нельзя пробить физически).
    - Если уровень превысил max_retries_per_level — break (та же физика).
    - WS-ответ на заполнение обрабатывается в _process_interference (+qty), не в _process_close (-qty).
    """
    def __init__(self, cfg: dict, min_exchange_notional: float = 5.0):
        self.cfg = cfg
        self.enable = cfg.get("enable", False)
        
        # 1. Задержка перед стартом скупки (чтобы стакан успокоился)
        self.stab_ttl = cfg.get("stabilization_ttl", 2.0)
        
        # 2. Лимиты объемов
        self.avg_vol_pct = cfg.get("average_vol_pct_to_init_size", 3) / 100
        self.max_vol_pct = cfg.get("max_vol_pct_to_init_size", 9) / 100
        self.max_retries_per_level = cfg.get("max_retries_per_level", 2)
        self.min_exchange_notional = min_exchange_notional
        
        # Скупаем только если ПНЛ нулевой или отрицательный (спред <= 0)
        self.negative_spread_pct = 0.0

    def analyze(self, depth: DepthTop, pos: ActivePosition, now: float) -> dict | None:
        if not self.enable or pos.interference_disabled:
            return None
            
        if pos.qty <= 0 or pos.current_close_price <= 0:
            return None

        # --- 1. ПАУЗА СТАБИЛИЗАЦИИ ---
        time_in_pos = now - pos.opened_at
        if time_in_pos < self.stab_ttl:
            return None

        # --- 2. ПРОВЕРКА ПНЛ (Используем общий хелпер) ---
        if not check_is_negative(pos, depth, self.negative_spread_pct):
            return None

        # --- 3. РАСЧЕТ ДОСТУПНОГО ОБЪЕМА ---
        allowed_remains = (pos.init_qty * self.max_vol_pct) - pos.interf_bought_qty
        if allowed_remains <= 0:
            pos.interference_disabled = True
            return None

        max_chunk_vol = pos.init_qty * self.avg_vol_pct

        # --- 4. ПОИСК ЦЕЛИ ---
        target = self._find_target(depth, pos, max_chunk_vol)

        if target:
            price, _ = target
            buy_qty = min(max_chunk_vol, allowed_remains)
            
            # Биржа не пропустит ордера меньше ~5 USDT
            approx_value_usdt = buy_qty * price
            if approx_value_usdt < self.min_exchange_notional:
                pos.interference_disabled = True
                return None
                
            return {"action": "BUY_OUT_INTERFERENCE", "price": price, "qty": buy_qty}

        return None
    
    def _find_target(self, depth: DepthTop, pos: ActivePosition, max_chunk_vol: float) -> tuple[float, float] | None:
        """
        Сканирует стакан и ищет первую доступную преграду ("котях").
        Если преграда слишком большая (vol > max_chunk_vol) - поиск прерывается (Законы физики).
        """
        if pos.side == "LONG":
            # Ищем сопротивление (Аски) от лучших (дешевых) к худшим. Они уже отсортированы стримом!
            for price, vol in depth.asks:
                if price <= pos.entry_price or price >= pos.current_close_price:
                    continue
                    
                if vol > max_chunk_vol:
                    break 
                    
                if pos.failed_interference_prices.get(str(price), 0) >= self.max_retries_per_level:
                    break 
                    
                return (price, vol)
        else:
            # Ищем поддержку (Биды) от лучших (дорогих) к худшим. Они уже отсортированы стримом!
            for price, vol in depth.bids:
                if price >= pos.entry_price or price <= pos.current_close_price:
                    continue
                    
                if vol > max_chunk_vol:
                    break
                    
                if pos.failed_interference_prices.get(str(price), 0) >= self.max_retries_per_level:
                    break
                    
                return (price, vol)

        return None