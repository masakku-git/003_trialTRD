# Plan: ルールベース自律売買システム（DB差分取得設計）

## Context
Hetzner (Ubuntu) + moomoo証券(Futu OpenD) によるルールベース自律売買システム。
**APIキー不要**（Claude Code Proで戦略開発、日次運用はアルゴリズムで自動実行）。
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
-- 戦略マスタ（トレード戦略の名称・説明）
CREATE TABLE strategies (
    strategy_name   TEXT PRIMARY KEY,   -- 'ma_cross' / 'rsi_oversold' / 'breakout' など
    description     TEXT,
    created_at      TEXT
);

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
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT,
    symbol          TEXT,
    action          TEXT,           -- buy/sell
    quantity        INTEGER,
    price           REAL,
    stop_loss       REAL,
    take_profit     REAL,
    status          TEXT,           -- pending/executed/cancelled
    reason          TEXT,
    strategy_name   TEXT            -- 使用戦略名（strategies.strategy_name参照）
);

-- ポジション（現在保有）
CREATE TABLE positions (
    symbol          TEXT PRIMARY KEY,
    quantity        INTEGER,
    avg_cost        REAL,
    stop_loss       REAL,
    take_profit     REAL,
    opened_at       TEXT,
    strategy_name   TEXT            -- 建値時の戦略名
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

## 全体フロー（ルールベース・DB対応版）

```
cron (毎営業日 08:45 JST)
        │
        ▼
┌──────────────────────────────────────────────────────────┐
│  Orchestrator（ルールベース統括）                          │
└────┬──────────────────────────────────────────────────────┘
     │
     ▼ Step 1
MarketScanner（ルールベース）
  ├─ 全銘柄スキャン（変化率・出来高でスコアリング）
  ├─ 上位3〜5銘柄を選定
  └─ DB確認 → 差分データのみ取得・保存
     │
     ▼ Step 2
TechnicalAnalyst（戦略パターン）
  └─ 全戦略のgenerate_signal() → 最良シグナル選定（DBのみ使用）
     │
     ▼ Step 3
BacktestValidator（ルールベース）
  └─ 勝率≥40% / RR≥0.3 / サンプル≥5（DBキャッシュ7日間優先）
     │
     ▼ Step 3.5
StrategyCritic（ヒューリスティック）← 悪魔の代弁者
  ├─ approve → そのまま通過
  ├─ caution → 信頼度を0.7倍に減衰して通過
  └─ reject  → 除外（重大な欠陥ありと判定）
     │
     ▼ Step 4
RiskManager（ルールベース）
  └─ RR≥1.5 / ポジションサイズ計算 / スロット・キャッシュチェック
     │
     ▼ Step 5
Orchestrator 最終判断（ルールベース）
  └─ criticality_scoreで優先順位付け
     │
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
  │   ├─ technical_analyst.py     # 全戦略を試して最良シグナルを選定
  │   ├─ backtest_validator.py    # 戦略ファイルのbacktest()を使用
  │   ├─ strategy_critic.py
  │   └─ risk_manager.py
  ├─ strategies/                  # ★ 戦略パターン（Strategy Pattern）
  │   ├─ __init__.py              # BaseStrategy / StrategyRegistry 公開
  │   ├─ base.py                  # 基底クラス・共通ヘルパー・自動レジストリ
  │   ├─ ma_cross.py              # 移動平均クロス戦略
  │   ├─ rsi_oversold.py          # RSI売られすぎ戦略
  │   ├─ breakout.py              # ブレイクアウト戦略
  │   └─ (新戦略.py)              # ← ファイル追加のみで新戦略を導入可能
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
4. Python環境: `venv` + `pip install futu-api yfinance pandas ta python-dotenv`
5. GitHubからコードデプロイ
6. `python -c "from tools.db import init_db; init_db()"` でDB初期化
7. cron設定

```cron
45 8 * * 1-5 /home/trading/bot/venv/bin/python /home/trading/bot/main.py >> /home/trading/bot/logs/trading_$(date +\%Y\%m\%d).log 2>&1
```

---

## 実行方式

**ルールベース（APIキー不要）** — Claude Code Proで戦略を開発・改善し、日次運用はアルゴリズムで自動実行。

| モジュール | 方式 | 役割 |
|------------|------|------|
| Orchestrator | ルールベース | 統括・最終判断（criticality_scoreでソート） |
| StrategyCritic | ヒューリスティック | 戦略の弱点検出（統計・指標矛盾チェック） |
| RiskManager | ルールベース | ポジションサイズ計算・RR比/勝率フィルタ |
| MarketScanner | ルールベース | 変化率・出来高でスコアリング |
| TechnicalAnalyst | 戦略パターン | 全戦略のgenerate_signal()から最良選定 |
| BacktestValidator | ルールベース | 勝率/RR/サンプル数で通過判定 |

**ランニングコスト: API利用料なし**（yfinanceの市場データ取得のみ）

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


  実装済みファイル一覧

  ┌──────────────────────────────┬──────────────────────────────────────────────────────────────────────────┐
  │           ファイル           │                                   役割                                   │
  ├──────────────────────────────┼──────────────────────────────────────────────────────────────────────────┤
  │ main.py                      │ エントリポイント。--dry-run / --init-db / --scan-only オプション対応     │
  ├──────────────────────────────┼──────────────────────────────────────────────────────────────────────────┤
  │ agents/orchestrator.py       │ Opus-4.6でStep1〜5を統括・最終判断                                       │
  ├──────────────────────────────┼──────────────────────────────────────────────────────────────────────────┤
  │ agents/market_scanner.py     │ Haiku-4.5で軽量スクリーニング + 差分データ取得のトリガー                 │
  ├──────────────────────────────┼──────────────────────────────────────────────────────────────────────────┤
  │ agents/technical_analyst.py  │ 全戦略のgenerate_signal()を試し最良シグナルを選定（DBのみ使用）          │
  ├──────────────────────────────┼──────────────────────────────────────────────────────────────────────────┤
  │ agents/backtest_validator.py │ 戦略ファイルのbacktest()を使用（DBキャッシュ7日間優先）                  │
  ├──────────────────────────────┼──────────────────────────────────────────────────────────────────────────┤
  │ agents/strategy_critic.py    │ Sonnet-4.6で戦略の弱点を批判的審査（悪魔の代弁者）                      │
  ├──────────────────────────────┼──────────────────────────────────────────────────────────────────────────┤
  │ agents/risk_manager.py       │ Sonnet-4.6でポジションサイズ決定・リスク評価                             │
  ├──────────────────────────────┼──────────────────────────────────────────────────────────────────────────┤
  │ strategies/base.py           │ BaseStrategy基底クラス・StrategyRegistry（自動検出・登録）               │
  ├──────────────────────────────┼──────────────────────────────────────────────────────────────────────────┤
  │ strategies/ma_cross.py       │ 移動平均クロス戦略（MA5/MA20ゴールデンクロス）                           │
  ├──────────────────────────────┼──────────────────────────────────────────────────────────────────────────┤
  │ strategies/rsi_oversold.py   │ RSI売られすぎ戦略（RSI30割れからの反発）                                 │
  ├──────────────────────────────┼──────────────────────────────────────────────────────────────────────────┤
  │ strategies/breakout.py       │ ブレイクアウト戦略（20日高値更新突破）                                   │
  ├──────────────────────────────┼──────────────────────────────────────────────────────────────────────────┤
  │ tools/db.py                  │ SQLiteラッパー（全テーブルのCRUD・upsert）                               │
  ├──────────────────────────────┼──────────────────────────────────────────────────────────────────────────┤
  │ tools/data_fetcher.py        │ 差分取得ロジック（新規→90日、既存→翌日以降のみ）                         │
  ├──────────────────────────────┼──────────────────────────────────────────────────────────────────────────┤
  │ tools/futu_client.py         │ moomoo注文・残高・ポジション取得（futu-api未インストール時はモック動作） │
  ├──────────────────────────────┼──────────────────────────────────────────────────────────────────────────┤
  │ tools/executor.py            │ 注文実行・DB記録・スナップショット保存                                   │
  ├──────────────────────────────┼──────────────────────────────────────────────────────────────────────────┤
  │ data/watchlist.json          │ 東証20銘柄のサンプルウォッチリスト                                       │
  ├──────────────────────────────┼──────────────────────────────────────────────────────────────────────────┤
  │ requirements.txt             │ 依存パッケージ一覧                                                       │
  ├──────────────────────────────┼──────────────────────────────────────────────────────────────────────────┤
  │ setup_server.sh              │ Hetzner Ubuntu初期セットアップスクリプト                                 │
  ├──────────────────────────────┼──────────────────────────────────────────────────────────────────────────┤
  │ futu_openD.service           │ systemdサービスファイル                                                  │
  └──────────────────────────────┴──────────────────────────────────────────────────────────────────────────┘

  ---
  初回セットアップ手順

  # 1. 依存インストール
  pip install -r requirements.txt

  # 2. .envを作成
  cp .env.example .env
  # → FUTU_TRADE_PWD, FUTU_ACCOUNT_ID等を編集

  # 3. DB初期化
  python main.py --init-db

  # 4. ペーパートレードでテスト
  python main.py --dry-run

  # 5. スキャンのみ確認
  python main.py --scan-only