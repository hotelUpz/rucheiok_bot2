# ============================================================
# FILE: ENTRY/engine.py
# ROLE: Оркестратор логики входа (Pattern math, Binance filter, TTLs, Funding)
# ============================================================
from __future__ import annotations

import time
from typing import Dict, Any, Optional, TYPE_CHECKING

from ENTRY.pattern_math import StakanEntryPattern, EntrySignal
from ENTRY.funding_filter import FundingFilter
from API.PHEMEX.stakan import DepthTop

if TYPE_CHECKING:
    from API.PHEMEX.funding import PhemexFunding


class EntryEngine:
    def __init__(self, cfg: Dict[str, Any], funding_api: PhemexFunding):
        self.cfg = cfg
        self.phemex_cfg: Dict[str, Any] = cfg["pattern"]["phemex"]
        self.binance_cfg: Dict[str, Any] = cfg["pattern"]["binance"]
        
        self.pattern_math = StakanEntryPattern(self.phemex_cfg)
        self.funding_filter = FundingFilter(cfg.get("pattern", {}).get("phemex_funding_filter", {}), funding_api)
        
        self.target_depth: int = self.phemex_cfg.get("depth", 8)
        self.pattern_ttl: int = self.phemex_cfg.get("pattern_ttl_sec", 0)
        
        self.binance_enabled: bool = self.binance_cfg.get("enable", True)
        self.min_price_spread: float = abs(self.binance_cfg.get("min_price_spread_pct", 0.1))
        self.spread_ttl: int = self.binance_cfg.get("spread_ttl_sec", 0)
        
        # Внутренние таймеры для отслеживания удержания сигнала
        self._pattern_first_seen: Dict[str, float] = {}
        self._spread_first_seen: Dict[str, float] = {}

    def analyze(self, depth: DepthTop, b_price: float, p_price: float) -> Optional[EntrySignal]:
        symbol: str = depth.symbol
        
        # Семантический опрос валидатора фандинга
        if not self.funding_filter.is_trade_allowed(symbol):
            return None
            
        now: float = time.time()
        
        # 1. Извлекаем ключи (цены) и сортируем их
        # bids - по убыванию (лучшая цена сверху), asks - по возрастанию
        top_bid_keys = sorted(depth.bids.keys(), reverse=True)[:self.target_depth]
        top_ask_keys = sorted(depth.asks.keys())[:self.target_depth]
        
        if len(top_bid_keys) < self.target_depth or len(top_ask_keys) < self.target_depth:
            return None

        # 2. Формируем список кортежей (price, qty) для математики.
        bids_sliced: list[tuple[float, float]] = [(p, depth.bids[p]) for p in top_bid_keys]
        asks_sliced: list[tuple[float, float]] = [(p, depth.asks[p]) for p in top_ask_keys]
        
        signal: Optional[EntrySignal] = self.pattern_math.analyze(bids_sliced, asks_sliced)
        
        if not signal:
            keys_to_pop = [f"{symbol}_LONG", f"{symbol}_SHORT"]
            for k in keys_to_pop:
                self._pattern_first_seen.pop(k, None)
                self._spread_first_seen.pop(k, None)
            return None
            
        pos_key: str = f"{symbol}_{signal['side']}"

        # Извлекаем базу для ТП из второго уровня
        ask2: float = asks_sliced[1][0] if len(asks_sliced) > 1 else asks_sliced[0][0]
        bid2: float = bids_sliced[1][0] if len(bids_sliced) > 1 else bids_sliced[0][0]
        signal["base_target_price_100"] = ask2 if signal["side"] == "LONG" else bid2

        passed_binance: bool = False
        spread_val: float = 0.0
        
        if self.binance_enabled:
            if b_price and p_price:
                spread_pct = (b_price - p_price) / p_price * 100
                passed_binance = (spread_pct >= self.min_price_spread) if signal["side"] == "LONG" else (spread_pct <= -self.min_price_spread)
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
        
        signal["b_price"] = b_price
        signal["p_price"] = p_price
        signal["spread"] = spread_val
        
        return signal