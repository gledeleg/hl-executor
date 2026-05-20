import os
import time
from decimal import Decimal, ROUND_DOWN
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
        symbol.replace("/USDT", "")
        .replace("USDT", "")
        .replace("/USDC", "")
        .replace("USDC", "")
        .upper()
    )


def get_clients():
    if not PRIVATE_KEY:
        raise HTTPException(status_code=500, detail="PRIVATE_KEY is not set")

    account = Account.from_key(PRIVATE_KEY)
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    exchange = Exchange(account, constants.MAINNET_API_URL)

    return account, info, exchange


def get_coin_meta(info: Info, coin: str):
    meta = info.meta()
    universe = meta.get("universe", [])

    for item in universe:
        if item.get("name") == coin:
            return item

    raise Exception(f"Coin meta not found for {coin}")


def round_size_down(size: float, sz_decimals: int) -> float:
    step = Decimal("1").scaleb(-sz_decimals)
    value = Decimal(str(size)).quantize(step, rounding=ROUND_DOWN)
    return float(value)


def round_price(price: float) -> float:
    return float(Decimal(str(price)).quantize(Decimal("0.01"), rounding=ROUND_DOWN))


def get_position_size(info: Info, address: str, coin: str) -> float:
    state = info.user_state(address)
    positions = state.get("assetPositions", [])

    for p in positions:
        pos = p.get("position", {})
        if pos.get("coin") == coin:
            return float(pos.get("szi", 0))

    return 0.0


def has_hl_error(result) -> bool:
    try:
        statuses = result.get("response", {}).get("data", {}).get("statuses", [])
        for status in statuses:
            if "error" in status:
                return True
    except Exception:
        pass
    return False


def get_hl_error(result) -> str:
    try:
        statuses = result.get("response", {}).get("data", {}).get("statuses", [])
        for status in statuses:
            if "error" in status:
                return str(status["error"])
    except Exception:
        pass
    return "Unknown Hyperliquid error"


def cancel_existing_stop_orders(info: Info, exchange: Exchange, address: str, coin: str):
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

            reduce_only = order.get("reduceOnly") is True
            is_trigger = (
                order.get("isTrigger") is True
                or "trigger" in str(order).lower()
                or "stop" in str(order).lower()
            )

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


def place_stop_loss(exchange: Exchange, coin: str, position_size: float, stop_price: float):
    if position_size == 0:
        raise Exception("POSITION SIZE = 0")

    if stop_price <= 0:
        raise Exception("INVALID STOP PRICE")

    is_buy = position_size < 0
    size = abs(position_size)
    stop_price = round_price(float(stop_price))

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

    if has_hl_error(stop_result):
        raise Exception(f"STOP ORDER ERROR: {get_hl_error(stop_result)}")

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
    coin = normalize_symbol(payload.symbol)

    if action == "NO_ACTION":
        return {
            "ok": True,
            "dryRun": DRY_RUN,
            "message": "NO_ACTION",
            "coin": coin,
            "payload": dump_payload(payload)
        }

    allowed_actions = [
        "OPEN_LONG",
        "OPEN_SHORT",
        "CLOSE_LONG",
        "CLOSE_SHORT",
        "MOVE_STOP"
    ]

    if action not in allowed_actions:
        raise HTTPException(status_code=400, detail=f"Unknown action: {payload.action}")

    if DRY_RUN:
        return {
            "ok": True,
            "dryRun": True,
            "message": "DRY RUN ONLY",
            "coin": coin,
            "payload": dump_payload(payload)
        }

    try:
        account, info, exchange = get_clients()
        address = account.address

        coin_meta = get_coin_meta(info, coin)
        sz_decimals = int(coin_meta.get("szDecimals", 2))

        print("ADDRESS:", address)
        print("COIN:", coin)
        print("ACTION:", action)
        print("SZ_DECIMALS:", sz_decimals)

        current_position_size = get_position_size(info, address, coin)
        print("CURRENT POSITION SIZE:", current_position_size)

        if action in ["OPEN_LONG", "OPEN_SHORT"]:
            if current_position_size != 0:
                return {
                    "ok": False,
                    "dryRun": False,
                    "message": "POSITION ALREADY EXISTS",
                    "coin": coin,
                    "currentPositionSize": current_position_size,
                    "payload": dump_payload(payload)
                }

            position_size_input = payload.positionSize
            if position_size_input is None:
                position_size_input = payload.positionSizeEth

            if position_size_input is None:
                raise Exception("positionSize missing")

            if payload.stopPrice is None:
                raise Exception("stopPrice missing")

            size = round_size_down(float(position_size_input), sz_decimals)
            stop_price = round_price(float(payload.stopPrice))

            if size <= 0:
                raise Exception("INVALID SIZE AFTER ROUNDING")

            if stop_price <= 0:
                raise Exception("INVALID STOP PRICE")

            is_buy = action == "OPEN_LONG"

            print("SEND MARKET OPEN")
            print("BUY:", is_buy)
            print("SIZE:", size)

            open_result = exchange.market_open(coin, is_buy, size)
            print("OPEN RESULT:", open_result)

            if has_hl_error(open_result):
                raise Exception(f"OPEN ORDER ERROR: {get_hl_error(open_result)}")

            time.sleep(1)

            real_position_size = get_position_size(info, address, coin)
            print("REAL POSITION SIZE:", real_position_size)

            if real_position_size == 0:
                return {
                    "ok": False,
                    "dryRun": False,
                    "message": "POSITION NOT FOUND AFTER OPEN",
                    "openResult": open_result,
                    "payload": dump_payload(payload)
                }

            cancelled = cancel_existing_stop_orders(info, exchange, address, coin)
            stop_result = place_stop_loss(exchange, coin, real_position_size, stop_price)

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
            if current_position_size == 0:
                return {
                    "ok": False,
                    "dryRun": False,
                    "message": "NO POSITION TO CLOSE",
                    "coin": coin,
                    "action": action
                }

            if action == "CLOSE_LONG" and current_position_size < 0:
                return {
                    "ok": False,
                    "dryRun": False,
                    "message": "WRONG POSITION SIDE. CURRENT IS SHORT",
                    "coin": coin,
                    "positionSize": current_position_size
                }

            if action == "CLOSE_SHORT" and current_position_size > 0:
                return {
                    "ok": False,
                    "dryRun": False,
                    "message": "WRONG POSITION SIDE. CURRENT IS LONG",
                    "coin": coin,
                    "positionSize": current_position_size
                }

            cancelled = cancel_existing_stop_orders(info, exchange, address, coin)

            print("SEND MARKET CLOSE")
            close_result = exchange.market_close(coin)
            print("CLOSE RESULT:", close_result)

            if has_hl_error(close_result):
                raise Exception(f"CLOSE ORDER ERROR: {get_hl_error(close_result)}")

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

            stop_price = round_price(float(payload.stopPrice))

            if stop_price <= 0:
                raise Exception("INVALID STOP PRICE")

            real_position_size = get_position_size(info, address, coin)

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

            cancelled = cancel_existing_stop_orders(info, exchange, address, coin)
            stop_result = place_stop_loss(exchange, coin, real_position_size, stop_price)

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
