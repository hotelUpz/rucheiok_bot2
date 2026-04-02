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
        """Считываем словарь. По дефолту пустой."""
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

    async def apply(self) -> None:
        if not self.api_key or not self.api_secret:
            logger.error("❌ Отсутствуют API ключи для установки параметров.")
            return

        logger.info("🔄 Загрузка спецификаций с Phemex для настройки плеча/маржи...")
        sym_api = PhemexSymbols()
        try:
            symbols_info: List[SymbolInfo] = await sym_api.get_all(quote="USDT", only_active=True)
        finally:
            await sym_api.aclose()

        if not symbols_info:
            logger.warning("⚠️ Не удалось получить список символов.")
            return

        current_cache = self._load_cache()
        new_cache = current_cache.copy()

        async with aiohttp.ClientSession() as session:
            client = PhemexPrivateClient(self.api_key, self.api_secret, session, retries=1)
            
            success_count = 0
            skipped_count = 0
            
            for spec in symbols_info:
                sym = spec.symbol

                # 1. Если плечо None (null) - скипаем монету полностью
                if self.leverage_val is None:
                    skipped_count += 1
                    continue

                # 2. Скип ЧС
                if sym in self.black_list:
                    skipped_count += 1
                    continue

                # 3. Скип Кэша
                if self.use_cache and sym in current_cache:
                    skipped_count += 1
                    continue

                target_leverage = self.leverage_val
                actual_leverage = None

                try:
                    # ⚠️ УДАР ВСЛЕПУЮ: Пробуем поставить именно то плечо, которое запросили
                    await client.set_margin_mode(sym, margin_mode=self.margin_mode, leverage=target_leverage)
                    await asyncio.sleep(0.1)
                    await client.set_leverage(sym, "Merged", target_leverage, mode="hedged")
                    
                    actual_leverage = target_leverage
                    logger.debug(f"[{sym}] Успешно: MarginMode={self.margin_mode}, Lev={actual_leverage}x")

                except Exception as e:
                    err_msg = str(e).lower()
                    
                    # Если биржа просто сообщает, что параметры и так уже стоят - это успех
                    if "has no change" in err_msg or "same" in err_msg:
                        actual_leverage = target_leverage
                        logger.debug(f"[{sym}] Успешно (без изменений): MarginMode={self.margin_mode}, Lev={actual_leverage}x")
                    
                    # 🚨 ДЕТЕРМИНАЦИЯ ОТКАЗА: Ошибка плеча (превышение лимита щитка) 
                    # Код Phemex 11088 или слова out of range/exceed. 
                    # Либо мы локально понимаем, что target превысил спецификацию монеты.
                    elif ("leverage" in err_msg and ("exceed" in err_msg or "range" in err_msg or "max" in err_msg)) or "11088" in err_msg or target_leverage > spec.max_leverage:
                        
                        fallback_leverage = spec.max_leverage
                        logger.warning(f"[{sym}] Плечо {target_leverage}x отклонено (превышен лимит монеты). Ставим максимально доступное {fallback_leverage}x...")
                        
                        try:
                            # ФОЛБЭК: Ставим максимально допустимое по спецификации
                            await client.set_margin_mode(sym, margin_mode=self.margin_mode, leverage=fallback_leverage)
                            await asyncio.sleep(0.1)
                            await client.set_leverage(sym, "Merged", fallback_leverage, mode="hedged")
                            
                            actual_leverage = fallback_leverage
                            logger.debug(f"[{sym}] Успешно (Фолбэк): MarginMode={self.margin_mode}, Lev={actual_leverage}x")
                            
                        except Exception as fb_e:
                            fb_err_msg = str(fb_e).lower()
                            if "has no change" in fb_err_msg or "same" in fb_err_msg:
                                actual_leverage = fallback_leverage
                                logger.debug(f"[{sym}] Успешно (Фолбэк без изменений): MarginMode={self.margin_mode}, Lev={actual_leverage}x")
                            else:
                                logger.error(f"[{sym}] Ошибка Fallback настройки: {str(fb_e)[:100]}")
                    
                    else:
                        # Любая другая ошибка сети или API (не связанная с лимитом плеча)
                        logger.error(f"[{sym}] Ошибка настройки: {str(e)[:100]}")

                # Сохраняем в кэш результат (включая None, если всё-таки зафейлилось)
                new_cache[sym] = actual_leverage
                if actual_leverage is not None:
                    success_count += 1

                await asyncio.sleep(self.delay_sec)

        # Сохраняем в конце
        self._save_cache(new_cache)
        logger.info(f"✅ Настройка завершена. Обработано: {success_count}, Пропущено (Null/Кэш/ЧС): {skipped_count}")