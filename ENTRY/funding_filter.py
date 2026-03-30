# ============================================================
# FILE: ENTRY/funding_filter.py
# ROLE: Фоновый чекер фандинга (изолирован в модуле ENTRY)
# ============================================================

import asyncio
import time
from c_log import UnifiedLogger

logger = UnifiedLogger("entry")

class FundingFilter:
    def __init__(self, cfg: dict, funding_api):
        self.cfg = cfg
        self.funding_api = funding_api
        self.enable = cfg.get("enable", False)
        self.interval = cfg.get("check_interval_sec", 60)
        self.threshold = cfg.get("funding_threshold_pct", 0.5) / 100.0
        self.skip_sec = cfg.get("skip_before_counter_sec", 1800)
        
        self.blocked_symbols = set()
        self._last_blocked_funding = set()
        self._is_running = False

    async def run(self):
        if not self.enable: return
        self._is_running = True
        logger.info(f"⏱ Чекер фандинга запущен. Интервал: {self.interval}с, Порог: {self.threshold*100}%, Скип за: {self.skip_sec}с.")
        
        while self._is_running:
            try:
                rows = await self.funding_api.get_all()
                now_ms = time.time() * 1000
                blocked = set()
                
                for r in rows:
                    time_left_sec = (r.next_funding_time_ms - now_ms) / 1000.0
                    if 0 < time_left_sec <= self.skip_sec:
                        if abs(r.funding_rate) >= self.threshold:
                            blocked.add(r.symbol)
                
                self.blocked_symbols = blocked
                
                if blocked != self._last_blocked_funding:
                    if blocked:
                        logger.info(f"💸 Фандинг-блок! Под запретом {len(blocked)} монет: {', '.join(list(blocked)[:5])}...")
                    elif self._last_blocked_funding:
                        logger.info("💸 Фандинг-блок снят со всех монет.")
                    self._last_blocked_funding = blocked

            except Exception as e:
                logger.error(f"❌ Ошибка обновления фандинга: {e}")
                
            await asyncio.sleep(self.interval)
            
    def stop(self):
        self._is_running = False
        
    def is_blocked(self, symbol: str) -> bool:
        return self.enable and symbol in self.blocked_symbols