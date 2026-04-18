# ============================================================
# FILE: ENTRY/engine.py
# ROLE: Оркестратор логики входа (Pattern math, Binance filter, TTLs, Funding)
# ============================================================
from __future__ import annotations

import time
from typing import Dict, Any, Optional, TYPE_CHECKING

from ENTRY.pattern_math import StakanEntryPattern

if TYPE_CHECKING:
    from API.PHEMEX.funding import PhemexFunding
    from API.BINANCE.funding import BinanceFunding
    from ENTRY.pattern_math import EntrySignal
    from API.PHEMEX.stakan import DepthTop
    from ENTRY.funding_filters import FundingFilter1, FundingFilter2
    from ENTRY.funding_manager import FundingManager


class SignalEngine:
    def __init__(self, cfg: Dict[str, Any], funding_manager: 'FundingManager'):
        self.cfg = cfg
        self.funding_manager = funding_manager  
        
        self.phemex_cfg: Dict[str, Any] = cfg["pattern"]["phemex"]
        self.binance_cfg: Dict[str, Any] = cfg["pattern"]["binance"]
        
        self.pattern_math = StakanEntryPattern(self.phemex_cfg)
        
        self.target_depth: int = self.phemex_cfg["depth"]
        self.pattern_ttl: int = self.phemex_cfg["pattern_ttl_sec"]
        
        self.binance_enabled: bool = self.binance_cfg["enable"]
        self.min_price_spread_rate: float = abs(self.binance_cfg["min_price_spread_rate"])
        self.spread_ttl: int = self.binance_cfg["spread_ttl_sec"]
        
        self._pattern_first_seen: Dict[str, float] = {}
        self._spread_first_seen: Dict[str, float] = {}

    def analyze(self, depth: DepthTop, b_price: float, p_price: float) -> Optional[EntrySignal]:
        symbol: str = depth.symbol
        
        # Семантический опрос валидатора фандинга
        if not self.funding_manager.is_trade_allowed(symbol):
            return None
            
        now: float = time.time()
        
        # 1. Извлекаем нужную глубину из уже отсортированных стримом списков
        bids_sliced = depth.bids[:self.target_depth]
        asks_sliced = depth.asks[:self.target_depth]
        
        if len(bids_sliced) < self.target_depth or len(asks_sliced) < self.target_depth:
            return None

        # 2. Передаем в математику
        signal: Optional[EntrySignal] = self.pattern_math.analyze(bids_sliced, asks_sliced)
        
        if not signal:
            keys_to_pop = [f"{symbol}_LONG", f"{symbol}_SHORT"]
            for k in keys_to_pop:
                self._pattern_first_seen.pop(k, None)
                self._spread_first_seen.pop(k, None)
            return None
            
        pos_key: str = f"{symbol}_{signal.side}"

        passed_binance: bool = False
        spread_val: float = 0.0
        
        if self.binance_enabled:
            if b_price and p_price:
                spread_pct = (b_price - p_price) / p_price * 100
                min_price_spread = abs(signal.spr2_pct * self.min_price_spread_rate)
                passed_binance = (spread_pct >= min_price_spread) if signal.side == "LONG" else (spread_pct <= -min_price_spread)
                spread_val = abs(spread_pct)
        else:
            passed_binance = True

        if not passed_binance:
            self._spread_first_seen.pop(pos_key, None)
            return None

        if self.pattern_ttl > 0:
            first_seen_p = self._pattern_first_seen.setdefault(pos_key, now)
            if now - first_seen_p < self.pattern_ttl: 
                return None

        if self.binance_enabled and self.spread_ttl > 0:
            first_seen_s = self._spread_first_seen.setdefault(pos_key, now)
            if now - first_seen_s < self.spread_ttl: 
                return None

        self._pattern_first_seen.pop(pos_key, None)
        self._spread_first_seen.pop(pos_key, None)
        
        signal.b_price = b_price
        signal.p_price = p_price
        signal.spread = spread_val
        
        return signal