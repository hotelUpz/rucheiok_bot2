# ============================================================
# FILE: CORE/funding_manager.py
# ROLE: Оркестратор API-вызовов и кэширования для фандингов
# ============================================================
from __future__ import annotations

import asyncio
import time
from typing import Dict, Any, TYPE_CHECKING
from c_log import UnifiedLogger
from ENTRY.funding_filters import FundingFilter1, FundingFilter2

if TYPE_CHECKING:
    from API.PHEMEX.funding import PhemexFunding, FundingInfo as PhmFundingInfo
    from API.BINANCE.funding import BinanceFunding, FundingInfo as BinanceFundingInfo
    from CORE._utils import SymbolListManager

logger = UnifiedLogger("entry")

def ensure_ms(ts: int | float) -> int:
    """Хелпер: гарантирует, что timestamp в миллисекундах"""
    if not ts: return 0
    # Если значение меньше 10 миллиардов (2286 год), то это секунды.
    if ts < 10_000_000_000:
        return int(ts * 1000)
    return int(ts)

class FundingManager:
    def __init__(self, cfg: dict[str, Any], phemex_api: 'PhemexFunding', binance_api: 'BinanceFunding', symbol_manager: 'SymbolListManager'):
        self.phemex_api = phemex_api
        self.binance_api = binance_api
        self.symbol_manager = symbol_manager
        
        # Лобовой доступ к секциям паттернов фандинга
        self.filter1 = FundingFilter1(cfg["funding_pattern1"])
        self.filter2 = FundingFilter2(cfg["funding_pattern2"])
        
        # Логика включения живолупы
        self.enable_phemex = self.filter1.enable or self.filter2.enable
        self.enable_binance = self.filter2.enable
        
        # Прямой доступ к интервалам
        i1 = cfg["funding_pattern1"]["check_interval_sec"]
        i2 = cfg["funding_pattern2"]["check_interval_sec"]
        self.interval = min(i1, i2) if self.enable_phemex else 60

        # O(1) Кэши объектов
        self.phemex_cache: Dict[str, PhmFundingInfo] = {}
        self.binance_cache: Dict[str, BinanceFundingInfo] = {}
        self.last_diffs: Dict[str, float] = {}

        self.is_running: bool = False

    def is_trade_allowed(self, symbol: str) -> bool:
        """Проверка монеты сразу по двум фильтрам фандинга"""
        return self.filter1.is_allowed(symbol) and self.filter2.is_allowed(symbol)

    async def run(self) -> None:
        if not self.enable_phemex:
            return
            
        self.is_running = True
        logger.info(f"🔄 Менеджер Фандингов запущен (Интервал: {self.interval}с). Binance API включен: {self.enable_binance}")
        
        while self.is_running:
            try:
                # 1. Параллельный сбор данных
                tasks = [asyncio.create_task(self.phemex_api.get_all())]
                if self.enable_binance:
                    tasks.append(asyncio.create_task(self.binance_api.get_all()))
                else:
                    async def dummy_task(): return []
                    tasks.append(asyncio.create_task(dummy_task()))

                phm_rows, bin_rows = await asyncio.gather(*tasks)

                # 2. Нормализация (приведение к ms) и кэширование в словари O(1) (только разрешенные)
                for r in phm_rows:
                    sym = r.symbol.upper()
                    if not self.symbol_manager.is_allowed(sym):
                        continue
                        
                    object.__setattr__(r, 'next_funding_time_ms', ensure_ms(r.next_funding_time_ms))
                    self.phemex_cache[sym] = r

                for r in bin_rows:
                    sym = r.symbol.upper()
                    if not self.symbol_manager.is_allowed(sym):
                        continue
                        
                    object.__setattr__(r, 'next_funding_time_ms', ensure_ms(r.next_funding_time_ms))
                    self.binance_cache[sym] = r

                now_ms = time.time() * 1000

                # 3. Расчет общих переменных (диффы)
                self.last_diffs.clear()
                if self.enable_binance:
                    for sym, p_info in self.phemex_cache.items():
                        b_info = self.binance_cache.get(sym)
                        if b_info:
                            self.last_diffs[sym] = abs(p_info.funding_rate - b_info.funding_rate)

                # logger.debug(f"🔄 Кэш Фандингов обновлен. Phemex: {len(self.phemex_cache)} монет, Binance: {len(self.binance_cache)} монет.")

                # 4. Передаем в математические блоки
                self.filter1.process(self.phemex_cache, now_ms)
                self.filter2.process(self.phemex_cache, self.binance_cache, self.last_diffs, now_ms)

            except Exception as e:
                logger.error(f"❌ Ошибка в Менеджере Фандингов: {e}")
                
            await asyncio.sleep(self.interval)
            
    def stop(self) -> None:
        self.is_running = False