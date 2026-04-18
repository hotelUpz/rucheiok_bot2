# ============================================================
# FILE: CORE/funding_filters.py
# ROLE: Чистая математика для фильтров фандинга (SRP)
# ============================================================
from __future__ import annotations

from typing import Set, Dict, Any, TYPE_CHECKING
from c_log import UnifiedLogger

if TYPE_CHECKING:
    from API.PHEMEX.funding import FundingInfo as PhmFundingInfo
    from API.BINANCE.funding import FundingInfo as BinanceFundingInfo

logger = UnifiedLogger("entry")

BUFFER_TIME = -10.0

class FundingFilter1:
    def __init__(self, cfg: dict[str, Any]):
        self.enable: bool = cfg["enable"]
        self.threshold: float = cfg["threshold_pct"] / 100.0
        self.skip_sec: int = cfg["skip_before_counter_sec"]
        
        self.blocked_symbols: Set[str] = set()
        self._last_blocked: Set[str] = set()

    def process(self, phemex_cache: Dict[str, PhmFundingInfo], now_ms: float) -> None:
        if not self.enable: 
            return
            
        current_blocked: Set[str] = set()
        
        for sym, info in phemex_cache.items():
            time_left_sec = (info.next_funding_time_ms - now_ms) / 1000.0
            
            if BUFFER_TIME <= time_left_sec <= self.skip_sec:
                if abs(info.funding_rate) >= self.threshold:
                    current_blocked.add(sym)
                    # logger.debug(f"[{sym}] Фандинг 1 триггер: rate={info.funding_rate*100}%, time_left={time_left_sec:.1f}s")
        
        self.blocked_symbols = current_blocked
        
        if current_blocked != self._last_blocked:
            if current_blocked:
                logger.info(f"💸 [Фандинг 1] Блок Phemex! Под запретом {len(current_blocked)} монет: {', '.join(list(current_blocked)[:5])}...")
            elif self._last_blocked:
                logger.info("💸 [Фандинг 1] Блок снят со всех монет.")
            self._last_blocked = current_blocked

    def is_allowed(self, symbol: str) -> bool:
        return not self.enable or symbol not in self.blocked_symbols


class FundingFilter2:
    def __init__(self, cfg: dict[str, Any]):
        self.enable: bool = cfg["enable"]
        self.diff_threshold: float = cfg["diff_threshold_pct"] / 100.0
        self.skip_sec: int = cfg["skip_before_counter_sec"]
        
        self.blocked_symbols: Set[str] = set()
        self._last_blocked: Set[str] = set()

    def process(self, phemex_cache: Dict[str, PhmFundingInfo],
                binance_cache: Dict[str, BinanceFundingInfo], diffs: Dict[str, float], now_ms: float) -> None:
        if not self.enable: 
            return
            
        current_blocked: Set[str] = set()
        
        for sym, p_info in phemex_cache.items():
            b_info = binance_cache.get(sym)
            if not b_info:
                continue
                
            diff = diffs.get(sym, 0.0)
            
            # Берем минимальное время до фандинга из двух (чтобы быть в безопасности на любой бирже)
            time_left_p = (p_info.next_funding_time_ms - now_ms) / 1000.0
            time_left_b = (b_info.next_funding_time_ms - now_ms) / 1000.0
            min_time_left = min(time_left_p, time_left_b)
            
            if BUFFER_TIME <= min_time_left <= self.skip_sec:
                if diff >= self.diff_threshold:
                    current_blocked.add(sym)
                    # logger.debug(f"[{sym}] Фандинг 2 (Diff) триггер: diff={diff*100}%, min_time_left={min_time_left:.1f}s")
        
        self.blocked_symbols = current_blocked
        
        if current_blocked != self._last_blocked:
            # if current_blocked:
            #     # logger.info(f"⚖️ [Фандинг 2] Diff-Блок! Под запретом {len(current_blocked)} монет: {', '.join(list(current_blocked)[:5])}...")
            # elif self._last_blocked:
            #     # logger.info("⚖️ [Фандинг 2] Diff-Блок снят со всех монет.")
            self._last_blocked = current_blocked

    def is_allowed(self, symbol: str) -> bool:
        return not self.enable or symbol not in self.blocked_symbols