from __future__ import annotations
from typing import TYPE_CHECKING
from c_log import UnifiedLogger

# ИМПОРТ ИЗ УТИЛИТ
from EXIT.utils import get_top_bid_ask

if TYPE_CHECKING:
    from CORE.models_fsm import ActivePosition
    from API.PHEMEX.stakan import DepthTop

logger = UnifiedLogger("exit")

class ExtrimeClose:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.enable = cfg.get("enable", True)
        self.retry_ttl = cfg.get("retry_ttl", 3)
        self.retry_num = cfg.get("retry_num", "inf")
        self.bid_to_ask_orientation = cfg.get("bid_to_ask_orientation", 0.0)
        self.increase_fraction = cfg.get("increase_fraction", 2.5) / 100

    def analyze(self, depth: DepthTop, pos: ActivePosition, now: float) -> float | None:
        if not self.enable: return None
        
        # ЗАМЕНА на min_notional
        # if pos.current_qty < pos.min_notional_asset: return None
        if pos.current_qty == 0.0: return None
        
        if now - pos.last_extrime_try_ts < self.retry_ttl: return None
        
        # ИСПОЛЬЗУЕМ УТИЛИТУ
        bid1, ask1 = get_top_bid_ask(depth)
        if not ask1 or not bid1: return None

        if str(self.retry_num).lower() != "inf" and pos.extrime_retries_count >= int(self.retry_num):
            logger.warning(f"Осторожно! extrime_retries_count >= retry_num, но позиция {pos.symbol} не закрыта!")
            return None

        mid = (ask1 + bid1) / 2
        spread = ask1 - bid1
        base_price = mid + (spread * self.bid_to_ask_orientation)
        shift = spread * self.increase_fraction * pos.extrime_retries_count
        
        target_price = base_price - shift if pos.side == "LONG" else base_price + shift
            
        pos.extrime_retries_count += 1
        pos.last_extrime_try_ts = now
        
        return target_price