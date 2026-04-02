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

    def _load_cache(self) -> Dict[str, Any]:
        """Считываем словарь кэша плечей."""
        if not self.use_cache or not self.cache_path.exists():
            return {}
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Ошибка чтения {self.cache_path.name}: {e}")
            return {}

    def _save_cache(self, data: Dict[str, Any]) -> None:
        """Сохраняем финальный словарь на диск."""
        if not self.use_cache:
            return
        try:
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            logger.error(f"Ошибка записи {self.cache_path.name}: {e}")

    async def _apply_setup_with_fallback(self, client: PhemexPrivateClient, sym: str, target_lev: float, max_lev: float) -> float | None:
        """
        Вспомогательный метод: пробует установить плечо. 
        При превышении лимита монеты — ставит максимально возможное.
        """
        try:
            # Просто устанавливаем плечо. Phemex автоматически включит Isolated режим.
            await client.set_leverage(sym, "Merged", target_lev, mode="hedged")
            logger.debug(f"[{sym}] Успешно: Lev={target_lev}x (Isolated)")
            return target_lev

        except Exception as e:
            err_msg = str(e).lower()
            
            # Кейс А: Параметры уже стоят
            if "has no change" in err_msg or "same" in err_msg:
                return target_lev
            
            # Кейс Б: Детерминация превышения плеча (Ошибка 11088 или текст)
            is_out_of_range = (
                "leverage" in err_msg and ("exceed" in err_msg or "range" in err_msg or "max" in err_msg)
            ) or "11088" in err_msg or target_lev > max_lev

            if is_out_of_range:
                logger.warning(f"[{sym}] Плечо {target_lev}x отклонено (лимит). Фолбэк на {max_lev}x...")
                try:
                    await client.set_leverage(sym, "Merged", max_lev, mode="hedged")
                    return max_lev
                except Exception as fb_e:
                    if "has no change" in str(fb_e).lower() or "same" in str(fb_e).lower():
                        return max_lev
                    logger.error(f"[{sym}] Критическая ошибка Fallback: {fb_e}")
                    return None
            
            # Кейс В: Прочие ошибки (Signature, Network и т.д.)
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

                # Условие 1: leverage_val == None -> Полный скип установки
                if self.leverage_val is None:
                    skipped_count += 1
                    continue

                # Условие 2: Черный список -> Скип
                if sym in self.black_list:
                    skipped_count += 1
                    continue

                # Условие 3: Кэш (если включен) -> Скип
                if self.use_cache and sym in current_cache:
                    skipped_count += 1
                    continue

                # Выполняем установку с логикой фолбэка
                res_lev = await self._apply_setup_with_fallback(client, sym, self.leverage_val, spec.max_leverage)
                
                # Сохраняем результат (даже если None - для фиксации фейла в текущей сессии)
                new_cache[sym] = res_lev
                if res_lev is not None:
                    success_count += 1

                await asyncio.sleep(self.delay_sec)

        self._save_cache(new_cache)
        logger.info(f"✅ Готово. Настроено: {success_count}, Пропущено: {skipped_count}")