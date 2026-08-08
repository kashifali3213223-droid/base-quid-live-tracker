import os
import json
import time
import websocket

ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY")

if not ALCHEMY_API_KEY:
    raise RuntimeError("ALCHEMY_API_KEY is missing")

WS_URL = f"wss://base-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}"

QUID_POOL = "0x07c4bc0f5fb6cb069124df3e1ae0b8fd8148ccc4"
SWAP_TOPIC = "0x19b47279256b2a23a1665c810c8d55a1758940ee09377d4f8d26497a3577dc83"

USDC_DECIMALS = 6

total_swaps = 0
total_volume_usd = 0.0


def decode_int256(value):
    number = int(value, 16)

    if number >= 2**255:
        number -= 2**256

    return number


def handle_swap(log):
    global total_swaps, total_volume_usd

    data = log["data"][2:]

    # PancakeSwap V3 Swap event:
    # amount0, amount1, sqrtPriceX96, liquidity, tick,
    # protocolFeesToken0, protocolFeesToken1

    amount0 = decode_int256(data[0:64])
    amount1 = decode_int256(data[64:128])

    # Pool token1 is USDC, so amount1 gives USDC volume.
    volume_usd = abs(amount1) / (10 ** USDC_DECIMALS)

    total_swaps += 1
    total_volume_usd += volume_usd

    block_number = int(log["blockNumber"], 16)
    tx_hash = log["transactionHash"]

    print()
    print("🔥 QUID SWAP DETECTED")
    print("--------------------------------------")
    print("Block:", block_number)
    print("TX:", tx_hash)
    print("QUID amount:", abs(amount0) / 10**18)
    print("USDC volume: $", f"{volume_usd:,.6f}")
    print("TOTAL SWAPS:", total_swaps)
    print("TOTAL VOLUME: $", f"{total_volume_usd:,.6f}")
    print("--------------------------------------")


def listen():
    while True:
        try:
            print("Connecting to Base via Alchemy WebSocket...")

            ws = websocket.create_connection(
                WS_URL,
                timeout=30
            )

            subscribe_message = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_subscribe",
                "params": [
                    "logs",
                    {
                        "address": QUID_POOL,
                        "topics": [SWAP_TOPIC]
                    }
                ]
            }

            ws.send(json.dumps(subscribe_message))

            response = json.loads(ws.recv())

            print("Subscription:", response)
            print("🔥 Listening for QUID swaps...")
            print()

            while True:
                message = ws.recv()

                if not message:
                    continue

                data = json.loads(message)

                if data.get("method") != "eth_subscription":
                    continue

                result = data.get("params", {}).get("result")

                if result:
                    handle_swap(result)

        except Exception as e:
            print("WebSocket error:", e)
            print("Reconnecting in 5 seconds...")
            time.sleep(5)


if __name__ == "__main__":
    print("======================================")
    print("       BASE QUID LIVE VOLUME TRACKER")
    print("======================================")
    print("Pool:", QUID_POOL)
    print("Dune: DISABLED")
    print()

    listen()
    if "error" in result:
        raise Exception(result["error"])

    return result["result"]


def get_latest_block():
    return int(rpc("eth_blockNumber", []), 16)


def get_block(block_number):
    block_hex = hex(block_number)

    return rpc(
        "eth_getBlockByNumber",
        [block_hex, True]
    )


def get_receipt(tx_hash):
    return rpc(
        "eth_getTransactionReceipt",
        [tx_hash]
    )


def is_transfer_to_token(log, token):
    return (
        log.get("address", "").lower() == token
        and len(log.get("topics", [])) >= 3
        and log["topics"][0].lower() == TRANSFER_TOPIC
    )


def check_transaction(tx):
    tx_hash = tx["hash"]

    # Only PancakeSwap Universal Router transactions
    if not tx.get("to"):
        return None

    if tx["to"].lower() != PANCAKE_ROUTER:
        return None

    receipt = get_receipt(tx_hash)

    if not receipt:
        return None

    # Failed transaction
    if receipt.get("status") != "0x1":
        return None

    logs = receipt.get("logs", [])

    quid_transfer = False
    usdc_transfer = False

    for log in logs:
        if is_transfer_to_token(log, QUID):
            quid_transfer = True

        if is_transfer_to_token(log, USDC):
            usdc_transfer = True

    # A QUID <-> USDC swap should contain both token transfers
    if quid_transfer and usdc_transfer:
        return tx_hash

    return None


def main():
    print("======================================")
    print("       BASE QUID LIVE TRACKER")
    print("======================================")
    print("QUID:", QUID)
    print("PancakeSwap Router:", PANCAKE_ROUTER)
    print("Listening for new Base blocks...")
    print()

    last_block = get_latest_block()
    total_count = 0

    while True:
        try:
            latest_block = get_latest_block()

            if latest_block > last_block:
                for block_number in range(last_block + 1, latest_block + 1):

                    block = get_block(block_number)

                    if not block:
                        continue

                    transactions = block.get("transactions", [])

                    for tx in transactions:
                        try:
                            result = check_transaction(tx)

                            if result:
                                total_count += 1

                                print()
                                print("🔥 QUID SWAP DETECTED")
                                print("Block:", block_number)
                                print("TX:", result)
                                print("TOTAL QUID SWAPS:", total_count)
                                print("--------------------------------------")

                        except Exception as e:
                            print("TX ERROR:", e)

                last_block = latest_block

            time.sleep(POLL_SECONDS)

        except Exception as e:
            print("RPC ERROR:", e)
            time.sleep(3)


if __name__ == "__main__":
    main()
