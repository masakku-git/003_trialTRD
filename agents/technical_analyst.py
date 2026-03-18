"""
TechnicalAnalyst（ルールベース）
DBのOHLCVデータでテクニカル分析を行いシグナルを生成する
API再取得は行わない（DBのみ使用）

各戦略ファイル（strategies/）の generate_signal() を使用し、
全登録戦略を銘柄ごとに試して最良のシグナルを選ぶ。
"""
import logging
from datetime import date, timedelta
from typing import Optional

import pandas as pd

from strategies import StrategyRegistry
from tools.db import get_prices

logger = logging.getLogger(__name__)


def _compute_indicators(df: pd.DataFrame) -> dict:
    """基本テクニカル指標を計算して辞書で返す"""
    if len(df) < 20:
        return {}

    close = df["Close"]

    # 移動平均
    ma5 = close.rolling(5).mean().iloc[-1]
    ma20 = close.rolling(20).mean().iloc[-1]
    ma60 = close.rolling(60).mean().iloc[-1] if len(df) >= 60 else None

    # RSI(14)
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / (loss + 1e-10)
    rsi = (100 - 100 / (1 + rs)).iloc[-1]

    # ボリンジャーバンド(20日)
    std20 = close.rolling(20).std().iloc[-1]
    bb_upper = float(ma20) + 2 * float(std20)
    bb_lower = float(ma20) - 2 * float(std20)

    # MACD
    ema12 = close.ewm(span=12).mean()
    ema26 = close.ewm(span=26).mean()
    macd = (ema12 - ema26).iloc[-1]
    signal_line = (ema12 - ema26).ewm(span=9).mean().iloc[-1]

    # 出来高（5日平均比）
    vol_avg5 = df["Volume"].rolling(5).mean().iloc[-1]
    vol_ratio = float(df["Volume"].iloc[-1]) / (float(vol_avg5) + 1)

    current_close = float(close.iloc[-1])

    return {
        "close": round(current_close, 1),
        "ma5": round(float(ma5), 1),
        "ma20": round(float(ma20), 1),
        "ma60": round(float(ma60), 1) if ma60 is not None else None,
        "rsi14": round(float(rsi), 1),
        "macd": round(float(macd), 3),
        "macd_signal": round(float(signal_line), 3),
        "bb_upper": round(bb_upper, 1),
        "bb_lower": round(bb_lower, 1),
        "vol_ratio_5d": round(vol_ratio, 2),
        "above_ma5": current_close > float(ma5),
        "above_ma20": current_close > float(ma20),
        "ma5_above_ma20": float(ma5) > float(ma20),
    }


def analyze_symbol(symbol: str, name: str = "") -> Optional[dict]:
    """
    単一銘柄のテクニカル分析を実行。
    全登録戦略の generate_signal() を試し、最も信頼度の高いシグナルを採用する。
    """
    start = (date.today() - timedelta(days=120)).isoformat()
    df = get_prices(symbol, start=start)

    if df.empty or len(df) < 20:
        logger.warning(f"[analyst] {symbol}: データ不足 ({len(df)}件)")
        return None

    indicators = _compute_indicators(df)
    if not indicators:
        return None

    # 全戦略を試してシグナルを収集
    all_strategies = StrategyRegistry.get_all()
    candidates = []

    for strategy_name, strategy in all_strategies.items():
        try:
            signal = strategy.generate_signal(df)
            if signal and signal.get("signal") != "hold":
                signal["strategy_name"] = strategy_name
                candidates.append(signal)
                logger.info(
                    f"[analyst] {symbol}: 戦略'{strategy_name}' → "
                    f"{signal['signal']} (confidence={signal['confidence']})"
                )
        except Exception as e:
            logger.error(f"[analyst] {symbol}: 戦略'{strategy_name}' 実行失敗 - {e}")

    if not candidates:
        logger.info(f"[analyst] {symbol}: 全戦略でシグナルなし")
        return None

    # 最も信頼度の高いシグナルを採用
    best = max(candidates, key=lambda s: s.get("confidence", 0))
    best["symbol"] = symbol
    best["indicators"] = indicators
    best["all_strategy_signals"] = [
        {"strategy": c["strategy_name"], "signal": c["signal"], "confidence": c["confidence"]}
        for c in candidates
    ]

    logger.info(
        f"[analyst] {symbol}: 採用戦略='{best['strategy_name']}' "
        f"signal={best['signal']} confidence={best['confidence']}"
    )
    return best


def run_technical_analysis(candidates: list[dict]) -> list[dict]:
    """
    候補銘柄リストに対してテクニカル分析を実行
    Returns: シグナルありの銘柄リスト
    """
    # 戦略モジュールを自動検出・ロード
    StrategyRegistry.discover()
    logger.info(f"[analyst] 登録戦略: {StrategyRegistry.list_names()}")

    signals = []
    for c in candidates:
        symbol = c["symbol"]
        result = analyze_symbol(symbol)
        if result and result.get("signal") != "hold":
            signals.append(result)
    logger.info(f"[analyst] シグナル銘柄: {[s['symbol'] for s in signals]}")
    return signals
