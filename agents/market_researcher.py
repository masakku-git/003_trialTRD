"""
MarketResearcher（ルールベース）
マクロ市場分析：市場レジーム分類・セクター分析・ボラティリティ評価
MarketScannerの前段（Step 0）として実行され、全体の取引判断に影響を与える
"""
import logging
from datetime import date

import pandas as pd
import yfinance as yf

from tools.data_fetcher import fetch_latest_2days
from tools.db import get_active_symbols, get_market_context, save_market_context

logger = logging.getLogger(__name__)

# ウォッチリスト銘柄のセクター分類
SECTOR_MAP = {
    "Automotive":   ["7203.T", "7267.T"],
    "Technology":   ["6758.T", "6861.T", "8035.T", "6501.T"],
    "Finance":      ["8306.T"],
    "Pharma":       ["4519.T", "4502.T", "4568.T"],
    "Telecom":      ["9432.T", "9433.T"],
    "Industrial":   ["6367.T", "6954.T"],
    "Chemical":     ["4063.T"],
    "IT/Services":  ["9984.T", "2413.T"],
    "Consumer":     ["7974.T", "4661.T"],
    "Trading":      ["8001.T"],
}


def run_market_researcher() -> dict:
    """
    メイン実行関数。MarketContextを返す。
    日次キャッシュがあればそれを返却し、なければ新規計算する。
    """
    today_str = date.today().isoformat()

    # 日次キャッシュチェック
    cached = get_market_context(today_str)
    if cached:
        logger.info(f"[researcher] キャッシュヒット ({today_str}): regime={cached.get('regime')}")
        return cached

    logger.info("[researcher] マクロ市場分析開始")

    # 指数データ取得（N225 + TOPIX 一括）
    index_data = _fetch_index_data()

    # 各分析実行
    index_analysis = _analyze_indices(index_data)
    breadth = _analyze_breadth()
    volatility = _analyze_volatility(index_data)
    sector_analysis = _analyze_sectors()

    # レジーム分類
    regime, confidence, regime_score = _classify_regime(
        index_analysis, breadth, volatility
    )

    # トレーディングゲート
    trading_gate = _compute_trading_gate(regime, confidence, volatility)

    context = {
        "date": today_str,
        "regime": regime,
        "confidence": round(confidence, 3),
        "regime_score": round(regime_score, 3),
        "index_analysis": index_analysis,
        "breadth": breadth,
        "volatility": volatility,
        "sector_analysis": sector_analysis,
        "trading_gate": trading_gate,
    }

    # DB保存
    save_market_context(today_str, regime, confidence, regime_score, context)

    logger.info(f"[researcher] レジーム: {regime} (score={regime_score:.3f}, "
                f"confidence={confidence:.3f})")
    logger.info(f"[researcher] ゲート: allow={trading_gate['allow_trading']}, "
                f"multiplier={trading_gate['position_size_multiplier']}, "
                f"blocked={trading_gate['blocked_strategies']}")

    return context


# ─── データ取得 ──────────────────────────────────────────────────────────────

def _fetch_index_data() -> dict[str, pd.DataFrame]:
    """N225とTOPIXの90日データを取得"""
    result = {}
    for symbol, key in [("^N225", "nikkei225"), ("^TPX", "topix")]:
        try:
            df = yf.download(symbol, period="3mo", progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            if not df.empty:
                result[key] = df
                logger.info(f"[researcher] {key}: {len(df)}日分取得")
            else:
                logger.warning(f"[researcher] {key}: データなし")
        except Exception as e:
            logger.error(f"[researcher] {key}: 取得失敗 - {e}")
    return result


# ─── 指数分析 ────────────────────────────────────────────────────────────────

def _analyze_single_index(df: pd.DataFrame) -> dict:
    """単一指数のMA分析・変化率を算出"""
    if len(df) < 5:
        return {}

    close = df["Close"].astype(float)
    latest = float(close.iloc[-1])

    ma5 = float(close.rolling(5).mean().iloc[-1]) if len(df) >= 5 else latest
    ma20 = float(close.rolling(20).mean().iloc[-1]) if len(df) >= 20 else latest
    ma60 = float(close.rolling(60).mean().iloc[-1]) if len(df) >= 60 else latest

    change_1d = (latest / float(close.iloc[-2]) - 1) * 100 if len(df) >= 2 else 0
    change_5d = (latest / float(close.iloc[-6]) - 1) * 100 if len(df) >= 6 else 0
    change_20d = (latest / float(close.iloc[-21]) - 1) * 100 if len(df) >= 21 else 0

    above_ma5 = latest > ma5
    above_ma20 = latest > ma20
    above_ma60 = latest > ma60
    ma5_above_ma20 = ma5 > ma20
    ma20_above_ma60 = ma20 > ma60

    # トレンド判定
    if ma5_above_ma20 and ma20_above_ma60:
        trend = "up"
    elif not ma5_above_ma20 and not ma20_above_ma60:
        trend = "down"
    else:
        trend = "flat"

    return {
        "close": round(latest, 2),
        "change_1d_pct": round(change_1d, 2),
        "change_5d_pct": round(change_5d, 2),
        "change_20d_pct": round(change_20d, 2),
        "ma5": round(ma5, 2),
        "ma20": round(ma20, 2),
        "ma60": round(ma60, 2),
        "above_ma5": above_ma5,
        "above_ma20": above_ma20,
        "above_ma60": above_ma60,
        "ma5_above_ma20": ma5_above_ma20,
        "ma20_above_ma60": ma20_above_ma60,
        "trend": trend,
    }


def _analyze_indices(index_data: dict[str, pd.DataFrame]) -> dict:
    """全指数を分析"""
    result = {}
    for key, df in index_data.items():
        analysis = _analyze_single_index(df)
        if analysis:
            result[key] = analysis
    return result


# ─── 市場幅（AD比率） ───────────────────────────────────────────────────────

def _analyze_breadth() -> dict:
    """ウォッチリスト銘柄の騰落を集計"""
    symbols = get_active_symbols()
    advancing = 0
    declining = 0
    unchanged = 0

    for symbol in symbols:
        data = fetch_latest_2days(symbol)
        if data is None:
            continue
        change = data.get("change_pct", 0)
        if change > 0.1:
            advancing += 1
        elif change < -0.1:
            declining += 1
        else:
            unchanged += 1

    ad_ratio = advancing / max(declining, 1)

    if ad_ratio > 2.0:
        breadth_signal = "positive"
    elif ad_ratio < 0.5:
        breadth_signal = "negative"
    else:
        breadth_signal = "neutral"

    return {
        "advancing": advancing,
        "declining": declining,
        "unchanged": unchanged,
        "ad_ratio": round(ad_ratio, 2),
        "breadth_signal": breadth_signal,
    }


# ─── ボラティリティ ─────────────────────────────────────────────────────────

def _analyze_volatility(index_data: dict[str, pd.DataFrame]) -> dict:
    """N225のATR(14)でボラティリティを評価"""
    df = index_data.get("nikkei225")
    if df is None or len(df) < 15:
        return {
            "nikkei_atr14": 0,
            "atr_pct": 0,
            "daily_range_avg5": 0,
            "volatility_level": "normal",
        }

    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    close = df["Close"].astype(float)

    # True Range
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr14 = float(tr.rolling(14).mean().iloc[-1])
    latest_close = float(close.iloc[-1])
    atr_pct = atr14 / latest_close * 100

    # 5日平均デイリーレンジ
    daily_range = (high - low) / close
    daily_range_avg5 = float(daily_range.iloc[-5:].mean()) * 100

    # ボラティリティ分類
    if atr_pct > 3.5:
        level = "extreme"
    elif atr_pct > 2.0:
        level = "high"
    elif atr_pct > 1.0:
        level = "normal"
    else:
        level = "low"

    return {
        "nikkei_atr14": round(atr14, 2),
        "atr_pct": round(atr_pct, 3),
        "daily_range_avg5": round(daily_range_avg5, 3),
        "volatility_level": level,
    }


# ─── セクター分析 ────────────────────────────────────────────────────────────

def _analyze_sectors() -> dict:
    """セクター別の相対強度を算出"""
    # 全銘柄の5日変化率を取得（breadth分析で既に取得済みだが、セクター分類のため再取得）
    symbol_changes = {}
    for symbol_list in SECTOR_MAP.values():
        for symbol in symbol_list:
            if symbol not in symbol_changes:
                data = fetch_latest_2days(symbol)
                if data:
                    symbol_changes[symbol] = data.get("change_pct", 0)

    if not symbol_changes:
        return {"strong_sectors": [], "weak_sectors": [], "sector_details": {}}

    # 全体平均
    all_changes = list(symbol_changes.values())
    overall_avg = sum(all_changes) / len(all_changes) if all_changes else 0

    sector_details = {}
    for sector, symbols in SECTOR_MAP.items():
        changes = [symbol_changes[s] for s in symbols if s in symbol_changes]
        if not changes:
            continue
        sector_avg = sum(changes) / len(changes)
        relative_strength = sector_avg / overall_avg if overall_avg != 0 else 1.0
        sector_details[sector] = {
            "change_pct": round(sector_avg, 2),
            "relative_strength": round(relative_strength, 2),
        }

    strong = [s for s, d in sector_details.items() if d["relative_strength"] > 1.1]
    weak = [s for s, d in sector_details.items() if d["relative_strength"] < 0.9]

    return {
        "strong_sectors": strong,
        "weak_sectors": weak,
        "sector_details": sector_details,
    }


# ─── レジーム分類 ────────────────────────────────────────────────────────────

def _classify_regime(index_analysis: dict, breadth: dict,
                     volatility: dict) -> tuple[str, float, float]:
    """
    各指標のスコアを合算してレジームを分類する。
    Returns: (regime, confidence, regime_score)
    """
    score = 0.0

    # 指数分析スコア（各指数ごとに加算）
    for key in ["nikkei225", "topix"]:
        ia = index_analysis.get(key, {})
        if not ia:
            continue
        # 価格 vs MA20
        if ia.get("above_ma20"):
            score += 0.15
        else:
            score -= 0.15
        # MA5 vs MA20
        if ia.get("ma5_above_ma20"):
            score += 0.10
        else:
            score -= 0.10
        # MA20 vs MA60
        if ia.get("ma20_above_ma60"):
            score += 0.10
        else:
            score -= 0.10
        # 5日変化率
        change_5d = ia.get("change_5d_pct", 0)
        if change_5d > 1.0:
            score += 0.05
        elif change_5d < -1.0:
            score -= 0.05

    # 市場幅スコア
    ad_ratio = breadth.get("ad_ratio", 1.0)
    if ad_ratio > 2.0:
        score += 0.15
    elif ad_ratio > 1.2:
        score += 0.05
    elif ad_ratio < 0.5:
        score -= 0.15
    elif ad_ratio < 0.8:
        score -= 0.05

    # ボラティリティスコア
    vol_level = volatility.get("volatility_level", "normal")
    if vol_level == "extreme":
        score -= 0.10
    elif vol_level == "high":
        score -= 0.05

    # スコアを -1.0 ~ +1.0 にクリップ
    score = max(-1.0, min(1.0, score))

    # レジーム分類
    if vol_level == "extreme":
        regime = "UNCERTAIN"
        confidence = 0.5
    elif score > 0.30:
        regime = "BULLISH"
        confidence = min(abs(score) / 0.7, 1.0)
    elif score < -0.15:
        regime = "BEARISH"
        confidence = min(abs(score) / 0.7, 1.0)
    else:
        regime = "RANGE"
        confidence = 1.0 - abs(score) * 3

    confidence = max(0.1, min(1.0, confidence))

    return regime, confidence, score


# ─── トレーディングゲート ────────────────────────────────────────────────────

def _compute_trading_gate(regime: str, confidence: float,
                          volatility: dict) -> dict:
    """レジームに基づいてトレーディングゲートを決定"""
    if regime == "BULLISH" and confidence >= 0.6:
        return {
            "allow_trading": True,
            "position_size_multiplier": 1.2,
            "blocked_strategies": [],
            "reason": "強気相場（高信頼度）",
        }
    elif regime == "BULLISH":
        return {
            "allow_trading": True,
            "position_size_multiplier": 1.0,
            "blocked_strategies": [],
            "reason": "強気相場（低信頼度）",
        }
    elif regime == "RANGE":
        return {
            "allow_trading": True,
            "position_size_multiplier": 0.8,
            "blocked_strategies": ["breakout"],
            "reason": "レンジ相場 — ブレイクアウト戦略をブロック",
        }
    elif regime == "BEARISH" and confidence >= 0.6:
        return {
            "allow_trading": False,
            "position_size_multiplier": 0.0,
            "blocked_strategies": ["all"],
            "reason": "強い弱気相場 — 全取引停止",
        }
    elif regime == "BEARISH":
        return {
            "allow_trading": True,
            "position_size_multiplier": 0.5,
            "blocked_strategies": ["breakout", "ma_cross"],
            "reason": "弱気相場 — ブレイクアウト・MA戦略をブロック",
        }
    else:  # UNCERTAIN
        return {
            "allow_trading": True,
            "position_size_multiplier": 0.3,
            "blocked_strategies": ["breakout"],
            "reason": "不確実な市場環境 — ポジション縮小",
        }
