# ============================================================
# FILE: EXIT/engine.py
# ROLE: Анализатор сценариев выхода из позиции (Единый Пайплайн)
# ============================================================
from __future__ import annotations

import asyncio
import time
from typing import Dict, Any, Optional, TYPE_CHECKING

from EXIT.scenarios.average import AverageScenario
from EXIT.scenarios.negative import NegativeScenario
from EXIT.position_ttl_close import PositionTTLClose
from EXIT.extrime_close import ExtrimeClose
from EXIT.interference import Interference
from c_log import UnifiedLogger

if TYPE_CHECKING:
    from CORE.bot import TradingBot
    from CORE.models import ActivePosition
    from API.PHEMEX.stakan import DepthTop

logger = UnifiedLogger("exit_engine")

class ExitEngine:
    def __init__(self, exit_cfg: Dict[str, Any], tb: "TradingBot"):
        self.cfg = exit_cfg
        self.tb = tb
        scenarios_cfg = self.cfg.get("scenarios", {})

        self.average = AverageScenario(scenarios_cfg.get("average", {}))
        self.negative = NegativeScenario(scenarios_cfg.get("negative", {}))
        self.ttl_close = PositionTTLClose(self.cfg.get("position_ttl_close", {}))
        self.extrime_close = ExtrimeClose(self.cfg.get("extrime_close", {}))
        self.interference = Interference(self.cfg.get("interference", {}), self.tb.min_exchange_notional)

    def initialize_position_state(self, pos: ActivePosition):
        pos.current_target_rate = self.average.cfg.get("target_rate", 0.7)
        pos.is_average_stabilized = False
        pos.last_shift_ts = 0.0
        pos.last_negative_check_ts = time.time()
        pos.negative_duration_sec = 0.0

        pos.in_breakeven_mode = False
        pos.breakeven_start_ts = 0.0
        pos.in_extrime_mode = False
        pos.extrime_retries_count = 0
        pos.current_close_price = 0.0

    def evaluate_pipeline(self, depth: DepthTop, pos: ActivePosition) -> Optional[Dict[str, Any]]:
        """
        Единый пайплайн оценки выхода. Приоритет убывает сверху вниз.

        Порядок проверок (от наивысшего приоритета к низшему):
          [P1] Extrime     — если уже активирован, ни о чём другом речи нет
          [P2] TTL         — глобальный таймаут → БУ → Extrime
          [P3] Negative    — затяжной убыток → Extrime
          [P4] Interference — скупка мелких помех (перебивает Average)
          [P5] Average      — основной охотник TP (дефолтный сценарий)

        Возвращает СТРОГО детерминированный ActionDict или None.
        """
        now = time.time()

        if not depth.asks or not depth.bids:
            return None

        # [P1] Extrime-режим — если уже активирован, только долбим стакан
        if pos.in_extrime_mode:
            return self.extrime_close.analyze(depth, pos, now)

        # [P2] Глобальный TTL
        ttl_action = self.ttl_close.analyze(pos, now)
        if ttl_action:
            if ttl_action["action"] == "TRIGGER_BREAKEVEN":
                pos.in_breakeven_mode = True
                pos.breakeven_start_ts = now
                pos.current_target_rate = 0.0
                target_price = ttl_action.get("price") or self.ttl_close.build_target_price(pos)
                lifetime = round(now - pos.opened_at, 1)
                logger.info(f"⌛ [EXIT] #{pos.symbol} TTL истек ({lifetime}с). Цель переведена в БУ: {target_price}")
                pos_key = f"{pos.symbol}_{pos.side}"
                if self.tb.tg:
                    asyncio.create_task(self.tb.tg.send_message(
                        f"⌛ <b>БУ РЕЖИМ</b>\n#{pos_key}\nВремя в сделке: {lifetime}с. Цель переведена в БУ: {target_price}"
                    ))
                return {"action": "UPDATE_TARGET", "price": target_price, "reason": "TTL_BREAKEVEN"}

            elif ttl_action["action"] == "TRIGGER_EXTRIME":
                pos.in_breakeven_mode = False
                self._transition_to_extrime(pos, "БУ-лимитка по TTL не сработала")
                return self.extrime_close.analyze(depth, pos, now)

        # [P3] Негативный сценарий (затяжной убыток)
        neg_action = self.negative.analyze(depth, pos, now)
        if neg_action and neg_action.get("action") == "TRIGGER_EXTRIME":
            self._transition_to_extrime(pos, "Затяжной негативный PnL")
            return self.extrime_close.analyze(depth, pos, now)

        # [P4] Скупка помех (Интерференция) — перебивает Average
        interf_action = self.interference.analyze(depth, pos, now)
        if interf_action and interf_action.get("action") == "BUY_OUT_INTERFERENCE":
            return interf_action

        # [P5] Основной сценарий (Average) — дефолтный охотник
        if self.average.enable:
            avg_action = self.average.analyze(depth, pos, now)
            if avg_action:
                act = avg_action.get("action")
                if act == "TRIGGER_EXTRIME":
                    # Не бросаем сразу в extrime — сначала пробуем breakeven по цене входа
                    # (аналогично TTL P2). TTL (P2) подхватит breakeven_start_ts и через
                    # breakeven_wait_sec сам переведёт в extrime.
                    if not pos.in_breakeven_mode:
                        pos.in_breakeven_mode = True
                        pos.breakeven_start_ts = now
                        pos.current_target_rate = 0.0
                        target_price = self.ttl_close.build_target_price(pos)
                        lifetime = round(now - pos.opened_at, 1)
                        pos_key = f"{pos.symbol}_{pos.side}"
                        logger.info(f"⌛ [EXIT] #{pos_key} Average исчерпал лимиты ({lifetime}с). Цель → БУ: {target_price}")
                        if self.tb.tg:
                            asyncio.create_task(self.tb.tg.send_message(
                                f"⌛ <b>БУ РЕЖИМ (Average)</b>\n#{pos_key}\nВремя: {lifetime}с. Цель → БУ: {target_price}"
                            ))
                        return {"action": "UPDATE_TARGET", "price": target_price, "reason": "AVERAGE_BREAKEVEN"}
                    # Уже в breakeven — TTL (P2) подхватит переход в extrime
                    return None

                if act in ("CANCEL_CLOSE", "PLACE_DYNAMIC_CLOSE", "UPDATE_TARGET"):
                    return avg_action

        return None

    def _transition_to_extrime(self, pos: ActivePosition, reason: str):
        """
        Штатный переход в экстрим-режим.
        ИНВАРИАНТ: переход идемпотентен — повторный вызов игнорируется.
        TG-уведомление отправляется ОДИН РАЗ при первом входе.
        """
        if not pos.in_extrime_mode:
            pos.in_extrime_mode = True
            pos.hunting_active_until = 0.0
            pos.extrime_retries_count = 0
            # ИНВАРИАНТ: сбрасываем current_close_price, чтобы первый extrime-выстрел
            # не блокировался idempotency-проверкой executor-а.
            # UPDATE_TARGET мог записать breakeven_price, а extrime вычисляет mid ≈ breakeven →
            # abs(breakeven - mid) < tick_size → executor возвращался без выстрела.
            pos.current_close_price = 0.0
            pos_key = f"{pos.symbol}_{pos.side}"
            logger.info(f"🔄 [EXIT] #{pos_key} Переход в EXTRIME CLOSE. Причина: {reason}")
            if self.tb.tg:
                asyncio.create_task(self.tb.tg.send_message(
                    f"⚠️ <b>EXTRIME CLOSE</b>\n#{pos_key}\nПричина: {reason}"
                ))