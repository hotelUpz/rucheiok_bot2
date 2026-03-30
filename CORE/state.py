# ============================================================
# FILE: CORE/state.py
# ROLE: Менеджер состояния (Memory & Local Storage)
# ============================================================

import os
import json
import asyncio
from typing import Dict, Set, List
from CORE.models import ActivePosition
from c_log import UnifiedLogger

logger = UnifiedLogger("core")

class BotState:
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

    def _sync_save(self, state_dict: dict):
        tmp_file = f"{self.filepath}.{os.getpid()}.tmp"
        for attempt in range(3):
            try:
                with open(tmp_file, "w", encoding="utf-8") as f:
                    json.dump(state_dict, f, indent=4)
                os.replace(tmp_file, self.filepath)
                return
            except Exception as e:
                if attempt == 2:
                    logger.error(f"Ошибка сохранения стейта: {e}")
                else:
                    import time as _time
                    _time.sleep(0.05 * (attempt + 1))
            finally:
                try:
                    if os.path.exists(tmp_file):
                        os.remove(tmp_file)
                except Exception:
                    pass

    async def save(self):
        """Потокобезопасная отправка данных на диск"""
        async with self._lock:
            state_dict = {
                "positions": {
                    pos_key: pos.to_dict() 
                    for pos_key, pos in self.active_positions.items() 
                    if pos.symbol not in self.black_list
                },
                "fails": dict(self.consecutive_fails),
                "quarantine": dict(self.quarantine_until)
            }
            await asyncio.to_thread(self._sync_save, state_dict)

    def load(self):
        """Синхронная загрузка стейта при старте"""
        if not os.path.exists(self.filepath): return
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            self.consecutive_fails = data.get("fails", {})
            self.quarantine_until = data.get("quarantine", {})
            
            saved_positions = data.get("positions", {})
            self.active_positions.clear()
            for pos_key, pos_data in saved_positions.items():
                pos = ActivePosition.from_dict(pos_data)
                if pos.symbol in self.black_list:
                    continue
                self.active_positions[pos_key] = pos
        except Exception as e:
            logger.error(f"Ошибка чтения стейта: {e}")

    # async def save(self):
    #     """Потокобезопасная отправка данных на диск"""
    #     async with self._lock: # <-- ДОБАВИТЬ ЭТО
    #         state_dict = {
    #             "positions": {sym: pos.to_dict() for sym, pos in self.active_positions.items() if sym not in self.black_list},
    #             "fails": dict(self.consecutive_fails),
    #             "quarantine": dict(self.quarantine_until)
    #         }
    #         await asyncio.to_thread(self._sync_save, state_dict)

    # def load(self):
    #     """Синхронная загрузка стейта при старте"""
    #     if not os.path.exists(self.filepath): return
    #     try:
    #         with open(self.filepath, "r", encoding="utf-8") as f:
    #             data = json.load(f)
            
    #         self.consecutive_fails = data.get("fails", {})
    #         self.quarantine_until = data.get("quarantine", {})
            
    #         saved_positions = data.get("positions", {})
    #         self.active_positions.clear()
    #         for sym, pos_data in saved_positions.items():
    #             if sym in self.black_list:
    #                 continue
    #             self.active_positions[sym] = ActivePosition.from_dict(pos_data)
    #     except Exception as e:
    #         logger.error(f"Ошибка чтения стейта: {e}")