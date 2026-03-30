# ============================================================
# FILE: ENTRY/engine.py
# ROLE: Оркестратор логики входа (Pattern math, Binance filter, TTLs, Funding)
# ============================================================

import time
from typing import Dict, Any, Optional
from ENTRY.pattern_math import StakanEntryPattern
from ENTRY.funding_filter import FundingFilter
from API.PHEMEX.stakan import DepthTop

class EntryEngine:
    def __init__(self, cfg: Dict[str, Any], funding_api):
        self.cfg = cfg
        self.phemex_cfg = cfg["pattern"]["phemex"]
        self.binance_cfg = cfg["pattern"]["binance"]
        
        self.pattern_math = StakanEntryPattern(self.phemex_cfg)
        # Фандинг-фильтр лежит на уровне pattern.*, а не внутри pattern.phemex.*
        self.funding_filter = FundingFilter(cfg.get("pattern", {}).get("phemex_funding_filter", {}), funding_api)
        
        self.target_depth = self.phemex_cfg.get("depth", 8)
        self.pattern_ttl = self.phemex_cfg.get("pattern_ttl_sec", 0)
        
        self.binance_enabled = self.binance_cfg.get("enable", True)
        self.min_price_spread = abs(self.binance_cfg.get("min_price_spread_pct", 0.1))
        self.spread_ttl = self.binance_cfg.get("spread_ttl_sec", 0)
        
        self._pattern_first_seen: Dict[str, float] = {}
        self._spread_first_seen: Dict[str, float] = {}

    def analyze(self, depth: DepthTop, b_price: float, p_price: float) -> Optional[Dict[str, Any]]:
        symbol = depth.symbol
        
        if self.funding_filter.is_blocked(symbol):
            return None
            
        now = time.time()
        bids_sliced = depth.bids[:self.target_depth]
        asks_sliced = depth.asks[:self.target_depth]
        
        signal = self.pattern_math.analyze(bids_sliced, asks_sliced)
        if not signal:
            self._pattern_first_seen.pop(symbol, None)
            self._spread_first_seen.pop(symbol, None)
            return None

        passed_binance = False
        spread_val = 0.0
        if self.binance_enabled:
            if b_price and p_price:
                spread_pct = (b_price - p_price) / p_price * 100
                passed_binance = (spread_pct >= self.min_price_spread) if signal["side"] == "LONG" else (spread_pct <= -self.min_price_spread)
                spread_val = abs(spread_pct)
        else:
            passed_binance = True

        if not passed_binance:
            self._spread_first_seen.pop(symbol, None)
            return None

        if self.pattern_ttl > 0:
            first_seen_p = self._pattern_first_seen.setdefault(symbol, now)
            if now - first_seen_p < self.pattern_ttl: return None

        if self.binance_enabled and self.spread_ttl > 0:
            first_seen_s = self._spread_first_seen.setdefault(symbol, now)
            if now - first_seen_s < self.spread_ttl: return None

        self._pattern_first_seen.pop(symbol, None)
        self._spread_first_seen.pop(symbol, None)
        
        signal["b_price"] = b_price
        signal["p_price"] = p_price
        signal["spread"] = spread_val
        return signal