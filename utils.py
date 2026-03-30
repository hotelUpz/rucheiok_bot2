import os
import json
import asyncio
from decimal import Decimal, ROUND_HALF_DOWN

def round_step(value: float, step: float) -> float:
    """Округление значения (цена/объем) до ближайшего шага (tick size / lot size)."""
    if not step or step <= 0:
        return value
    val_d = Decimal(str(value))
    step_d = Decimal(str(step))
    quantized = (val_d / step_d).quantize(Decimal('1'), rounding=ROUND_HALF_DOWN) * step_d
    return float(quantized)

def float_to_str(value: float) -> str:
    """Предотвращает появление научной записи (1e-05) при конвертации."""
    return f"{Decimal(str(value)):f}"

def deep_update(d: dict, u: dict) -> dict:
    """Рекурсивное (глубокое) слияние словарей для приоритетного конфига."""
    for k, v in u.items():
        if isinstance(v, dict) and k in d and isinstance(d[k], dict):
            deep_update(d[k], v)
        else:
            d[k] = v
    return d

def get_config_summary(cfg: dict) -> str:
    """Генерирует полный и красивый текстовый дашборд из JSON конфига."""
    lines = []
    
    # --- GLOBAL & RISK ---
    app = cfg.get("app", {})
    risk = cfg.get("risk", {})
    lines.append("🎛 <b>[APP & RISK]</b>")
    lines.append(f"Name: <b>{app.get('name', '')}</b> | Quota: <b>{cfg.get('quota_asset', '')}</b>")
    lines.append(f"Max Positions: <b>{app.get('max_active_positions')}</b>")
    lines.append(f"Leverage: <b>{risk.get('leverage')}x</b> | Margin Mode: <b>{risk.get('margin_mode')}</b>")
    lines.append(f"Margin Size: <b>{risk.get('margin_size')}</b> | Notional Limit: <b>{risk.get('notional_limit')} USDT</b>")
    lines.append(f"Margin Over Size: <b>{risk.get('margin_over_size_pct')}%</b>")
    q = risk.get("quarantine", {})
    lines.append(f"Quarantine: <b>{q.get('max_consecutive_fails')}</b> fails for <b>{q.get('quarantine_hours')}h</b>")
    
    # --- LOG & TG ---
    log = cfg.get("log", {})
    tg = cfg.get("tg", {})
    lines.append("\n📡 <b>[SYSTEM]</b>")
    lines.append(f"TG Enabled: <b>{tg.get('enable')}</b>")
    lines.append(f"Logs: D:{log.get('debug')} I:{log.get('info')} W:{log.get('warning')} E:{log.get('error')} | Lines: {log.get('max_lines')}")

    # --- ENTRY ---
    entry = cfg.get("entry", {}).get("pattern", {})
    ph = entry.get("phemex", {})
    binance = entry.get("binance", {})
    ff = entry.get("phemex_funding_filter", {})
    
    lines.append("\n🎯 <b>[ENTRY PHEMEX]</b>")
    lines.append(f"Enable: <b>{ph.get('enable')}</b> | Depth: <b>{ph.get('depth')}</b> | TTL: <b>{ph.get('pattern_ttl_sec')}s</b>")
    lines.append(f"Vol Limits: <b>{ph.get('min_first_row_usdt_notional')} - {ph.get('max_first_row_usdt_notional')} USDT</b>")
    
    hdr = ph.get("header", {})
    bdy = ph.get("body", {})
    btm = ph.get("bottom", {})
    lines.append(f"ROC Window: <b>{hdr.get('roc_window')}</b> | Max 1 ROC: <b>{hdr.get('max_one_roc_pct')}%</b>")
    lines.append(f"ROC SMA Window: <b>{bdy.get('roc_sma_window')}</b>")
    lines.append(f"Sprd 2 row: &gt;= <b>{btm.get('min_spread_between_two_row_pct')}%</b>")
    lines.append(f"Sprd 3 row: &gt;= <b>{btm.get('min_spread_between_three_row_pct')}%</b>")
    lines.append(f"H/B rate: <b>{ph.get('header_to_bottom_desired_rate')}</b> | Max B/A dist: <b>{ph.get('max_bid_ask_distance_rate')}</b>")
    
    lines.append("\n🔶 <b>[ENTRY BINANCE & FUNDING]</b>")
    lines.append(f"Binance: <b>{binance.get('enable')}</b> | Sprd: &gt;= <b>{binance.get('min_price_spread_pct')}%</b> | TTL: <b>{binance.get('spread_ttl_sec')}s</b>")
    lines.append(f"Funding: <b>{ff.get('enable')}</b> | Thresh: &gt;= <b>{ff.get('funding_threshold_pct')}%</b> | Skip before: <b>{ff.get('skip_before_counter_sec')}s</b>")

    # --- EXIT ---
    exit_cfg = cfg.get("exit", {})
    scen = exit_cfg.get("scenarious", {})
    avg = scen.get("average", {})
    neg = scen.get("negative", {})
    inter = exit_cfg.get("interference", {})
    ttl = exit_cfg.get("position_ttl_close", {})
    ext = exit_cfg.get("extrime_close", {})
    
    lines.append("\n🚪 <b>[EXIT SCENARIOS]</b>")
    lines.append(f"<b>Average:</b> {avg.get('enable')} | Stab TTL: <b>{avg.get('stabilization_ttl')}s</b>")
    lines.append(f"└ Trgt: <b>{avg.get('target_rate')}</b> -&gt; Min: <b>{avg.get('min_target_rate')}</b> | Shift: <b>-{avg.get('shift_demotion')}</b> / {avg.get('shift_ttl')}s")
    
    lines.append(f"<b>Negative:</b> {neg.get('enable')} | Stab TTL: <b>{neg.get('stabilization_ttl')}s</b>")
    lines.append(f"└ Spread &lt;= <b>{neg.get('negative_spread_pct')}%</b> for <b>{neg.get('negative_ttl')}s</b>")
    
    lines.append(f"<b>Interference:</b> {inter.get('enable')} | Avg Vol: <b>{inter.get('average_vol_pct_to_init_size')}%</b> | Max Vol: <b>{inter.get('max_vol_pct_to_init_size')}%</b>")
    
    lines.append(f"<b>TTL Close:</b> {ttl.get('enable')} | TTL: <b>{ttl.get('position_ttl')}s</b> | Wait: <b>{ttl.get('breakeven_wait_sec')}s</b> | Orient: <b>{ttl.get('to_entry_orientation')}</b>")
    
    lines.append(f"<b>Extrime:</b> {ext.get('enable')} | Retries: <b>{ext.get('retry_num')}</b> per <b>{ext.get('retry_ttl')}s</b>")
    lines.append(f"└ Incr Frac: <b>+{ext.get('increase_fraction')}%</b> | Orient: <b>{ext.get('bid_to_ask_orientation')}</b>")

    return "\n".join(lines)