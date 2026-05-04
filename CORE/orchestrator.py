# ============================================================
# FILE: CORE/orchestrator.py
# ROLE: Главный оркестратор торговых процессов (Game Loop Pattern)
# ============================================================

from __future__ import annotations
import asyncio
import time
import traceback
from typing import Dict, Any, Set, TYPE_CHECKING, List, Tuple, Optional
import os
from curl_cffi.requests import AsyncSession
from pathlib import Path

from API.PHEMEX.symbol import PhemexSymbols
from API.PHEMEX.stakan import PhemexStakanStream
from API.PHEMEX.order import PhemexPrivateClient
from API.PHEMEX.ws_private import PhemexPrivateWS
from API.BINANCE.ticker import BinanceTickerAPI
from API.PHEMEX.ticker import PhemexTickerAPI
from API.PHEMEX.funding import PhemexFunding
from API.BINANCE.funding import BinanceFunding
from API.BINANCE.stakan import BinanceStakanStream, DepthTop as BinanceDepthTop
from API.DEX.dexscreener import DexscreenerAPI

from ENTRY.signal_engine import SignalEngine
from CORE.restorator import BotState
from CORE.executor import OrderExecutor
from CORE.models_fsm import WsInterpreter, ActivePosition, EntryPayload
from CORE._utils import PriceCacheManager, ConfigManager, Reporters, SymbolListManager, RiskManager, AnalyticsManager, DexUpdater, TradeManager

from EXIT.scenarios.base import BaseScenario
from EXIT.scenarios.negative import NegativeScenario
from EXIT.scenarios.breakeven import PositionTTLClose
from EXIT.interference import Interference
from EXIT.extrime_close import ExtrimeClose
from ENTRY.funding_manager import FundingManager
from ANALYTICS.tracker import PerformanceTracker

from c_log import UnifiedLogger
from utils import get_config_summary

from API.PHEMEX.klines import PhemexKlinesAPI
from CORE.rsi_manager import RSIManager

if TYPE_CHECKING:
    from API.PHEMEX.stakan import DepthTop

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
        
        # binance_trigger update interval
        upd_sec = self.cfg["entry"]["pattern"]["binance_trigger"]["update_prices_sec"]
        
        api_key = os.getenv("API_KEY") or self.cfg["credentials"]["api_key"]
        api_secret = os.getenv("API_SECRET") or self.cfg["credentials"]["api_secret"]

        self.symbol_manager = SymbolListManager(CFG_PATH, self.quota_asset)
        self.symbol_manager.load_from_config(self.cfg["black_list"], self.cfg["white_list"])
        
        self.state = BotState(black_list=self.symbol_manager.black_list, white_list=self.symbol_manager.white_list)
        self.tracker = PerformanceTracker(self.state)   
        self._is_running = False

        self.session = AsyncSession(
            impersonate="chrome120",
            http_version=2,
            verify=True
        )

        self.phemex_sym_api = PhemexSymbols(session=self.session)
        self.binance_ticker_api = BinanceTickerAPI(session=self.session)
        self.phemex_ticker_api = PhemexTickerAPI(session=self.session)        
        self.phemex_funding_api = PhemexFunding(session=self.session)
        self.binance_funding_api = BinanceFunding(session=self.session)
        self.klines_api = PhemexKlinesAPI(session=self.session)
        self.rsi_manager = RSIManager(self.klines_api, self.cfg["entry"]["rsi_filter"])

        self.private_client = PhemexPrivateClient(api_key, api_secret, self.session)
        self.private_ws = PhemexPrivateWS(api_key, api_secret)

        tg_cfg = self.cfg["tg"]
        if tg_cfg["enable"]:
            from TG.tg_sender import TelegramSender
            token = os.getenv("TELEGRAM_TOKEN") or tg_cfg["token"]
            chat_id = os.getenv("TELEGRAM_CHAT_ID") or tg_cfg["chat_id"]
            self.tg = TelegramSender(token, chat_id)
        else:
            self.tg = None

        self.dex_api = DexscreenerAPI(session=self.session)
        self.risk_manager = RiskManager(self.state, self.cfg)
        self.analytics = AnalyticsManager()
        self.trade_manager = TradeManager(self.tracker, self.risk_manager, self.analytics, self.tg)
        
        self.dex_upd_sec = self.cfg["exit"]["scenarios"]["base"]["update_prices_sec"]
        self.dex_updater = DexUpdater(self.dex_api, None, self.state, self.dex_upd_sec)

        # --- ОТЧЕТНОСТЬ (как в uranus) ---
        report_id = os.getenv("REPORT_CHAT_ID")
        self.report_tg = TelegramSender(
            os.getenv("TELEGRAM_TOKEN") or tg_cfg["token"],
            report_id
        ) if report_id else None
        self.report_interval_hours = self.cfg["app"]["report_interval_hours"]

        self._stream: PhemexStakanStream | None = None
        self._processing: Set[str] = set()
        self._latest_market_data: Dict[str, DepthTop] = {}
        self._signal_timeouts: Dict[str, float] = {}
        self.cfg_manager = ConfigManager(CFG_PATH, self)
        
        self.active_positions_locker: Dict[str, asyncio.Lock] = {} 

        self.ws_handler = WsInterpreter(state=self.state, active_positions_locker=self.active_positions_locker) 
        self.executor = OrderExecutor(self)
        self._entry_lock = asyncio.Lock()
        
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

    @property
    def black_list(self) -> List[str]:
        return self.symbol_manager.black_list

    @property
    def white_list(self) -> List[str]:
        return self.symbol_manager.white_list

    async def _await_task(self, task: asyncio.Task | None):
        if not task: return
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
        except Exception as e: logger.debug(f"Task shutdown note: {e}")

    async def _on_ws_subscribe(self):
        """Срабатывает при первом подключении и каждом успешном реконнекте WS"""
        logger.info("🔄 WS Подписан на приватный канал. Запуск синхронизации стейта (Recover)...")
        await self.state.sync_with_exchange(self.private_client)

    def _get_lock(self, pos_key: str) -> asyncio.Lock:
        if pos_key not in self.active_positions_locker:
            self.active_positions_locker[pos_key] = asyncio.Lock()
        return self.active_positions_locker[pos_key]
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
        
        # 1. Проверяем наличие активной позиции ПЕРЕД расчетами
        pos_l = self.state.active_positions.get(long_key)
        pos_s = self.state.active_positions.get(short_key)
        has_pos = (pos_l and pos_l.in_position) or (pos_s and pos_s.in_position)
        if not has_pos:
            return

        actions_to_execute: List[Tuple] = []

        b_price, p_price = self.price_manager.get_prices(symbol)
        if p_price <= 0:
            return

        # Данные для расчета спреда
        p_bid1, p_ask1 = snap.bids[0][0], snap.asks[0][0]
        b_depth = self.price_manager.get_binance_depth(symbol)
        dex_price = self.price_manager.get_dex_price(symbol)

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
                            pos.exit_reason = "EXTREME"
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
                            pos.exit_reason = "BREAKEVEN"
                            actions_to_execute.append(("BREAKEVEN", be_price, self.breakeven_order_timeout_sec, pos_key))
                    continue # ЖЕСТКИЙ СКИП

                # 3. НОРМАЛЬНЫЙ РЕЖИМ: Охота (HUNTING)
                if not pos.exit_in_flight:
                    base_price = self.scen_base.scen_base_analyze(snap, pos, now)
                    if base_price:
                        pos.exit_status = "HUNTING"
                        pos.exit_in_flight = True
                        logger.debug(f"[{pos_key}] Попытка охоты (HUNTING)...")
                        pos.exit_reason = "HUNTING"
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
        if not self.symbol_manager.is_allowed(symbol):
            return
            
        if not self.risk_manager.check_risk_limits(symbol, self.hedge_mode, self.max_active_positions):
            return

        # 3. Сигнал на вход (только если нет позиции и нет лока)
        b_price, p_price = self.price_manager.get_prices(symbol)
        b_depth = self.price_manager.get_binance_depth(symbol)
        b_fair, p_fair = self.price_manager.get_fair_prices(symbol)
        
        signal: "EntryPayload" = await self.signal_engine.analyze(snap, b_price, p_price, b_depth, b_fair, p_fair)
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
                self.risk_manager.apply_entry_quarantine(symbol)
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
            if not await self.risk_manager.is_in_quarantine(symbol):
                return
                
            pos_long_key, pos_short_key = f"{symbol}_LONG", f"{symbol}_SHORT"
            await self._evaluate_exit_scenarios(snap, symbol, pos_long_key, pos_short_key)
            
            async with self._entry_lock:
                if not self.risk_manager.check_risk_limits(symbol, self.hedge_mode, self.max_active_positions):
                    return
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
                            reason_str = f" [{pos.exit_reason}]" if pos.exit_reason else " [EXTERNAL/MANUAL]"
                            logger.info(f"[{pos_key}] 🏁 Выход выполнен{reason_str}. Объем: {pos.max_realized_qty} (≈ {round(pos.max_realized_qty * pos.realized_exit_price, 2)} $)")
                            # Обработка закрытия через TradeManager
                            await self.trade_manager.process_position_closure(
                                pos_key, pos, 
                                self.min_quarantine_threshold_usdt, 
                                self.force_quarantine_threshold_usdt
                            )
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
        await self.state.sync_with_exchange(self.private_client)

    async def _on_binance_depth(self, d: BinanceDepthTop):
        """Коллбэк для потока стаканов Binance"""
        if d.bids and d.asks:
            # Обновляем кэш цен (полный объект DepthTop)
            self.price_manager.binance_depth[d.symbol] = d

    async def start(self):
        if getattr(self, '_is_running', False): return
        self._is_running = True
        
        # 0. Загрузка сохраненного стейта (позиции, аналитика)
        self.state.load()
        
        # Синхронизация баланса для аналитики
        if self.tracker.data["start_balance"] == 0.0:
            try:
                equity = await self.private_client.get_equity(self.quota_asset)
                self.tracker.set_initial_balance(equity)
                logger.info(f"💰 Инициализация баланса (Equity): {equity:.2f} {self.quota_asset}")
            except Exception as e:
                logger.warning(f"⚠️ Не удалось получить баланс для аналитики: {e}")
        else:
            # Если баланс уже есть, просто пересчитываем метрики из истории
            self.tracker._recalc_from_history()
            logger.info(f"📊 Аналитика восстановлена. Текущий PnL: {self.tracker.data['total_pnl']:.2f} $")

        logger.info("▶️ Инициализация систем...")
        summary = get_config_summary(self.cfg)
        logger.info(f"⚙️ БОТ ЗАПУЩЕН С НАСТРОЙКАМИ\n{summary}")
        if self.tg: asyncio.create_task(self.tg.send_message("🟢 <b>ТОРГОВЛЯ НАЧАТА</b>"))

        # 1. Сбор и фильтрация символов
        all_phm = await self.phemex_sym_api.get_all(quote=self.symbol_manager.quota_asset, only_active=True)
        self.active_symbols = set(self.symbol_manager.get_filtered_list([s.symbol for s in all_phm if s]))
        self.symbol_specs = {s.symbol: s for s in all_phm if s and s.symbol in self.active_symbols}
        
        if not self.active_symbols:
            logger.error("❌ Нет доступных символов для торговли (проверьте WhiteList/BlackList)")
            self._is_running = False
            return

        # 2. Инициализация менеджеров данных с финальным списком
        self.price_manager = PriceCacheManager(
            self.binance_ticker_api, self.phemex_ticker_api, 
            self.active_symbols, 
            upd_sec=self.cfg.get("price_update_sec", 0.2),
            rsi_manager=self.rsi_manager
        )
        
        # Поток стаканов Binance (USDT-M)
        self.binance_stream = BinanceStakanStream(list(self.active_symbols), chunk_size=50)
        self._binance_stream_task: Optional[asyncio.Task] = None
        
        self.funding_manager = FundingManager(self.cfg["entry"]["pattern"], self.phemex_funding_api, self.binance_funding_api, self.symbol_manager)
        self.signal_engine = SignalEngine(self.cfg["entry"], self.funding_manager, self.rsi_manager, self.dex_api)

        # Подключаем price_manager к dex_updater
        self.dex_updater.price_manager = self.price_manager

        # Прогрев RSI (загрузка истории свечей)
        if self.rsi_manager:
            logger.info("⏳ Синхронизация RSI (Warmup)...")
            await self.rsi_manager.warmup(list(self.active_symbols))
            self._rsi_sync_task = asyncio.create_task(self.rsi_manager.background_loop(list(self.active_symbols)))

        await self.state.sync_with_exchange(self.private_client)

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
        # 4. Фоновые циклы
        self._price_updater_task = asyncio.create_task(self.price_manager.loop())
        self._funding_task = asyncio.create_task(self.funding_manager.run())
        self._binance_stream_task = asyncio.create_task(self.binance_stream.run(self._on_binance_depth))
        self._game_loop_task = asyncio.create_task(self._main_trading_loop())
        
        # Запускаем фоновое обновление DEX цен для открытых позиций
        self._dex_updater_task = asyncio.create_task(self.dex_updater.run())
        
        await asyncio.sleep(1)

        self._private_ws_task = asyncio.create_task(
            self.private_ws.run(
                self.ws_handler.process_phemex_message,
                on_subscribe=self._on_ws_subscribe
            )
        )

        # 3. Подписка WebSocket
        self._stream = PhemexStakanStream(symbols=list(self.active_symbols), depth=10, chunk_size=40)
        self._stream_task = asyncio.create_task(self._stream.run(self._stakan_data_sink))

        self._report_task = asyncio.create_task(self._periodic_report_loop())

    async def _periodic_report_loop(self):
        """Фоновый луп для отправки отчетов каждые N часов."""
        logger.info(f"📊 Цикл отчетов запущен (каждые {self.report_interval_hours}ч)")
        while self._is_running:
            try:
                await asyncio.sleep(self.report_interval_hours * 3600)
                if self._is_running:
                    await self.analytics.send_developer_report(self.tg, self.tracker, self.risk_manager)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in report loop: {e}")
                await asyncio.sleep(60)

    async def aclose(self):
        await self.stop()
        logger.info("💾 Финальное сохранение стейта на диск...")
        await self.state.save()
        
        # 1. Закрываем ресурсы с собственными сессиями (WS, TG)
        await self.private_ws.aclose()
        if self.tg: 
            await self.tg.aclose()
        await self.klines_api.aclose()
        
        # 2. Закрываем единую сессию для всех REST-клиентов
        if self.session:
            try:
                await self.session.close()
                logger.info("🌐 Shared session closed successfully.")
            except Exception as e:
                logger.debug(f"Session close note: {e}")

    async def stop(self):
        if not getattr(self, '_is_running', False): return
        self._is_running = False
        logger.info("⏹ Остановка процессов...")
        if self.tg: await self.tg.send_message("⏹ Остановка процессов...")
        self.price_manager.stop()
        self.funding_manager.stop()
        self.rsi_manager.stop()
        self.binance_stream.stop()
        if self._stream: self._stream.stop()
        await self.private_ws.aclose()
        
        # Ожидаем завершения всех задач
        await self._await_task(getattr(self, '_price_updater_task', None))
        await self._await_task(getattr(self, '_funding_task', None))
        await self._await_task(getattr(self, '_binance_stream_task', None))
        await self._await_task(getattr(self, '_private_ws_task', None))
        await self._await_task(getattr(self, '_stream_task', None))
        await self._await_task(getattr(self, '_game_loop_task', None))
        await self._await_task(getattr(self, '_report_task', None))
        await self._await_task(getattr(self, '_rsi_sync_task', None))
        await self._await_task(getattr(self, '_dex_updater_task', None))
        self._latest_market_data.clear()
        self._processing.clear()
        self._signal_timeouts.clear()
        await self.state.save()