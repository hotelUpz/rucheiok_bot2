# hotelupz/rucheiok/rucheiok-.../consts.py
from __future__ import annotations
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CFG_PATH = BASE_DIR / "cfg.json"

def load_cfg(path: str | Path = CFG_PATH) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Ошибка загрузки {path}: {e}")
        return {}

_CFG = load_cfg()
_LOG = _CFG.get("log", {})

LOG_DEBUG = bool(_LOG.get("debug", True))
LOG_ERROR = bool(_LOG.get("error", True))
LOG_INFO = bool(_LOG.get("info", True))
LOG_WARNING = bool(_LOG.get("warning", True))
MAX_LOG_LINES = int(_LOG.get("max_lines", 1500))
TIME_ZONE = str(_LOG.get("timezone", "UTC"))