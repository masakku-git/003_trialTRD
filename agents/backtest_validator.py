"""
BacktestValidator Agent (claude-haiku-4-5)
DBキャッシュを優先し、古い場合のみ再計算
戦略ファイル（strategies/）のbacktest()を使用
"""
import json
import logging
from datetime import date, timedelta

import anthropic
import pandas as pd

from strategies import StrategyRegistry
from tools.db import get_prices, get_backtest_cache, save_backtest_cache

logger = logging.getLogger(__name__)

HAIKU_MODEL = "claude-haiku-4-5-20251001"


def validate_signal(client: anthropic.Anthropic, symbol: str, signal_type: str) -> dict:
    """
    バックテスト結果を取得（DBキャッシュ優先）
    Returns: {"symbol", "signal_type", "win_rate", "avg_rr", "max_dd", "sample_cnt", "from_cache"}
    """
    # キャッシュ確認（7日以内）
    cached = get_backtest_cache(symbol, signal_type, max_age_days=7)
    if cached:
        logger.info(f"[backtest] {symbol}/{signal_type}: キャッシュ使用 (computed={cached['computed_at']})")
        return {**cached, "from_cache": True}

    # DBからデータ取得（APIコールなし）
    start = (date.today() - timedelta(days=365)).isoformat()
    df = get_prices(symbol, start=start)

    if df.empty or len(df) < 60:
        logger.warning(f"[backtest] {symbol}: データ不足でバックテスト不可")
        return {"symbol": symbol, "signal_type": signal_type,
                "win_rate": 0, "avg_rr": 0, "max_dd": 0, "sample_cnt": 0, "from_cache": False}

    # 戦略ファイルのbacktest()を使用
    strategy = StrategyRegistry.get(signal_type)
    if strategy:
        stats = strategy.backtest(df)
    else:
        logger.warning(f"[backtest] 戦略 '{signal_type}' が未登録。スキップ")
        return {"symbol": symbol, "signal_type": signal_type,
                "win_rate": 0, "avg_rr": 0, "max_dd": 0, "sample_cnt": 0, "from_cache": False}

    # LLMによる評価コメント
    prompt = f"""バックテスト結果を評価してください。

銘柄: {symbol}
シグナル: {signal_type}
勝率: {stats['win_rate']*100:.1f}%
平均RR: {stats['avg_rr']:.2f}
最大DD: {stats['max_dd']*100:.1f}%
サンプル数: {stats['sample_cnt']}

このシグナルは本日の実トレードに採用すべきか、JSON形式のみで回答：
{{"viable": true|false, "comment": "評価コメント（日本語50字以内）"}}
"""
    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        evaluation = json.loads(text)
    except Exception:
        evaluation = {"viable": stats["win_rate"] >= 0.5, "comment": "自動判定"}

    # DBキャッシュに保存
    save_backtest_cache(symbol, signal_type,
                        stats["win_rate"], stats["avg_rr"], stats["max_dd"], stats["sample_cnt"])

    result = {
        "symbol": symbol,
        "signal_type": signal_type,
        **stats,
        "viable": evaluation.get("viable", False),
        "comment": evaluation.get("comment", ""),
        "from_cache": False,
    }
    logger.info(f"[backtest] {symbol}/{signal_type}: win={stats['win_rate']:.1%} viable={result['viable']}")
    return result


def run_backtest_validation(client: anthropic.Anthropic, signals: list[dict]) -> list[dict]:
    """全シグナルのバックテスト検証を実行し、viable=Trueのみ返す"""
    validated = []
    for sig in signals:
        symbol = sig["symbol"]
        signal_type = sig.get("strategy_name", _infer_signal_type(sig))
        bt = validate_signal(client, symbol, signal_type)
        if bt.get("viable", False) and bt.get("sample_cnt", 0) >= 5:
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
