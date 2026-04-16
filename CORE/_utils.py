# ============================================================
# FILE: CORE/bot_utils.py
# ROLE: Вспомогательные сервисы (BlackList, PriceCache, Reporters, Config)
# ============================================================
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Dict, List, Tuple, TYPE_CHECKING
from ENTRY.signal_engine import SignalEngine
from c_log import UnifiedLogger

if TYPE_CHECKING:
    from CORE.orchestrator import TradingBot
    from API.PHEMEX.ticker import PhemexTickerAPI
    from API.BINANCE.ticker import BinanceTickerAPI
    from ENTRY.pattern_math import EntrySignal

logger = UnifiedLogger("core")


class BlackListManager:
    """Управление черным списком: парсинг, квотирование, сохранение."""
    def __init__(self, cfg_path: Path | str, quota_asset: str = "USDT"):
        self.cfg_path = Path(cfg_path)
        self.quota_asset = quota_asset.upper()
        self.symbols: List[str] = []

    def load_from_config(self, raw_symbols: List[str]) -> List[str]:
        new_bl = []
        for sym in raw_symbols:
            sym = sym.upper().strip()
            if not sym: continue
            
            full_sym = sym if sym.endswith(self.quota_asset) else sym + self.quota_asset
            if full_sym not in new_bl:
                new_bl.append(full_sym)
                
            # if "OG" in full_sym: # -- так и оставить закомент.
            #     new_bl.append(full_sym.replace("OG", "0G"))
            # if "0G" in full_sym:
            #     new_bl.append(full_sym.replace("0G", "OG"))
                
        self.symbols = new_bl
        return self.symbols

    def update_and_save(self, raw_symbols: List[str]) -> Tuple[bool, str]:
        clean_symbols_for_cfg = []
        for sym in raw_symbols:
            sym = sym.upper().strip()
            if sym: clean_symbols_for_cfg.append(sym)
            
        self.load_from_config(clean_symbols_for_cfg)
        
        try:
            with open(self.cfg_path, "r", encoding="utf-8") as f:
                c = json.load(f)
            c["black_list"] = clean_symbols_for_cfg
            with open(self.cfg_path, "w", encoding="utf-8") as f:
                json.dump(c, f, indent=4)
            return True, "✅ Список успешно обновлен."
        except Exception as e:
            return False, f"❌ Ошибка записи: {e}"


class PriceCacheManager:
    """Асинхронный кэш цен тикеров Binance/Phemex, обновляемый в фоне."""
    def __init__(self, binance_api: 'BinanceTickerAPI', phemex_api: 'PhemexTickerAPI', upd_sec: float = 3.0):
        self.binance_api = binance_api
        self.phemex_api = phemex_api
        self.upd_sec = upd_sec
        self.binance_prices: Dict[str, float] = {}
        self.phemex_prices: Dict[str, float] = {}
        self._is_running = False

    async def warmup(self):
        await self._fetch()

    async def _fetch(self):
        try:
            b_prices, p_prices = await asyncio.gather(
                self.binance_api.get_all_prices(),
                self.phemex_api.get_all_prices()
            )
            self.binance_prices = b_prices
            self.phemex_prices = p_prices
        except Exception as e:
            logger.debug(f"Ошибка фонового обновления цен тикеров: {e}")

    async def loop(self):
        self._is_running = True
        while self._is_running:
            await self._fetch()
            await asyncio.sleep(self.upd_sec)

    def stop(self):
        self._is_running = False

    def get_prices(self, symbol: str) -> Tuple[float, float]:
        return self.binance_prices.get(symbol, 0.0), self.phemex_prices.get(symbol, 0.0)


class ConfigManager:
    def __init__(self, cfg_path: Path | str, tb: "TradingBot"):
        self.cfg_path = cfg_path
        self.tb = tb

    def reload_config(self) -> tuple[bool, str]:
        try:
            with open(self.cfg_path, "r", encoding="utf-8") as f:
                new_cfg = json.load(f)

            self.tb.cfg = new_cfg
            self.tb.max_active_positions = self.tb.cfg.get("app", {}).get("max_active_positions", 1)
            
            self.tb.black_list = self.tb.bl_manager.load_from_config(self.tb.cfg.get("black_list", []))
            if hasattr(self.tb.state, 'black_list'):
                self.tb.state.black_list = self.tb.black_list

            # --- ПЕРЕЗАГРУЗКА ИНСТРУМЕНТОВ ВХОДА (ВКЛЮЧАЯ ФАНДИНГ V5) ---
            from ENTRY.funding_manager import FundingManager
            from ENTRY.signal_engine import SignalEngine
            
            # Тормозим старую таску фандинга, если есть
            if hasattr(self.tb, 'funding_manager'):
                self.tb.funding_manager.stop()
                
            old_task = getattr(self.tb, '_funding_task', None)
            if old_task and not old_task.done():
                old_task.cancel()

            # Создаем новый инстанс FundingManager
            self.tb.funding_manager = FundingManager(
                self.tb.cfg.get("entry", {}).get("pattern", {}), 
                self.tb.phemex_funding_api, 
                self.tb.binance_funding_api
            )
            
            # Пересоздаем движок сигналов (математика стакана обновится здесь же)
            self.tb.signal_engine = SignalEngine(self.tb.cfg["entry"], self.tb.funding_manager)

            # Перезапускаем луп фандинга
            if getattr(self.tb, '_is_running', False):
                self.tb._funding_task = asyncio.create_task(self.tb.funding_manager.run())

            # --- ПЕРЕЗАГРУЗКА СЦЕНАРИЕВ ВЫХОДА ---
            exit_cfg = self.tb.cfg.get("exit", {})
            scen_cfg = exit_cfg.get("scenarios", {})
            
            from EXIT.scenarios.base import BaseScenario
            from EXIT.scenarios.negative import NegativeScenario
            from EXIT.scenarios.breakeven import PositionTTLClose
            from EXIT.interference import Interference
            from EXIT.extrime_close import ExtrimeClose
            
            self.tb.scen_base = BaseScenario(scen_cfg.get("base", {}))
            self.tb.scen_neg = NegativeScenario(scen_cfg.get("negative", {}))
            self.tb.scen_ttl = PositionTTLClose(scen_cfg.get("breakeven_ttl_close", {}), self.tb.active_positions_locker)
            self.tb.scen_interf = Interference(exit_cfg.get("interference", {}))
            self.tb.scen_extrime = ExtrimeClose(exit_cfg.get("extrime_close", {}))

            self.tb.base_order_timeout_sec = scen_cfg.get("base", {}).get("order_timeout_sec", 0.1)   
            self.tb.breakeven_order_timeout_sec = scen_cfg.get("breakeven_ttl_close", {}).get("order_timeout_sec", 0.1)     
            self.tb.interference_order_timeout_sec = exit_cfg.get("interference", {}).get("order_timeout_sec", 0.1)        
            self.tb.extrime_order_timeout_sec = exit_cfg.get("extrime_close", {}).get("order_timeout_sec", 0.1)

            return True, "Конфигурация успешно обновлена в памяти!"
        except Exception as e:
            logger.error(f"Config reload error: {e}")
            return False, f"Ошибка загрузки в память: {e}"


class Reporters:
    @staticmethod
    def entry_signal(symbol: str, signal: EntrySignal, b_price: float, p_price: float) -> str:
        side_str = "🟢 LONG" if signal.side == "LONG" else "🔴 SHORT"
        return (
            f"<b>#{symbol}</b> | {side_str}\n"
            f"Вход: <b>{signal.price}</b>\n"
            f"Rate: {signal.rate or 0}x | Spr2: {signal.spr2_pct or 0}% | Spr3: {signal.spr3_pct or 0}%\n\n"
            f"Binance: {b_price} | Phemex: {p_price}"
        )

    @staticmethod
    def extrime_alert(symbol: str, reason: str) -> str:
        return f"🚨 <b>ОТКАЗ API</b>\n#{symbol}\nЭкстрим ордера отклоняются: {reason}"

    @staticmethod
    def exit_success(pos_key: str, semantic: str, price: float) -> str:
        return f"✅ <b>{semantic}</b>\n#{pos_key}\nЦена закрытия: <b>{price}</b>"