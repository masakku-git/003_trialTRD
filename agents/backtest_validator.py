"""
BacktestValidator（ルールベース）
DBキャッシュを優先し、古い場合のみ再計算
戦略ファイル（strategies/）のbacktest()を使用
"""
import logging
from datetime import date, timedelta

import pandas as pd

from strategies import StrategyRegistry
from tools.db import get_prices, get_backtest_cache, save_backtest_cache

logger = logging.getLogger(__name__)

# バックテスト通過基準
MIN_WIN_RATE = 0.40
MIN_SAMPLE_CNT = 5
MIN_AVG_RR = 0.3


def validate_signal(symbol: str, signal_type: str) -> dict:
    """
    バックテスト結果を取得（DBキャッシュ優先）
    Returns: {"symbol", "signal_type", "win_rate", "avg_rr", "max_dd", "sample_cnt", "from_cache", "viable"}
    """
    # キャッシュ確認（7日以内）
    cached = get_backtest_cache(symbol, signal_type, max_age_days=7)
    if cached:
        logger.info(f"[backtest] {symbol}/{signal_type}: キャッシュ使用 (computed={cached['computed_at']})")
        viable = _is_viable(cached)
        return {**cached, "from_cache": True, "viable": viable}

    # DBからデータ取得（APIコールなし）
    start = (date.today() - timedelta(days=365)).isoformat()
    df = get_prices(symbol, start=start)

    if df.empty or len(df) < 60:
        logger.warning(f"[backtest] {symbol}: データ不足でバックテスト不可")
        return {"symbol": symbol, "signal_type": signal_type,
                "win_rate": 0, "avg_rr": 0, "max_dd": 0, "sample_cnt": 0,
                "from_cache": False, "viable": False}

    # 戦略ファイルのbacktest()を使用
    strategy = StrategyRegistry.get(signal_type)
    if strategy:
        stats = strategy.backtest(df)
    else:
        logger.warning(f"[backtest] 戦略 '{signal_type}' が未登録。スキップ")
        return {"symbol": symbol, "signal_type": signal_type,
                "win_rate": 0, "avg_rr": 0, "max_dd": 0, "sample_cnt": 0,
                "from_cache": False, "viable": False}

    # DBキャッシュに保存
    save_backtest_cache(symbol, signal_type,
                        stats["win_rate"], stats["avg_rr"], stats["max_dd"], stats["sample_cnt"])

    viable = _is_viable(stats)

    result = {
        "symbol": symbol,
        "signal_type": signal_type,
        **stats,
        "viable": viable,
        "from_cache": False,
    }
    logger.info(f"[backtest] {symbol}/{signal_type}: win={stats['win_rate']:.1%} viable={viable}")
    return result


def _is_viable(stats: dict) -> bool:
    """ルールベースのバックテスト通過判定"""
    return (
        stats.get("win_rate", 0) >= MIN_WIN_RATE
        and stats.get("sample_cnt", 0) >= MIN_SAMPLE_CNT
        and stats.get("avg_rr", 0) >= MIN_AVG_RR
    )


def run_backtest_validation(signals: list[dict]) -> list[dict]:
    """全シグナルのバックテスト検証を実行し、viable=Trueのみ返す"""
    validated = []
    for sig in signals:
        symbol = sig["symbol"]
        signal_type = sig.get("strategy_name", _infer_signal_type(sig))
        bt = validate_signal(symbol, signal_type)
        if bt.get("viable", False):
            validated.append({**sig, "backtest": bt, "strategy_name": signal_type})
    logger.info(f"[backtest] バックテスト通過: {[v['symbol'] for v in validated]}")
    return validated


def _infer_signal_type(signal: dict) -> str:
    """シグナル辞書からシグナルタイプを推定（後方互換用フォールバック）"""
    reasoning = signal.get("reasoning", "").lower()
    if "rsi" in reasoning or "売られ" in signal.get("reasoning", ""):
        return "rsi_oversold"
    if "ブレイク" in signal.get("reasoning", "") or "breakout" in reasoning:
        return "breakout"
    return "ma_cross"
