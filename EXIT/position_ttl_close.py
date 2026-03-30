from CORE.models import ActivePosition

class PositionTTLClose:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.enable = cfg.get("enable", True)
        self.position_ttl = cfg.get("position_ttl", 600)
        self.breakeven_wait_sec = cfg.get("breakeven_wait_sec", 2.0)
        self.to_entry_orientation = float(cfg.get("to_entry_orientation", 0.0))

    def build_target_price(self, pos: ActivePosition) -> float:
        orient_pct = self.to_entry_orientation
        if pos.side == "LONG":
            return pos.entry_price * (1 + orient_pct / 100.0)
        return pos.entry_price * (1 - orient_pct / 100.0)

    def analyze(self, pos: ActivePosition, now: float) -> dict | None:
        if not self.enable:
            return None

        if now - pos.opened_at >= self.position_ttl:
            if not pos.in_breakeven_mode:
                return {
                    "action": "TRIGGER_BREAKEVEN",
                    "reason": "TTL_EXPIRED",
                    "price": self.build_target_price(pos),
                }
            if now - pos.breakeven_start_ts >= self.breakeven_wait_sec:
                return {"action": "TRIGGER_EXTRIME", "reason": "BREAKEVEN_TIMEOUT"}
        return None
