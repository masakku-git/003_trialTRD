# Plan: Claude マルチエージェント自律売買システム（DB差分取得設計）

## Context
Hetzner (Ubuntu) + moomoo証券(Futu OpenD) + Claude APIによる自律売買システム。
**毎回全データを取得せず、DBに差分のみ蓄積**し、スクリーニング済み候補のデータだけを
必要なタイミングで取得・補完する設計にする。

---

## データ取得の基本方針

```
毎日のcron実行時:
  1. MarketScanner が軽量スクリーニング（出来高・変化率）を実行
     → 候補に挙がった銘柄のみを後続処理に渡す

  2. 候補銘柄ごとに DB を確認
     ├─ 過去データあり → 最新日付以降の差分のみ取得
     └─ 過去データなし（新規候補）→ 90日分の履歴を一括取得し DB に保存

  3. バックテスト・テクニカル分析は DB のデータを使う（API再取得しない）

  4. 実行結果（注文・ポジション・残高）を DB に保存
```

---

## データベース設計（SQLite）

SQLite を採用（シングルサーバで十分・運用シンプル）

```
~/bot/db/trading.db
```

### テーブル一覧

```sql
-- ウォッチリスト（スキャン対象銘柄マスタ）
CREATE TABLE watchlist (
    symbol      TEXT PRIMARY KEY,
    name        TEXT,
    market      TEXT,           -- 'TSE'など
    active      INTEGER DEFAULT 1,
    added_at    TEXT
);

-- 株価履歴（差分蓄積）
CREATE TABLE daily_prices (
    symbol      TEXT,
    date        TEXT,           -- YYYY-MM-DD
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,
    volume      INTEGER,
    PRIMARY KEY (symbol, date)
);

-- スクリーニング結果（毎日保存）
CREATE TABLE screening_results (
    date        TEXT,
    symbol      TEXT,
    score       REAL,
    reason      TEXT,
    PRIMARY KEY (date, symbol)
);

-- バックテスト結果（銘柄×シグナルごとにキャッシュ）
CREATE TABLE backtest_cache (
    symbol      TEXT,
    signal_type TEXT,
    computed_at TEXT,
    win_rate    REAL,
    avg_rr      REAL,
    max_dd      REAL,
    sample_cnt  INTEGER,
    PRIMARY KEY (symbol, signal_type)
);

-- 注文履歴
CREATE TABLE orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT,
    symbol      TEXT,
    action      TEXT,           -- buy/sell
    quantity    INTEGER,
    price       REAL,
    stop_loss   REAL,
    take_profit REAL,
    status      TEXT,           -- pending/executed/cancelled
    reason      TEXT
);

-- ポジション（現在保有）
CREATE TABLE positions (
    symbol      TEXT PRIMARY KEY,
    quantity    INTEGER,
    avg_cost    REAL,
    stop_loss   REAL,
    take_profit REAL,
    opened_at   TEXT
);

-- 資金スナップショット（日次）
CREATE TABLE portfolio_snapshots (
    date        TEXT PRIMARY KEY,
    cash        REAL,
    total_value REAL,
    positions_json TEXT
);
```

---

## マルチエージェント全体フロー（DB対応版）

```
cron (毎営業日 08:45 JST)
        │
        ▼
┌─────────────────────────────────────────────────────┐
│  Orchestrator Agent (claude-opus-4-6)                │
└────┬──────────┬──────────┬──────────┬───────────────┘
     │          │          │          │
     ▼          ▼          ▼          ▼
MarketScanner  TechnicalAnalyst  RiskManager  BacktestValidator
(Haiku)        (Haiku)           (Sonnet)     (Haiku)
     │
     ├─ 全銘柄スキャン（軽量：前日比・出来高のみ）
     ├─ 候補3〜5銘柄を選定
     └─ DB確認 → 差分データのみ取得・保存

                     ↓ 候補銘柄 + DBのデータを使って分析
                     ↓ （APIの再取得なし）

                                                 ▼
                                          Trade Executor
                                          futu-api → moomoo
```

---

## ファイル構成

```
~/bot/
  ├─ .env
  ├─ main.py                     # エントリポイント
  ├─ agents/
  │   ├─ orchestrator.py
  │   ├─ market_scanner.py
  │   ├─ technical_analyst.py
  │   ├─ risk_manager.py
  │   └─ backtest_validator.py
  ├─ tools/
  │   ├─ db.py                   # DB操作（SQLiteラッパー）
  │   ├─ data_fetcher.py         # 差分取得ロジック（yfinance/Futu API）
  │   ├─ futu_client.py          # moomoo注文実行
  │   └─ executor.py
  ├─ db/
  │   └─ trading.db              # SQLiteファイル
  ├─ data/
  │   └─ watchlist.json          # スキャン対象銘柄リスト
  └─ logs/
```

---

## 差分取得ロジック（data_fetcher.py）

```python
import yfinance as yf
from tools.db import get_latest_date, upsert_prices
from datetime import date, timedelta

def fetch_prices_incremental(symbol: str, lookback_days: int = 90):
    """
    DB に存在する最新日付から今日までの差分のみ取得。
    データが全くない場合は lookback_days 分を一括取得。
    """
    latest = get_latest_date(symbol)  # DBの最新日付を返す or None

    if latest is None:
        # 新規候補 → 履歴を一括取得
        start = (date.today() - timedelta(days=lookback_days)).isoformat()
    else:
        # 差分のみ取得（最新日の翌日から）
        last_date = date.fromisoformat(latest)
        if last_date >= date.today() - timedelta(days=1):
            return  # 最新データあり → 取得不要
        start = (last_date + timedelta(days=1)).isoformat()

    df = yf.download(symbol, start=start, progress=False)
    if not df.empty:
        upsert_prices(symbol, df)  # DB に upsert（重複しない）
```

---

## MarketScanner の軽量スクリーニング戦略

```python
# 全ウォッチリスト銘柄に対して yfinance の "2d" (直近2日) だけ取得
# → 前日比・出来高急増のみチェックし、候補銘柄を絞る
# → 候補に選ばれた銘柄だけ fetch_prices_incremental を呼ぶ
```

---

## バックテストのキャッシュ戦略

```python
# backtest_cache テーブルを確認
# ├─ 当日計算済み → DBのキャッシュを返す（API・計算不要）
# └─ キャッシュなし or 7日以上古い → 再計算してDBに保存
```

---

## Hetznerサーバ構築手順（要約）

1. CX22 + Ubuntu 24.04 でサーバ作成
2. 初期設定: SSHユーザ・ufw・rootログイン無効化・JST設定
3. Futu OpenD インストール + systemdサービス化
4. Python環境: `venv` + `pip install anthropic futu-api yfinance pandas ta python-dotenv`
5. GitHubからコードデプロイ
6. `python -c "from tools.db import init_db; init_db()"` でDB初期化
7. cron設定

```cron
45 8 * * 1-5 /home/trading/bot/venv/bin/python /home/trading/bot/main.py >> /home/trading/bot/logs/trading_$(date +\%Y\%m\%d).log 2>&1
```

---

## モデル選定とコスト試算

| エージェント | モデル | 役割 |
|------------|-------|------|
| Orchestrator | claude-opus-4-6 | 統括・最終判断 |
| RiskManager | claude-sonnet-4-6 | リスク評価 |
| MarketScanner / TechnicalAnalyst / BacktestValidator | claude-haiku-4-5 | 軽量処理 |

1日1回実行 → 推定 **$0.05〜$0.15/日**

---

## Futu OpenD 認証注意事項

- 初回起動時にmoomoo二段階認証が必要
- Linux ヘッドレス環境での恒久運用可否を **事前にmoomooサポートへ確認**

---

## テスト・検証方法

1. `python main.py` 手動実行 → DBにデータが正しく蓄積されるか確認
2. 2回目実行で差分のみ取得されているか（APIコール回数をログで確認）
3. バックテストキャッシュが正しく再利用されるか確認
4. **本番前に2週間以上のペーパートレード**で動作確認
