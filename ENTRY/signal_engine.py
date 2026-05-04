# ============================================================
# FILE: ENTRY/engine.py
# ROLE: Оркестратор логики входа (Pattern math, Binance filter, TTLs, Funding)
# ============================================================
from __future__ import annotations

import time
from typing import Dict, Any, Optional, TYPE_CHECKING, Literal, Tuple

from ENTRY.pattern_math import StakanEntryPattern
from CORE.models_fsm import EntryPayload
from c_log import UnifiedLogger

if TYPE_CHECKING:
    from API.PHEMEX.stakan import DepthTop
    from API.BINANCE.stakan import DepthTop as BinanceDepthTop
    from ENTRY.funding_manager import FundingManager
    from CORE.rsi_manager import RSIManager
    from API.DEX.dexscreener import DexscreenerAPI


logger = UnifiedLogger("signal_engine")

class SignalEngine:
    def __init__(self, cfg: Dict[str, Any], funding_manager: 'FundingManager', rsi_manager: Optional['RSIManager'] = None, dex_api: Optional['DexscreenerAPI'] = None):
        self.cfg = cfg
        self.funding_manager = funding_manager  
        self.rsi_manager = rsi_manager
        self.dex_api = dex_api
        
        self.binance_trigger_cfg = cfg["pattern"]["binance_trigger"]
        self.ob_cfg = cfg["pattern"]["orderbook_filter"]
        self.fair_cfg = cfg["pattern"].get("fair_price_filter", {"enable": False})
        self.rsi_cfg = cfg["pattern"].get("rsi_filter", {"enable": False})
        self.dex_cfg = cfg["pattern"].get("dex_filter", {"enable": False})
        
        self.pattern_math = StakanEntryPattern(self.ob_cfg)
        
        # binance_trigger
        self.spread_enabled = self.binance_trigger_cfg["enable"]
        self.spread_mode = self.binance_trigger_cfg["spread_mode"].upper()
        self.spread_to_entry_pct = abs(self.binance_trigger_cfg["spread_to_entry_pct"])
        self.min_price_spread_rate = abs(self.binance_trigger_cfg["min_price_spread_rate"])
        self.spread_ttl = self.binance_trigger_cfg["ttl_sec"]
        if self.spread_enabled:
            logger.info(f"📡 SignalEngine: Spread Mode = {self.spread_mode}")
        
        # orderbook_filter
        self.ob_ttl = self.ob_cfg["pattern_ttl_sec"]
        self.ob_depth_limit = self.ob_cfg["depth_limit"]
        
        # fair_price_filter
        self.fair_enabled = self.fair_cfg["enable"]
        self.min_fair_spread_pct = self.fair_cfg["min_fair_spread_pct"]

        # rsi_filter
        self.rsi_enabled = self.rsi_cfg["enable"]
        self.rsi_overbought = self.rsi_cfg["overbought"]
        self.rsi_oversold = self.rsi_cfg["oversold"]

        # dex_filter
        self.dex_enabled = self.dex_cfg["enable"]
        self.min_dex_spread_pct = abs(self.dex_cfg["min_dex_spread_pct"])

        self.price_mode = cfg["price_mode"].upper()

        self.allowed_directions: list[str] = []
        self._setup_directions(cfg)
        
        self._pattern_first_seen: Dict[str, float] = {}
        self._spread_first_seen: Dict[str, float] = {}
        
        self._max_spreads: Dict[str, float] = {}
        self._last_spread_log_ts = time.time()

    def calculate_spread(self, direction: Literal["LONG", "SHORT"], p_bid1: float, p_ask1: float, b_bid1: float, b_ask1: float, b_price: float, p_price: float) -> float:
        """Расчет спреда согласно выбранному режиму и направлению."""
        if self.spread_mode == "BOOK":
            if direction == "LONG":
                return (b_ask1 - p_ask1) / p_ask1 * 100 if p_ask1 > 0 else 0
            else:
                # SHORT: Спред отрицательный, если Phemex Bid > Binance Bid
                return (b_bid1 - p_bid1) / p_bid1 * 100 if p_bid1 > 0 else 0
        elif self.spread_mode == "MID":
            b_mid = (b_bid1 + b_ask1) / 2
            p_mid = (p_bid1 + p_ask1) / 2
            return (b_mid - p_mid) / p_mid * 100 if p_mid > 0 else 0
        else: # TICKER
            return (b_price - p_price) / p_price * 100 if p_price > 0 else 0

    async def analyze(self, depth: DepthTop, b_price: float, p_price: float, b_depth: Optional[BinanceDepthTop], b_fair: float = 0.0, p_fair: float = 0.0) -> Optional[EntryPayload]:
        """
        Иерархия фильтров:
        1. Funding (Global)
        2. Orderbook Pattern (PatternMath) -> Heart of strategy
        3. Binance Trigger (Spread)
        4. Fair Price Filter
        5. RSI Filter
        6. DEX Filter (Dexscreener)
        7. TTL Checks (Parallel maturity)
        """
        symbol: str = depth.symbol
        now: float = time.time()

        # 1. Проверка фандинга (глобальный фильтр)
        if not self.funding_manager.is_trade_allowed(symbol):
            return None

        # Пре-расчет спредов уровней
        p_bid1, p_ask1 = depth.bids[0][0], depth.asks[0][0]
        p_bid2, p_ask2 = depth.bids[1][0], depth.asks[1][0]
        spr2_long = abs(p_ask2 - p_ask1) / p_ask1 * 100 if p_ask1 > 0 else 0
        spr2_short = abs(p_bid2 - p_bid1) / p_bid1 * 100 if p_bid1 > 0 else 0

        # 2. Фильтр: orderbook_filter (Pattern Math) - СЕРДЦЕ СТРАТЕГИИ (ВСЕГДА ВКЛЮЧЕН)
        bids_sliced = depth.bids[:self.ob_depth_limit]
        asks_sliced = depth.asks[:self.ob_depth_limit]
        
        # Проверяем LONG, затем SHORT
        signal: Optional[EntryPayload] = self.pattern_math.analyze(bids_sliced, asks_sliced, symbol, "LONG", spr2_long, spr2_short)
        if not signal:
            signal = self.pattern_math.analyze(bids_sliced, asks_sliced, symbol, "SHORT", spr2_long, spr2_short)
            
        if not signal:
            self._pattern_first_seen.pop(f"{symbol}_LONG", None)
            self._pattern_first_seen.pop(f"{symbol}_SHORT", None)
            return None
            
        direction = signal.side
        pos_key = f"{symbol}_{direction}"

        # Проверка разрешенных направлений
        if direction not in self.allowed_directions:
            return None

        # 3. Триггер: binance_trigger
        spread_pct = 0.0
        if self.spread_enabled:
            if b_depth is None or not b_depth.bids or not b_depth.asks:
                return None # Нет данных с бинанса - нет входа если он включен
                
            b_bid1, b_ask1 = b_depth.bids[0][0], b_depth.asks[0][0]
            spread_pct = self.calculate_spread(direction, p_bid1, p_ask1, b_bid1, b_ask1, b_price, p_price)
                
            abs_spread = abs(spread_pct)
            if abs_spread > self._max_spreads.get(symbol, 0.0):
                self._max_spreads[symbol] = abs_spread
            
            if now - self._last_spread_log_ts >= 60:
                self._flush_spread_logs()
                self._last_spread_log_ts = now

            # Вычисляем динамический порог
            min_spread = (spr2_long if direction == "LONG" else spr2_short) * self.min_price_spread_rate
            min_spread = max(min_spread, self.spread_to_entry_pct)

            passed_binance = (spread_pct >= min_spread) if direction == "LONG" else (spread_pct <= -min_spread)
            if not passed_binance:
                self._spread_first_seen.pop(pos_key, None)
                return None

        # 4. Фильтр: Справедливая цена Phemex
        if self.fair_enabled and p_fair > 0:
            fair_diff_pct = (p_fair - p_price) / p_price * 100
            if direction == "LONG":
                if fair_diff_pct < self.min_fair_spread_pct:
                    return None
            else: # SHORT
                if fair_diff_pct > -self.min_fair_spread_pct:
                    return None
            
        # 5. Фильтр: RSI
        if self.rsi_enabled and self.rsi_manager:
            rsi = self.rsi_manager.get_rsi(symbol)
            if rsi is not None:
                if direction == "LONG" and rsi >= self.rsi_overbought:
                    return None
                if direction == "SHORT" and rsi <= self.rsi_oversold:
                    return None

        # 6. Фильтр: DEX Price (Dexscreener)
        dex_price = 0.0
        dex_spread_pct = 0.0
        if self.dex_enabled and self.dex_api:
            # Используем кэшированное или быстрое получение цены
            pair_data = await self.dex_api.get_price_by_symbol(symbol, ref_price=p_price)
            if pair_data:
                dex_price = float(pair_data.get("priceUsd", 0))
                if dex_price > 0:
                    dex_spread_pct = (dex_price - p_price) / p_price * 100
                    passed_dex = (dex_spread_pct >= self.min_dex_spread_pct) if direction == "LONG" else (dex_spread_pct <= -self.min_dex_spread_pct)
                    if not passed_dex:
                        return None
            else:
                # Если DEX обязателен (напр. min_dex_spread_pct > 0) и данных нет - можно скипать, 
                # но здесь оставим опциональным пропуском
                pass


        # 7. ПРОВЕРКА ВЫДЕРЖКИ (TTLs) - ПАРАЛЛЕЛЬНО В КОНЦЕ
        # Если мы дошли сюда, значит все фильтры прошли. Теперь проверяем время удержания.
        
        # TTL для спреда
        spread_ready = True
        if self.spread_ttl > 0:
            first_seen_s = self._spread_first_seen.setdefault(pos_key, now)
            if now - first_seen_s < self.spread_ttl:
                spread_ready = False
        
        # TTL для паттерна
        pattern_ready = True
        if self.ob_ttl > 0:
            first_seen_p = self._pattern_first_seen.setdefault(pos_key, now)
            if now - first_seen_p < self.ob_ttl:
                pattern_ready = False
                
        if not spread_ready or not pattern_ready:
            return None

        # Сброс TTL после успешного прохождения
        self._spread_first_seen.pop(pos_key, None)
        self._pattern_first_seen.pop(pos_key, None)
        
        # Финализация сигнала
        signal.b_price = b_price
        signal.p_price = p_price
        signal.spread = spread_pct

        # Логирование
        rsi_val = self.rsi_manager.get_rsi(symbol) if self.rsi_manager else None
        rsi_str = f"{rsi_val:.1f}" if rsi_val is not None else "N/A"
        fair_spread = (p_fair - p_price) / p_price * 100 if p_fair > 0 else 0
        
        logger.info(
            f"🚀 [SIGNAL] {symbol} ({direction}) | "
            f"Price: {signal.price} | "
            f"BIN-PHM: {spread_pct:.2f}% | "
            f"DEX-PHM: {dex_spread_pct:.2f}% | "
            f"FAIR-PHM: {fair_spread:.2f}% | "
            f"RSI: {rsi_str} | "
            f"DEX: {dex_price}$ | BIN: {b_price}$ | PHM: {p_price}$"
        )
        if self.price_mode == "MID":
            signal.price = signal.mid_price

        return signal

    def _setup_directions(self, cfg: Dict[str, Any]) -> None:
        """Парсинг и валидация разрешенных направлений торговли."""
        raw_dirs = cfg.get("allowed_directions", ["LONG", "SHORT"])
        if not isinstance(raw_dirs, list):
            raw_dirs = [raw_dirs]
            
        self.allowed_directions = [str(x).strip().upper() for x in raw_dirs if x]
        
        # Гарантируем, что есть хотя бы одно валидное направление
        if not any(d in ("LONG", "SHORT") for d in self.allowed_directions):
            logger.warning(f"⚠️ Критическая ошибка в конфиге allowed_directions: {self.allowed_directions}. Сброс на LONG+SHORT.")
            self.allowed_directions = ["LONG", "SHORT"]

    def _flush_spread_logs(self) -> None:
        if not self._max_spreads:
            return
        
        sorted_items = sorted(self._max_spreads.items(), key=lambda x: x[1], reverse=True)
        top_10 = sorted_items[:10]
        
        msg = "📊 [MAX SPREADS 1m]: " + " | ".join([f"{s}: {v:.3f}%" for s, v in top_10])
        logger.info(msg)
        self._max_spreads.clear()
