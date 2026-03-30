import time
from CORE.models import ActivePosition

class AverageScenario:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.enable = cfg.get("enable", True)
        self.stab_avg = cfg.get("stabilization_ttl", 2)
        self.shift_ttl = cfg.get("shift_ttl", 60)
        self.shift_demotion = cfg.get("shift_demotion", 0.2)
        self.min_target_rate = cfg.get("min_target_rate", 0.3)

    def analyze(self, pos: ActivePosition, now: float) -> dict | None:
        if not self.enable:
            return None

        time_in_pos = now - pos.opened_at

        # 1. Первичная постановка цели (Ждем стабилизации)
        if not pos.is_average_stabilized:
            if time_in_pos >= self.stab_avg:
                pos.is_average_stabilized = True
                pos.last_shift_ts = now # Таймер сдвигов стартует только сейчас
                return {"action": "UPDATE_TARGET", "new_rate": pos.current_target_rate, "reason": "INITIAL_TP"}
        
        # 2. Логика сдвига (Уже стабилизированы)
        else:
            if self.enable and (now - pos.last_shift_ts >= self.shift_ttl):
                new_rate = pos.current_target_rate - self.shift_demotion
                if pos.current_target_rate > self.min_target_rate:
                    pos.current_target_rate = max(new_rate, self.min_target_rate)
                    pos.last_shift_ts = now
                    pos.negative_duration_sec = 0.0 # Сброс негативного таймера по ТЗ
                    return {"action": "UPDATE_TARGET", "new_rate": pos.current_target_rate, "reason": "SHIFT_DEMOTION"}
        return None