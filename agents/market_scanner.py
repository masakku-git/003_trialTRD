"""
MarketScanner Agent (claude-haiku-4-5)
軽量スクリーニング：全ウォッチリストの直近2日データを取得し候補銘柄を絞る
候補のみに対して差分データを補完する
"""
import json
import logging
import os
from datetime import date

import anthropic

from tools.data_fetcher import fetch_latest_2days, fetch_prices_incremental
from tools.db import get_active_symbols, save_screening_result

logger = logging.getLogger(__name__)

HAIKU_MODEL = "claude-haiku-4-5-20251001"


def run_market_scanner(client: anthropic.Anthropic) -> list[dict]:
    """
    Returns: スクリーニング通過銘柄のリスト
    [{"symbol", "score", "reason", "change_pct", "vol_ratio"}]
    """
    symbols = get_active_symbols()
    logger.info(f"[scanner] ウォッチリスト: {len(symbols)}銘柄")

    # 全銘柄の軽量データ取得（直近2日のみ）
    market_data = []
    for symbol in symbols:
        data = fetch_latest_2days(symbol)
        if data:
            market_data.append(data)

    if not market_data:
        logger.warning("[scanner] 市場データ取得ゼロ")
        return []

    # Claude Haikuにスクリーニング判断を依頼
    prompt = f"""以下は本日({date.today().isoformat()})の東証銘柄の前日比データです。

{json.dumps(market_data, ensure_ascii=False, indent=2)}

以下の条件で上位3〜5銘柄を選定してください：
- 前日比変化率(change_pct)が±2%以上
- 出来高比率(vol_ratio)が1.5倍以上（出来高急増）
- または変化率が+3%以上の強い上昇銘柄

必ず以下のJSON形式のみで回答してください（他のテキスト不要）：
[
  {{"symbol": "銘柄コード", "score": 0.0〜1.0, "reason": "選定理由（日本語50字以内）"}},
  ...
]
"""
    response = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    try:
        text = response.content[0].text.strip()
        # JSON部分を抽出
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        candidates = json.loads(text)
    except Exception as e:
        logger.error(f"[scanner] LLMレスポンスのパース失敗: {e}\n{response.content[0].text}")
        # フォールバック：変化率・出来高で機械的に選定
        candidates = _fallback_screening(market_data)

    today_str = date.today().isoformat()
    result = []
    for c in candidates:
        symbol = c["symbol"]
        score = float(c.get("score", 0.5))
        reason = c.get("reason", "")
        save_screening_result(today_str, symbol, score, reason)

        # 候補銘柄のみ差分データをDB補完
        logger.info(f"[scanner] {symbol}: 差分データ取得開始")
        fetch_prices_incremental(symbol)

        # market_dataから詳細情報を付加
        mdata = next((m for m in market_data if m["symbol"] == symbol), {})
        result.append({
            "symbol": symbol,
            "score": score,
            "reason": reason,
            "change_pct": mdata.get("change_pct", 0),
            "vol_ratio": mdata.get("vol_ratio", 0),
            "close": mdata.get("close", 0),
        })

    logger.info(f"[scanner] 候補銘柄: {[r['symbol'] for r in result]}")
    return result


def _fallback_screening(market_data: list[dict]) -> list[dict]:
    """LLM失敗時のフォールバック：スコアリングで機械選定"""
    scored = []
    for d in market_data:
        score = 0.0
        if abs(d.get("change_pct", 0)) >= 2.0:
            score += 0.4
        if d.get("vol_ratio", 0) >= 1.5:
            score += 0.4
        if d.get("change_pct", 0) >= 3.0:
            score += 0.2
        if score > 0:
            scored.append({"symbol": d["symbol"], "score": round(score, 2),
                           "reason": f"変化率{d['change_pct']}% 出来高比{d['vol_ratio']}x"})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:5]
