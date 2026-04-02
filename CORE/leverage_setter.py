# ============================================================
# FILE: CORE/leverage_setter.py
# ROLE: Глобальная пред-установка плеча и маржи (с JSON кешем)
# ============================================================
from __future__ import annotations

import json
import asyncio
import aiohttp
from typing import List, Dict, Optional, Any, TYPE_CHECKING
from pathlib import Path

from API.PHEMEX.symbol import PhemexSymbols, SymbolInfo
from API.PHEMEX.order import PhemexPrivateClient
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
        self.cache_path = Path(cache_path)
        self.delay_sec = delay_sec
        
        # Хардкодим биржевой режим аккаунта, так как бот использует posSide
        self.api_pos_mode = "hedged"

    def _load_cache(self) -> Dict[str, Any]:
        """Считываем словарь кэша плечей. Если use_cache=False, возвращаем пустой словарь для пересборки."""
        if not self.use_cache or not self.cache_path.exists():
            return {}
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Ошибка чтения {self.cache_path.name}: {e}")
            return {}

    def _save_cache(self, data: Dict[str, Any]) -> None:
        """Сохраняем финальный словарь на диск ВСЕГДА (создаем/обновляем слепок)."""
        try:
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            logger.error(f"Ошибка записи {self.cache_path.name}: {e}")

    async def _apply_setup_with_fallback(self, client: PhemexPrivateClient, sym: str, target_lev: float, max_lev: float) -> float | None:
        safe_target_lev = target_lev
        
        # Предварительная защита: если мы изначально знаем, что просим больше лимита
        if target_lev > max_lev:
            logger.warning(f"[{sym}] Запрошено {target_lev}x, но лимит {max_lev}x. Сразу применяем фолбэк...")
            safe_target_lev = max(1, int(max_lev))  # Принудительно целое число

        try:
            if self.margin_mode == 2:
                # Включаем CROSS маржу
                await client.set_margin_mode(sym, 2, safe_target_lev, mode=self.api_pos_mode)
                logger.debug(f"[{sym}] Успешно: Lev={safe_target_lev}x (CROSS Margin)")
            else:
                # Включаем ISOLATED маржу (Phemex сам включает Isolated при смене плеча)
                await client.set_leverage(sym, "Merged", safe_target_lev, mode=self.api_pos_mode)
                logger.debug(f"[{sym}] Успешно: Lev={safe_target_lev}x (ISOLATED Margin)")
            return safe_target_lev

        except Exception as e:
            err_msg = str(e).lower()
            if "has no change" in err_msg or "same" in err_msg:
                return safe_target_lev
            
            # Если мы уже на фолбэке, и он упал - выходим
            if safe_target_lev == max(1, int(max_lev)):
                logger.error(f"[{sym}] Ошибка настройки (лимит исчерпан): {err_msg[:100]}")
                return None

            # Пробуем фолбэк, если ошибка связана с плечом
            is_out_of_range = "leverage" in err_msg and ("exceed" in err_msg or "range" in err_msg or "max" in err_msg or "20003" in err_msg)
            
            if is_out_of_range or "11088" in err_msg:
                fallback_lev = max(1, int(max_lev))
                logger.warning(f"[{sym}] Плечо {target_lev}x отклонено. Фолбэк на {fallback_lev}x...")
                try:
                    if self.margin_mode == 2:
                        await client.set_margin_mode(sym, 2, fallback_lev, mode=self.api_pos_mode)
                    else:
                        await client.set_leverage(sym, "Merged", fallback_lev, mode=self.api_pos_mode)
                    return fallback_lev
                except Exception as fb_e:
                    if "has no change" in str(fb_e).lower() or "same" in str(fb_e).lower():
                        return fallback_lev
                    logger.error(f"[{sym}] Критическая ошибка Fallback: {fb_e}")
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
            logger.warning("⚠️ Список символов пуст.")
            return

        current_cache = self._load_cache()
        new_cache = current_cache.copy()

        async with aiohttp.ClientSession() as session:
            client = PhemexPrivateClient(self.api_key, self.api_secret, session, retries=1)
            success_count = 0
            skipped_count = 0
            
            for spec in symbols_info:
                sym = spec.symbol

                if self.leverage_val is None:
                    skipped_count += 1
                    continue

                if sym in self.black_list:
                    skipped_count += 1
                    continue

                if self.use_cache and sym in current_cache:
                    skipped_count += 1
                    continue

                res_lev = await self._apply_setup_with_fallback(client, sym, self.leverage_val, spec.max_leverage)
                
                new_cache[sym] = res_lev
                if res_lev is not None:
                    success_count += 1

                await asyncio.sleep(self.delay_sec)

        self._save_cache(new_cache)
        logger.info(f"✅ Готово. Настроено: {success_count}, Пропущено: {skipped_count}")