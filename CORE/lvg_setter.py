from __future__ import annotations

import asyncio
from curl_cffi.requests import AsyncSession
from typing import List, Dict, Optional, Any, TYPE_CHECKING
from pathlib import Path

from API.PHEMEX.symbol import PhemexSymbols
from API.PHEMEX.order import PhemexPrivateClient
from utils import load_json, save_json_safe
from c_log import UnifiedLogger

logger = UnifiedLogger("lever")

"""Утилитарная сущность для настройки плеч и режима маржи на Phemex G-API."""

class GlobalLeverageSetter:
    def __init__(self, api_key: str, api_secret: str, leverage_val: Optional[float], 
                 margin_mode: int, black_list: List[str], use_cache: bool, 
                 cache_path: str | Path, delay_sec: float = 0.25):
        self.api_key = api_key
        self.api_secret = api_secret
        self.leverage_val = leverage_val
        self.margin_mode = margin_mode
        self.black_list = set(black_list)
        self.use_cache = use_cache
        self.cache_path = str(cache_path)
        self.delay_sec = delay_sec

    def _load_cache(self) -> Dict[str, Any]:
        if not self.use_cache: return {}
        return load_json(self.cache_path, default={})

    def _save_cache(self, data: Dict[str, Any]) -> None:
        save_json_safe(self.cache_path, data)

    async def _apply_setup_with_fallback(self, client: PhemexPrivateClient, sym: str, target_lev: float, max_lev: float, pos_mode: str) -> float | None:
        # Берем минимальное между желаемым и максимально допустимым для этого символа
        safe_target_lev = min(float(target_lev), float(max_lev))
        
        target_margin_mode = "Isolated"
        if str(self.margin_mode).lower() in ("1", "cross"):
            target_margin_mode = "Cross"

        try:
            # 1. "Прогрев" - принудительная установка режима позиции (Hedged/OneWay)
            try:
                await client.switch_position_mode(symbol=sym, mode=pos_mode)
            except Exception as e:
                # 10500 часто означает, что символ не найден или не поддерживается в G-API
                if "10500" in str(e) or "not found" in str(e).lower():
                    logger.debug(f"[{sym}] Skip: Symbol not found or not supported")
                    return None
                logger.debug(f"[{sym}] Switch mode note: {e}")

            # 2. Установка плеча и режима маржи (одним запросом в UTA)
            res_lev = await client.set_leverage(sym, int(safe_target_lev), margin_mode=target_margin_mode, pos_mode=pos_mode)
            if (res_res_code := res_lev.get("code")) == 0:
                logger.debug(f"[{sym}] Setup OK: {int(safe_target_lev)}x ({target_margin_mode})")
                return float(safe_target_lev)
            else:
                msg = res_lev.get('msg', '').lower()
                # Если 39108 (invalid leverage) или ошибка лимитов - пробуем жесткий фолбэк
                if "leverage" in msg or "39108" in msg or "limit" in msg:
                    # Фолбэк на 3x или меньше
                    fallback_lev = min(3, int(max_lev))
                    if fallback_lev == int(safe_target_lev): # Если уже пробовали мало
                        logger.warning(f"[{sym}] Плечо {safe_target_lev}x отклонено даже при малых значениях.")
                        return None
                        
                    logger.warning(f"[{sym}] {safe_target_lev}x отклонено ({msg}). Фолбэк на {fallback_lev}x...")
                    try:
                        res_fb = await client.set_leverage(sym, int(fallback_lev), margin_mode=target_margin_mode, pos_mode=pos_mode)
                        if res_fb.get("code") == 0:
                            return float(fallback_lev)
                    except: pass
                
                logger.error(f"[{sym}] Ошибка настройки: {res_lev.get('msg')}")
                return None

        except Exception as e:
            err = str(e).lower()
            if "has no change" in err or "same" in err: 
                return float(safe_target_lev)
            logger.error(f"[{sym}] Исключение настройки: {err[:100]}")
            return None

    async def apply(self) -> None:
        if not self.api_key or not self.api_secret: return
        
        logger.info("🔄 Загрузка спецификаций Phemex...")
        sym_api = PhemexSymbols()
        try: 
            symbols_info = await sym_api.get_all(quote="USDT", only_active=True)
            # Дедупликация символов (через словарь по имени)
            sym_map = {s.symbol: s for s in symbols_info}
            unique_symbols = list(sym_map.values())
        finally: 
            await sym_api.aclose()

        if not unique_symbols: return

        target_margin_mode = "Isolated"
        if str(self.margin_mode).lower() in ("1", "cross"):
            target_margin_mode = "Cross"

        async with AsyncSession(impersonate="chrome120") as session:
            client = PhemexPrivateClient(self.api_key, self.api_secret, session, retries=1)
            
            logger.info("📡 Получение текущих настроек аккаунта...")
            pos_res = await client.get_account_positions()
            current_states = {}
            pos_mode = "Hedged" # По умолчанию
            
            if pos_res.get("code") == 0:
                data = pos_res.get("data", {})
                account = data.get("account", {})
                pos_mode = account.get("posMode", "Hedged")
                logger.info(f"ℹ️ Текущий режим аккаунта: {pos_mode}")
                
                positions = data.get("positions", [])
                for p in positions:
                    s = p.get("symbol")
                    lev = float(p.get("leverageEr", 0)) / 10000
                    m_mode = p.get("marginMode")
                    current_states[s] = {"lev": lev, "margin": m_mode}

            current_cache = self._load_cache()
            new_cache = current_cache.copy()
            success_count, skipped_count, already_correct = 0, 0, 0
            
            for spec in unique_symbols:
                sym = spec.symbol
                if self.leverage_val is None or sym in self.black_list:
                    skipped_count += 1
                    continue

                # Сравниваем с текущим состоянием на бирже
                state = current_states.get(sym)
                if state:
                    is_lev_ok = abs(state["lev"] - self.leverage_val) < 0.1
                    is_margin_ok = state["margin"] == target_margin_mode
                    if is_lev_ok and is_margin_ok:
                        already_correct += 1
                        new_cache[sym] = self.leverage_val
                        continue

                # Если в кэше - скипаем
                if self.use_cache and sym in current_cache:
                    skipped_count += 1
                    continue
                
                res_lev = await self._apply_setup_with_fallback(client, sym, self.leverage_val, spec.max_leverage, pos_mode)
                new_cache[sym] = res_lev
                if res_lev is not None:
                    success_count += 1
                    
                await asyncio.sleep(self.delay_sec)

        self._save_cache(new_cache)
        logger.info(f"✅ Настроено: {success_count}, Уже ок: {already_correct}, Пропущено: {skipped_count}")