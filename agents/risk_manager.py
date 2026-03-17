"""
RiskManager Agent (claude-sonnet-4-6)
ポートフォリオ全体のリスクを評価し、最終的な注文サイズと実行可否を決定する
"""
import json
import logging
import os
from datetime import date

import anthropic

from tools.db import get_positions, get_latest_snapshot

logger = logging.getLogger(__name__)

SONNET_MODEL = "claude-sonnet-4-6"

MAX_POSITION_SIZE = float(os.getenv("MAX_POSITION_SIZE", "100000"))
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "5"))
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.02"))


def run_risk_management(client: anthropic.Anthropic, validated_signals: list[dict]) -> list[dict]:
    """
    バックテスト通過済みシグナルにリスク管理フィルタを適用
    Returns: 実行可能な注文リスト
    """
    if not validated_signals:
        return []

    # 現在のポートフォリオ状態をDBから取得
    positions = get_positions()
    snapshot = get_latest_snapshot()
    cash = snapshot["cash"] if snapshot else 1_000_000.0
    total_value = snapshot["total_value"] if snapshot else cash

    # ポジション数チェック
    current_position_count = len(positions)
    available_slots = MAX_POSITIONS - current_position_count

    # リスク評価のためのコンテキスト
    portfolio_context = {
        "date": date.today().isoformat(),
        "cash": cash,
        "total_value": total_value,
        "current_positions": current_position_count,
        "max_positions": MAX_POSITIONS,
        "available_slots": available_slots,
        "risk_per_trade_pct": RISK_PER_TRADE * 100,
        "max_position_size_jpy": MAX_POSITION_SIZE,
        "existing_symbols": [p["symbol"] for p in positions],
    }

    signals_summary = []
    for sig in validated_signals:
        bt = sig.get("backtest", {})
        signals_summary.append({
            "symbol": sig["symbol"],
            "signal": sig["signal"],
            "confidence": sig.get("confidence", 0),
            "entry_price": sig.get("entry_price", 0),
            "stop_loss": sig.get("stop_loss", 0),
            "take_profit": sig.get("take_profit", 0),
            "reasoning": sig.get("reasoning", ""),
            "backtest_win_rate": bt.get("win_rate", 0),
            "backtest_avg_rr": bt.get("avg_rr", 0),
            "backtest_sample_cnt": bt.get("sample_cnt", 0),
        })

    prompt = f"""あなたはプロのリスクマネージャーです。以下のポートフォリオ状況とトレードシグナルを評価してください。

【ポートフォリオ状況】
{json.dumps(portfolio_context, ensure_ascii=False, indent=2)}

【トレードシグナル候補】
{json.dumps(signals_summary, ensure_ascii=False, indent=2)}

各シグナルについて以下を判断してください：
1. リスクリワード比が1.5以上あるか
2. バックテスト勝率が40%以上かつサンプル数10以上か
3. 既存ポジションとの重複・相関リスクはないか
4. 資金的に実行可能か（スロット残あり、キャッシュ十分）
5. 各注文の株数（ポジションサイズ = リスク許容額 / (エントリー - ストップロス)）

必ず以下のJSON形式のみで回答（他のテキスト不要）：
{{
  "approved_orders": [
    {{
      "symbol": "銘柄コード",
      "action": "buy" | "sell",
      "quantity": 株数(整数),
      "price": エントリー価格,
      "stop_loss": ストップロス価格,
      "take_profit": 利確価格,
      "reason": "実行理由（日本語100字以内）"
    }}
  ],
  "rejected": [
    {{"symbol": "銘柄コード", "reason": "却下理由"}}
  ],
  "overall_assessment": "ポートフォリオ全体の評価コメント"
}}
"""
    try:
        response = client.messages.create(
            model=SONNET_MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        result = json.loads(text)
    except Exception as e:
        logger.error(f"[risk] LLMレスポンス解析失敗: {e}")
        result = {"approved_orders": [], "rejected": [], "overall_assessment": "解析失敗"}

    approved = result.get("approved_orders", [])
    rejected = result.get("rejected", [])
    assessment = result.get("overall_assessment", "")

    # スロット制限の強制チェック
    approved = approved[:available_slots]

    # validated_signals の strategy_name を承認注文に引き継ぐ
    strategy_map = {sig["symbol"]: sig.get("strategy_name", "") for sig in validated_signals}
    for order in approved:
        order["strategy_name"] = strategy_map.get(order["symbol"], "")

    logger.info(f"[risk] 承認: {[o['symbol'] for o in approved]}, "
                f"却下: {[r['symbol'] for r in rejected]}")
    logger.info(f"[risk] 総評: {assessment}")

    return approved
