"""
DB操作モジュール（SQLiteラッパー）
差分取得・upsert・キャッシュ管理を担当
"""
import sqlite3
import json
import os
from datetime import date
from typing import Optional
import pandas as pd

DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "../db/trading.db"))


def get_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """全テーブルを作成（初回のみ）"""
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS watchlist (
            symbol      TEXT PRIMARY KEY,
            name        TEXT,
            market      TEXT,
            active      INTEGER DEFAULT 1,
            added_at    TEXT
        );

        CREATE TABLE IF NOT EXISTS daily_prices (
            symbol      TEXT,
            date        TEXT,
            open        REAL,
            high        REAL,
            low         REAL,
            close       REAL,
            volume      INTEGER,
            PRIMARY KEY (symbol, date)
        );

        CREATE TABLE IF NOT EXISTS screening_results (
            date        TEXT,
            symbol      TEXT,
            score       REAL,
            reason      TEXT,
            PRIMARY KEY (date, symbol)
        );

        CREATE TABLE IF NOT EXISTS backtest_cache (
            symbol      TEXT,
            signal_type TEXT,
            computed_at TEXT,
            win_rate    REAL,
            avg_rr      REAL,
            max_dd      REAL,
            sample_cnt  INTEGER,
            PRIMARY KEY (symbol, signal_type)
        );

        CREATE TABLE IF NOT EXISTS orders (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT,
            symbol      TEXT,
            action      TEXT,
            quantity    INTEGER,
            price       REAL,
            stop_loss   REAL,
            take_profit REAL,
            status      TEXT,
            reason      TEXT
        );

        CREATE TABLE IF NOT EXISTS positions (
            symbol      TEXT PRIMARY KEY,
            quantity    INTEGER,
            avg_cost    REAL,
            stop_loss   REAL,
            take_profit REAL,
            opened_at   TEXT
        );

        CREATE TABLE IF NOT EXISTS portfolio_snapshots (
            date            TEXT PRIMARY KEY,
            cash            REAL,
            total_value     REAL,
            positions_json  TEXT
        );
    """)
    conn.commit()
    conn.close()
    print(f"[db] initialized: {DB_PATH}")


# ─── watchlist ───────────────────────────────────────────────────────────────

def load_watchlist_to_db(watchlist_path: str):
    """watchlist.json の内容を watchlist テーブルへ同期"""
    with open(watchlist_path) as f:
        data = json.load(f)
    conn = get_conn()
    today = date.today().isoformat()
    for item in data["symbols"]:
        conn.execute(
            "INSERT OR IGNORE INTO watchlist (symbol, name, market, active, added_at) VALUES (?,?,?,1,?)",
            (item["symbol"], item["name"], item["market"], today)
        )
    conn.commit()
    conn.close()


def get_active_symbols() -> list[str]:
    conn = get_conn()
    rows = conn.execute("SELECT symbol FROM watchlist WHERE active=1").fetchall()
    conn.close()
    return [r["symbol"] for r in rows]


# ─── daily_prices ─────────────────────────────────────────────────────────────

def get_latest_date(symbol: str) -> Optional[str]:
    """DBに存在する最新日付を返す。データなしの場合 None"""
    conn = get_conn()
    row = conn.execute(
        "SELECT MAX(date) as max_date FROM daily_prices WHERE symbol=?", (symbol,)
    ).fetchone()
    conn.close()
    return row["max_date"] if row else None


def upsert_prices(symbol: str, df: pd.DataFrame):
    """DataFrameをdaily_pricesにupsert（重複無視）"""
    conn = get_conn()
    df = df.copy()
    df.index = pd.to_datetime(df.index)

    # MultiIndex対応（yfinanceがMultiIndex返す場合）
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    for idx, row in df.iterrows():
        conn.execute(
            """INSERT OR REPLACE INTO daily_prices
               (symbol, date, open, high, low, close, volume)
               VALUES (?,?,?,?,?,?,?)""",
            (
                symbol,
                idx.strftime("%Y-%m-%d"),
                float(row.get("Open", 0)),
                float(row.get("High", 0)),
                float(row.get("Low", 0)),
                float(row.get("Close", 0)),
                int(row.get("Volume", 0)),
            )
        )
    conn.commit()
    conn.close()


def get_prices(symbol: str, start: Optional[str] = None, end: Optional[str] = None) -> pd.DataFrame:
    """DBから価格データをDataFrameで取得"""
    conn = get_conn()
    query = "SELECT date, open, high, low, close, volume FROM daily_prices WHERE symbol=?"
    params: list = [symbol]
    if start:
        query += " AND date >= ?"
        params.append(start)
    if end:
        query += " AND date <= ?"
        params.append(end)
    query += " ORDER BY date ASC"
    df = pd.read_sql_query(query, conn, params=params, index_col="date", parse_dates=["date"])
    conn.close()
    df.columns = [c.capitalize() for c in df.columns]
    return df


# ─── screening_results ────────────────────────────────────────────────────────

def save_screening_result(date_str: str, symbol: str, score: float, reason: str):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO screening_results (date, symbol, score, reason) VALUES (?,?,?,?)",
        (date_str, symbol, score, reason)
    )
    conn.commit()
    conn.close()


def get_screening_results(date_str: str) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT symbol, score, reason FROM screening_results WHERE date=? ORDER BY score DESC",
        (date_str,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─── backtest_cache ───────────────────────────────────────────────────────────

def get_backtest_cache(symbol: str, signal_type: str, max_age_days: int = 7) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM backtest_cache WHERE symbol=? AND signal_type=?",
        (symbol, signal_type)
    ).fetchone()
    conn.close()
    if row is None:
        return None
    computed = date.fromisoformat(row["computed_at"])
    age = (date.today() - computed).days
    if age > max_age_days:
        return None
    return dict(row)


def save_backtest_cache(symbol: str, signal_type: str,
                        win_rate: float, avg_rr: float, max_dd: float, sample_cnt: int):
    conn = get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO backtest_cache
           (symbol, signal_type, computed_at, win_rate, avg_rr, max_dd, sample_cnt)
           VALUES (?,?,?,?,?,?,?)""",
        (symbol, signal_type, date.today().isoformat(), win_rate, avg_rr, max_dd, sample_cnt)
    )
    conn.commit()
    conn.close()


# ─── orders ───────────────────────────────────────────────────────────────────

def save_order(date_str: str, symbol: str, action: str, quantity: int,
               price: float, stop_loss: float, take_profit: float,
               status: str, reason: str) -> int:
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO orders
           (date, symbol, action, quantity, price, stop_loss, take_profit, status, reason)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (date_str, symbol, action, quantity, price, stop_loss, take_profit, status, reason)
    )
    order_id = cur.lastrowid
    conn.commit()
    conn.close()
    return order_id


def update_order_status(order_id: int, status: str):
    conn = get_conn()
    conn.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
    conn.commit()
    conn.close()


def get_orders(status: Optional[str] = None) -> list[dict]:
    conn = get_conn()
    if status:
        rows = conn.execute("SELECT * FROM orders WHERE status=? ORDER BY date DESC", (status,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM orders ORDER BY date DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─── positions ────────────────────────────────────────────────────────────────

def upsert_position(symbol: str, quantity: int, avg_cost: float,
                    stop_loss: float, take_profit: float):
    conn = get_conn()
    if quantity == 0:
        conn.execute("DELETE FROM positions WHERE symbol=?", (symbol,))
    else:
        conn.execute(
            """INSERT OR REPLACE INTO positions
               (symbol, quantity, avg_cost, stop_loss, take_profit, opened_at)
               VALUES (?,?,?,?,?,COALESCE(
                   (SELECT opened_at FROM positions WHERE symbol=?), ?
               ))""",
            (symbol, quantity, avg_cost, stop_loss, take_profit,
             symbol, date.today().isoformat())
        )
    conn.commit()
    conn.close()


def get_positions() -> list[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM positions").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─── portfolio_snapshots ──────────────────────────────────────────────────────

def save_snapshot(date_str: str, cash: float, total_value: float, positions: list[dict]):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO portfolio_snapshots (date, cash, total_value, positions_json) VALUES (?,?,?,?)",
        (date_str, cash, total_value, json.dumps(positions, ensure_ascii=False))
    )
    conn.commit()
    conn.close()


def get_latest_snapshot() -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM portfolio_snapshots ORDER BY date DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if row is None:
        return None
    d = dict(row)
    d["positions"] = json.loads(d["positions_json"])
    return d
