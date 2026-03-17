"""
Orchestrator Agent (claude-opus-4-6)
全エージェントを統括し、最終的なトレード判断を下す
"""
import json
import logging
from datetime import date

import anthropic

from agents.market_scanner import run_market_scanner
from agents.technical_analyst import run_technical_analysis
from agents.backtest_validator import run_backtest_validation
from agents.risk_manager import run_risk_management
from tools.executor import TradeExecutor
from tools.futu_client import FutuClient
from tools.db import get_positions, get_latest_snapshot

logger = logging.getLogger(__name__)

OPUS_MODEL = "claude-opus-4-6"


def run_orchestrator(dry_run: bool = False):
    """
    メインオーケストレーター実行
    dry_run=True の場合は注文を発行しない（ペーパートレード）
    """
    client = anthropic.Anthropic()
    today = date.today().isoformat()

    logger.info(f"=" * 60)
    logger.info(f"[orchestrator] 実行開始 {today} dry_run={dry_run}")
    logger.info(f"=" * 60)

    # Step 1: MarketScanner — 軽量スクリーニング + 差分データ取得
    logger.info("[orchestrator] Step1: MarketScanner 実行")
    candidates = run_market_scanner(client)

    if not candidates:
        logger.info("[orchestrator] 候補銘柄なし。本日は終了。")
        return {"status": "no_candidates", "orders": []}

    logger.info(f"[orchestrator] 候補銘柄 ({len(candidates)}): {[c['symbol'] for c in candidates]}")

    # Step 2: TechnicalAnalyst — DBデータで分析
    logger.info("[orchestrator] Step2: TechnicalAnalyst 実行")
    signals = run_technical_analysis(client, candidates)

    if not signals:
        logger.info("[orchestrator] トレードシグナルなし。本日は終了。")
        return {"status": "no_signals", "candidates": candidates, "orders": []}

    # Step 3: BacktestValidator — DBキャッシュ優先で検証
    logger.info("[orchestrator] Step3: BacktestValidator 実行")
    validated = run_backtest_validation(client, signals)

    if not validated:
        logger.info("[orchestrator] バックテスト通過シグナルなし。本日は終了。")
        return {"status": "no_validated_signals", "signals": signals, "orders": []}

    # Step 4: RiskManager — ポートフォリオリスク評価
    logger.info("[orchestrator] Step4: RiskManager 実行")
    approved_orders = run_risk_management(client, validated)

    if not approved_orders:
        logger.info("[orchestrator] 承認注文なし。本日は終了。")
        return {"status": "no_approved_orders", "validated": validated, "orders": []}

    # Step 5: Orchestrator最終判断
    logger.info("[orchestrator] Step5: 最終判断")
    final_orders = _final_decision(client, approved_orders, candidates, signals)

    # Step 6: 実行
    futu = FutuClient()
    executor = TradeExecutor(futu)

    if not dry_run:
        try:
            futu.connect()
            results = executor.execute_orders(final_orders)
            executor.sync_positions_to_db()
            executor.save_portfolio_snapshot()
        finally:
            futu.disconnect()
    else:
        logger.info(f"[orchestrator] DRY RUN: 以下の注文を発行予定:")
        for o in final_orders:
            logger.info(f"  {o['symbol']} {o['action']} {o['quantity']}株 @{o['price']}")
        results = [{**o, "status": "dry_run"} for o in final_orders]
        # dry runでもスナップショットは保存
        futu.connect()
        executor.save_portfolio_snapshot()
        futu.disconnect()

    logger.info(f"[orchestrator] 完了。実行注文: {len(results)}件")
    return {
        "status": "completed",
        "date": today,
        "candidates": [c["symbol"] for c in candidates],
        "signals": [s["symbol"] for s in signals],
        "validated": [v["symbol"] for v in validated],
        "orders": results,
    }


def _final_decision(client: anthropic.Anthropic,
                    approved_orders: list[dict],
                    candidates: list[dict],
                    signals: list[dict]) -> list[dict]:
    """
    Orchestratorが最終的にOpusモデルで判断
    リスクマネージャーが承認した注文に対して最終GoサインをつけるOR削減する
    """
    if len(approved_orders) <= 1:
        return approved_orders

    context = {
        "today": date.today().isoformat(),
        "approved_orders": approved_orders,
        "candidate_scores": [{"symbol": c["symbol"], "score": c["score"]} for c in candidates],
    }

    prompt = f"""本日({date.today().isoformat()})の自律売買システムの最終判断をお願いします。

リスクマネージャーが承認した注文候補:
{json.dumps(approved_orders, ensure_ascii=False, indent=2)}

市場スキャンスコア:
{json.dumps(context['candidate_scores'], ensure_ascii=False, indent=2)}

以下の観点で最終確認してください:
1. 市場全体の状況（複数銘柄が同方向に動いている場合、単一セクター集中リスク）
2. 注文間の相関・集中リスク
3. 本日の実行推奨度（全実行/一部/見送り）

最終的に実行する注文のみをJSON形式で返してください（他のテキスト不要）:
[
  {{
    "symbol": "銘柄コード",
    "action": "buy" | "sell",
    "quantity": 株数,
    "price": 価格,
    "stop_loss": ストップロス,
    "take_profit": 利確,
    "reason": "最終承認理由"
  }}
]
"""
    try:
        response = client.messages.create(
            model=OPUS_MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        final = json.loads(text)
        # LLMの出力に strategy_name がないため、承認済み注文から引き継ぐ
        strategy_map = {o["symbol"]: o.get("strategy_name", "") for o in approved_orders}
        for order in final:
            order["strategy_name"] = strategy_map.get(order["symbol"], "")
        logger.info(f"[orchestrator] 最終承認: {[o['symbol'] for o in final]}")
        return final
    except Exception as e:
        logger.error(f"[orchestrator] 最終判断失敗 ({e})、リスクマネージャー承認結果をそのまま使用")
        return approved_orders
