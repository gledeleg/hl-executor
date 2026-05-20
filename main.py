import os
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

load_dotenv()

# =========================
# ENV
# =========================

DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
EXECUTOR_SECRET = os.getenv("EXECUTOR_SECRET")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")

app = FastAPI()

# =========================
# PAYLOAD
# =========================

class TradePayload(BaseModel):
    botName: str | None = None

    symbol: str
    action: str
    side: Optional[str] = None

    positionSize: float | None = None
    positionSizeEth: float | None = None
    
    accountValue: float | None = None

    riskPercent: float | None = None
    riskAmount: float | None = None

    entryPrice: Optional[float] = None
    stopPrice: Optional[float] = None
    stopDistance: float | None = None

    positionValueUsdc: float | None = None

    atr: float | None = None
    adx: float | None = None
    ema100Daily: float | None = None

    lastClosedCandleTime: int | None = None

# =========================
# HELPERS
# =========================

def check_secret(x_executor_secret: str | None):
    if not EXECUTOR_SECRET:
        raise HTTPException(
            status_code=500,
            detail="EXECUTOR_SECRET is not set"
        )

    if x_executor_secret != EXECUTOR_SECRET:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized"
        )


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
        raise HTTPException(
            status_code=500,
            detail="PRIVATE_KEY is not set"
        )

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


def get_position_size(info: Info, address: str, coin: str):
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

                result = exchange.cancel(
                    coin,
                    oid
                )

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

    # LONG -> stop sells
    # SHORT -> stop buys

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

# =========================
# HEALTH
# =========================

@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "hl-executor",
        "dryRun": DRY_RUN,
        "privateKeyLoaded": bool(PRIVATE_KEY)
    }

# =========================
# TRADE
# =========================

@app.post("/trade")
def trade(
    payload: TradePayload,
    x_executor_secret: str | None = Header(default=None)
):
    check_secret(x_executor_secret)

    print("===================================")
    print("NEW TRADE REQUEST")
    print(payload.dict())

    if payload.action == "NO_ACTION":
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

    if payload.action not in allowed_actions:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown action: {payload.action}"
        )

    coin = normalize_symbol(payload.symbol)

    # =========================
    # DRY RUN
    # =========================

    if DRY_RUN:
        return {
            "ok": True,
            "dryRun": True,

            "message": "DRY RUN ONLY",

            "payload": payload.dict()
        }

    # =========================
    # REAL EXECUTION
    # =========================

    try:
        account, info, exchange = get_clients()

        address = account.address

        print("ADDRESS:", address)
        print("COIN:", coin)
        print("ACTION:", payload.action)

        # =========================
        # OPEN LONG / SHORT
        # =========================

        if payload.action in ["OPEN_LONG", "OPEN_SHORT"]:

            position_size = payload.positionSize or payload.positionSizeEth

            if not position_size:
                raise Exception("positionSize missing")

            if not payload.stopPrice:
                raise Exception("stopPrice missing")

            size = float(position_size)

            stop_price = float(payload.stopPrice)

            if size <= 0:
                raise Exception("INVALID SIZE")

            is_buy = payload.action == "OPEN_LONG"

            print("SEND MARKET OPEN")
            print("BUY:", is_buy)
            print("SIZE:", size)

            open_result = exchange.market_open(
                coin,
                is_buy,
                size
            )

            print("OPEN RESULT:", open_result)

            # =========================
            # GET REAL POSITION SIZE
            # =========================

            position_size = get_position_size(
                info,
                address,
                coin
            )

            print("POSITION SIZE:", position_size)

            if position_size == 0:
                return {
                    "ok": False,
                    "dryRun": False,
                    "message": "POSITION NOT FOUND AFTER OPEN",
                    "openResult": open_result
                }

            # =========================
            # REMOVE OLD STOP
            # =========================

            cancelled = cancel_existing_stop_orders(
                info,
                exchange,
                address,
                coin
            )

            # =========================
            # PLACE NEW STOP
            # =========================

            stop_result = place_stop_loss(
                exchange,
                coin,
                position_size,
                stop_price
            )

            return {
                "ok": True,
                "dryRun": False,

                "message": "POSITION OPENED",

                "coin": coin,
                "action": payload.action,

                "openResult": open_result,
                "stopResult": stop_result,

                "cancelledStops": cancelled,

                "positionSize": position_size
            }

        # =========================
        # CLOSE LONG / SHORT
        # =========================

        if payload.action in [
            "CLOSE_LONG",
            "CLOSE_SHORT"
        ]:

            cancelled = cancel_existing_stop_orders(
                info,
                exchange,
                address,
                coin
            )

            print("SEND MARKET CLOSE")

            close_result = exchange.market_close(
                coin
            )

            print("CLOSE RESULT:", close_result)

            return {
                "ok": True,
                "dryRun": False,

                "message": "POSITION CLOSED",

                "coin": coin,
                "action": payload.action,

                "closeResult": close_result,
                "cancelledStops": cancelled
            }

        # =========================
        # MOVE STOP
        # =========================

        if payload.action == "MOVE_STOP":

            if not payload.stopPrice:
                raise Exception("stopPrice missing")

            stop_price = float(payload.stopPrice)

            position_size = get_position_size(
                info,
                address,
                coin
            )

            print("MOVE STOP")
            print("POSITION SIZE:", position_size)

            if position_size == 0:
                return {
                    "ok": False,
                    "dryRun": False,
                    "message": "NO POSITION FOR MOVE_STOP"
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
                position_size,
                stop_price
            )

            return {
                "ok": True,
                "dryRun": False,

                "message": "STOP MOVED",

                "coin": coin,
                "action": payload.action,

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

            "payload": payload.dict()
        }
