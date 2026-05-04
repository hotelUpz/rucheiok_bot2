# ============================================================
# FILE: CORE/restorator.py
# ROLE: Data restotator for trading state
# ============================================================
from __future__ import annotations

import asyncio
from typing import Dict, Set, List, Any
from utils import load_json, save_json_safe
from CORE.models_fsm import ActivePosition
from c_log import UnifiedLogger

logger = UnifiedLogger("core")


class BotState:
    """Вызываем в ключевые моменты движения торговой итерации."""
    def __init__(self, black_list: List, white_list: List = None, filepath: str = "bot_state.json"):
        self.filepath = filepath
        self.active_positions: Dict[str, ActivePosition] = {}
        self.consecutive_fails: Dict[str, int] = {}
        self.quarantine_until: Dict[str, float] = {}
        
        self.pending_entry_orders: Dict[str, str] = {}
        self.pending_interference_orders: Dict[str, str] = {}
        self.in_flight_orders: Set[str] = set()
        self.leverage_configured: Set[str] = set()
        self._lock = asyncio.Lock()
        self.black_list = black_list
        self.white_list = white_list or []
        self.analytics: dict = {}

    def _sync_save(self, state_dict: dict):
        save_json_safe(self.filepath, state_dict)

    async def save(self):
        async with self._lock:
            current_positions = list(self.active_positions.items())
            state_dict = {
                "positions": {
                    pos_key: pos.to_dict() 
                    for pos_key, pos in current_positions 
                    if self._is_allowed(pos.symbol)
                },
                "fails": dict(self.consecutive_fails),
                "quarantine": {x: str(y) for x, y in dict(self.quarantine_until).items() if x and y},
                "analytics": getattr(self, 'analytics', {})  
            }
            await asyncio.to_thread(self._sync_save, state_dict)

    def load(self):
        data = load_json(self.filepath, default={})
        if not data: return
        
        for k, v in data.get("fails", {}).items():
            if k not in self.consecutive_fails:
                self.consecutive_fails[k] = v
                
        for k, v in data.get("quarantine", {}).items():
            if k not in self.quarantine_until:
                self.quarantine_until[k] = v
        
        self.analytics.update(data.get("analytics", {}))
        
        saved_positions = data.get("positions", {})
        self.active_positions.clear()
        for pos_key, pos_data in saved_positions.items():
            pos = ActivePosition.from_dict(pos_data)
            # УДАЛЕНО: Фильтрация по спискам в лоаде стейта ЗАПРЕЩЕНА (чтобы не бросать сирот)
            self.active_positions[pos_key] = pos

    async def sync_with_exchange(self, private_client: Any):
        """Синхронизирует стейт с реальными позициями на бирже."""
        try:
            resp = await private_client.get_active_positions()
            data_block = resp.get("data", {})
            data = data_block.get("positions", []) if isinstance(data_block, dict) else data_block
            if not isinstance(data, list): data = []

            exchange_positions = {}
            for item in data:
                size = float(item.get("sizeRq", item.get("size", 0)))
                if size != 0:
                    symbol = item.get("symbol")
                    pos_side_raw = item.get("posSide", item.get("side", "")).lower()
                    pos_side = "LONG" if pos_side_raw in ("long", "buy") else "SHORT"
                    pos_key = f"{symbol}_{pos_side}"
                    exchange_positions[pos_key] = {"size": abs(size), "side": pos_side, "symbol": symbol}

            # 1. Помечаем на удаление те, которых нет на бирже
            keys_to_remove = [k for k in list(self.active_positions.keys()) if k not in exchange_positions]
            for k in keys_to_remove:
                pos = self.active_positions.get(k)
                if pos and pos.in_position:
                    pos.is_closed_by_exchange = True # Отдаем на растерзание Game Loop
                else:
                    self.active_positions.pop(k, None) 

            # 2. Обновляем существующие
            for pos_key, ex_data in exchange_positions.items():
                if pos_key in self.active_positions:
                    self.active_positions[pos_key].current_qty = ex_data["size"]
                    self.active_positions[pos_key].in_position = True
                    self.active_positions[pos_key].in_pending = False
                else:
                    # Если позиция есть на бирже, но её нет в стейте — логируем (усыновление не делаем по ТЗ)
                    logger.warning(f"⚠️ На бирже есть позиция {pos_key}, которой нет в JSON!")

            await self.save()
            logger.info(f"✅ Стейт синхронизирован. В памяти {len(self.active_positions)} активных позиций.")
        except Exception as e:
            logger.error(f"❌ Ошибка sync_with_exchange: {e}")

    def _is_allowed(self, symbol: str) -> bool:
        if symbol in self.black_list:
            return False
        if self.white_list:
            return symbol in self.white_list
        return True