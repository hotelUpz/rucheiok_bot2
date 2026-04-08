# ============================================================
# FILE: CORE/ws_handler.py
# ROLE: FSM для ActivePosition. Интерпретатор событий WebSocket.
# ============================================================

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, Any, TYPE_CHECKING
from CORE.models_fsm import ActivePosition
from c_log import UnifiedLogger

logger = UnifiedLogger("ws")


@dataclass
class ActivePosition:
    # Только лучше сделать две отдельные структуры (одна для Лонга вторая для Шорта. Либо тупо перевести в Dict по типу:
    # {symbol_name: "LONG": {... все нижеследующие агрументы}, "SHORT": {... все нижеследующие агрументы}}
    """ROLE: Data structures for trading state. (Над рядом имен можно поработать с целью улучшения семантичности.)"""
    symbol: str       # Symbol в формате BTCUSDT
    side: str         # LONG | SHORT
    in_signal: bool   # -- возможно надо, возможно нет -- для лока сигналов.
    in_pending: bool  # -- отмечаем факт что пытаемся поставить лимитку. Отменяем после in_position True либо неудавшегося ордера.
    in_position: bool # -- факт что находимся в позиции, то есть current_qty > 0 (даже current_qty > min_qty_notional (берем из специф биржи))
    pending_price: float # -- цена в которую кидали открывающую лимитку. Возможно, если будем использовать Dict то персенифицируем под ключ 'entry' то же сделать и для 'exit' и так далее. (Например потому что у закрывающего ордера тоже ж будет свой pending_price и real exit_price)
    entry_price: float # в 99.999 % случаев entry_price == pending_price (получаем из живолупы ws в models_fsm.py)
    avg_price: float   # возможно не стоит заводить отдельно, а может и стоит. Это средняя точка входа с учетом скупа интерференций и так далее.
    pending_qty: float # плановое расчетное количество открывающего ордера. (расчитываем в оркестраторе)
    current_qty: float # фактическое состояние qty в моменте. (получаем (постоянно обновляем) из живолупы ws в models_fsm.py)
    init_ask1: float   # значение первого аска из в момент сигнала (берем из сигнала)
    init_bid1: float   # значение первого бида из в момент сигнала (берем из сигнала)
    base_target_price_100: float  # значение второго аска|бида для лонгового|Шортового сигнала в момент сигнала (берем из сигнала)
    in_base_mode: bool # флаг фиксации что основной режим охоты запущен.
    current_target_rate: float # текущее значение exit.scenarios.base.target_rate c учетом прогресса смещений shift_demotion
    last_shift_ts: int # время последнего сдвига current_target_rate
    opened_at: float = field(default_factory=time.time) # время первого фила (получаем из живолупы ws в models_fsm.py)
    
    last_negative_check_ts: float = field(default_factory=time.time) # фиксируем после вызова await execute_entry + wait stabilization_ttl
    negative_duration_sec: float = 0.0 # как разница между (time.time() - last_negative_check_ts) и для проверки negative.negative_ttl
    
    in_negative_mode: bool = False # регистрируем запуск закрывающего сценария breakeven_ttl_close. (Лимитка физическая)
    breakeven_start_ts: float = 0.0 # время начала запуска сценария breakeven_ttl_close
    
    in_extrime_mode: bool = False # регистрируем запуск закрывающего сценария extrime_close
    extrime_retries_count: int = 0 # счетчик перемещений закрывающей лимитки при очередном несрабатывании. (Лимитка физическая)
    last_extrime_try_ts: float = 0.0

    interference_disabled: bool # превышен лимит на скупку помех
    
    interf_current_bought_qty: float = 0.0 # фактически берем весь объем уровня, так как мы его целиком скупаем с целью разгрузки стакана в нашу пользу. Возможно нет смысла заводить отдельное поле.
    # interf_left_bought_qty: float = 0.0 # можно не заводить как отдельное поле потому что перед действие все равно будем перевычислять, ориентируясь на текущий current_qty. (Только не забываем Доллары согласовать с количеством-контрактами.)

    # Возможно ниже понадобятся некие утилиты преобразования елементов структуры в читаемо-записываемый формат для json.


class WsInterpreter:
    """
    Роль: получаеет живой снапшот от ws_private.py, на основании которого мутирует стату ActivePosition.
    """
    def __init__(self, active_positions_locker): # типы указываем конкретные -- маст хев. везде и всегда.
        self.active_positions: Dict[str, str[Dict[ActivePosition]]] = {} # или как-то по другому.
        self.active_positions_locker = active_positions_locker
    

    async def ws_event_listener(self, event): # (возможно создать под него датакласс и указать тип. То есть внутри API/PHEMEX/ws_private.py и импортировать сюда)
        """
        if not event: return None

        symbol = event.symbol
        pos_side = event.pos_side # LONG | SHORT

        if event.name.upper() in ("CANCELED", "REJECTED"): return None #(нет смысла обрабатывать эти события)

        if event.name.upper() == "PENDING": 
            async with self.active_positions_locker...:
                # нам важно как-то понимать, открывающий это ордер или закрывающий.
                cur_position = self.active_positions.get(symbol, {}).get(pos_side, {})
                if cur_position:
                    cur_position.in_pending = True
                    cur_position.pending_price = event.entry_price # (первый раз pending_price ставим чисто расчетный наверху у орка.)       

        if event.name.upper() in ("FILLED", "PARTIALLYFILLED"): 
            #(нет разницы, регистрируем любые вхождения). Однако нам важно как-то понимать, открывающий это ордер или закрывающий.
            async with self.active_positions_locker...:
                cur_position = self.active_positions.get(symbol, {}).get(pos_side, {})
                if cur_position:
                    if not cur_position.in_position:
                        cur_position.opened_at = time.time()
                        cur_position.entry_price = event.entry_price
                        
                    cur_position.in_position = True
                    cur_position.current_qty = event.gty
                    # cur_position.in_pending = False     # нужно подумать стоит ли его отменять здесь или в оркестраторе, но отменять нужно.                    
                    

        """

# Ниже вариант обработчика из копи-системы под биржу Мекс для целей копитрейдинга, возможно некоторые решения бдут полезными. (например детерминатор закрытости позиции выполнен филигранно.)

# USER.MASTER.payload_.py

# from __future__ import annotations

# import asyncio
# from dataclasses import dataclass, field
# from typing import *

# from c_utils import Utils, now

# if TYPE_CHECKING:
#     from .state_ import SignalCache, SignalEvent
#     from c_log import UnifiedLogger


# # =====================================================================
# # HL PROTOCOL
# # =====================================================================
# HL_EVENT = Literal["buy", "sell", "canceled"]
# METHOD = Literal["market", "limit", "limit2", "trigger", "del_order_id"]


# # =====================================================================
# # MASTER EVENT
# # =====================================================================
# @dataclass
# class MasterEvent:
#     # _cid # -- can be
#     event: HL_EVENT
#     method: METHOD
#     symbol: str
#     pos_side: str
#     closed: bool
#     payload: Dict[str, Any]
#     sig_type: Literal["copy", "manual"]
#     ts: int = field(default_factory=now)


# # =====================================================================
# # TIMESTAMP EXTRACTION
# # =====================================================================
# def _extract_exchange_ts(ev_raw: "SignalEvent") -> Optional[int]:
#     if not ev_raw:
#         return None
#     raw = ev_raw.raw or {}
#     for key in ("updateTime", "createTime", "timestamp", "time", "ts"):
#         val = raw.get(key)
#         if isinstance(val, (int, float)) and val > 0:
#             if val < 10_000_000_000:
#                 val *= 1000
#             return int(val)
#     return None


# # =====================================================================
# # MASTER PAYLOAD — EXECUTION ONLY
# # =====================================================================
# class MasterPayload:
#     """
#     EXECUTION-ONLY PAYLOAD

#     ИСТИНЫ:
#     • Источник сигналов — ТОЛЬКО execution reports
#     • LIMIT placed = intent
#     • LIMIT filled = execution
#     • MARKET / TRIGGER filled = execution
#     • OCO / TP / SL — НЕ СУЩЕСТВУЮТ на этом уровне
#     """

#     def __init__(
#         self,
#         cache: "SignalCache",
#         logger: "UnifiedLogger",
#         stop_flag: Callable[[], bool],
#     ):
#         self.cache = cache
#         self.logger = logger
#         self.stop_flag = stop_flag

#         self._pending: list[MasterEvent] = []
#         self._stop = False

#         self.out_queue = asyncio.Queue(maxsize=1000)

#         # anti-double fire for LIMIT intent
#         self._limit_intents: set[str] = set()
#         self._pos_qty: dict[str, float] = {}

#     # --------------------------------------------------
#     def stop(self):
#         self._stop = True
#         self.logger.info("MasterPayload: stop requested")

#     # --------------------------------------------------
#     async def run(self):
#         self.logger.info("MasterPayload READY")

#         while not self._stop and not self.stop_flag():
#             await self.cache._event_notify.wait()
#             events = await self.cache.pop_events()

#             for ev in events:
#                 self._route(ev)

#             for mev in self._pending:
#                 await self.out_queue.put(mev)

#             self._pending.clear()

#         self.logger.info("MasterPayload STOPPED")

#     def _is_position_closed(self, symbol: str, pos_side: str,
#                          payload: Optional[dict]) -> Optional[bool]:
#         try:
#             # if not payload.get("reduce_only"):
#             #     return False
#             self._pos_qty.setdefault(symbol, 0)            
#             qty = payload.get("qty")
#             # self.logger.debug(f"qty: {qty}")

#             if qty is not None:
#                 sign = -1 if pos_side.upper() == "SHORT" else 1
#                 self._pos_qty[symbol] += sign * Utils.safe_float(qty, 0)  
#                 return self._pos_qty.get(symbol) == 0
#         except:
#             pass

#         return False         

#     # --------------------------------------------------
#     def _route(self, ev: "SignalEvent"):
#         et = ev.event_type
#         symbol, pos_side = ev.symbol, ev.pos_side
#         if not symbol or not pos_side:
#             return

#         raw = ev.raw
#         if not raw: return

#         if et == "market_filled":
#             payload = self._base_payload(raw)
#             closed = self._is_position_closed(symbol, pos_side, payload)
#             # self.logger.debug(f"closed: {closed}")

#             emit_side = (
#                 {"LONG": "SHORT", "SHORT": "LONG"}[pos_side]
#                 if closed
#                 else pos_side
#             )

#             self._emit(
#                 event="sell" if closed else "buy",
#                 method="market",
#                 symbol=symbol,
#                 pos_side=emit_side,
#                 closed=closed,
#                 payload=self._base_payload(raw),
#                 ev_raw=ev,
#             )
#             return

#         # ==================================================
#         # LIMIT FILLED
#         # ==================================================
#         elif et == "limit_filled":
#             payload = self._base_payload(raw)
#             closed = self._is_position_closed(symbol, pos_side, payload)

#             oid = raw.get("orderId")
#             if oid and oid in self._limit_intents:
#                 self._limit_intents.discard(oid)

#                 self._emit(
#                     event=None,                     # 🔑 НЕ торговое событие
#                     method="del_order_id",           # 🔑 housekeeping
#                     symbol=symbol,
#                     pos_side=pos_side,               # 🔑 ТА ЖЕ сторона
#                     closed=False,                    # или None
#                     payload={"order_id": oid},       # 🔑 ЭТО ГЛАВНОЕ
#                     ev_raw=ev,
#                 )
#                 return 

#             emit_side = (
#                 {"LONG": "SHORT", "SHORT": "LONG"}[pos_side]
#                 if closed
#                 else pos_side
#             )
#             # self.logger.debug(f"closed: {closed}")

#             self._emit(
#                 event="sell" if closed else "buy",
#                 method="limit",
#                 symbol=symbol,
#                 pos_side=emit_side,
#                 closed=closed,
#                 payload=payload,
#                 ev_raw=ev,
#             )
#             return

#         # ==================================================
#         # LIMIT PLACED (INTENT)
#         # ==================================================
#         elif et == "limit_placed":
#             # self.logger.debug("limit_placed")
#             oid = raw.get("orderId")
#             if oid:
#                 self._limit_intents.add(oid)

#             self._emit(
#                 event="buy",
#                 method="limit2",
#                 symbol=symbol,
#                 pos_side=pos_side,
#                 closed=False,
#                 payload=self._base_payload(raw),
#                 ev_raw=ev,
#             )
#             return

#         # ==================================================
#         # TRIGGER FILLED
#         # ==================================================
#         elif et == "trigger_filled":
#             reduce_only = bool(raw.get("reduceOnly"))
#             is_sell = raw.get("side") not in (1, 3)

#             emit_side = (
#                 {"LONG": "SHORT", "SHORT": "LONG"}[pos_side]
#                 if reduce_only
#                 else pos_side
#             )

#             self._emit(
#                 event="sell" if is_sell else "buy",
#                 method="trigger",
#                 symbol=symbol,
#                 pos_side=emit_side,
#                 closed=reduce_only,
#                 payload=self._base_payload(raw),
#                 ev_raw=ev,
#             )
#             return

#         # ==================================================
#         # CANCEL
#         # ==================================================
#         if et in ("order_cancelled", "order_invalid"):
#             oid = raw.get("orderId")
#             if oid:
#                 self._limit_intents.discard(oid)

#             self._emit(
#                 event="canceled",
#                 method="limit2",
#                 symbol=symbol,
#                 pos_side=pos_side,
#                 closed=False,
#                 payload={"order_id": oid},
#                 ev_raw=ev,
#             )

#     # --------------------------------------------------
#     @staticmethod
#     def _base_payload(raw: dict) -> dict:
#         return {
#             "order_id": raw.get("orderId"),
#             "qty": Utils.safe_float(raw.get("vol")),
#             "price": Utils.safe_float(
#                 raw.get("price")
#                 or raw.get("dealAvgPrice")
#                 or raw.get("avgPrice")
#             ),
#             "leverage": raw.get("leverage"),
#             "open_type": raw.get("openType"),
#             "reduce_only": bool(raw.get("reduceOnly")),
#         }

#     # --------------------------------------------------
#     def _emit(
#         self,
#         *,
#         event: HL_EVENT,
#         method: METHOD,
#         symbol: str,
#         pos_side: str,
#         payload: Dict[str, Any],
#         ev_raw: Optional["SignalEvent"],
#         closed: bool = False,
#         sig_type: Literal["copy", "manual"] = "copy",
#     ):
#         ts = now()
#         exec_ts = _extract_exchange_ts(ev_raw)
#         tech_ts = ev_raw.ts if ev_raw else ts
#         if exec_ts and tech_ts:
#             ts = min(exec_ts, tech_ts)

#         payload = dict(payload)
#         payload["exec_ts"] = exec_ts

#         self._pending.append(
#             MasterEvent(
#                 event=event,
#                 method=method,
#                 symbol=symbol,
#                 pos_side=pos_side,
#                 closed=closed,
#                 payload=payload,
#                 sig_type=sig_type,
#                 ts=ts,
#             )
#         )