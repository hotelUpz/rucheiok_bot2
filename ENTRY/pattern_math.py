from __future__ import annotations

class StakanEntryPattern:
    def __init__(self, phemex_cfg: dict):
        self.cfg = phemex_cfg
        self.depth = self.cfg.get("depth", 8)
        self.min_first_row_usdt_notional = self.cfg.get("min_first_row_usdt_notional")
        self.max_first_row_usdt_notional = self.cfg.get("max_first_row_usdt_notional")

    def analyze(self, bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> dict | None:
        if not self.cfg.get("enable", True):
            return None
        if len(bids) < self.depth or len(asks) < self.depth:
            return None

        # Проверяем лонг (работаем с асками)
        long_signal = self._check_long(bids, asks)
        if long_signal:
            return long_signal

        # Проверяем шорт (работаем с бидами)
        short_signal = self._check_short(bids, asks)
        if short_signal:
            return short_signal

        return None

    def _check_long(self, bids: list, asks: list) -> dict | None:
        # print(bids[:self.depth], asks[:self.depth])
        ask1, bid1 = asks[0][0], bids[0][0]
        ask2, ask3 = asks[1][0], asks[2][0]
        bottom = self.cfg["bottom"]

        ask1_vol_asset = asks[0][1]
        ask1_vol_usdt = ask1_vol_asset * ask1
        # print(ask1_vol_usdt)

        if self.min_first_row_usdt_notional:
            if ask1_vol_usdt < self.min_first_row_usdt_notional: return None

        if self.max_first_row_usdt_notional:
            if ask1_vol_usdt > self.max_first_row_usdt_notional: return None
        
        # 1. Проценты между ближайшими асками (bottom)
        spr2_pct = (ask2 - ask1) / ask1 * 100
        if spr2_pct < bottom["min_spread_between_two_row_pct"]: return None

        spr3_pct = (ask3 - ask1) / ask1 * 100
        if spr3_pct < bottom["min_spread_between_three_row_pct"]: return None

        # 2. Дистанция bid/ask (Используем формулу из конфига: (ask1 - bid1) / (ask3 - ask1))
        dist_denom = ask3 - ask1
        if dist_denom <= 0: return None # Защита от деления на ноль и аномалий стакана
        
        dist_rate = (ask1 - bid1) / dist_denom
        if dist_rate > self.cfg["max_bid_ask_distance_rate"]: return None

        # 3. Лесенка ROC (header & body)
        header, body = self.cfg["header"], self.cfg["body"]
        roc_window, roc_sma_window = header["roc_window"], body["roc_sma_window"]

        rocs = []
        # От 8-го крайнего аска вниз (считаем разницу между соседями)
        for i in range(self.depth - 1, self.depth - 1 - roc_sma_window, -1):
            roc = (asks[i][0] - asks[i-1][0]) / asks[i-1][0] * 100
            rocs.append(roc)

        # Проверяем только верхние roc_window уровней на превышение
        if any(r > header["max_one_roc_pct"] for r in rocs[:roc_window]):
            return None

        roc_sma = sum(rocs) / len(rocs) if rocs else 0.000001
        roc_sma = max(roc_sma, 0.000001)
        # print(f"ROC SMA: {roc_sma:.6f}, SPR3%: {spr3_pct:.4f}, Rate: {spr3_pct / roc_sma:.2f}")

        # 4. Множитель (header_to_bottom_desired_rate)
        rate = spr3_pct / roc_sma
        if rate < self.cfg["header_to_bottom_desired_rate"]: return None

        return {"side": "LONG", "price": ask1, "spr3_pct": round(spr3_pct, 4), "rate": round(rate, 2), "row_vol_usdt": ask1_vol_usdt}

    def _check_short(self, bids: list, asks: list) -> dict | None:
        ask1, bid1 = asks[0][0], bids[0][0]
        bid2, bid3 = bids[1][0], bids[2][0]
        bottom = self.cfg["bottom"]

        bid1_vol_asset = bids[0][1]
        bid1_vol_usdt = bid1_vol_asset * bid1
        # print(bid1_vol_usdt)

        if self.min_first_row_usdt_notional:
            if bid1_vol_usdt < self.min_first_row_usdt_notional: return None

        if self.max_first_row_usdt_notional:
            if bid1_vol_usdt > self.max_first_row_usdt_notional: return None
        
        # 1. Проценты между ближайшими бидами (bottom) - биды идут по убыванию
        spr2_pct = (bid1 - bid2) / bid1 * 100
        if spr2_pct < bottom["min_spread_between_two_row_pct"]: return None

        spr3_pct = (bid1 - bid3) / bid1 * 100
        if spr3_pct < bottom["min_spread_between_three_row_pct"]: return None

        # 2. Дистанция bid/ask (Для шорта знаменатель: bid1 - bid3)
        dist_denom = bid1 - bid3
        if dist_denom <= 0: return None
        
        dist_rate = (ask1 - bid1) / dist_denom
        if dist_rate > self.cfg["max_bid_ask_distance_rate"]: return None

        # 3. Лесенка ROC (header & body)
        header, body = self.cfg["header"], self.cfg["body"]
        roc_window, roc_sma_window = header["roc_window"], body["roc_sma_window"]

        rocs = []
        # От 8-го крайнего бида вверх (считаем разницу между соседями)
        for i in range(self.depth - 1, self.depth - 1 - roc_sma_window, -1):
            roc = (bids[i-1][0] - bids[i][0]) / bids[i-1][0] * 100
            rocs.append(roc)

        if any(r > header["max_one_roc_pct"] for r in rocs[:roc_window]):
            return None

        roc_sma = sum(rocs) / len(rocs) if rocs else 0.000001
        roc_sma = max(roc_sma, 0.000001)

        rate = spr3_pct / roc_sma
        if rate < self.cfg["header_to_bottom_desired_rate"]: return None

        return {"side": "SHORT", "price": bid1, "spr3_pct": round(spr3_pct, 4), "rate": round(rate, 2), "row_vol_usdt": bid1_vol_usdt}