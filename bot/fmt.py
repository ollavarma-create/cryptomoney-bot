# bot/fmt.py
from __future__ import annotations
from decimal import Decimal

__all__ = [
    # formatterit
    "human_amount",
    "fmt_token",
    "fmt_weth",
    "fmt_usdc",
    "price_usdc_per_weth_from_amounts",
    "fill_price_debug",
    # konversiot (USDC)
    "usdc_human_to_base",
    "usdc_base_to_human",
    "usdc_scale_from6",
    # konversiot (ETH/WETH/wei/gwei)
    "eth_to_wei",
    "wei_to_eth",
    "gwei_to_wei",
    "wei_to_gwei",
]
# ---------- constants ----------
USDC_DECIMALS_DEFAULT = 6
WEI_DECIMALS = 18

# ---------- helpers ----------
D = lambda x: Decimal(str(x))

def pct_to_decimal(x) -> Decimal:
    """
    "2%" -> 0.02, "1.5%" -> 0.015, 2 -> 0.02, "0.5" -> 0.5 (jos alle 1 ei-loppuu-%:lla, tulkitaan jo desimaaliksi)
    """
    s = str(x).strip()
    if s.endswith("%"):
        return D(s[:-1]) / Decimal(100)
    v = D(s or "0")
    return v/Decimal(100) if v >= 1 else v


def _to_dec(x) -> Decimal:
    if isinstance(x, Decimal):
        return x
    if isinstance(x, (int, str)):
        return Decimal(x)
    # float → käytä str(x), ettei tule binääripyöristyksiä
    return Decimal(str(x))

def _price_usdc_per_weth(usdc_base: int, weth_wei: int, usdc_decimals: int) -> Decimal:
    """Laskee USDC per 1 WETH täsmälleen base-määristä (fill-hinta)."""
    if weth_wei <= 0:
        return Decimal(0)
    usdc = Decimal(usdc_base) / (10 ** usdc_decimals)
    weth = Decimal(weth_wei) / (10 ** 18)
    return usdc / weth

# ---------- conversions (keskitetty paikka) ----------
def usdc_scale_from6(raw_6: int, usdc_decimals: int) -> int:
    if usdc_decimals > 6:
        return raw_6 * (10 ** (usdc_decimals - 6))
    if usdc_decimals < 6:
        return raw_6 // (10 ** (6 - usdc_decimals))
    return raw_6

def usdc_human_to_base(amount_human: float | str | Decimal, usdc_decimals: int) -> int:
    return int(D(amount_human) * (Decimal(10) ** usdc_decimals))

def usdc_base_to_human(amount_base: int, usdc_decimals: int) -> Decimal:
    return (Decimal(amount_base) / (Decimal(10) ** usdc_decimals)).quantize(Decimal("0.000001"))

def eth_to_wei(eth_human: float | str | Decimal) -> int:
    return int(D(eth_human) * Decimal(10**18))

def wei_to_eth(wei: int) -> Decimal:
    return (Decimal(wei) / Decimal(10**18)).quantize(Decimal("0.0000000001"))
# gwei/wei apurit
def gwei_to_wei(gwei: int | float | str | Decimal) -> int:
    d = _to_dec(gwei) * (10 ** 9)
    return int(d.to_integral_value(rounding="ROUND_DOWN"))

def wei_to_gwei(wei: int | str | Decimal) -> str:
    d = _to_dec(wei) / (10 ** 9)
    return f"{d:.6f}"

# ---------- public API ----------
def fill_price_debug(usdc_base: int, weth_wei: int, usdc_decimals: int) -> tuple[str, str]:
    """
    Palauttaa (price_str, diag_str), missä price_str = USDC per 1 WETH.
    Diag-str näyttää selvästi käytetyt ihmismuotoiset määrät.
    """
    px = _price_usdc_per_weth(usdc_base, weth_wei, usdc_decimals)
    if px == 0:
        return "n/a", "weth_wei<=0"
    usdc = Decimal(usdc_base) / (10 ** usdc_decimals)
    weth = Decimal(weth_wei) / (10 ** 18)
    diag = f"usdc={usdc:.6f} (base={usdc_base}, dec={usdc_decimals}) | weth={weth:.9f} (wei={weth_wei})"
    return f"{px:.6f}", diag

def human_amount(amount: int | float | str | Decimal, decimals: int, ndigits: int = 6) -> str:
    """Ihmismuotoinen string ilman symbolia."""
    q = _to_dec(amount) / (10 ** decimals)
    return f"{q:.{ndigits}f}"

def fmt_token(amount: int, decimals: int, symbol: str, ndigits: int = 6) -> str:
    """Esim. fmt_token(1234567, 6, 'USDC') → '1.234567 USDC (1234567 base)'"""
    return f"{human_amount(amount, decimals, ndigits)} {symbol} ({amount} base)"

def fmt_weth(wei: int, ndigits: int = 8) -> str:
    return f"{human_amount(wei, 18, ndigits)} WETH ({wei} wei)"

def fmt_usdc(base: int, usdc_decimals: int = 6, ndigits: int = 6) -> str:
    return fmt_token(base, usdc_decimals, "USDC", ndigits)

def price_usdc_per_weth_from_amounts(
    usdc_base: int,
    weth_wei: int,
    usdc_decimals: int,
    ndigits: int = 6
) -> str:
    """Toteutunut hinta (USDC per 1 WETH) annetuista määristä."""
    px = _price_usdc_per_weth(usdc_base, weth_wei, usdc_decimals)
    if px == 0:
        return "n/a"
    return f"{px:.{ndigits}f}"
