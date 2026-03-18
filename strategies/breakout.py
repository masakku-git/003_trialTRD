"""
ブレイクアウト戦略（20日高値更新突破）
"""
from typing import Optional

import pandas as pd

from strategies.base import BaseStrategy


class BreakoutStrategy(BaseStrategy):
    name = "breakout"
    description = "ブレイクアウト戦略（20日高値更新突破）"

    stop_loss_pct = 0.04    # 4%
    take_profit_pct = 0.10  # 10%
    hold_days = 5
    lookback = 20
    breakout_margin = 0.01  # 1%超えでブレイクアウト判定

    def generate_signal(self, df: pd.DataFrame) -> Optional[dict]:
        if len(df) < self.lookback + 2:
            return None

        close = df["Close"]
        high = df["High"]

        # 前日までの20日高値
        prev_high20 = float(high.iloc[-(self.lookback + 1):-1].max())
        current_close = float(close.iloc[-1])

        # 終値が20日高値を1%超えてブレイクアウト
        if current_close > prev_high20 * (1 + self.breakout_margin):
            return {
                "signal": "buy",
                "confidence": self._calc_confidence(df, prev_high20),
                "entry_price": current_close,
                "stop_loss": round(current_close * (1 - self.stop_loss_pct), 1),
                "take_profit": round(current_close * (1 + self.take_profit_pct), 1),
                "reasoning": f"20日高値({prev_high20:.1f})を{self.breakout_margin*100:.0f}%超ブレイクアウト。終値={current_close:.1f}",
            }
        return None

    def _calc_confidence(self, df: pd.DataFrame, prev_high: float) -> float:
        """ブレイクアウトの強さと出来高で信頼度を算出"""
        current_close = float(df["Close"].iloc[-1])
        vol_avg = float(df["Volume"].rolling(self.lookback).mean().iloc[-1])
        vol_current = float(df["Volume"].iloc[-1])

        # ブレイクアウト率
        breakout_strength = (current_close - prev_high) / prev_high
        # 出来高倍率（大きいほど信頼度UP）
        vol_ratio = min(vol_current / (vol_avg + 1), 3.0) / 3.0

        confidence = min(0.4 + breakout_strength * 5 + vol_ratio * 0.3, 1.0)
        return round(confidence, 2)

    def backtest(self, df: pd.DataFrame) -> dict:
        if len(df) < 60:
            return {"win_rate": 0, "avg_rr": 0, "max_dd": 0, "sample_cnt": 0}

        close = df["Close"]
        high20 = df["High"].rolling(self.lookback).max()
        trades = []

        for i in range(self.lookback + 1, len(df) - self.hold_days):
            if float(close.iloc[i]) > float(high20.iloc[i - 1]) * (1 + self.breakout_margin):
                entry = float(close.iloc[i])
                stop = entry * (1 - self.stop_loss_pct)
                target = entry * (1 + self.take_profit_pct)
                exit_price = float(close.iloc[i + self.hold_days])
                win = exit_price >= target
                rr = (exit_price - entry) / (entry - stop + 1e-10)
                trades.append({"win": win, "rr": rr})

        return self._summarize_trades(trades)
