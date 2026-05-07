# ============================================================
# FILE: CORE/sma_manager.py
# ROLE: Менеджер свечей и расчет SMA в реальном времени
# ============================================================
from __future__ import annotations
import asyncio
import time
from collections import deque
from typing import Dict, List, Optional, Any, TYPE_CHECKING
from c_log import UnifiedLogger

if TYPE_CHECKING:
    from API.PHEMEX.klines import PhemexKlinesAPI

logger = UnifiedLogger("sma")

import pickle
import os

class SMAManager:
    """Управление свечами и расчет SMA без лишних запросов к API."""
    def __init__(self, kline_api: 'PhemexKlinesAPI', cfg: Dict[str, Any], cache_dir: str = ".gemini/cache"):
        self.api = kline_api
        self.cfg = cfg
        self.enabled = cfg.get("enable", False)
        self.interval_str = cfg.get("timeframe", "1m")
        self.limit = cfg.get("candles_to_fetch", 50)
        self.window = cfg.get("window", 14)
        # Интервал полного обновления (синхронизации) - по аналогии с RSI
        self.full_update_sec = 10 * 60 # 10 минут
        
        self.cache_path = os.path.join(cache_dir, "sma_candles.pkl")
        if not os.path.exists(cache_dir): os.makedirs(cache_dir, exist_ok=True)
        
        # Перевод интервала в секунды
        self.interval_sec = self.api.res_map.get(self.interval_str, 60)
        
        # Хранилище: symbol -> deque(closes)
        self.data: Dict[str, deque[float]] = {}
        # Хранилище: symbol -> timestamp последней закрытой свечи
        self.last_ts: Dict[str, int] = {}
        
        self._is_running = False
        self._last_save_ts = 0.0

    async def warmup(self, symbols: List[str], force: bool = False):
        """Первоначальная загрузка свечей перед стартом бота."""
        if not self.enabled: return
        
        # 1. Пробуем загрузить из кэша (если не принудительно)
        if not force and self._load_from_cache():
            logger.info("📦 SMA данные успешно загружены из кэша (pickle).")
            # Проверяем, не слишком ли старый кэш
            max_ts = max(self.last_ts.values()) if self.last_ts else 0
            if time.time() - max_ts < self.interval_sec * 2:
                logger.info("✅ Кэш SMA актуален. Полная загрузка пропущена.")
                return

        logger.info(f"🕯️ SMA Sync: Загрузка {self.interval_str} свечей для {len(symbols)} монет (limit={self.limit})...")
        all_data = await self.api.get_all_klines(symbols, self.interval_str, self.limit)
        
        for sym, klines in all_data.items():
            if klines:
                closes = [k.close for k in klines]
                # Сохраняем limit свечей
                self.data[sym] = deque(closes, maxlen=self.limit + 1)
                self.last_ts[sym] = klines[-1].timestamp
        
        self._save_to_cache()
        logger.info(f"✅ SMA Sync завершен. Обновлено {len(self.data)} инструментов.")

    def update_price(self, symbol: str, price: float):
        """Обновление цены 'внутри' последней свечи или сдвиг при наступлении нового периода."""
        if not self.enabled or symbol not in self.data: return
        
        now_ts = int(time.time() // self.interval_sec * self.interval_sec)
        
        if now_ts > self.last_ts.get(symbol, 0):
            # Наступила новая свеча
            self.data[symbol].append(price)
            self.last_ts[symbol] = now_ts
            
            # Сохраняем кэш при сдвиге свечи (но не чаще раза в 10 сек)
            if time.time() - self._last_save_ts > 10:
                self._save_to_cache()
        else:
            # Обновляем Close текущей (последней) свечи
            self.data[symbol][-1] = price

    def _save_to_cache(self):
        try:
            with open(self.cache_path, "wb") as f:
                pickle.dump({"data": self.data, "last_ts": self.last_ts}, f)
            self._last_save_ts = time.time()
        except Exception as e:
            logger.debug(f"SMA Cache Save Error: {e}")

    def _load_from_cache(self) -> bool:
        if not os.path.exists(self.cache_path): return False
        try:
            with open(self.cache_path, "rb") as f:
                cache = pickle.load(f)
                self.data = cache.get("data", {})
                self.last_ts = cache.get("last_ts", {})
            return True
        except Exception as e:
            logger.debug(f"SMA Cache Load Error: {e}")
            return False

    def get_sma(self, symbol: str) -> Optional[float]:
        """Расчет SMA (Simple Moving Average)."""
        if not self.enabled or symbol not in self.data: return None
        
        closes = list(self.data[symbol])
        if len(closes) < self.window: return None
        
        # Берем последние window значений
        subset = closes[-self.window:]
        return sum(subset) / self.window

    async def background_loop(self, symbols: List[str]):
        """Фоновое обновление всех свечей раз в N минут для синхронизации с биржей."""
        self._is_running = True
        while self._is_running:
            await asyncio.sleep(self.full_update_sec)
            try:
                logger.info("🔄 SMA Background Sync: Полное обновление свечей с биржи...")
                await self.warmup(symbols, force=True)
            except Exception as e:
                logger.error(f"SMA Sync Error: {e}")

    def stop(self):
        self._is_running = False
