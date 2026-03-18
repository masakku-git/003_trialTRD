"""
StrategyCritic（ルールベース）
バックテスト通過済みシグナルを批判的に審査し、過信・落とし穴を洗い出す。
ヒューリスティックによる「悪魔の代弁者」フィルタ。
"""
import logging

logger = logging.getLogger(__name__)

# 批判レベルしきい値（0.0〜1.0: 数値が高いほど厳格）
REJECT_THRESHOLD = 0.7   # criticality_score がこれ以上 → 却下
CAUTION_THRESHOLD = 0.4  # これ以上 → 信頼度を0.7倍に減衰


def _assess_signal_quality(signal: dict) -> dict:
    """
    バックテスト統計の構造的弱点をヒューリスティックに評価する
    Returns: {"flags": [...], "score": 0.0〜1.0}
    """
    bt = signal.get("backtest", {})
    flags = []
    score = 0.0

    sample_cnt = bt.get("sample_cnt", 0)
    win_rate = bt.get("win_rate", 0)
    avg_rr = bt.get("avg_rr", 0)
    max_dd = bt.get("max_dd", 0)
    indicators = signal.get("indicators", {})

    # サンプル数が少ない（過学習リスク）
    if sample_cnt < 15:
        flags.append(f"サンプル数が少ない ({sample_cnt}件) — 統計的信頼性が低い")
        score += 0.3
    elif sample_cnt < 30:
        flags.append(f"サンプル数がやや少ない ({sample_cnt}件)")
        score += 0.1

    # 勝率が高すぎる（バックテストの過最適化）
    if win_rate > 0.75:
        flags.append(f"勝率が高すぎる ({win_rate:.0%}) — 過学習・カーブフィッティングの疑い")
        score += 0.25

    # 最大DDが大きい
    if max_dd > 0.20:
        flags.append(f"最大ドローダウンが大きい ({max_dd:.0%})")
        score += 0.2

    # 平均RRが低い（期待値が薄い）
    if avg_rr < 0.5:
        flags.append(f"平均RR比が低い ({avg_rr:.2f}) — コスト考慮後はマイナス期待値になり得る")
        score += 0.2

    # テクニカル指標の矛盾チェック
    sig_dir = signal.get("signal", "hold")
    rsi = indicators.get("rsi14", 50)
    if sig_dir == "buy" and rsi > 70:
        flags.append(f"買いシグナルだがRSI={rsi:.0f} (過買い圏) — 逆張りリスク")
        score += 0.2
    if sig_dir == "sell" and rsi < 30:
        flags.append(f"売りシグナルだがRSI={rsi:.0f} (過売り圏) — 逆張りリスク")
        score += 0.2

    # MACDと方向の矛盾
    macd = indicators.get("macd", 0)
    macd_sig = indicators.get("macd_signal", 0)
    if sig_dir == "buy" and macd < macd_sig:
        flags.append("買いシグナルだがMACDがシグナル線を下回っている")
        score += 0.1
    if sig_dir == "sell" and macd > macd_sig:
        flags.append("売りシグナルだがMACDがシグナル線を上回っている")
        score += 0.1

    return {"flags": flags, "score": min(score, 1.0)}


def critique_signal(signal: dict) -> dict:
    """
    単一シグナルをヒューリスティックに審査する。
    Returns: {
        "symbol": str,
        "verdict": "approve" | "caution" | "reject",
        "criticality_score": float,
        "red_flags": [str],
    }
    """
    symbol = signal["symbol"]
    assessment = _assess_signal_quality(signal)
    score = assessment["score"]
    flags = assessment["flags"]

    verdict = _score_to_verdict(score)

    logger.info(
        f"[critic] {symbol}: verdict={verdict} "
        f"criticality={score:.2f} flags={len(flags)}件"
    )

    return {
        "symbol": symbol,
        "verdict": verdict,
        "criticality_score": round(score, 3),
        "red_flags": flags,
    }


def _score_to_verdict(score: float) -> str:
    if score >= REJECT_THRESHOLD:
        return "reject"
    if score >= CAUTION_THRESHOLD:
        return "caution"
    return "approve"


def run_strategy_critic(validated_signals: list[dict]) -> list[dict]:
    """
    バックテスト通過済みシグナルを批判的に審査する。
    - reject → リストから除外
    - caution → 信頼度を0.7倍に減衰して通過（criticismを付与）
    - approve → そのまま通過

    Returns: 批判フィルタ通過済みシグナルリスト（criticism情報を含む）
    """
    if not validated_signals:
        return []

    passed = []
    for sig in validated_signals:
        criticism = critique_signal(sig)
        verdict = criticism["verdict"]

        if verdict == "reject":
            logger.warning(
                f"[critic] {sig['symbol']} を却下: "
                f"{', '.join(criticism['red_flags'][:2])}"
            )
            continue

        # シグナルに批判情報を付与
        updated_sig = {**sig, "criticism": criticism}

        if verdict == "caution":
            # 信頼度を0.7倍に減衰
            original_conf = updated_sig.get("confidence", 0.5)
            updated_sig["confidence"] = round(original_conf * 0.7, 3)
            logger.info(
                f"[critic] {sig['symbol']} は要注意通過 "
                f"(confidence: {original_conf:.2f} → {updated_sig['confidence']:.2f})"
            )
        else:
            logger.info(f"[critic] {sig['symbol']} は承認通過")

        passed.append(updated_sig)

    logger.info(
        f"[critic] 審査結果: 入力={len(validated_signals)}件 → "
        f"通過={len(passed)}件 "
        f"(却下={len(validated_signals) - len(passed)}件)"
    )
    return passed
