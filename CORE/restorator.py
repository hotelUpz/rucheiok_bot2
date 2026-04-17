# ============================================================
# FILE: CORE/restorator.py
# ROLE: Data restotator for trading state
# ============================================================
from __future__ import annotations

import asyncio
from typing import Dict, Set, List
from utils import load_json, save_json_safe
from CORE.models_fsm import ActivePosition
from c_log import UnifiedLogger

logger = UnifiedLogger("core")


class BotState:
    """Вызываем в ключевые моменты движения торговой итерации."""
    def __init__(self, black_list: List, filepath: str = "bot_state.json"):
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
                    if pos.symbol not in self.black_list
                },
                "fails": dict(self.consecutive_fails),
                "quarantine": {x: str(y) for x, y in dict(self.quarantine_until).items() if x and y},
                "analytics": getattr(self, 'analytics', {})  # <--- ВОТ ЭТА СТРОКА ДОЛЖНА БЫТЬ ЗДЕСЬ
            }
            await asyncio.to_thread(self._sync_save, state_dict)

    def load(self):
        data = load_json(self.filepath, default={})
        if not data: return
        
        # ИСПРАВЛЕНИЕ: Мягкое обновление. Мы не делаем .clear(), 
        # чтобы реконнекты WS не затирали свежие фейлы живыми данными с диска.
        for k, v in data.get("fails", {}).items():
            if k not in self.consecutive_fails:
                self.consecutive_fails[k] = v
                
        for k, v in data.get("quarantine", {}).items():
            if k not in self.quarantine_until:
                self.quarantine_until[k] = v
        
        # Для аналитики используем update, чтобы сохранить дефолтные ключи Трекера
        self.analytics.update(data.get("analytics", {}))
        
        saved_positions = data.get("positions", {})
        self.active_positions.clear()
        for pos_key, pos_data in saved_positions.items():
            pos = ActivePosition.from_dict(pos_data)
            if pos.symbol in self.black_list:
                continue
            self.active_positions[pos_key] = pos