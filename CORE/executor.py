# ============================================================
# FILE: CORE/executor.py
# ROLE: Отправка ордеров на биржу и защита от гонок потоков (Locks)
# ============================================================
from __future__ import annotations

import asyncio
import time
from typing import Dict, Any, TYPE_CHECKING
from c_log import UnifiedLogger
from CORE.models import ActivePosition
from utils import round_step

if TYPE_CHECKING:
    from CORE.bot import TradingBot
    from API.PHEMEX.stakan import DepthTop

logger = UnifiedLogger("core")

class OrderExecutor:
    """Асинхронный отправитель торговых приказов на биржу. Оперирует локами для защиты от гонок потоков."""
    
    def __init__(self, tb: 'TradingBot') -> None:
        self.tb = tb
        self.locks: Dict[str, asyncio.Lock] = {}

    def get_lock(self, pos_key: str) -> asyncio.Lock:
        if pos_key not in self.locks:
            self.locks[pos_key] = asyncio.Lock()
        return self.locks[pos_key]

    async def _handle_order_fail(self, symbol: str, pos_key: str, pos: ActivePosition) -> None:
        """Централизованный диспетчер аварий API."""
        max_fails: int = self.tb.cfg.get("app", {}).get("max_place_order_retries", 5)
        
        if pos.place_order_fails >= max_fails:
            if pos.qty > 0:
                if not pos.in_extrime_mode:
                    logger.error(f"[{pos_key}] ⚠️ Лимит ошибок исчерпан! Перевод в EXTRIME MODE.")
                    pos.in_extrime_mode = True
                    pos.place_order_fails = 0 
                else:
                    logger.error(f"[{pos_key}] 🚨 КРИТИЧЕСКИЙ ОТКАЗ! Экстрим-ордера отклоняются биржей. Требуется ручное вмешательство! Позиция ({pos.qty} шт) СОХРАНЕНА.")
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

            # 1. Отмена физического ордера (запрос от Снайпера для переоценки стакана)
            if act == "CANCEL_CLOSE":
                if pos.close_order_id:
                    try: 
                        await self.tb.private_client.cancel_order(symbol, pos.close_order_id, pos_side)
                    except Exception: 
                        pass
                    pos.close_order_id = None
                return

            # 2. Обновление виртуальной цели (Без постановки ордеров!)
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

                # Если ордер уже висит
                if pos.close_order_id:
                    # Если цена та же самая - игнорируем (защита от спама)
                    if pos.current_close_price > 0 and abs(pos.current_close_price - target_price) < max(spec.tick_size, 1e-12):
                        return
                    # Если цена ИЗМЕНИЛАСЬ - СНОСИМ СТАРЫЙ ОРДЕР ПЕРЕД ПОСТАНОВКОЙ НОВОГО!
                    else:
                        try:
                            await self.tb.private_client.cancel_order(symbol, pos.close_order_id, pos_side)
                        except Exception:
                            pass
                        pos.close_order_id = None

                pos.current_close_price = target_price
                
                # ⏱ ВЗВОД ТАЙМЕРА ОХОТЫ
                # Устанавливаем флаг "Охоты" (заморозки пересмотра цели) ДО сетевого запроса, 
                # чтобы WS-поток не обогнал REST-ответ.
                hunting_timeout: float = float(self.tb.cfg.get("exit", {}).get("hunting_timeout_sec", 1.0))
                pos.hunting_active_until = time.time() + hunting_timeout

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
                        logger.info(f"⚡ [{pos_key}] ФИЗИЧЕСКИЙ ВЫСТРЕЛ: {reason} | Объем: {pos.qty} | Цена: {target_price} | Охота: {hunting_timeout}с")
                        await self.tb.state.save()
                    else:
                        # Тихий отказ (не вернулся ID) - снимаем блок
                        pos.hunting_active_until = 0.0
                        
                except Exception as e:
                    # При сетевом сбое снимаем блокировку охоты, чтобы логика пошла на новый круг
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

                # Блокировка интерференции при исчерпании бюджета (предотвращение тихого цикла)
                if interf_qty <= 0 or order_value_usdt < 5.0: 
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

            margin_size_cfg = self.tb.cfg.get("risk", {}).get("margin_size", "row")
            base_vol_usdt: float = signal.get("row_vol_usdt", price * signal.get("row_vol_asset", 0)) if str(margin_size_cfg).lower() == "row" else float(margin_size_cfg)

            target_vol_usdt: float = min(
                base_vol_usdt * (1 + self.tb.cfg.get("risk", {}).get("margin_over_size_pct", 1) / 100), 
                self.tb.cfg.get("risk", {}).get("notional_limit", 5000)
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
            
            # Резервируем стейт от обгона REST'а вебсокетом
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