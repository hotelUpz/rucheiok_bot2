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
        self.enable = cfg["enable"]
        self.retry_ttl = cfg["retry_ttl"]
        self.retry_num = cfg["retry_num"]
        self.bid_to_ask_orientation = cfg["bid_to_ask_orientation"]
        self.increase_fraction = cfg["increase_fraction"] / 100.0

    def scen_extrime_analyze(self, depth: DepthTop, pos: ActivePosition, now: float) -> float | None:
        if not self.enable: return None
        if pos.current_qty <= 0.0: return None        
        if now - pos.last_extrime_try_ts < self.retry_ttl: return None # 
        
        # ИСПОЛЬЗУЕМ УТИЛИТУ
        bid1, ask1 = get_top_bid_ask(depth)
        if not ask1 or not bid1: return None

        if str(self.retry_num).lower() != "inf" and pos.extrime_retries_count >= int(self.retry_num):
            logger.warning(f"Осторожно! extrime_retries_count >= retry_num, но позиция {pos.symbol} не закрыта!")
            return None

        mid = (ask1 + bid1) / 2
        spread = ask1 - bid1
        base_price = mid + (spread * self.bid_to_ask_orientation) if pos.side == "LONG"\
            else mid - (spread * self.bid_to_ask_orientation)
        shift = spread * self.increase_fraction * pos.extrime_retries_count
        
        return base_price - shift if pos.side == "LONG" else base_price + shift