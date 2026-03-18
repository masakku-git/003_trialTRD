"""
RiskManager（ルールベース）
ポートフォリオ全体のリスクを評価し、最終的な注文サイズと実行可否を決定する
"""
import logging
import math
import os

from tools.db import get_positions, get_latest_snapshot

logger = logging.getLogger(__name__)

MAX_POSITION_SIZE = float(os.getenv("MAX_POSITION_SIZE", "100000"))
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "5"))
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.02"))

# リスク管理の通過基準
MIN_RR_RATIO = 1.5
MIN_BACKTEST_WIN_RATE = 0.40
MIN_BACKTEST_SAMPLE = 10


def run_risk_management(validated_signals: list[dict],
                        market_context: dict = None) -> list[dict]:
    """
    バックテスト通過済みシグナルにリスク管理フィルタを適用
    market_context: MarketResearcherの分析結果（ポジションサイズ調整・戦略ブロック）
    Returns: 実行可能な注文リスト
    """
    if not validated_signals:
        return []

    # MarketContext によるブロック戦略フィルタ
    blocked_strategies = []
    size_multiplier = 1.0
    if market_context:
        gate = market_context.get("trading_gate", {})
        blocked_strategies = gate.get("blocked_strategies", [])
        size_multiplier = gate.get("position_size_multiplier", 1.0)
        logger.info(f"[risk] MarketContext: multiplier={size_multiplier}, "
                    f"blocked={blocked_strategies}")

    if "all" in blocked_strategies:
        logger.info("[risk] 全戦略ブロック — 注文なし")
        return []

    # ブロック対象戦略を事前フィルタ
    if blocked_strategies:
        before_count = len(validated_signals)
        validated_signals = [
            s for s in validated_signals
            if s.get("strategy_name", "") not in blocked_strategies
        ]
        filtered = before_count - len(validated_signals)
        if filtered:
            logger.info(f"[risk] 戦略ブロックで{filtered}件除外")

    # 現在のポートフォリオ状態をDBから取得
    positions = get_positions()
    snapshot = get_latest_snapshot()
    cash = snapshot["cash"] if snapshot else 1_000_000.0
    total_value = snapshot["total_value"] if snapshot else cash

    # ポジション数チェック
    current_position_count = len(positions)
    available_slots = MAX_POSITIONS - current_position_count
    existing_symbols = {p["symbol"] for p in positions}

    if available_slots <= 0:
        logger.info(f"[risk] ポジション枠なし ({current_position_count}/{MAX_POSITIONS})")
        return []

    approved = []
    rejected = []

    for sig in validated_signals:
        symbol = sig["symbol"]
        bt = sig.get("backtest", {})
        entry_price = sig.get("entry_price", 0)
        stop_loss = sig.get("stop_loss", 0)
        take_profit = sig.get("take_profit", 0)

        # 既存ポジションとの重複チェック
        if symbol in existing_symbols:
            rejected.append({"symbol": symbol, "reason": "既存ポジションと重複"})
            continue

        # リスクリワード比チェック
        if entry_price and stop_loss and take_profit:
            risk = abs(entry_price - stop_loss)
            reward = abs(take_profit - entry_price)
            rr_ratio = reward / risk if risk > 0 else 0
        else:
            rr_ratio = bt.get("avg_rr", 0)

        if rr_ratio < MIN_RR_RATIO:
            rejected.append({"symbol": symbol, "reason": f"RR比不足 ({rr_ratio:.2f} < {MIN_RR_RATIO})"})
            continue

        # バックテスト統計チェック
        if bt.get("win_rate", 0) < MIN_BACKTEST_WIN_RATE:
            rejected.append({"symbol": symbol, "reason": f"勝率不足 ({bt.get('win_rate', 0):.0%})"})
            continue
        if bt.get("sample_cnt", 0) < MIN_BACKTEST_SAMPLE:
            rejected.append({"symbol": symbol, "reason": f"サンプル不足 ({bt.get('sample_cnt', 0)}件)"})
            continue

        # ポジションサイズ計算: リスク許容額 / (エントリー - ストップロス)
        risk_amount = total_value * RISK_PER_TRADE
        if entry_price and stop_loss:
            risk_per_share = abs(entry_price - stop_loss)
            if risk_per_share > 0:
                quantity = int(math.floor(risk_amount / risk_per_share))
            else:
                quantity = 0
        else:
            # SL未設定の場合、MAX_POSITION_SIZE / エントリー価格で概算
            quantity = int(math.floor(MAX_POSITION_SIZE / entry_price)) if entry_price else 0

        # MarketContextによるサイズ調整
        quantity = int(math.floor(quantity * size_multiplier))

        # ポジションサイズ上限チェック
        position_value = quantity * entry_price if entry_price else 0
        if position_value > MAX_POSITION_SIZE:
            quantity = int(math.floor(MAX_POSITION_SIZE / entry_price))

        # キャッシュ不足チェック
        if position_value > cash:
            quantity = int(math.floor(cash / entry_price)) if entry_price else 0

        if quantity <= 0:
            rejected.append({"symbol": symbol, "reason": "ポジションサイズがゼロ"})
            continue

        # スロット制限
        if len(approved) >= available_slots:
            rejected.append({"symbol": symbol, "reason": "ポジション枠超過"})
            continue

        order = {
            "symbol": symbol,
            "action": sig.get("signal", "buy"),
            "quantity": quantity,
            "price": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "reason": sig.get("reasoning", ""),
            "strategy_name": sig.get("strategy_name", ""),
        }
        approved.append(order)

    logger.info(f"[risk] 承認: {[o['symbol'] for o in approved]}, "
                f"却下: {[r['symbol'] for r in rejected]}")
    return approved
