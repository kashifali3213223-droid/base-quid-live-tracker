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
FACTORY = "0x0bfbcf9fa4f9c56b0f40a671ad40e0805a091865"

# PancakeSwap V3 PoolCreated
POOL_CREATED_TOPIC = (
    "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"
)

# PancakeSwap V3 Swap
SWAP_TOPIC = (
    "0x19b47279256b2a23a1665c810c8d55a1758940ee09377d4f8"
    "26497a3577dc83"
)

QUID_DECIMALS = 18
USDC_DECIMALS = 6

# Known QUID/USDC pool from your verified transaction
KNOWN_USDC_POOL = "0x07c4bc0f5fb6cb069124df3e1ae0b8fd8148ccc4"

wallet_volume = {}
seen_transactions = set()

qid_pools = set()
pool_tokens = {}

quid_usdc_price = 0.0


def rpc(ws, method, params, request_id=100):
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


def pad_address(address):
    return "0x" + address[2:].lower().zfill(64)


def decode_pool_created(log):
    topics = log.get("topics", [])

    if len(topics) < 4:
        return None

    token0 = "0x" + topics[1][-40:].lower()
    token1 = "0x" + topics[2][-40:].lower()

    data = log.get("data", "")

    if len(data) < 64:
        return None

    pool = "0x" + data[-40:].lower()

    if token0 == QUID or token1 == QUID:
        return pool, token0, token1

    return None


def discover_pools(ws):
    print("Discovering PancakeSwap QUID pools...")

    latest_hex = rpc(ws, "eth_blockNumber", [], 101)
    latest = int(latest_hex, 16)

    # Search in manageable chunks.
    # The filter is only for PancakeSwap PoolCreated events
    # involving the QUID token.
    chunk = 200000

    quid_topic = pad_address(QUID)

    for start in range(0, latest + 1, chunk):
        end = min(start + chunk - 1, latest)

        try:
            logs = rpc(
                ws,
                "eth_getLogs",
                [{
                    "fromBlock": hex(start),
                    "toBlock": hex(end),
                    "address": FACTORY,
                    "topics": [
                        POOL_CREATED_TOPIC,
                        [quid_topic],
                    ]
                }],
                102
            )

            for log in logs:
                decoded = decode_pool_created(log)

                if decoded:
                    pool, token0, token1 = decoded

                    qid_pools.add(pool)
                    pool_tokens[pool] = (token0, token1)

        except Exception as e:
            print("Pool discovery error:", e)

    # Also make absolutely sure the verified USDC pool is included.
    qid_pools.add(KNOWN_USDC_POOL)

    print("QUID pools found:", len(qid_pools))

    for pool in sorted(qid_pools):
        tokens = pool_tokens.get(pool)

        if tokens:
            print("POOL:", pool, "TOKENS:", tokens)
        else:
            print("POOL:", pool)


def get_transaction_sender(ws, tx_hash):
    tx = rpc(
        ws,
        "eth_getTransactionByHash",
        [tx_hash],
        200
    )

    if not tx:
        return None

    return tx.get("from", "").lower()


def get_pool_tokens(ws, pool):
    if pool in pool_tokens:
        return pool_tokens[pool]

    token0_call = {
        "to": pool,
        "data": "0x0dfe1681"
    }

    token1_call = {
        "to": pool,
        "data": "0xd21220a7"
    }

    token0_hex = rpc(ws, "eth_call", [token0_call, "latest"], 201)
    token1_hex = rpc(ws, "eth_call", [token1_call, "latest"], 202)

    token0 = "0x" + token0_hex[-40:].lower()
    token1 = "0x" + token1_hex[-40:].lower()

    pool_tokens[pool] = (token0, token1)

    return token0, token1


def update_usdc_price(log):
    global quid_usdc_price

    pool = log["address"].lower()

    if pool != KNOWN_USDC_POOL:
        return

    data = log["data"][2:]

    if len(data) < 128:
        return

    amount0 = signed_int256(data[0:64])
    amount1 = signed_int256(data[64:128])

    if amount0 == 0 or amount1 == 0:
        return

    # QUID is token0 and USDC is token1 in the verified pool.
    quid_amount = abs(amount0) / (10 ** QUID_DECIMALS)
    usdc_amount = abs(amount1) / (10 ** USDC_DECIMALS)

    if quid_amount > 0:
        quid_usdc_price = usdc_amount / quid_amount


def extract_quid_amount(log, token0, token1):
    data = log["data"][2:]

    if len(data) < 128:
        return 0.0

    amount0 = signed_int256(data[0:64])
    amount1 = signed_int256(data[64:128])

    if token0 == QUID:
        return abs(amount0) / (10 ** QUID_DECIMALS)

    if token1 == QUID:
        return abs(amount1) / (10 ** QUID_DECIMALS)

    return 0.0


def process_swap(ws, log):
    global quid_usdc_price

    tx_hash = log.get("transactionHash", "").lower()

    if not tx_hash:
        return

    # One transaction can produce multiple pool Swap events
    # during a multi-hop route. We only want the user's QUID trade
    # represented once for each QUID-containing pool.
    if tx_hash in seen_transactions:
        return

    pool = log.get("address", "").lower()

    if pool not in qid_pools:
        return

    token0, token1 = get_pool_tokens(ws, pool)

    if QUID not in (token0, token1):
        return

    update_usdc_price(log)

    quid_amount = extract_quid_amount(log, token0, token1)

    if quid_amount <= 0:
        return

    # For non-USDC QUID pairs, value the QUID amount using
    # the latest QUID/USDC pool price.
    if pool == KNOWN_USDC_POOL:
        volume_usd = quid_amount * quid_usdc_price
    else:
        if quid_usdc_price <= 0:
            print("Waiting for QUID/USDC price...")
            return

        volume_usd = quid_amount * quid_usdc_price

    if volume_usd <= 0:
        return

    wallet = get_transaction_sender(ws, tx_hash)

    if not wallet:
        return

    seen_transactions.add(tx_hash)

    wallet_volume[wallet] = wallet_volume.get(wallet, 0.0) + volume_usd

    print()
    print("🔥 QUID PANCAKESWAP TRADE")
    print("--------------------------------------")
    print("Wallet:", wallet)
    print("Pool:", pool)
    print("TX:", tx_hash)
    print("QUID amount:", f"{quid_amount:,.6f}")
    print("Trade USD:", f"${volume_usd:,.6f}")
    print()
    print("===== WALLET VOLUME =====")

    ranked = sorted(
        wallet_volume.items(),
        key=lambda x: x[1],
        reverse=True
    )

    for address, volume in ranked:
        print(
            f"{address} | ${volume:,.2f}"
        )

    print("--------------------------------------")


def subscribe_to_pools(ws):
    print("Subscribing to QUID pool Swap events...")

    subscription_ids = []

    for pool in sorted(qid_pools):
        message = {
            "jsonrpc": "2.0",
            "id": 300 + len(subscription_ids),
            "method": "eth_subscribe",
            "params": [
                "logs",
                {
                    "address": pool,
                    "topics": [SWAP_TOPIC]
                }
            ]
        }

        ws.send(json.dumps(message))
        subscription_ids.append(pool)

    # Read all subscription acknowledgements.
    acknowledged = 0

    while acknowledged < len(subscription_ids):
        response = json.loads(ws.recv())

        if "result" in response:
            acknowledged += 1

    print("Subscribed to:", len(subscription_ids), "QUID pools")
    print("🔥 LIVE QUID TRACKER READY")


def listen():
    while True:
        try:
            print("Connecting to Base via Alchemy...")

            ws = websocket.create_connection(
                WS_URL,
                timeout=60
            )

            discover_pools(ws)

            subscribe_to_pools(ws)

            while True:
                message = ws.recv()

                if not message:
                    continue

                data = json.loads(message)

                if data.get("method") != "eth_subscription":
                    continue

                result = data.get("params", {}).get("result")

                if result:
                    try:
                        process_swap(ws, result)
                    except Exception as e:
                        print("SWAP ERROR:", e)

        except Exception as e:
            print("WEBSOCKET ERROR:", e)
            print("Reconnecting in 5 seconds...")
            time.sleep(5)


if __name__ == "__main__":
    print("======================================")
    print("     BASE QUID WALLET VOLUME TRACKER")
    print("======================================")
    print("Dune: DISABLED")
    print("Rule: PancakeSwap Swap + QUID")
    print("Output: Wallet + Total Volume USD")
    print()

    listen()
