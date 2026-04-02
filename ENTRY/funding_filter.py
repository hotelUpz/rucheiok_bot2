# ============================================================
# FILE: ENTRY/funding_filter.py
# ROLE: Валидатор фандинга (Фоновый чекер для обхода Rate Limits)
# ============================================================
from __future__ import annotations

import asyncio
import time
from typing import Set, Any, TYPE_CHECKING
from c_log import UnifiedLogger

if TYPE_CHECKING:
    from API.PHEMEX.funding import PhemexFunding, FundingInfo

logger = UnifiedLogger("bot")

class FundingFilter:
    def __init__(self, cfg: dict[str, Any], funding_api: PhemexFunding):
        self.cfg = cfg
        self.funding_api = funding_api
        self.enable: bool = cfg.get("enable", False)
        self.interval: int = cfg.get("check_interval_sec", 60)
        self.threshold: float = cfg.get("funding_threshold_pct", 0.5) / 100.0
        self.skip_sec: int = cfg.get("skip_before_counter_sec", 1800)
        
        # Инкапсулированные стейты
        self._blocked_symbols: Set[str] = set()
        self._last_blocked_funding: Set[str] = set()
        self._is_running: bool = False

    async def run(self) -> None:
        if not self.enable: 
            return
            
        self._is_running = True
        logger.info(f"⏱ Чекер фандинга запущен. Обновление каждые: {self.interval}с, Порог: {self.threshold*100}%, Скип за: {self.skip_sec}с.")
        
        while self._is_running:
            try:
                rows: list[FundingInfo] = await self.funding_api.get_all()
                now_ms: float = time.time() * 1000
                current_blocked: Set[str] = set()
                
                for r in rows:
                    time_left_sec = (r.next_funding_time_ms - now_ms) / 1000.0
                    # Если до выплаты осталось меньше skip_sec (и время не ушло в минус)
                    if 0 < time_left_sec <= self.skip_sec:
                        if abs(r.funding_rate) >= self.threshold:
                            current_blocked.add(r.symbol)
                
                self._blocked_symbols = current_blocked
                
                # Логируем только при изменении состояния
                if current_blocked != self._last_blocked_funding:
                    if current_blocked:
                        logger.info(f"💸 Фандинг-блок! Под запретом {len(current_blocked)} монет: {', '.join(list(current_blocked)[:5])}...")
                    elif self._last_blocked_funding:
                        logger.info("💸 Фандинг-блок снят со всех монет.")
                    self._last_blocked_funding = current_blocked

            except Exception as e:
                logger.error(f"❌ Ошибка обновления фандинга: {e}")
                
            await asyncio.sleep(self.interval)
            
    def stop(self) -> None:
        self._is_running = False
        
    def is_trade_allowed(self, symbol: str) -> bool:
        """
        Семантический метод проверки. Движок спрашивает: "Можно ли торговать?"
        Если фильтр выключен - всегда True.
        Если включен - True только если монеты нет в черном списке фандинга.
        """
        if not self.enable:
            return True
        return symbol not in self._blocked_symbols