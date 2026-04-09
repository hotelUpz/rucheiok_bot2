# ============================================================
# FILE: CORE/orchestrator.py
# ROLE: Главный оркестратор торговых процессов (Game Loop Pattern)
# ============================================================
from __future__ import annotations
import asyncio
import time
import traceback
from typing import Dict, Any, Set, TYPE_CHECKING
import os
import aiohttp
from pathlib import Path

from API.PHEMEX.symbol import PhemexSymbols
from API.PHEMEX.stakan import PhemexStakanStream
from API.PHEMEX.order import PhemexPrivateClient
from API.PHEMEX.ws_private import PhemexPrivateWS
from API.BINANCE.ticker import BinanceTickerAPI
from API.PHEMEX.ticker import PhemexTickerAPI
from API.PHEMEX.funding import PhemexFunding

from ENTRY.signal_engine import SignalEngine
from CORE.restorator import BotState
from CORE.executor import OrderExecutor
from CORE.models_fsm import WsInterpreter
from CORE._utils import BlackListManager, PriceCacheManager, ConfigManager, Reporters

from EXIT.scenarios.base import BaseScenario
from EXIT.scenarios.negative import NegativeScenario
from EXIT.scenarios.breakeven import PositionTTLClose
from EXIT.interference import Interference
from EXIT.extrime_close import ExtrimeClose

from c_log import UnifiedLogger
from utils import get_config_summary

if TYPE_CHECKING:
    from API.PHEMEX.stakan import DepthTop

logger = UnifiedLogger("bot")
BASE_DIR = Path(__file__).resolve().parent.parent
CFG_PATH = BASE_DIR / "cfg.json"

class TradingBot:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.max_active_positions = self.cfg.get("app", {}).get("max_active_positions", 1)
        self.quota_asset = self.cfg.get("quota_asset", "USDT")
        
        self.signal_timeout_sec = self.cfg.get("entry", {}).get("signal_timeout_sec", 0.1)
        self.hedge_mode = self.cfg.get("risk", {}).get("hedge_mode", False)
        # min_exchange_notional ПОЛНОСТЬЮ УДАЛЕН отсюда
        
        upd_sec = self.cfg.get("entry", {}).get("pattern", {}).get("binance", {}).get("update_prices_sec", 3.0)
        
        api_key = os.getenv("API_KEY") or self.cfg["credentials"].get("api_key", "")
        api_secret = os.getenv("API_SECRET") or self.cfg["credentials"].get("api_secret", "")   

        self.bl_manager = BlackListManager(CFG_PATH, self.quota_asset)        
        self.black_list = self.bl_manager.load_from_config(self.cfg.get("black_list", []))
        
        self.state = BotState(black_list=self.black_list)       
        self._is_running = False

        self.phemex_sym_api = PhemexSymbols()
        self.binance_ticker_api = BinanceTickerAPI()
        self.phemex_ticker_api = PhemexTickerAPI()
        self.phemex_funding_api = PhemexFunding()
        self.session = aiohttp.ClientSession()

        self.price_manager = PriceCacheManager(self.binance_ticker_api, self.phemex_ticker_api, upd_sec)
        self.private_client = PhemexPrivateClient(api_key, api_secret, self.session)
        self.private_ws = PhemexPrivateWS(api_key, api_secret)

        tg_cfg = self.cfg.get("tg", {})
        from TG.tg_sender import TelegramSender
        self.tg = TelegramSender(
            os.getenv("TELEGRAM_TOKEN") or tg_cfg.get("token", ""),
            os.getenv("TELEGRAM_CHAT_ID") or tg_cfg.get("chat_id", ""),
        ) if tg_cfg.get("enable") else None

        self.signal_engine = SignalEngine(cfg["entry"], self.phemex_funding_api)

        self._stream: PhemexStakanStream | None = None
        self._processing: Set[str] = set()
        self._latest_market_data: Dict[str, DepthTop] = {}
        self._signal_timeouts: Dict[str, float] = {}
        self.cfg_manager = ConfigManager(CFG_PATH, self)
        
        self.active_positions_locker: Dict[str, asyncio.Lock] = {} 

        self.ws_handler = WsInterpreter(state=self.state, active_positions_locker=self.active_positions_locker) 
        self.executor = OrderExecutor(self)
        
        exit_cfg = self.cfg.get("exit", {})
        scen_cfg = exit_cfg.get("scenarios", {})
        
        self.scen_base = BaseScenario(scen_cfg.get("base", {}))
        self.scen_neg = NegativeScenario(scen_cfg.get("negative", {}))
        self.scen_ttl = PositionTTLClose(scen_cfg.get("breakeven_ttl_close", {}), self.active_positions_locker)
        self.scen_interf = Interference(exit_cfg.get("interference", {}))
        self.scen_extrime = ExtrimeClose(exit_cfg.get("extrime_close", {}))

    async def _await_task(self, task: asyncio.Task | None):
        if not task: return
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
        except Exception as e: logger.debug(f"Task shutdown note: {e}")

    def set_blacklist(self, symbols: list) -> tuple[bool, str]:
        success, msg = self.bl_manager.update_and_save(symbols)
        if success:
            self.black_list = self.bl_manager.symbols
            if hasattr(self.state, 'black_list'):
                self.state.black_list = self.black_list
        return success, msg    

    async def quarantine_util(self, symbol) -> bool:        
        if symbol in self.state.quarantine_until:
            q_val = self.state.quarantine_until[symbol]
            
            # ФИКС КРАША: Безопасное приведение к float (переварит и строку "inf", и числа)
            try:
                limit_time = float(q_val)
            except (ValueError, TypeError):
                limit_time = 0.0

            if time.time() > limit_time:
                del self.state.quarantine_until[symbol]
                self.state.consecutive_fails[symbol] = 0
                await self.state.save()
                return True            
            elif not any(k.startswith(f"{symbol}_") for k in self.state.active_positions): 
                return False
        return True
        
    def apply_entry_quarantine(self, symbol: str):
        q_hours = self.cfg.get("entry", {}).get("quarantine", {}).get("quarantine_hours", 1)
        if str(q_hours).lower() == "inf":
            self.state.quarantine_until[symbol] = float('inf')
            logger.warning(f"[{symbol}] 🚫 Помещен в бессрочный карантин (вход не удался).")
        elif float(q_hours) > 0:
            self.state.quarantine_until[symbol] = time.time() + (float(q_hours) * 3600)
            logger.warning(f"[{symbol}] 🚫 Помещен в карантин на {q_hours}ч (вход не удался).")

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
                    pos_side_raw = item.get("posSide", item.get("side", "")).lower()
                    pos_side = "LONG" if pos_side_raw in ("long", "buy") else "SHORT"
                    pos_key = f"{symbol}_{pos_side}"
                    exchange_positions[pos_key] = {"size": abs(size), "side": pos_side, "symbol": symbol}

            self.state.active_positions.clear()
            await self.state.save()
            logger.info("✅ Стейт успешно восстановлен.")
        except Exception as e:
            logger.error(f"❌ Ошибка Recovery: {e}")

    def _get_lock(self, pos_key: str) -> asyncio.Lock:
        if pos_key not in self.active_positions_locker:
            self.active_positions_locker[pos_key] = asyncio.Lock()
        return self.active_positions_locker[pos_key]

    def _check_risk_limits(self, symbol: str, long_key: str, short_key: str) -> bool:
        has_long = long_key in self.state.active_positions or long_key in self.state.pending_entry_orders
        has_short = short_key in self.state.active_positions or short_key in self.state.pending_entry_orders

        if not self.hedge_mode and (has_long or has_short): return False

        active_symbols = set(p.symbol for p in self.state.active_positions.values())
        pending_symbols = set(k.split("_")[0] for k in self.state.pending_entry_orders)
        used_slots = len(active_symbols | pending_symbols)

        if used_slots >= self.max_active_positions and symbol not in active_symbols and symbol not in pending_symbols:
            return False

        return True
    
    async def _evaluate_exit_scenarios(self, snap: DepthTop, symbol: str, long_key: str, short_key: str) -> None:
        now = time.time()
        exit_cfg = self.cfg.get("exit", {})
        timeout_extrime = exit_cfg.get("extrime_close", {}).get("extrime_timeout_sec", 0.1)
        timeout_hunt = exit_cfg.get("hunting_timeout_sec", 0.1)
        
        for pos_key in (long_key, short_key):
            async with self._get_lock(pos_key):
                pos = self.state.active_positions.get(pos_key)
                if not pos or pos.current_qty <= 0:
                    continue

                neg_res = self.scen_neg.analyze(snap, pos, now)
                if neg_res == "NEGATIVE_TIMEOUT": pos.in_extrime_mode = True

                ttl_res = await self.scen_ttl.analyze(pos, now)
                if ttl_res == "EXTRIME_SCENARIO": pos.in_extrime_mode = True

                if pos.in_extrime_mode:
                    ext_price = self.scen_extrime.analyze(snap, pos, now)
                    if ext_price and pos.current_close_price != ext_price:
                        pos.current_close_price = ext_price 
                        asyncio.create_task(self.executor.execute_exit(symbol, pos_key, ext_price, timeout_extrime))
                    continue 

                if pos.in_breakeven_mode:
                    be_price = self.scen_ttl.build_target_price(pos)
                    if pos.current_close_price != be_price:
                        pos.current_close_price = be_price 
                        asyncio.create_task(self.executor.execute_exit(symbol, pos_key, be_price, timeout_hunt))
                    continue

                base_price = self.scen_base.analyze(snap, pos, now)
                if base_price and pos.current_close_price != base_price:
                    pos.current_close_price = base_price 
                    asyncio.create_task(self.executor.execute_exit(symbol, pos_key, base_price, timeout_hunt))

                if not pos.interf_in_flight:
                    interf_res = self.scen_interf.analyze(snap, pos, now)
                    if interf_res:
                        i_price, i_qty = interf_res
                        pos.interf_in_flight = True 
                        asyncio.create_task(self.executor.interf_bought(symbol, pos_key, i_qty, i_price))

    async def _evaluate_entry_signal(self, snap: DepthTop, symbol: str) -> None:
        b_price, p_price = self.price_manager.get_prices(symbol)
        signal = self.signal_engine.analyze(snap, b_price, p_price)
        if not signal: return

        pos_key = f"{symbol}_{signal['side']}"
        if pos_key in self.state.active_positions or pos_key in self.state.pending_entry_orders: 
            return 

        if self._signal_timeouts.get(pos_key, 0) > time.time(): return
        self._signal_timeouts[pos_key] = time.time() + self.signal_timeout_sec

        try:
            signal["row_vol_asset"] = snap.asks[0][1] if signal["side"] == "LONG" else snap.bids[0][1]
            signal["init_ask1"] = snap.asks[0][0]
            signal["init_bid1"] = snap.bids[0][0]
            
            self.state.pending_entry_orders[pos_key] = str(time.time())
            asyncio.create_task(self.executor.execute_entry(symbol, pos_key, signal))
        except Exception as e:
            logger.error(f"[{pos_key}] Ошибка постановки входа: {e}")
            self.state.pending_entry_orders.pop(pos_key, None) 

    async def _process_symbol_pipeline(self, snap: DepthTop):
        symbol = snap.symbol
        if symbol in self._processing: return
        self._processing.add(symbol)
        try:
            if symbol in self.black_list and not any(k.startswith(f"{symbol}_") for k in self.state.active_positions): return
            if hasattr(self, 'quarantine_util') and not await self.quarantine_util(symbol): return
            pos_long_key, pos_short_key = f"{symbol}_LONG", f"{symbol}_SHORT"
            await self._evaluate_exit_scenarios(snap, symbol, pos_long_key, pos_short_key)
            if not self._check_risk_limits(symbol, pos_long_key, pos_short_key): return
            await self._evaluate_entry_signal(snap, symbol)
        except Exception as e:
            # ФИКС ЛОГИРОВАНИЯ: Больше никаких скрытых трейсбеков! Выводим полную трассу ошибки.
            err_tb = traceback.format_exc()
            logger.error(f"Pipeline error for {symbol}: {e}\n{err_tb}")
        finally:
            self._processing.discard(symbol)

    async def _stakan_data_sink(self, snap: DepthTop):
        self._latest_market_data[snap.symbol] = snap

    async def _main_trading_loop(self):
        logger.info("🎮 Главная торговая живолупа (Game Loop) запущена.")
        while self._is_running:
            keys_to_check = list(self.state.active_positions.keys())
            for pos_key in keys_to_check:
                async with self._get_lock(pos_key):
                    pos = self.state.active_positions.get(pos_key)
                    if pos and getattr(pos, 'is_closed_by_exchange', False):
                        if self.tg and pos.entry_price > 0.0:
                            if pos.in_extrime_mode: semantic = "⚠️ Аварийный выход (Extrime Mode)"
                            elif pos.in_breakeven_mode: semantic = "🛡 Выход по безубытку (TTL)"
                            else: semantic = "🎯 Тейк-профит (Base Scenario)"
                            
                            exit_pr = pos.realized_exit_price if pos.realized_exit_price > 0 else (pos.current_close_price or pos.avg_price)
                            msg = Reporters.exit_success(pos_key, semantic, exit_pr)
                            asyncio.create_task(self.tg.send_message(msg))
                        elif pos.entry_price == 0.0:
                            logger.info(f"[{pos_key}] Вход отменен.")

                        logger.info(f"[{pos_key}] 🛑 Позиция закрыта физически. Стейт очищен оркестратором.")
                        self.state.active_positions.pop(pos_key, None)

            current_snaps = list(self._latest_market_data.values())
            if not current_snaps:
                await asyncio.sleep(0.01)
                continue
            tasks = [self._process_symbol_pipeline(snap) for snap in current_snaps]
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0.001)

    async def start(self):
        if getattr(self, '_is_running', False): return
        self._is_running = True
        logger.info("▶️ Инициализация систем...")
        summary = get_config_summary(self.cfg)
        logger.info(f"⚙️ БОТ ЗАПУЩЕН С НАСТРОЙКАМИ\n{summary}")
        if self.tg: asyncio.create_task(self.tg.send_message("🟢 <b>ТОРГОВЛЯ НАЧАТА</b>"))

        symbols_info = await self.phemex_sym_api.get_all(quote=self.bl_manager.quota_asset, only_active=True)
        self.symbol_specs = {s.symbol: s for s in symbols_info if s and s.symbol not in self.black_list}

        await self._recover_state()

        logger.info("🔄 Прогрев кэша цен и фандинга...")
        await self.price_manager.warmup()
        self._price_updater_task = asyncio.create_task(self.price_manager.loop())
        self._funding_task = asyncio.create_task(self.signal_engine.funding_filter.run())
        
        self._private_ws_task = asyncio.create_task(self.private_ws.run(self.ws_handler.process_phemex_message))

        symbols = [s.symbol for s in symbols_info if s and s.symbol not in self.black_list]
        self._stream = PhemexStakanStream(symbols=symbols, depth=10, chunk_size=40)
        self._stream_task = asyncio.create_task(self._stream.run(self._stakan_data_sink))

        self._game_loop_task = asyncio.create_task(self._main_trading_loop())

    async def aclose(self):
        await self.stop()
        logger.info("💾 Финальное сохранение стейта на диск...")
        await self.state.save()
        await self.phemex_sym_api.aclose()
        await self.binance_ticker_api.aclose()
        await self.phemex_ticker_api.aclose()
        await self.phemex_funding_api.aclose()
        if self.tg: await self.tg.aclose()
        if self.session and not self.session.closed: await self.session.close()

    async def stop(self):
        if not getattr(self, '_is_running', False): return
        self._is_running = False
        logger.info("⏹ Остановка процессов...")
        if self.tg: await self.tg.send_message("⏹ Остановка процессов...")
        self.price_manager.stop()
        self.signal_engine.funding_filter.stop()
        if self._stream: self._stream.stop()
        await self.private_ws.aclose()
        await self._await_task(getattr(self, '_price_updater_task', None))
        await self._await_task(getattr(self, '_funding_task', None))
        await self._await_task(getattr(self, '_private_ws_task', None))
        await self._await_task(getattr(self, '_stream_task', None))
        await self._await_task(getattr(self, '_game_loop_task', None))
        self._latest_market_data.clear()
        self._processing.clear()
        self._signal_timeouts.clear()
        await self.state.save()