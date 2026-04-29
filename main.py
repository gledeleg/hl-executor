import os
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
EXECUTOR_SECRET = os.getenv("EXECUTOR_SECRET", "123456")

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
    if x_executor_secret != EXECUTOR_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "hl-executor",
        "dryRun": DRY_RUN
    }


@app.post("/trade")
def trade(payload: TradePayload, x_executor_secret: str | None = Header(default=None)):
    check_secret(x_executor_secret)

    if payload.action == "NO_ACTION":
        return {
            "ok": True,
            "dryRun": DRY_RUN,
            "message": "No trading signal",
            "payload": payload.dict()
        }

    if payload.action not in ["OPEN_LONG", "OPEN_SHORT", "CLOSE_LONG", "CLOSE_SHORT", "MOVE_STOP"]:
        raise HTTPException(status_code=400, detail=f"Unknown action: {payload.action}")

    if DRY_RUN:
        return {
            "ok": True,
            "dryRun": True,
            "message": "DRY RUN: real order was NOT sent",
            "wouldExecute": {
                "symbol": payload.symbol,
                "action": payload.action,
                "side": payload.side,
                "entryPrice": payload.entryPrice,
                "stopPrice": payload.stopPrice,
                "positionSizeEth": payload.positionSizeEth,
                "positionValueUsdc": payload.positionValueUsdc,
                "riskAmount": payload.riskAmount,
                "riskPercent": payload.riskPercent,
            }
        }

    return {
        "ok": False,
        "dryRun": False,
        "message": "Real trading is not enabled yet. Hyperliquid signing will be added in next step."
    }