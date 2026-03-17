"""
TechnicalAnalyst Agent (claude-haiku-4-5)
DBのOHLCVデータでテクニカル分析を行いシグナルを生成する
API再取得は行わない（DBのみ使用）
"""
import json
import logging
from datetime import date, timedelta
from typing import Optional

import anthropic
import pandas as pd

from tools.db import get_prices

logger = logging.getLogger(__name__)

HAIKU_MODEL = "claude-haiku-4-5-20251001"


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


def analyze_symbol(client: anthropic.Anthropic, symbol: str, name: str = "") -> Optional[dict]:
    """
    単一銘柄のテクニカル分析を実行
    Returns: {"symbol", "signal", "confidence", "entry_price", "stop_loss", "take_profit", "reasoning"}
    """
    start = (date.today() - timedelta(days=120)).isoformat()
    df = get_prices(symbol, start=start)

    if df.empty or len(df) < 20:
        logger.warning(f"[analyst] {symbol}: データ不足 ({len(df)}件)")
        return None

    indicators = _compute_indicators(df)
    if not indicators:
        return None

    # 直近5日の価格推移をサマリ
    recent = df.tail(5)[["Close", "Volume"]].to_string()

    prompt = f"""銘柄: {symbol} {name}
本日: {date.today().isoformat()}

【テクニカル指標】
{json.dumps(indicators, ensure_ascii=False, indent=2)}

【直近5日の価格・出来高】
{recent}

以上を分析し、本日のトレードシグナルを判定してください。
考慮事項:
- トレンドの方向性（MA5/MA20/MA60の並び）
- RSIによる過買い・過売り判定（70以上=過買い, 30以下=過売り）
- MACDのゴールデン/デッドクロス
- ボリンジャーバンドの位置
- 出来高の増減

必ず以下のJSON形式のみで回答（他のテキスト不要）：
{{
  "signal": "buy" | "sell" | "hold",
  "confidence": 0.0〜1.0,
  "entry_price": 数値,
  "stop_loss": 数値,
  "take_profit": 数値,
  "reasoning": "根拠（日本語100字以内）"
}}
"""
    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        result = json.loads(text)
        result["symbol"] = symbol
        result["indicators"] = indicators
        logger.info(f"[analyst] {symbol}: signal={result['signal']} confidence={result['confidence']}")
        return result
    except Exception as e:
        logger.error(f"[analyst] {symbol}: 分析失敗 - {e}")
        return None


def run_technical_analysis(client: anthropic.Anthropic, candidates: list[dict]) -> list[dict]:
    """
    候補銘柄リストに対してテクニカル分析を実行
    Returns: シグナルありの銘柄リスト
    """
    signals = []
    for c in candidates:
        symbol = c["symbol"]
        result = analyze_symbol(client, symbol)
        if result and result.get("signal") != "hold":
            signals.append(result)
    logger.info(f"[analyst] シグナル銘柄: {[s['symbol'] for s in signals]}")
    return signals
