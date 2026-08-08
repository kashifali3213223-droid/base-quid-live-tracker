import json
import time
import urllib.request

RPC_URL = "https://mainnet.base.org"

QUID = "0x1a44233FAe8D50F1AeB3a5d58dd426ff4814Cb53".lower()
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913".lower()

PANCAKE_ROUTER = "0xd9c500dff816a1da21a48a732d349bf09dc9aeb".lower()

POLL_SECONDS = 1

# ERC-20 Transfer event
TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4"
    "a11628f55a4df523b3ef"
)


def rpc(method, params):
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params
    }).encode()

    request = urllib.request.Request(
        RPC_URL,
        data=payload,
        headers={"Content-Type": "application/json"}
    )

    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.loads(response.read().decode())

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
