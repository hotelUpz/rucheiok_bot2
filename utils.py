# ============================================================
# FILE: utils.py
# ============================================================
import os
import json
from typing import Any
from decimal import Decimal, ROUND_HALF_DOWN

def round_step(value: float, step: float) -> float:
    if not step or step <= 0:
        return value
    val_d = Decimal(str(value))
    step_d = Decimal(str(step))
    quantized = (val_d / step_d).quantize(Decimal('1'), rounding=ROUND_HALF_DOWN) * step_d
    return float(quantized)

def float_to_str(value: float) -> str:
    return f"{Decimal(str(value)):f}"

def deep_update(d: dict, u: dict) -> dict:
    for k, v in u.items():
        if isinstance(v, dict) and k in d and isinstance(d[k], dict):
            deep_update(d[k], v)
        else:
            d[k] = v
    return d

def get_config_summary(cfg: dict) -> str:
    lines = []
    
    app = cfg.get("app", {})
    risk = cfg.get("risk", {})
    
    lines.append("🎛 <b>[APP & RISK]</b>")
    lines.append(f"Name: <b>{app.get('name', 'N/A')}</b> | Quota: <b>{cfg.get('quota_asset', 'N/A')}</b>")
    lines.append(f"Max Positions: <b>{app.get('max_active_positions', 'N/A')}</b>")
    
    lev_cfg = risk.get('leverage', {})
    lev_val = lev_cfg.get('val', 'N/A') if isinstance(lev_cfg, dict) else lev_cfg
    margin_mod = lev_cfg.get('margin_mode', 'N/A') if isinstance(lev_cfg, dict) else 'N/A'
    
    lines.append(f"Leverage: <b>{lev_val}x</b> | Margin Mode: <b>{margin_mod}</b>")
    lines.append(f"Margin Size: <b>{risk.get('margin_size', 'N/A')}</b> | Notional Limit: <b>{risk.get('notional_limit', 'N/A')} USDT</b>")
    lines.append(f"Margin Over Size: <b>{risk.get('margin_over_size_pct', 'N/A')}%</b>")
    
    q = risk.get("quarantine", {})
    lines.append(f"Quarantine: <b>{q.get('max_consecutive_fails', 'N/A')}</b> fails for <b>{q.get('quarantine_hours', 'N/A')}h</b>")
    
    log = cfg.get("log", {})
    tg = cfg.get("tg", {})
    lines.append("\n📡 <b>[SYSTEM]</b>")
    lines.append(f"TG Enabled: <b>{tg.get('enable', 'N/A')}</b>")
    lines.append(f"Logs: D:{log.get('debug', 'N/A')} I:{log.get('info', 'N/A')} W:{log.get('warning', 'N/A')} E:{log.get('error', 'N/A')}")

    # Входные паттерны
    entry = cfg.get("entry", {}).get("pattern", {})
    ph = entry.get("phemex", {})
    binance = entry.get("binance", {})
    f1 = entry.get("funding_pattern1", {})
    f2 = entry.get("funding_pattern2", {})
    
    lines.append("\n🎯 <b>[ENTRY PHEMEX]</b>")
    lines.append(f"Enable: <b>{ph.get('enable', 'N/A')}</b> | Depth: <b>{ph.get('depth', 'N/A')}</b> | TTL: <b>{ph.get('pattern_ttl_sec', 'N/A')}s</b>")
    
    hdr = ph.get("header", {})
    btm = ph.get("bottom", {})
    lines.append(f"ROC Window: <b>{hdr.get('roc_window', 'N/A')}</b> | Max 1 ROC: <b>{hdr.get('max_one_roc_pct', 'N/A')}%</b>")
    lines.append(f"Sprd 3 row: &gt;= <b>{btm.get('min_spread_between_three_row_pct', 'N/A')}%</b>")
    
    lines.append("\n🔶 <b>[ENTRY BINANCE & FUNDING]</b>")
    lines.append(f"Binance: <b>{binance.get('enable', 'N/A')}</b> | Sprd: &gt;= <b>{binance.get('min_price_spread_rate', 'N/A')}%</b>")
    lines.append(f"Fund1(Phemex): <b>{f1.get('enable', 'N/A')}</b> | Thresh: &gt;= <b>{f1.get('threshold_pct', 'N/A')}%</b>")
    lines.append(f"Fund2(Diff): <b>{f2.get('enable', 'N/A')}</b> | Diff: &gt;= <b>{f2.get('diff_threshold_pct', 'N/A')}%</b>")

    # Сценарии выхода
    exit_cfg = cfg.get("exit", {})
    scen = exit_cfg.get("scenarios", {})
    
    avg = scen.get("base", {})
    neg = scen.get("negative", {})
    ttl = scen.get("breakeven_ttl_close", {})
    
    inter = exit_cfg.get("interference", {})
    ext = exit_cfg.get("extrime_close", {})
    
    lines.append("\n🚪 <b>[EXIT SCENARIOS]</b>")
    lines.append(f"<b>Base (Take):</b> {avg.get('enable', True)} | Stab TTL: <b>{avg.get('stabilization_ttl', 'N/A')}s</b>")
    lines.append(f"<b>Negative:</b> {neg.get('enable', 'N/A')} | Neg Spread &lt;= <b>{neg.get('negative_spread_pct', 'N/A')}%</b> for <b>{neg.get('negative_ttl', 'N/A')}s</b>")
    lines.append(f"<b>Interference:</b> {inter.get('enable', 'N/A')} | Usual Vol: <b>{inter.get('usual_vol_pct_to_init_size', 'N/A')}%</b>")
    lines.append(f"<b>TTL Close:</b> {ttl.get('enable', 'N/A')} | TTL: <b>{ttl.get('position_ttl', 'N/A')}s</b> | Wait: <b>{ttl.get('breakeven_wait_sec', 'N/A')}s</b>")
    lines.append(f"<b>Extrime:</b> {ext.get('enable', 'N/A')} | Retries: <b>{ext.get('retry_num', 'N/A')}</b> per <b>{ext.get('retry_ttl', 'N/A')}s</b>")

    return "\n".join(lines)

def load_json(filepath: str, default: Any = None) -> Any:
    if not os.path.exists(filepath): return default if default is not None else {}
    try:
        with open(filepath, "r", encoding="utf-8") as f: return json.load(f)
    except Exception as e:
        return default if default is not None else {}

def save_json_safe(filepath: str, data: Any) -> None:
    tmp_file = f"{filepath}.{os.getpid()}.tmp"
    for attempt in range(3):
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
            os.replace(tmp_file, filepath)
            return
        except Exception:
            import time
            time.sleep(0.05 * (attempt + 1))
        finally:
            if os.path.exists(tmp_file):
                try: os.remove(tmp_file)
                except: pass