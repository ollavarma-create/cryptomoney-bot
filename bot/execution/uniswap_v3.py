from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
import logging
from bot.log import enabled, rate_limited
from web3 import Web3
from web3.exceptions import (
    TimeExhausted,
    BadFunctionCallOutput,
    ContractLogicError,
    ABIFunctionNotFound,
    MismatchedABI,
)
from bot.fmt import (
    human_amount,
    fmt_token,
    fmt_weth,
    fmt_usdc,
    wei_to_eth,
    wei_to_gwei,
)

MAX_UINT = 2**256 - 1
PREFLIGHT_STRICT = True  # set True if you want preflight failures to abort

# -------------------------
# Minimal ABIs (Router02 / QuoterV2 / Factory / Pool / ERC20)
# -------------------------

# SwapRouter02 (NO deadline in exactInputSingle / exactInput)
V3_ROUTER02_ABI = [
    {
        "type": "function",
        "stateMutability": "payable",
        "name": "exactInputSingle",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "tokenIn", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "recipient", "type": "address"},
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "amountOutMinimum", "type": "uint256"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
            }
        ],
        "outputs": [{"name": "amountOut", "type": "uint256"}],
    },
    {
        "type": "function",
        "stateMutability": "payable",
        "name": "exactInput",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "path", "type": "bytes"},
                    {"name": "recipient", "type": "address"},
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "amountOutMinimum", "type": "uint256"},
                ],
            }
        ],
        "outputs": [{"name": "amountOut", "type": "uint256"}],
    },
    {
        "type": "function",
        "stateMutability": "view",
        "name": "factory",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "type": "function",
        "stateMutability": "view",
        "name": "WETH9",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
]

QUOTER_V2_ABI = [
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "quoteExactInput",
        "inputs": [
            {"name": "path", "type": "bytes"},
            {"name": "amountIn", "type": "uint256"},
        ],
        "outputs": [
            {"name": "amountOut", "type": "uint256"},
            {"name": "sqrtPriceX96AfterList", "type": "uint160[]"},
            {"name": "initializedTicksCrossedList", "type": "uint32[]"},
            {"name": "gasEstimate", "type": "uint256"},
        ],
    }
]

V3_FACTORY_ABI = [
    {
        "type": "function",
        "stateMutability": "view",
        "name": "getPool",
        "inputs": [
            {"name": "tokenA", "type": "address"},
            {"name": "tokenB", "type": "address"},
            {"name": "fee", "type": "uint24"},
        ],
        "outputs": [{"name": "pool", "type": "address"}],
    }
]

V3_POOL_MIN_ABI = [
    {
        "type": "function",
        "stateMutability": "view",
        "name": "liquidity",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint128"}],
    }
]

ERC20_ABI = [
    {
        "name": "approve",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "name": "allowance",
        "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
        "outputs": [{"name": "remaining", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "name": "balanceOf",
        "inputs": [{"name": "_owner", "type": "address"}],
        "outputs": [{"name": "balance", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "name": "decimals",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "name": "symbol",
        "inputs": [],
        "outputs": [{"name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "name": "transferFrom",
        "inputs": [
            {"name": "from", "type": "address"},
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "name": "name",
        "inputs": [],
        "outputs": [{"name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    },
]

# Optional Circle-style check; cheap and sometimes helpful
USDC_BLACKLIST_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "addr", "type": "address"}],
        "name": "isBlacklisted",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
]


@dataclass
class TradeResult:
    tx_hash: str | None
    amount_out: int
    # Lisätelemetriaa: mitä min_outia ja fee-tieriä todella käytettiin tx:ssä
    used_min_out: int = 0
    used_fee: int | None = None
    # Kaasut: täytetään, jos kuitti saadaan tässä funktiossa
    gas_used: int | None = None
    gas_price_gwei: str | None = None  # Decimal string
    gas_token: str = "ETH"
    gas_cost_amount: str | None = None # ETH määränä (Decimal string)


class UniswapV3Executor:
    """
    Uniswap v3 executor using QuoterV2 + SwapRouter02 (exactInputSingle / exactInput).
    """

    def __init__(
        self,
        w3,
        router_v3: str,
        quoter_v2: str,
        weth: str,
        usdc: str,
        chain_id: int,
        fee_tiers: list[int],
        gas_mult: float,
        gas_buf_pct: float,
        rpc_timeout: int,
        paper: bool,
        logger=None,
    ):
        self.w3 = w3
        self.chain_id = int(chain_id)
        self.gas_mult = float(gas_mult)
        self.gas_buf_pct = float(gas_buf_pct)
        self.rpc_timeout = int(rpc_timeout)
        self.paper = bool(paper)

        self.logger = logger or logging.getLogger("executor.uniswap_v3")

        self.router_addr = Web3.to_checksum_address(router_v3)
        self.quoter_addr = Web3.to_checksum_address(quoter_v2)
        self.weth = Web3.to_checksum_address(weth)
        self.usdc = Web3.to_checksum_address(usdc)

        if enabled("executor"):
            self.logger.info(f"[EXECUTOR] router={self.router_addr} quoter={self.quoter_addr} weth={self.weth} usdc={self.usdc}")

        # Bind contracts
        self.router = self.w3.eth.contract(address=self.router_addr, abi=V3_ROUTER02_ABI)
        self.quoter = self.w3.eth.contract(address=self.quoter_addr, abi=QUOTER_V2_ABI)

        # Derive factory from router
        try:
            self.v3_factory_addr = Web3.to_checksum_address(self.router.functions.factory().call())
            self.router_weth_addr = Web3.to_checksum_address(self.router.functions.WETH9().call())
            if enabled("executor") and not rate_limited("executor.router"):
                self.logger.info(f"[ROUTER] factory={self.v3_factory_addr} WETH9={self.router_weth_addr}")

        except Exception as e:
            self.v3_factory_addr = None
            self.logger.warning(f"[ROUTER] unable to read factory/WETH9: {type(e).__name__}: {e}")

        self.fee_tiers = [int(x) for x in (fee_tiers or [3000])]
        self._pending_approvals = {}
        self._last_slippage = 0.005  # default for quote bookkeeping

    def _gas_meta_from_receipt(self, rcpt) -> tuple[int | None, str | None, str | None]:
        """
        Palauttaa (gas_used, gas_price_gwei_str, gas_cost_eth_str).
        Palauttaa None-arvot, jos kuittia ei ole tai kentät puuttuvat.
        """
        try:
            gas_used = int(getattr(rcpt, "gasUsed", None) or getattr(rcpt, "gas_used", None) or rcpt.get("gasUsed"))
            eff_wei = (getattr(rcpt, "effectiveGasPrice", None)
                       or getattr(rcpt, "effective_gas_price", None)
                       or rcpt.get("effectiveGasPrice")
                       or getattr(rcpt, "gasPrice", None)
                       or rcpt.get("gasPrice"))
            if gas_used is None or eff_wei is None:
                return None, None, None

            gwei = wei_to_gwei(eff_wei)
            eth_cost = str(wei_to_eth(Decimal(gas_used) * Decimal(eff_wei)))
            return gas_used, gwei, eth_cost
        except Exception:
            return None, None, None



    # -------------------------
    # Small utils / formatting
    # -------------------------

    def _erc20_meta(self, addr: str) -> tuple[str, int]:
        try:
            c = self.w3.eth.contract(address=Web3.to_checksum_address(addr), abi=ERC20_ABI)
            return c.functions.symbol().call(), int(c.functions.decimals().call())
        except (BadFunctionCallOutput, ContractLogicError, ValueError) as e:
            # fallback to known tokens
            if Web3.to_checksum_address(addr) == self.usdc:
                return "USDC", 6
            if Web3.to_checksum_address(addr) == self.weth:
                return "WETH", 18
            logging.getLogger("executor.uniswap_v3").warning(
                f"[META] fallback for addr={addr}: {type(e).__name__}: {e}"
            )
            return addr[:6], 18

    def _fmt_amount(self, raw: int, decimals: int) -> str:
        # yhtenäinen formatteri koko koodipohjassa
        return human_amount(raw, decimals, ndigits=9)

    def _log_exact_input_single_params(self, params: tuple):
        token_in, token_out, fee, recipient, amount_in, min_out, sqrt_limit = params
        sym_in, dec_in = self._erc20_meta(token_in)
        sym_out, dec_out = self._erc20_meta(token_out)
        self.logger.info(
            "[PARAMS] exactInputSingle("
            f"tokenIn={token_in}({sym_in}), tokenOut={token_out}({sym_out}), fee={fee}, "
            f"recipient={recipient}, amountIn={amount_in}({self._fmt_amount(amount_in, dec_in)} {sym_in}), "
            f"amountOutMinimum={min_out}({self._fmt_amount(min_out, dec_out)} {sym_out}), "
            f"sqrtPriceLimitX96={sqrt_limit})"
        )

    def calculate_dynamic_slippage(self, amount_in: int, decimals: int) -> Decimal:
        """
        Skaalaa määrän tokenin desimaalien mukaan ja tuottaa kokoluokkaan perustuvan slippagetoleranssin.
        """
        amount_dec = Decimal(amount_in) / (Decimal(10) ** Decimal(decimals))

        if amount_dec < Decimal("100"):  # < $100
            return Decimal("0.0050")  # 0.50 %
        elif amount_dec < Decimal("1000"):  # < $1,000
            return Decimal("0.0075")  # 0.75 %
        else:
            return Decimal("0.0100")  # 1.00 %

    def slippage_for_usdc(self, amount_usdc_base6: int) -> Decimal:
        return self.calculate_dynamic_slippage(amount_usdc_base6, 6)

    def slippage_for_weth(self, amount_weth_wei: int) -> Decimal:
        return self.calculate_dynamic_slippage(amount_weth_wei, 18)

    # -------------------------
    # On-chain helpers
    # -------------------------

    def _check_router_ready(self) -> bool:
        try:
            ra = Web3.to_checksum_address(self.router.address)
            code = self.w3.eth.get_code(ra)
            fnames = [f.get("name") for f in getattr(self.router, "abi", V3_ROUTER02_ABI) if "name" in f]
            if enabled("executor") and not rate_limited("executor.router"):
                self.logger.info(f"[ROUTER] addr={ra} code_len={len(code)} fns={fnames}")

        except (ValueError, ) as e:
            self.logger.error(f"[ROUTER CHECK] checksum/get_code failed: {type(e).__name__}: {e}")
            return False

        if len(code) == 0:
            self.logger.error("Router address has no code.")
            return False
        if "exactInputSingle" not in fnames:
            self.logger.error("ABI missing exactInputSingle(tuple).")
            return False
        return True

    def _encode_path(self, tokens: list[str], fees: list[int]) -> bytes:
        assert len(tokens) >= 2 and len(fees) == len(tokens) - 1
        def strip0x(a: str) -> str:
            a = a.lower()
            return a[2:] if a.startswith("0x") else a
        out = b""
        for i, fee in enumerate(fees):
            out += bytes.fromhex(strip0x(tokens[i])) + int(fee).to_bytes(3, "big")
        out += bytes.fromhex(strip0x(tokens[-1]))
        return out

    def _eip1559_fees(self) -> tuple[int, int]:
        block = self.w3.eth.get_block("latest")
        base = int(block.get("baseFeePerGas", self.w3.eth.gas_price))
        tip = int(2e9)  # 2 gwei
        # L2-ketjuilla baseFee voi olla hyvin matala; varmistetaan että max_fee >= 2*tip
        if base < tip:
            base = tip * 2
        max_fee = int(base * self.gas_mult) + tip
        return max_fee, tip

    def _apply_gas_or_force(self, tx: dict, default_gas: int = 500_000) -> dict:
        """
        Strict: jos gas-estimate epäonnistuu, aborttaa (älä pakota kiinteää gasia).
        Näin oikea revert-syy ei peity eikä TX:ää yritetä väkisin väärillä ehdoilla.
        """
        try:
            est = self.w3.eth.estimate_gas(tx)
        except (ContractLogicError, BadFunctionCallOutput, ValueError) as e:
            self.logger.error(f"[GAS] estimate failed: {type(e).__name__}: {e} → abort (no forced gas)")
            raise
        tx["gas"] = int(est * (1 + self.gas_buf_pct / 100))
        if enabled("executor") and not rate_limited("executor.gas"):
            self.logger.info(f"[GAS] estimate={est}, with buffer={tx['gas']}")
        return tx

    def _ensure_allowance(self, token: str, owner: str, spender: str, needed: int, account) -> bool:
        if self.paper:
            return True
        erc20 = self.w3.eth.contract(address=token, abi=ERC20_ABI)
        current = int(erc20.functions.allowance(owner, spender).call())
        if current >= needed:
            self._pending_approvals.pop((token, spender), None)
            return True

        pending = self._pending_approvals.get((token, spender))
        if pending:
            nonce = pending["nonce"]
            max_fee, tip = self._bump_fees(pending["max_fee"], pending["tip"])
            self.logger.warning(f"[APPROVE] RBF approve token={token} spender={spender} nonce={nonce}")
        else:
            nonce = self.w3.eth.get_transaction_count(owner, "pending")
            max_fee, tip = self._eip1559_fees()

        tx = erc20.functions.approve(spender, MAX_UINT).build_transaction(
            {
                "from": owner,
                "nonce": nonce,
                "chainId": self.chain_id,
                "maxFeePerGas": max_fee,
                "maxPriorityFeePerGas": tip,
            }
        )
        tx = self._apply_gas_or_force(tx, default_gas=80_000)
        signed = account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        self._pending_approvals[(token, spender)] = {"nonce": nonce, "max_fee": max_fee, "tip": tip, "tx_hash": tx_hash}

        try:
            self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=self.rpc_timeout)
            self.logger.info(f"[APPROVE] confirmed token={token} spender={spender} tx={tx_hash.hex()}")
            self._pending_approvals.pop((token, spender), None)
            return int(erc20.functions.allowance(owner, spender).call()) >= needed
        except TimeExhausted:
            self.logger.warning(f"[APPROVE] pending tx={tx_hash.hex()} > {self.rpc_timeout}s; retry next tick (RBF ok)")
            return False
        except Exception as e:
            self.logger.warning(f"[APPROVE] error: {type(e).__name__}: {e}; will retry next tick")
            return False

    def _bump_fees(self, max_fee: int, tip: int, factor: float = 1.25, add_tip_gwei: float = 1.0) -> tuple[int, int]:
        new_tip = tip + int(add_tip_gwei * 1e9)
        new_max = max(int(max_fee * factor), new_tip * 2)
        return new_max, new_tip

    # -------------------------
    # Safety checks
    # -------------------------

    def _pre_swap_policy_check(
            self,
            token_addr: str,
            owner_addr: str,
            router_addr: str,
            amount_probe: int = 1,
    ) -> None:
        token_addr = Web3.to_checksum_address(token_addr)
        owner_addr = Web3.to_checksum_address(owner_addr)
        router_addr = Web3.to_checksum_address(router_addr)

        erc = self.w3.eth.contract(address=token_addr, abi=ERC20_ABI)

        # --- Token symbol/name kapeilla excepteillä, järkevä fallback ---
        try:
            sym = erc.functions.symbol().call()
        except (BadFunctionCallOutput, ContractLogicError, ABIFunctionNotFound, MismatchedABI, ValueError) as e_sym:
            try:
                sym = erc.functions.name().call()
            except (
            BadFunctionCallOutput, ContractLogicError, ABIFunctionNotFound, MismatchedABI, ValueError) as e_name:
                self.logger.warning(
                    f"[POLICY] token meta fallback for {token_addr}: "
                    f"symbol()→{type(e_sym).__name__}, name()→{type(e_name).__name__}"
                )
                sym = token_addr[:6]

        if enabled("executor"):
            self.logger.info(f"[POLICY] {sym} checks owner={owner_addr} router={router_addr}")

        # --- Optional: blacklist-probe (best-effort; ohita jos ei tuettu) ---
        try:
            blc = self.w3.eth.contract(address=token_addr, abi=USDC_BLACKLIST_ABI)
            try:
                if blc.functions.isBlacklisted(owner_addr).call():
                    raise RuntimeError("owner blacklisted")
            except (BadFunctionCallOutput, ContractLogicError, ABIFunctionNotFound, MismatchedABI, ValueError):
                pass  # ei tuettu → ok

            try:
                if blc.functions.isBlacklisted(router_addr).call():
                    raise RuntimeError("router blacklisted")
            except (BadFunctionCallOutput, ContractLogicError, ABIFunctionNotFound, MismatchedABI, ValueError):
                pass  # ei tuettu → ok
        except (ValueError, MismatchedABI) as e:
            # outo ABI/osoite → debug riittää (probe on vain informatiivinen)
            self.logger.debug(f"[POLICY] blacklist probe skipped for {token_addr}: {type(e).__name__}: {e}")

        # --- transferFrom-simulaatio (spender=router) varmistaa peruskäyttäytymisen ---
        probe = max(int(amount_probe), 1)  # ei nollaa
        try:
            ok = erc.functions.transferFrom(owner_addr, router_addr, probe).call({"from": router_addr})
            if ok is False:
                raise RuntimeError("transferFrom(owner→router) simulation returned false (no revert)")
        except (ContractLogicError, BadFunctionCallOutput, ValueError) as e:
            raise RuntimeError("transferFrom(owner→router) simulation reverted") from e

    def _get_pool_and_liquidity(self, token_a: str, token_b: str, fee: int) -> tuple[str, int]:
        a = Web3.to_checksum_address(token_a)
        b = Web3.to_checksum_address(token_b)
        if a.lower() > b.lower():
            a, b = b, a

        faddr = self.v3_factory_addr
        if not faddr:
            return "0x0000000000000000000000000000000000000000", 0

        factory = self.w3.eth.contract(address=faddr, abi=V3_FACTORY_ABI)
        try:
            pool = Web3.to_checksum_address(factory.functions.getPool(a, b, int(fee)).call())
        except (BadFunctionCallOutput, ContractLogicError, ABIFunctionNotFound, MismatchedABI, ValueError) as e:
            self.logger.warning(
                f"[POOL] getPool({a},{b},{fee}) failed: {type(e).__name__}: {e}"
            )
            return "0x0000000000000000000000000000000000000000", 0

        code = self.w3.eth.get_code(pool)

        if not code:
            if enabled("executor") and not rate_limited("executor.pool"):
                self.logger.info(f"[POOL] no code at {pool} → liq=0")

        pool_c = self.w3.eth.contract(address=pool, abi=V3_POOL_MIN_ABI)
        try:
            liq = int(pool_c.functions.liquidity().call())
        except (BadFunctionCallOutput, ContractLogicError, ABIFunctionNotFound, MismatchedABI, ValueError) as e:
            if enabled("executor"):
                self.logger.debug(f"[POOL] liquidity() failed for {pool}: {type(e).__name__}: {e} → assume liq=0")
            liq = 0
        return pool, liq

    def _sanity_probe_router_call(self, account, token_in: str, token_out: str, fee: int, tiny_in: int = 1) -> None:
        """
        Minimal read-only probe of router.exactInputSingle with Router02 tuple (NO deadline).
        Raises with a clear message on revert.
        """
        params = (
            Web3.to_checksum_address(token_in),
            Web3.to_checksum_address(token_out),
            int(fee),
            Web3.to_checksum_address(account.address),
            int(max(1, tiny_in)),
            0,  # amountOutMinimum
            0,  # sqrtPriceLimitX96
        )
        try:
            self.router.functions.exactInputSingle(params).call({"from": account.address, "value": 0})
        except (ContractLogicError, BadFunctionCallOutput, ValueError) as e:
            msg = str(e)
            if "execution reverted" in msg and msg.endswith("'0x')"):
                raise RuntimeError(
                    "Router02 probe reverted with empty reason — check router/factory compatibility"
                )
            raise RuntimeError(f"Router02 probe failed: {type(e).__name__}: {e}")

    # -------------------------
    # Quotes
    # -------------------------

    def _quote_best_fee(self, token_in: str, token_out: str, amount_in: int, slippage: float) -> tuple[int, int, int]:
        best_fee = None
        best_out = 0
        self._last_slippage = float(slippage)

        for fee in self.fee_tiers:
            path = self._encode_path([token_in, token_out], [fee])
            try:
                res = self.quoter.functions.quoteExactInput(path, int(amount_in)).call()
            except (ContractLogicError, BadFunctionCallOutput, ValueError) as e:
                self.logger.warning(f"[QUOTE] fee={fee} failed: {type(e).__name__}: {e}")
                continue
            out = int(res[0])
            if out > best_out:
                best_out = out
                best_fee = fee

        if best_fee is None:
            raise RuntimeError("Quoter failed for all fee tiers")

        min_out = int(Decimal(best_out) * (Decimal(1) - Decimal(slippage)))

        if enabled("quotes"):
            self.logger.info(f"Quote | in={amount_in} | exp_out={best_out} | min_out={min_out} | "
                f"slippage={slippage * 100:.2f}% | best_fee={best_fee}")

        # Tulosta täsmällisesti token_in/out perusteella
        try:
            sym_in, dec_in = self._erc20_meta(token_in)
            sym_out, dec_out = self._erc20_meta(token_out)
            human_in = fmt_token(int(amount_in), int(dec_in), sym_in)
            human_out = fmt_token(int(best_out), int(dec_out), sym_out)

            if enabled("quotes"):
                self.logger.info(f"[QUOTE•HUMAN] in≈{human_in} → out≈{human_out} (fee={best_fee})")

        except (BadFunctionCallOutput, ContractLogicError, ValueError):
            # Ei koskaan rikota vaihtoa pelkän logituksen takia
            pass
        return min_out, best_out, best_fee

    def quote_usdc_to_weth_min(self, amount_usdc: int, slippage: float) -> tuple[int, int, int]:
        return self._quote_best_fee(self.usdc, self.weth, int(amount_usdc), slippage)

    def quote_weth_to_usdc_min(self, amount_weth: int, slippage: float) -> tuple[int, int, int]:
        return self._quote_best_fee(self.weth, self.usdc, int(amount_weth), slippage)

    # -------------------------
    # Swaps
    # -------------------------

    def swap_usdc_for_weth(self, account, amount_usdc: int, min_out: int, state=None) -> TradeResult:
        try:
            self.logger.info("[SWAP] USDC → WETH")
            if self.paper:
                return TradeResult(tx_hash=None, amount_out=min_out)

            if not self._check_router_ready():
                return TradeResult(tx_hash=None, amount_out=0)

            # Clamp to on-chain balance (token_in = USDC)
            erc_usdc = self.w3.eth.contract(address=self.usdc, abi=ERC20_ABI)
            try:
                bal = int(erc_usdc.functions.balanceOf(account.address).call())
            except (ContractLogicError, BadFunctionCallOutput, ValueError) as e:
                self.logger.error(f"[BAL] USDC balanceOf failed: {type(e).__name__}: {e}")
                return TradeResult(tx_hash=None, amount_out=0)

            if amount_usdc > bal:
                self.logger.warning(
                    f"[BAL] requested {fmt_usdc(amount_usdc)} > balance {fmt_usdc(bal)} → using balance")
                amount_usdc = bal
                if amount_usdc == 0:
                    # Ei sisääntuloa → pidä päälooppi puhtaana: synkkaa state ketjun tilanteeseen
                    if state is not None:
                        try:
                            self.sync_state_with_chain(state, account)
                            self.logger.info("[STATE] synced after USDC clamp=0")
                        except Exception as e:
                            self.logger.error(f"[STATE] sync after USDC clamp=0 failed: {type(e).__name__}: {e}")

            # Ensin allowance…
            if not self._ensure_allowance(self.usdc, account.address, self.router.address, amount_usdc, account):
                return TradeResult(tx_hash=None, amount_out=0)
            # …sitten policy-check (nyt transferFrom-simulaatio ei kaadu allowanceen)
            try:
                self._pre_swap_policy_check(self.usdc, account.address, self.router.address, amount_probe=1)
            except RuntimeError as e:
                self.logger.error(f"[POLICY] {e}")
                return TradeResult(tx_hash=None, amount_out=0)

            # Fee selection (always compute internally)
            try:
                _, _, fee = self._quote_best_fee(self.usdc, self.weth, amount_usdc,
                                                 getattr(self, "_last_slippage", 0.005))
            except (RuntimeError, ContractLogicError, BadFunctionCallOutput, ValueError):
                fee = 3000

            # Pool sanity (optional)
            pool, liq = self._get_pool_and_liquidity(self.usdc, self.weth, fee)

            # Jos pool puuttuu tai liq=0, ei jatketa
            try:
                int_pool = int(pool, 16)
            except Exception:
                int_pool = 0
            if int_pool == 0 or liq == 0:
                self.logger.error(f"[POOL] missing/empty for fee={fee} pool={pool} liq={liq}")
                return TradeResult(tx_hash=None, amount_out=0)

            if enabled("executor") and not rate_limited("executor.pool"):
                self.logger.info(f"[POOL] fee={fee} pool={pool} liq={liq}")

            # Router02 probe (read-only)
            try:
                self._sanity_probe_router_call(account, self.usdc, self.weth, fee, tiny_in=1)
            except RuntimeError as e:
                self.logger.error(f"[SANITY] {e}")
                return TradeResult(tx_hash=None, amount_out=0)

            # Build 7-field tuple (NO deadline)
            def params(a_in: int, a_min: int):
                return (
                    self.usdc,
                    self.weth,
                    fee,
                    Web3.to_checksum_address(account.address),
                    int(a_in),
                    int(a_min),
                    0,  # sqrtPriceLimitX96
                )

            # Preflight: min_out=0  (ANKKUROIDAAN min_out preflightin outiin)
            p0 = params(amount_usdc, 0)
            ok0, sim0 = self._pref_single(account, p0)

            if not ok0:
                self.logger.error("[PREFLIGHT] min_out=0 revert → abort")
                return TradeResult(tx_hash=None, amount_out=0)
            # Strict: älä madalla min_outia preflightin perusteella
            if sim0 > 0 and sim0 < min_out:
                self.logger.error(f"[SLIPPAGE] preflight_out {sim0} < min_out {min_out} → abort")
                return TradeResult(tx_hash=None, amount_out=0)


            p = params(amount_usdc, min_out)
            ok1, _ = self._pref_single(account, p)

            if not ok1:
                self.logger.error("[PREFLIGHT] revert with requested min_out → abort")
                return TradeResult(tx_hash=None, amount_out=0)

            # Dry-run call (better reason if fail)
            try:
                call_tx = self.router.functions.exactInputSingle(p).build_transaction(
                    {"from": account.address, "chainId": self.chain_id, "value": 0}
                )
                self.w3.eth.call({k: v for k, v in call_tx.items() if k in ("to", "from", "data", "value")})
            except (ContractLogicError, BadFunctionCallOutput, ValueError) as ee:
                if enabled("executor"):
                    self.logger.error(f"[DRYRUN] call revert: {type(ee).__name__}: {ee}")

            # Build + send
            max_fee, tip = self._eip1559_fees()

            # --- ENNEN tx:ää: lue WETH-saldo (out-token) deltaa varten
            erc_weth = self.w3.eth.contract(address=self.weth, abi=ERC20_ABI)
            try:
                before = int(erc_weth.functions.balanceOf(account.address).call())
            except (ContractLogicError, BadFunctionCallOutput, ValueError):
                before = None  # delta mittaus best-effort

            tx = self.router.functions.exactInputSingle(p).build_transaction(
                {
                    "from": account.address,
                    "nonce": self.w3.eth.get_transaction_count(account.address, "pending"),
                    "chainId": self.chain_id,
                    "maxFeePerGas": max_fee,
                    "maxPriorityFeePerGas": tip,
                    "value": 0,
                }
            )
            tx = self._apply_gas_or_force(tx, default_gas=600_000)
            signed = account.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            rcpt = None
            try:
                rcpt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=self.rpc_timeout)
            except TimeExhausted:
                self.logger.warning(f"[SEND] wait_for_receipt timeout for {tx_hash.hex()} — continuing best-effort")

            # --- JÄLKEEN: lue WETH-saldo ja laske delta (best-effort)
            try:
                after = int(erc_weth.functions.balanceOf(account.address).call())
            except (ContractLogicError, BadFunctionCallOutput, ValueError):
                after = None
            if before is not None and after is not None:
                delta = after - before
                actual_out = delta if delta > 0 else int(min_out)
            else:
                actual_out = int(min_out)

            gas_used = gas_price_gwei = gas_cost_eth = None
            if rcpt is not None:
                gas_used, gas_price_gwei, gas_cost_eth = self._gas_meta_from_receipt(rcpt)

            return TradeResult(
                tx_hash=self.w3.to_hex(tx_hash),
                amount_out=actual_out,
                used_min_out=int(min_out),
                used_fee=int(fee),
                gas_used=gas_used,
                gas_price_gwei=gas_price_gwei,
                gas_token="ETH",
                gas_cost_amount=gas_cost_eth,
            )

        except Exception as e:
            self.logger.error(f"swap_usdc_for_weth failed: {type(e).__name__}: {e}")
            return TradeResult(tx_hash=None, amount_out=0)

    def swap_weth_for_usdc(self, account, amount_weth: int, min_out: int, state=None) -> TradeResult:
        try:
            self.logger.info("[SWAP] WETH → USDC")
            if self.paper:
                return TradeResult(tx_hash=None, amount_out=min_out)
            if not self._check_router_ready():
                return TradeResult(tx_hash=None, amount_out=0)

            # Clamp to on-chain balance (token_in = WETH)
            erc_weth = self.w3.eth.contract(address=self.weth, abi=ERC20_ABI)

            try:
                bal = int(erc_weth.functions.balanceOf(account.address).call())
            except (ContractLogicError, BadFunctionCallOutput, ValueError) as e:
                self.logger.error(f"[BAL] WETH balanceOf failed: {type(e).__name__}: {e}")
                return TradeResult(tx_hash=None, amount_out=0)

            if amount_weth > bal:
                    self.logger.warning(f"[BAL] requested {fmt_weth(amount_weth)} > balance {fmt_weth(bal)} → using balance")
                    amount_weth = bal
            if amount_weth == 0:
                # Ei sisääntuloa → pidä päälooppi puhtaana: synkkaa state ketjun tilanteeseen
                if state is not None:
                    try:
                        self.sync_state_with_chain(state, account)
                        self.logger.info("[STATE] synced after WETH clamp=0")
                    except Exception as e:
                        self.logger.error(f"[STATE] sync after WETH clamp=0 failed: {type(e).__name__}: {e}")
                return TradeResult(tx_hash=None, amount_out=0)

            # Ensin allowance…
            if not self._ensure_allowance(self.weth, account.address, self.router.address, amount_weth, account):
                return TradeResult(tx_hash=None, amount_out=0)
            # …sitten policy-check (nyt transferFrom-simulaatio ei kaadu allowanceen)
            try:
                self._pre_swap_policy_check(self.weth, account.address, self.router.address, amount_probe=1)
            except RuntimeError as e:
                self.logger.error(f"[POLICY] {e}")
                return TradeResult(tx_hash=None, amount_out=0)

            # Fee selection (always compute internally; direction WETH → USDC)
            try:
                _, _, fee = self._quote_best_fee(self.weth, self.usdc, amount_weth,
                                                 getattr(self, "_last_slippage", 0.005))
            except (RuntimeError, ContractLogicError, BadFunctionCallOutput, ValueError):
                fee = 3000

            # Pool sanity (optional)
            pool, liq = self._get_pool_and_liquidity(self.weth, self.usdc, fee)
            self.logger.info(f"[POOL] fee={fee} pool={pool} liq={liq}")

            try:
                pool_int = int(pool, 16)
            except Exception:
                pool_int = 0
            if pool_int == 0 or liq == 0:
                self.logger.error("[POOL] missing/empty")
                return TradeResult(tx_hash=None, amount_out=0)

            # Router02 probe (read-only)
            try:
                self._sanity_probe_router_call(account, self.weth, self.usdc, fee, tiny_in=1)
            except RuntimeError as e:
                self.logger.error(f"[SANITY] {e}")
                return TradeResult(tx_hash=None, amount_out=0)

            # Build tuple (NO deadline)
            def params(a_in: int, a_min: int):
                return (
                    self.weth,
                    self.usdc,
                    fee,
                    Web3.to_checksum_address(account.address),
                    int(a_in),
                    int(a_min),
                    0,
                )

            # Preflight: min_out=0  (ANKKUROINTI)
            p0 = params(amount_weth, 0)
            ok0, sim0 = self._pref_single(account, p0)
            if not ok0:
                self.logger.error("[PREFLIGHT] min_out=0 revert → abort")
                return TradeResult(tx_hash=None, amount_out=0)

            # Strict: älä madalla min_outia preflightin perusteella
            if sim0 > 0 and sim0 < min_out:
                self.logger.error(f"[SLIPPAGE] preflight_out {sim0} < min_out {min_out} → abort")
                return TradeResult(tx_hash=None, amount_out=0)

            # Preflight with requested min_out (relax to 99.7% if needed)
            p = params(amount_weth, min_out)
            ok1, _ = self._pref_single(account, p)
            if not ok1:
                self.logger.error("[PREFLIGHT] revert with requested min_out → abort")
                return TradeResult(tx_hash=None, amount_out=0)

            # Build + send
            max_fee, tip = self._eip1559_fees()

            # --- ENNEN tx:ää: lue USDC-saldo (out-token) deltaa varten
            erc_usdc = self.w3.eth.contract(address=self.usdc, abi=ERC20_ABI)
            try:
                before = int(erc_usdc.functions.balanceOf(account.address).call())
            except (ContractLogicError, BadFunctionCallOutput, ValueError):
                before = None

            tx = self.router.functions.exactInputSingle(p).build_transaction(
                {
                    "from": account.address,
                    "nonce": self.w3.eth.get_transaction_count(account.address, "pending"),
                    "chainId": self.chain_id,
                    "maxFeePerGas": max_fee,
                    "maxPriorityFeePerGas": tip,
                    "value": 0,
                }
            )
            tx = self._apply_gas_or_force(tx, default_gas=600_000)
            signed = account.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            rcpt = None
            try:
                rcpt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=self.rpc_timeout)
            except TimeExhausted:
                self.logger.warning(f"[SEND] wait_for_receipt timeout for {tx_hash.hex()} — continuing best-effort")

            # Best-effort actual received (from balance delta)
            try:
                after = int(erc_usdc.functions.balanceOf(account.address).call())
            except (ContractLogicError, BadFunctionCallOutput, ValueError):
                after = None
            if before is not None and after is not None:
                actual_out = max(0, after - before)
                if actual_out == 0:
                    actual_out = int(min_out)
            else:
                actual_out = int(min_out)

            gas_used = gas_price_gwei = gas_cost_eth = None
            if rcpt is not None:
                gas_used, gas_price_gwei, gas_cost_eth = self._gas_meta_from_receipt(rcpt)

            return TradeResult(
                tx_hash=self.w3.to_hex(tx_hash),
                amount_out=actual_out,
                used_min_out=int(min_out),
                used_fee=int(fee),
                gas_used=gas_used,
                gas_price_gwei=gas_price_gwei,
                gas_token="ETH",
                gas_cost_amount=gas_cost_eth,
            )

        except Exception as e:
            self.logger.error(f"swap_weth_for_usdc failed: {type(e).__name__}: {e}")
            return TradeResult(tx_hash=None, amount_out=0)

    # -------------------------
    # Light preflight wrappers
    # -------------------------

    def _pref_single(self, account, params_tuple) -> tuple[bool, int]:
        try:
            out = self.router.functions.exactInputSingle(params_tuple).call(
                {"from": account.address, "value": 0}
            )
            if enabled("executor"):
                self.logger.info(f"[PREFLIGHT] exactInputSingle → {out}")
            return True, int(out)
        except (ContractLogicError, BadFunctionCallOutput, ValueError) as e:
            # Yritetään antaa hieman selitteisempi viesti yleisiin revertteihin
            msg = str(e)
            if "Too little received" in msg:
                self.logger.error(f"[PREFLIGHT] revert: Too little received")
            else:
                self.logger.error(f"[PREFLIGHT] revert: {type(e).__name__}: {e}")
            return False, 0

    # -------------------------
    # State sync / debug
    # -------------------------

    def sync_state_with_chain(self, state, account):
        erc_weth = self.w3.eth.contract(address=self.weth, abi=ERC20_ABI)
        erc_usdc = self.w3.eth.contract(address=self.usdc, abi=ERC20_ABI)
        real_weth = int(erc_weth.functions.balanceOf(account.address).call())
        real_usdc = int(erc_usdc.functions.balanceOf(account.address).call())

        prev_weth = int(getattr(state, "holding_weth", "0") or "0")
        if prev_weth != real_weth:
            self.logger.warning(
                f"[SYNC] state had {fmt_weth(prev_weth)}, chain has {fmt_weth(real_weth)} → corrected"
            )
        state.holding_weth = str(real_weth)
        state.holding = real_weth > 0

        eth_wei = self.w3.eth.get_balance(account.address)
        eth_human = wei_to_eth(eth_wei)
        self.logger.info(
            f"[SYNC] ETH={eth_human} | {fmt_weth(real_weth)} | {fmt_usdc(real_usdc)}"
        )
        return state

    def debug_balances_and_allowances(self, account):
        eth_wei = self.w3.eth.get_balance(account.address)
        erc_weth = self.w3.eth.contract(address=self.weth, abi=ERC20_ABI)
        erc_usdc = self.w3.eth.contract(address=self.usdc, abi=ERC20_ABI)
        weth_bal = int(erc_weth.functions.balanceOf(account.address).call())
        usdc_bal = int(erc_usdc.functions.balanceOf(account.address).call())
        weth_allow = int(erc_weth.functions.allowance(account.address, self.router.address).call())
        usdc_allow = int(erc_usdc.functions.allowance(account.address, self.router.address).call())

        if enabled("balances"):
            self.logger.info(f"[DEBUG] ETH={wei_to_eth(eth_wei)} | {fmt_weth(weth_bal)} | {fmt_usdc(usdc_bal)}")
            self.logger.info(f"[DEBUG] ALLOW | WETH→router={weth_allow} | USDC→router={usdc_allow}")