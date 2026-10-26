from decimal import Decimal
import logging
from web3 import Web3
from bot.fmt import fmt_weth, fmt_usdc, wei_to_eth

ERC20_ABI = [
    {"name": "balanceOf","inputs": [{"name": "_owner", "type": "address"}],
     "outputs": [{"name": "balance", "type": "uint256"}],
     "stateMutability": "view","type": "function"},
    {"name": "decimals","inputs": [],"outputs": [{"name": "", "type": "uint8"}],
     "stateMutability": "view","type": "function"},
]

# Minimi-ABI WETH9:lle (deposit/withdraw riittävät)
WETH_ABI = [
    {
        "type": "function",
        "stateMutability": "payable",
        "name": "deposit",
        "inputs": [],
        "outputs": []
    },
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "withdraw",
        "inputs": [{"name": "wad", "type": "uint256"}],
        "outputs": []
    },
    # bonus: balanceOf jotta voidaan lukea saldot samasta instanssista, jos halutaan
    {
        "name": "balanceOf",
        "inputs": [{"name": "_owner", "type": "address"}],
        "outputs": [{"name": "balance", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

class WalletManager:
    def __init__(self, w3, account, weth_addr, usdc_addr, router_addr,
                 usdc_decimals=6,
                 min_eth_gas_buffer_wei=int(0.02 * 10**18),
                 eth_topup_target_wei=int(0.05 * 10**18),
                 wrap_excess_eth_when_flat=True,
                 rebalance_cooldown_seconds=300):
        self.w3 = w3
        self.account = account
        self.weth = Web3.to_checksum_address(weth_addr)
        self.usdc = Web3.to_checksum_address(usdc_addr)
        self.router = Web3.to_checksum_address(router_addr)
        self.usdc_decimals = int(usdc_decimals)
        self.min_eth_gas_buffer = int(min_eth_gas_buffer_wei)
        self.eth_topup_target = int(eth_topup_target_wei)
        self.wrap_excess_eth_when_flat = wrap_excess_eth_when_flat
        self.rebalance_cooldown_seconds = int(rebalance_cooldown_seconds)
        self._last_rebalance_ts = 0
        self.log = logging.getLogger("wallet")

        # WETH-sopimuksen instanssi wrap/unwrapia varten
        self._weth_c = self.w3.eth.contract(address=self.weth, abi=WETH_ABI)

    # ---------- helpers ----------

    def _erc20_balance(self, token_addr):
        c = self.w3.eth.contract(address=token_addr, abi=ERC20_ABI)
        return int(c.functions.balanceOf(self.account.address).call())

    def _eip1559_fees(self) -> tuple[int, int]:
        """
        Perus EIP-1559 fee builder: tip ~2 gwei; maxFee ~ base*1.2 + tip.
        Ei yritä olla markkina-älykäs – vain turvallinen oletus.
        """
        block = self.w3.eth.get_block("latest")
        base = int(block.get("baseFeePerGas", self.w3.eth.gas_price))
        tip = int(2e9)  # 2 gwei
        if base < tip:  # L2: varmistetaan ettei max_fee < 2*tip
            base = tip * 2
        max_fee = int(base * 1.2) + tip
        return max_fee, tip

    def _apply_gas_with_buffer(self, tx: dict, default_gas: int = 120_000, buf_pct: float = 20.0) -> dict:
        """
        Estimate + buffer. Jos estimate epäonnistuu, käytetään default_gas.
        """
        try:
            est = int(self.w3.eth.estimate_gas(tx))
            tx["gas"] = int(est * (1 + buf_pct / 100))
            self.log.info(f"[GAS] estimate={est} buffered={tx['gas']}")
        except Exception as e:
            tx["gas"] = int(default_gas)
            self.log.warning(f"[GAS] estimate failed ({type(e).__name__}: {e}) → using default {default_gas}")
        return tx

    # ---------- wrap / unwrap ----------

    def wrap_eth(self, amount_wei: int) -> str | None:
        """
        ETH → WETH (deposit). amount_wei on Wei.
        Palauttaa tx-hashin hex-muodossa tai None jos jäi tekemättä.
        """
        amt = int(max(0, amount_wei))
        if amt <= 0:
            return None

        max_fee, tip = self._eip1559_fees()
        tx = self._weth_c.functions.deposit().build_transaction({
            "from": self.account.address,
            "nonce": self.w3.eth.get_transaction_count(self.account.address, "pending"),
            "chainId": self.w3.eth.chain_id,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": tip,
            "value": amt,
        })
        tx = self._apply_gas_with_buffer(tx, default_gas=90_000)

        self.log.info(f"[WRAP] deposit {wei_to_eth(amt)} → WETH")
        signed = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        try:
            self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        except Exception as e:
            self.log.warning(f"[WRAP] wait_for_receipt timeout/err: {type(e).__name__}: {e}")
        return self.w3.to_hex(tx_hash)

    def unwrap_weth(self, amount_wei: int) -> str | None:
        """
        WETH → ETH (withdraw). amount_wei on Wei.
        Palauttaa tx-hashin hex-muodossa tai None jos jäi tekemättä.
        """
        amt = int(max(0, amount_wei))
        if amt <= 0:
            return None

        max_fee, tip = self._eip1559_fees()
        tx = self._weth_c.functions.withdraw(amt).build_transaction({
            "from": self.account.address,
            "nonce": self.w3.eth.get_transaction_count(self.account.address, "pending"),
            "chainId": self.w3.eth.chain_id,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": tip,
            "value": 0,
        })
        tx = self._apply_gas_with_buffer(tx, default_gas=110_000)

        self.log.info(f"[UNWRAP] withdraw {fmt_weth(amt)} → ETH")
        signed = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        try:
            self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        except Exception as e:
            self.log.warning(f"[UNWRAP] wait_for_receipt timeout/err: {type(e).__name__}: {e}")
        return self.w3.to_hex(tx_hash)

    # ---------- snapshots / policy ----------

    def snapshot_usd_estimate(self, price_usdc_per_weth: float):
        eth_bal = self.w3.eth.get_balance(self.account.address)
        weth_bal = self._erc20_balance(self.weth)
        usdc_bal = self._erc20_balance(self.usdc)

        usdc_val = Decimal(usdc_bal) / (Decimal(10) ** self.usdc_decimals)
        weth_val = (Decimal(weth_bal) / Decimal(10**18)) * Decimal(price_usdc_per_weth)
        eth_val = (Decimal(eth_bal) / Decimal(10**18)) * Decimal(price_usdc_per_weth)
        total_usd = usdc_val + weth_val + eth_val

        self.log.info(
            f"[SNAP] {fmt_weth(weth_bal)}, {fmt_usdc(usdc_bal, self.usdc_decimals)}, est_total_usd≈{float(total_usd):.2f}")
        return dict(eth=eth_bal, weth=weth_bal, usdc=usdc_bal,
                    est_total_usd=float(total_usd))

    # ---------- rebalancing ----------

    def rebalance_if_flat(self, state, executor, pricefeed):
        import time
        now = time.time()
        if now - self._last_rebalance_ts < self.rebalance_cooldown_seconds:
            return
        if state.holding:
            return

        eth_bal = self.w3.eth.get_balance(self.account.address)
        weth_bal = self._erc20_balance(self.weth)

        if getattr(state, "holding", False):
            logging.getLogger("wallet").info("[REBAL] holding=True → skip")
            self._last_rebalance_ts = now
            return

        logging.getLogger("wallet").info("[REBAL] flat → evaluate WETH→USDC & wrap/unwrap policy")

        # 1) varmista että kaasupuskuri on riittävä (unwrap WETH → ETH)
        if eth_bal < self.min_eth_gas_buffer and weth_bal > 0:
            need = max(0, self.eth_topup_target - eth_bal)
            amt = min(weth_bal, need)
            if amt > 0:
                self.log.info(f"[WALLET] unwrap {fmt_weth(amt)} → ETH (gas buffer top-up)")
                try:
                    self.unwrap_weth(amt)
                except Exception as e:
                    self.log.error(f"[WALLET] unwrap failed: {type(e).__name__}: {e}")

        # 2) jos flat, myy kaikki WETH → USDC
        if weth_bal > 0:
            min_usdc, exp_usdc, fee = executor.quote_weth_to_usdc_min(weth_bal, 0.005)
            res = executor.swap_weth_for_usdc(self.account, weth_bal, min_usdc)
            self.log.info(f"[WALLET] auto-sell {fmt_weth(weth_bal)} → {fmt_usdc(res.amount_out, self.usdc_decimals)}")

        # 3) wrappaa ylimääräinen ETH (ETH → WETH), jos niin halutaan
        eth_bal = self.w3.eth.get_balance(self.account.address)  # päivitä mahdollisen unwrapin/myynnin jälkeen
        if self.wrap_excess_eth_when_flat and eth_bal > self.eth_topup_target * 2:
            excess = eth_bal - self.eth_topup_target
            if excess > 0:
                self.log.info(f"[WALLET] wrap {wei_to_eth(excess)} ETH → WETH")
                try:
                    self.wrap_eth(excess)
                except Exception as e:
                    self.log.error(f"[WALLET] wrap failed: {type(e).__name__}: {e}")

        self._last_rebalance_ts = now
