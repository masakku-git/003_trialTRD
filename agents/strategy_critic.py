"""
StrategyCritic Agent (claude-sonnet-4-6)
バックテスト通過済みシグナルを批判的に審査し、過信・落とし穴を洗い出す。
「悪魔の代弁者」として機能し、承認前のシグナルに反論を提示する。
"""
import json
import logging
from datetime import date, timedelta

import anthropic

from tools.db import get_prices

logger = logging.getLogger(__name__)

SONNET_MODEL = "claude-sonnet-4-6"

# 批判レベルしきい値（0.0〜1.0: 数値が高いほど厳格）
REJECT_THRESHOLD = 0.7   # criticality_score がこれ以上 → 却下
CAUTION_THRESHOLD = 0.4  # これ以上 → 信頼度を0.7倍に減衰


def _assess_signal_quality(signal: dict) -> dict:
    """
    バックテスト統計の構造的弱点をヒューリスティックに評価する（LLM呼び出し前の事前チェック）
    Returns: {"flags": [...], "pre_score": 0.0〜1.0}
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

    return {"flags": flags, "pre_score": min(score, 1.0)}


def critique_signal(client: anthropic.Anthropic, signal: dict) -> dict:
    """
    単一シグナルを批判的に審査する。
    Returns: {
        "symbol": str,
        "verdict": "approve" | "caution" | "reject",
        "criticality_score": float,  # 0.0(問題なし) 〜 1.0(危険)
        "red_flags": [str],
        "llm_criticism": str,
    }
    """
    symbol = signal["symbol"]
    pre = _assess_signal_quality(signal)
    pre_flags = pre["flags"]
    pre_score = pre["pre_score"]

    bt = signal.get("backtest", {})
    indicators = signal.get("indicators", {})

    prompt = f"""あなたは懐疑的な取引戦略アナリストです。
以下のトレードシグナルの「なぜこの取引はうまくいかないか」を批判的に評価してください。
感情を持たず、客観的な反論と潜在的リスクを指摘することがあなたの役割です。

【銘柄】{symbol}
【本日】{date.today().isoformat()}
【シグナル】{signal.get('signal')} (信頼度: {signal.get('confidence', 0):.0%})
【根拠】{signal.get('reasoning', '不明')}
【エントリー】{signal.get('entry_price')} / SL: {signal.get('stop_loss')} / TP: {signal.get('take_profit')}

【バックテスト統計】
- 勝率: {bt.get('win_rate', 0):.0%}
- 平均RR: {bt.get('avg_rr', 0):.2f}
- 最大DD: {bt.get('max_dd', 0):.0%}
- サンプル数: {bt.get('sample_cnt', 0)}件
- キャッシュ利用: {bt.get('from_cache', False)}

【テクニカル指標】
{json.dumps(indicators, ensure_ascii=False, indent=2)}

【事前フラグ】
{chr(10).join(f'- {f}' for f in pre_flags) if pre_flags else '特になし'}

以下の観点から批判的に評価してください:
1. バックテスト期間の市場環境依存性（トレンド相場 vs レンジ相場）
2. 取引コスト（スプレッド・手数料）考慮後の期待値
3. 現在の指標が矛盾していないか
4. このシグナルが「今日」機能しない理由
5. 90日間のデータしかない場合の過学習リスク

必ず以下のJSON形式のみで回答（他のテキスト不要）：
{{
  "criticality_score": 0.0〜1.0,
  "red_flags": ["指摘事項1", "指摘事項2"],
  "main_criticism": "最も重大な問題点（日本語100字以内）",
  "verdict": "approve" | "caution" | "reject"
}}

criticality_scoreの基準:
- 0.0〜0.3: 問題なし → approve
- 0.4〜0.6: 要注意 → caution
- 0.7〜1.0: 重大な懸念 → reject
"""

    try:
        response = client.messages.create(
            model=SONNET_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        result = json.loads(text)
    except Exception as e:
        logger.error(f"[critic] {symbol}: LLM評価失敗 ({e})、事前スコアで判定")
        result = {
            "criticality_score": pre_score,
            "red_flags": pre_flags,
            "main_criticism": "LLM評価失敗",
            "verdict": _score_to_verdict(pre_score),
        }

    # 事前スコアとLLMスコアを合成（LLM重視・事前チェックを補完）
    llm_score = float(result.get("criticality_score", pre_score))
    final_score = min(llm_score * 0.7 + pre_score * 0.3, 1.0)

    all_flags = pre_flags + result.get("red_flags", [])
    verdict = result.get("verdict", _score_to_verdict(final_score))

    # スコアとverdictの整合性を強制
    if final_score >= REJECT_THRESHOLD:
        verdict = "reject"
    elif final_score >= CAUTION_THRESHOLD and verdict == "approve":
        verdict = "caution"

    logger.info(
        f"[critic] {symbol}: verdict={verdict} "
        f"criticality={final_score:.2f} flags={len(all_flags)}件"
    )

    return {
        "symbol": symbol,
        "verdict": verdict,
        "criticality_score": round(final_score, 3),
        "red_flags": all_flags,
        "llm_criticism": result.get("main_criticism", ""),
    }


def _score_to_verdict(score: float) -> str:
    if score >= REJECT_THRESHOLD:
        return "reject"
    if score >= CAUTION_THRESHOLD:
        return "caution"
    return "approve"


def run_strategy_critic(client: anthropic.Anthropic, validated_signals: list[dict]) -> list[dict]:
    """
    バックテスト通過済みシグナルを批判的に審査する。
    - reject → リストから除外
    - caution → 信頼度を減衰して通過（criticismを付与）
    - approve → そのまま通過

    Returns: 批判フィルタ通過済みシグナルリスト（criticism情報を含む）
    """
    if not validated_signals:
        return []

    passed = []
    for sig in validated_signals:
        criticism = critique_signal(client, sig)
        verdict = criticism["verdict"]

        if verdict == "reject":
            logger.warning(
                f"[critic] {sig['symbol']} を却下: {criticism['llm_criticism']}"
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
