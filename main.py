import os
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

load_dotenv()

DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
EXECUTOR_SECRET = os.getenv("EXECUTOR_SECRET")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")

app = FastAPI()


class TradePayload(BaseModel):
    botName: Optional[str] = None
    symbol: str
    action: str
    side: Optional[str] = None

    positionSize: Optional[float] = None
    positionSizeEth: Optional[float] = None

    accountValue: Optional[float] = None
    riskPercent: Optional[float] = None
    riskAmount: Optional[float] = None

    entryPrice: Optional[float] = None
    stopPrice: Optional[float] = None
    stopDistance: Optional[float] = None

    positionValueUsdc: Optional[float] = None

    atr: Optional[float] = None
    adx: Optional[float] = None
    ema100Daily: Optional[float] = None

    lastClosedCandleTime: Optional[int] = None


def dump_payload(payload: TradePayload):
    return payload.model_dump()


def check_secret(x_executor_secret: Optional[str]):
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


def get_clients():
    if not PRIVATE_KEY:
        raise HTTPException(status_code=500, detail="PRIVATE_KEY is not set")

    account = Account.from_key(PRIVATE_KEY)

    info = Info(
        constants.MAINNET_API_URL,
        skip_ws=True
    )

    exchange = Exchange(
        account,
        constants.MAINNET_API_URL
    )

    return account, info, exchange


def get_position_size(info: Info, address: str, coin: str) -> float:
    state = info.user_state(address)
    positions = state.get("assetPositions", [])

    for p in positions:
        pos = p.get("position", {})
        if pos.get("coin") == coin:
            return float(pos.get("szi", 0))

    return 0.0


def cancel_existing_stop_orders(
    info: Info,
    exchange: Exchange,
    address: str,
    coin: str
):
    cancelled = []

    try:
        orders = info.frontend_open_orders(address)
    except Exception as e:
        print("ERROR FETCHING OPEN ORDERS:", str(e))
        return cancelled

    for order in orders:
        try:
            if order.get("coin") != coin:
                continue

            is_trigger = order.get("isTrigger") is True
            reduce_only = order.get("reduceOnly") is True

            if is_trigger and reduce_only:
                oid = order.get("oid")
                print("CANCEL STOP:", oid)

                result = exchange.cancel(coin, oid)

                cancelled.append({
                    "oid": oid,
                    "result": result
                })

        except Exception as e:
            print("STOP CANCEL ERROR:", str(e))

    return cancelled


def place_stop_loss(
    exchange: Exchange,
    coin: str,
    position_size: float,
    stop_price: float
):
    if position_size == 0:
        raise Exception("POSITION SIZE = 0")

    if stop_price <= 0:
        raise Exception("INVALID STOP PRICE")

    is_buy = position_size < 0
    size = abs(position_size)

    print("PLACE STOP")
    print("coin:", coin)
    print("is_buy:", is_buy)
    print("size:", size)
    print("stop:", stop_price)

    stop_result = exchange.order(
        coin,
        is_buy,
        size,
        stop_price,
        order_type={
            "trigger": {
                "isMarket": True,
                "triggerPx": str(stop_price),
                "tpsl": "sl"
            }
        },
        reduce_only=True
    )

    print("STOP RESULT:", stop_result)

    return stop_result


@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "hl-executor",
        "dryRun": DRY_RUN,
        "privateKeyLoaded": bool(PRIVATE_KEY)
    }


@app.post("/trade")
def trade(
    payload: TradePayload,
    x_executor_secret: Optional[str] = Header(default=None)
):
    check_secret(x_executor_secret)

    print("===================================")
    print("NEW TRADE REQUEST")
    print(dump_payload(payload))

    action = payload.action.upper()

    if action == "NO_ACTION":
        return {
            "ok": True,
            "dryRun": DRY_RUN,
            "message": "NO_ACTION"
        }

    allowed_actions = [
        "OPEN_LONG",
        "OPEN_SHORT",
        "CLOSE_LONG",
        "CLOSE_SHORT",
        "MOVE_STOP"
    ]

    if action not in allowed_actions:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown action: {payload.action}"
        )

    coin = normalize_symbol(payload.symbol)

    if DRY_RUN:
        return {
            "ok": True,
            "dryRun": True,
            "message": "DRY RUN ONLY",
            "payload": dump_payload(payload)
        }

    try:
        account, info, exchange = get_clients()
        address = account.address

        print("ADDRESS:", address)
        print("COIN:", coin)
        print("ACTION:", action)

        if action in ["OPEN_LONG", "OPEN_SHORT"]:

            position_size_input = payload.positionSize

            if position_size_input is None:
                position_size_input = payload.positionSizeEth

            if position_size_input is None:
                raise Exception("positionSize missing")

            if payload.stopPrice is None:
                raise Exception("stopPrice missing")

            size = float(position_size_input)
            stop_price = float(payload.stopPrice)

            if size <= 0:
                raise Exception("INVALID SIZE")

            if stop_price <= 0:
                raise Exception("INVALID STOP PRICE")

            is_buy = action == "OPEN_LONG"

            print("SEND MARKET OPEN")
            print("BUY:", is_buy)
            print("SIZE:", size)

            open_result = exchange.market_open(
                coin,
                is_buy,
                size
            )

            print("OPEN RESULT:", open_result)

            real_position_size = get_position_size(
                info,
                address,
                coin
            )

            print("REAL POSITION SIZE:", real_position_size)

            if real_position_size == 0:
                return {
                    "ok": False,
                    "dryRun": False,
                    "message": "POSITION NOT FOUND AFTER OPEN",
                    "openResult": open_result,
                    "payload": dump_payload(payload)
                }

            cancelled = cancel_existing_stop_orders(
                info,
                exchange,
                address,
                coin
            )

            stop_result = place_stop_loss(
                exchange,
                coin,
                real_position_size,
                stop_price
            )

            return {
                "ok": True,
                "dryRun": False,
                "message": "POSITION OPENED",
                "coin": coin,
                "action": action,
                "openResult": open_result,
                "stopResult": stop_result,
                "cancelledStops": cancelled,
                "positionSize": real_position_size
            }

        if action in ["CLOSE_LONG", "CLOSE_SHORT"]:

            cancelled = cancel_existing_stop_orders(
                info,
                exchange,
                address,
                coin
            )

            print("SEND MARKET CLOSE")

            close_result = exchange.market_close(coin)

            print("CLOSE RESULT:", close_result)

            return {
                "ok": True,
                "dryRun": False,
                "message": "POSITION CLOSED",
                "coin": coin,
                "action": action,
                "closeResult": close_result,
                "cancelledStops": cancelled
            }

        if action == "MOVE_STOP":

            if payload.stopPrice is None:
                raise Exception("stopPrice missing")

            stop_price = float(payload.stopPrice)

            if stop_price <= 0:
                raise Exception("INVALID STOP PRICE")

            real_position_size = get_position_size(
                info,
                address,
                coin
            )

            print("MOVE STOP")
            print("REAL POSITION SIZE:", real_position_size)

            if real_position_size == 0:
                return {
                    "ok": False,
                    "dryRun": False,
                    "message": "NO POSITION FOR MOVE_STOP",
                    "coin": coin,
                    "action": action
                }

            cancelled = cancel_existing_stop_orders(
                info,
                exchange,
                address,
                coin
            )

            stop_result = place_stop_loss(
                exchange,
                coin,
                real_position_size,
                stop_price
            )

            return {
                "ok": True,
                "dryRun": False,
                "message": "STOP MOVED",
                "coin": coin,
                "action": action,
                "stopResult": stop_result,
                "cancelledStops": cancelled
            }

    except Exception as e:
        print("EXECUTION ERROR:", str(e))

        return {
            "ok": False,
            "dryRun": False,
            "message": "EXECUTION ERROR",
            "error": str(e),
            "payload": dump_payload(payload)
        }
