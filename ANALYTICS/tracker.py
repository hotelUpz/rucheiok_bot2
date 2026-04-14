import time
from typing import Dict, Any

class PerformanceTracker:
    def __init__(self, state_manager, fee_rate: float = 0.0006):
        """
        fee_rate: 0.06% по умолчанию (типичная комиссия тейкера на Phemex/Binance).
        Можно настроить из конфига.
        """
        self.state = state_manager
        self.fee_rate = fee_rate
        
        # Инициализируем структуру в стейте, если запускаемся впервые
        if not hasattr(self.state, 'analytics') or not isinstance(getattr(self.state, 'analytics', None), dict):
            self.state.analytics = {}
            
        self.data = self.state.analytics
        
        # Заполняем дефолтные ключи, если их нет
        defaults = {
            "start_balance": 0.0,
            "current_balance": 0.0,
            "max_balance": 0.0,
            "min_balance": 0.0,
            "mdd_usd": 0.0,  # Max Drawdown $
            "mdd_pct": 0.0,  # Max Drawdown %
            "total_wins": 0,
            "total_losses": 0,
            "total_pnl": 0.0,
            "symbols": {},   # {"BTCUSDT": {"wins": 1, "losses": 0, "pnl": 5.5}}
            "history": []    # Очередь последних сделок (чтобы JSON не раздувался бесконечно)
        }
        for k, v in defaults.items():
            if k not in self.data:
                self.data[k] = v

    def set_initial_balance(self, actual_balance: float):
        """Вызывается на старте бота. Запишет начальный баланс только 1 раз."""
        if self.data["start_balance"] == 0.0 and actual_balance > 0:
            self.data["start_balance"] = actual_balance
            self.data["current_balance"] = actual_balance
            self.data["max_balance"] = actual_balance
            self.data["min_balance"] = actual_balance

    def register_trade(self, symbol: str, side: str, entry_price: float, exit_price: float, qty: float) -> tuple[float, bool]:
        """Расчет после закрытия сделки. Возвращает (Net PnL, Is Win)"""
        if entry_price <= 0 or exit_price <= 0 or qty <= 0:
            return 0.0, False

        # 1. Расчет Gross PnL
        direction = 1 if side == "LONG" else -1
        gross_pnl = (exit_price - entry_price) * qty * direction
        
        # 2. Вычет комиссий (за вход и выход)
        fee_cost = (entry_price * qty * self.fee_rate) + (exit_price * qty * self.fee_rate)
        net_pnl = gross_pnl - fee_cost
        
        is_win = net_pnl > 0

        # 3. Обновляем Total
        self.data["total_wins"] += 1 if is_win else 0
        self.data["total_losses"] += 1 if not is_win else 0
        self.data["total_pnl"] += net_pnl

        # 4. Обновляем Баланс и Просадки
        if self.data["start_balance"] > 0:
            self.data["current_balance"] += net_pnl
            cb = self.data["current_balance"]
            
            if cb > self.data["max_balance"]:
                self.data["max_balance"] = cb
            if cb < self.data["min_balance"]:
                self.data["min_balance"] = cb

            # MDD считается от исторического максимума баланса
            drawdown_usd = self.data["max_balance"] - cb
            drawdown_pct = (drawdown_usd / self.data["max_balance"]) * 100 if self.data["max_balance"] > 0 else 0.0

            if drawdown_usd > self.data["mdd_usd"]:
                self.data["mdd_usd"] = drawdown_usd
            if drawdown_pct > self.data["mdd_pct"]:
                self.data["mdd_pct"] = drawdown_pct

        # 5. По-символьная статистика
        if symbol not in self.data["symbols"]:
            self.data["symbols"][symbol] = {"wins": 0, "losses": 0, "pnl": 0.0}
        
        sym_stat = self.data["symbols"][symbol]
        sym_stat["wins"] += 1 if is_win else 0
        sym_stat["losses"] += 1 if not is_win else 0
        sym_stat["pnl"] += net_pnl

        # 6. Запись в историю (ограничим 100 записями)
        self.data["history"].append({
            "ts": time.time(),
            "symbol": symbol,
            "side": side,
            "pnl": round(net_pnl, 4),
            "is_win": is_win
        })
        if len(self.data["history"]) > 100:
            self.data["history"].pop(0)

        return net_pnl, is_win

    def get_summary_text(self) -> str:
        """Метод для генерации красивого отчета в Telegram"""
        total = self.data["total_wins"] + self.data["total_losses"]
        winrate = (self.data["total_wins"] / total * 100) if total > 0 else 0.0
        pnl = self.data["total_pnl"]
        
        sign = "🟢" if pnl >= 0 else "🔴"
        
        text = f"📊 <b>АУДИТ ПОРТФЕЛЯ</b>\n"
        text += f"━━━━━━━━━━━━━━━━━━━━\n"
        text += f"{sign} <b>Total PnL: {pnl:.2f} $</b>\n"
        text += f"🎯 Сделок: {total} (✅ {self.data['total_wins']} | ❌ {self.data['total_losses']})\n"
        text += f"⚖️ Винрейт: {winrate:.1f}%\n"
        
        if self.data["start_balance"] > 0:
            text += f"━━━━━━━━━━━━━━━━━━━━\n"
            text += f"💰 Баланс: {self.data['start_balance']:.2f} ➔ <b>{self.data['current_balance']:.2f}</b>\n"
            text += f"📉 Max Просадка: {self.data['mdd_pct']:.2f}% (-{self.data['mdd_usd']:.2f} $)\n"
            
        return text