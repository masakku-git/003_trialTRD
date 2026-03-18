"""
戦略モジュール（Strategy Pattern）
各戦略は BaseStrategy を継承し、共通インターフェースを実装する。
"""
from strategies.base import BaseStrategy, StrategyRegistry

__all__ = ["BaseStrategy", "StrategyRegistry"]
