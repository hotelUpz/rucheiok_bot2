# ============================================================
# FILE: EXIT/utils.py
# ============================================================
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from CORE.models import ActivePosition
    from API.PHEMEX.stakan import DepthTop

def get_top_bid_ask(depth: DepthTop) -> tuple[float, float]:
    """Безопасное извлечение лучших цен (bid1, ask1) из словарей стакана."""
    ask1 = min(depth.asks.keys()) if depth.asks else 0.0
    bid1 = max(depth.bids.keys()) if depth.bids else 0.0
    return bid1, ask1

def check_is_negative(pos: ActivePosition, depth: DepthTop, negative_spread_pct: float) -> bool:
    """Хелпер для проверки: находится ли позиция в просадке (ПНЛ <= порога)."""
    bid1, ask1 = get_top_bid_ask(depth)
    if not ask1 or not bid1: 
        return False

    if pos.side == "LONG":
        spread = (ask1 - pos.init_ask1) / pos.init_ask1 * 100
        return spread <= negative_spread_pct
    else:
        spread = (pos.init_bid1 - bid1) / pos.init_bid1 * 100
        return spread <= negative_spread_pct