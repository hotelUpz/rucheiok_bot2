# ============================================================
# FILE: c_log.py
# ROLE: Унифицированный логгер с поддержкой таймзон и ротацией файлов
# ============================================================
from __future__ import annotations

import inspect
import logging
import sys
# import os
import time
from datetime import datetime
from functools import wraps
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional, Dict

import pytz

from consts import LOG_DEBUG, LOG_ERROR, LOG_INFO, LOG_WARNING, MAX_LOG_LINES, TIME_ZONE

TZ = pytz.timezone(TIME_ZONE)


class _TzFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, TZ)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat()


class UnifiedLogger:
    def __init__(self, name: str, log_dir: str = "./logs", max_lines: int = MAX_LOG_LINES, context: Optional[str] = None):
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        log_path = Path(log_dir) / f"{name}.log"
        approx_line_len = 350
        max_bytes = max(100_000, approx_line_len * max_lines)

        base_logger = logging.getLogger(name)
        base_logger.setLevel(logging.DEBUG)
        base_logger.propagate = False

        if not base_logger.handlers:
            # Создаем единый форматер для всех выводов
            formatter = _TzFormatter("%(asctime)s | %(levelname)s | %(context)s | %(message)s", "%Y-%m-%d %H:%M:%S")

            # 1. Обработчик для записи в файл
            file_handler = RotatingFileHandler(log_path, maxBytes=max_bytes, backupCount=2, encoding="utf-8")
            file_handler.setFormatter(formatter)
            base_logger.addHandler(file_handler)

            # 2. Обработчик для вывода в консоль (заменяет print)
            if LOG_DEBUG: 
                console_handler = logging.StreamHandler(sys.stdout)
                console_handler.setFormatter(formatter)
                base_logger.addHandler(console_handler)

        self._logger = logging.LoggerAdapter(base_logger, extra={"context": context or name})
        self._last_logs: Dict[str, float] = {}
        self._spam_throttle = 10.0 # Секунд между одинаковыми сообщениями

    def _check_spam(self, msg: str) -> bool:
        now = time.time()
        if msg in self._last_logs:
            if now - self._last_logs[msg] < self._spam_throttle:
                return True
        self._last_logs[msg] = now
        return False

    def debug(self, msg: str, *args, **kwargs) -> None:
        if LOG_DEBUG:
            if self._check_spam(msg): return
            self._logger.debug(msg, *args, **kwargs)

    def info(self, msg: str, *args, **kwargs) -> None:
        if LOG_INFO:
            if self._check_spam(msg): return
            self._logger.info(msg, *args, **kwargs)

    def warning(self, msg: str, *args, **kwargs) -> None:
        if LOG_WARNING:
            if self._check_spam(msg): return
            self._logger.warning(msg, *args, **kwargs)

    def error(self, msg: str, *args, **kwargs) -> None:
        if LOG_ERROR:
            # Ошибки не фильтруем от спама, они важны всегда
            self._logger.error(msg, *args, **kwargs)

    def exception(self, msg: str, *args, **kwargs) -> None:
        if LOG_ERROR:
            self._logger.exception(msg, *args, **kwargs)

    def total_exception_decor(self, func, context: Optional[Any] = None):
        if getattr(func, "_is_wrapped", False):
            return func

        if context is not None:
            target_logger = logging.LoggerAdapter(self._logger.logger, extra={"context": context})
        else:
            target_logger = self._logger

        if hasattr(func, "__call__"):
            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                try:
                    return await func(*args, **kwargs)
                except Exception:
                    target_logger.exception("Unhandled async exception in %s", getattr(func, "__qualname__", repr(func)))
                    return None

            @wraps(func)
            def sync_wrapper(*args, **kwargs):
                try:
                    return func(*args, **kwargs)
                except Exception:
                    target_logger.exception("Unhandled sync exception in %s", getattr(func, "__qualname__", repr(func)))
                    return None

            wrapper = async_wrapper if inspect.iscoroutinefunction(func) else sync_wrapper
            wrapper._is_wrapped = True
            return wrapper
        return func