import os
import json
import time
import websocket

ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY")

if not ALCHEMY_API_KEY:
    raise RuntimeError("ALCHEMY_API_KEY is missing")

WS_URL = f"wss://base-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}"

QUID = "0x1a44233fae8d50f1aeb3a5d58dd426ff4814cb53"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"

# PancakeSwap V3 Factory on Base
PANCAKE_FACTORY = "0x0bfbcf9fa4f9c56b0f40a671ad40e0805a091865"

# Verified QUID / USDC PancakeSwap V3 pool
QUID_USDC_POOL = "0x07c4bc0f5fb6cb069124df3e1ae0b8fd8148ccc4"

# ERC-20 Transfer event
TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4"
    "a11628f55a4df523b3ef"
)

# PancakeSwap V3 Swap event
SWAP_TOPIC = (
    "0x19b47279256b2a23a1665c810c8d55a1758940ee09377d4f8"
    "26497a3577dc83"
)

QUID_DECIMALS = 18
USDC_DECIMALS = 6

# Pool factory() selector
FACTORY_SELECTOR = "0xc45a0155"

# Pool slot0() selector
SLOT0_SELECTOR = "0x3850c7bd"

wallet_volume = {}
seen_transactions = set()
known_pancake_pools = set()

quid_usdc_price = 0.0
last_price_update = 0


def rpc(ws, method, params, request_id):
    message = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params
    }

    ws.send(json.dumps(message))

    while True:
        response = json.loads(ws.recv())

        if response.get("id") == request_id:
            if "error" in response:
                raise RuntimeError(str(response["error"]))

            return response.get("result")


def signed_int256(value):
    number = int(value, 16)

    if number >= 2 ** 255:
        number -= 2 ** 256

    return number


def get_wallet(ws, tx_hash):
    tx = rpc(
        ws,
        "eth_getTransactionByHash",
        [tx_hash],
        500
    )

    if not tx:
        return None

    return tx.get("from", "").lower()


def get_pool_factory(ws, pool):
    try:
        result = rpc(
            ws,
            "eth_call",
            [{
                "to": pool,
                "data": FACTORY_SELECTOR
            }, "latest"],
            600
        )

        if not result or len(result) < 42:
            return None

        return "0x" + result[-40:].lower()

    except Exception as e:
        print("Factory check error:", e)
        return None


def is_pancakeswap_pool(ws, pool):
    pool = pool.lower()

    if pool in known_pancake_pools:
        return True

    factory = get_pool_factory(ws, pool)

    if factory == PANCAKE_FACTORY:
        known_pancake_pools.add(pool)
        return True

    return False


def update_quid_usdc_price(ws):
    global quid_usdc_price
    global last_price_update

    now = time.time()

    # Refresh price at most once every 30 seconds.
    if now - last_price_update < 30 and quid_usdc_price > 0:
        return quid_usdc_price

    try:
        result = rpc(
            ws,
            "eth_call",
            [{
                "to": QUID_USDC_POOL,
                "data": SLOT0_SELECTOR
            }, "latest"],
            700
        )

        if not result or len(result) < 66:
            return quid_usdc_price

        data = result[2:]

        # slot0 first value = sqrtPriceX96
        sqrt_price_x96 = int(data[0:64], 16)

        if sqrt_price_x96 <= 0:
            return quid_usdc_price

        # QUID is token0 and USDC is token1.
        raw_price = (
            sqrt_price_x96 * sqrt_price_x96
        ) / (2 ** 192)

        # Convert raw token1/token0 to human USDC/QUID.
        price = raw_price * (
            10 ** QUID_DECIMALS
        ) / (
            10 ** USDC_DECIMALS
        )

        if price > 0:
            quid_usdc_price = price
            last_price_update = now

    except Exception as e:
        print("Price update error:", e)

    return quid_usdc_price


def extract_quid_amount_from_swap(log):
    data = log.get("data", "")

    if not data or len(data) < 258:
        return 0.0

    data = data[2:]

    amount0 = signed_int256(data[0:64])
    amount1 = signed_int256(data[64:128])

    pool = log.get("address", "").lower()

    # Known QUID/USDC pool:
    # token0 = QUID
    # token1 = USDC
    if pool == QUID_USDC_POOL:
        return abs(amount0) / (10 ** QUID_DECIMALS)

    return None


def get_quid_amount_for_unknown_pool(ws, pool, swap_log):
    """
    Read token0()/token1() from the pool and determine
    which Swap amount belongs to QUID.
    """

    try:
        token0_result = rpc(
            ws,
            "eth_call",
            [{
                "to": pool,
                "data": "0x0dfe1681"
            }, "latest"],
            800
        )

        token1_result = rpc(
            ws,
            "eth_call",
            [{
                "to": pool,
                "data": "0xd21220a7"
            }, "latest"],
            801
        )

        token0 = "0x" + token0_result[-40:].lower()
        token1 = "0x" + token1_result[-40:].lower()

        data = swap_log["data"][2:]

        amount0 = signed_int256(data[0:64])
        amount1 = signed_int256(data[64:128])

        if token0 == QUID:
            return abs(amount0) / (10 ** QUID_DECIMALS)

        if token1 == QUID:
            return abs(amount1) / (10 ** QUID_DECIMALS)

    except Exception as e:
        print("Pool token error:", e)

    return 0.0


def find_quid_swaps(ws, receipt):
    """
    Look through the transaction receipt.

    A transaction only counts if:
    1. It contains QUID.
    2. It contains a PancakeSwap V3 Swap event.
    3. The Swap event comes from a PancakeSwap V3 pool.
    """

    swap_logs = []

    for log in receipt.get("logs", []):
        topics = log.get("topics", [])

        if not topics:
            continue

        if topics[0].lower() != SWAP_TOPIC:
            continue

        pool = log.get("address", "").lower()

        if not is_pancakeswap_pool(ws, pool):
            continue

        swap_logs.append(log)

    return swap_logs


def process_quid_transfer(ws, transfer_log):
    tx_hash = transfer_log.get("transactionHash", "").lower()

    if not tx_hash:
        return

    if tx_hash in seen_transactions:
        return

    # Get complete transaction receipt.
    receipt = rpc(
        ws,
        "eth_getTransactionReceipt",
        [tx_hash],
        900
    )

    if not receipt:
        return

    if receipt.get("status") != "0x1":
        return

    swap_logs = find_quid_swaps(ws, receipt)

    if not swap_logs:
        # It was a QUID transfer, but NOT a PancakeSwap swap.
        return

    # One transaction = one trade for our tracker.
    swap_log = swap_logs[0]

    pool = swap_log.get("address", "").lower()

    if pool == QUID_USDC_POOL:
        quid_amount = extract_quid_amount_from_swap(swap_log)
    else:
        quid_amount = get_quid_amount_for_unknown_pool(
            ws,
            pool,
            swap_log
       
