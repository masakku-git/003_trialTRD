"""
BacktestValidator Agent (claude-haiku-4-5)
DBキャッシュを優先し、古い場合のみ再計算
シグナルタイプ別に過去の勝率・リスクリワードを算出
"""
import json
import logging
from datetime import date, timedelta

import anthropic
import pandas as pd

from tools.db import get_prices, get_backtest_cache, save_backtest_cache

logger = logging.getLogger(__name__)

HAIKU_MODEL = "claude-haiku-4-5-20251001"


def _run_simple_backtest(df: pd.DataFrame, signal_type: str) -> dict:
    """
    シンプルバックテスト:
    signal_type: 'ma_cross' | 'rsi_oversold' | 'breakout'
    """
    if len(df) < 60:
        return {"win_rate": 0, "avg_rr": 0, "max_dd": 0, "sample_cnt": 0}

    close = df["Close"]
    trades = []

    if signal_type == "ma_cross":
        ma5 = close.rolling(5).mean()
        ma20 = close.rolling(20).mean()
        for i in range(20, len(df) - 5):
            # ゴールデンクロス（MA5がMA20を上抜け）
            if ma5.iloc[i-1] <= ma20.iloc[i-1] and ma5.iloc[i] > ma20.iloc[i]:
                entry = float(close.iloc[i])
                stop = entry * 0.97
                target = entry * 1.06
                # 5日後の結果
                exit_price = float(close.iloc[min(i+5, len(df)-1)])
                win = exit_price >= target or (exit_price > entry and exit_price > stop)
                rr = (exit_price - entry) / (entry - stop + 1e-10)
                trades.append({"win": win, "rr": rr})

    elif signal_type == "rsi_oversold":
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rsi = 100 - 100 / (1 + gain / (loss + 1e-10))
        for i in range(20, len(df) - 5):
            if rsi.iloc[i-1] > 30 and rsi.iloc[i] <= 30:  # RSI30割れ
                entry = float(close.iloc[i])
                stop = entry * 0.95
                target = entry * 1.08
                exit_price = float(close.iloc[min(i+5, len(df)-1)])
                win = exit_price >= target
                rr = (exit_price - entry) / (entry - stop + 1e-10)
                trades.append({"win": win, "rr": rr})

    elif signal_type == "breakout":
        high20 = df["High"].rolling(20).max()
        for i in range(21, len(df) - 5):
            if float(close.iloc[i]) > float(high20.iloc[i-1]) * 1.01:
                entry = float(close.iloc[i])
                stop = entry * 0.96
                target = entry * 1.10
                exit_price = float(close.iloc[min(i+5, len(df)-1)])
                win = exit_price >= target
                rr = (exit_price - entry) / (entry - stop + 1e-10)
                trades.append({"win": win, "rr": rr})

    if not trades:
        return {"win_rate": 0, "avg_rr": 0, "max_dd": 0, "sample_cnt": 0}

    win_rate = sum(1 for t in trades if t["win"]) / len(trades)
    avg_rr = sum(t["rr"] for t in trades) / len(trades)

    # 簡易最大ドローダウン
    equity = [1.0]
    for t in trades:
        equity.append(equity[-1] * (1 + t["rr"] * 0.02))  # 2%リスク想定
    peak = equity[0]
    max_dd = 0.0
    for v in equity:
        if v > peak:
            peak = v
        dd = (peak - v) / peak
        max_dd = max(max_dd, dd)

    return {
        "win_rate": round(win_rate, 3),
        "avg_rr": round(avg_rr, 3),
        "max_dd": round(max_dd, 3),
        "sample_cnt": len(trades),
    }


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

    stats = _run_simple_backtest(df, signal_type)

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
        signal_type = _infer_signal_type(sig)
        bt = validate_signal(client, symbol, signal_type)
        if bt.get("viable", False) and bt.get("sample_cnt", 0) >= 5:
            validated.append({**sig, "backtest": bt})
    logger.info(f"[backtest] バックテスト通過: {[v['symbol'] for v in validated]}")
    return validated


def _infer_signal_type(signal: dict) -> str:
    """シグナル辞書からシグナルタイプを推定"""
    reasoning = signal.get("reasoning", "").lower()
    if "rsi" in reasoning or "売られ" in signal.get("reasoning", ""):
        return "rsi_oversold"
    if "ブレイク" in signal.get("reasoning", "") or "breakout" in reasoning:
        return "breakout"
    return "ma_cross"
