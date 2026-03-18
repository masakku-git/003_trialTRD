"""
移動平均クロス戦略（MA5/MA20ゴールデンクロス）
"""
from typing import Optional

import pandas as pd

from strategies.base import BaseStrategy


class MACrossStrategy(BaseStrategy):
    name = "ma_cross"
    description = "移動平均クロス戦略（MA5/MA20ゴールデンクロス）"

    stop_loss_pct = 0.03   # 3%
    take_profit_pct = 0.06  # 6%
    hold_days = 5

    def generate_signal(self, df: pd.DataFrame) -> Optional[dict]:
        if len(df) < 20:
            return None

        close = df["Close"]
        ma5 = close.rolling(5).mean()
        ma20 = close.rolling(20).mean()

        # 直近でゴールデンクロスが発生（前日: MA5 <= MA20, 当日: MA5 > MA20）
        if ma5.iloc[-2] <= ma20.iloc[-2] and ma5.iloc[-1] > ma20.iloc[-1]:
            current = float(close.iloc[-1])
            return {
                "signal": "buy",
                "confidence": self._calc_confidence(df),
                "entry_price": current,
                "stop_loss": round(current * (1 - self.stop_loss_pct), 1),
                "take_profit": round(current * (1 + self.take_profit_pct), 1),
                "reasoning": f"MA5がMA20を上抜け（ゴールデンクロス）。MA5={ma5.iloc[-1]:.1f}, MA20={ma20.iloc[-1]:.1f}",
            }
        return None

    def _calc_confidence(self, df: pd.DataFrame) -> float:
        """クロスの強さと出来高で信頼度を算出"""
        close = df["Close"]
        ma5 = close.rolling(5).mean().iloc[-1]
        ma20 = close.rolling(20).mean().iloc[-1]
        vol_avg = df["Volume"].rolling(5).mean().iloc[-1]
        vol_current = float(df["Volume"].iloc[-1])

        # MA乖離率が大きいほど強いクロス
        spread = abs(float(ma5) - float(ma20)) / float(ma20)
        # 出来高が平均より多いと信頼度UP
        vol_factor = min(vol_current / (float(vol_avg) + 1), 2.0) / 2.0

        confidence = min(0.5 + spread * 10 + vol_factor * 0.2, 1.0)
        return round(confidence, 2)

    def backtest(self, df: pd.DataFrame) -> dict:
        if len(df) < 60:
            return {"win_rate": 0, "avg_rr": 0, "max_dd": 0, "sample_cnt": 0}

        close = df["Close"]
        ma5 = close.rolling(5).mean()
        ma20 = close.rolling(20).mean()
        trades = []

        for i in range(20, len(df) - self.hold_days):
            if ma5.iloc[i - 1] <= ma20.iloc[i - 1] and ma5.iloc[i] > ma20.iloc[i]:
                entry = float(close.iloc[i])
                stop = entry * (1 - self.stop_loss_pct)
                target = entry * (1 + self.take_profit_pct)
                exit_price = float(close.iloc[i + self.hold_days])
                win = exit_price >= target or (exit_price > entry and exit_price > stop)
                rr = (exit_price - entry) / (entry - stop + 1e-10)
                trades.append({"win": win, "rr": rr})

        return self._summarize_trades(trades)
