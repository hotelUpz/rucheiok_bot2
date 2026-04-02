# ============================================================
# FILE: CORE/bot.py
# ROLE: Главный оркестратор торговых процессов
# ============================================================
from __future__ import annotations

import asyncio
import time
from typing import Dict, Any, Set, TYPE_CHECKING
import os
import json
import aiohttp
from pathlib import Path

from API.PHEMEX.symbol import PhemexSymbols
from API.PHEMEX.stakan import PhemexStakanStream
from API.PHEMEX.order import PhemexPrivateClient
from API.PHEMEX.ws_private import PhemexPrivateWS
from API.BINANCE.ticker import BinanceTickerAPI
from API.PHEMEX.ticker import PhemexTickerAPI
from API.PHEMEX.funding import PhemexFunding

from ENTRY.engine import EntryEngine
from EXIT.engine import ExitEngine
from CORE.models import BotState
from CORE.executor import OrderExecutor
from CORE.ws_handler import PrivateWSHandler
from CORE.bot_utils import BlackListManager, PriceCacheManager
from c_log import UnifiedLogger
from TG.tg_sender import TelegramSender
from utils import get_config_summary

if TYPE_CHECKING:
    from CORE.models import ActivePosition
    from API.PHEMEX.symbol import SymbolInfo
    from API.PHEMEX.stakan import DepthTop


logger = UnifiedLogger("bot")
BASE_DIR = Path(__file__).resolve().parent.parent
CFG_PATH = BASE_DIR / "cfg.json"

class TradingBot:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.max_active_positions = self.cfg.get("app", {}).get("max_active_positions", 1)
        quota_asset = self.cfg.get("quota_asset", "USDT")

        # Инициализация сервисов (Черный список и Кэш цен)
        self.bl_manager = BlackListManager(CFG_PATH, quota_asset)
        self.black_list = self.bl_manager.load_from_config(self.cfg.get("black_list", []))
        
        self.state = BotState(black_list=self.black_list)
        self.state.load()

        self.executor = OrderExecutor(self)
        self.ws_handler = PrivateWSHandler(self)
        self._is_running = False

        self.phemex_sym_api = PhemexSymbols()
        self.binance_ticker_api = BinanceTickerAPI()
        self.phemex_ticker_api = PhemexTickerAPI()
        self.phemex_funding_api = PhemexFunding()
        self.session = aiohttp.ClientSession()
        
        # Настройка Менеджера цен
        upd_sec = self.cfg.get("entry", {}).get("pattern", {}).get("binance", {}).get("update_prices_sec", 3.0)
        self.price_manager = PriceCacheManager(self.binance_ticker_api, self.phemex_ticker_api, upd_sec)

        api_key = os.getenv("API_KEY") or self.cfg["credentials"].get("api_key", "")
        api_secret = os.getenv("API_SECRET") or self.cfg["credentials"].get("api_secret", "")

        self.private_client = PhemexPrivateClient(api_key, api_secret, self.session)
        self.private_ws = PhemexPrivateWS(api_key, api_secret)

        tg_cfg = self.cfg.get("tg", {})
        self.tg = TelegramSender(
            os.getenv("TELEGRAM_TOKEN") or tg_cfg.get("token", ""),
            os.getenv("TELEGRAM_CHAT_ID") or tg_cfg.get("chat_id", ""),
        ) if tg_cfg.get("enable") else None

        self.entry_engine = EntryEngine(cfg["entry"], self.phemex_funding_api)
        self.exit_engine = ExitEngine(cfg["exit"], self)
        self.symbol_specs: Dict[str, SymbolInfo] = {}

        self._stream: PhemexStakanStream | None = None
        self._processing: Set[str] = set()
        self._latest_depth: Dict[str, DepthTop] = {}
        self._depth_workers: Dict[str, asyncio.Task] = {}
        self._funding_task: asyncio.Task | None = None
        self._private_ws_task: asyncio.Task | None = None
        self._stream_task: asyncio.Task | None = None
        self._price_updater_task: asyncio.Task | None = None

    async def aclose(self):
        await self.stop()
        logger.info("💾 Финальное сохранение стейта на диск...")
        await self.state.save()

        await self.phemex_sym_api.aclose()
        await self.binance_ticker_api.aclose()
        await self.phemex_ticker_api.aclose()
        await self.phemex_funding_api.aclose()
        if self.tg:
            await self.tg.aclose()
        if self.session and not self.session.closed:
            await self.session.close()

    async def _await_task(self, task: asyncio.Task | None):
        if not task: return
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
        except Exception as e: logger.debug(f"Task shutdown note: {e}")

    def reload_config(self) -> tuple[bool, str]:
        try:
            with open(CFG_PATH, "r", encoding="utf-8") as f:
                new_cfg = json.load(f)

            self.cfg = new_cfg
            self.max_active_positions = self.cfg.get("app", {}).get("max_active_positions", 1)
            
            # Делегируем обновление ЧС утилите
            self.black_list = self.bl_manager.load_from_config(self.cfg.get("black_list", []))
            if hasattr(self.state, 'black_list'):
                self.state.black_list = self.black_list

            old_ff = getattr(self.entry_engine, 'funding_filter', None) if hasattr(self, 'entry_engine') else None
            if old_ff: old_ff.stop()

            self.entry_engine = EntryEngine(self.cfg["entry"], self.phemex_funding_api)
            self.exit_engine = ExitEngine(self.cfg["exit"], self)

            if getattr(self, '_is_running', False):
                self._funding_task = asyncio.create_task(self.entry_engine.funding_filter.run())

            return True, "Конфигурация успешно обновлена в памяти!"
        except Exception as e:
            logger.error(f"Config reload error: {e}")
            return False, f"Ошибка загрузки в память: {e}"

    def set_blacklist(self, symbols: list) -> tuple[bool, str]:
        # Вся черновая работа с ЧС передана менеджеру
        success, msg = self.bl_manager.update_and_save(symbols)
        if success:
            self.black_list = self.bl_manager.symbols
            if hasattr(self.state, 'black_list'):
                self.state.black_list = self.black_list
        return success, msg
    
    async def _recover_state(self):
        try:
            self.state.load()
            resp = await self.private_client.get_active_positions()
            data_block = resp.get("data", {})
            data = data_block.get("positions", []) if isinstance(data_block, dict) else data_block
            if not isinstance(data, list): data = []

            exchange_positions = {}
            for item in data:
                size = float(item.get("sizeRq", item.get("size", 0)))
                if size != 0:
                    symbol = item.get("symbol")
                    pos_side = "LONG" if item.get("posSide", item.get("side", "")) in ("Long", "Buy") else "SHORT"
                    pos_key = f"{symbol}_{pos_side}"
                    exchange_positions[pos_key] = {"size": abs(size), "side": pos_side, "symbol": symbol}

            old_positions = dict(self.state.active_positions)
            self.state.active_positions.clear()

            for pos_key, pos in old_positions.items():
                if pos_key in exchange_positions:
                    exch_data = exchange_positions[pos_key]
                    if pos.side != exch_data["side"]: continue
                        
                    try: await self.private_client.cancel_all_orders(pos.symbol)
                    except Exception: pass

                    pos.qty = exch_data["size"]
                    pos.close_order_id = None
                    pos.entry_finalized = pos.qty > 0
                    pos.entry_cancel_requested = False
                    pos.interference_cancel_requested = False

                    self.state.active_positions[pos_key] = pos
                    logger.info(f"🔄 [RECOVERY] Восстановлена позиция: {pos_key}. Объем: {pos.qty}, Вход: {pos.entry_price}")
                else:
                    logger.info(f"🗑 [RECOVERY] Позиция {pos_key} закрыта извне. Удаляем из кэша.")

            await self.state.save()
        except Exception as e:
            logger.error(f"❌ Ошибка Recovery: {e}")

    # ==========================================
    # ИСПРАВЛЕННЫЕ И БЕЗОПАСНЫЕ ВОРКЕРЫ СТАКАНА
    # ==========================================
    async def _depth_worker_loop(self, symbol: str):
        """Безопасный цикл обработки стакана без рекурсии в finally."""
        while self._is_running:
            snap = self._latest_depth.pop(symbol, None)
            if snap is None:
                break # Нет свежих данных - гасим воркер, он освобождает память
            await self._process_depth(snap)

    async def _on_depth_received(self, snap: DepthTop):
        if not self._is_running: return
        self._latest_depth[snap.symbol] = snap
        
        worker = self._depth_workers.get(snap.symbol)
        # Если воркера нет или он завершил цикл обработки (done) - запускаем новый
        if worker is None or worker.done():
            self._depth_workers[snap.symbol] = asyncio.create_task(self._depth_worker_loop(snap.symbol))

    async def _process_depth(self, snap: DepthTop):
        if not self._is_running: return
        symbol = snap.symbol
        
        if symbol in self.black_list and not any(k.startswith(f"{symbol}_") for k in self.state.active_positions): return
        if symbol in self.state.quarantine_until:
            if time.time() > self.state.quarantine_until[symbol]:
                del self.state.quarantine_until[symbol]
                self.state.consecutive_fails[symbol] = 0
                await self.state.save()
            elif not any(k.startswith(f"{symbol}_") for k in self.state.active_positions): return

        if symbol in self._processing: return
        self._processing.add(symbol)
        try:
            pos_long_key, pos_short_key = f"{symbol}_LONG", f"{symbol}_SHORT"
            
            # 1. Обработка Выхода
            for pos_key in (pos_long_key, pos_short_key):
                if pos_key in self.state.active_positions:
                    pos: 'ActivePosition' = self.state.active_positions[pos_key]
                    
                    if pos_key in self.state.pending_entry_orders and pos.qty <= 0: 
                        continue
                        
                    # 🛑 БЛОКИРОВКА АНАЛИЗА ПРИ АКТИВНОЙ ОХОТЕ
                    # Если мы ударили по стакану, даем ордеру минимальное время (напр. 1 сек).
                    # Таймеры деградации цели замораживаются. Ждем развязки (Full Fill или Partial Fill).
                    if pos.hunting_active_until > time.time():
                        continue
                        
                    action: Dict[str, Any] | None = self.exit_engine.analyze(snap, pos)
                    if action: 
                        await self.executor.handle_exit_action(symbol, pos_key, action)
            
            # 2. Логика слотов и Hedge Mode
            hedge_mode = self.cfg.get("risk", {}).get("hedge_mode", False)
            has_long = pos_long_key in self.state.active_positions or pos_long_key in self.state.in_flight_orders
            has_short = pos_short_key in self.state.active_positions or pos_short_key in self.state.in_flight_orders

            if not hedge_mode and (has_long or has_short): return

            active_symbols = set(p.symbol for p in self.state.active_positions.values())
            in_flight_symbols = set(k.split("_")[0] for k in self.state.in_flight_orders)
            used_slots = len(active_symbols | in_flight_symbols)

            if used_slots >= self.max_active_positions and symbol not in active_symbols and symbol not in in_flight_symbols:
                return

            # 3. Вход: Мгновенное чтение из кэша утилиты
            b_price, p_price = self.price_manager.get_prices(symbol)
            signal = self.entry_engine.analyze(snap, b_price, p_price)
            
            if signal:
                pos_key = f"{symbol}_{signal['side']}"
                if pos_key in self.state.active_positions or pos_key in self.state.pending_entry_orders or pos_key in self.state.in_flight_orders: 
                    return 

                self.state.in_flight_orders.add(pos_key)
                try:
                    signal["row_vol_asset"] = snap.asks[0][1] if signal["side"] == "LONG" else snap.bids[0][1]
                    await self.executor.execute_entry(symbol, pos_key, signal, snap)
                finally:
                    self.state.in_flight_orders.discard(pos_key)
                    
        except Exception as e:
            logger.debug(f"Scan error: {e}")
        finally:
            self._processing.discard(symbol)

    async def start(self):
        if getattr(self, '_is_running', False): return
        self._is_running = True
        logger.info("▶️ Запуск торговых процессов...")

        summary = get_config_summary(self.cfg)
        logger.info(f"⚙️ БОТ ЗАПУЩЕН С НАСТРОЙКАМИ\n{summary}")

        if self.tg:
            asyncio.create_task(self.tg.send_message("🟢 <b>ТОРГОВЛЯ НАЧАТА</b>"))

        symbols_info = await self.phemex_sym_api.get_all(quote=self.bl_manager.quota_asset, only_active=True)
        self.symbol_specs = {s.symbol: s for s in symbols_info if s and s.symbol not in self.black_list}

        await self._recover_state()

        # Прогрев и старт менеджера цен
        logger.info("🔄 Прогрев кэша цен (Binance/Phemex)...")
        await self.price_manager.warmup()
        self._price_updater_task = asyncio.create_task(self.price_manager.loop())
        
        self._funding_task = asyncio.create_task(self.entry_engine.funding_filter.run())
        self._private_ws_task = asyncio.create_task(self.private_ws.run(self.ws_handler.process_phemex_message))

        symbols = [s.symbol for s in symbols_info if s and s.symbol not in self.black_list]
        self._stream = PhemexStakanStream(symbols=symbols, depth=10, chunk_size=40)
        self._stream_task = asyncio.create_task(self._stream.run(self._on_depth_received))

    async def stop(self):
        if not getattr(self, '_is_running', False): return
        self._is_running = False
        logger.info("⏹ Остановка процессов...")

        self.price_manager.stop()
        self.entry_engine.funding_filter.stop()
        if self._stream: self._stream.stop()
        await self.private_ws.aclose()
        
        await self._await_task(self._price_updater_task)
        await self._await_task(self._funding_task)
        await self._await_task(self._private_ws_task)
        await self._await_task(self._stream_task)
        
        for task in list(self._depth_workers.values()):
            await self._await_task(task)
            
        self._depth_workers.clear()
        self._latest_depth.clear()
        self._processing.clear()

        await self.state.save()