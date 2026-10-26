# CryptoBot — README

This README documents the bot's current features and the latest additions:
**Bookkeeper accounting**, **asynchronous gas receipt updates**, **PnL calculation
with gas costs**, and **dual re-entry** logic. Practical examples of `ledger.jsonl`
entries and setup tips are included.

---

## Quick Start

1. **Requirements**
   - Python 3.10+
   - `web3`, `eth_account`, `decimal`, etc. (see `requirements.txt`)
2. **Configuration**
   - Copy `malli_setings.json` → your own `settings.json` (path is resolved
     automatically from `DATA_DIR`).
   - Choose `profiles` → `network_profile` (e.g. `sepolia` or `mainnet`) and
     adjust the trader parameters.
3. **Environment variable**
   - Set your private key (the `0x` prefix is optional):
     ```bash
     export BOT_PRIVATE_KEY='0xyour_private_key'
     ```
4. **Run the bot**
   ```bash
   python -m bot.main
   ```

---

## Profiles, Network, and State

- `settings.json` manages the active profile (`profiles.*`) and the RPC endpoint.
- **Paper vs. Live:** `testrun=true` → paper mode (no on-chain transactions),
  `testrun=false` → live mode.
- **State:** minimal runtime state is persisted in `state.json` (path from
  `ETH_STATE_FILE` in settings). It holds the current position, the most recent
  entry/exit prices, and the `rolling_high` value used by dual re-entry logic.

---

## New Features (TL;DR)

### 1) Bookkeeper + `ledger.jsonl`

Every trade is recorded to `ledger.jsonl` / `ledger.json` (path from `LEDGER_FILE`
in settings).

**Recorded fields:**
- `ts`, `side` (`BUY`/`SELL`), `token_in`, `token_out`
- `amount_in`, `amount_out` (always in *base units*: USDC 6 decimals, WETH in wei)
- `price` (USDC per 1 WETH — **fill price** derived from actual amounts)
- `tx_hash`
- **Gas metadata:** `gas_used`, `gas_price_gwei`, `gas_token`, `gas_cost_token`,
  `gas_cost_amount` (in token units, typically ETH)
- `notes` (diagnostic info, trigger reason, etc.)

**Example (JSONL row):**
```json
{"ts": 1714900000.0, "side": "SELL", "token_in": "WETH", "token_out": "USDC",
 "amount_in": "4258430093396593", "amount_out": "49501560", "price": "11624.368350",
 "tx_hash": "0x0853...13c2",
 "gas_used": 114881, "gas_price_gwei": "2.001055103",
 "gas_token": "ETH", "gas_cost_token": "ETH", "gas_cost_amount": "0.000229",
 "notes": "sl_hit pnl_usdc=-? pnl_pct=-1.17%"}
```

> **Note:** If you use **JSON** format (`.json` instead of `.jsonl`), `Bookkeeper`
> rewrites the entire structure `{"trades":[...]}` on every update.

### 2) Asynchronous Gas Receipt Updates

- After every on-chain tx, `_queue_gas_update(tx_hash)` is called.
- `_pump_gas_updates()` (called inside `build()`) runs **on every tick**, fetches the
  receipt via `get_transaction_receipt`, and updates the ledger with:
  - `gas_used`, `effectiveGasPrice` → `gas_price_gwei`
  - `gas_cost_amount` calculated precisely in `gas_token` units (ETH)
- This eliminates the need to block the main loop while waiting for receipt
  confirmation.

**Handling known version differences:**
- Web3 v5/v6 differences in receipt field names are normalised defensively
  (`gasUsed` / `gas_used`, `effectiveGasPrice` / `gasPrice`).

### 3) Realised PnL with Gas Costs

`Bookkeeper.realized_pnl(fx_func)` performs FIFO lot matching:

- **BUY (USDC→WETH):** creates a lot (`qty=amount_out`, `cost_per_unit=amount_in/amount_out`).
- **SELL (WETH→USDC):** unwinds lots FIFO and calculates `proceeds - matched_cost`.
- **Gas** is deducted from PnL; if gas is denominated in a currency other than
  `report_ccy`, `fx_func(from, to)` is used for conversion.

Default reporting currency is `USDC`, but this can be changed in the class constructor.

### 4) Dual Re-entry: Pullback + Breakout + Ladder

`trading.reentry_mode="dual"` activates combined logic:

- **Pullback:** buy when price drops ≥ `pullback_pct` from `rolling_high`.
- **Breakout:** buy when price rises ≥ `breakout_pct` from `last_sell_price`.
- **Ladder:** define steps via `ladder_levels_pct` + corresponding `ladder_sizes`
  (e.g. 40% + 30% + 30%).
- `rolling_high` updates in **FLAT** state (no positions); it is not raised while
  a long position is open.

**Example configuration:**
```json
"trading": {
  "reentry_mode": "dual",
  "dual_reentry": {
    "pullback_pct": "0.04",
    "breakout_pct": "0.015",
    "ladder_levels_pct": ["0.015","0.03","0.045"],
    "ladder_sizes": [0.4, 0.3, 0.3],
    "breakout_initial_allocation_pct": "0.5"
  }
}
```

### 5) Price Hygiene and Transparency

- **Fill price** is always derived from actual executed amounts:
  - BUY: `fill = paid_USDC / received_WETH`
  - SELL: `fill = received_USDC / sent_WETH`
- **Quote vs. Fill — price impact (bps)** is logged for every trade.

### 6) Wallet Manager

- Monitors `min_eth_gas_buffer_wei`, `eth_topup_target_wei`, and the
  `wrap_excess_eth_when_flat` policy.
- Performs automatic rebalancing only in **FLAT** state during the cooldown period.

---

## Log Levels and Debug Flags

Set in `settings.json` → `debug`:

- `trade_decisions` — prints decision logic (TP/SL comparisons).
- `reentry` — shows dual trigger threshold prices and hits.
- `ladder_hits` — logs ladder steps hit during pullback.

---

## Key Paths and Definitions

- **`ETH_STATE_FILE`** — path to `state.json`.
- **`LEDGER_FILE`** — path to `ledger.jsonl` / `ledger.json`.
- **`W3_CLIENT_URL`, `ETH_CHAIN_ID`** — RPC endpoint and chain ID from the active profile.
- **`UNISWAP_V3_FEE_TIERS`** — fee tiers used for the quoter and swaps.

---

## Known Nuances

- **Receipt delay:** gas fields appear in the ledger in the **next** tick or a few
  ticks later, once the receipt is available.
- **Types and IDE warnings:** Web3 receipt structures vary between versions; the code
  uses defensive lookups and you may see IDE warnings (e.g. TypedDict keys) in some
  places — functionality does not depend on them.
- **USDC decimals:** fetched from the chain at startup; falls back to 6 if the call fails.

---

## Developer Checklist

- Call `_queue_gas_update(tx_hash)` after **every** on-chain trade (BUY/SELL) when
  `executor.paper == False`.
- Run `_pump_gas_updates()` at the **start of each tick** — do not block the main
  loop waiting for a receipt.
- Record the **fill price** from *actual executed amounts*, not from quote values.
- Keep `state.last_buy_price`, `state.last_spent_usdc`, and `state.holding_weth`
  up to date on BUY; reset/clear them on SELL and update `rolling_high` when
  transitioning to **FLAT** state.

---

## PnL Example (FIFO + Gas)

1. **BUY USDC→WETH:** creates a lot (`qty`, `cost_per_unit`); gas is deducted in the
   reporting currency.
2. **SELL WETH→USDC:** realised PnL = `proceeds - matched_cost - gas_in_report_ccy`.
3. `Bookkeeper.realized_pnl(fx_func)` returns:
   ```json
   {"report_currency": "USDC", "realized_pnl": "12.34", "num_trades": 4}
   ```

`fx_func` uses the same `V3PriceFeed` price for USDC↔WETH/ETH conversions
(note: other token pairs return a 1:1 ratio).

---

## Tools

### `tools/transfer_eth_arbitrum.py` — Cold Wallet Withdrawal

Transfers ETH (native) and/or WETH (ERC-20) from the bot wallet to a cold wallet
(e.g. Trezor/Ledger) on Arbitrum One. Intended for shutting down the bot and
moving funds without requiring a browser-based wallet.

**Requirements:**
```bash
pip install web3 python-dotenv
```

**`.env` file** (place in project root or alongside the script):
```
PRIVATE_KEY=0x<bot_wallet_private_key>
TO_ADDRESS=0x<cold_wallet_address>
RPC_URL=https://arb1.arbitrum.io/rpc   # optional — this is the default
```

**Usage:**
```bash
# Dry-run (default) — inspect amounts and gas estimates, nothing is sent:
python tools/transfer_eth_arbitrum.py

# Transfer everything (WETH first, then ETH):
python tools/transfer_eth_arbitrum.py --execute

# Transfer only native ETH:
python tools/transfer_eth_arbitrum.py --execute --eth-only

# Transfer only WETH:
python tools/transfer_eth_arbitrum.py --execute --weth-only
```

**Example output (`--execute`):**
```
============================================================
  Network     : Arbitrum One (chain ID 42161)
  From        : 0x<bot_wallet>        (PRIVATE_KEY from .env)
  To          : 0x<cold_wallet>       (TO_ADDRESS from .env)
============================================================
── WETH (ERC-20) ─────────────────────────────────────
  Contract    : 0x82aF49447D8a07e3bd95BD0d56f35241523fBab1
  Balance     : 0.00123400 WETH
  Gas estimate: 35821  (limit with buffer: 44776)
  Send amount : 0.00123400 WETH  (full balance)
  TX hash     : 0x...ea1fd4af4278db99c7403edaaa4d56b7fe0c7ae49792282931
  Explorer    : https://arbiscan.io/tx/0x...
  Waiting for confirmation…
  ✓ Confirmed in block 12347715
  Gas used    : 35821  (cost: 0.00000072 ETH)
── ETH (native) ─────────────────────────────────────
  Balance     : 0.00412000 ETH
  Gas estimate: 58000  (limit with buffer: 72500)
  Gas price   : base 0.0200 gwei  |  max 0.1401 gwei
  Max gas cost: 0.00001015 ETH
  Send amount : 0.00410985 ETH
  TX hash     : 0x...4372ebe74340779ff981ee862f9527f45ba4e362adffbd
  Explorer    : https://arbiscan.io/tx/0x...
  Waiting for confirmation…
  ✓ Confirmed in block 12347723
  Gas used    : 57200  (cost: 0.00000801 ETH)
============================================================
```

**Notes:**
- Gas limits are estimated dynamically via `estimate_gas()` with a 25 % buffer —
  Arbitrum's L2 overhead makes static limits unreliable.
- WETH is always transferred **before** ETH so that ETH remains available to pay
  for the ERC-20 transfer gas.
- Never commit `.env` to version control. Add it to `.gitignore`.

---

## Troubleshooting

- **RPC chain ID mismatch:** verify that `network_profile` and `W3_CLIENT_URL` point
  to the same network.
- **Allowance / approve pending:** the bot attempts a pre-approval at startup and also
  performs on-demand approval before each swap if needed.
- **Balance clamp:** buys are clamped to the on-chain USDC balance to prevent failed
  transactions.

---

## License

MIT License

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
