# ============================================================
# FILE: CORE/executor.py
# ROLE: Шаблоны отправки ордеров на биржу, защита от гонок потоков...
# ============================================================
from __future__ import annotations

import asyncio
import time
import pytz
from typing import Dict, Any, TYPE_CHECKING, Optional
from c_log import UnifiedLogger
from decimal import Decimal, ROUND_DOWN
from consts import TIME_ZONE
from CORE._utils import Reporters

if TYPE_CHECKING:
    from CORE.orchestrator import TradingBot
    from ENTRY.pattern_math import EntrySignal

from dotenv import load_dotenv    
load_dotenv()

logger = UnifiedLogger(name="bot")
TZ = pytz.timezone(TIME_ZONE)

def round_step(value: float, step: float) -> float:
    if not step or step <= 0:
        return value
    val_d = Decimal(str(value))
    step_d = Decimal(str(step))
    rounded = (val_d / step_d).quantize(Decimal("1"), rounding=ROUND_DOWN) * step_d
    return float(rounded)

class OrderExecutor:
    """
    Прокладка между оркестратором и сетевым адаптером. Содержит шаблоны рутинных операций перед финальной отправкой запросов.
    """
    def __init__(self, tb: "TradingBot"):
        self.tb = tb
        self.client = tb.private_client
        self.cfg = tb.cfg
        
        # Лобовой доступ к параметрам входа
        entry_cfg = self.cfg["entry"]
        self.entry_timeout = entry_cfg["entry_timeout_sec"]
        self.max_entry_retries = entry_cfg["max_place_order_retries"]
        
        # Параметры выхода
        self.max_exit_retries = self.cfg["exit"]["max_place_order_retries"]
        
        # Минимальный тайминг жизни ордера (чтобы дать бирже шанс налить)
        self.min_order_life_sec = self.cfg.get("exit", {}).get("min_order_life_sec", 0.05)
        
        # Блок риска: если Notional или Margin не заданы, падает сразу
        risk = self.cfg["risk"]
        self.notional_limit = float(risk["notional_limit"])
        self.margin_over_size_pct = float(risk["margin_over_size_pct"])

    async def _smart_wait(self, pos_key: str, initial_qty: float, timeout_sec: float, min_wait_sec: float) -> None:
        """
        Безопасная утилита умного поллинга. 
        Ждет минимальное время, а затем каждые 10мс проверяет изменение объема позиции.
        Если объем изменился (или позиция закрылась) — досрочно вырывается из петли.
        """
        start_ts = time.time()
        
        # 1. Обязательный минимальный таймаут (чтобы ордер успел встать в стакан)
        if min_wait_sec > 0:
            await asyncio.sleep(min_wait_sec)
            
        # 2. Петля поллинга с шагом 10 мс
        poll_step = 0.01 
        while time.time() - start_ts < timeout_sec:
            async with self.tb._get_lock(pos_key):
                pos = self.tb.state.active_positions.get(pos_key)
                
                # Если позиции уже нет (успела закрыться) — выходим
                if not pos:
                    break
                
                # Если текущий объем перестал быть равен стартовому (налили частично или полностью) — выходим
                if pos.current_qty != initial_qty:
                    break
                    
            await asyncio.sleep(poll_step)

    async def cancel_all_orders(self, symbol: str) -> bool:
        try:
            resp = await self.client.cancel_all_orders(symbol)
            return resp.get("code") == 0
        except: return False

    async def execute_cancel(self, symbol: str, pos_side: str, order_id: str) -> bool:
        """
        Роль: некая промежуточная прокладка между сетевым адаптером и инициирующей стороной.
        Ошибку неудачи отмены логируем.
        """
        if not order_id: return False
        try:
            resp = await self.client.cancel_order(symbol, order_id, pos_side)
            if resp.get("code") == 0: return True
            
            err_msg = str(resp.get("msg", "")).lower()
            if "filled" not in err_msg and "not found" not in err_msg:
                logger.debug(f"[{symbol}] Отказ отмены {order_id}: {resp}")
            return False
        except Exception as e:
            logger.debug(f"[{symbol}] Исключение отмены {order_id}: {e}")
            return False
        
    async def interf_bought(self, symbol: str, pos_key: str, qty: float, order_price: float, interf_timeout) -> Optional[float]:
        """
        Роль: скупка маленьких ордеров. Неудачи логируем. Допустимые остатки для скупки переосмысливаются в оркестраторе.
        Возврат -- фактически исполненное количество ордера float | None
        """
        try:
            spec = self.tb.symbol_specs.get(symbol)
            if not spec: return None

            price = round_step(order_price, spec.tick_size)
            req_qty = round_step(qty, spec.lot_size)
            if req_qty < spec.lot_size: return None

            initial_qty = 0.0
            async with self.tb._get_lock(pos_key):
                pos = self.tb.state.active_positions.get(pos_key)
                if not pos: return None
                pos_side_raw = pos.side
                initial_qty = pos.current_qty

            side = "Buy" if pos_side_raw == "LONG" else "Sell"
            phemex_pos_side = "Long" if pos_side_raw == "LONG" else "Short"

            resp = await self.client.place_limit_order(symbol, side, req_qty, price, phemex_pos_side)
            if resp.get("code") == 0:
                order_id = resp.get("data", {}).get("orderID")
                usdt_vol = req_qty * price
                logger.debug(f"[{pos_key}] Скупка помехи прошла успешно! Объем: {req_qty} (≈ {usdt_vol:.2f} $)")
                
                # Умное ожидание вместо глупого слипа
                await self._smart_wait(pos_key, initial_qty, interf_timeout, self.min_order_life_sec)
                
                # Отмена остатка — fire-and-forget, не блокируем основной поток
                if order_id:
                    asyncio.create_task(self.execute_cancel(symbol, phemex_pos_side, order_id))

                async with self.tb._get_lock(pos_key):
                    pos = self.tb.state.active_positions.get(pos_key)
                    if not pos: return None
                    pos_side_raw = pos.side

                    return req_qty
            else:
                logger.debug(f"[{pos_key}] Отказ скупки помехи: {resp}")
        except Exception as e:
            logger.debug(f"[{pos_key}] Ошибка скупки помех: {e}")
        return None

    async def execute_entry(self, symbol: str, pos_key: str, signal: EntrySignal) -> bool:
        """
        1. Расчитывает количество ордера и цену постатовки лимитного ордера, согласно рискам.
        2. Ставит ордер через await. Дожидается ответа в течение entry_timeout_sec.
        3. Если успех: логирует результат, проверяет остаток, отменяет остаток, Возвращает True.
           Если неуспех: логирует ошибку, делает ретрай, Возвращает False.
        """
        try:
            spec = self.tb.symbol_specs.get(symbol)
            if not spec: return False

            price = round_step(signal.price, spec.tick_size)
            if not price: return
            row_vol_asset = signal.row_vol_asset or 0.0
            
            target_usdt = row_vol_asset * price * (1 + self.margin_over_size_pct / 100.0)
            target_usdt = min(target_usdt, self.notional_limit)
            
            qty = round_step(target_usdt / price, spec.lot_size)
            if qty < spec.lot_size: 
                logger.warning(f"[{pos_key}] Qty {qty} меньше лота {spec.lot_size}. Пропуск.")
                return False
            
            initial_qty = 0.0
            async with self.tb._get_lock(pos_key):
                pos = self.tb.state.active_positions.get(pos_key)
                if pos:
                    initial_qty = pos.current_qty
                pos.pending_qty = qty

            side = "Buy" if signal.side == "LONG" else "Sell"
            phemex_pos_side = "Long" if signal.side == "LONG" else "Short"

            for attempt in range(max(1, self.max_entry_retries)):
                try:
                    resp = await self.client.place_limit_order(symbol, side, qty, price, phemex_pos_side)
                    if resp.get("code") == 0:
                        order_id = resp.get("data", {}).get("orderID")
                        
                        # Умное ожидание вместо глупого слипа
                        await self._smart_wait(pos_key, initial_qty, self.entry_timeout, self.min_order_life_sec)
                        
                        if order_id: 
                            curr_qty = 0.0
                            async with self.tb._get_lock(pos_key):
                                pos = self.tb.state.active_positions.get(pos_key)
                                if pos: curr_qty = pos.current_qty
                            
                            # Если частичное исполнение или вообще ноль — отменяем остаток fire-and-forget
                            if curr_qty < qty:
                                asyncio.create_task(self.execute_cancel(symbol, phemex_pos_side, order_id))
                        
                        # Проверяем, налило ли хоть что-то
                        async with self.tb._get_lock(pos_key):
                            pos = self.tb.state.active_positions.get(pos_key)
                            if pos and (pos.current_qty > 0 or getattr(pos, 'in_position', False)): 
                                entry_usd_vol = pos.current_qty * price # Считаем доллары
                                logger.info(f"[{pos_key}] ✅ Вход выполнен. Объем: {pos.current_qty} (≈ {entry_usd_vol:.2f} $)")

                                if pos.max_allowed_remains == 0.0:
                                    pos.max_allowed_remains = pos.pending_qty * self.tb.scen_interf.max_vol_pct   
                                    max_allowed_remains_usd_vol = pos.max_allowed_remains * price # Считаем доллары 
                                    logger.debug(f"[{pos_key}] Зафиксирован лимит скупки: {pos.max_allowed_remains} (≈ {max_allowed_remains_usd_vol:.2f} $)")    

                                if self.tb.tg:
                                    msg = Reporters.entry_signal(symbol, signal, signal.b_price, signal.p_price)
                                    asyncio.create_task(self.tb.tg.send_message(msg))
                                return True
                                
                        return False # Ордер не налился
                    else:
                        logger.warning(f"[{pos_key}] ❌ Ошибка входа API: {resp}")
                except Exception as e:
                    logger.error(f"[{pos_key}] Исключение execute_entry: {e}")
                
            return False
            
        except Exception as e:
            logger.error(f"[{pos_key}] Глобальная ошибка execute_entry: {e}")
            return False
        
    async def execute_exit(self, symbol: str, pos_key: str, order_price: float, timeout_sec: float) -> bool:
        """
        1. Безопасно отменяет предыдущий лимитный ордер закрытия (если он завис из-за обрыва сети).
        2. Ставит новый ордер и ждет timeout_sec через умный поллинг.
        3. Если позиция не закрылась полностью — отменяет остаток.
        """
        try:
            spec = self.tb.symbol_specs.get(symbol)
            if not spec: return False

            # 1. Забираем параметры и проверяем наличие старого ордера
            async with self.tb._get_lock(pos_key):
                pos = self.tb.state.active_positions.get(pos_key)
                if not pos or not getattr(pos, 'in_position', False) or pos.current_qty <= 0: 
                    return False
                qty = pos.current_qty
                pos_side_raw = pos.side
                old_order_id = pos.close_order_id # <-- Читаем ID старого ордера

            phemex_pos_side = "Long" if pos_side_raw == "LONG" else "Short"
            
            # 2. УБИВАЕМ СТАРЫЙ ОРДЕР fire-and-forget (разблокируем маржу для нового ордера)
            if old_order_id:
                asyncio.create_task(self.execute_cancel(symbol, phemex_pos_side, old_order_id))
                # Очищаем память от старого ордера
                async with self.tb._get_lock(pos_key):
                    pos = self.tb.state.active_positions.get(pos_key)
                    if pos: pos.close_order_id = ""

            # 3. Подготовка к постановке нового ордера
            price = round_step(order_price, spec.tick_size)
            target_qty = round_step(qty, spec.lot_size)
            if target_qty < spec.lot_size: return False

            side = "Sell" if pos_side_raw == "LONG" else "Buy"

            # В методе execute_exit (перед отправкой ордера)
            async with self.tb._get_lock(pos_key):
                pos = self.tb.state.active_positions.get(pos_key)
                if pos:
                    pos.exit_price_hint = price  # <--- Сохраняем "намерение"

            # 4. Основной цикл постановки
            for attempt in range(max(1, self.max_exit_retries)):
                try:
                    resp = await self.client.place_limit_order(symbol, side, target_qty, price, phemex_pos_side)
                    if resp.get("code") == 0:
                        new_order_id = resp.get("data", {}).get("orderID")
                        
                        # Сохраняем новый ID в стейт на случай краша (чтобы на следующем тике Оркестратор его нашел и убил)
                        if new_order_id:
                            async with self.tb._get_lock(pos_key):
                                pos = self.tb.state.active_positions.get(pos_key)
                                if pos: pos.close_order_id = new_order_id
                            
                        # Умное ожидание вместо глупого слипа
                        await self._smart_wait(pos_key, qty, timeout_sec, self.min_order_life_sec)
                        
                        # 5. Проверка исполнения после паузы
                        if new_order_id: 
                            curr_qty = 0.0
                            async with self.tb._get_lock(pos_key):
                                pos = self.tb.state.active_positions.get(pos_key)
                                if pos: curr_qty = pos.current_qty
                                
                            if curr_qty > 0:
                                # Позиция всё еще висит -> отменяем лимитку fire-and-forget
                                asyncio.create_task(self.execute_cancel(symbol, phemex_pos_side, new_order_id))
                                async with self.tb._get_lock(pos_key):
                                    pos = self.tb.state.active_positions.get(pos_key)
                                    if pos and pos.close_order_id == new_order_id:
                                        pos.close_order_id = "" # Успешно отменили, сбрасываем ID
                            else:
                                # Позиция закрыта
                                exit_usd_vol = target_qty * price # Считаем доллары
                                logger.info(f"[{pos_key}] 🏁 Выход выполнен. Объем: {target_qty} (≈ {exit_usd_vol:.2f} $)")
                                
                        return True
                    else:
                        logger.warning(f"[{pos_key}] ❌ Ошибка выхода API: {resp}")
                except Exception as e:
                    logger.error(f"[{pos_key}] Исключение execute_exit: {e}")
                
            return False
            
        except Exception as e:
            logger.error(f"[{pos_key}] Глобальная ошибка execute_exit: {e}")
            return False
