from __future__ import annotations

import asyncio
import aiohttp
from typing import List, Dict, Optional, Any, TYPE_CHECKING
from pathlib import Path

from API.PHEMEX.symbol import PhemexSymbols, SymbolInfo
from API.PHEMEX.order import PhemexPrivateClient
from utils import load_json, save_json_safe
from c_log import UnifiedLogger

if TYPE_CHECKING:
    from API.PHEMEX.symbol import SymbolInfo

logger = UnifiedLogger("setup")

class GlobalLeverageSetter:
    def __init__(
        self, 
        api_key: str, 
        api_secret: str, 
        leverage_val: Optional[float],
        margin_mode: int,
        black_list: List[str],
        use_cache: bool,
        cache_path: str | Path,
        delay_sec: float = 0.3
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.leverage_val = leverage_val
        self.margin_mode = margin_mode
        self.black_list = set(black_list)
        self.use_cache = use_cache
        self.cache_path = str(cache_path)
        self.delay_sec = delay_sec

    def _load_cache(self) -> Dict[str, Any]:
        if not self.use_cache: 
            return {}
        return load_json(self.cache_path, default={})

    def _save_cache(self, data: Dict[str, Any]) -> None:
        # Пишем ВСЕГДА, чтобы создать кэш, даже если use_cache = False
        save_json_safe(self.cache_path, data)

    async def _apply_setup_with_fallback(self, client: PhemexPrivateClient, sym: str, target_lev: float, max_lev: float) -> float | None:
        try:
            if self.margin_mode == 2:
                # Для CROSS маржи: только эндпоинт switch-isolated. Плечо не ставим. 
                # Иначе биржа перекинет позицию в ISOLATED.
                await client.set_margin_mode(sym, margin_mode=2, leverage=0, mode="hedged")
                logger.debug(f"[{sym}] Успешно: CROSS Margin")
                return target_lev
            else:
                # Для ISOLATED: просто ставим плечо. Биржа сама включит Isolated.
                await client.set_leverage(sym, "Merged", target_lev, mode="hedged")
                logger.debug(f"[{sym}] Успешно: ISOLATED Margin, Lev={target_lev}x")
                return target_lev

        except Exception as e:
            err_msg = str(e).lower()
            
            if "has no change" in err_msg or "same" in err_msg:
                return target_lev
            
            is_out_of_range = (
                "leverage" in err_msg and ("exceed" in err_msg or "range" in err_msg or "max" in err_msg or "20003" in err_msg)
            ) or "11088" in err_msg or target_lev > max_lev

            # В Кросс-марже плечо не используется, фолбэк применим только для Isolated
            if is_out_of_range and self.margin_mode != 2:
                fallback_lev = max(1.0, float(int(max_lev)))
                logger.warning(f"[{sym}] Плечо {target_lev}x отклонено (лимит). Фолбэк на {fallback_lev}x...")
                try:
                    await client.set_leverage(sym, "Merged", fallback_lev, mode="hedged")
                    return fallback_lev
                except Exception as fb_e:
                    if "has no change" in str(fb_e).lower() or "same" in str(fb_e).lower():
                        return fallback_lev
                    logger.error(f"[{sym}] Ошибка Fallback: {fb_e}")
                    return None
            
            logger.error(f"[{sym}] Ошибка настройки: {err_msg[:100]}")
            return None

    async def apply(self) -> None:
        if not self.api_key or not self.api_secret:
            logger.error("❌ Отсутствуют API ключи.")
            return

        logger.info("🔄 Загрузка спецификаций Phemex...")
        sym_api = PhemexSymbols()
        try:
            symbols_info: List[SymbolInfo] = await sym_api.get_all(quote="USDT", only_active=True)
        finally:
            await sym_api.aclose()

        if not symbols_info:
            return

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