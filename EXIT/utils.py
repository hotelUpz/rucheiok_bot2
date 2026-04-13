from __future__ import annotations

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from CORE.models_fsm import ActivePosition
    from API.PHEMEX.stakan import DepthTop


def get_top_bid_ask(depth: DepthTop) -> tuple[float, float]:
    """Быстрое извлечение лучших цен (bid1, ask1) из списков стакана."""
    ask1 = depth.asks[0][0] if depth.asks else 0.0
    bid1 = depth.bids[0][0] if depth.bids else 0.0
    return bid1, ask1

def check_is_negative(pos: ActivePosition, depth: DepthTop, negative_spread_pct: float, math_mode: int = 2) -> bool:
    """Хелпер для проверки: находится ли позиция в просадке (ПНЛ <= порога)."""
    bid1, ask1 = get_top_bid_ask(depth)
    if not ask1 or not bid1: 
        return False
    
    if math_mode == 1:
        if pos.side == "LONG":
            spread = (ask1 - pos.init_ask1) / pos.init_ask1 * 100
            return spread <= negative_spread_pct
        else:
            spread = (pos.init_bid1 - bid1) / pos.init_bid1 * 100
            return spread <= negative_spread_pct
        
    elif math_mode == 2:
        cur_mid = (ask1 + bid1) / 2

        if pos.side == "LONG":
            spread = (cur_mid - pos.mid_price) / pos.mid_price * 100
            return spread <= negative_spread_pct
        else:
            spread = (pos.mid_price - cur_mid) / pos.mid_price * 100
            return spread <= negative_spread_pct