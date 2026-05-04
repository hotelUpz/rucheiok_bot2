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
    """
    Рекурсивно формирует читаемый вывод конфига. 
    Больше не нужно переписывать эту функцию при изменениях структуры!
    """
    lines = ["🎛 <b>[ТЕКУЩИЙ КОНФИГ БОТА]</b>"]
    
    def parse_dict(d: dict, indent: int = 0):
        prefix = "  " * indent
        for k, v in d.items():
            # Пропускаем скрытые/технические ключи
            if str(k).startswith("_"): continue 
            
            if isinstance(v, dict):
                # Если в блоке есть "красивое имя"
                label = v.get("_label", k.upper())
                lines.append(f"{prefix}🔹 <b>[{label}]</b>")
                parse_dict(v, indent + 1)
            elif isinstance(v, list):
                # Для списков (например, proxies или black_list) выводим просто количество
                lines.append(f"{prefix}▪️ {k}: <b>{len(v)} элементов</b>")
            else:
                # Обычные параметры: bool, int, float, str
                val_str = "ON" if v is True else "OFF" if v is False else str(v)
                lines.append(f"{prefix}▪️ {k}: <b>{val_str}</b>")

    parse_dict(cfg)
    lines.append("────────────────────────────────────────")
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