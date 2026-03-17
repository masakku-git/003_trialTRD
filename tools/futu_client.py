"""
moomoo(Futu OpenD) クライアント
注文・ポジション・残高の取得・発注を担当
"""
import logging
import os

logger = logging.getLogger(__name__)

FUTU_HOST = os.getenv("FUTU_HOST", "127.0.0.1")
FUTU_PORT = int(os.getenv("FUTU_PORT", "11111"))
FUTU_TRADE_PWD = os.getenv("FUTU_TRADE_PWD", "")
FUTU_TRADE_ENV_STR = os.getenv("FUTU_TRADE_ENV", "SIMULATE")


def _get_trade_env():
    try:
        from futu import TrdEnv
        return TrdEnv.SIMULATE if FUTU_TRADE_ENV_STR == "SIMULATE" else TrdEnv.REAL
    except ImportError:
        return None


class FutuClient:
    """Futu OpenD との接続・注文発行ラッパー"""

    def __init__(self):
        self.trd_ctx = None
        self.quote_ctx = None

    def connect(self):
        try:
            from futu import OpenSecTradeContext, OpenQuoteContext, TrdMarket
            self.trd_ctx = OpenSecTradeContext(
                filter_trdmarket=TrdMarket.JP,
                host=FUTU_HOST,
                port=FUTU_PORT,
                security_firm=__import__("futu").SecurityFirm.FUTUSECURITIES,
            )
            self.quote_ctx = OpenQuoteContext(host=FUTU_HOST, port=FUTU_PORT)
            logger.info(f"[futu] 接続成功 ({FUTU_HOST}:{FUTU_PORT})")
        except ImportError:
            logger.warning("[futu] futu-api 未インストール。モック動作します。")
        except Exception as e:
            logger.error(f"[futu] 接続失敗: {e}")
            raise

    def disconnect(self):
        if self.trd_ctx:
            self.trd_ctx.close()
        if self.quote_ctx:
            self.quote_ctx.close()
        logger.info("[futu] 切断")

    def get_account_info(self) -> dict:
        """残高・資産情報を取得"""
        if self.trd_ctx is None:
            return self._mock_account_info()
        try:
            ret, data = self.trd_ctx.accinfo_query(trd_env=_get_trade_env())
            if ret == 0:
                row = data.iloc[0]
                return {
                    "cash": float(row.get("cash", 0)),
                    "total_assets": float(row.get("total_assets", 0)),
                    "market_value": float(row.get("market_val", 0)),
                }
            logger.error(f"[futu] accinfo_query失敗: {data}")
            return {}
        except Exception as e:
            logger.error(f"[futu] get_account_info失敗: {e}")
            return {}

    def get_positions(self) -> list[dict]:
        """現在のポジションリストを取得"""
        if self.trd_ctx is None:
            return []
        try:
            ret, data = self.trd_ctx.position_list_query(trd_env=_get_trade_env())
            if ret == 0 and not data.empty:
                return data[["code", "qty", "cost_price", "market_val"]].to_dict("records")
            return []
        except Exception as e:
            logger.error(f"[futu] get_positions失敗: {e}")
            return []

    def place_order(self, symbol: str, action: str, quantity: int,
                    price: float, order_type: str = "NORMAL") -> dict:
        """
        注文発行
        action: 'buy' or 'sell'
        order_type: 'NORMAL'(指値) or 'MARKET'(成行)
        """
        if self.trd_ctx is None:
            return self._mock_place_order(symbol, action, quantity, price)
        try:
            from futu import TrdSide, OrderType
            trd_side = TrdSide.BUY if action == "buy" else TrdSide.SELL
            ret, data = self.trd_ctx.place_order(
                price=price,
                qty=quantity,
                code=symbol,
                trd_side=trd_side,
                order_type=OrderType.NORMAL,
                trd_env=_get_trade_env(),
                pwd_unlock=FUTU_TRADE_PWD,
            )
            if ret == 0:
                order_id = data["order_id"].values[0]
                logger.info(f"[futu] 注文成功 {symbol} {action} {quantity}株 @{price} order_id={order_id}")
                return {"success": True, "order_id": str(order_id)}
            logger.error(f"[futu] 注文失敗: {data}")
            return {"success": False, "error": str(data)}
        except Exception as e:
            logger.error(f"[futu] place_order例外: {e}")
            return {"success": False, "error": str(e)}

    def _mock_account_info(self) -> dict:
        logger.info("[futu-mock] account_info")
        return {"cash": 1_000_000.0, "total_assets": 1_000_000.0, "market_value": 0.0}

    def _mock_place_order(self, symbol, action, quantity, price) -> dict:
        logger.info(f"[futu-mock] place_order {symbol} {action} {quantity} @{price}")
        return {"success": True, "order_id": "MOCK_001"}
