import os
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

load_dotenv()

DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
EXECUTOR_SECRET = os.getenv("EXECUTOR_SECRET")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")

app = FastAPI()


class TradePayload(BaseModel):
    botName: str | None = None
    symbol: str
    action: str
    side: str | None = None

    accountValue: float | None = None
    riskPercent: float | None = None
    riskAmount: float | None = None

    entryPrice: float | None = None
    stopPrice: float | None = None
    stopDistance: float | None = None

    positionSizeEth: float | None = None
    positionValueUsdc: float | None = None

    atr: float | None = None
    adx: float | None = None
    ema100Daily: float | None = None
    lastClosedCandleTime: int | None = None


def check_secret(x_executor_secret: str | None):
    if not EXECUTOR_SECRET:
        raise HTTPException(status_code=500, detail="EXECUTOR_SECRET is not set")

    if x_executor_secret != EXECUTOR_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")


def normalize_symbol(symbol: str) -> str:
    return (
        symbol
        .replace("/USDT", "")
        .replace("USDT", "")
        .replace("/USDC", "")
        .replace("USDC", "")
        .upper()
    )


@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "hl-executor",
        "dryRun": DRY_RUN,
        "privateKeyLoaded": bool(PRIVATE_KEY),
    }


@app.post("/trade")
def trade(payload: TradePayload, x_executor_secret: str | None = Header(default=None)):
    check_secret(x_executor_secret)

    print("========== NEW TRADE REQUEST ==========")
    print("PAYLOAD:", payload.dict())

    if payload.action == "NO_ACTION":
        return {
            "ok": True,
            "dryRun": DRY_RUN,
            "message": "No trading signal",
            "payload": payload.dict(),
        }

    allowed_actions = [
        "OPEN_LONG",
        "OPEN_SHORT",
        "CLOSE_LONG",
        "CLOSE_SHORT",
        "MOVE_STOP",
    ]

    if payload.action not in allowed_actions:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown action: {payload.action}",
        )

    coin = normalize_symbol(payload.symbol)

    if DRY_RUN:
        return {
            "ok": True,
            "dryRun": True,
            "message": "DRY RUN: real order was NOT sent",
            "wouldExecute": {
                "coin": coin,
                "symbol": payload.symbol,
                "action": payload.action,
                "side": payload.side,
                "entryPrice": payload.entryPrice,
                "stopPrice": payload.stopPrice,
                "positionSizeEth": payload.positionSizeEth,
                "positionValueUsdc": payload.positionValueUsdc,
                "riskAmount": payload.riskAmount,
                "riskPercent": payload.riskPercent,
            },
        }

    if not PRIVATE_KEY:
        raise HTTPException(status_code=500, detail="PRIVATE_KEY is not set")

    try:
        account = Account.from_key(PRIVATE_KEY)

        exchange = Exchange(
            account,
            constants.MAINNET_API_URL,
        )

        print("ACCOUNT ADDRESS:", account.address)
        print("COIN:", coin)
        print("ACTION:", payload.action)
        print("SIDE:", payload.side)
        print("SIZE:", payload.positionSizeEth)

        if payload.action in ["OPEN_LONG", "OPEN_SHORT"]:
            if not payload.positionSizeEth or payload.positionSizeEth <= 0:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid position size: {payload.positionSizeEth}",
                )

            is_buy = payload.action == "OPEN_LONG"

            print("SENDING MARKET OPEN")
            print("is_buy:", is_buy)
            print("size:", float(payload.positionSizeEth))

            order_result = exchange.market_open(
                coin,
                is_buy,
                float(payload.positionSizeEth),
            )

            print("ORDER RESULT:", order_result)

            return {
                "ok": True,
                "dryRun": False,
                "message": "Market open sent",
                "coin": coin,
                "action": payload.action,
                "orderResult": order_result,
            }

        if payload.action in ["CLOSE_LONG", "CLOSE_SHORT"]:
            print("SENDING MARKET CLOSE")

            close_result = exchange.market_close(coin)

            print("CLOSE RESULT:", close_result)

            return {
                "ok": True,
                "dryRun": False,
                "message": "Market close sent",
                "coin": coin,
                "action": payload.action,
                "orderResult": close_result,
            }

        if payload.action == "MOVE_STOP":
            print("MOVE_STOP received, but stop-order modification is not implemented yet")

            return {
                "ok": True,
                "dryRun": False,
                "message": "MOVE_STOP received, but stop-order modification is not implemented yet",
                "coin": coin,
                "action": payload.action,
                "stopPrice": payload.stopPrice,
                "payload": payload.dict(),
            }

    except Exception as e:
        print("EXECUTION ERROR:", str(e))

        return {
            "ok": False,
            "dryRun": False,
            "message": "Execution error",
            "error": str(e),
            "payload": payload.dict(),
        }
