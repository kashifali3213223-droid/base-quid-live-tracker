import os
import json
import time
import websocket

ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY")
if not ALCHEMY_API_KEY:
    raise RuntimeError("ALCHEMY_API_KEY is missing")

WS_URL = f"wss://base-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}"

QUID = "0x1a44233fae8d50f1aeb3a5d58dd426ff4814cb53".lower()
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913".lower()

# Verified PancakeSwap V3 QUID/USDC pool on Base.
QUID_USDC_POOL = "0x07c4bc0f5fb6cb069124df3e1ae0b8fd8148ccc4"

# PancakeSwap V3 factory on Base.
PANCAKE_FACTORY = "0x0bfbcf9fa4f9c56b0f40a671ad40e0805a091865"

FACTORY_SELECTOR = "0xc45a0155"
SLOT0_SELECTOR = "0x3850c7bd"

SWAP_TOPIC = (
    "0x19b47279256b2a23a1665c810c8d55a1758940ee09377d4f8"
    "26497a3577dc83"
)

QUID_DECIMALS = 18
USDC_DECIMALS = 6

wallet_volume = {}
wallet_swap_count = {}
seen_transactions = set()

# Cache pool -> True/False so factory() is not called for every swap.
pancake_pool_cache = {
    QUID_USDC_POOL: True
}

# Cache pool -> (token0, token1).
pool_tokens_cache = {}

quid_usdc_price = 0.0
request_id = 1


def rpc(ws, method, params):
    global request_id

    rid = request_id
    request_id += 1

    ws.send(json.dumps({
        "jsonrpc": "2.0",
        "id": rid,
        "method": method,
        "params": params
    }))

    while True:
        response = json.loads(ws.recv())

        if response.get("id") == rid:
            if "error" in response:
                raise RuntimeError(str(response["error"]))

            return response.get("result")


def eth_call(ws, to, data):
    return rpc(
        ws,
        "eth_call",
        [{
            "to": to,
            "data": data
        }, "latest"]
    )


def signed_int256(value):
    number = int(value, 16)

    if number >= 2 ** 255:
        number -= 2 ** 256

    return number


def get_pool_tokens(ws, pool):
    pool = pool.lower()

    if pool in pool_tokens_cache:
        return pool_tokens_cache[pool]

    token0_result = eth_call(
        ws,
        pool,
        "0x0dfe1681"
    )

    token1_result = eth_call(
        ws,
        pool,
        "0xd21220a7"
    )

    token0 = "0x" + token0_result[-40:].lower()
    token1 = "0x" + token1_result[-40:].lower()

    pool_tokens_cache[pool] = (token0, token1)

    return token0, token1


def is_pancakeswap_v3_pool(ws, pool):
    pool = pool.lower()

    if pool in pancake_pool_cache:
        return pancake_pool_cache[pool]

    try:
        factory_result = eth_call(
            ws,
            pool,
            FACTORY_SELECTOR
        )

        if not factory_result or len(factory_result) < 42:
            pancake_pool_cache[pool] = False
            return False

        factory = "0x" + factory_result[-40:].lower()

        is_pancake = factory == PANCAKE_FACTORY
        pancake_pool_cache[pool] = is_pancake

        return is_pancake

    except Exception as e:
        print("Pool factory check error:", e)
        return False


def update_quid_usdc_price(ws):
    """
    Get the USD price ONLY from the verified QUID/USDC pool.

    This is important: a QUID/WETH or QUID/other-token swap must
    NOT be interpreted as if the other token were USDC.
    """

    global quid_usdc_price

    try:
        result = eth_call(
            ws,
            QUID_USDC_POOL,
            SLOT0_SELECTOR
        )

        if not result or len(result) < 66:
            return quid_usdc_price

        sqrt_price_x96 = int(
            result[2:66],
            16
        )

        if sqrt_price_x96 <= 0:
            return quid_usdc_price

        # QUID is token0 and USDC is token1 in the verified pool.
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

    except Exception as e:
        print("QUID/USDC price error:", e)

    return quid_usdc_price


def get_transaction_sender(ws, tx_hash):
    tx = rpc(
        ws,
        "eth_getTransactionByHash",
        [tx_hash]
    )

    if not tx:
        return None

    return tx.get("from", "").lower()


def get_quid_amount(ws, pool, log):
    data = log.get("data", "")

    if not data or len(data) < 258:
        return 0.0

    data = data[2:]

    # PancakeSwap V3 Swap:
    # amount0, amount1, sqrtPriceX96, liquidity, tick, ...
    amount0 = signed_int256(data[0:64])
    amount1 = signed_int256(data[64:128])

    token0, token1 = get_pool_tokens(
        ws,
        pool
    )

    if token0 == QUID:
        return abs(amount0) / (10 ** QUID_DECIMALS)

    if token1 == QUID:
        return abs(amount1) / (10 ** QUID_DECIMALS)

    return 0.0


def process_swap(ws, log):
    tx_hash = log.get(
        "transactionHash",
        ""
    ).lower()

    pool = log.get(
        "address",
        ""
    ).lower()

    if not tx_hash or not pool:
        return

    # Count a transaction only once even if a route emits multiple
    # QUID-containing PancakeSwap pool Swap events.
    if tx_hash in seen_transactions:
        return

    # Ignore non-PancakeSwap V3 pools.
    if not is_pancakeswap_v3_pool(ws, pool):
        return

    try:
        token0, token1 = get_pool_tokens(
            ws,
            pool
        )
    except Exception as e:
        print("Pool token lookup error:", e)
        return

    # ONLY QUID pairs count.
    if QUID not in (token0, token1):
        return

    quid_amount = get_quid_amount(
        ws,
        pool,
        log
    )

    if quid_amount <= 0:
        return

    wallet = get_transaction_sender(
        ws,
        tx_hash
    )

    if not wallet:
        return

    # Mark after all validation so failed/non-QUID events don't block
    # a valid event from the same transaction.
    seen_transactions.add(tx_hash)

    # USD valuation is based on the verified QUID/USDC spot price.
    price = update_quid_usdc_price(ws)

    if price <= 0:
        print("QUID/USDC price unavailable; swap detected but USD volume skipped.")
        return

    volume_usd = quid_amount * price

    wallet_swap_count[wallet] = (
        wallet_swap_count.get(wallet, 0) + 1
    )

    wallet_volume[wallet] = (
        wallet_volume.get(wallet, 0.0) + volume_usd
    )

    print()
    print("🔥 QUID PANCAKESWAP V3 SWAP")
    print("----------------------------------------")
    print("Wallet:", wallet)
    print("Pool:", pool)
    print("TX:", tx_hash)
    print("QUID amount:", f"{quid_amount:,.6f}")
    print("QUID price USD:", f"${price:,.8f}")
    print("Trade volume USD:", f"${volume_usd:,.6f}")
    print("Wallet QUID swaps:", wallet_swap_count[wallet])
    print("Wallet total USD:", f"${wallet_volume[wallet]:,.6f}")
    print("----------------------------------------")

    print("===== WALLET TOTALS =====")

    ranked = sorted(
        wallet_volume.items(),
        key=lambda item: item[1],
        reverse=True
    )

    for address, volume in ranked:
        print(
            f"{address} | "
            f"swaps: {wallet_swap_count.get(address, 0)} | "
            f"${volume:,.2f}"
        )

    print("----------------------------------------")


def subscribe(ws):
    print("Subscribing to PancakeSwap V3 Swap events...")

    # Listen to Swap events across Base.
    # process_swap() filters them down to PancakeSwap V3 pools
    # where QUID is token0 or token1.
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_subscribe",
        "params": [
            "logs",
            {
                "topics": [SWAP_TOPIC]
            }
        ]
    }

    ws.send(json.dumps(message))

    while True:
        response = json.loads(ws.recv())

        if response.get("id") == 1:
            if "error" in response:
                raise RuntimeError(
                    str(response["error"])
                )

            print("Subscription:", response)
            break

    print()
    print("🔥 LIVE QUID PANCAKESWAP V3 TRACKER READY")
    print("Rule: PancakeSwap V3 + QUID in either token")
    print("Count: QUID swaps only")
    print()


def listen():
    while True:
        ws = None

        try:
            print("Connecting to Base via Alchemy...")

            ws = websocket.create_connection(
                WS_URL,
                timeout=60
            )

            # Get an initial QUID/USDC price before swaps arrive.
            update_quid_usdc_price(ws)

            subscribe(ws)

            while True:
                message = ws.recv()

                if not message:
                    continue

                data = json.loads(message)

                if data.get("method") != "eth_subscription":
                    continue

                result = (
                    data.get("params", {})
                    .get("result")
                )

                if not result:
                    continue

                try:
                    process_swap(
                        ws,
                        result
                    )

                except Exception as e:
                    print("SWAP ERROR:", e)

        except Exception as e:
            print("WEBSOCKET ERROR:", e)
            print("Reconnecting in 5 seconds...")
            time.sleep(5)

        finally:
            try:
                if ws:
                    ws.close()
            except Exception:
                pass


if __name__ == "__main__":
    print("========================================")
    print("    BASE QUID WALLET VOLUME TRACKER")
    print("========================================")
    print("Rule: PancakeSwap V3 + QUID in either token")
    print("Count: QUID swaps only")
    print("Output: Wallet + QUID swap count + USD volume")
    print()

    listen()
    
