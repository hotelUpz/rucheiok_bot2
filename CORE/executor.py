# ============================================================
# FILE: CORE/executor.py
# ROLE: Отправка ордеров на биржу и защита от гонок потоков (Locks)
# ============================================================

import asyncio
from typing import Dict
from c_log import UnifiedLogger
from CORE.models import ActivePosition
from API.PHEMEX.stakan import DepthTop
from utils import round_step

logger = UnifiedLogger("core")

class OrderExecutor:
    def __init__(self, tb):
        self.tb = tb
        self.locks: Dict[str, asyncio.Lock] = {}

    def get_lock(self, pos_key: str) -> asyncio.Lock:
        if pos_key not in self.locks:
            self.locks[pos_key] = asyncio.Lock()
        return self.locks[pos_key]

    async def _handle_order_fail(self, symbol: str, pos_key: str, pos: ActivePosition):
        max_fails = self.tb.cfg.get("app", {}).get("max_place_order_retries", 5)
        semantic = "ЛОНГ" if pos.side == "LONG" else "ШОРТ"
        
        if pos.place_order_fails >= max_fails:
            if pos.qty > 0:
                if not pos.in_extrime_mode:
                    logger.error(f"[{pos_key}] ⚠️ Лимит ошибок исчерпан! Перевод в EXTRIME MODE.")
                    pos.in_extrime_mode = True
                    pos.in_breakeven_mode = False
                    pos.place_order_fails = 0 
                    if self.tb.tg:
                        asyncio.create_task(self.tb.tg.send_message(f"🆘 <b>Авария Ордеров</b>\n#{symbol} ({semantic}): Ошибки API. Включен Экстрим."))
                else:
                    logger.error(f"[{pos_key}] 🚨 КРИТИЧЕСКИЙ ОТКАЗ! Экстрим-ордера отклоняются биржей. Требуется ручное вмешательство! Позиция ({pos.qty} шт) СОХРАНЕНА.")
                    if self.tb.tg:
                        asyncio.create_task(self.tb.tg.send_message(f"🚨 <b>КРИТИЧЕСКАЯ АВАРИЯ</b>\n#{symbol} ({semantic}): Экстрим-ордера отклоняются! Проверьте баланс!"))
            else:
                logger.error(f"[{pos_key}] 🗑 Ошибки API до открытия позиции (объем 0). Сбрасываем стейт.")
                self.tb.state.active_positions.pop(pos_key, None)
                
        await self.tb.state.save()

    async def restore_tp(self, symbol: str, pos_key: str, pos: ActivePosition):
        async with self.get_lock(pos_key):
            if pos.close_order_id: return
            spec = self.tb.symbol_specs.get(symbol)
            if not spec or pos.current_close_price <= 0 or pos.qty <= 0: return

            avg_enabled = self.tb.cfg.get("exit", {}).get("scenarious", {}).get("average", {}).get("enable", True)
            if avg_enabled and not pos.in_extrime_mode and not pos.in_breakeven_mode:
                return

            close_side = "Sell" if pos.side == "LONG" else "Buy"
            pos_side = "Long" if pos.side == "LONG" else "Short"

            try:
                resp = await self.tb.private_client.place_limit_order(
                    symbol=symbol, side=close_side, qty=pos.qty, price=pos.current_close_price, pos_side=pos_side
                )
                order_id = str(resp.get("data", {}).get("orderID") or resp.get("result", {}).get("orderId", ""))
                if order_id:
                    pos.close_order_id = order_id
                    pos.place_order_fails = 0
                    pos.close_cancel_requested = False
                    await self.tb.state.save()
                    logger.warning(f"[{pos_key}] 🛡 ТП восстановлен Watchdog-ом на {pos.current_close_price}!")
            except Exception as e:
                logger.error(f"[{pos_key}] Ошибка Watchdog: {e}")
                pos.place_order_fails += 1
                await self._handle_order_fail(symbol, pos_key, pos)

    async def handle_exit_action(self, symbol: str, pos_key: str, action: dict):
        async with self.get_lock(pos_key):
            pos = self.tb.state.active_positions.get(pos_key)
            spec = self.tb.symbol_specs.get(symbol)
            if not pos or not spec or pos.qty <= 0: return

            act = action["action"]
            close_side = "Sell" if pos.side == "LONG" else "Buy"
            pos_side = "Long" if pos.side == "LONG" else "Short"
            semantic_action = "ЗАКРЫТЬ ЛОНГ" if pos.side == "LONG" else "ЗАКРЫТЬ ШОРТ"

            if act == "CANCEL_CLOSE":
                logger.info(f"🧹 [{pos_key}] Отмена зависшего динамического ордера: {action.get('reason', '')}")
                return

            if act in ("UPDATE_TARGET", "PLACE_EXTRIME_LIMIT", "PLACE_DYNAMIC_CLOSE"):
                if act == "UPDATE_TARGET":
                    if "price" in action: target_price = action["price"]
                    elif pos.side == "LONG": target_price = pos.entry_price + (pos.base_target_price_100 - pos.entry_price) * action["new_rate"]
                    else: target_price = pos.entry_price - (pos.entry_price - pos.base_target_price_100) * action["new_rate"]
                else: target_price = action["price"]

                target_price = round_step(target_price, spec.tick_size)
                if target_price <= 0: return

                if pos.close_order_id and pos.current_close_price > 0 and abs(pos.current_close_price - target_price) < max(spec.tick_size, 1e-12):
                    return
                
                pos.current_close_price = target_price

                try:
                    resp = await self.tb.private_client.place_limit_order(
                        symbol=symbol, side=close_side, qty=pos.qty, price=target_price, pos_side=pos_side
                    )
                    pos.close_order_id = str(resp.get("data", {}).get("orderID") or resp.get("result", {}).get("orderId", ""))
                    pos.place_order_fails = 0
                    pos.close_cancel_requested = False

                    reason = action.get("reason", "")
                    log_msg = f"[{pos_key}] ACTION: {reason} | Qty: {pos.qty} | New Target: {target_price:.6f}"

                    if reason == "INITIAL_TP": logger.info(f"🎯 {semantic_action} (ПЕРВИЧНАЯ ЦЕЛЬ): {log_msg}")
                    elif reason == "SHIFT_DEMOTION": logger.info(f"📉 {semantic_action} (СДВИГ ЦЕЛИ): {log_msg}")
                    elif reason == "DYNAMIC_TP_HIT": logger.info(f"⚡ {semantic_action} (ДИНАМИЧЕСКИЙ TAKER): {log_msg}")
                    elif reason == "TTL_BREAKEVEN": logger.warning(f"⌛ {semantic_action} (БЕЗУБЫТОК): {log_msg}")
                    else: logger.warning(f"🆘 {semantic_action} (EXTRIME): {log_msg}")

                    await self.tb.state.save()
                except Exception as e:
                    logger.error(f"[{pos_key}] ❌ Ошибка выставления ТП: {e}")
                    pos.place_order_fails += 1
                    await self._handle_order_fail(symbol, pos_key, pos)

            elif act == "BUY_OUT_INTERFERENCE":
                if pos_key in self.tb.state.pending_interference_orders: return
                current_notional = pos.qty * pos.entry_price
                notional_limit = self.tb.cfg.get("risk", {}).get("notional_limit", 5000)
                available = notional_limit - current_notional
                if available <= 0: return

                interf_side = "Buy" if pos.side == "LONG" else "Sell"
                interf_price = round_step(action['price'], spec.tick_size)
                if interf_price <= 0: return

                max_qty = available / interf_price
                raw_qty = min(action['qty'], max_qty)
                interf_qty = round_step(raw_qty, spec.lot_size)
                order_value_usdt = interf_qty * interf_price

                # [ФИНАЛЬНЫЙ ФИКС]: Если экзекьютор порезал объем до неторгуемого лимита - отключаем интерференцию навсегда!
                if interf_qty <= 0 or order_value_usdt < 5.0: 
                    pos.interference_disabled = True
                    await self.tb.state.save()
                    return

                try:
                    resp = await self.tb.private_client.place_limit_order(
                        symbol=symbol, side=interf_side, qty=interf_qty, price=interf_price, pos_side=pos_side
                    )
                    order_id = str(resp.get("data", {}).get("orderID") or resp.get("result", {}).get("orderId", ""))
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

    async def execute_entry(self, symbol: str, pos_key: str, signal: dict, depth: DepthTop):
        spec = self.tb.symbol_specs.get(symbol)
        if not spec: return

        try:
            if symbol not in self.tb.state.leverage_configured:
                leverage_cfg = self.tb.cfg.get("risk", {}).get("leverage")
                if leverage_cfg is not None:
                    leverage_val = spec.max_leverage if str(leverage_cfg).lower() == "max" else float(leverage_cfg)
                    try:
                        await self.tb.private_client.set_leverage(symbol, "Merged", leverage_val, mode="hedged")
                        logger.debug(f"[{symbol}] Плечо: {leverage_val}x")
                    except Exception: pass
                self.tb.state.leverage_configured.add(symbol)

            price = round_step(signal['price'], spec.tick_size)
            if price <= 0: return

            margin_size_cfg = self.tb.cfg.get("risk", {}).get("margin_size", "row")
            if str(margin_size_cfg).lower() == "row": base_vol_usdt = signal.get("row_vol_usdt", price * signal.get("row_vol_asset", 0))
            else: base_vol_usdt = float(margin_size_cfg)

            margin_over_size_pct = self.tb.cfg.get("risk", {}).get("margin_over_size_pct", 1)
            target_vol_usdt = base_vol_usdt * (1 + margin_over_size_pct / 100)
            notional_limit = self.tb.cfg.get("risk", {}).get("notional_limit", 5000)
            if target_vol_usdt > notional_limit: target_vol_usdt = notional_limit

            qty = round_step(target_vol_usdt / price, spec.lot_size)
            if qty <= 0: return

            base_target = signal.get("base_target_price_100")
            if not base_target: 
                spr2_pct = signal.get("spr2_pct", 0)
                base_target = price * (1 + (spr2_pct / 100)) if signal["side"] == "LONG" else price * (1 - (spr2_pct / 100))

            pos_side = "Long" if signal["side"] == "LONG" else "Short"
            side = "Buy" if signal["side"] == "LONG" else "Sell"
            semantic_action = "ОТКРЫТЬ ЛОНГ" if signal["side"] == "LONG" else "ОТКРЫТЬ ШОРТ"

            ask1, bid1 = depth.asks[0][0], depth.bids[0][0]
            pos = ActivePosition(
                symbol=symbol, side=signal["side"], entry_price=price, qty=0.0, init_qty=qty,
                init_ask1=ask1, init_bid1=bid1, base_target_price_100=base_target,
            )
            self.tb.exit_engine.initialize_position_state(pos)
            self.tb.state.active_positions[pos_key] = pos
            self.tb.state.pending_entry_orders[pos_key] = "PENDING_HTTP" 

            resp = await self.tb.private_client.place_limit_order(symbol=symbol, side=side, qty=qty, price=price, pos_side=pos_side)
            order_id = str(resp.get("data", {}).get("orderID") or resp.get("result", {}).get("orderId", ""))

            if order_id:
                # [СКАЛЬПЕЛЬ 1]: Защита от Зомби. Вписываем ID только если вебсокет еще не завершил цикл!
                if self.tb.state.pending_entry_orders.get(pos_key) == "PENDING_HTTP":
                    self.tb.state.pending_entry_orders[pos_key] = order_id
                    
                await self.tb.state.save()
                logger.info(f"🚀 [{pos_key}] {semantic_action} ОТПРАВЛЕН: по {price} (Плановый объем: {qty})")
                if self.tb.tg:
                    await self.tb.tg.send_message(f"🟢 <b>Сигнал на вход ({semantic_action})</b>\nМонета: #{symbol}\nЦена: {price}\nОбъем: {qty}")
            else:
                self.tb.state.active_positions.pop(pos_key, None)
                self.tb.state.pending_entry_orders.pop(pos_key, None)
                
        except Exception as e:
            # [ГАЛОПЕРИДОЛ 2]: Спасение орфанных позиций при таймаутах сети
            pos_check = self.tb.state.active_positions.get(pos_key)
            if pos_check and pos_check.qty > 0:
                logger.warning(f"[{pos_key}] Ошибка HTTP '{e}', НО вебсокет уже налил {pos_check.qty}! Оставляем позу.")
                self.tb.state.pending_entry_orders.pop(pos_key, None) 
            else:
                self.tb.state.active_positions.pop(pos_key, None)
                self.tb.state.pending_entry_orders.pop(pos_key, None)
                logger.error(f"[{pos_key}] ❌ Критическая ошибка входа: {e}")