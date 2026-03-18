"""
RSI売られすぎ戦略（RSI30割れからの反発）
"""
from typing import Optional

import pandas as pd

from strategies.base import BaseStrategy


class RSIOversoldStrategy(BaseStrategy):
    name = "rsi_oversold"
    description = "RSI売られすぎ戦略（RSI30割れからの反発）"

    stop_loss_pct = 0.05   # 5%
    take_profit_pct = 0.08  # 8%
    hold_days = 5
    rsi_period = 14
    rsi_threshold = 30

    def _calc_rsi(self, close: pd.Series) -> pd.Series:
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(self.rsi_period).mean()
        loss = (-delta.clip(upper=0)).rolling(self.rsi_period).mean()
        rs = gain / (loss + 1e-10)
        return 100 - 100 / (1 + rs)

    def generate_signal(self, df: pd.DataFrame) -> Optional[dict]:
        if len(df) < self.rsi_period + 5:
            return None

        close = df["Close"]
        rsi = self._calc_rsi(close)

        # RSIが30を下回った（前日>30, 当日<=30）= 売られすぎ圏突入
        if rsi.iloc[-2] > self.rsi_threshold and rsi.iloc[-1] <= self.rsi_threshold:
            current = float(close.iloc[-1])
            return {
                "signal": "buy",
                "confidence": self._calc_confidence(rsi),
                "entry_price": current,
                "stop_loss": round(current * (1 - self.stop_loss_pct), 1),
                "take_profit": round(current * (1 + self.take_profit_pct), 1),
                "reasoning": f"RSI({self.rsi_period})が{self.rsi_threshold}割れ（{rsi.iloc[-1]:.1f}）。売られすぎ圏からの反発を狙う",
            }
        return None

    def _calc_confidence(self, rsi: pd.Series) -> float:
        """RSIの深さで信頼度を算出（深いほど反発期待が高い）"""
        current_rsi = float(rsi.iloc[-1])
        # RSI 20以下なら高信頼、30付近なら低信頼
        depth = max(0, self.rsi_threshold - current_rsi) / self.rsi_threshold
        confidence = min(0.4 + depth * 0.6, 0.9)
        return round(confidence, 2)

    def backtest(self, df: pd.DataFrame) -> dict:
        if len(df) < 60:
            return {"win_rate": 0, "avg_rr": 0, "max_dd": 0, "sample_cnt": 0}

        close = df["Close"]
        rsi = self._calc_rsi(close)
        trades = []

        for i in range(20, len(df) - self.hold_days):
            if rsi.iloc[i - 1] > self.rsi_threshold and rsi.iloc[i] <= self.rsi_threshold:
                entry = float(close.iloc[i])
                stop = entry * (1 - self.stop_loss_pct)
                target = entry * (1 + self.take_profit_pct)
                exit_price = float(close.iloc[i + self.hold_days])
                win = exit_price >= target
                rr = (exit_price - entry) / (entry - stop + 1e-10)
                trades.append({"win": win, "rr": rr})

        return self._summarize_trades(trades)
