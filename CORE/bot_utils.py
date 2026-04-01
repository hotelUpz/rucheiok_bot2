# ============================================================
# FILE: CORE/bot_utils.py
# ROLE: Вспомогательные сервисы (BlackList, PriceCache)
# ============================================================

import asyncio
import json
from pathlib import Path
from typing import Dict, List, Tuple, Any

from c_log import UnifiedLogger

logger = UnifiedLogger("utils")

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
                
            # Вариации OG/0G
            if "OG" in full_sym:
                new_bl.append(full_sym.replace("OG", "0G"))
            if "0G" in full_sym:
                new_bl.append(full_sym.replace("0G", "OG"))
                
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
    def __init__(self, binance_api, phemex_api, upd_sec: float = 3.0):
        self.binance_api = binance_api
        self.phemex_api = phemex_api
        self.upd_sec = upd_sec
        self.binance_prices: Dict[str, float] = {}
        self.phemex_prices: Dict[str, float] = {}
        self._is_running = False

    async def warmup(self):
        """Единоразовый прогрев до старта основного цикла"""
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
        """Возвращает (binance_price, phemex_price)"""
        return self.binance_prices.get(symbol, 0.0), self.phemex_prices.get(symbol, 0.0)