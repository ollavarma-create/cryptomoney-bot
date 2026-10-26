import random
from web3 import Web3
from web3.types import HexStr
from eth_account import Account
from decimal import Decimal
from decimal import InvalidOperation
import time, logging
from bot.config import load_config, DATA_DIR, SETTINGS_FILE
from bot.log import setup_logging, enabled
from bot.state import State
from bot.marketdata.v3_pricefeed import V3PriceFeed
from bot.execution.uniswap_v3 import UniswapV3Executor
from bot.marketdata.v3_pricefeed import D
from bot.wallet import WalletManager
import os
from bot.execution.bookkeeper import append_ledger
from bot.fmt import (
    fmt_weth,
    fmt_usdc,
    price_usdc_per_weth_from_amounts,
    fill_price_debug,
    usdc_scale_from6,
)

# print("[BOOT] Using executor module at:", ex.__file__)
# print("[RPC] raw env:", repr(os.environ.get("W3_CLIENT_URL")))

USDC_DECIMALS = 6  # päivitetään build():ssa ketjun mukaan

ERC20_BAL_ABI = [{
    "constant": True,
    "inputs": [{"name": "owner", "type": "address"}],
    "name": "balanceOf",
    "outputs": [{"name": "", "type": "uint256"}],
    "stateMutability": "view",
    "type": "function"
}]

def _clean_url(u: str) -> str:
    if not isinstance(u, str):
        raise ValueError("W3_CLIENT_URL must be a string")
    u = u.strip()
    # Poista vahingossa mukaan tulleet ympäröivät ' tai "
    if (u.startswith('"') and u.endswith('"')) or (u.startswith("'") and u.endswith("'")):
        u = u[1:-1].strip()
    return u

def connect_w3(rpc_url: str, headers=None, tries: int = 3, delay: float = 1.5) -> Web3:
    rpc_url = _clean_url(rpc_url)
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30, **({"headers": headers} if headers else {})}))
    for _ in range(tries):
        if w3.is_connected():
            return w3
        time.sleep(delay)
    raise RuntimeError(f"RPC not connected (url={rpc_url}). Check API key/secret, project restrictions, or network/SSL.")

def _rand_pct(lo, hi):
    lo_d = _norm_pct(lo)
    hi_d = _norm_pct(hi)
    if hi_d < lo_d:
        lo_d, hi_d = hi_d, lo_d
    r = lo_d + (hi_d - lo_d) * Decimal(str(random.random()))
    return r.quantize(Decimal("0.00001"))

def _clamp01_or_none(x):
    try:
        d = D(x)
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if Decimal("0") <= d <= Decimal("1") else None

def _d(s) -> Decimal:
    """Safe Decimal() helper that accepts str/float/int/None/''."""
    if s in (None, ""):
        return Decimal(0)
    return Decimal(str(s))

def _fmt(x) -> str:
    """Format Decimal/float/int back to canonical string for state.json."""
    return str(Decimal(str(x)))

def ensure_ladder_progress(state, cfg):
    """
    Varmista, että state.ladder_progress pituus vastaa asetuksissa olevia ladder-portaita.
    Jos reentry_mode != dual tai portaita ei ole määritelty, lista voi olla tyhjä.
    """
    dre = getattr(cfg.trading, "dual_reentry", None)
    levels = []
    if dre is not None:
        levels = getattr(dre, "ladder_levels_pct", None) or (isinstance(dre, dict) and dre.get("ladder_levels_pct") or [])
    n = len(levels or [])
    if not state.ladder_progress or len(state.ladder_progress) != n:
        state.ladder_progress = [False] * n


def _norm_pct(x) -> Decimal:
    s = str(x).strip()
    had_pct = s.endswith('%')
    if had_pct:
        s = s[:-1]
    v = D(s or "0")
    if had_pct:
        return v / Decimal(100)       # "1%"  -> 0.01, "2.5%" -> 0.025
    return v / Decimal(100) if v >= 1 else v  # "1" -> 0.01, "2" -> 0.02, "0.5" -> 0.5

def _append_gas_update_v11(tx_hash: str, gas_used: int, eff_wei: int, eth_price_usdc: float | None):
    from bot.config import GasInfo
    from time import time
    from decimal import Decimal

    gas_cost_wei = int(Decimal(gas_used) * Decimal(eff_wei))
    gas_cost_usdc = None
    if eth_price_usdc is not None:
        gas_cost_usdc = float(Decimal(gas_cost_wei) / Decimal(10**18) * Decimal(str(eth_price_usdc)))

    gi = GasInfo(
        gas_used=int(gas_used),
        gas_price_wei=int(eff_wei),
        gas_cost_wei=int(gas_cost_wei),
        eth_price_usdc=float(eth_price_usdc) if eth_price_usdc is not None else None,
        gas_cost_usdc=gas_cost_usdc,
    )
    row = {
        "schema_version": "1.1",
        "ts_ms": int(time() * 1000),
        "type": "gas_update",
        "tx_hash": tx_hash,
        "fill_gas": gi.__dict__,  # sama schema kuin FillInfo.gas
    }
    append_ledger(row)



def _pick_from_range_or_default(rng, default_val: Decimal) -> Decimal:
    if rng and isinstance(rng, (list, tuple)) and len(rng) == 2:
        return _rand_pct(rng[0], rng[1])
    return default_val

# ---- Executed fill price helpers (use real filled amounts) --------------------

def compute_price_impact_bps(side: str, quoted_out: int, amount_in: int, amount_out: int, usdc_decimals: int = 6) -> int:
    """
    Laskee toteutuneen hinnan (fill) ja quoterin hinnan (quote) välisen eron per 10 000 (= bps).
    side="SELL": WETH→USDC, side="BUY": USDC→WETH.
    """
    try:
        if side.upper() == "SELL":
            # Quote-hinta per 1 WETH: (exp_usdc / 10^6) / (weth_in / 10^18)
            if amount_in == 0 or quoted_out == 0:
                return 0
            q_price = (Decimal(quoted_out) / (Decimal(10) ** usdc_decimals)) / (Decimal(amount_in) / Decimal(10**18))
            f_price = (Decimal(amount_out) / (Decimal(10) ** usdc_decimals)) / (Decimal(amount_in) / Decimal(10**18))
        else:
            # BUY: quote palauttaa WETH out → quote-hinta per 1 WETH: (usdc_in / 10^6) / (exp_weth / 10^18)
            if amount_out == 0 or quoted_out == 0:
                return 0
            q_price = (Decimal(amount_in) / (Decimal(10) ** usdc_decimals)) / (Decimal(quoted_out) / Decimal(10**18))
            f_price = (Decimal(amount_in) / (Decimal(10) ** usdc_decimals)) / (Decimal(amount_out) / Decimal(10**18))
        if q_price == 0:
            return 0
        impact = (f_price - q_price) / q_price * Decimal(10000)  # → bps
        return int(impact)
    except Exception:
        return 0

def _to_bps(frac: float | Decimal) -> int:
    try:
        return int(Decimal(str(frac)) * Decimal(10_000))
    except Exception:
        return 0

def _mk_gasinfo_from_result(res, eth_price_usdc_float: float | None):
    """
    Muunna executor-resultin gas-meta Ledger GasInfo -kentäksi.
    Palauttaa tuple: (gas_dict_for_legacy_notes, gas_info_obj_or_None)
    """
    gas = _gas_meta_from_result(res)
    try:
        gas_used = int(gas["gas_used"]) if gas["gas_used"] is not None else None
        gp_gwei  = Decimal(gas["gas_price_gwei"]) if gas["gas_price_gwei"] else None
        gas_price_wei = int(gp_gwei * Decimal(1_000_000_000)) if (gp_gwei is not None) else None
        gas_cost_wei = int(Decimal(gas_used) * (gp_gwei * Decimal(1_000_000_000))) if (gas_used is not None and gp_gwei is not None) else None

        from bot.config import GasInfo  # paikallinen import, jotta moduuli latautuu kevyesti
        gi = GasInfo(
            gas_used=gas_used or 0,
            gas_price_wei=gas_price_wei or 0,
            gas_cost_wei=gas_cost_wei or 0,
            eth_price_usdc=float(eth_price_usdc_float) if eth_price_usdc_float is not None else None,
            gas_cost_usdc=( (gas_cost_wei or 0) / 1e18 * float(eth_price_usdc_float) ) if (gas_cost_wei and eth_price_usdc_float) else None,
        )
        return gas, gi
    except Exception:
        return gas, None



def log_trade_result(side: str, quoted_out: int, min_out: int, result) -> None:
    """
    side: "BUY" tai "SELL"
    quoted_out: quoterin antama best_out (base-yksiköissä)
    min_out: kutsuun välitetty min_out (base-yksiköissä)
    result: TradeResult (sis. used_min_out, used_fee)
    """
    # Turvallinen haku, jos vanha binaari palauttaisi 2-kenttäisen TradeResultin
    used_min = getattr(result, "used_min_out", min_out)
    used_fee = getattr(result, "used_fee", None)

    try:
        if side.upper() == "SELL":  # WETH → USDC
            human_out = fmt_usdc(result.amount_out, USDC_DECIMALS)
            human_min = fmt_usdc(used_min, USDC_DECIMALS)
            human_quote = fmt_usdc(quoted_out, USDC_DECIMALS)
        else:  # BUY: USDC → WETH
            human_out = fmt_weth(result.amount_out)  # wei
            human_min = fmt_weth(used_min)
            human_quote = fmt_weth(quoted_out)
    except Exception:
        human_out, human_min, human_quote = map(str, (result.amount_out, used_min, quoted_out))

    fee_str = f"{used_fee}" if used_fee is not None else "?"
    logging.getLogger("main").info(
        f"{side} tx={result.tx_hash} quoted≈{human_quote}, min(asked)={human_min} "
        f"| used_min_out(base)={used_min} fee={fee_str} → got={human_out}"
    )

def _gas_meta_from_result(res) -> dict:
    # yritetään lukea mahdolliset kentät; oletuksena None
    return {
        "gas_used": getattr(res, "gas_used", None),
        "gas_price_gwei": (str(getattr(res, "gas_price_gwei", "")) or None),
        "gas_token": getattr(res, "gas_token", "ETH"),  # useimmiten ETH
        "gas_cost_token": getattr(res, "gas_token", "ETH"),
        "gas_cost_amount": (str(getattr(res, "gas_cost_amount", "")) or None),
    }

# --- GAS-kuittien ei-blokkaava päivitys ---
from web3.exceptions import TransactionNotFound
pending_gas: set[HexStr] = set()

def _queue_gas_update(tx_hash: str | None) -> None:
    if tx_hash:
        # varmistetaan HexStr ja 0x-prefiksi
        s = tx_hash if tx_hash.startswith("0x") else f"0x{tx_hash}"
        pending_gas.add(HexStr(s))

# -----------------------------------------------------------------------------

def build():
    conf = load_config()
    print(f"[CFG] network_profile={getattr(conf, 'network_profile', None)} "
          f"chain_id={conf.ETH_CHAIN_ID} rpc={conf.W3_CLIENT_URL}")

    tcfg = conf.trading
    private_key = os.getenv("BOT_PRIVATE_KEY")
    if not private_key:
        print("Virhe: Ympäristömuuttujaa BOT_PRIVATE_KEY ei löydy.")
        print("Aseta privaattiavain ympäristömuuttujaksi seuraavalla komennolla:")
        print("export BOT_PRIVATE_KEY='<your_private_key_here>'")
        raise RuntimeError("Ympäristömuuttujaa BOT_PRIVATE_KEY ei löydy.")

    key = private_key.removeprefix("0x")

    if len(key) != 64:
        print(f"Virhe: Privaattiavaimen pituus on virheellinen. Odotettu 64, saatu {len(key)}.")
        raise ValueError(f"Privaattiavaimen pituus on virheellinen. Odotettu 64, saatu {len(key)}.")

    # Privaattiavain ympäristömuuttujasta
    acct = Account.from_key(key)

    # Verkko- ja sopimusosoitteet tulevat aina aktiivisesta profileista (config.load_config() hoitaa)
    chain_id = conf.ETH_CHAIN_ID
    router   = conf.ETH_V3_SWAPROUTER02_ADDRESS
    quoter   = conf.ETH_V3_QUOTERV2_ADDRESS
    weth     = conf.ETH_WETH_ADDRESS
    usdc     = conf.ETH_USDC_ADDRESS
    fee_tiers = conf.UNISWAP_V3_FEE_TIERS

    # Set up logging (idempotent). Käytä settings.json → debug-blokkia.
    try:
        setup_logging(getattr(conf, "debug", {}))
    except Exception:
        # vanha fallback, jos conf ei sisällä 'debug' (ei kaadeta)
        setup_logging({})
    log = logging.getLogger("main")
    log.info(f"Using settings from {SETTINGS_FILE}")
    log.info(f"DATA_DIR={DATA_DIR}")
    log.info(f"chain_id={chain_id} routerV3={router} quoterV2={quoter} weth={weth} usdc={usdc}")

    # Näytä TP/SL range & scalar riippumatta siitä ollaanko testnetissä vai mainnetissä
    log.info(f"[CFG] tp_range={getattr(tcfg, 'target_profit_pct_range', None)} "
             f"sl_range={getattr(tcfg, 'stop_loss_pct_range', None)} "
             f"tp_scalar={getattr(tcfg, 'target_profit_pct', None)} "
             f"sl_scalar={getattr(tcfg, 'stop_loss_pct', None)}")

    if not conf.W3_CLIENT_URL or not str(conf.ETH_CHAIN_ID):
        raise RuntimeError(
            "Missing RPC or chain_id after profile resolution. Check settings.json profiles + network_profile.")

    w3 = connect_w3(os.environ["W3_CLIENT_URL"])

    if not w3.is_connected():
        raise RuntimeError("RPC not connected")
    actual = w3.eth.chain_id
    desired = conf.ETH_CHAIN_ID

    if actual != desired:
        raise SystemExit(f"[CONFIG] RPC chain_id {actual} ≠ settings chain_id {desired}. "
            f"Tarkista network_profile ja W3_CLIENT_URL.")



    # --- Hae USDC:n desimaalit ketjusta ---
    ERC20_DECIMALS_ABI = [{
        "constant": True, "inputs": [], "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view", "type": "function"
    }]
    from web3.exceptions import BadFunctionCallOutput, ContractLogicError

    try:
        usdc_decimals = int(
            w3.eth.contract(
                address=Web3.to_checksum_address(usdc),
                abi=ERC20_DECIMALS_ABI
            ).functions.decimals().call()
        )
    except (BadFunctionCallOutput, ContractLogicError, ValueError) as e:
        logging.getLogger("main").warning(
            f"[DECIMALS] fallback to 6 (failed to fetch on-chain): {type(e).__name__}: {e}")
        usdc_decimals = 6

    global USDC_DECIMALS
    USDC_DECIMALS = usdc_decimals
    log.info(f"USDC decimals={USDC_DECIMALS} (amounts treated in base units)")

    # Luo pricefeed vasta nyt (kun desimaalit tiedossa)
    pricefeed = V3PriceFeed(w3, quoter, weth, usdc, fee_tiers, usdc_decimals=USDC_DECIMALS)

    def _pump_gas_updates() -> None:
        for tx in list(pending_gas):
            try:
                rcpt = w3.eth.get_transaction_receipt(tx)
            except TransactionNotFound:
                continue
            except Exception as e:
                logging.getLogger("main").debug(f"[GAS] rcpt fetch fail {tx}: {type(e).__name__}: {e}")
                continue

            gas_used = (getattr(rcpt, "gasUsed", None)
                        or getattr(rcpt, "gas_used", None)
                        or rcpt.get("gasUsed"))
            eff_wei = (getattr(rcpt, "effectiveGasPrice", None)
                       or getattr(rcpt, "effective_gas_price", None)
                       or rcpt.get("effectiveGasPrice")
                       or getattr(rcpt, "gasPrice", None)
                       or rcpt.get("gasPrice")) # type: ignore[attr-defined]
            if gas_used is None or eff_wei is None:
                continue

            try:
                eth_px = float(pricefeed.price_usdc_per_weth())  # USDC per 1 ETH~WETH
            except Exception:
                eth_px = None

            _append_gas_update_v11(str(tx), int(gas_used), int(eff_wei), eth_px)
            pending_gas.discard(tx)  # valmis

    from pathlib import Path
    state_path = Path(conf.ETH_STATE_FILE)
    state = State()
    state.load(state_path)

    # --- UUSI: ladder-progress ja rolling_high -alustus (resume-friendly) -----
    ensure_ladder_progress(state, conf)

    # rolling_high: jos FLAT ja arvo uupuu → aloita viimeisestä myyntihinnasta
    try:
        if not state.holding:
            if (state.rolling_high or "") == "":
                if (state.last_sell_price or "") != "":
                    state.rolling_high = state.last_sell_price
        else:
            # holding-tilassa rolling_high ei ole kriittinen, mutta voidaan alustaa last_buy_price:sta
            if (state.rolling_high or "") == "" and (state.last_buy_price or "") != "":
                state.rolling_high = state.last_buy_price
    except Exception as _e:
        # ei faila käynnistystä; vain informatiivinen
        logging.getLogger("main").warning(f"[INIT] rolling_high init warning: {type(_e).__name__}: {_e}")
    # --------------------------------------------------------------------------


    fix = False
    for k in ("target_pct_active", "stop_pct_active"):
        v = getattr(state, k, "")
        if not v or _clamp01_or_none(v) is None:
            setattr(state, k, "0")  # << bugifix: "" → "0"
            fix = True
    if fix:
        logging.getLogger("main").warning("[SANITY] TP/SL invalid in state → will re-pick from ranges")
        state.save(state_path)

    executor = UniswapV3Executor(
        w3, router, quoter, weth, usdc,
        chain_id=chain_id,
        fee_tiers=fee_tiers,
        gas_mult=conf.ETH_GAS_PRICE_MULTIPLIER,
        gas_buf_pct=conf.ETH_MAX_GAS_BUFFER_PCT,
        rpc_timeout=conf.ETH_RPC_TIMEOUT,
        paper=bool(conf.testrun),  # testrun=True ⇒ PAPER (ei tx), testrun=False ⇒ LIVE
        logger=logging.getLogger("executor.uniswap_v3"),
    )

    # synkkaa chainin kanssa
    state = executor.sync_state_with_chain(state, acct)
    state.save(state_path)

    wallet = WalletManager(
        w3, acct,
        weth_addr=weth,
        usdc_addr=usdc,
        router_addr=router,
        usdc_decimals=USDC_DECIMALS,
        min_eth_gas_buffer_wei=conf.wallet.min_eth_gas_buffer_wei,
        eth_topup_target_wei=conf.wallet.eth_topup_target_wei,
        wrap_excess_eth_when_flat=conf.wallet.wrap_excess_eth_when_flat,
        rebalance_cooldown_seconds=conf.wallet.rebalance_cooldown_seconds,
    )

    logging.getLogger("wallet").info("WalletManager ready")
    log.info(f"[CFG.wallet] buffer={conf.wallet.min_eth_gas_buffer_wei} "
             f"topup={conf.wallet.eth_topup_target_wei} "
             f"wrap_flat={conf.wallet.wrap_excess_eth_when_flat} "
             f"cooldown={conf.wallet.rebalance_cooldown_seconds}s")

    # ---- Trading config: ranget + defaultit -----------------------------------
    tp_range = getattr(tcfg, "target_profit_pct_range", None)
    sl_range = getattr(tcfg, "stop_loss_pct_range", None)

    # Lue yksittäiset; jos puuttuvat/0, käytä range-mediaania defaultiksi
    target = _norm_pct(getattr(tcfg, "target_profit_pct", "0"))
    stop   = _norm_pct(getattr(tcfg, "stop_loss_pct", "0"))
    if target == 0 and tp_range:
        target = (_norm_pct(tp_range[0]) + _norm_pct(tp_range[1])) / Decimal(2)
    if stop == 0 and sl_range:
        stop   = (_norm_pct(sl_range[0]) + _norm_pct(sl_range[1])) / Decimal(2)

    # --- Bootstrap + näkyvä status jos holdataan ---------------------------------
    try:
        # 1) Hae markkinahinta (USDC / 1 WETH)
        price = D(pricefeed.price_usdc_per_weth())

        # 2) Jos holdataan mutta entry puuttuu → rekonstruoi quoterilla
        if state.holding:
            last_buy_raw = getattr(state, "last_buy_price", "") or ""
            if last_buy_raw == "" or D(last_buy_raw) == 0:
                weth_amt = int(D(getattr(state, "holding_weth", "0") or "0"))
                if weth_amt > 0:
                    try:
                        _, exp_usdc, _ = executor.quote_weth_to_usdc_min(weth_amt, 0.0)
                        price_est = D(exp_usdc) / (D(10) ** USDC_DECIMALS) / (D(weth_amt) / (D(10) ** 18))
                        state.last_buy_price = str(price_est)
                        if not getattr(state, "last_spent_usdc", ""):
                            state.last_spent_usdc = str(exp_usdc)  # base units
                        state.save(state_path)
                        logging.getLogger("main").info(
                            f"[BOOTSTRAP] Reconstructed last_buy_price={price_est} and last_spent_usdc={state.last_spent_usdc}"
                        )
                    except Exception as e:
                        logging.getLogger("main").warning(
                            f"[BOOTSTRAP] could not reconstruct last_buy_price: {type(e).__name__}: {e}"
                        )

        # 3) Varmista että tp/sl aktiiviset on asetettu heti bootissa
        if state.holding:
            if not getattr(state, "target_pct_active", "") or D(getattr(state, "target_pct_active", "0") or "0") == 0:
                state.target_pct_active = str(_pick_from_range_or_default(tp_range, target))
            if not getattr(state, "stop_pct_active", "") or D(getattr(state, "stop_pct_active", "0") or "0") == 0:
                state.stop_pct_active = str(_pick_from_range_or_default(sl_range, stop))
            state.save(state_path)

            # Tulosta ensimmäinen status ei-nollilla arvoilla
            last_buy = D(getattr(state, "last_buy_price", "0") or "0")
            weth_wei = int(D(getattr(state, "holding_weth", "0") or "0"))
            p = ((price - last_buy) / last_buy) if last_buy > 0 else D(0)
            tp_use = D(state.target_pct_active)
            sl_use = D(state.stop_pct_active)
            log.info(f"status=HOLDING Δ={p:.3%} tp={tp_use:.3%} sl={-sl_use:.3%} pos={fmt_weth(weth_wei)}")
        else:
            log.info("status=FLAT (no position)")

    except Exception as e:
        log.warning(f"[BOOTSTRAP] unexpected error: {type(e).__name__}: {e}")

    # Pre-approve on demand
    eth_bal = w3.eth.get_balance(acct.address)
    if eth_bal == 0:
        log.info("No ETH on this chain for gas → skipping startup pre-approve; will attempt on-demand later.")
    else:
        try:
            if not executor._ensure_allowance(usdc, acct.address, executor.router.address, 1, acct):
                log.info("USDC approve pending at startup → will retry in ticks")
            if not executor._ensure_allowance(weth, acct.address, executor.router.address, 1, acct):
                log.info("WETH approve pending at startup → will retry in ticks")
        except Exception as e:
            log.warning(f"pre-approve failed (will retry in ticks): {type(e).__name__}: {e}")

    net = f"chain_id={conf.ETH_CHAIN_ID}"
    mode = "PAPER" if executor.paper else "LIVE"
    log.info(f"CryptoBotti startattu ({mode}, {net})")

    _tick_i = 0
    def on_tick():
        nonlocal log, _tick_i
        _tick_i += 1

        # Päivitä mahdolliset gas-kuitit joka tikillä
        try:
            _pump_gas_updates()
        except Exception as e:
            log.debug(f"[GAS] pump failed: {type(e).__name__}: {e}")

        now = time.time()
        last_ts = float(getattr(state, "last_trade_ts", "0") or "0")

        # Kaksi erillistä suojaa: lyhyt trade-interval + pidempi cooldown
        short_left = tcfg.min_trade_interval_seconds - (now - last_ts)
        long_left  = getattr(tcfg, "min_cooldown_sec", 0) - (now - last_ts)
        wait_left  = max(short_left, long_left)
        if wait_left > 0:
            wallet.rebalance_if_flat(state, executor, pricefeed)
            try:
                log.info(f"cooldown: {int(wait_left)}s left")
            except Exception:
                # fallback, jos Decimal/float tulkinta sakkaa
                try:
                    log.info(f"cooldown: {int(float(wait_left))}s left")
                except Exception:
                    log.info("cooldown active")
            return

        try:
            # pricefeed returns float → normalize via D() to avoid ConversionSyntax/float artifacts
            price = D(pricefeed.price_usdc_per_weth())  # USDC per 1 WETH as Decimal

            if state.holding:
                # Varmista tp/sl aktiiviset (jos puuttuvat) – kerran per käynnistys/tilanne
                if not getattr(state, "target_pct_active", "") or D(getattr(state, "target_pct_active", "0") or "0") == 0:
                    state.target_pct_active = str(_pick_from_range_or_default(tp_range, target))
                if not getattr(state, "stop_pct_active", "") or D(getattr(state, "stop_pct_active", "0") or "0") == 0:
                    state.stop_pct_active = str(_pick_from_range_or_default(sl_range, stop))
                state.save(state_path)

                # Be defensive: state values are strings
                last_buy_raw = getattr(state, "last_buy_price", "0") or "0"
                last_buy = D(last_buy_raw)
                if last_buy == 0:
                    # Try reconstructing on the fly
                    weth_amt_tmp = int(D(getattr(state, "holding_weth", "0") or "0"))
                    if weth_amt_tmp > 0:
                        try:
                            _, exp_usdc, _ = executor.quote_weth_to_usdc_min(weth_amt_tmp, 0.0)
                            price_est = D(exp_usdc) / (D(10) ** USDC_DECIMALS) / (D(weth_amt_tmp) / (D(10) ** 18))
                            state.last_buy_price = str(price_est)
                            if not getattr(state, "last_spent_usdc", ""):
                                state.last_spent_usdc = str(exp_usdc)
                            state.save(state_path)
                            last_buy = D(state.last_buy_price)
                            log.warning(f"[SELF-HEAL] last_buy_price was missing; set to {price_est}")
                        except Exception as ee:
                            log.warning(
                                f"holding=True but last_buy_price invalid and reconstruct failed: {type(ee).__name__}: {ee}")
                            return
                    else:
                        log.warning("holding=True but holding_weth is zero/invalid → skip this tick")
                        return

                p = (price - last_buy) / last_buy

                weth_raw = getattr(state, "holding_weth", "0") or "0"
                weth_wei = int(D(weth_raw))

                tp_use = D(getattr(state, "target_pct_active", "0") or "0")
                sl_use = D(getattr(state, "stop_pct_active", "0") or "0")
                if tp_use == 0:
                    tp_use = _pick_from_range_or_default(tp_range, target)
                if sl_use == 0:
                    sl_use = _pick_from_range_or_default(sl_range, stop)

                # Päätöslogi näkyväksi (gated)
                if enabled("trade_decisions"):
                    log.info(
                        f"[DECISION] holding Δ={p:.3%} vs tp={tp_use:.3%} / -sl={-sl_use:.3%}")

                log.info(f"status=HOLDING Δ={p:.3%} tp={tp_use:.3%} sl={-sl_use:.3%} pos={fmt_weth(weth_wei)}")
            else:
                log.info("status=FLAT (no position)")
                # FLAT: näytä re-entry mittatikku (dip vs target), jotta tiedät miksi ei ostettu
                if state.last_sell_price and enabled("reentry"):
                    last_sell_dbg = D(getattr(state, "last_sell_price", "0") or "0")
                    if last_sell_dbg > 0:
                        dip_dbg = (last_sell_dbg - price) / last_sell_dbg
                        log.info(f"[REENTRY] last_sell={last_sell_dbg:.6f} price={price:.6f} "
                                 f"dip={dip_dbg:.3%} target={target:.3%}")
                log.info(f"price_usdc_per_weth={price}")  # keep after successful parse
                # --- UUSI: rolling_high päivitys FLAT-tilassa ----------------------
                try:
                    rh = _d(state.rolling_high)
                    if rh == 0 or price > rh:
                        state.rolling_high = _fmt(price)
                        state.save(state_path)
                except Exception:
                    pass


        except Exception as e:
            log.warning(f"pricefeed failed (will retry next tick): {type(e).__name__}: {e}")
            log.debug(
                f"[DBG] state.last_buy_price={getattr(state, 'last_buy_price', None)!r} "
                f"state.holding_weth={getattr(state, 'holding_weth', None)!r} "
                f"target_cfg={getattr(conf.trading, 'target_profit_pct', None)!r} "
                f"stop_cfg={getattr(conf.trading, 'stop_loss_pct', None)!r}"
            )
            return

        # --- Initial buy ---------------------------------------------------------
        if not state.holding and state.first_trade:
            state.target_pct_active = "0"
            state.stop_pct_active = "0"

            raw = int(tcfg.trade_amount_usdc)  # oletus 6-des yksiköissä
            usdc_amt = usdc_scale_from6(raw, USDC_DECIMALS)

            # CLAMP: käytä enintään on-chain saldoa
            erc_usdc = w3.eth.contract(
                address=Web3.to_checksum_address(conf.ETH_USDC_ADDRESS),
                abi=ERC20_BAL_ABI
            )
            bal = erc_usdc.functions.balanceOf(acct.address).call()
            if usdc_amt > bal:
                log.warning(f"Requested to spend {fmt_usdc(usdc_amt, USDC_DECIMALS)}, "
                            f"actual balance {fmt_usdc(bal, USDC_DECIMALS)} → adjust")
                usdc_amt = bal
                if usdc_amt == 0:
                    log.info("USDC balance is zero → skip this tick")
                    return

            # Re-quote clampatulla määrällä → saat oikean min_outin JA fee-hintin
            min_weth, exp_weth, quote_fee_bps = executor.quote_usdc_to_weth_min(
                usdc_amt,
                float(executor.calculate_dynamic_slippage(usdc_amt, USDC_DECIMALS))
            )
            slip_frac = executor.calculate_dynamic_slippage(usdc_amt, USDC_DECIMALS)
            slippage_bps = _to_bps(slip_frac)

            if min_weth <= 0:
                log.info("min_weth=0 (no liquidity?) → skip this tick")
                return

            if hasattr(executor, "debug_balances_and_allowances"):
                executor.debug_balances_and_allowances(acct)

            res = executor.swap_usdc_for_weth(acct, usdc_amt, min_weth, state=state)
            if res.tx_hash is None and res.amount_out == 0:
                log.info("approve pending → retry next tick")
                return


            # update state after the very first buy (use executed/fill price)
            fill_px = price_usdc_per_weth_from_amounts(usdc_amt, res.amount_out, USDC_DECIMALS)
            # Price impact (fill vs quote), bps
            pi_bps = compute_price_impact_bps("BUY", quoted_out=exp_weth, amount_in=usdc_amt, amount_out=res.amount_out,
                                              usdc_decimals=USDC_DECIMALS)
            log.info(f"[IMPACT] BUY  price_impact={pi_bps} bps (fill vs quote)")

            # --- Ledger v1.1 ---
            from bot.config import RouteInfo, QuoteInfo, FillInfo, make_ledger_entry
            eth_price_now = float(pricefeed.price_usdc_per_weth())  # USDC per 1 WETH ~ ETH

            impact_bps = compute_price_impact_bps("BUY", quoted_out=exp_weth, amount_in=usdc_amt,
                                                  amount_out=res.amount_out, usdc_decimals=USDC_DECIMALS)
            gas_meta, gas_info = _mk_gasinfo_from_result(res, eth_price_now)

            q = QuoteInfo(
                amount_in_base=int(usdc_amt),
                expected_out_base=int(exp_weth),
                min_out_base=int(min_weth),
                slippage_bps=slippage_bps,
                route=RouteInfo(best_fee_bps=int(quote_fee_bps)),
            )
            q.amount_in_human = fmt_usdc(usdc_amt, USDC_DECIMALS)
            q.expected_out_human = fmt_weth(exp_weth)
            q.min_out_human = fmt_weth(min_weth)

            f = FillInfo(
                tx_hash=getattr(res, "tx_hash", "") or "",
                amount_out_base=int(res.amount_out),
                amount_out_human=fmt_weth(res.amount_out),
                impact_bps=int(impact_bps),
                used_fee_bps=getattr(res, "used_fee", None),
                price_usdc_per_weth=float(fill_px),
                gas=gas_info,
            )

            entry = make_ledger_entry(
                side="BUY",
                pair="WETH/USDC",
                chain_id=conf.ETH_CHAIN_ID,
                network_profile=(conf.network_profile or "<none>"),
                router=executor.router.address,
                quoter=pricefeed.quoter.address,
                wallet=acct.address,
                quote=q,
                fill=f,
                price_usdc_per_weth=float(fill_px),
                min_out_base_used=getattr(res, "used_min_out", int(min_weth)),
                context={
                    "weth": weth, "usdc": usdc,
                    "paper": executor.paper,
                    "reason": "first_buy",
                    "settings_file": str(SETTINGS_FILE),
                },
            )
            append_ledger(entry.to_dict())
            # --- /Ledger ---

            state.last_buy_price = str(fill_px)       # executed price
            state.last_spent_usdc = str(usdc_amt)     # base USDC sent (base units)
            state.holding_weth = str(res.amount_out)  # wei actually received
            state.holding = True
            state.first_trade = False
            state.last_trade_ts = str(time.time())

            # pick randomized targets for this position (or use static as fallback)
            tp_active = _pick_from_range_or_default(tp_range, target)
            sl_active = _pick_from_range_or_default(sl_range, stop)
            state.target_pct_active = str(tp_active)
            state.stop_pct_active = str(sl_active)

            gas = _gas_meta_from_result(res)

            if not executor.paper and getattr(res, "tx_hash", None):
                _queue_gas_update(res.tx_hash)

            state.save(state_path)

            log.info(f"[TARGETS] picked tp_active={Decimal(state.target_pct_active):.3%} "
                     f"sl_active={Decimal(state.stop_pct_active):.3%} source=range")
            log_trade_result("BUY", quoted_out=exp_weth, min_out=min_weth, result=res)
            log.info(f"(diag) price={fill_px}")

        # --- Manage existing position -------------------------------------------
        if state.holding:
            tp_use = D(getattr(state, "target_pct_active", "0") or "0")
            sl_use = D(getattr(state, "stop_pct_active", "0") or "0")
            if tp_use == 0:
                tp_use = _pick_from_range_or_default(tp_range, target)
            if sl_use == 0:
                sl_use = _pick_from_range_or_default(sl_range, stop)

            last_buy = D(getattr(state, "last_buy_price", "0") or "0")
            if last_buy == 0:
                log.warning("holding=True but last_buy_price is zero/invalid → skip this tick")
                return
            # price already fetched above
            p = (price - last_buy) / last_buy

            weth_amt = int(D(getattr(state, "holding_weth", "0") or "0"))  # wei
            min_usdc, exp_usdc, quote_fee_bps = executor.quote_weth_to_usdc_min(
                weth_amt,
                float(executor.calculate_dynamic_slippage(weth_amt, 18))
            )
            slip_frac = executor.calculate_dynamic_slippage(weth_amt, 18)
            slippage_bps = _to_bps(slip_frac)

            if tp_use > 0 and p >= tp_use:
                state.target_pct_active = "0"
                state.stop_pct_active = "0"

                if hasattr(executor, "debug_balances_and_allowances"):
                    executor.debug_balances_and_allowances(acct)

                res = executor.swap_weth_for_usdc(acct, weth_amt, min_usdc, state=state)
                if res.tx_hash is None and res.amount_out == 0:
                    log.info("approve pending → retry next tick")
                    return

                profit = Decimal(res.amount_out) - Decimal(state.last_spent_usdc)  # base USDC
                fill_px = price_usdc_per_weth_from_amounts(res.amount_out, weth_amt, USDC_DECIMALS)
                px_str, diag = fill_price_debug(res.amount_out, weth_amt, USDC_DECIMALS)
                state.last_sell_price = str(fill_px)

                state.holding = False
                state.holding_weth = ""
                state.last_trade_ts = str(time.time())
                # UUSI: resetoi dual-tilat myynnin jälkeen
                state.rolling_high = state.last_sell_price  # aloita uusi huippu myynnin hinnasta
                state.reentry_reason = ""
                try:
                    ensure_ladder_progress(state, conf); state.ladder_progress = [False]*len(state.ladder_progress)
                except Exception: pass
                gas = _gas_meta_from_result(res)

                # --- Ledger v1.1 (SL hit) ---
                from bot.config import RouteInfo, QuoteInfo, FillInfo, make_ledger_entry
                eth_price_now = float(pricefeed.price_usdc_per_weth())

                impact_bps = compute_price_impact_bps("SELL", quoted_out=exp_usdc, amount_in=weth_amt,
                                                      amount_out=res.amount_out, usdc_decimals=USDC_DECIMALS)
                _, gas_info = _mk_gasinfo_from_result(res, eth_price_now)

                q = QuoteInfo(
                    amount_in_base=int(weth_amt),
                    expected_out_base=int(exp_usdc),
                    min_out_base=int(min_usdc),
                    slippage_bps=_to_bps(executor.calculate_dynamic_slippage(weth_amt, 18)),
                    route=RouteInfo(best_fee_bps=int(quote_fee_bps)),
                )
                q.amount_in_human = fmt_weth(weth_amt)
                q.expected_out_human = fmt_usdc(exp_usdc, USDC_DECIMALS)
                q.min_out_human = fmt_usdc(min_usdc, USDC_DECIMALS)

                f = FillInfo(
                    tx_hash=getattr(res, "tx_hash", "") or "",
                    amount_out_base=int(res.amount_out),
                    amount_out_human=fmt_usdc(res.amount_out, USDC_DECIMALS),
                    impact_bps=int(impact_bps),
                    used_fee_bps=getattr(res, "used_fee", None),
                    price_usdc_per_weth=float(fill_px),
                    gas=gas_info,
                )
                entry = make_ledger_entry(
                    side="SELL",
                    pair="WETH/USDC",
                    chain_id=conf.ETH_CHAIN_ID,
                    network_profile=(conf.network_profile or "<none>"),
                    router=executor.router.address,
                    quoter=pricefeed.quoter.address,
                    wallet=acct.address,
                    quote=q,
                    fill=f,
                    price_usdc_per_weth=float(fill_px),
                    min_out_base_used=getattr(res, "used_min_out", int(min_usdc)),
                    context={"weth": weth, "usdc": usdc, "paper": executor.paper, "reason": "sl_hit"},
                )
                append_ledger(entry.to_dict())

                state.save(state_path)

                # --- Ledger v1.1 ---
                from bot.config import RouteInfo, QuoteInfo, FillInfo, make_ledger_entry
                eth_price_now = float(pricefeed.price_usdc_per_weth())

                impact_bps = compute_price_impact_bps("SELL", quoted_out=exp_usdc, amount_in=weth_amt,
                                                      amount_out=res.amount_out, usdc_decimals=USDC_DECIMALS)
                gas_meta, gas_info = _mk_gasinfo_from_result(res, eth_price_now)

                q = QuoteInfo(
                    amount_in_base=int(weth_amt),
                    expected_out_base=int(exp_usdc),
                    min_out_base=int(min_usdc),
                    slippage_bps=slippage_bps,
                    route=RouteInfo(best_fee_bps=int(quote_fee_bps)),
                )
                q.amount_in_human = fmt_weth(weth_amt)
                q.expected_out_human = fmt_usdc(exp_usdc, USDC_DECIMALS)
                q.min_out_human = fmt_usdc(min_usdc, USDC_DECIMALS)

                f = FillInfo(
                    tx_hash=getattr(res, "tx_hash", "") or "",
                    amount_out_base=int(res.amount_out),
                    amount_out_human=fmt_usdc(res.amount_out, USDC_DECIMALS),
                    impact_bps=int(impact_bps),
                    used_fee_bps=getattr(res, "used_fee", None),
                    price_usdc_per_weth=float(fill_px),
                    gas=gas_info,
                )

                entry = make_ledger_entry(
                    side="SELL",
                    pair="WETH/USDC",
                    chain_id=conf.ETH_CHAIN_ID,
                    network_profile=(conf.network_profile or "<none>"),
                    router=executor.router.address,
                    quoter=pricefeed.quoter.address,
                    wallet=acct.address,
                    quote=q,
                    fill=f,
                    price_usdc_per_weth=float(fill_px),
                    min_out_base_used=getattr(res, "used_min_out", int(min_usdc)),
                    context={"weth": weth, "usdc": usdc, "paper": executor.paper, "reason": "tp_hit"},
                )
                append_ledger(entry.to_dict())
                # --- /Ledger ---

                # Price impact (fill vs quote), bps
                pi_bps = compute_price_impact_bps("SELL", quoted_out=exp_usdc, amount_in=weth_amt, amount_out=res.amount_out, usdc_decimals=USDC_DECIMALS)
                log.info(f"[IMPACT] SELL price_impact={pi_bps} bps (fill vs quote)")


                if executor.paper:
                    log_trade_result("SELL", quoted_out=exp_usdc, min_out=min_usdc, result=res)
                else:
                    log_trade_result("SELL", quoted_out=exp_usdc, min_out=min_usdc, result=res)
                    log.info(f"(diag) price={fill_px} ≈ {px_str} USDC/WETH [{diag}]")


            elif sl_use > 0 and p <= -sl_use:
                state.target_pct_active = "0"
                state.stop_pct_active = "0"

                res = executor.swap_weth_for_usdc(acct, weth_amt, min_usdc, state=state)
                if res.tx_hash is None and res.amount_out == 0:
                    log.info("approve pending → retry next tick")
                    return

                loss = Decimal(res.amount_out) - Decimal(state.last_spent_usdc)
                fill_px = price_usdc_per_weth_from_amounts(res.amount_out, weth_amt, USDC_DECIMALS)

                # Price impact (fill vs quote), bps
                pi_bps = compute_price_impact_bps("SELL", quoted_out=exp_usdc, amount_in=weth_amt,
                                                  amount_out=res.amount_out, usdc_decimals=USDC_DECIMALS)
                log.info(f"[IMPACT] SELL price_impact={pi_bps} bps (fill vs quote)")

                state.last_sell_price = str(fill_px)

                state.holding = False
                state.holding_weth = ""
                state.last_trade_ts = str(time.time())

                # UUSI: resetoi dual-tilat myynnin jälkeen
                state.rolling_high = state.last_sell_price
                state.reentry_reason = ""
                try:
                    ensure_ladder_progress(state, conf); state.ladder_progress = [False]*len(state.ladder_progress)
                except Exception: pass

                gas = _gas_meta_from_result(res)

                if not executor.paper and getattr(res, "tx_hash", None):
                    _queue_gas_update(res.tx_hash)

                state.save(state_path)
                log_trade_result("SELL", quoted_out=exp_usdc, min_out=min_usdc, result=res)
                px_str, diag = fill_price_debug(res.amount_out, weth_amt, USDC_DECIMALS)
                log.info(f"SL hit, profit_pct={p:.2%} (fill≈{px_str} USDC/WETH) [{diag}]")

        # --- Re-entry (dual-mode) tai legacy buy-back ----------------------------
        else:
            # Jos settingsissä on reentry_mode="dual", käytä pullback+breakout -logiikkaa
            if getattr(conf.trading, "reentry_mode", "dip") == "dual":
                # Normalisoi dual_reentry olio -> dict
                dre_obj = getattr(conf.trading, "dual_reentry", None)
                if dre_obj is not None and not isinstance(dre_obj, dict):
                    dre = {
                        "pullback_pct": str(getattr(dre_obj, "pullback_pct", "0.04")),
                        "breakout_pct": str(getattr(dre_obj, "breakout_pct", "0.015")),
                        "ladder_levels_pct": list(getattr(dre_obj, "ladder_levels_pct", []) or []),
                        "ladder_sizes": [str(x) for x in (getattr(dre_obj, "ladder_sizes", []) or [])],
                        "breakout_initial_allocation_pct": str(getattr(dre_obj, "breakout_initial_allocation_pct", "0.5"))
                    }
                else:
                    dre = dict(dre_obj or {})

                pullback_pct = _d(dre.get("pullback_pct", "0.04"))
                breakout_pct = _d(dre.get("breakout_pct", "0.015"))
                levels       = dre.get("ladder_levels_pct", ["0.015","0.03","0.045"])
                sizes_raw    = dre.get("ladder_sizes", [0.4, 0.3, 0.3])
                sizes        = [Decimal(str(x)) for x in sizes_raw]
                breakout_init= _d(dre.get("breakout_initial_allocation_pct", "0.5"))

                rolling_high = _d(state.rolling_high)
                last_sell    = _d(state.last_sell_price)

                pullback_trig = (rolling_high > 0 and price <= rolling_high * (Decimal(1) - pullback_pct))
                breakout_trig = (last_sell    > 0 and price >= last_sell    * (Decimal(1) + breakout_pct))

                # Loggaa rajat (jos päällä)
                if enabled("reentry"):
                    pb_price = (rolling_high * (Decimal(1) - pullback_pct)) if rolling_high > 0 else None
                    bo_price = (last_sell    * (Decimal(1) + breakout_pct)) if last_sell    > 0 else None
                    logging.getLogger("main").info(
                        f"[REENTRY] mode=dual price={price:.6f} rh={rolling_high if rolling_high>0 else None} "
                        f"pb_price={pb_price} bo_price={bo_price} trig={'pullback' if pullback_trig else ('breakout' if breakout_trig else None)}"
                    )

                if pullback_trig or breakout_trig:
                    # Laske "täysi allokaatio" USDC:ssä tämän tickin mukaan
                    raw_full = int(conf.trading.trade_amount_usdc)
                    full_usdc_amt = usdc_scale_from6(raw_full, USDC_DECIMALS)

                    # Clampataan saatavilla olevaan saldoon
                    erc_usdc = w3.eth.contract(
                        address=Web3.to_checksum_address(conf.ETH_USDC_ADDRESS),
                        abi=ERC20_BAL_ABI
                    )
                    bal = erc_usdc.functions.balanceOf(acct.address).call()
                    if full_usdc_amt > bal:
                        full_usdc_amt = bal
                        if full_usdc_amt == 0:
                            log.info("USDC balance is zero → skip dual re-entry")
                            return

                    spend_usdc = 0
                    reason = "pullback" if pullback_trig else "breakout"

                    if pullback_trig:
                        # Ladder-portaat: summataan osumat ja ostetaan kerralla yhteismäärällä
                        ensure_ladder_progress(state, conf)
                        for i, lvl in enumerate(levels):
                            # ohitetaan jo osuneet
                            if i < len(state.ladder_progress) and state.ladder_progress[i]:
                                continue
                            lvl_pct = _d(lvl)
                            if rolling_high == 0:
                                continue
                            lvl_price = rolling_high * (Decimal(1) - lvl_pct)
                            if price <= lvl_price:
                                # lisää osuus
                                part = sizes[i] if i < len(sizes) else Decimal(0)
                                spend_usdc += int(full_usdc_amt * part)
                                if i < len(state.ladder_progress):
                                    state.ladder_progress[i] = True
                                if enabled("ladder_hits"):
                                    log.info(f"[LADDER_HIT] rung={i} lvl={lvl_pct} lvl_price={lvl_price}")
                        if spend_usdc == 0:
                            # osuttiin pullback-triggereihin, mutta ei yhtään porrasta vielä alittunut
                            # ei osteta mitään tällä tikillä
                            wallet.rebalance_if_flat(state, executor, pricefeed)
                            return
                    else:
                        # breakout: osta esim. 50 % täyspositiosta
                        spend_usdc = int(full_usdc_amt * breakout_init)
                        # reset ladder uuteen trendiin
                        ensure_ladder_progress(state, conf)
                        state.ladder_progress = [False] * len(state.ladder_progress)

                    # Quote ja swap yhdellä tx:llä
                    min_weth, exp_weth, _ = executor.quote_usdc_to_weth_min(
                        spend_usdc,
                        float(executor.calculate_dynamic_slippage(spend_usdc, USDC_DECIMALS))
                    )
                    if min_weth <= 0:
                        log.info("min_weth=0 (no liquidity?) → skip dual re-entry tick")
                        return

                    if hasattr(executor, "debug_balances_and_allowances"):
                        executor.debug_balances_and_allowances(acct)

                    res = executor.swap_usdc_for_weth(acct, spend_usdc, min_weth, state=state)
                    if res.tx_hash is None and res.amount_out == 0:
                        log.info("approve pending → retry next tick")
                        return

                    fill_px = price_usdc_per_weth_from_amounts(spend_usdc, res.amount_out, USDC_DECIMALS)
                    pi_bps = compute_price_impact_bps("BUY", quoted_out=exp_weth, amount_in=spend_usdc,
                                                      amount_out=res.amount_out, usdc_decimals=USDC_DECIMALS)
                    log.info(f"[IMPACT] BUY  price_impact={pi_bps} bps (fill vs quote)")

                    # --- Ledger v1.1 (dual re-entry BUY) ---
                    from bot.config import RouteInfo, QuoteInfo, FillInfo, make_ledger_entry
                    eth_price_now = float(pricefeed.price_usdc_per_weth())
                    _, gas_info = _mk_gasinfo_from_result(res, eth_price_now)

                    q = QuoteInfo(
                        amount_in_base=int(spend_usdc),
                        expected_out_base=int(exp_weth),
                        min_out_base=int(min_weth),
                        slippage_bps=_to_bps(executor.calculate_dynamic_slippage(spend_usdc, USDC_DECIMALS)),
                        route=RouteInfo(best_fee_bps=0),
                    )
                    q.amount_in_human = fmt_usdc(spend_usdc, USDC_DECIMALS)
                    q.expected_out_human = fmt_weth(exp_weth)
                    q.min_out_human = fmt_weth(min_weth)

                    f = FillInfo(
                        tx_hash=getattr(res, "tx_hash", "") or "",
                        amount_out_base=int(res.amount_out),
                        amount_out_human=fmt_weth(res.amount_out),
                        impact_bps=int(pi_bps),
                        used_fee_bps=getattr(res, "used_fee", None),
                        price_usdc_per_weth=float(fill_px),
                        gas=gas_info,
                    )
                    entry = make_ledger_entry(
                        side="BUY",
                        pair="WETH/USDC",
                        chain_id=conf.ETH_CHAIN_ID,
                        network_profile=(conf.network_profile or "<none>"),
                        router=executor.router.address,
                        quoter=pricefeed.quoter.address,
                        wallet=acct.address,
                        quote=q,
                        fill=f,
                        price_usdc_per_weth=float(fill_px),
                        min_out_base_used=getattr(res, "used_min_out", int(min_weth)),
                        context={"weth": weth, "usdc": usdc, "paper": executor.paper, "reason": reason},
                    )
                    append_ledger(entry.to_dict())
                    # --- /Ledger ---

                    state.last_buy_price = str(fill_px)
                    state.last_spent_usdc = str(spend_usdc)
                    state.holding_weth = str(res.amount_out)
                    state.holding = True
                    state.first_trade = False
                    state.last_trade_ts = str(time.time())
                    state.reentry_reason = reason
                    # rolling_highia ei päivitetä LONG-tilassa

                    # Aseta TP/SL uuteen positioon (range tai scalar)
                    tp_active = _pick_from_range_or_default(tp_range, target)
                    sl_active = _pick_from_range_or_default(sl_range, stop)
                    state.target_pct_active = str(tp_active)
                    state.stop_pct_active = str(sl_active)

                    # kirjaa kauppa
                    gas = _gas_meta_from_result(res)

                    if not executor.paper and getattr(res, "tx_hash", None):
                        _queue_gas_update(res.tx_hash)

                    state.save(state_path)

                    fill_px_human = price_usdc_per_weth_from_amounts(spend_usdc, res.amount_out, USDC_DECIMALS)
                    log.info(f"[TARGETS] picked tp={tp_active:.3%} sl={sl_active:.3%} reason={reason}")
                    log_trade_result("BUY", quoted_out=exp_weth, min_out=min_weth, result=res)
                    log.info(f"(diag) price={fill_px} ≈ {fill_px_human} USDC/WETH")


                    return


            if state.last_sell_price:
                last_sell = D(getattr(state, "last_sell_price", "0") or "0")
                if last_sell == 0:
                    log.warning("last_sell_price invalid → skip buy-back check this tick")
                    return

                if enabled("reentry"):
                    dip_dbg = (last_sell - price) / last_sell
                    log.info(f"[REENTRY] last_sell={last_sell:.6f} price={price:.6f} "
                             f"dip={dip_dbg:.3%} target={target:.3%}")

                # has price fallen back by at least target% from the last sell?
                if (last_sell - price) / last_sell >= target:
                    raw = int(tcfg.trade_amount_usdc)  # human config in “USDC units”
                    usdc_amt = usdc_scale_from6(raw, USDC_DECIMALS)

                    erc_usdc = w3.eth.contract(
                        address=Web3.to_checksum_address(conf.ETH_USDC_ADDRESS),
                        abi=ERC20_BAL_ABI
                    )
                    bal = erc_usdc.functions.balanceOf(acct.address).call()
                    if usdc_amt > bal:
                        log.warning(
                            f"Requested to spend {fmt_usdc(usdc_amt, USDC_DECIMALS)}, "
                            f"actual balance {fmt_usdc(bal, USDC_DECIMALS)} → adjust"
                        )

                        usdc_amt = bal
                        if usdc_amt == 0:
                            log.info("USDC balance is zero → skip this tick")
                            return

                    # min_weth, exp_weth, fee = executor.quote_usdc_to_weth_min(usdc_amt, float(executor.calculate_dynamic_slippage(usdc_amt)))
                    min_weth, exp_weth, _ = executor.quote_usdc_to_weth_min(
                        usdc_amt,
                        float(executor.calculate_dynamic_slippage(usdc_amt, USDC_DECIMALS))
                    )

                    if min_weth <= 0:
                        log.info("min_weth=0 (no liquidity?) → skip this tick")
                        return

                    if hasattr(executor, "debug_balances_and_allowances"):
                        executor.debug_balances_and_allowances(acct)

                    res = executor.swap_usdc_for_weth(acct, usdc_amt, min_weth, state=state)
                    if res.tx_hash is None and res.amount_out == 0:
                        log.info("approve pending → retry next tick")
                        return

                    fill_px = price_usdc_per_weth_from_amounts(usdc_amt, res.amount_out, USDC_DECIMALS)
                    pi_bps = compute_price_impact_bps("BUY", quoted_out=exp_weth, amount_in=usdc_amt, amount_out=res.amount_out, usdc_decimals=USDC_DECIMALS)
                    log.info(f"[IMPACT] BUY  price_impact={pi_bps} bps (fill vs quote)")

                    # --- Ledger v1.1 (legacy dip re-entry BUY) ---
                    from bot.config import RouteInfo, QuoteInfo, FillInfo, make_ledger_entry
                    eth_price_now = float(pricefeed.price_usdc_per_weth())
                    _, gas_info = _mk_gasinfo_from_result(res, eth_price_now)

                    q = QuoteInfo(
                        amount_in_base=int(usdc_amt),
                        expected_out_base=int(exp_weth),
                        min_out_base=int(min_weth),
                        slippage_bps=_to_bps(executor.calculate_dynamic_slippage(usdc_amt, USDC_DECIMALS)),
                        route=RouteInfo(best_fee_bps=0),
                    )
                    q.amount_in_human = fmt_usdc(usdc_amt, USDC_DECIMALS)
                    q.expected_out_human = fmt_weth(exp_weth)
                    q.min_out_human = fmt_weth(min_weth)

                    f = FillInfo(
                        tx_hash=getattr(res, "tx_hash", "") or "",
                        amount_out_base=int(res.amount_out),
                        amount_out_human=fmt_weth(res.amount_out),
                        impact_bps=int(pi_bps),
                        used_fee_bps=getattr(res, "used_fee", None),
                        price_usdc_per_weth=float(fill_px),
                        gas=gas_info,
                    )
                    entry = make_ledger_entry(
                        side="BUY",
                        pair="WETH/USDC",
                        chain_id=conf.ETH_CHAIN_ID,
                        network_profile=(conf.network_profile or "<none>"),
                        router=executor.router.address,
                        quoter=pricefeed.quoter.address,
                        wallet=acct.address,
                        quote=q,
                        fill=f,
                        price_usdc_per_weth=float(fill_px),
                        min_out_base_used=getattr(res, "used_min_out", int(min_weth)),
                        context={"weth": weth, "usdc": usdc, "paper": executor.paper, "reason": "buyback"},
                    )
                    append_ledger(entry.to_dict())
                    # --- /Ledger ---

                    state.last_buy_price = str(fill_px)
                    state.last_spent_usdc = str(usdc_amt)   # base units
                    state.holding_weth = str(res.amount_out)  # wei
                    state.holding = True
                    state.first_trade = False
                    state.last_trade_ts = str(time.time())

                    tp_active = _pick_from_range_or_default(tp_range, target)
                    sl_active = _pick_from_range_or_default(sl_range, stop)
                    state.target_pct_active = str(tp_active)
                    state.stop_pct_active = str(sl_active)
                    gas = _gas_meta_from_result(res)


                    if not executor.paper and getattr(res, "tx_hash", None):
                        _queue_gas_update(res.tx_hash)

                    state.save(state_path)
                    log.info(f"[TARGETS] picked tp={tp_active:.3%} sl={sl_active:.3%} for this position")

                    fill_px_human = price_usdc_per_weth_from_amounts(usdc_amt, res.amount_out, USDC_DECIMALS)
                    log_trade_result("BUY", quoted_out=exp_weth, min_out=min_weth, result=res)
                    log.info(f"(diag) price={fill_px} ≈ {fill_px_human} USDC/WETH")

                    return

                wallet.rebalance_if_flat(state, executor, pricefeed)

    return on_tick, conf

if __name__ == "__main__":
    on_tick, conf = build()
    try:
        while True:
            on_tick()
            time.sleep(conf.ETH_CYCLE_SLEEP)
    except KeyboardInterrupt:
        pass