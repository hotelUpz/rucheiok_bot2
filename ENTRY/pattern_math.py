# ============================================================
# FILE: ENTRY/pattern_math.py
# ROLE: Оптимизированная математика паттернов стакана (HFT)
# ============================================================

from __future__ import annotations
from typing import Optional, TypedDict, Literal, Any, List

class EntrySignal(TypedDict, total=False):
    side: Literal["LONG", "SHORT"]
    price: float
    spr3_pct: float
    rate: float
    row_vol_usdt: float
    base_target_price_100: float
    b_price: float
    p_price: float
    spread: float
    row_vol_asset: float

class StakanEntryPattern:
    def __init__(self, phemex_cfg: dict[str, Any]):
        self.cfg = phemex_cfg
        self.enabled: bool = self.cfg.get("enable", True)
        self.depth: int = self.cfg.get("depth", 8)
        self.min_vol: Optional[float] = self.cfg.get("min_first_row_usdt_notional")
        self.max_vol: Optional[float] = self.cfg.get("max_first_row_usdt_notional")
        
        btm = self.cfg.get("bottom", {})
        self.min_spr2: float = btm.get("min_spread_between_two_row_pct", 0.0)
        self.min_spr3: float = btm.get("min_spread_between_three_row_pct", 0.0)
        
        hdr = self.cfg.get("header", {})
        self.roc_window: int = hdr.get("roc_window", 0)
        self.max_one_roc: float = hdr.get("max_one_roc_pct", 0.0)
        
        body = self.cfg.get("body", {})
        self.sma_window: int = body.get("roc_sma_window", 0)
        
        self.desired_rate: float = self.cfg.get("header_to_bottom_desired_rate", 0.0)
        self.max_dist_rate: float = self.cfg.get("max_bid_ask_distance_rate", 0.0)

    def analyze(self, bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> Optional[EntrySignal]:
        if not self.enabled or len(bids) < self.depth or len(asks) < self.depth:
            return None

        return self._check_pattern(asks, bids, "LONG") or self._check_pattern(bids, asks, "SHORT")

    def _check_pattern(self, side: list[tuple[float, float]], opp_side: list[tuple[float, float]], direction: Literal["LONG", "SHORT"]) -> Optional[EntrySignal]:
        p1, p2, p3 = side[0][0], side[1][0], side[2][0]
        opp_p1 = opp_side[0][0]
        
        # 1. Проверка объема первой строки
        vol_usdt = side[0][1] * p1
        if (self.min_vol and vol_usdt < self.min_vol) or (self.max_vol and vol_usdt > self.max_vol):
            return None

        # 2. Расчет спредов (bottom)
        spr2_pct = abs(p2 - p1) / p1 * 100
        spr3_pct = abs(p3 - p1) / p1 * 100
        
        if spr2_pct < self.min_spr2 or spr3_pct < self.min_spr3:
            return None

        # 3. Дистанция bid/ask
        dist_denom = abs(p3 - p1)
        if dist_denom <= 0: return None
        
        if abs(p1 - opp_p1) / dist_denom > self.max_dist_rate:
            return None

        # 4. Лесенка ROC (header & body)
        rocs: List[float] = []
        for i in range(self.depth - 1, self.depth - 1 - self.sma_window, -1):
            prev_p = side[i-1][0]
            rocs.append(abs(side[i][0] - prev_p) / prev_p * 100)

        if any(r > self.max_one_roc for r in rocs[:self.roc_window]):
            return None

        roc_sma = sum(rocs) / len(rocs) if rocs else None
        if not roc_sma: return None

        # 5. Итоговый коэффициент (rate)
        rate = spr3_pct / roc_sma
        if rate < self.desired_rate:
            return None

        return {
            "side": direction,
            "price": p1,
            "spr3_pct": round(spr3_pct, 4),
            "rate": round(rate, 2),
            "row_vol_usdt": vol_usdt
        }