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
