# ============================================================
# FILE: CORE/executor.py
# ROLE: Отправка ордеров на биржу, защита от гонок потоков и управление финалом цикла
# ============================================================
from __future__ import annotations

import asyncio
import time
from typing import Dict, Any, TYPE_CHECKING
from c_log import UnifiedLogger
from CORE.models import ActivePosition
from CORE.bot_utils import Reporters
from utils import round_step

if TYPE_CHECKING:
    from CORE.bot import TradingBot
    from API.PHEMEX.stakan import DepthTop

logger = UnifiedLogger("bot")

class OrderExecutor:
    """
    Единственная точка отправки ордеров на биржу.

    Ключевые инварианты:
    - get_lock(pos_key): один AsyncLock на pos_key предотвращает гонки потоков.
    - execute_entry: блокирует pos_key через PENDING_HTTP до получения order_id.
    - handle_exit_action:
        * CANCEL_CLOSE       — отмена физического ордера, сброс close_order_id
        * UPDATE_TARGET      — смена виртуальной цели (pos.current_close_price)
        * PLACE_DYNAMIC_CLOSE/PLACE_EXTRIME_LIMIT — cancel+place с идемпотентной проверкой цены
        * BUY_OUT_INTERFERENCE — доливка к позиции (не закрытие!)
    - pos.current_close_price = цена последнего размещённого ордера (только Executor пишет).
    - finalize_position_cycle: зачистка, карантин, сохранение — вызывается из ws_handler.
    - _transition_to_extrime вызывается через self.tb.exit_engine (единая точка с TG).
    """
    
    def __init__(self, tb: 'TradingBot') -> None:
        self.tb = tb
        self.locks: Dict[str, asyncio.Lock] = {}
        
        # Инвариант: таймауты устояния ордеров перед срезанием остатка
        self.hunting_timeout_sec = float(self.tb.cfg.get("exit", {}).get("hunting_timeout_sec", 1.0))
        self.entry_timeout_sec = float(self.tb.cfg.get("entry", {}).get("entry_timeout_sec", 0.5))

    def get_lock(self, pos_key: str) -> asyncio.Lock:
        if pos_key not in self.locks:
            self.locks[pos_key] = asyncio.Lock()
        return self.locks[pos_key]

    async def cancel_order_via_ws(self, symbol: str, order_id: str, pos_side: str) -> None:
        """Инвариант: мгновенная отмена остатка ордера."""
        try:
            await self.tb.private_client.cancel_order(symbol, order_id, pos_side)
            logger.info(f"🧹 Запрошена отмена остатка ордера #{order_id[:8]}...")
        except Exception as e:
            logger.debug(f"Ошибка отмены остатка #{order_id[:8]}: {e}")

    async def finalize_dust_position(self, symbol: str, pos_key: str, pos: ActivePosition) -> None:
        """Сброс стейта, если остаток позиции (огрызок) меньше 5 USDT."""
        logger.info(f"🧹 [{pos_key}] Огрызок позиции < 5 USDT. Сбрасываем стейт.")
        self.tb.state.pending_entry_orders.pop(pos_key, None)
        self.tb.state.pending_interference_orders.pop(pos_key, None)
        self.tb.state.active_positions.pop(pos_key, None)
        await self.tb.state.save()

    def finalize_position_cycle(self, symbol: str, pos_key: str, close_price: float) -> None:
        """Конец жизненного цикла: зачистка хвостов, расчет карантина, удаление из стейта."""
        pos = self.tb.state.active_positions.get(pos_key)
        if not pos: return
        
        pos_side = "Long" if pos.side == "LONG" else "Short"
        
        # 1. Убиваем зависшие лимитки (помехи и входы)
        interf_id = self.tb.state.pending_interference_orders.get(pos_key)
        if interf_id and interf_id != "PENDING_HTTP":
            asyncio.create_task(self.tb.private_client.cancel_order(symbol, interf_id, pos_side))
            
        entry_id = self.tb.state.pending_entry_orders.get(pos_key)
        if entry_id and entry_id != "PENDING_HTTP":
            asyncio.create_task(self.tb.private_client.cancel_order(symbol, entry_id, pos_side))

        # 2. Логика карантина (Risk Management)
        is_loss = False
        if close_price > 0:
            if pos.side == "LONG" and close_price < pos.entry_price: is_loss = True
            if pos.side == "SHORT" and close_price > pos.entry_price: is_loss = True

        if is_loss:
            self.tb.state.consecutive_fails[symbol] = self.tb.state.consecutive_fails.get(symbol, 0) + 1
            q_cfg = self.tb.cfg.get("risk", {}).get("quarantine", {})
            max_fails = q_cfg.get("max_consecutive_fails")
            
            if max_fails is not None and self.tb.state.consecutive_fails[symbol] >= max_fails:
                q_hours = q_cfg.get("quarantine_hours", 24)
                if str(q_hours).lower() == "inf":
                    self.tb.state.quarantine_until[symbol] = float("inf")
                    self.tb.set_blacklist(list(self.tb.black_list) + [symbol])
                    q_msg = "Навсегда (BlackList)"
                else:
                    self.tb.state.quarantine_until[symbol] = time.time() + float(q_hours) * 3600
                    q_msg = f"на {q_hours} ч."
                    
                logger.warning(f"🚨 КАРАНТИН #{symbol}: {q_msg} (Failures: {self.tb.state.consecutive_fails[symbol]})")
                if self.tb.tg: 
                    asyncio.create_task(self.tb.tg.send_message(f"☣️ <b>Карантин</b>\nМонета: #{symbol}\nСрок: {q_msg}"))
        else:
            self.tb.state.consecutive_fails[symbol] = 0

        # 3. Финальный сброс стейта
        self.tb.state.pending_entry_orders.pop(pos_key, None)
        self.tb.state.pending_interference_orders.pop(pos_key, None)
        self.tb.state.active_positions.pop(pos_key, None)
        asyncio.create_task(self.tb.state.save())
        logger.info(f"✅ Цикл #{pos_key} успешно завершен. Сторона свободна.")

    async def _handle_order_fail(self, symbol: str, pos_key: str, pos: ActivePosition) -> None:
        """Централизованный диспетчер аварий API."""
        max_fails: int = self.tb.cfg.get("app", {}).get("max_place_order_retries", 5)
        
        if pos.place_order_fails >= max_fails:
            if pos.qty > 0:
                if not pos.in_extrime_mode:
                    # Делаем переход через единственную точку входа (TG + сброс счётчиков)
                    logger.error(f"[{pos_key}] ⚠️ Лимит ошибок API исчерпан! Перевод в EXTRIME MODE.")
                    self.tb.exit_engine._transition_to_extrime(pos, "Лимит ошибок API исчерпан")
                    pos.place_order_fails = 0
                else:
                    logger.error(f"[{pos_key}] 🚨 КРИТИЧЕСКИЙ ОТКАЗ! Экстрим-ордера отклоняются биржей. Ручное вмешательство! Позиция ({pos.qty} шт) СОХРАНЕНА.")
                    if self.tb.tg:
                        asyncio.create_task(self.tb.tg.send_message(
                            Reporters.extrime_alert(symbol, f"Позиция {pos.qty} шт. Требуется ручное вмешательство!")
                        ))
            else:
                logger.error(f"[{pos_key}] 🗑 Ошибки API до открытия позиции. Сбрасываем стейт.")
                self.tb.state.active_positions.pop(pos_key, None)
                
        await self.tb.state.save()

    async def handle_exit_action(self, symbol: str, pos_key: str, action: Dict[str, Any]) -> None:
        """Выполнение предписанных сценариями выхода (Exit Engine) действий."""
        async with self.get_lock(pos_key):
            pos: ActivePosition | None = self.tb.state.active_positions.get(pos_key)
            spec = self.tb.symbol_specs.get(symbol)
            if not pos or not spec or pos.qty <= 0: 
                return

            act: str = action["action"]
            pos_side: str = "Long" if pos.side == "LONG" else "Short"
            close_side: str = "Sell" if pos.side == "LONG" else "Buy"

            # 1. Отмена физического ордера
            if act == "CANCEL_CLOSE":
                if pos.close_order_id:
                    try: 
                        await self.tb.private_client.cancel_order(symbol, pos.close_order_id, pos_side)
                    except Exception: 
                        pass
                    pos.close_order_id = None
                return

            # 2. Обновление виртуальной цели
            if act == "UPDATE_TARGET":
                if "price" in action: 
                    target_price = action["price"]
                elif pos.side == "LONG": 
                    target_price = pos.entry_price + (pos.base_target_price_100 - pos.entry_price) * action["new_rate"]
                else: 
                    target_price = pos.entry_price - (pos.entry_price - pos.base_target_price_100) * action["new_rate"]

                target_price = round_step(target_price, spec.tick_size)
                if target_price > 0:
                    pos.current_close_price = target_price
                    await self.tb.state.save()
                    logger.info(f"🎯 [{pos_key}] ВИРТУАЛЬНАЯ ЦЕЛЬ ИЗМЕНЕНА: {pos.current_close_price}")
                return

            # 3. Физический удар по стакану (Снайпер или Экстрим)
            if act in ("PLACE_EXTRIME_LIMIT", "PLACE_DYNAMIC_CLOSE"):
                target_price: float = round_step(action["price"], spec.tick_size)
                if target_price <= 0: 
                    return

                if pos.close_order_id:
                    if pos.current_close_price > 0 and abs(pos.current_close_price - target_price) < max(spec.tick_size, 1e-12):
                        return
                    else:
                        try:
                            await self.tb.private_client.cancel_order(symbol, pos.close_order_id, pos_side)
                        except Exception:
                            pass
                        pos.close_order_id = None

                pos.current_close_price = target_price
                
                # ⏱ ВЗВОД ТАЙМЕРА ОХОТЫ
                pos.hunting_active_until = time.time() + self.hunting_timeout_sec

                try:
                    resp: Dict[str, Any] = await self.tb.private_client.place_limit_order(
                        symbol=symbol, side=close_side, qty=pos.qty, price=target_price, pos_side=pos_side
                    )
                    order_id: str = str(resp.get("data", {}).get("orderID") or resp.get("result", {}).get("orderId", ""))
                    
                    if order_id:
                        pos.close_order_id = order_id
                        pos.place_order_fails = 0
                        pos.close_cancel_requested = False
                        
                        reason: str = action.get("reason", "")
                        logger.info(f"⚡ [{pos_key}] ФИЗИЧЕСКИЙ ВЫСТРЕЛ: {reason} | Объем: {pos.qty} | Цена: {target_price} | Охота: {self.hunting_timeout_sec}с")
                        await self.tb.state.save()
                    else:
                        pos.hunting_active_until = 0.0
                        
                except Exception as e:
                    pos.hunting_active_until = 0.0
                    logger.error(f"[{pos_key}] ❌ Ошибка выстрела ТП: {e}")
                    pos.place_order_fails += 1
                    await self._handle_order_fail(symbol, pos_key, pos)

            # 4. Интерференция (Скупка помех)
            elif act == "BUY_OUT_INTERFERENCE":
                if pos_key in self.tb.state.pending_interference_orders: 
                    return
                
                current_notional: float = pos.qty * pos.entry_price
                notional_limit: float = self.tb.cfg.get("risk", {}).get("notional_limit", 5000)
                available: float = notional_limit - current_notional
                if available <= 0: 
                    return

                interf_price: float = round_step(action['price'], spec.tick_size)
                if interf_price <= 0: 
                    return

                interf_qty: float = round_step(min(action['qty'], available / interf_price), spec.lot_size)
                order_value_usdt: float = interf_qty * interf_price

                if interf_qty <= 0 or order_value_usdt < self.tb.min_exchange_notional: 
                    pos.interference_disabled = True
                    await self.tb.state.save()
                    return

                try:
                    resp = await self.tb.private_client.place_limit_order(
                        symbol=symbol, side=("Buy" if pos.side == "LONG" else "Sell"), qty=interf_qty, price=interf_price, pos_side=pos_side
                    )
                    order_id: str = str(resp.get("data", {}).get("orderID") or resp.get("result", {}).get("orderId", ""))
                    if order_id:
                        self.tb.state.pending_interference_orders[pos_key] = order_id
                        pos.interference_cancel_requested = False
                        pos.place_order_fails = 0 
                        logger.info(f"🛒 [{pos_key}] СКУПКА ПОМЕХИ: Отправлен ордер {interf_qty} шт. по {interf_price}")
                        await self.tb.state.save()
                except Exception as e:
                    if "TE_NO_ENOUGH_AVAILABLE_BALANCE" in str(e):
                        pos.interference_disabled = True
                        logger.warning(f"[{pos_key}] ⛔ Скупка помех отключена: недостаточно баланса.")
                    else:
                        logger.error(f"[{pos_key}] ❌ Ошибка скупки помехи: {e}")
                        pos.failed_interference_prices[str(interf_price)] = pos.failed_interference_prices.get(str(interf_price), 0) + 1
                    
                    pos.place_order_fails += 1
                    await self._handle_order_fail(symbol, pos_key, pos)


    async def execute_entry(self, symbol: str, pos_key: str, signal: Dict[str, Any], depth: DepthTop) -> None:
        """Расчет объема и постановка открывающей лимитной заявки по сигналу."""
        spec = self.tb.symbol_specs.get(symbol)
        if not spec: 
            return

        try:
            price: float = round_step(signal['price'], spec.tick_size)
            if price <= 0: 
                return

            risk_cfg = self.tb.cfg.get("risk", {})
            margin_size_cfg = risk_cfg.get("margin_size", "row")
            leverage_val = float(risk_cfg.get("leverage", {}).get("val", 1.0))

            if str(margin_size_cfg).lower() == "row":
                base_vol_usdt: float = float(signal.get("row_vol_usdt", price * signal.get("row_vol_asset", 0)))
            else:
                base_vol_usdt: float = float(margin_size_cfg) * leverage_val

            target_vol_usdt: float = min(
                base_vol_usdt * (1 + risk_cfg.get("margin_over_size_pct", 1) / 100), 
                risk_cfg.get("notional_limit", 5000)
            )
            qty: float = round_step(target_vol_usdt / price, spec.lot_size)
            if qty <= 0: 
                return

            base_target: float = signal.get("base_target_price_100") or (price * (1 + (signal.get("spr2_pct", 0) / 100)) if signal["side"] == "LONG" else price * (1 - (signal.get("spr2_pct", 0) / 100)))

            ask1, bid1 = depth.asks[0][0], depth.bids[0][0]
            pos = ActivePosition(
                symbol=symbol, side=signal["side"], entry_price=price, qty=0.0, init_qty=qty,
                init_ask1=ask1, init_bid1=bid1, base_target_price_100=base_target,
            )
            self.tb.exit_engine.initialize_position_state(pos)
            self.tb.state.active_positions[pos_key] = pos
            
            # Блокируем стейт от гонок с вебсокетом
            self.tb.state.pending_entry_orders[pos_key] = "PENDING_HTTP" 

            resp = await self.tb.private_client.place_limit_order(
                symbol=symbol, side=("Buy" if signal["side"] == "LONG" else "Sell"), qty=qty, price=price, pos_side=("Long" if signal["side"] == "LONG" else "Short")
            )
            order_id: str = str(resp.get("data", {}).get("orderID") or resp.get("result", {}).get("orderId", ""))

            if order_id:
                if self.tb.state.pending_entry_orders.get(pos_key) == "PENDING_HTTP":
                    self.tb.state.pending_entry_orders[pos_key] = order_id
                await self.tb.state.save()
                semantic: str = "ОТКРЫТЬ ЛОНГ" if signal["side"] == "LONG" else "ОТКРЫТЬ ШОРТ"
                logger.info(f"🚀 [{pos_key}] {semantic} ОТПРАВЛЕН: по {price} (Плановый объем: {qty})")
                if self.tb.tg:
                    b_price, p_price = self.tb.price_manager.get_prices(symbol)
                    asyncio.create_task(self.tb.tg.send_message(
                        Reporters.entry_signal(symbol, signal, b_price, p_price)
                    ))
            else:
                self.tb.state.active_positions.pop(pos_key, None)
                self.tb.state.pending_entry_orders.pop(pos_key, None)
                
        except Exception as e:
            pos_check: ActivePosition | None = self.tb.state.active_positions.get(pos_key)
            if pos_check and pos_check.qty > 0:
                self.tb.state.pending_entry_orders.pop(pos_key, None) 
            else:
                self.tb.state.active_positions.pop(pos_key, None)
                self.tb.state.pending_entry_orders.pop(pos_key, None)
                logger.error(f"[{pos_key}] ❌ Ошибка выставления открывающего ордера: {e}")