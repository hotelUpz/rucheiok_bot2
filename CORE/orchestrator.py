# ============================================================
# FILE: CORE/orchestrator.py
# ROLE: Главный оркестратор торговых процессов (Game Loop Pattern)
# ============================================================

from __future__ import annotations
import asyncio
import time
import traceback
from typing import Dict, Any, Set, TYPE_CHECKING, List, Tuple
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
from API.BINANCE.funding import BinanceFunding

from ENTRY.signal_engine import SignalEngine
from CORE.restorator import BotState
from CORE.executor import OrderExecutor
from CORE.models_fsm import WsInterpreter, ActivePosition
from CORE._utils import BlackListManager, PriceCacheManager, ConfigManager, Reporters

from EXIT.scenarios.base import BaseScenario
from EXIT.scenarios.negative import NegativeScenario
from EXIT.scenarios.breakeven import PositionTTLClose
from EXIT.interference import Interference
from EXIT.extrime_close import ExtrimeClose
from ENTRY.funding_manager import FundingManager
from ANALYTICS.tracker import PerformanceTracker, format_duration

from c_log import UnifiedLogger
from utils import get_config_summary

if TYPE_CHECKING:
    from API.PHEMEX.stakan import DepthTop
    from ENTRY.pattern_math import EntrySignal

logger = UnifiedLogger("bot")
BASE_DIR = Path(__file__).resolve().parent.parent
CFG_PATH = BASE_DIR / "cfg.json"


class TradingBot:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        
        self.max_active_positions = self.cfg["app"]["max_active_positions"]
        self.quota_asset = self.cfg["quota_asset"]
        
        self.signal_timeout_sec = self.cfg["entry"]["signal_timeout_sec"]
        self.hedge_mode = self.cfg["risk"]["hedge_mode"]
        upd_sec = self.cfg["entry"]["pattern"]["binance"]["update_prices_sec"]
        
        api_key = os.getenv("API_KEY") or self.cfg["credentials"]["api_key"]
        api_secret = os.getenv("API_SECRET") or self.cfg["credentials"]["api_secret"]

        self.bl_manager = BlackListManager(CFG_PATH, self.quota_asset)        
        self.black_list = self.bl_manager.load_from_config(self.cfg.get("black_list", []))
        
        self.state = BotState(black_list=self.black_list)    
        self.tracker = PerformanceTracker(self.state)   
        self._is_running = False

        self.phemex_sym_api = PhemexSymbols()
        self.binance_ticker_api = BinanceTickerAPI()
        self.phemex_ticker_api = PhemexTickerAPI()        
        self.phemex_funding_api = PhemexFunding()
        self.binance_funding_api = BinanceFunding()
        self.session = aiohttp.ClientSession()

        self.price_manager = PriceCacheManager(self.binance_ticker_api, self.phemex_ticker_api, upd_sec)
        self.private_client = PhemexPrivateClient(api_key, api_secret, self.session)
        self.private_ws = PhemexPrivateWS(api_key, api_secret)

        tg_cfg = self.cfg.get("tg", {})
        if tg_cfg.get("enable"):
            from TG.tg_sender import TelegramSender
            token = os.getenv("TELEGRAM_TOKEN") or tg_cfg["token"]
            chat_id = os.getenv("TELEGRAM_CHAT_ID") or tg_cfg["chat_id"]
            self.tg = TelegramSender(token, chat_id)
        else:
            self.tg = None

        self.funding_manager = FundingManager(
            self.cfg["entry"]["pattern"],
            self.phemex_funding_api,
            self.binance_funding_api
        )

        self.signal_engine = SignalEngine(self.cfg["entry"], self.funding_manager)

        self._stream: PhemexStakanStream | None = None
        self._processing: Set[str] = set()
        self._latest_market_data: Dict[str, DepthTop] = {}
        self._signal_timeouts: Dict[str, float] = {}
        self.cfg_manager = ConfigManager(CFG_PATH, self)
        
        self.active_positions_locker: Dict[str, asyncio.Lock] = {} 

        self.ws_handler = WsInterpreter(state=self.state, active_positions_locker=self.active_positions_locker) 
        self.executor = OrderExecutor(self)
        
        exit_cfg = self.cfg["exit"]
        scen_cfg = exit_cfg["scenarios"]
        
        self.scen_base = BaseScenario(scen_cfg["base"])
        self.scen_neg = NegativeScenario(scen_cfg["negative"])
        self.scen_ttl = PositionTTLClose(scen_cfg["breakeven_ttl_close"], self.active_positions_locker)
        self.scen_interf = Interference(exit_cfg["interference"])
        self.scen_extrime = ExtrimeClose(exit_cfg["extrime_close"])

        self.base_order_timeout_sec = scen_cfg["base"]["order_timeout_sec"]
        self.breakeven_order_timeout_sec = scen_cfg["breakeven_ttl_close"]["order_timeout_sec"]
        self.interference_order_timeout_sec = exit_cfg["interference"]["order_timeout_sec"]
        self.extrime_order_timeout_sec = exit_cfg["extrime_close"]["order_timeout_sec"]     
        self.min_quarantine_threshold_usdt = -abs(self.cfg["risk"]["quarantine"]["min_quarantine_threshold_usdt"])
        self.force_quarantine_threshold_usdt = -abs(self.cfg["risk"]["quarantine"]["force_quarantine_threshold_usdt"])

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
            try: limit_time = float(q_val)
            except (ValueError, TypeError): limit_time = 0.0

            if time.time() > limit_time:
                del self.state.quarantine_until[symbol]
                self.state.consecutive_fails[symbol] = 0
                asyncio.create_task(self.state.save()) 
                return True            
            elif not any(k.startswith(f"{symbol}_") for k in self.state.active_positions): 
                return False
        return True
        
    def apply_entry_quarantine(self, symbol: str):
        q_hours = self.cfg.get("entry", {}).get("quarantine", {}).get("quarantine_hours", 1)
        if str(q_hours).lower() == "inf":
            self.state.quarantine_until[symbol] = "inf"
            logger.warning(f"[{symbol}] 🚫 Помещен в бессрочный карантин (вход не удался).")
        elif float(q_hours) > 0:
            self.state.quarantine_until[symbol] = time.time() + (float(q_hours) * 3600)
            logger.warning(f"[{symbol}] 🚫 Помещен в карантин на {q_hours}ч (вход не удался).")
        asyncio.create_task(self.state.save())

    def apply_loss_quarantine(self, symbol: str, trade_pnl: float):
        q_cfg = self.cfg.get("risk", {}).get("quarantine", {})
        max_fails = q_cfg.get("max_consecutive_fails", 1)
        q_hours = q_cfg.get("quarantine_hours", "inf")

        self.state.consecutive_fails[symbol] = self.state.consecutive_fails.get(symbol, 0) + 1
        quarantine_condition = (
            (self.state.consecutive_fails[symbol] >= max_fails and
            trade_pnl <= self.min_quarantine_threshold_usdt) or (trade_pnl <= self.force_quarantine_threshold_usdt)
        )
        if quarantine_condition:
            if str(q_hours).lower() == "inf": self.state.quarantine_until[symbol] = "inf" 
            else: self.state.quarantine_until[symbol] = time.time() + (float(q_hours) * 3600)
            logger.warning(f"[{symbol}] 💀 Карантин ПОТЕРЬ: {self.state.consecutive_fails[symbol]} фейлов. Блокировка на {q_hours}ч.")
        asyncio.create_task(self.state.save())

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

            keys_to_remove = [k for k in list(self.state.active_positions.keys()) if k not in exchange_positions]
            for k in keys_to_remove:
                pos = self.state.active_positions.get(k)
                
                # 👇 БАГФИКС: Мягкое удаление для спасения аналитики
                if pos and pos.in_position:
                    pos.is_closed_by_exchange = True # Отдаем на растерзание Game Loop
                else:
                    self.state.active_positions.pop(k, None) # Удаляем только пустые/фантомные

            for pos_key, ex_data in exchange_positions.items():
                if pos_key in self.state.active_positions:
                    self.state.active_positions[pos_key].current_qty = ex_data["size"]
                    self.state.active_positions[pos_key].in_position = True
                    self.state.active_positions[pos_key].in_pending = False

            await self.state.save()
            logger.info(f"✅ Стейт синхронизирован. В памяти {len(self.state.active_positions)} активных позиций.")
        except Exception as e:
            logger.error(f"❌ Ошибка Recovery: {e}")

    def _get_lock(self, pos_key: str) -> asyncio.Lock:
        if pos_key not in self.active_positions_locker:
            self.active_positions_locker[pos_key] = asyncio.Lock()
        return self.active_positions_locker[pos_key]

    def _check_risk_limits(self, symbol: str) -> bool:
        working_symbols = set()
        has_long, has_short = False, False
        
        for pos_key, pos in self.state.active_positions.items():
            if pos.in_position or pos.in_pending:
                working_symbols.add(pos.symbol)
                if pos.symbol == symbol:
                    if pos.side == "LONG": has_long = True
                    if pos.side == "SHORT": has_short = True

        if not self.hedge_mode and (has_long or has_short):
            return False

        if symbol in working_symbols:
            return True

        if len(working_symbols) >= self.max_active_positions:
            return False
            
        return True
    
    # --- СЕТЕВЫЕ ОПЕРАЦИИ (ВНЕ ЛОКА) ---
    async def _payloader(self, action_payload: List[Tuple], symbol: str) -> None:
        
        async def process_action(action: Tuple):
            cmd = action[0]

            if cmd in ("EXTREME", "BREAKEVEN", "HUNTING"):
                price, timeout, pos_key = action[1], action[2], action[3]
                
                try:
                    # Уходим в сеть (вне лока)
                    success = await self.executor.execute_exit(symbol, pos_key, price, timeout)
                finally:
                    # ГАРАНТИРОВАННО снимаем флаг полета после возврата управления
                    async with self._get_lock(pos_key):
                        p = self.state.active_positions.get(pos_key)
                        if p:
                            p.exit_in_flight = False
                            p.last_exit_status = p.exit_status
                            
                            # 👇 Эгоистичный сброс: HUNTING сбрасывает ТОЛЬКО HUNTING
                            if p.exit_status == "HUNTING":
                                p.exit_status = "NORMAL"

            elif cmd == "INTERFERENCE":
                price, qty, interf_timeout, pos_key = action[1], action[2], action[3], action[4]

                async with self._get_lock(pos_key):
                    p = self.state.active_positions.get(pos_key)
                    if not p or not p.in_position or p.current_qty <= 0:
                        if p:
                            p.interf_in_flight = False
                        return

                try:
                    # Уходим в сеть (вне лока)
                    filled_qty = await self.executor.interf_bought(symbol, pos_key, qty, price, interf_timeout)
                finally:
                    # ГАРАНТИРОВАННО обрабатываем результат и снимаем флаг
                    async with self._get_lock(pos_key):
                        p = self.state.active_positions.get(pos_key)
                        if p:
                            p.interf_in_flight = False
                            # exit_status больше не сбрасываем, так как мы его не устанавливали
                                
                            if filled_qty is not None and filled_qty > 0:
                                p.interf_comulative_qty += filled_qty
                                max_rem = getattr(p, 'max_allowed_remains', 0.0)
                                pct = (p.interf_comulative_qty / max_rem * 100) if max_rem > 0 else 0
                                logger.info(f"[{pos_key}] Успешная скупка помех. Куплено: {filled_qty:.4f}. Cumulative: {p.interf_comulative_qty:.4f} ({pct:.1f}% лимита)")

        # Параллельный запуск всех экшенов. gather дождется всех, но флаги снимутся асинхронно!
        tasks = [process_action(act) for act in action_payload]
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for res in results:
                if isinstance(res, Exception):
                    logger.error(f"Критическая ошибка внутри _payloader: {res}")

    # --- ЛОГИКА АНАЛИЗА И ВЫХОДА ---
    async def _evaluate_exit_scenarios(self, snap: DepthTop, symbol: str, long_key: str, short_key: str) -> None:
        now = time.time()
        actions_to_execute: List[Tuple] = []

        for pos_key in (long_key, short_key):
            async with self._get_lock(pos_key):
                pos = self.state.active_positions.get(pos_key)
                if not pos or not pos.in_position or pos.current_qty <= 0:
                    continue

                is_extrime = pos.exit_status == "EXTREME"
                is_breakeven = pos.exit_status == "BREAKEVEN"

                ttl_res = None
                if not is_extrime:
                    ttl_res = await self.scen_ttl.scen_ttl_analyze(pos, now)

                # 1. ПРИОРИТЕТ 1: EXTREME
                if is_extrime or ttl_res == "BREAKEVEN_EXTRIME" or \
                   self.scen_neg.scen_neg_analyze(snap, pos, now) == "NEGATIVE_TIMEOUT":
                    
                    pos.exit_status = "EXTREME"
                    # Если сеть не занята выходом — бьем
                    if not pos.exit_in_flight:
                        ext_price = self.scen_extrime.scen_extrime_analyze(snap, pos, now)
                        if ext_price:
                            pos.exit_in_flight = True
                            pos.last_extrime_try_ts = now
                            pos.extrime_retries_count += 1
                            actions_to_execute.append(("EXTREME", ext_price, self.extrime_order_timeout_sec, pos_key))
                    continue # ЖЕСТКИЙ СКИП

                # 2. ПРИОРИТЕТ 2: BREAKEVEN
                elif is_breakeven or ttl_res == "BREAKEVEN":
                    
                    pos.exit_status = "BREAKEVEN"
                    if not getattr(pos, 'breakeven_start_ts', None):
                        pos.breakeven_start_ts = now
                        
                    if not pos.exit_in_flight:
                        be_price = self.scen_ttl.build_target_price(pos)
                        if be_price:
                            pos.exit_in_flight = True
                            logger.debug(f"[{pos_key}] Попытка закрыть позицию в безубыток (BREAKEVEN)...")
                            actions_to_execute.append(("BREAKEVEN", be_price, self.breakeven_order_timeout_sec, pos_key))
                    continue # ЖЕСТКИЙ СКИП

                # 3. НОРМАЛЬНЫЙ РЕЖИМ: Охота (HUNTING)
                if not pos.exit_in_flight:
                    base_price = self.scen_base.scen_base_analyze(snap, pos, now)
                    if base_price:
                        pos.exit_status = "HUNTING"
                        pos.exit_in_flight = True
                        logger.debug(f"[{pos_key}] Попытка охоты (HUNTING)...")
                        actions_to_execute.append(("HUNTING", base_price, self.base_order_timeout_sec, pos_key))

                # 4. СКУПКА (INTERFERENCE) - Истинный параллельный поток
                if not pos.interf_in_flight:
                    interf_res = self.scen_interf.scen_interf_analyze(snap, pos, now)
                    if interf_res:
                        i_price, i_qty = interf_res
                        # ВАЖНО: Больше не трогаем pos.exit_status! Скупка - это не выход.
                        pos.interf_in_flight = True
                        logger.debug(f"[{pos_key}] Попытка скупки помех (INTERFERENCE)...")
                        actions_to_execute.append(("INTERFERENCE", i_price, i_qty, self.interference_order_timeout_sec, pos_key))

        # --- СЕТЕВЫЕ ОПЕРАЦИИ (ВНЕ ЛОКА) ---
        if actions_to_execute:
            await self._payloader(actions_to_execute, symbol)

    async def _evaluate_entry_signal(self, snap: DepthTop, symbol: str) -> None:
        b_price, p_price = self.price_manager.get_prices(symbol)
        signal: "EntrySignal" = self.signal_engine.analyze(snap, b_price, p_price)
        if not signal: return

        pos_key = f"{symbol}_{signal.side}"
        if pos_key in self.state.active_positions: 
            return 

        if self._signal_timeouts.get(pos_key, 0) > time.time(): return
        self._signal_timeouts[pos_key] = time.time() + self.signal_timeout_sec

        try:            
            async with self._get_lock(pos_key):
                if pos_key not in self.state.active_positions:
                    self.state.active_positions[pos_key] = ActivePosition(
                        symbol=symbol, side=signal.side, pending_qty=0.0,
                        in_pending=True,
                        in_position=False,
                        init_ask1=signal.init_ask1,
                        init_bid1=signal.init_bid1,
                        mid_price=signal.mid_price,
                        base_target_price_100=signal.base_target_price_100
                    )

            success = await self.executor.execute_entry(symbol, pos_key, signal)
            
            if success:
                await self.state.save()
            else:
                self.apply_entry_quarantine(symbol)
                async with self._get_lock(pos_key):
                    p = self.state.active_positions.get(pos_key)
                    if p:
                        p.in_pending = False
                        if not getattr(p, 'in_position', False):
                            p.marked_for_death_ts = time.time()

        except Exception as e:
            logger.error(f"[{pos_key}] Ошибка постановки входа: {e}")
            async with self._get_lock(pos_key):
                if pos_key in self.state.active_positions:
                    p = self.state.active_positions[pos_key]
                    p.in_pending = False
                    if not getattr(p, 'in_position', False):
                        p.marked_for_death_ts = time.time()

    async def _process_symbol_pipeline(self, snap: DepthTop):
        symbol = snap.symbol
        if symbol in self._processing: return
        self._processing.add(symbol)
        try:
            if symbol in self.black_list: return
            if hasattr(self, 'quarantine_util') and not await self.quarantine_util(symbol): return
            
            pos_long_key, pos_short_key = f"{symbol}_LONG", f"{symbol}_SHORT"
            await self._evaluate_exit_scenarios(snap, symbol, pos_long_key, pos_short_key)
            
            if not self._check_risk_limits(symbol): return
            await self._evaluate_entry_signal(snap, symbol)
        except Exception as e:
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
                    try:
                        pos: "ActivePosition" = self.state.active_positions.get(pos_key)
                        if not pos: continue

                        # --- СБОРЩИК ФАНТОМНЫХ ВХОДОВ ---
                        if getattr(pos, 'marked_for_death_ts', 0) > 0:
                            if pos.in_position:
                                pos.marked_for_death_ts = 0.0 
                            elif time.time() - pos.marked_for_death_ts > 5.0:
                                logger.debug(f"[{pos_key}] 🗑 Удаление фантомной позиции (no WS fill).")
                                self.state.active_positions.pop(pos_key, None)
                                self.active_positions_locker.pop(pos_key, None)
                                asyncio.create_task(self.state.save())
                                continue

                        if getattr(pos, 'is_closed_by_exchange', False):
                            if pos.entry_price > 0.0:
                                
                                exit_pr = pos.realized_exit_price if pos.realized_exit_price > 0 else (pos.exit_price_hint or pos.avg_price)
                                duration_sec = time.time() - pos.opened_at
                                
                                net_pnl, is_win = self.tracker.register_trade(
                                    symbol=pos.symbol,
                                    side=pos.side,
                                    entry_price=pos.avg_price if pos.avg_price > 0 else pos.entry_price,
                                    exit_price=exit_pr,
                                    qty=pos.max_realized_qty,
                                    duration_sec=duration_sec
                                )

                                emoji = "💵" if is_win else "🩸"

                                current_status = pos.exit_status if pos.exit_status in ("EXTREME", "BREAKEVEN") else pos.last_exit_status
                                
                                if current_status == "EXTREME": 
                                    semantic = "⚠️ Аварийный выход (EXTREME Mode)"
                                elif current_status == "BREAKEVEN": 
                                    semantic = "🛡 Выход по безубытку (TTL)"                                
                                else: 
                                    semantic = "🎯 Тейк-профит" if is_win else "📉 Убыток (Ручное/Неизвестно)"

                                if is_win:
                                    self.state.consecutive_fails[pos.symbol] = 0
                                    self.state.quarantine_until.pop(pos.symbol, None)
                                else:
                                    self.apply_loss_quarantine(pos.symbol, net_pnl)

                                if self.tg:
                                    msg = Reporters.exit_success(pos_key, semantic, exit_pr)
                                    msg += f"\n⏳ Время в сделке: {format_duration(duration_sec)}"
                                    asyncio.create_task(self.tg.send_message(msg))
                                    
                                logger.info(f"[{pos_key}] 🛑 Позиция закрыта. {emoji} PnL: {net_pnl:.4f}$ | Время: {format_duration(duration_sec)}")

                            self.state.active_positions.pop(pos_key, None)
                            self.active_positions_locker.pop(pos_key, None) 
                            asyncio.create_task(self.state.save())

                    except Exception as e:
                        logger.error(f"[{pos_key}] 💥 Критическая ошибка при обработке позиции в Game Loop: {e}\n{traceback.format_exc()}")

            current_snaps = list(self._latest_market_data.values())
            if not current_snaps:
                await asyncio.sleep(0.01)
                continue
            tasks = [self._process_symbol_pipeline(snap) for snap in current_snaps]
            await asyncio.gather(*tasks, return_exceptions=False)
            await asyncio.sleep(0.01)

    async def _on_ws_subscribe(self):
        """Срабатывает при первом подключении и каждом успешном реконнекте WS"""
        logger.info("🔄 WS Подписан на приватный канал. Запуск синхронизации стейта (Recover)...")
        await self._recover_state()

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

        # ==========================================
        # ИНИЦИАЛИЗАЦИЯ ФИНАНСОВОГО АУДИТА
        # ==========================================
        try:
            if hasattr(self, 'tracker'):
                saved_start_balance = self.tracker.data.get("start_balance", 0.0)
                
                if saved_start_balance > 0:
                    logger.info(f"💰 Стартовый баланс успешно загружен из стейта: {saved_start_balance:.2f} {self.quota_asset}")
                    # Всегда пересчитываем max_profit и mdd из истории при рестарте
                    self.tracker._recalc_from_history()
                    await self.state.save()
                else:
                    logger.info("📡 Запрашиваем Equity с биржи для инициализации аудита...")
                    usd_balance = await self.private_client.get_equity(self.quota_asset)
                    
                    self.tracker.set_initial_balance(usd_balance)  # внутри тоже вызывает _recalc_from_history
                    logger.info(f"💰 Стартовый баланс зафиксирован: {usd_balance:.2f} {self.quota_asset}")
                    await self.state.save()
                    
        except Exception as e:
            logger.error(f"❌ Не удалось получить/установить стартовый баланс: {e}")
        # ==========================================

        logger.info("🔄 Прогрев кэша цен и фандинга...")
        await self.price_manager.warmup()
        self._price_updater_task = asyncio.create_task(self.price_manager.loop())
        await self._await_task(getattr(self, '_funding_task', None))
        self._funding_task = asyncio.create_task(self.funding_manager.run())
        await asyncio.sleep(1)

        self._private_ws_task = asyncio.create_task(
            self.private_ws.run(
                self.ws_handler.process_phemex_message,
                on_subscribe=self._on_ws_subscribe
            )
        )

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
        self.funding_manager.stop()
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