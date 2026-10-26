from __future__ import annotations
import os, json, pathlib, time
from datetime import datetime, timezone
from typing import Optional, List, Literal, Dict, Any
from decimal import Decimal

from dataclasses import dataclass, asdict, field
from pydantic import BaseModel, Field
from web3 import Web3

from bot.log import setup_logging
from bot.fmt import (
    usdc_human_to_base, usdc_base_to_human,
    eth_to_wei, wei_to_eth,
)

LEDGER_SCHEMA_VERSION = "1.1"
STATE_SCHEMA_VERSION = "1.1"

PairSide = Literal["BUY", "SELL"]

# Sallitut fee-tierit bps: 100 = 0.01%, 500 = 0.05%, 3000 = 0.3%, 10000 = 1%
DEFAULT_FEE_TIERS_BPS = (100, 500, 3000, 10000)

# ---------- helpers ----------
def ms_now() -> int:
    return int(time.time() * 1000)

def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _as_json(d: Dict[str, Any]) -> Dict[str, Any]:
    """Muuta dataclass -> JSON-kelpoinen dict (Decimal -> str, poista None:t)."""
    out = {}
    for k, v in d.items():
        if v is None:
            continue
        if isinstance(v, Decimal):
            out[k] = format(v, "f")
        elif isinstance(v, dict):
            out[k] = _as_json(v)
        elif isinstance(v, list):
            out[k] = [
                _as_json(x) if isinstance(x, dict) else (format(x, "f") if isinstance(x, Decimal) else x)
                for x in v
            ]
        else:
            out[k] = v
    return out


def _dee(x) -> Decimal:
    if x is None: return Decimal(0)
    if isinstance(x, int): return Decimal(x)
    if isinstance(x, float): return Decimal(str(x))
    s = str(x).strip().replace(",", "")
    if s.endswith("%"): return Decimal(s[:-1].strip()) / Decimal(100)
    return Decimal(s)

def _parse_pct(val) -> Decimal:
    x = _dee(val)
    return x / Decimal(100) if x > 1 else x

def _checksum(addr: Optional[str]) -> Optional[str]:
    if not addr: return addr
    try:
        return Web3.to_checksum_address(addr)
    except Exception:
        raise SystemExit(f"[CONFIG] Invalid EVM address in settings: {addr!r}")

def _parse_pct_range(seq) -> Optional[list[str]]:
    if not seq or not isinstance(seq, (list, tuple)) or len(seq) != 2:
        return None
    a = str(_parse_pct(seq[0])); b = str(_parse_pct(seq[1]))
    return [a, b] if float(a) <= float(b) else [b, a]

def _parse_pct_list(seq) -> Optional[list[str]]:
    if not seq or not isinstance(seq, (list, tuple)): return None
    return [str(_parse_pct(v)) for v in seq]


# ---------- paths ----------
APP_NAME = os.getenv("APP_NAME", "cryptobot")
APP_DATA_DIR = os.getenv("APP_DATA_DIR")
xdg_base = pathlib.Path(os.getenv("XDG_DATA_HOME", pathlib.Path.home() / ".local" / "share"))

# Käytä aina cryptobot-kansiota (tai APP_DATA_DIR), älä fallbackkaa vanhaan "chatbot":iin
DATA_DIR = pathlib.Path(APP_DATA_DIR).resolve() if APP_DATA_DIR else (xdg_base / APP_NAME)
DATA_DIR.mkdir(parents=True, exist_ok=True)

SETTINGS_FILE = DATA_DIR / "settings.json"

# ---------- models ----------
class DualReentryCfg(BaseModel):
    pullback_pct: Decimal = Decimal("0.04")
    breakout_pct: Decimal = Decimal("0.015")
    ladder_levels_pct: Optional[List[str]] = None
    ladder_sizes: Optional[List[Decimal]] = None
    breakout_initial_allocation_pct: Decimal = Decimal("0.50")

class RiskCfg(BaseModel):
    atr_lookback: int = 60
    tp_atr_mult: Decimal = Decimal("1.2")
    sl_atr_mult: Decimal = Decimal("0.8")
    tp_min_floor_pct: Decimal = Decimal("0.015")
    sl_min_floor_pct: Decimal = Decimal("0.0075")
    fee_roundtrip_pct: Decimal = Decimal("0.006")

class TradingCfg(BaseModel):
    target_profit_pct: Decimal = Decimal("0.05")
    stop_loss_pct: Decimal = Decimal("0.02")
    target_profit_pct_range: Optional[List[str]] = None
    stop_loss_pct_range: Optional[List[str]] = None
    pick_mode: str = "mid"
    reentry_mode: str = "dip"
    recovery_rebuy_pct: Decimal = Decimal("0.02")
    dual_reentry: Optional[DualReentryCfg] = None

    max_slippage_pct: Decimal = Decimal("0.005")
    max_slippage_limit_pct: Decimal = Decimal("0.01")
    slippage_step_pct: Decimal = Decimal("0.0001")
    max_price_impact_pct: Decimal = Decimal("0.0001")
    max_price_impact_limit_pct: Decimal = Decimal("0.0005")
    price_impact_step_pct: Decimal = Decimal("0.00005")

    # Ihmislukuinen + base (base lasketaan loaderissa)
    trade_amount_usdc_human: float = 50.0
    trade_amount_usdc: int = int(50.0 * 10**6)

    min_time_between_trades_sec: int = 300
    min_trade_interval_seconds: int = 300
    min_cooldown_sec: int = 1800
    price_stabilization_threshold_pct: Decimal = Decimal("0.005")
    price_stabilization_wait_seconds: int = 600

class WalletCfg(BaseModel):
    min_eth_gas_buffer_wei: int = int(0.02 * 10**18)
    eth_topup_target_wei: int = int(0.05 * 10**18)
    min_eth_gas_buffer_eth_human: Optional[Decimal] = None
    eth_topup_target_eth_human: Optional[Decimal] = None
    wrap_excess_eth_when_flat: bool = True
    rebalance_cooldown_seconds: int = 300

class AppCfg(BaseModel):
    testrun: bool = True
    network_profile: Optional[str] = None
    profiles: Optional[Dict[str, Any]] = None

    ETH_CHAIN_ID: int = 1
    W3_CLIENT_URL: str  # aina ENV:stä

    ETH_WETH_ADDRESS: str
    ETH_USDC_ADDRESS: str
    ETH_V3_SWAPROUTER02_ADDRESS: str
    ETH_V3_QUOTERV2_ADDRESS: str

    ETH_PERMIT2_ADDRESS: Optional[str] = None
    ETH_UNIVERSAL_ROUTER_ADDRESS: Optional[str] = None
    UNISWAP_V3_FEE_TIERS: List[int] = Field(default_factory=lambda: [3000])

    ETH_FACTORY_ADDRESS: Optional[str] = None
    ETH_USDC_POOL_V2: Optional[str] = None
    ETH_ROUTER_ADDRESS: Optional[str] = None

    ETH_STATE_FILE: str = str(DATA_DIR / "state.json")
    ETH_GAS_PRICE_MULTIPLIER: float = 1.1
    ETH_MIN_GAS_LIMIT: int = 500_000
    ETH_MAX_GAS_BUFFER_PCT: int = 20
    ETH_RPC_TIMEOUT: int = 30
    ETH_CYCLE_SLEEP: int = 60

    LEDGER_FILE: str = str(DATA_DIR / "ledger.jsonl")

    risk: RiskCfg = RiskCfg()
    trading: TradingCfg
    wallet: WalletCfg = WalletCfg()

@dataclass
class RouteInfo:
    best_fee_bps: int
    # Uniswap V3 voi reitittää monen poolin läpi; säilytetään yksinkertainen lista
    # esim. ["USDC/ETH@500", "ETH/DAI@3000"]
    pools: Optional[List[str]] = None

    def to_dict(self) -> Dict[str, Any]:
        return _as_json(asdict(self))


@dataclass
class QuoteInfo:
    # per direction quote
    amount_in_base: int
    expected_out_base: int
    min_out_base: int
    slippage_bps: int
    route: RouteInfo

    # ihmismuotoiset kentät lisätään kirjoitushetkellä (fmt.py hoitaa)
    amount_in_human: Optional[str] = None
    expected_out_human: Optional[str] = None
    min_out_human: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return _as_json(asdict(self))


@dataclass
class GasInfo:
    gas_used: int
    gas_price_wei: int
    gas_cost_wei: int
    # Valinnainen: arvohetken ETH→USDC – jos lisäät tämän, voidaan laskea gas_cost_usdc
    eth_price_usdc: Optional[float] = None
    gas_cost_usdc: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return _as_json(asdict(self))


@dataclass
class FillInfo:
    tx_hash: str
    amount_out_base: int
    # vaikutus suhteessa quoteen: >0 = fill parempi kuin quote, <0 = huonompi
    impact_bps: Optional[int] = None
    # Toteutunut käytetty fee-tier (bps)
    used_fee_bps: Optional[int] = None
    block_number: Optional[int] = None
    price_usdc_per_weth: Optional[float] = None  # toteumahinta diag purposes
    gas: Optional[GasInfo] = None

    # Ihmismuoto
    amount_out_human: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return _as_json(asdict(self))


@dataclass
class LedgerEntry:
    schema_version: str
    ts_ms: int
    ts_iso: str
    side: PairSide                     # "BUY" tai "SELL"
    pair: str                          # esim. "WETH/USDC"
    chain_id: int
    network_profile: str
    router: str
    quoter: str
    wallet: Optional[str] = None

    quote: Optional[QuoteInfo] = None
    fill: Optional[FillInfo] = None

    # Hintadiagnostiikka
    price_usdc_per_weth: Optional[float] = None

    # PnL (täytetään yleensä myynnillä; osto voi olla 0)
    realized_pnl_usdc: Optional[float] = None
    realized_pnl_pct: Optional[float] = None
    cost_basis_usdc_per_weth: Optional[float] = None

    # Lisäkenttiä forensiikkaan
    min_out_base_used: Optional[int] = None
    # Kirjataan myös ympäristö (hyödyllinen moniverkkotesteissä)
    context: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # upota alidatat JSON-kelpoisiksi
        if self.quote is not None:
            d["quote"] = self.quote.to_dict()
        if self.fill is not None:
            d["fill"] = self.fill.to_dict()
        d["schema_version"] = self.schema_version or LEDGER_SCHEMA_VERSION
        return _as_json(d)

def make_ledger_entry(
    *,
    side: PairSide,
    pair: str,
    chain_id: int,
    network_profile: str,
    router: str,
    quoter: str,
    wallet: Optional[str],
    quote: Optional[QuoteInfo],
    fill: Optional[FillInfo],
    price_usdc_per_weth: Optional[float],
    min_out_base_used: Optional[int],
    context: Optional[Dict[str, Any]] = None,
) -> LedgerEntry:
    return LedgerEntry(
        schema_version=LEDGER_SCHEMA_VERSION,
        ts_ms=ms_now(),
        ts_iso=iso_now(),
        side=side,
        pair=pair,
        chain_id=chain_id,
        network_profile=network_profile,
        router=router,
        quoter=quoter,
        wallet=wallet,
        quote=quote,
        fill=fill,
        price_usdc_per_weth=price_usdc_per_weth,
        min_out_base_used=min_out_base_used,
        context=context or {},
    )

# ---------- main loader ----------
def load_config() -> AppCfg:
    # 1) pakota käyttäjää luomaan settings.json
    if not SETTINGS_FILE.exists():
        raise SystemExit(
            f"[CONFIG] {SETTINGS_FILE} is missing. Copy and edit examplesettings.json → {SETTINGS_FILE}"
        )

    # 2) lue settings ja validoi json
    try:
        settings = json.loads(SETTINGS_FILE.read_text())
    except Exception as e:
        raise SystemExit(f"[CONFIG] settings.json parse/load failed at {SETTINGS_FILE}: {e}")

    # paper → testrun
    if "paper" in settings:
        settings["testrun"] = bool(settings["paper"])

    # 3) profile override (EI RPC:ta profiilista)
    prof = settings.get("network_profile")
    if prof:
        profiles = settings.get("profiles")
        if not isinstance(profiles, dict):
            raise SystemExit(f"[CONFIG] 'profiles' is not a dict in {SETTINGS_FILE}")
        if prof not in profiles:
            raise SystemExit(f"[CONFIG] Unknown network_profile '{prof}'. "
                             f"Available: {', '.join(profiles.keys())}")
        net = profiles[prof]
        if not isinstance(net, dict) or not net:
            raise SystemExit(f"[CONFIG] Profile '{prof}' is empty or invalid")

        # kopioi vain verkkoavaimet (ei RPC)
        for k in [
            "ETH_CHAIN_ID",
            "ETH_WETH_ADDRESS", "ETH_USDC_ADDRESS",
            "ETH_V3_SWAPROUTER02_ADDRESS", "ETH_V3_QUOTERV2_ADDRESS",
            "ETH_PERMIT2_ADDRESS", "ETH_UNIVERSAL_ROUTER_ADDRESS",
            "UNISWAP_V3_FEE_TIERS",
        ]:
            if k in net and net[k] not in (None, ""):
                settings[k] = net[k]

        try:
            settings["ETH_CHAIN_ID"] = int(settings["ETH_CHAIN_ID"])
        except Exception as e:
            raise SystemExit(f"[CONFIG] Profile '{prof}' has non-integer ETH_CHAIN_ID: {settings.get('ETH_CHAIN_ID')} ({e})")

    # 4) ENV-override (RPC pakollinen ENV:stä)
    env_rpc = os.getenv("W3_CLIENT_URL")
    env_chain = os.getenv("ETH_CHAIN_ID")

    if env_chain:
        try:
            settings["ETH_CHAIN_ID"] = int(env_chain)
        except Exception as e:
            raise SystemExit(f"[CONFIG] ENV ETH_CHAIN_ID invalid: {env_chain} ({e})")
        print(f"[CFG] (env) ETH_CHAIN_ID={settings['ETH_CHAIN_ID']}")

    if env_rpc:
        settings["W3_CLIENT_URL"] = env_rpc
        print(f"[CFG] (env) W3_CLIENT_URL={env_rpc}")
    else:
        # jos settingsiin on jäänyt RPC, estä käyttö
        if settings.get("W3_CLIENT_URL"):
            raise SystemExit(
                "[CONFIG] W3_CLIENT_URL must be provided via environment variable. "
                "Remove it from settings.json and export W3_CLIENT_URL in your shell."
            )
        raise SystemExit("[CONFIG] W3_CLIENT_URL is not set. Export it in your environment.")

    # 5) checksum osoitteille
    for key in [
        "ETH_WETH_ADDRESS", "ETH_USDC_ADDRESS",
        "ETH_V3_SWAPROUTER02_ADDRESS", "ETH_V3_QUOTERV2_ADDRESS",
        "ETH_PERMIT2_ADDRESS", "ETH_UNIVERSAL_ROUTER_ADDRESS",
        "ETH_FACTORY_ADDRESS", "ETH_USDC_POOL_V2", "ETH_ROUTER_ADDRESS",
    ]:
        if key in settings:
            settings[key] = _checksum(settings.get(key))

    # 6) trading normalisointi
    t = dict(settings.get("trading") or {})

    def _set_pct(key: str, default_value: str):
        v = t.get(key, default_value); t[key] = str(_parse_pct(v))

    for k, dv in [
        ("target_profit_pct", "5%"),  # 0.05
        ("stop_loss_pct", "2%"),  # 0.02
        ("max_slippage_pct", "0.5%"),  # 0.005
        ("max_slippage_limit_pct", "1%"),  # 0.01
        ("slippage_step_pct", "0.01%"),  # 0.0001
        ("max_price_impact_pct", "0.01%"),  # 0.0001
        ("max_price_impact_limit_pct", "0.05%"),  # 0.0005
        ("price_impact_step_pct", "0.005%"),  # 0.00005
        ("price_stabilization_threshold_pct", "0.5%"),  # 0.005
    ]:
        _set_pct(k, dv)

    t["target_profit_pct_range"] = _parse_pct_range(t.get("target_profit_pct_range"))
    t["stop_loss_pct_range"] = _parse_pct_range(t.get("stop_loss_pct_range"))
    t["pick_mode"] = (t.get("pick_mode") or "mid").lower()
    t["reentry_mode"] = (t.get("reentry_mode") or "dip").lower()
    t["recovery_rebuy_pct"] = str(_parse_pct(t.get("recovery_rebuy_pct", "2%")))
    t["min_time_between_trades_sec"] = int(t.get("min_time_between_trades_sec", t.get("min_trade_interval_seconds", 300)))
    t["min_trade_interval_seconds"] = t["min_time_between_trades_sec"]
    t["min_cooldown_sec"] = int(t.get("min_cooldown_sec", 1800))

    dre = dict(t.get("dual_reentry") or {})
    if dre:
        t["dual_reentry"] = {
            "pullback_pct": str(_parse_pct(dre.get("pullback_pct", "4%"))),
            "breakout_pct": str(_parse_pct(dre.get("breakout_pct", "1.5%"))),
            "ladder_levels_pct": _parse_pct_list(dre.get("ladder_levels_pct") or ["1.5%", "3%", "4.5%"]),
            "ladder_sizes": [Decimal(str(x)) for x in dre.get("ladder_sizes", [0.4, 0.3, 0.3])],
            "breakout_initial_allocation_pct": str(_parse_pct(dre.get("breakout_initial_allocation_pct", "50%")))
        }
    else:
        t["dual_reentry"] = None

    # trade amount: human↔base (USDC 6)
    human = t.get("trade_amount_usdc_human", None)
    legacy_base = t.get("trade_amount_usdc", None)
    if human is not None:
        base_val = usdc_human_to_base(human, usdc_decimals=6)
        t["trade_amount_usdc"] = base_val
        t["trade_amount_usdc_human"] = str(Decimal(str(human)))
        print(f"[CFG] trading.amount: human={t['trade_amount_usdc_human']} USDC -> base={base_val}")
    elif legacy_base is not None:
        base_val = int(legacy_base)
        t["trade_amount_usdc"] = base_val
        t["trade_amount_usdc_human"] = str(usdc_base_to_human(base_val, usdc_decimals=6))
        print(f"[CFG] trading.amount: base={base_val} -> human={t['trade_amount_usdc_human']} USDC")
    else:
        base_val = usdc_human_to_base("50.0", usdc_decimals=6)
        t["trade_amount_usdc_human"] = "50.0"
        t["trade_amount_usdc"] = base_val
        print(f"[CFG] trading.amount: default human=50.0 USDC -> base={base_val}")

    settings["trading"] = t

    # 6b) risk-lohko
    r = dict(settings.get("risk") or {})
    def _risk_pct(key: str, default_value: str):
        r[key] = str(_parse_pct(r.get(key, default_value)))
    r["atr_lookback"] = int(r.get("atr_lookback", 60))
    r["tp_atr_mult"] = Decimal(str(r.get("tp_atr_mult", "1.2")))
    r["sl_atr_mult"] = Decimal(str(r.get("sl_atr_mult", "0.8")))
    _risk_pct("tp_min_floor_pct", "1.5%")
    _risk_pct("sl_min_floor_pct", "0.75%")
    _risk_pct("fee_roundtrip_pct", "0.6%")
    settings["risk"] = r

    # fee tiers ints (varmistus)
    tiers = settings.get("UNISWAP_V3_FEE_TIERS", [3000])
    settings["UNISWAP_V3_FEE_TIERS"] = [int(x) for x in tiers]

    # 7) required keys
    required = [
        "ETH_CHAIN_ID", "W3_CLIENT_URL",
        "ETH_WETH_ADDRESS", "ETH_USDC_ADDRESS",
        "ETH_V3_SWAPROUTER02_ADDRESS", "ETH_V3_QUOTERV2_ADDRESS"
    ]
    missing = [k for k in required if not settings.get(k)]
    if missing:
        raise SystemExit(f"[CONFIG] Missing required network keys after profile resolution: {missing}")

    # 8) wallet: human↔wei
    w = dict(settings.get("wallet") or {})

    buf_h, buf_w = w.get("min_eth_gas_buffer_eth_human"), w.get("min_eth_gas_buffer_wei")
    if buf_h is not None:
        w["min_eth_gas_buffer_wei"] = eth_to_wei(buf_h)
        w["min_eth_gas_buffer_eth_human"] = str(Decimal(str(buf_h)))
    elif buf_w is not None:
        w["min_eth_gas_buffer_wei"] = int(buf_w)
        w["min_eth_gas_buffer_eth_human"] = str(wei_to_eth(buf_w))
    else:
        w["min_eth_gas_buffer_eth_human"] = "0.02"
        w["min_eth_gas_buffer_wei"] = eth_to_wei("0.02")

    top_h, top_w = w.get("eth_topup_target_eth_human"), w.get("eth_topup_target_wei")
    if top_h is not None:
        w["eth_topup_target_wei"] = eth_to_wei(top_h)
        w["eth_topup_target_eth_human"] = str(Decimal(str(top_h)))
    elif top_w is not None:
        w["eth_topup_target_wei"] = int(top_w)
        w["eth_topup_target_eth_human"] = str(wei_to_eth(top_w))
    else:
        w["eth_topup_target_eth_human"] = "0.05"
        w["eth_topup_target_wei"] = eth_to_wei("0.05")

    settings["wallet"] = w

    # 9) info + logging
    print(f"[CFG] profile={settings.get('network_profile') or '<none>'} "
          f"chain_id={settings.get('ETH_CHAIN_ID')} RPC={settings.get('W3_CLIENT_URL')}")

    try:
        setup_logging(settings.get("debug", {}))
    except Exception as e:
        print(f"[CFG] warning: logging setup failed: {e}")

    return AppCfg.model_validate(settings)

__all__ = [
    "load_config", "DATA_DIR", "SETTINGS_FILE",
    "RouteInfo", "QuoteInfo", "GasInfo", "FillInfo", "LedgerEntry",
    "make_ledger_entry",
    "LEDGER_SCHEMA_VERSION", "STATE_SCHEMA_VERSION",
]
