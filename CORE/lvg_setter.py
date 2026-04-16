from __future__ import annotations

import asyncio
import aiohttp
from typing import List, Dict, Optional, Any, TYPE_CHECKING
from pathlib import Path

from API.PHEMEX.symbol import PhemexSymbols
from API.PHEMEX.order import PhemexPrivateClient
from utils import load_json, save_json_safe
from c_log import UnifiedLogger

logger = UnifiedLogger("lever")


"""Утилитарная сущность. По идее ей не место в ядре. Но для совместимости можно оставить. Еще. Надо сохранять кеш (и читать) на папку выше корня проекта."""

class GlobalLeverageSetter:
    def __init__(self, api_key: str, api_secret: str, leverage_val: Optional[float], 
                 margin_mode: int, black_list: List[str], use_cache: bool, 
                 cache_path: str | Path, delay_sec: float = 0.3):
        self.api_key = api_key
        self.api_secret = api_secret
        self.leverage_val = leverage_val
        self.margin_mode = margin_mode
        self.black_list = set(black_list)
        self.use_cache = use_cache
        self.cache_path = str(cache_path)
        self.delay_sec = delay_sec
        self.api_pos_mode = "hedged"

    def _load_cache(self) -> Dict[str, Any]:
        if not self.use_cache: return {}
        return load_json(self.cache_path, default={})

    def _save_cache(self, data: Dict[str, Any]) -> None:
        save_json_safe(self.cache_path, data)

    async def _apply_setup_with_fallback(self, client: PhemexPrivateClient, sym: str, target_lev: float, max_lev: float) -> float | None:
        safe_target_lev = float(int(max_lev)) if target_lev > max_lev else target_lev

        try:
            # Просто ставим плечо. Для Phemex это автоматически включает Изолированную маржу.
            await client.set_leverage(sym, leverage=safe_target_lev, mode=self.api_pos_mode)
            logger.debug(f"[{sym}] Успешно: Margin SET, Lev={safe_target_lev}x")
            return float(safe_target_lev)

        except Exception as e:
            err = str(e).lower()
            if "has no change" in err or "same" in err: 
                return float(safe_target_lev)
            
            # Фолбэк на максимальное, если биржа отвергла наше плечо (лимиты)
            if "leverage" in err or "11088" in err or safe_target_lev > max_lev:
                fallback_lev = float(int(max_lev))
                logger.warning(f"[{sym}] Плечо {target_lev}x отклонено. Фолбэк на {fallback_lev}x...")
                try:
                    await client.set_leverage(sym, leverage=fallback_lev, mode=self.api_pos_mode)
                    return fallback_lev
                except Exception as fb_e:
                    if "has no change" in str(fb_e).lower() or "same" in str(fb_e).lower():
                        return fallback_lev
                    logger.error(f"[{sym}] Ошибка Fallback: {fb_e}")
                    return None
            
            logger.error(f"[{sym}] Ошибка настройки: {err[:100]}")
            return None

    async def apply(self) -> None:
        if not self.api_key or not self.api_secret: return
        
        logger.info("🔄 Загрузка спецификаций Phemex...")
        sym_api = PhemexSymbols()
        try: symbols_info = await sym_api.get_all(quote="USDT", only_active=True)
        finally: await sym_api.aclose()

        if not symbols_info: return

        current_cache = self._load_cache()
        new_cache = current_cache.copy()

        async with aiohttp.ClientSession() as session:
            client = PhemexPrivateClient(self.api_key, self.api_secret, session, retries=1)
            success_count, skipped_count = 0, 0
            
            for spec in symbols_info:
                sym = spec.symbol
                if self.leverage_val is None or sym in self.black_list or (self.use_cache and sym in current_cache):
                    skipped_count += 1
                    continue
                
                res_lev = await self._apply_setup_with_fallback(client, sym, self.leverage_val, spec.max_leverage)
                new_cache[sym] = res_lev
                if res_lev is not None:
                    success_count += 1
                await asyncio.sleep(self.delay_sec)

        self._save_cache(new_cache)
        logger.info(f"✅ Готово. Настроено: {success_count}, Пропущено: {skipped_count}")