# ============================================================
# FILE: CORE/bot_utils.py
# ROLE: Вспомогательные сервисы (BlackList, PriceCache, Reporters, Config)
# ============================================================
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Dict, List, Tuple, TYPE_CHECKING, Optional, Set, Any
from c_log import UnifiedLogger

if TYPE_CHECKING:
    from ENTRY.signal_engine import SignalEngine
    from CORE.orchestrator import TradingBot
    from API.PHEMEX.ticker import PhemexTickerAPI
    from API.BINANCE.ticker import BinanceTickerAPI
    from CORE.models_fsm import EntryPayload, ActivePosition
    from CORE.rsi_manager import RSIManager
    from API.DEX.dexscreener import DexscreenerAPI
    from CORE.restorator import BotState
    from API.BINANCE.stakan import DepthTop as BinanceDepthTop

logger = UnifiedLogger("core")


class SymbolListManager:
    """Управление списками монет (White/Black): парсинг, фильтрация, сохранение."""
    def __init__(self, cfg_path: Path | str, quota_asset: str = "USDT"):
        self.cfg_path = Path(cfg_path)
        self.quota_asset = quota_asset.upper()
        self.black_list: List[str] = []
        self.white_list: List[str] = []

    def _clean_list(self, raw_symbols: List[str]) -> List[str]:
        clean = []
        for sym in raw_symbols:
            sym = sym.upper().strip()
            if not sym: continue
            full_sym = sym if sym.endswith(self.quota_asset) else sym + self.quota_asset
            if full_sym not in clean:
                clean.append(full_sym)
        return clean

    def load_from_config(self, raw_black: List[str], raw_white: List[str]):
        self.black_list = self._clean_list(raw_black)
        self.white_list = self._clean_list(raw_white)
        logger.info(f"📋 Lists loaded: WhiteList={len(self.white_list)}, BlackList={len(self.black_list)}")

    def is_allowed(self, symbol: str) -> bool:
        symbol = symbol.upper()
        # 1. Черный список безусловен
        if symbol in self.black_list:
            return False
        # 2. Если белый список не пуст, только монеты из него
        if self.white_list:
            return symbol in self.white_list
        # 3. Если белый список пуст, разрешено всё, что не в черном
        return True

    def get_filtered_list(self, all_symbols: List[str]) -> List[str]:
        """Возвращает финальный список рабочих символов из набора доступных на бирже."""
        return [s for s in all_symbols if self.is_allowed(s)]

    def update_and_save_black(self, raw_symbols: List[str]) -> Tuple[bool, str]:
        clean = [s.upper().strip() for s in raw_symbols if s.strip()]
        try:
            with open(self.cfg_path, "r", encoding="utf-8") as f:
                c = json.load(f)
            c["black_list"] = clean
            with open(self.cfg_path, "w", encoding="utf-8") as f:
                json.dump(c, f, indent=4)
            self.black_list = self._clean_list(clean)
            return True, "✅ Черный список успешно обновлен."
        except Exception as e:
            return False, f"❌ Ошибка записи: {e}"


class PriceCacheManager:
    """Асинхронный кэш цен тикеров Binance/Phemex, обновляемый в фоне."""
    def __init__(self, binance_api: 'BinanceTickerAPI', phemex_api: 'PhemexTickerAPI', target_symbols: Set[str], upd_sec: float = 0.2, rsi_manager: Optional['RSIManager'] = None):
        self.binance_api = binance_api
        self.phemex_api = phemex_api
        self.target_symbols = target_symbols
        self.upd_sec = upd_sec
        self.rsi_manager = rsi_manager
        self.binance_prices: Dict[str, float] = {}
        self.phemex_prices: Dict[str, float] = {}
        self.binance_fair_prices: Dict[str, float] = {}
        self.phemex_fair_prices: Dict[str, float] = {}
        self.dex_prices: Dict[str, Tuple[float, float]] = {} # symbol -> (price, timestamp)
        self.binance_depth: Dict[str, 'BinanceDepthTop'] = {} # symbol -> DepthTop
        
        self._is_running = False
        self._last_fetch_ts = 0.0

    async def warmup(self):
        await self._fetch()

    async def _fetch(self):
        try:
            # Используем gather для параллельного получения цен
            b_tickers, p_tickers = await asyncio.gather(
                self.binance_api.get_all_tickers(),
                self.phemex_api.get_all_tickers(),
                return_exceptions=True
            )
            
            if isinstance(b_tickers, dict):
                for sym in self.target_symbols:
                    if sym in b_tickers:
                        t = b_tickers[sym]
                        self.binance_prices[sym] = t.price
                        self.binance_fair_prices[sym] = t.fair_price

            if isinstance(p_tickers, dict):
                for sym in self.target_symbols:
                    if sym in p_tickers:
                        t = p_tickers[sym]
                        self.phemex_prices[sym] = t.price
                        self.phemex_fair_prices[sym] = t.fair_price
                        if self.rsi_manager:
                            self.rsi_manager.update_price(sym, t.price)
                
            self._last_fetch_ts = time.time()
        except Exception as e:
            logger.debug(f"Ошибка фонового обновления цен тикеров: {e}")

    async def loop(self):
        self._is_running = True
        logger.info(f"🚀 PriceCacheManager loop started with interval {self.upd_sec}s")
        while self._is_running:
            start_t = time.monotonic()
            await self._fetch()
            
            # Динамический слип для поддержания частоты
            elapsed = time.monotonic() - start_t
            sleep_t = max(0.01, self.upd_sec - elapsed)
            await asyncio.sleep(sleep_t)

    def stop(self):
        self._is_running = False

    def get_prices(self, symbol: str) -> Tuple[float, float]:
        """Возвращает (BinancePrice, PhemexPrice)"""
        return self.binance_prices.get(symbol, 0.0), self.phemex_prices.get(symbol, 0.0)

    def get_binance_depth(self, symbol: str) -> Optional['BinanceDepthTop']:
        """Возвращает объект DepthTop Binance"""
        return self.binance_depth.get(symbol)

    def get_fair_prices(self, symbol: str) -> Tuple[float, float]:
        """Возвращает (BinanceFairPrice, PhemexFairPrice)"""
        return self.binance_fair_prices.get(symbol, 0.0), self.phemex_fair_prices.get(symbol, 0.0)

    def get_dex_price(self, symbol: str) -> Tuple[float, float]:
        """Возвращает (последняя цена, timestamp) с Dexscreener"""
        return self.dex_prices.get(symbol, (0.0, 0.0))

    def get_all_phemex_prices(self) -> Dict[str, float]:
        return self.phemex_prices


class ConfigManager:
    def __init__(self, cfg_path: Path | str, tb: "TradingBot"):
        self.cfg_path = cfg_path
        self.tb = tb

    def reload_config(self) -> tuple[bool, str]:
        try:
            with open(self.cfg_path, "r", encoding="utf-8") as f:
                new_cfg = json.load(f)

            self.tb.cfg = new_cfg
            self.tb.max_active_positions = self.tb.cfg["app"]["max_active_positions"]
            
            self.tb.symbol_manager.load_from_config(
                self.tb.cfg["black_list"],
                self.tb.cfg["white_list"]
            )
            
            # --- ОБНОВЛЕНИЕ РАБОЧЕГО СПИСКА СИМВОЛОВ ---
            # Нам нужно asyncio для получения всех символов с биржи
            async def update_symbols():
                all_phm = await self.tb.phemex_sym_api.get_all(quote=self.tb.symbol_manager.quota_asset, only_active=True)
                conf_syms = set(self.tb.symbol_manager.get_filtered_list([s.symbol for s in all_phm if s]))
                pos_syms = {p.symbol for p in self.tb.state.active_positions.values()}
                new_active = conf_syms | pos_syms
                
                # Если список изменился — перезапускаем WS
                if new_active != getattr(self.tb, 'active_symbols', set()):
                    logger.info(f"🔄 Список символов изменился ({len(new_active)}). Перезапуск WebSocket...")
                    self.tb.active_symbols = new_active
                    self.tb.symbol_specs = {s.symbol: s for s in all_phm if s and s.symbol in self.tb.active_symbols}
                    
                    if hasattr(self.tb, 'price_manager'):
                        self.tb.price_manager.target_symbols = self.tb.active_symbols
                        
                    if hasattr(self.tb, '_stream') and self.tb._stream:
                        self.tb._stream.stop()
                        if hasattr(self.tb, '_stream_task'):
                            self.tb._stream_task.cancel()
                        
                        from API.PHEMEX.stakan import PhemexStakanStream
                        self.tb._stream = PhemexStakanStream(symbols=list(self.tb.active_symbols), depth=10, chunk_size=40)
                        self.tb._stream_task = asyncio.create_task(self.tb._stream.run(self.tb._stakan_data_sink))
            
            # Запускаем обновление символов
            asyncio.create_task(update_symbols())

            # Обновляем ссылки в стейте (если нужно)
            if hasattr(self.tb.state, 'black_list'):
                self.tb.state.black_list = self.tb.symbol_manager.black_list
            if hasattr(self.tb.state, 'white_list'):
                self.tb.state.white_list = self.tb.symbol_manager.white_list

            # --- ПЕРЕЗАГРУЗКА ИНСТРУМЕНТОВ ВХОДА (ВКЛЮЧАЯ ФАНДИНГ V5) ---
            from ENTRY.funding_manager import FundingManager
            from ENTRY.signal_engine import SignalEngine
            
            # Тормозим старую таску фандинга, если есть
            if hasattr(self.tb, 'funding_manager'):
                self.tb.funding_manager.stop()
                
            old_task = getattr(self.tb, '_funding_task', None)
            if old_task and not old_task.done():
                old_task.cancel()

            # Создаем новый инстанс FundingManager
            self.tb.funding_manager = FundingManager(
                self.tb.cfg["entry"]["pattern"], 
                self.tb.phemex_funding_api, 
                self.tb.binance_funding_api,
                self.tb.symbol_manager
            )
            
            # Пересоздаем движок сигналов (математика стакана обновится здесь же)
            self.tb.signal_engine = SignalEngine(self.tb.cfg["entry"], self.tb.funding_manager, self.tb.price_manager, self.tb.rsi_manager)

            # Перезапускаем луп фандинга
            if getattr(self.tb, '_is_running', False):
                self.tb._funding_task = asyncio.create_task(self.tb.funding_manager.run())

            # --- ОБНОВЛЕНИЕ ПАРАМЕТРОВ EXECUTOR ---
            if hasattr(self.tb, 'executor'):
                self.tb.executor.cfg = self.tb.cfg
                self.tb.executor.entry_timeout = self.tb.cfg["entry"]["entry_timeout_sec"]
                self.tb.executor.max_entry_retries = self.tb.cfg["entry"]["max_place_order_retries"]
                self.tb.executor.max_exit_retries = self.tb.cfg["exit"]["max_place_order_retries"]
                self.tb.executor.min_order_life_sec = self.tb.cfg["exit"]["min_order_life_sec"]
                risk = self.tb.cfg["risk"]
                self.tb.executor.notional_limit = float(risk["notional_limit"])
                self.tb.executor.margin_over_size_pct = float(risk["margin_over_size_pct"])

            # --- ОБНОВЛЕНИЕ PriceCacheManager ---
            if hasattr(self.tb, 'price_manager'):
                new_upd = self.tb.cfg["entry"]["pattern"]["binance_trigger"]["update_prices_sec"]
                self.tb.price_manager.upd_sec = new_upd

            # --- ПЕРЕЗАГРУЗКА СЦЕНАРИЕВ ВЫХОДА ---
            exit_cfg = self.tb.cfg.get("exit", {})
            scen_cfg = exit_cfg.get("scenarios", {})
            
            from EXIT.scenarios.base import BaseScenario
            from EXIT.scenarios.negative import NegativeScenario
            from EXIT.scenarios.breakeven import PositionTTLClose
            from EXIT.interference import Interference
            from EXIT.extrime_close import ExtrimeClose
            
            self.tb.scen_base = BaseScenario(scen_cfg["base"])
            self.tb.scen_neg = NegativeScenario(scen_cfg["negative"])
            self.tb.scen_ttl = PositionTTLClose(scen_cfg["breakeven_ttl_close"], self.tb.active_positions_locker)
            self.tb.scen_interf = Interference(exit_cfg["interference"])
            self.tb.scen_extrime = ExtrimeClose(exit_cfg["extrime_close"])

            self.tb.base_order_timeout_sec = scen_cfg["base"]["order_timeout_sec"]   
            self.tb.breakeven_order_timeout_sec = scen_cfg["breakeven_ttl_close"]["order_timeout_sec"]     
            self.tb.interference_order_timeout_sec = exit_cfg["interference"]["order_timeout_sec"]        
            self.tb.extrime_order_timeout_sec = exit_cfg["extrime_close"]["order_timeout_sec"]

            return True, "Конфигурация успешно обновлена в памяти!"
        except Exception as e:
            logger.error(f"Config reload error: {e}")
            return False, f"Ошибка загрузки в память: {e}"


class Reporters:
    @staticmethod
    def entry_signal(symbol: str, signal: EntryPayload, b_price: float, p_price: float) -> str:
        side_str = "🟢 LONG" if signal.side == "LONG" else "🔴 SHORT"
        spread_info = f"Spread: {signal.spread:.2f}%" if hasattr(signal, 'spread') else ""
        return (
            f"<b>#{symbol}</b> | {side_str}\n"
            f"Вход: <b>{signal.price}</b>\n"
            f"{spread_info}\n\n"
            f"Binance: {b_price} | Phemex: {p_price}"
        )

    @staticmethod
    def extrime_alert(symbol: str, reason: str) -> str:
        return f"🚨 <b>ОТКАЗ API</b>\n#{symbol}\nЭкстрим ордера отклоняются: {reason}"

    @staticmethod
    def exit_success(pos_key: str, semantic: str, price: float, pnl: float = 0.0, emoji: str = "💵") -> str:
        # Форматируем PnL с плюсом для прибыли
        pnl_str = f"+{pnl:.4f}" if pnl > 0 else f"{pnl:.4f}"
        return (
            f"{emoji} <b>{semantic}</b>\n"
            f"#{pos_key}\n"
            f"Цена закрытия: <b>{price}</b>\n"
            f"PnL: <b>{pnl_str}$</b>"
        )


class RiskManager:
    """Управление рисками, лимитами и карантином монет."""
    def __init__(self, state: 'BotState', cfg: Dict[str, Any]):
        self.state = state
        self.cfg = cfg
        
    def check_risk_limits(self, symbol: str, hedge_mode: bool, max_active_positions: int) -> bool:
        working_symbols = set()
        has_long, has_short = False, False
        
        for pos_key, pos in self.state.active_positions.items():
            if pos.in_position or pos.in_pending:
                working_symbols.add(pos.symbol)
                if pos.symbol == symbol:
                    if pos.side == "LONG": has_long = True
                    if pos.side == "SHORT": has_short = True

        if not hedge_mode and (has_long or has_short):
            return False

        if symbol in working_symbols:
            return True

        if len(working_symbols) >= max_active_positions:
            return False
            
        return True

    async def is_in_quarantine(self, symbol: str) -> bool:        
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

    def apply_loss_quarantine(self, symbol: str, trade_pnl: float, min_threshold: float, force_threshold: float):
        q_cfg = self.cfg.get("risk", {}).get("quarantine", {})
        max_fails = q_cfg.get("max_consecutive_fails", 1)
        q_hours = q_cfg.get("quarantine_hours", "inf")

        self.state.consecutive_fails[symbol] = self.state.consecutive_fails.get(symbol, 0) + 1
        quarantine_condition = (
            (self.state.consecutive_fails[symbol] >= max_fails and
            trade_pnl <= min_threshold) or (trade_pnl <= force_threshold)
        )
        if quarantine_condition:
            if str(q_hours).lower() == "inf": self.state.quarantine_until[symbol] = "inf" 
            else: self.state.quarantine_until[symbol] = time.time() + (float(q_hours) * 3600)
            logger.warning(f"[{symbol}] 💀 Карантин ПОТЕРЬ: {self.state.consecutive_fails[symbol]} фейлов. Блокировка на {q_hours}ч.")
        asyncio.create_task(self.state.save())


class AnalyticsManager:
    """Расчет и форматирование метрик баланса и прибыли."""
    @staticmethod
    def format_duration(seconds: float) -> str:
        if seconds < 60: return f"{int(seconds)}с"
        if seconds < 3600: return f"{int(seconds // 60)}м {int(seconds % 60)}с"
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}ч {m}м"

    @staticmethod
    def get_balance_summary(start_balance: float, current_balance: float, active_positions_count: int) -> str:
        profit = current_balance - start_balance
        profit_pct = (profit / start_balance * 100) if start_balance > 0 else 0
        return (
            f"💰 <b>БАЛАНС</b>: {current_balance:.2f} USDT\n"
            f"📈 <b>PnL</b>: {profit:.2f}$ ({profit_pct:.2f}%)\n"
            f"🔄 <b>Активных сделок</b>: {active_positions_count}"
        )

    @staticmethod
    async def send_developer_report(tg_client, tracker, risk_manager):
        """Отправка периодического отчета разработчику."""
        if not tg_client: return
        try:
            mdd = tracker.data["mdd_pct"]
            trades_count = len(tracker.data["history"])
            
            report = (
                f"📊 <b>DAILY REPORT</b>\n"
                f"Trades: {trades_count}\n"
                f"MDD: {mdd:.2f}%\n"
                f"Quarantine: {len(risk_manager.state.quarantine_until)} symbols"
            )
            await tg_client.send_message(report)
        except Exception as e:
            logger.debug(f"Failed to send developer report: {e}")

    @staticmethod
    def flush_exit_spread_logs(spreads_data: Dict[str, Tuple[float, str]]):
        """Логирование накопленных спредов выхода."""
        if not spreads_data: return
        items = []
        fallbacks = []
        for sym, (spr, src) in spreads_data.items():
            items.append(f"{sym}: {spr:.2f}%({src})")
            if src == "BIN":
                fallbacks.append(sym)
        
        msg = "📊 [EXIT SPREADS]: " + " | ".join(items)
        logger.debug(msg)
        
        if fallbacks:
            logger.warning(f"⚠️ [EXIT FALLBACK]: {', '.join(fallbacks)} missing DEX price!")


class TradeManager:
    """Логика обработки закрытия сделок и их регистрации."""
    def __init__(self, tracker: Any, risk_manager: 'RiskManager', analytics: 'AnalyticsManager', tg: Optional[Any] = None):
        self.tracker = tracker
        self.risk_manager = risk_manager
        self.analytics = analytics
        self.tg = tg

    async def process_position_closure(self, pos_key: str, pos: 'ActivePosition', min_q_threshold: float, force_q_threshold: float):
        """Полный цикл обработки закрытой позиции."""
        try:
            if pos.entry_price <= 0.0:
                return

            exit_pr = pos.realized_exit_price if pos.realized_exit_price > 0 else (pos.exit_price_hint or pos.avg_price)
            duration_sec = time.time() - pos.opened_at
            
            # 1. Регистрация в трекере
            net_pnl, is_win = self.tracker.register_trade(
                symbol=pos.symbol,
                side=pos.side,
                entry_price=pos.avg_price if pos.avg_price > 0 else pos.entry_price,
                exit_price=exit_pr,
                qty=pos.max_realized_qty,
                duration_sec=duration_sec
            )

            # 2. Определение семантики и эмодзи
            emoji = "💵" if is_win else "🩸"
            current_status = pos.exit_status if pos.exit_status in ("EXTREME", "BREAKEVEN") else pos.last_exit_status
            
            if current_status == "EXTREME": 
                semantic = "⚠️ Аварийный выход (EXTREME Mode)"
            elif current_status == "BREAKEVEN": 
                semantic = "🛡 Выход по безубытку (TTL)"                                
            else: 
                semantic = "🎯 Тейк-профит" if is_win else "📉 Убыток (Ручное/Неизвестно)"

            # 3. Карантин
            if is_win:
                self.risk_manager.state.consecutive_fails[pos.symbol] = 0
                self.risk_manager.state.quarantine_until.pop(pos.symbol, None)
            else:
                self.risk_manager.apply_loss_quarantine(pos.symbol, net_pnl, min_q_threshold, force_q_threshold)

            # 4. Уведомление в Telegram
            if self.tg:
                msg = Reporters.exit_success(pos_key, semantic, exit_pr, net_pnl, emoji)
                msg += f"\n⏳ Время в сделке: {self.analytics.format_duration(duration_sec)}"
                asyncio.create_task(self.tg.send_message(msg))
                
            logger.info(f"[{pos_key}] 🛑 Позиция закрыта. {emoji} PnL: {net_pnl:.4f}$ | Время: {self.analytics.format_duration(duration_sec)}")
            
        except Exception as e:
            logger.error(f"Error in TradeManager.process_position_closure: {e}")


class DexUpdater:
    """Фоновое обновление цен с Dexscreener для активных позиций."""
    def __init__(self, dex_api: 'DexscreenerAPI', price_manager: 'PriceCacheManager', state: 'BotState', interval: float = 0.5):
        self.dex_api = dex_api
        self.price_manager = price_manager
        self.state = state
        self.interval = interval
        self._is_running = False

    async def run(self):
        self._is_running = True
        logger.info(f"🚀 DexUpdater loop started (Interval: {self.interval}s)")
        while self._is_running:
            try:
                # Обновляем цены для ВСЕХ рабочих символов
                target_symbols = list(self.price_manager.target_symbols)
                if target_symbols:
                    tasks = []
                    for symbol in target_symbols:
                        _, p_price = self.price_manager.get_prices(symbol)
                        if p_price > 0:
                            tasks.append(self._update_single(symbol, p_price))
                    
                    if tasks:
                        await asyncio.gather(*tasks)
                
                await asyncio.sleep(self.interval)
            except Exception as e:
                logger.error(f"Error in DexUpdater loop: {e}")
                await asyncio.sleep(1)

    async def _update_single(self, symbol: str, ref_price: float):
        try:
            pair_data = await self.dex_api.get_price_by_symbol(symbol, ref_price=ref_price)
            if pair_data:
                price_str = pair_data.get("priceUsd")
                if price_str:
                    self.price_manager.dex_prices[symbol] = (float(price_str), time.time())
        except Exception as e:
            logger.debug(f"Failed to update DEX price for {symbol}: {e}")

    def stop(self):
        self._is_running = False