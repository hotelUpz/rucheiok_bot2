from __future__ import annotations

import time
import os
import csv
from typing import Dict, Any, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from CORE.restorator import BotState 

def format_duration(seconds: float) -> str:
    """Хелпер: форматирует секунды в секунды или минуты."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{seconds/60:.1f}m"

class PerformanceTracker:
    def __init__(self, state_manager: 'BotState', fee_rate: float = 0.0006) -> None:
        self.state = state_manager
        self.fee_rate: float = fee_rate
        
        if not hasattr(self.state, 'analytics') or not isinstance(getattr(self.state, 'analytics', None), dict):
            self.state.analytics = {}
            
        self.data: Dict[str, Any] = self.state.analytics
        
        defaults: Dict[str, Any] = {
            "start_balance": 0.0,
            "current_balance": 0.0,
            "max_balance": 0.0,
            "min_balance": 0.0,
            "mdd_usd": 0.0,  
            "mdd_pct": 0.0,
            "max_profit_usd": 0.0,  # ДОБАВЛЕНО
            "max_profit_pct": 0.0,  # ДОБАВЛЕНО
            "total_wins": 0,
            "total_losses": 0,
            "total_pnl": 0.0,
            "symbols": {},   
            "history": []    
        }
        for k, v in defaults.items():
            if k not in self.data:
                self.data[k] = v

        self.history_file = "logs/trade_history.csv"
        os.makedirs(os.path.dirname(self.history_file), exist_ok=True)
        
        if not os.path.exists(self.history_file):
            try:
                with open(self.history_file, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        "timestamp", "symbol", "side", "entry_price", 
                        "exit_price", "qty", "entry_usd", "exit_usd", 
                        "net_pnl", "is_win", "duration"
                    ])
            except Exception:
                pass 

    def set_initial_balance(self, actual_balance: float) -> None:
        if self.data["start_balance"] == 0.0 and actual_balance > 0:
            self.data["start_balance"] = actual_balance
            self.data["current_balance"] = actual_balance
            self.data["max_balance"] = actual_balance
            self.data["min_balance"] = actual_balance
        # При каждом запуске пересчитываем max_profit и mdd из истории
        self._recalc_from_history()

    def _recalc_from_history(self) -> None:
        """Пересчитывает max_profit и mdd по полной истории сделок.
        Вызывается при старте, чтобы восстановить корректные значения из bot_state."""
        history = self.data.get("history", [])
        start_balance = self.data.get("start_balance", 0.0)
        if not history or start_balance <= 0:
            return

        running_balance = start_balance
        peak_balance = start_balance
        max_profit_usd = self.data.get("max_profit_usd", 0.0)
        max_profit_pct = self.data.get("max_profit_pct", 0.0)
        mdd_usd = self.data.get("mdd_usd", 0.0)
        mdd_pct = self.data.get("mdd_pct", 0.0)

        for trade in history:
            pnl = trade.get("pnl", 0.0)
            running_balance += pnl

            if running_balance > peak_balance:
                peak_balance = running_balance

            profit_usd = running_balance - start_balance
            profit_pct = (profit_usd / start_balance) * 100
            if profit_usd > max_profit_usd:
                max_profit_usd = profit_usd
            if profit_pct > max_profit_pct:
                max_profit_pct = profit_pct

            dd_usd = peak_balance - running_balance
            dd_pct = (dd_usd / peak_balance) * 100 if peak_balance > 0 else 0.0
            if dd_usd > mdd_usd:
                mdd_usd = dd_usd
            if dd_pct > mdd_pct:
                mdd_pct = dd_pct

        self.data["max_profit_usd"] = round(max_profit_usd, 4)
        self.data["max_profit_pct"] = round(max_profit_pct, 6)
        self.data["mdd_usd"] = round(mdd_usd, 6)
        self.data["mdd_pct"] = round(mdd_pct, 6)

    def register_trade(self, symbol: str, side: str, entry_price: float, exit_price: float, qty: float, duration_sec: float = 0.0) -> Tuple[float, bool]:
        if entry_price <= 0 or exit_price <= 0 or qty <= 0:
            return 0.0, False

        # --- РАСЧЕТ ДОЛЛАРОВОГО ОБЪЕМА ---
        entry_usd: float = entry_price * qty
        exit_usd: float = exit_price * qty
        # ---------------------------------

        direction: int = 1 if side == "LONG" else -1
        gross_pnl: float = (exit_price - entry_price) * qty * direction
        
        fee_cost: float = (entry_price * qty * self.fee_rate) + (exit_price * qty * self.fee_rate)
        net_pnl: float = gross_pnl - fee_cost
        
        is_win: bool = net_pnl > 0

        self.data["total_wins"] += 1 if is_win else 0
        self.data["total_losses"] += 1 if not is_win else 0
        self.data["total_pnl"] += net_pnl

        if self.data["start_balance"] > 0:
            self.data["current_balance"] += net_pnl
            cb: float = self.data["current_balance"]
            
            # Инициализируем переменные просадки перед ветвлением
            current_dd_usd: float = 0.0
            current_dd_pct: float = 0.0
            
            if cb > self.data["max_balance"]:
                self.data["max_balance"] = cb
            else:
                current_dd_usd = self.data["max_balance"] - cb
                current_dd_pct = (current_dd_usd / self.data["max_balance"]) * 100

            if cb < self.data["min_balance"]:
                self.data["min_balance"] = cb

            if current_dd_usd > self.data["mdd_usd"]:
                self.data["mdd_usd"] = current_dd_usd
            if current_dd_pct > self.data["mdd_pct"]:
                self.data["mdd_pct"] = current_dd_pct

            # --- РАСЧЕТ МАКСИМАЛЬНОЙ ПРИБЫЛИ (всегда, независимо от нового макс.) ---
            profit_usd = cb - self.data["start_balance"]
            profit_pct = (profit_usd / self.data["start_balance"]) * 100
            if profit_usd > self.data["max_profit_usd"]:
                self.data["max_profit_usd"] = round(profit_usd, 4)
            if profit_pct > self.data["max_profit_pct"]:
                self.data["max_profit_pct"] = round(profit_pct, 6)
            # --------------------------------------------------------------------------

        if symbol not in self.data["symbols"]:
            self.data["symbols"][symbol] = {"wins": 0, "losses": 0, "pnl": 0.0}
        
        sym_stat: Dict[str, Any] = self.data["symbols"][symbol]
        sym_stat["wins"] += 1 if is_win else 0
        sym_stat["losses"] += 1 if not is_win else 0
        sym_stat["pnl"] += net_pnl

        formatted_duration = format_duration(duration_sec)

        self.data["history"].append({
            "ts": time.time(),
            "symbol": symbol,
            "side": side,
            "entry_usd": round(entry_usd, 2),  
            "exit_usd": round(exit_usd, 2),    
            "pnl": round(net_pnl, 4),
            "is_win": is_win,
            "duration": formatted_duration
        })
        
        if len(self.data["history"]) > 100:
            self.data["history"].pop(0)

        try:
            with open(self.history_file, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    time.time(), symbol, side, entry_price, 
                    exit_price, qty, round(entry_usd, 2), round(exit_usd, 2), 
                    round(net_pnl, 4), is_win, formatted_duration
                ])
        except Exception:
            pass 

        return net_pnl, is_win

    def get_summary_text(self) -> str:
        total: int = self.data["total_wins"] + self.data["total_losses"]
        winrate: float = (self.data["total_wins"] / total * 100) if total > 0 else 0.0
        pnl: float = self.data["total_pnl"]
        
        sign: str = "🟢" if pnl >= 0 else "🔴"
        
        text: str = f"📊 <b>АУДИТ ПОРТФЕЛЯ</b>\n"
        text += f"━━━━━━━━━━━━━━━━━━━━\n"
        text += f"{sign} <b>Total PnL: {pnl:.2f} $</b>\n"
        text += f"🎯 Сделок: {total} (✅ {self.data['total_wins']} | ❌ {self.data['total_losses']})\n"
        text += f"⚖️ Винрейт: {winrate:.1f}%\n"
        
        if self.data["start_balance"] > 0:
            text += f"━━━━━━━━━━━━━━━━━━━━\n"
            text += f"💰 Баланс: {self.data['start_balance']:.2f} ➔ <b>{self.data['current_balance']:.2f}</b>\n"
            text += f"📈 Max Профит: {self.data['max_profit_pct']:.2f}% (+{self.data['max_profit_usd']:.2f} $)\n" # ДОБАВЛЕНО
            text += f"📉 Max Просадка: {self.data['mdd_pct']:.2f}% (-{self.data['mdd_usd']:.2f} $)\n"
            
        return text