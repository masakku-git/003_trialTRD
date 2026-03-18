"""
戦略の基底クラスとレジストリ
新しい戦略を追加するには BaseStrategy を継承したクラスを strategies/ に作成するだけでよい。
"""
from abc import ABC, abstractmethod
from importlib import import_module
from pathlib import Path
from typing import Optional

import pandas as pd


class BaseStrategy(ABC):
    """全戦略が実装すべき共通インターフェース"""

    # サブクラスで上書き
    name: str = ""
    description: str = ""

    # デフォルトパラメータ（サブクラスでオーバーライド可）
    stop_loss_pct: float = 0.03   # 3%
    take_profit_pct: float = 0.06  # 6%
    hold_days: int = 5             # バックテスト保持日数

    def __init_subclass__(cls, **kwargs):
        """サブクラス定義時に自動でレジストリに登録"""
        super().__init_subclass__(**kwargs)
        if cls.name:
            StrategyRegistry.register(cls)

    @abstractmethod
    def generate_signal(self, df: pd.DataFrame) -> Optional[dict]:
        """
        OHLCVデータからシグナルを生成する。

        Args:
            df: 日足OHLCVのDataFrame（カラム: Open, High, Low, Close, Volume）

        Returns:
            シグナルがある場合:
                {
                    "signal": "buy" | "sell",
                    "confidence": 0.0〜1.0,
                    "entry_price": float,
                    "stop_loss": float,
                    "take_profit": float,
                    "reasoning": str,
                }
            シグナルなしの場合: None
        """
        ...

    @abstractmethod
    def backtest(self, df: pd.DataFrame) -> dict:
        """
        過去データでバックテストを実行する。

        Args:
            df: 日足OHLCVのDataFrame（最低60日分推奨）

        Returns:
            {
                "win_rate": float,
                "avg_rr": float,
                "max_dd": float,
                "sample_cnt": int,
            }
        """
        ...

    def _calc_max_dd(self, trades: list[dict], risk_pct: float = 0.02) -> float:
        """トレード結果リストから最大ドローダウンを計算する共通ヘルパー"""
        if not trades:
            return 0.0
        equity = [1.0]
        for t in trades:
            equity.append(equity[-1] * (1 + t["rr"] * risk_pct))
        peak = equity[0]
        max_dd = 0.0
        for v in equity:
            if v > peak:
                peak = v
            dd = (peak - v) / peak
            max_dd = max(max_dd, dd)
        return round(max_dd, 3)

    def _summarize_trades(self, trades: list[dict]) -> dict:
        """トレード結果リストから統計サマリを返す共通ヘルパー"""
        if not trades:
            return {"win_rate": 0, "avg_rr": 0, "max_dd": 0, "sample_cnt": 0}
        win_rate = sum(1 for t in trades if t["win"]) / len(trades)
        avg_rr = sum(t["rr"] for t in trades) / len(trades)
        max_dd = self._calc_max_dd(trades)
        return {
            "win_rate": round(win_rate, 3),
            "avg_rr": round(avg_rr, 3),
            "max_dd": max_dd,
            "sample_cnt": len(trades),
        }


class StrategyRegistry:
    """戦略の自動検出・管理"""

    _strategies: dict[str, type["BaseStrategy"]] = {}

    @classmethod
    def register(cls, strategy_cls: type["BaseStrategy"]):
        cls._strategies[strategy_cls.name] = strategy_cls

    @classmethod
    def get(cls, name: str) -> Optional["BaseStrategy"]:
        """名前から戦略インスタンスを取得"""
        strategy_cls = cls._strategies.get(name)
        if strategy_cls is None:
            return None
        return strategy_cls()

    @classmethod
    def get_all(cls) -> dict[str, "BaseStrategy"]:
        """全登録済み戦略のインスタンスを返す"""
        return {name: strategy_cls() for name, strategy_cls in cls._strategies.items()}

    @classmethod
    def list_names(cls) -> list[str]:
        return list(cls._strategies.keys())

    @classmethod
    def discover(cls):
        """strategies/ ディレクトリ内の全モジュールを自動インポートして登録"""
        strategies_dir = Path(__file__).parent
        for py_file in strategies_dir.glob("*.py"):
            if py_file.name.startswith("_"):
                continue
            module_name = f"strategies.{py_file.stem}"
            try:
                import_module(module_name)
            except Exception:
                pass
