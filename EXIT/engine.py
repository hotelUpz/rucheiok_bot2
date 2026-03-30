import asyncio
import time
from typing import Dict, Any, Optional

from CORE.models import ActivePosition
from API.PHEMEX.stakan import DepthTop
from c_log import UnifiedLogger

from EXIT.scenarios.average import AverageScenario
from EXIT.scenarios.negative import NegativeScenario
from EXIT.position_ttl_close import PositionTTLClose
from EXIT.extrime_close import ExtrimeClose
from EXIT.interference import Interference

logger = UnifiedLogger("exit")


class ExitEngine:
    def __init__(self, exit_cfg: Dict[str, Any], tb):
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

    def analyze(self, depth: DepthTop, pos: ActivePosition) -> Optional[Dict[str, Any]]:
        now = time.time()

        ask1 = depth.asks[0][0] if depth.asks else 0.0
        bid1 = depth.bids[0][0] if depth.bids else 0.0
        if not ask1 or not bid1:
            return None

        if pos.in_extrime_mode:
            return self.extrime_close.analyze(depth, pos, now)

        ttl_action = self.ttl_close.analyze(pos, now)
        if ttl_action:
            if ttl_action["action"] == "TRIGGER_BREAKEVEN":
                pos.in_breakeven_mode = True
                pos.breakeven_start_ts = now
                pos.current_target_rate = 0.0

                # Используем жесткую цену из метода, написанного строго по ТЗ
                target_price = ttl_action.get("price")
                if not target_price or target_price <= 0:
                    target_price = self.ttl_close.build_target_price(pos)

                logger.warning(f"[EXIT] TTL позиции истек. Перевод цели в TTL-target.")
                if self.tb.tg:
                    asyncio.create_task(
                        self.tb.tg.send_message(
                            f"⌛ <b>БУ РЕЖИМ</b>\n#{pos.symbol}: Истек TTL, цель переведена в TTL-target."
                        )
                    )

                return {"action": "UPDATE_TARGET", "price": target_price, "reason": "TTL_BREAKEVEN"}

            if ttl_action["action"] == "TRIGGER_EXTRIME":
                pos.in_breakeven_mode = False
                pos.in_extrime_mode = True
                logger.warning("[EXIT] БУ лимитка не исполнилась. Запуск Extrime Mode.")
                if self.tb.tg:
                    asyncio.create_task(self.tb.tg.send_message(f"🆘 <b>EXTRIME MODE!</b>\n#{pos.symbol}: Лимитка БУ не сработала!"))
                return self.extrime_close.analyze(depth, pos, now)

        neg_action = self.negative.analyze(depth, pos, now)
        if neg_action and neg_action["action"] == "TRIGGER_EXTRIME":
            pos.in_extrime_mode = True
            logger.warning(f"[EXIT] Отработал негативный сценарий. Запуск Extrime Mode.")
            if self.tb.tg:
                asyncio.create_task(self.tb.tg.send_message(f"⚠️ <b>ВНИМАНИЕ!</b>\nСработал НЕГАТИВНЫЙ сценарий по #{pos.symbol}."))
            return self.extrime_close.analyze(depth, pos, now)

        # СТРОГО ПО ТЗ: Уважаем флаг average.enable
        if self.average.enable:
            avg_action = self.average.analyze(pos, now)
            if avg_action:
                return avg_action

        interf_action = self.interference.analyze(depth, pos)
        if interf_action:
            return interf_action

        return None
