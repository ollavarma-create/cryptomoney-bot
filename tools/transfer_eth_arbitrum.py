"""
transfer_eth_arbitrum.py
------------------------
Transfer ETH (native) and/or WETH (ERC-20) on Arbitrum One to a cold wallet.

Usage:
    # Dry-run (default) — shows what would happen, sends nothing:
    python transfer_eth_arbitrum.py

    # Live transfer of everything (ETH + WETH):
    python transfer_eth_arbitrum.py --execute

    # Transfer only ETH:
    python transfer_eth_arbitrum.py --execute --eth-only

    # Transfer only WETH:
    python transfer_eth_arbitrum.py --execute --weth-only

Requirements:
    pip install web3 python-dotenv

.env file (create in same directory):
    PRIVATE_KEY=0xyour_private_key_here
    TO_ADDRESS=0xyour_cold_wallet_address
    RPC_URL=https://arb1.arbitrum.io/rpc     # optional, this is the default
"""

import argparse
import os
import sys
from decimal import Decimal

from dotenv import load_dotenv
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ARBITRUM_ONE_CHAIN_ID = 42161
DEFAULT_RPC_URL = "https://arb1.arbitrum.io/rpc"

# WETH contract on Arbitrum One (canonical, verified on Arbiscan)
WETH_CONTRACT_ADDRESS = Web3.to_checksum_address("0x82aF49447D8a07e3bd95BD0d56f35241523fBab1")

# Multiplier applied on top of estimate_gas() result to add headroom.
# Arbitrum L2 overhead makes static limits unreliable, so we always estimate dynamically.
GAS_ESTIMATE_BUFFER = 1.25

# Minimal ERC-20 ABI — only the functions we actually call
ERC20_ABI = [
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "transfer",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "recipient", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "decimals",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
    },
    {
        "name": "symbol",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "string"}],
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Load and validate configuration from environment / .env file."""
    load_dotenv()

    private_key = os.getenv("PRIVATE_KEY", "").strip()
    to_address_raw = os.getenv("TO_ADDRESS", "").strip()
    rpc_url = os.getenv("RPC_URL", DEFAULT_RPC_URL).strip()

    errors = []
    if not private_key:
        errors.append("PRIVATE_KEY is missing from .env")
    if not to_address_raw:
        errors.append("TO_ADDRESS is missing from .env")
    if errors:
        for e in errors:
            print(f"[ERROR] {e}")
        sys.exit(1)

    try:
        to_address = Web3.to_checksum_address(to_address_raw)
    except ValueError:
        print(f"[ERROR] TO_ADDRESS is not a valid Ethereum address: {to_address_raw!r}")
        sys.exit(1)

    return {"private_key": private_key, "to_address": to_address, "rpc_url": rpc_url}


def connect(rpc_url: str) -> Web3:
    """Create and verify a Web3 connection to Arbitrum One."""
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    if not w3.is_connected():
        print(f"[ERROR] Cannot connect to RPC: {rpc_url}")
        sys.exit(1)

    chain_id = w3.eth.chain_id
    if chain_id != ARBITRUM_ONE_CHAIN_ID:
        print(
            f"[ERROR] Wrong network! Expected Arbitrum One (chain ID {ARBITRUM_ONE_CHAIN_ID}), "
            f"got chain ID {chain_id}."
        )
        sys.exit(1)

    return w3


def wei_to_eth(wei: int, decimals: int = 18) -> Decimal:
    return Decimal(wei) / Decimal(10 ** decimals)


def get_gas_params(w3: Web3) -> dict:
    """Fetch current EIP-1559 gas parameters (with legacy fallback)."""
    latest_block = w3.eth.get_block("latest")
    base_fee = latest_block.get("baseFeePerGas")

    if base_fee is not None:
        max_priority_fee = w3.to_wei("0.1", "gwei")
        max_fee_per_gas = base_fee * 2 + max_priority_fee
        return {
            "eip1559": True,
            "maxFeePerGas": max_fee_per_gas,
            "maxPriorityFeePerGas": max_priority_fee,
            "base_fee": base_fee,
        }
    else:
        gas_price = w3.eth.gas_price
        return {"eip1559": False, "gasPrice": gas_price}


def print_header(from_address: str, to_address: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  Network     : Arbitrum One (chain ID {ARBITRUM_ONE_CHAIN_ID})")
    print(f"  From        : {from_address}")
    print(f"  To          : {to_address}")
    print(f"{'=' * 60}")


def build_base_tx(w3: Web3, nonce: int, gas_limit: int, gas_params: dict) -> dict:
    """Build the common fields shared by both ETH and ERC-20 transactions."""
    tx: dict = {
        "chainId": ARBITRUM_ONE_CHAIN_ID,
        "nonce": nonce,
        "gas": gas_limit,
    }
    if gas_params["eip1559"]:
        tx["maxFeePerGas"] = gas_params["maxFeePerGas"]
        tx["maxPriorityFeePerGas"] = gas_params["maxPriorityFeePerGas"]
    else:
        tx["gasPrice"] = gas_params["gasPrice"]
    return tx


def wait_and_report(w3: Web3, tx_hash: bytes, gas_params: dict) -> None:
    """Wait for a transaction receipt and print the outcome."""
    print(f"  TX hash     : {tx_hash.hex()}")
    print(f"  Explorer    : https://arbiscan.io/tx/{tx_hash.hex()}")
    print("  Waiting for confirmation…")

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    if receipt["status"] == 1:
        effective_price = receipt.get(
            "effectiveGasPrice",
            gas_params.get("maxFeePerGas") or gas_params.get("gasPrice"),
        )
        actual_cost = wei_to_eth(receipt["gasUsed"] * effective_price)
        print(f"  ✓ Confirmed in block {receipt['blockNumber']}")
        print(f"  Gas used    : {receipt['gasUsed']}  (cost: {actual_cost:.8f} ETH)")
    else:
        print("  ✗ Transaction FAILED — check Arbiscan for details.")


# ---------------------------------------------------------------------------
# ETH transfer
# ---------------------------------------------------------------------------

def transfer_eth(
    w3: Web3,
    account,
    to_address: str,
    gas_params: dict,
    dry_run: bool,
) -> bool:
    """
    Transfer all available ETH (minus gas cost) to to_address.
    Returns True if there was a non-zero balance to work with.
    """
    print("\n── ETH (native) ─────────────────────────────────────")

    balance_wei = w3.eth.get_balance(account.address)
    balance_eth = wei_to_eth(balance_wei)
    print(f"  Balance     : {balance_eth:.8f} ETH")

    if balance_wei == 0:
        print("  [SKIP] Wallet holds no ETH.")
        return False

    max_fee = gas_params.get("maxFeePerGas") or gas_params.get("gasPrice")

    # Estimate gas using the full balance as a placeholder value.
    # The estimate will be slightly off because the final send amount is balance - gas_cost,
    # but for a plain transfer the estimate is stable regardless of value.
    raw_estimate = w3.eth.estimate_gas({
        "from": account.address,
        "to": to_address,
        "value": balance_wei,
    })
    gas_limit = int(raw_estimate * GAS_ESTIMATE_BUFFER)
    gas_cost_wei = gas_limit * max_fee
    send_wei = balance_wei - gas_cost_wei

    print(f"  Gas estimate: {raw_estimate}  (limit with buffer: {gas_limit})")

    if send_wei <= 0:
        print(
            f"  [SKIP] Balance ({balance_eth:.8f} ETH) is too low to cover "
            f"gas ({wei_to_eth(gas_cost_wei):.8f} ETH)."
        )
        return False

    send_eth = wei_to_eth(send_wei)
    if gas_params["eip1559"]:
        gwei_display = (
            f"base {wei_to_eth(gas_params['base_fee']) * 10**9:.4f} gwei  |  "
            f"max {wei_to_eth(max_fee) * 10**9:.4f} gwei"
        )
    else:
        gwei_display = f"{wei_to_eth(max_fee) * 10**9:.4f} gwei (legacy)"

    print(f"  Gas price   : {gwei_display}")
    print(f"  Max gas cost: {wei_to_eth(gas_cost_wei):.8f} ETH")
    print(f"  Send amount : {send_eth:.8f} ETH")

    if dry_run:
        print("  [DRY-RUN] Would send — use --execute to broadcast.")
        return True

    nonce = w3.eth.get_transaction_count(account.address, "pending")
    tx = build_base_tx(w3, nonce, gas_limit, gas_params)
    tx["to"] = to_address
    tx["value"] = send_wei

    signed = w3.eth.account.sign_transaction(tx, account.key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    wait_and_report(w3, tx_hash, gas_params)
    return True


# ---------------------------------------------------------------------------
# WETH (ERC-20) transfer
# ---------------------------------------------------------------------------

def transfer_weth(
    w3: Web3,
    account,
    to_address: str,
    gas_params: dict,
    dry_run: bool,
) -> bool:
    """
    Transfer entire WETH balance to to_address.
    Returns True if there was a non-zero balance to work with.
    """
    print("\n── WETH (ERC-20) ─────────────────────────────────────")
    print(f"  Contract    : {WETH_CONTRACT_ADDRESS}")

    contract = w3.eth.contract(address=WETH_CONTRACT_ADDRESS, abi=ERC20_ABI)

    decimals = contract.functions.decimals().call()
    symbol = contract.functions.symbol().call()
    balance_raw = contract.functions.balanceOf(account.address).call()
    balance = wei_to_eth(balance_raw, decimals)

    print(f"  Balance     : {balance:.8f} {symbol}")

    if balance_raw == 0:
        print("  [SKIP] Wallet holds no WETH.")
        return False

    # Verify source wallet has enough ETH to pay for the ERC-20 transfer gas.
    # We estimate against the actual contract call for accuracy.
    max_fee = gas_params.get("maxFeePerGas") or gas_params.get("gasPrice")
    eth_balance_wei = w3.eth.get_balance(account.address)

    raw_estimate = w3.eth.estimate_gas({
        "from": account.address,
        "to": WETH_CONTRACT_ADDRESS,
        "value": 0,
        "data": contract.encode_abi("transfer", args=[to_address, balance_raw]),
    })
    gas_limit = int(raw_estimate * GAS_ESTIMATE_BUFFER)
    gas_cost_wei = gas_limit * max_fee

    print(f"  Gas estimate: {raw_estimate}  (limit with buffer: {gas_limit})")

    if eth_balance_wei < gas_cost_wei:
        print(
            f"  [ERROR] Not enough ETH for gas. Need ~{wei_to_eth(gas_cost_wei):.8f} ETH, "
            f"have {wei_to_eth(eth_balance_wei):.8f} ETH."
        )
        print("  Tip: transfer ETH first, then re-run with --weth-only.")
        return False

    print(f"  Send amount : {balance:.8f} {symbol}  (full balance)")

    if dry_run:
        print("  [DRY-RUN] Would send — use --execute to broadcast.")
        return True

    nonce = w3.eth.get_transaction_count(account.address, "pending")
    tx = build_base_tx(w3, nonce, gas_limit, gas_params)

    # ERC-20 transfer is a contract call — value is 0 ETH, payload carries the token amount
    tx["to"] = WETH_CONTRACT_ADDRESS
    tx["value"] = 0
    tx["data"] = contract.encode_abi("transfer", args=[to_address, balance_raw])

    signed = w3.eth.account.sign_transaction(tx, account.key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    wait_and_report(w3, tx_hash, gas_params)
    return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Transfer ETH and/or WETH to a cold wallet on Arbitrum One."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually broadcast transactions (default: dry-run only)",
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--eth-only", action="store_true", help="Transfer only native ETH")
    mode.add_argument("--weth-only", action="store_true", help="Transfer only WETH")

    args = parser.parse_args()
    dry_run = not args.execute

    if dry_run:
        print("\n*** DRY-RUN MODE — no transactions will be broadcast ***")
        print("*** Run with --execute to perform actual transfers.     ***")

    config = load_config()
    w3 = connect(config["rpc_url"])
    account = w3.eth.account.from_key(config["private_key"])
    gas_params = get_gas_params(w3)

    print_header(account.address, config["to_address"])

    transfer_eth_flag = not args.weth_only
    transfer_weth_flag = not args.eth_only

    # Transfer WETH first to preserve ETH balance for gas payments
    if transfer_weth_flag:
        transfer_weth(w3, account, config["to_address"], gas_params, dry_run)

    if transfer_eth_flag:
        transfer_eth(w3, account, config["to_address"], gas_params, dry_run)

    print(f"\n{'=' * 60}\n")


if __name__ == "__main__":
    main()