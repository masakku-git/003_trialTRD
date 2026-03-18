"""
エントリポイント
cron または手動実行で呼び出される
"""
import argparse
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

# .env 読み込み（ファイルがない場合はスキップ）
load_dotenv()

# ログ設定
LOG_DIR = os.getenv("LOG_DIR", str(Path(__file__).parent / "logs"))
os.makedirs(LOG_DIR, exist_ok=True)

log_file = os.path.join(LOG_DIR, f"trading_{date.today().strftime('%Y%m%d')}.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="自律売買システム（ルールベース）")
    parser.add_argument("--dry-run", action="store_true",
                        help="注文を発行しない（ペーパートレードモード）")
    parser.add_argument("--init-db", action="store_true",
                        help="DBを初期化してウォッチリストを同期する")
    parser.add_argument("--scan-only", action="store_true",
                        help="スキャンのみ実行（注文なし）")
    parser.add_argument("--research-only", action="store_true",
                        help="マクロ市場分析のみ実行（MarketResearcher単体テスト）")
    args = parser.parse_args()

    # DB初期化
    from tools.db import init_db, load_watchlist_to_db

    if args.init_db:
        logger.info("DB初期化中...")
        init_db()
        watchlist_path = os.getenv(
            "WATCHLIST_PATH",
            str(Path(__file__).parent / "data" / "watchlist.json")
        )
        load_watchlist_to_db(watchlist_path)
        logger.info("DB初期化・ウォッチリスト同期完了")
        return

    # 通常は起動時に自動でDB初期化（テーブルがなければ作成）
    init_db()
    watchlist_path = os.getenv(
        "WATCHLIST_PATH",
        str(Path(__file__).parent / "data" / "watchlist.json")
    )
    load_watchlist_to_db(watchlist_path)

    if args.research_only:
        from agents.market_researcher import run_market_researcher
        context = run_market_researcher()
        print(json.dumps(context, ensure_ascii=False, indent=2, default=str))
        return

    if args.scan_only:
        from agents.market_scanner import run_market_scanner
        candidates = run_market_scanner()
        print(json.dumps(candidates, ensure_ascii=False, indent=2))
        return

    # メイン実行
    from agents.orchestrator import run_orchestrator

    dry_run = args.dry_run
    if dry_run:
        logger.info("=" * 60)
        logger.info("DRY RUN モード（注文は発行されません）")
        logger.info("=" * 60)

    try:
        result = run_orchestrator(dry_run=dry_run)
        logger.info(f"実行結果: {json.dumps(result, ensure_ascii=False, default=str)}")

        # 終了コード: 注文実行あり=0, なし=0（エラーのみ1）
        sys.exit(0)
    except KeyboardInterrupt:
        logger.info("手動中断")
        sys.exit(0)
    except Exception as e:
        logger.exception(f"予期しないエラー: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
