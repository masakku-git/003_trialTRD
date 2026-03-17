"""
Trade Executor: 注文決定をmoomooへ送信し、DBに記録する
"""
import logging
from datetime import date

from tools.db import save_order, update_order_status, upsert_position, save_snapshot
from tools.futu_client import FutuClient

logger = logging.getLogger(__name__)


class TradeExecutor:
    def __init__(self, futu: FutuClient):
        self.futu = futu

    def execute_orders(self, orders: list[dict]) -> list[dict]:
        """
        orders: [{"symbol", "action", "quantity", "price", "stop_loss", "take_profit", "reason"}]
        Returns: 実行結果リスト
        """
        results = []
        today = date.today().isoformat()

        for order in orders:
            symbol = order["symbol"]
            action = order["action"]
            quantity = order["quantity"]
            price = order["price"]
            stop_loss = order.get("stop_loss", 0.0)
            take_profit = order.get("take_profit", 0.0)
            reason = order.get("reason", "")
            strategy_name = order.get("strategy_name", "")

            # DBに pending 注文を記録
            order_id = save_order(
                date_str=today,
                symbol=symbol,
                action=action,
                quantity=quantity,
                price=price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                status="pending",
                reason=reason,
                strategy_name=strategy_name,
            )

            # moomooへ発注
            result = self.futu.place_order(symbol, action, quantity, price)

            if result.get("success"):
                update_order_status(order_id, "executed")
                logger.info(f"[executor] 注文実行 {symbol} {action} {quantity}株 @{price}")
                results.append({**order, "order_id": order_id, "status": "executed"})
            else:
                update_order_status(order_id, "failed")
                logger.error(f"[executor] 注文失敗 {symbol}: {result.get('error')}")
                results.append({**order, "order_id": order_id, "status": "failed",
                                 "error": result.get("error")})

        return results

    def sync_positions_to_db(self):
        """Futuのポジションをガバナンス的にDBと同期"""
        positions = self.futu.get_positions()
        for pos in positions:
            upsert_position(
                symbol=pos.get("code", ""),
                quantity=int(pos.get("qty", 0)),
                avg_cost=float(pos.get("cost_price", 0)),
                stop_loss=0.0,
                take_profit=0.0,
            )
        logger.info(f"[executor] ポジション同期完了: {len(positions)}件")

    def save_portfolio_snapshot(self):
        """残高スナップショットをDBに保存"""
        info = self.futu.get_account_info()
        positions = self.futu.get_positions()
        save_snapshot(
            date_str=date.today().isoformat(),
            cash=info.get("cash", 0.0),
            total_value=info.get("total_assets", 0.0),
            positions=positions,
        )
        logger.info(f"[executor] スナップショット保存 cash={info.get('cash')}")
