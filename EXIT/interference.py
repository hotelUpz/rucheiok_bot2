from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from CORE.models_fsm import ActivePosition
    from API.PHEMEX.stakan import DepthTop

class Interference:
    """
    Скупка мелких помех в стакане на пути к TP.
    """
    def __init__(self, cfg: dict, min_exchange_notional: float = 5.0):
        self.cfg = cfg
        self.enable = cfg.get("enable", False)
        
        # 1. Задержка перед стартом скупки (чтобы стакан успокоился)
        self.stab_ttl = cfg.get("stabilization_ttl", 2.0)
        
        # 2. Лимиты объемов
        self.usual_vol_pct = cfg.get("usual_vol_pct_to_init_size", 3) / 100
        self.max_vol_pct = cfg.get("max_vol_pct_to_init_size", 9) / 100
        self.min_exchange_notional = min_exchange_notional
        
        # Скупаем только если ПНЛ нулевой или отрицательный (спред <= 0)
        self.negative_spread_pct = 0.0

    # и get_top_bid_ask и check_is_negative можно и нужно вынести во внешнюю утилиту ибо используются в разных модулях.
    @staticmethod
    def get_top_bid_ask(depth: DepthTop) -> tuple[float, float]:
        """Быстрое извлечение лучших цен (bid1, ask1) из списков стакана."""
        ask1 = depth.asks[0][0] if depth.asks else 0.0
        bid1 = depth.bids[0][0] if depth.bids else 0.0
        return bid1, ask1

    def check_is_negative(self, pos: ActivePosition, depth: DepthTop, negative_spread_pct: float) -> bool:
        """Хелпер для проверки: находится ли позиция в просадке (ПНЛ <= порога)."""
        bid1, ask1 = self.get_top_bid_ask(depth)
        if not ask1 or not bid1: 
            return False

        if pos.side == "LONG":
            spread = (ask1 - pos.init_ask1) / pos.init_ask1 * 100
            return spread <= negative_spread_pct
        else:
            spread = (pos.init_bid1 - bid1) / pos.init_bid1 * 100
            return spread <= negative_spread_pct

    def _find_target(self, depth: DepthTop, pos: ActivePosition, max_chunk_vol: float) -> tuple[float, float] | None:
        """
        Сканирует стакан и ищет первую доступную преграду ("котях").
        Если преграда слишком большая (vol > max_chunk_vol) - поиск прерывается (Законы физики).
        """
        if pos.side == "LONG":
            # Ищем сопротивление (Аски) от лучших (дешевых) к худшим. Они уже отсортированы стримом!
            for price, vol in depth.asks:
                if price < pos.base_target_price_100 and vol <= max_chunk_vol: return (price, vol)
        else:
            # Ищем поддержку (Биды) от лучших (дорогих) к худшим. Они уже отсортированы стримом!
            for price, vol in depth.bids:
                if price > pos.base_target_price_100 and vol <= max_chunk_vol: return (price, vol)

        return None    

    def analyze(self, depth: DepthTop, pos: ActivePosition, now: float) -> tuple[float, float] | None:
        if not self.enable:
            return None
        
        if pos.in_breakeven_mode or pos.in_extrime_mode:
            return None
            
        if pos.current_qty <= 0.0 or pos.interference_disabled:
            return None

        # --- 1. ПАУЗА СТАБИЛИЗАЦИИ ---
        time_in_pos = now - pos.opened_at
        if time_in_pos < self.stab_ttl:
            return None

        # --- 2. ПРОВЕРКА ПНЛ (Используем общий хелпер) ---
        if not self.check_is_negative(pos, depth, self.negative_spread_pct):
            return None

        # --- 3. РАСЧЕТ ДОСТУПНОГО ОБЪЕМА ---
        allowed_remains = (pos.pending_qty * self.max_vol_pct) - pos.interf_comulative_qty
        if allowed_remains <= 0: # поменять на self.min_exchange_notional (только брать конкретно из спецификации биржи, не хардкодить) (можно для удобства доступа занести в pos структуру.)
            pos.interference_disabled = True
            return None

        max_chunk_vol = pos.pending_qty * self.usual_vol_pct

        # --- 4. ПОИСК ЦЕЛИ ---
        # target = self._find_target(depth, pos, max_chunk_vol) # возможно вместо max_chunk_vol нужно allowed_remains, а уже какой дают такой и брать.
        target = self._find_target(depth, pos, allowed_remains) # возможно вместо max_chunk_vol нужно allowed_remains, а уже какой дают такой и брать.

        if target:
            price, _ = target
            buy_qty = min(max_chunk_vol, allowed_remains)
            
            # Биржа не пропустит ордера меньше ~5 USDT
            approx_value_usdt = buy_qty * price
            if approx_value_usdt < self.min_exchange_notional:
                return None
                
            return price, buy_qty

        return None