# ============================================================
# FILE: CORE/models_fsm.py
# ROLE: FSM для ActivePosition. Интерпретатор событий WebSocket.
# ============================================================

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, fields
from typing import Dict, Any, TYPE_CHECKING
from c_log import UnifiedLogger

if TYPE_CHECKING:
    from CORE.restorator import BotState

logger = UnifiedLogger("ws")


@dataclass
class ActivePosition:
    """
    ROLE: Data structures for trading state.
    Хранит абсолютно все маркеры, необходимые для сценариев выхода и оркестратора.
    """
    # все таки лучше разделить по ключам LONG | SHORT. ActivePosition можно использовать как шаблон конкретной позиции. Но во всем остальном коде помнить, когда обращаться к данным по типу pos.base_target_price_100 (очевидно в режиме хедж режима это будут разные переменные)
    symbol: str             # Symbol в формате BTCUSDT
    side: str               # LONG | SHORT
    
    # --- State Flags (Флаги состояний) ---
    in_signal: bool = False
    in_pending: bool = False             # Пытаемся поставить лимитку
    in_position: bool = False            # Фактически в рынке (current_qty > 0)
    in_base_mode: bool = False           # Пройдена стабилизация, запущен Base Scenario
    in_breakeven_mode: bool = False      # Регистрация флага перехода в режим закрытия позиции по истечение тотального таймаута позиции.
    in_extrime_mode: bool = False        # Аварийный выход активен
    interference_disabled: bool = False  # Лимит на скупку помех превышен
    
    # --- Pricing (Ценовые маркеры) ---
    entry_price: float = 0.0             # Цена первого фила (фактический вход)
    pending_price: float = 0.0           # Расчетная цена лимитки на вход
    avg_price: float = 0.0               # Средняя цена с учетом доборов
    # current_close_price: float = 0.0     # Физическая цена стоящего ордера на выход (Идемпотентность)
    
    # --- Quantities (Объемы) ---
    pending_qty: float = 0.0             # Расчетный объем открывающего ордера
    current_qty: float = 0.0             # Фактический объем позиции (источник: WS positions)
    interf_comulative_qty: float = 0.0 
    
    # --- Signal Initialization (Данные из первоначального паттерна) ---
    init_ask1: float = 0.0
    init_bid1: float = 0.0
    base_target_price_100: float = 0.0   # Цель из паттерна стакана
    
    # --- Tracking & Hunting ---
    current_target_rate: float = 1.0     # Множитель цели (падает при shift_demotion)
    close_order_id: str = ""             # ID текущей лимитки на закрытие
    failed_interference_prices: Dict[str, int] = field(default_factory=dict)
    
    # --- Timestamps ---
    opened_at: float = field(default_factory=time.time)
    last_shift_ts: float = 0.0
    last_negative_check_ts: float = 0.0
    breakeven_start_ts: float = 0.0
    last_extrime_try_ts: float = 0.0
    
    # --- Counters ---
    extrime_retries_count: int = 0

    def to_dict(self) -> dict:
        return self.__dict__

    @classmethod
    def from_dict(cls, data: dict) -> "ActivePosition":
        valid_keys = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)


class WsInterpreter:
    """
    РОЛЬ: Асинхронно перехватывает сырые данные из WS, безопасно лочит конкретную
    пару/сторону и мутирует ActivePosition в глобальном стейте (BotState).
    """
    def __init__(self, state: BotState):
        self.state = state
        self._locks: Dict[str, asyncio.Lock] = {}

    def _get_lock(self, pos_key: str) -> asyncio.Lock:
        """Гранулярный лок на каждую позицию."""
        if pos_key not in self._locks:
            self._locks[pos_key] = asyncio.Lock()
        return self._locks[pos_key]

    @staticmethod
    def _safe_float(val: Any, default: float = 0.0) -> float:
        try:
            return float(val) if val is not None else default
        except (ValueError, TypeError):
            return default

    async def process_phemex_message(self, event_data: Dict[str, Any]):
        orders = event_data.get("orders") or event_data.get("order_p") or []
        positions = event_data.get("positions") or event_data.get("position_p") or []
        
        for order in orders:
            await self._handle_order_update(order)
            
        for pos in positions:
            await self._handle_position_update(pos)

    async def _handle_order_update(self, o: Dict[str, Any]):
        symbol = o.get("symbol")
        pos_side_raw = o.get("posSide", o.get("side", ""))
        
        if not symbol or not pos_side_raw: 
            return
            
        pos_side = "LONG" if pos_side_raw.lower() in ("long", "buy") else "SHORT"
        pos_key = f"{symbol}_{pos_side}"
        ord_status = str(o.get("ordStatus", "")).upper()
        
        async with self._get_lock(pos_key):
            pos: ActivePosition = self.state.active_positions.get(pos_key)
            if not pos: 
                return

            if ord_status in ("NEW", "UNTRIGGERED"):
                pos.in_pending = True
                
            elif ord_status in ("FILLED", "PARTIALLYFILLED"):
                if not pos.in_position:
                    pos.opened_at = time.time()
                    pos.entry_price = self._safe_float(o.get("priceRp", o.get("price")))
                
                pos.in_position = True
                if ord_status == "FILLED":
                    pos.in_pending = False
                    
            elif ord_status in ("CANCELED", "REJECTED"):
                pos.in_pending = False

    async def _handle_position_update(self, p: Dict[str, Any]):
        symbol = p.get("symbol")
        pos_side_raw = p.get("posSide", p.get("side", ""))
        
        if not symbol or not pos_side_raw: 
            return
            
        pos_side = "LONG" if pos_side_raw.lower() in ("long", "buy") else "SHORT"
        pos_key = f"{symbol}_{pos_side}"
        
        async with self._get_lock(pos_key):
            pos: ActivePosition = self.state.active_positions.get(pos_key)
            if not pos: 
                return
                
            size = self._safe_float(p.get("sizeRq", p.get("size")))
            avg_price = self._safe_float(p.get("avgEntryPriceRp", p.get("avgEntryPrice")))

            if size > 0:
                pos.current_qty = size
                if avg_price > 0:
                    pos.avg_price = avg_price
                pos.in_position = True
            else:
                """TODO: не уверен что можно и нужно так жестко сбрасываать позицию. если и так, нужен будет минимальный слепок для отчетности в тг и логи. Удалять жестко не стоит, возможно ресетить, прогоняя через начальный bootstrap"""                
                # logger.info(f"[{pos_key}] 🛑 Позиция закрыта (WS Payload). Стейт очищен.")
                # self.state.active_positions.pop(pos_key, None)