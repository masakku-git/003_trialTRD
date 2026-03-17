"""
差分取得ロジック
- DBに最新日付があれば翌日から差分のみ取得
- データなしなら lookback_days 分を一括取得
"""
import logging
from datetime import date, timedelta

import yfinance as yf

from tools.db import get_latest_date, upsert_prices

logger = logging.getLogger(__name__)


def fetch_prices_incremental(symbol: str, lookback_days: int = 90) -> bool:
    """
    差分のみ取得してDBに保存。
    Returns: True=取得実行, False=最新データあり(スキップ)
    """
    latest = get_latest_date(symbol)

    if latest is None:
        start = (date.today() - timedelta(days=lookback_days)).isoformat()
        logger.info(f"[fetcher] {symbol}: 新規 → {lookback_days}日分取得 (start={start})")
    else:
        last_date = date.fromisoformat(latest)
        # 前営業日のデータまであれば取得不要（週末・祝日考慮: 1日ではなく3日のバッファ）
        if last_date >= date.today() - timedelta(days=3):
            logger.debug(f"[fetcher] {symbol}: 最新データあり ({latest}) → スキップ")
            return False
        start = (last_date + timedelta(days=1)).isoformat()
        logger.info(f"[fetcher] {symbol}: 差分取得 start={start}")

    try:
        df = yf.download(symbol, start=start, progress=False, auto_adjust=True)
        if df.empty:
            logger.warning(f"[fetcher] {symbol}: データなし (start={start})")
            return False
        upsert_prices(symbol, df)
        logger.info(f"[fetcher] {symbol}: {len(df)}件 保存完了")
        return True
    except Exception as e:
        logger.error(f"[fetcher] {symbol}: 取得失敗 - {e}")
        return False


def fetch_latest_2days(symbol: str) -> dict | None:
    """
    MarketScanner用の軽量取得（直近2日のみ）
    前日比・出来高を返す。DBには保存しない。
    """
    try:
        df = yf.download(symbol, period="5d", progress=False, auto_adjust=True)
        if isinstance(df.columns, __import__("pandas").MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if len(df) < 2:
            return None
        prev = df.iloc[-2]
        curr = df.iloc[-1]
        change_pct = (float(curr["Close"]) - float(prev["Close"])) / float(prev["Close"]) * 100
        vol_ratio = float(curr["Volume"]) / (float(prev["Volume"]) + 1)
        return {
            "symbol": symbol,
            "close": float(curr["Close"]),
            "prev_close": float(prev["Close"]),
            "change_pct": round(change_pct, 2),
            "volume": int(curr["Volume"]),
            "vol_ratio": round(vol_ratio, 2),
            "date": df.index[-1].strftime("%Y-%m-%d"),
        }
    except Exception as e:
        logger.error(f"[fetcher] {symbol}: 軽量取得失敗 - {e}")
        return None
