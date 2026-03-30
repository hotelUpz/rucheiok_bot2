from CORE.models import ActivePosition
from API.PHEMEX.stakan import DepthTop

class Interference:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.enable = cfg.get("enable", True)
        self.avg_vol_pct = cfg.get("average_vol_pct_to_init_size", 10) / 100
        self.max_vol_pct = cfg.get("max_vol_pct_to_init_size", 100) / 100
        
        # Лимит попыток скупки на одном уровне цены
        self.max_retries_per_level = cfg.get("max_retries_per_level", 2)

    def analyze(self, depth: DepthTop, pos: ActivePosition) -> dict | None:
        if not self.enable or pos.interference_disabled:
            return None
        if pos.qty <= 0 or pos.current_close_price <= 0:
            return None
            
        allowed_remains = (pos.init_qty * self.max_vol_pct) - pos.interf_bought_qty
        if allowed_remains <= 0:
            pos.interference_disabled = True
            return None

        threshold_vol = pos.init_qty * self.avg_vol_pct

        target = None
        if pos.side == "LONG":
            for price, vol in depth.asks:
                if price <= pos.entry_price or price >= pos.current_close_price:
                    continue
                    
                # [ГАЛОПЕРИДОЛ 1]: Законы физики стакана. Если преграда большая - мы НЕ МОЖЕМ её перепрыгнуть.
                # Скупка невозможна, пока эта плита не исчезнет. Прерываем скан!
                if vol > threshold_vol:
                    break 
                    
                if pos.failed_interference_prices.get(str(price), 0) >= self.max_retries_per_level:
                    break # Долбежка в один уровень запрещена
                    
                target = (price, vol)
                break
        else:
            for price, vol in depth.bids:
                if price >= pos.entry_price or price <= pos.current_close_price:
                    continue
                    
                # [ГАЛОПЕРИДОЛ 1]: Законы физики стакана.
                if vol > threshold_vol:
                    break
                    
                if pos.failed_interference_prices.get(str(price), 0) >= self.max_retries_per_level:
                    break
                    
                target = (price, vol)
                break

        if target:
            price, target_vol = target
            buy_qty = min(pos.init_qty * self.avg_vol_pct, allowed_remains)
            
            approx_value_usdt = buy_qty * price
            if approx_value_usdt < 5.0:
                pos.interference_disabled = True
                return None
                
            return {"action": "BUY_OUT_INTERFERENCE", "price": price, "qty": buy_qty}

        return None