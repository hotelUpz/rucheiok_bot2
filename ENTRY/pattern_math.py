# ============================================================
# FILE: ENTRY/pattern_math.py
# ROLE: Оптимизированная математика паттернов стакана (HFT)
# ============================================================

from __future__ import annotations
from typing import Optional, Literal, Any
from CORE.models_fsm import EntryPayload
from c_log import UnifiedLogger

logger = UnifiedLogger("pattern_math")

class StakanEntryPattern:
    def __init__(self, phemex_cfg: dict[str, Any]):
        self.cfg = phemex_cfg
        
        self.depth: int = self.cfg["depth_limit"]
        self.min_vol: float = self.cfg["min_first_row_usdt_notional"]
        self.max_vol: float = self.cfg["max_first_row_usdt_notional"]

        self.max_spread_pct: float = self.cfg["max_spread_pct"]
        
        # Параметры из "разумной жизни" (старой системы)
        self.min_spr2: float = self.cfg["min_spr2_pct"]
        self.min_spr3: float = self.cfg["min_spr3_pct"]
        self.max_dist_rate: float = self.cfg["max_dist_rate"]
        
        # ROC параметры (теперь в более плоском виде)
        self.roc_window: int = self.cfg["roc_window"]
        self.roc_sma_window: int = self.cfg["sma_window"]
        self.max_one_roc: float = self.cfg["max_one_roc"]
        self.desired_rate: float = self.cfg["desired_rate"]

    def analyze(self, bids: list[tuple[float, float]], asks: list[tuple[float, float]],
    symbol: str, target_direction: Literal["LONG", "SHORT"],
    spr2_long: float, spr2_short: float) -> Optional[EntryPayload]:
        
        # Нам нужно как минимум self.depth уровней
        if len(bids) < self.depth or len(asks) < self.depth:
            return None

        return self._check_pattern(bids, asks, target_direction, symbol, spr2_long, spr2_short)

    def _check_pattern(self, bids: list[tuple[float, float]], asks: list[tuple[float, float]], direction: Literal["LONG", "SHORT"], symbol: str, spr2_long: float, spr2_short: float) -> Optional[EntryPayload]:
        """
        Проверка паттернов. Логика возвращена к 'разумному' состоянию.
        """
        bid1, bid2, bid3 = bids[0][0], bids[1][0], bids[2][0]
        ask1, ask2, ask3 = asks[0][0], asks[1][0], asks[2][0]        
        
        # 1. Спред внутри стакана (Phemex Ask1-Bid1)
        # Это общее для обоих направлений
        ob_spread_val = abs(ask1 - bid1)
        ob_spread_pct = ob_spread_val / bid1 * 100
        
        if self.max_spread_pct is not None and ob_spread_pct > self.max_spread_pct:
            # logger.debug(f"[{symbol}] [{direction}] Skip: OB Spread {ob_spread_pct:.2f}% > {self.max_spread_pct}%")
            return None

        # 2. Параметры в зависимости от направления
        if direction == "LONG":
            entry_price = ask1
            target_price = ask2
            spr2_pct = spr2_long
            spr3_pct = abs(ask3 - ask1) / ask1 * 100
            dist_denom = abs(ask2 - ask1)
            row_vol_asset = asks[0][1]
            roc_side = asks
        else: # SHORT
            entry_price = bid1
            target_price = bid2
            spr2_pct = spr2_short
            spr3_pct = abs(bid1 - bid3) / bid1 * 100
            dist_denom = abs(bid1 - bid2)
            row_vol_asset = bids[0][1]
            roc_side = bids

        # 3. Проверка объема первой строки
        vol_usdt = row_vol_asset * entry_price
        if self.min_vol is not None and vol_usdt < self.min_vol:
            # logger.debug(f"[{symbol}] [{direction}] Skip: Vol {vol_usdt:.1f} < {self.min_vol}")
            return None
        if self.max_vol is not None and vol_usdt > self.max_vol:
            # logger.debug(f"[{symbol}] [{direction}] Skip: Vol {vol_usdt:.1f} > {self.max_vol}")
            return None

        # 4. Проверка спредов уровней (spr2, spr3)
        if spr2_pct < self.min_spr2:
            # logger.debug(f"[{symbol}] [{direction}] Skip: spr2 {spr2_pct:.4f}% < {self.min_spr2}%")
            return None
        if spr3_pct < self.min_spr3:
            # logger.debug(f"[{symbol}] [{direction}] Skip: spr3 {spr3_pct:.4f}% < {self.min_spr3}%")
            return None

        # 5. Дистанция bid/ask относительно шага цены (max_dist_rate)
        if dist_denom <= 0: return None
        dist_rate = ob_spread_val / dist_denom
        if dist_rate > self.max_dist_rate:
            # logger.debug(f"[{symbol}] [{direction}] Skip: dist_rate {dist_rate:.2f} > {self.max_dist_rate}")
            return None

        # 6. Лесенка ROC (Rate of Change) - считаем С КОНЦА глубины
        rocs: list[float] = []
        # Начинаем с конца (self.depth - 1) и идем назад на roc_sma_window уровней
        for i in range(self.depth - 1, self.depth - 1 - self.roc_sma_window, -1):
            curr_p = roc_side[i][0]
            prev_p = roc_side[i-1][0]
            # ROC считается как (P_i - P_{i-1}) / P_{i-1}
            rocs.append(abs(curr_p - prev_p) / prev_p * 100)

        # Проверка одиночных скачков в начале окна роков (roc_window)
        if any(r > self.max_one_roc for r in rocs[:self.roc_window]):
            # logger.debug(f"[{symbol}] [{direction}] Skip: High ROC in window: {rocs[:self.roc_window]}")
            return None

        # ROC SMA (среднее по всему окну roc_sma_window)
        roc_sma = sum(rocs) / len(rocs) if rocs else 0.000001
        roc_sma = max(roc_sma, 0.000001)

        # 7. Итоговый коэффициент (rate)
        rate = spr3_pct / roc_sma
        if rate < self.desired_rate:
            # logger.debug(f"[{symbol}] [{direction}] Skip: Rate {rate:.2f} < {self.desired_rate}")
            return None

        return EntryPayload(
            side=direction,
            price=entry_price,
            init_ask1=ask1,
            init_bid1=bid1,
            row_vol_usdt=vol_usdt,
            row_vol_asset=row_vol_asset,
            base_target_price_100=target_price,
            mid_price=(ask1 + bid1) / 2,
            spr2_pct=spr2_pct
        )