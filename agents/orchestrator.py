"""
Orchestrator（ルールベース）
全エージェントを統括し、最終的なトレード判断を下す
"""
import logging
from datetime import date

from agents.market_researcher import run_market_researcher
from agents.market_scanner import run_market_scanner
from agents.technical_analyst import run_technical_analysis
from agents.backtest_validator import run_backtest_validation
from agents.strategy_critic import run_strategy_critic
from agents.risk_manager import run_risk_management
from tools.executor import TradeExecutor
from tools.futu_client import FutuClient

logger = logging.getLogger(__name__)


def run_orchestrator(dry_run: bool = False):
    """
    メインオーケストレーター実行
    dry_run=True の場合は注文を発行しない（ペーパートレード）
    """
    today = date.today().isoformat()

    logger.info("=" * 60)
    logger.info(f"[orchestrator] 実行開始 {today} dry_run={dry_run}")
    logger.info("=" * 60)

    # Step 0: MarketResearcher — マクロ市場分析
    logger.info("[orchestrator] Step0: MarketResearcher 実行")
    market_context = run_market_researcher()
    regime = market_context.get("regime", "UNCERTAIN")
    regime_score = market_context.get("regime_score", 0)
    logger.info(f"[orchestrator] 市場レジーム: {regime} (score={regime_score:.3f})")

    # 取引ゲートチェック
    gate = market_context.get("trading_gate", {})
    if not gate.get("allow_trading", True):
        logger.info(f"[orchestrator] 取引停止: {gate.get('reason', '市場環境が悪い')}")
        return {"status": "trading_halted", "regime": regime, "orders": []}

    # Step 1: MarketScanner — 軽量スクリーニング + 差分データ取得
    logger.info("[orchestrator] Step1: MarketScanner 実行")
    candidates = run_market_scanner()

    if not candidates:
        logger.info("[orchestrator] 候補銘柄なし。本日は終了。")
        return {"status": "no_candidates", "orders": []}

    logger.info(f"[orchestrator] 候補銘柄 ({len(candidates)}): {[c['symbol'] for c in candidates]}")

    # Step 2: TechnicalAnalyst — DBデータで分析
    logger.info("[orchestrator] Step2: TechnicalAnalyst 実行")
    signals = run_technical_analysis(candidates)

    if not signals:
        logger.info("[orchestrator] トレードシグナルなし。本日は終了。")
        return {"status": "no_signals", "candidates": candidates, "orders": []}

    # Step 3: BacktestValidator — DBキャッシュ優先で検証
    logger.info("[orchestrator] Step3: BacktestValidator 実行")
    validated = run_backtest_validation(signals)

    if not validated:
        logger.info("[orchestrator] バックテスト通過シグナルなし。本日は終了。")
        return {"status": "no_validated_signals", "signals": signals, "orders": []}

    # Step 3.5: StrategyCritic — 批判的審査（ヒューリスティック）
    logger.info("[orchestrator] Step3.5: StrategyCritic 実行")
    survived = run_strategy_critic(validated)

    if not survived:
        logger.info("[orchestrator] 批判フィルタ通過シグナルなし。本日は終了。")
        return {"status": "no_survived_signals", "validated": validated, "orders": []}

    # Step 4: RiskManager — ポートフォリオリスク評価
    logger.info("[orchestrator] Step4: RiskManager 実行")
    approved_orders = run_risk_management(survived, market_context=market_context)

    if not approved_orders:
        logger.info("[orchestrator] 承認注文なし。本日は終了。")
        return {"status": "no_approved_orders", "survived": survived, "orders": []}

    # Step 5: 最終フィルタ（ルールベース）
    logger.info("[orchestrator] Step5: 最終判断")
    final_orders = _final_decision(approved_orders, survived)

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
        logger.info("[orchestrator] DRY RUN: 以下の注文を発行予定:")
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
        "regime": regime,
        "regime_score": regime_score,
        "candidates": [c["symbol"] for c in candidates],
        "signals": [s["symbol"] for s in signals],
        "validated": [v["symbol"] for v in validated],
        "survived_critic": [s["symbol"] for s in survived],
        "orders": results,
    }


def _final_decision(approved_orders: list[dict],
                    survived: list[dict]) -> list[dict]:
    """
    最終フィルタ（ルールベース）
    StrategyCriticの批判情報を考慮し、criticality_scoreが高い銘柄を優先度下げ
    """
    if len(approved_orders) <= 1:
        return approved_orders

    # StrategyCriticの批判情報でソート（criticality_scoreが低い＝問題少ない順）
    criticism_map = {
        s["symbol"]: s.get("criticism", {})
        for s in survived
    }

    def sort_key(order):
        crit = criticism_map.get(order["symbol"], {})
        criticality = crit.get("criticality_score", 0)
        # criticality_scoreが低い（良い）ものを優先
        return criticality

    sorted_orders = sorted(approved_orders, key=sort_key)

    logger.info(f"[orchestrator] 最終承認: {[o['symbol'] for o in sorted_orders]}")
    return sorted_orders
