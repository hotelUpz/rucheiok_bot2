# {
#     "app": {
#         "name": "Rucheiok Bot",
#         "max_active_positions": 1
#     },
#     "credentials": {
#         "api_key": "",
#         "api_secret": ""
#     },
#     "tg": {
#         "enable": true,
#         "token": "",
#         "chat_id": ""
#     },
#     "log": {
#         "debug": true,
#         "info": true,
#         "warning": true,
#         "error": true,
#         "max_lines": 1500,
#         "timezone": "Europe/Kyiv"
#     },
#     "black_list": [
#         "RIVERUSDT",
#         "SIRENUSDT",
#         "0GUSDT",
#         "OGUSDT",
#         "0GUSDT",
#         "OGUSDT",
#         "PTBUSDT",
#         "ICPUSDT",
#         "MAGICUSDT",
#         "REZUSDT",
#         "DOODUSDT",
#         "DEGOUSDT",
#         "FIOUSDT",
#         "PIPPINUSDT",
#         "SKLUSDT",
#         "ARPAUSDT",
#         "REZUSDT",
#         "ENJUSDT",
#         "DRIFTUSDT",
#         "POPCATUSDT",
#         "STOUSDT",
#         "ARIAUSDT",
#         "NOMUSDT",
#         "SAHARAUSDT",
#         "ZETAUSDT"
#     ],
#     "quota_asset": "USDT",
#     "risk": {
#         "leverage": {
#             "set_previous": false,
#             "used_by_cache": false,
#             "val": 22,
#             "delay_sec": 0.3,
#             "margin_mode": 2
#         },
#         "margin_size": "row",
#         "notional_limit": 3000.0,
#         "margin_over_size_pct": 1,
#         "hedge_mode": false,
#         "quarantine": {
#             "max_consecutive_fails": 3,
#             "quarantine_hours": "inf"
#         }
#     },
#     "entry": {
#         "signal_timeout_sec": 0.01,
#         "entry_timeout_sec": 0.3,
#         "max_place_order_retries": 1,
#         "quarantine": {
#             "quarantine_hours": 1
#         },
#         "pattern": {
#             "phemex": {
#                 "name": "Rucheiok pattern",
#                 "enable": true,
#                 "depth": 8,
#                 "min_first_row_usdt_notional": 50,
#                 "max_first_row_usdt_notional": 2900,
#                 "header": {
#                     "roc_window": 3,
#                     "max_one_roc_pct": 0.5
#                 },
#                 "body": {
#                     "roc_sma_window": 5
#                 },
#                 "bottom": {
#                     "min_spread_between_two_row_pct": 0.25,
#                     "min_spread_between_three_row_pct": 0.3
#                 },
#                 "header_to_bottom_desired_rate": 10,
#                 "max_bid_ask_distance_rate": 0.4,
#                 "pattern_ttl_sec": 5
#             },
#             "binance": {
#                 "enable": true,
#                 "min_price_spread_rate": 0.8,
#                 "update_prices_sec": 1.1,
#                 "spread_ttl_sec": 2
#             },
#             "funding_pattern1": {
#                 "enable": true,
#                 "check_interval_sec": 60,
#                 "skip_before_counter_sec": 1800,
#                 "threshold_pct": 2.0
#             },
#             "funding_pattern2": {
#                 "enable": true,
#                 "check_interval_sec": 60,
#                 "skip_before_counter_sec": 1800,
#                 "diff_threshold_pct": 0.05
#             }
#         }
#     },
#     "exit": {
#         "max_place_order_retries": 1,
#         "scenarios": {
#             "base": {
#                 "order_timeout_sec": 0.25,
#                 "stabilization_ttl": 3,
#                 "target_rate": 0.7,
#                 "shift_demotion": 0.2,
#                 "min_target_rate": 0.3,
#                 "shift_ttl": 20
#             },
#             "negative": {
#                 "enable": true,
#                 "stabilization_ttl": 1.5,
#                 "negative_spread_pct": 0,
#                 "negative_ttl": 30
#             },
#             "breakeven_ttl_close": {
#                 "order_timeout_sec": 0.1,
#                 "position_ttl": 300,
#                 "breakeven_wait_sec": 2,
#                 "to_entry_orientation": 0.0
#             }
#         },
#         "interference": {
#             "enable": true,
#             "order_timeout_sec": 0.1,
#             "usual_vol_pct_to_init_size": 3,
#             "max_vol_pct_to_init_size": 9,
#             "max_retries_per_level": 3
#         },
#         "extrime_close": {
#             "enable": true,
#             "order_timeout_sec": 0.1,
#             "retry_ttl": 3,
#             "retry_num": "inf",
#             "bid_to_ask_orientation": 0.0,
#             "increase_fraction": 5.0
#         }
#     }
# }


# from __future__ import annotations

# import time
# import os
# import csv
# from typing import Dict, Any, Tuple, TYPE_CHECKING

# if TYPE_CHECKING:
#     from CORE.restorator import BotState 

# def format_duration(seconds: float) -> str:
#     """Хелпер: форматирует секунды в секунды или минуты."""
#     if seconds < 60:
#         return f"{seconds:.1f}s"
#     return f"{seconds/60:.1f}m"

# class PerformanceTracker:
#     def __init__(self, state_manager: 'BotState', fee_rate: float = 0.0006) -> None:
#         self.state = state_manager
#         self.fee_rate: float = fee_rate
        
#         if not hasattr(self.state, 'analytics') or not isinstance(getattr(self.state, 'analytics', None), dict):
#             self.state.analytics = {}
            
#         self.data: Dict[str, Any] = self.state.analytics
        
#         defaults: Dict[str, Any] = {
#             "start_balance": 0.0,
#             "current_balance": 0.0,
#             "max_balance": 0.0,
#             "min_balance": 0.0,
#             "mdd_usd": 0.0,  
#             "mdd_pct": 0.0,  
#             "total_wins": 0,
#             "total_losses": 0,
#             "total_pnl": 0.0,
#             "symbols": {},   
#             "history": []    
#         }
#         for k, v in defaults.items():
#             if k not in self.data:
#                 self.data[k] = v

#         self.history_file = "logs/trade_history.csv"
#         os.makedirs(os.path.dirname(self.history_file), exist_ok=True)
        
#         if not os.path.exists(self.history_file):
#             try:
#                 with open(self.history_file, "w", newline="", encoding="utf-8") as f:
#                     writer = csv.writer(f)
#                     # ДОБАВЛЕНЫ КОЛОНКИ: entry_usd, exit_usd
#                     writer.writerow([
#                         "timestamp", "symbol", "side", "entry_price", 
#                         "exit_price", "qty", "entry_usd", "exit_usd", 
#                         "net_pnl", "is_win", "duration"
#                     ])
#             except Exception:
#                 pass 

#     def set_initial_balance(self, actual_balance: float) -> None:
#         if self.data["start_balance"] == 0.0 and actual_balance > 0:
#             self.data["start_balance"] = actual_balance
#             self.data["current_balance"] = actual_balance
#             self.data["max_balance"] = actual_balance
#             self.data["min_balance"] = actual_balance

#     def register_trade(self, symbol: str, side: str, entry_price: float, exit_price: float, qty: float, duration_sec: float = 0.0) -> Tuple[float, bool]:
#         if entry_price <= 0 or exit_price <= 0 or qty <= 0:
#             return 0.0, False

#         # --- РАСЧЕТ ДОЛЛАРОВОГО ОБЪЕМА ---
#         entry_usd: float = entry_price * qty
#         exit_usd: float = exit_price * qty
#         # ---------------------------------

#         direction: int = 1 if side == "LONG" else -1
#         gross_pnl: float = (exit_price - entry_price) * qty * direction
        
#         fee_cost: float = (entry_price * qty * self.fee_rate) + (exit_price * qty * self.fee_rate)
#         net_pnl: float = gross_pnl - fee_cost
        
#         is_win: bool = net_pnl > 0

#         self.data["total_wins"] += 1 if is_win else 0
#         self.data["total_losses"] += 1 if not is_win else 0
#         self.data["total_pnl"] += net_pnl

#         if self.data["start_balance"] > 0:
#             self.data["current_balance"] += net_pnl
#             cb: float = self.data["current_balance"]
            
#             if cb > self.data["max_balance"]:
#                 self.data["max_balance"] = cb
#                 current_dd_usd: float = 0.0
#                 current_dd_pct: float = 0.0
#             else:
#                 current_dd_usd = self.data["max_balance"] - cb
#                 current_dd_pct = (current_dd_usd / self.data["max_balance"]) * 100

#             if cb < self.data["min_balance"]:
#                 self.data["min_balance"] = cb

#             if current_dd_usd > self.data["mdd_usd"]:
#                 self.data["mdd_usd"] = current_dd_usd
#             if current_dd_pct > self.data["mdd_pct"]:
#                 self.data["mdd_pct"] = current_dd_pct

#         if symbol not in self.data["symbols"]:
#             self.data["symbols"][symbol] = {"wins": 0, "losses": 0, "pnl": 0.0}
        
#         sym_stat: Dict[str, Any] = self.data["symbols"][symbol]
#         sym_stat["wins"] += 1 if is_win else 0
#         sym_stat["losses"] += 1 if not is_win else 0
#         sym_stat["pnl"] += net_pnl

#         formatted_duration = format_duration(duration_sec)

#         self.data["history"].append({
#             "ts": time.time(),
#             "symbol": symbol,
#             "side": side,
#             "entry_usd": round(entry_usd, 2),  # СОХРАНЯЕМ В СТЕЙТ
#             "exit_usd": round(exit_usd, 2),    # СОХРАНЯЕМ В СТЕЙТ
#             "pnl": round(net_pnl, 4),
#             "is_win": is_win,
#             "duration": formatted_duration
#         })
        
#         if len(self.data["history"]) > 100:
#             self.data["history"].pop(0)

#         try:
#             with open(self.history_file, "a", newline="", encoding="utf-8") as f:
#                 writer = csv.writer(f)
#                 writer.writerow([
#                     time.time(), symbol, side, entry_price, 
#                     exit_price, qty, round(entry_usd, 2), round(exit_usd, 2), 
#                     round(net_pnl, 4), is_win, formatted_duration
#                 ])
#         except Exception:
#             pass 

#         return net_pnl, is_win

#     def get_summary_text(self) -> str:
#         total: int = self.data["total_wins"] + self.data["total_losses"]
#         winrate: float = (self.data["total_wins"] / total * 100) if total > 0 else 0.0
#         pnl: float = self.data["total_pnl"]
        
#         sign: str = "🟢" if pnl >= 0 else "🔴"
        
#         text: str = f"📊 <b>АУДИТ ПОРТФЕЛЯ</b>\n"
#         text += f"━━━━━━━━━━━━━━━━━━━━\n"
#         text += f"{sign} <b>Total PnL: {pnl:.2f} $</b>\n"
#         text += f"🎯 Сделок: {total} (✅ {self.data['total_wins']} | ❌ {self.data['total_losses']})\n"
#         text += f"⚖️ Винрейт: {winrate:.1f}%\n"
        
#         if self.data["start_balance"] > 0:
#             text += f"━━━━━━━━━━━━━━━━━━━━\n"
#             text += f"💰 Баланс: {self.data['start_balance']:.2f} ➔ <b>{self.data['current_balance']:.2f}</b>\n"
#             text += f"📉 Max Просадка: {self.data['mdd_pct']:.2f}% (-{self.data['mdd_usd']:.2f} $)\n"
            
#         return text

