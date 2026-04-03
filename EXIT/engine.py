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
from CORE.bot_utils import Reporters
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
        scenarios_cfg = self.cfg.get("scenarious", {})

        self.average = AverageScenario(scenarios_cfg.get("average", {}))
        self.negative = NegativeScenario(scenarios_cfg.get("negative", {}))
        self.ttl_close = PositionTTLClose(self.cfg.get("position_ttl_close", {}))
        self.extrime_close = ExtrimeClose(self.cfg.get("extrime_close", {}))
        self.interference = Interference(self.cfg.get("interference", {}))

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
        Единый пайплайн оценки выхода.
        Возвращает СТРОГО ActionDict для CORE/executor.py
        """
        now = time.time()

        if not depth.asks or not depth.bids:
            return None

        # 1. Если уже в экстриме — другие сценарии мертвы, долбим стакан
        if pos.in_extrime_mode:
            return self.extrime_close.analyze(depth, pos, now)

        # 2. Проверка глобального TTL (Время жизни)
        ttl_action = self.ttl_close.analyze(pos, now)
        if ttl_action:
            if ttl_action["action"] == "TRIGGER_BREAKEVEN":
                pos.in_breakeven_mode = True
                pos.breakeven_start_ts = now
                pos.current_target_rate = 0.0
                target_price = ttl_action.get("price") or self.ttl_close.build_target_price(pos)

                logger.warning(f"⌛ [EXIT] #{pos.symbol} TTL позиции истек. Перевод цели в безубыток.")
                if self.tb.tg:
                    asyncio.create_task(self.tb.tg.send_message(
                        f"⌛ <b>БУ РЕЖИМ</b>\nМонета: #{pos.symbol}\nИстек TTL, цель переведена в безубыток."
                    ))
                return {"action": "UPDATE_TARGET", "price": target_price, "reason": "TTL_BREAKEVEN"}

            elif ttl_action["action"] == "TRIGGER_EXTRIME":
                pos.in_breakeven_mode = False
                self._activate_extrime_mode(pos, "БУ-лимитка по TTL не сработала")
                return self.extrime_close.analyze(depth, pos, now)

        # 3. Сценарий Average (Поиск объемов и плавный сдвиг цели)
        avg_action = None
        if self.average.enable:
            avg_action = self.average.analyze(depth, pos, now)
            if avg_action:
                # Внутренний сигнал от average, что двигаться некуда -> уходим в экстрим
                if avg_action.get("action") == "TRIGGER_EXTRIME":
                    self._activate_extrime_mode(pos, "Average исчерпал лимиты уступок")
                    return self.extrime_close.analyze(depth, pos, now)
                
                # Если требуется сброс старого ордера для переоценки
                if avg_action.get("action") == "CANCEL_CLOSE":
                    return avg_action

        # 4. Негативный сценарий (топтание в минусе)
        neg_action = self.negative.analyze(depth, pos, now)
        if neg_action and neg_action.get("action") == "TRIGGER_EXTRIME":
            self._activate_extrime_mode(pos, "Затяжной негативный PnL")
            return self.extrime_close.analyze(depth, pos, now)

        # 5. Скупка помех (Интерференция) - перебивает обычный хантинг
        interf_action = self.interference.analyze(depth, pos, now)
        if interf_action:
            return interf_action

        # 6. Возврат результата Average (Выстрел по стакану PLACE_DYNAMIC_CLOSE или UPDATE_TARGET)
        if avg_action:
            return avg_action

        return None

    def _activate_extrime_mode(self, pos: ActivePosition, reason: str):
        """Хелпер для жесткого перевода в Экстрим-режим с нотификацией."""
        if not pos.in_extrime_mode:
            pos.in_extrime_mode = True
            pos.hunting_active_until = 0.0 # Сбрасываем заморозки
            pos.extrime_retries_count = 0
            
            logger.error(f"🚨 [EXIT] #{pos.symbol} Активация EXTRIME MODE! Причина: {reason}")
            if self.tb.tg:
                pos_key = f"{pos.symbol}_{pos.side}"
                asyncio.create_task(self.tb.tg.send_message(Reporters.extreme_close(pos.symbol, pos_key, reason)))