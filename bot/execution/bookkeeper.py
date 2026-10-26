from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any, Iterable
from pathlib import Path
from decimal import Decimal, getcontext
import json
import os

# Desimaalilaskenta tarkaksi
getcontext().prec = 28

# Huom: emme tuo LEDGER_FILE:ä symbolina (sitä ei eksportata),
# vaan luemme sen load_config():n kautta, jolloin käytetään
# settings.json:ssa määriteltyä arvoa.
try:
    # ensisijainen nimiavaruus
    from bot.config import load_config
except ImportError:
    # fallback projektijuureen
    from bot.config import load_config # type: ignore

# lisää top-leveliin (importtien jälkeen)
def append_ledger(row: Dict[str, Any], path: Optional[Path | str] = None) -> None:
    """
    Kirjoita yksi JSON-rivi ledgeriin (uusi 1.1-skeema).
    Ei yritä muuntaa 'Trade'ksi.
    """
    if path is None:
        cfg = load_config()
        p = Path(cfg.LEDGER_FILE)
    else:
        p = Path(path)

    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + os.linesep)


@dataclass
class Trade:
    ts: float                        # epoch seconds
    side: str                        # "BUY" or "SELL"
    token_in: str                    # token spent (esim. "USDC")
    token_out: str                   # token received (esim. "WETH")
    amount_in: str                   # Decimal as str (token_in units)
    amount_out: str                  # Decimal as str (token_out units)
    price: str                       # per-unit price (token_out per token_in TAI quote/base) selitystä alla
    tx_hash: Optional[str] = None
    gas_used: Optional[int] = None
    gas_price_gwei: Optional[str] = None  # Decimal as str
    gas_token: Optional[str] = None       # e.g. "ETH"
    gas_cost_token: Optional[str] = None  # duplicated clarity (≈ gas_token)
    gas_cost_amount: Optional[str] = None # Decimal as str (in gas_token units)
    notes: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self) # type: ignore


class Bookkeeper:
    """
    - Tallentaa jokaisen kaupan JSONL-muotoon (1 rivi / trade) tiedostoon, jonka polku
      luetaan configista (cfg.LEDGER_FILE) ellei toisin anneta.
    - Laskee realisoidun PnL:n raportointivaluutassa (oletus 'USDC') FIFO-loteilla.
      * BUY (token_in == report_ccy, token_out == quote): luo lotin (määrä + kustannus)
      * SELL (token_out == report_ccy, token_in == quote): realisoi PnL lotteja purkamalla
    - Gas-kulut muunnetaan raportointivaluuttaan fx_func(from, to) avulla ja vähennetään PnL:stä.
    """

    def __init__(self,
                 path: Optional[Path] = None,
                 report_ccy: str = "USDC",
                 autoload: bool = True) -> None:
        self.report_ccy = report_ccy
        if path is None:
            cfg = load_config()
            self.path = Path(cfg.LEDGER_FILE)
        else:
            self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        self._trades: List[Trade] = []
        if autoload:
            self._load()

    # ---------- IO ----------
    def _is_jsonl(self) -> bool:
        return str(self.path).lower().endswith(".jsonl")

    def _load(self) -> None:
        self._trades.clear()
        if not self.path.exists():
            return
        if self._is_jsonl():
            with self.path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        if not isinstance(obj, dict):
                            continue
                    except Exception:
                        continue
                    # ohita uudet ledger 1.1 -rivit, koska ne eivät ole Trade-muotoa
                    if isinstance(obj, dict) and (
                            "schema_version" in obj or "quote" in obj or "fill" in obj
                    ):
                        continue
                    try:
                        self._trades.append(Trade(**obj))
                    except Exception:
                        continue
        else:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            items: Iterable[Dict[str, Any]]
            if isinstance(raw, dict) and "trades" in raw:
                items = raw.get("trades") or []
            elif isinstance(raw, list):
                items = raw
            else:
                items = []
            self._trades = []
            for t in items:
                try:
                    self._trades.append(Trade(**t))
                except Exception:
                    continue

    def _append_jsonl(self, trade: Trade) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(trade.to_dict(), ensure_ascii=False, separators=(",", ":")) + os.linesep)

    def _rewrite_json(self) -> None:
        payload = {"trades": [t.to_dict() for t in self._trades]}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def update_gas_by_tx(self, tx_hash: str, gas_used: int,
                         effective_gwei: Decimal, gas_token: str = "ETH") -> None:
        for t in self._trades:
            if t.tx_hash and t.tx_hash.lower() == tx_hash.lower():
                t.gas_used = int(gas_used)
                t.gas_price_gwei = str(effective_gwei)
                t.gas_token = t.gas_cost_token = gas_token
                # gas-kustannus ETH-yksiköissä
                wei_total = Decimal(gas_used) * (effective_gwei * Decimal(1_000_000_000))
                t.gas_cost_amount = str(wei_total / Decimal(10 ** 18))
                break

    def _rewrite_jsonl(self) -> None:
        with self.path.open("w", encoding="utf-8") as f:
            for t in self._trades:
                f.write(json.dumps(t.to_dict(), ensure_ascii=False) + os.linesep)

    # ---------- Public API ----------
    def record(self, trade: Trade) -> None:
        self._trades.append(trade)
        if self._is_jsonl():
            self._append_jsonl(trade)
        else:
            self._rewrite_json()

    def trades(self) -> List[Trade]:
        # Palautetaan kopio, ettei ulkopuoli muokkaa sisälistaa vahingossa
        return list(self._trades)

    def realized_pnl(self, fx_func) -> Dict[str, Any]:
        """
        Laske realisoitu PnL raportointivaluutassa (esim. USDC).
        FIFO-lotitus toteutetaan quote-assetille.

        Malli:
          - BUY: token_in == report_ccy (esim. USDC), token_out == quote (esim. WETH)
                 -> qty = amount_out (quote-yksiköissä)
                 -> cost_total = amount_in (report_ccy)
                 -> lot = (qty, cost_per_unit)
          - SELL: token_in == quote, token_out == report_ccy
                  -> qty_sold = amount_in (quote), proceeds = amount_out (report_ccy)
                  -> match FIFO-lotit → PnL = proceeds - matched_cost
          - gas: jos trade.gas_cost_amount on annettu, muunnetaan raporttivaluuttaan ja vähennetään

        fx_func: Callable(from_token:str, to_token:str) -> Decimal (hinta "to per from")
        """
        from collections import deque

        D0 = Decimal("0")
        lots = deque()   # {"qty": Decimal, "cost_per_unit": Decimal}
        realized = D0

        def gas_to_report(t: Trade) -> Decimal:
            if not t.gas_cost_amount or not t.gas_token:
                return D0
            amt = Decimal(t.gas_cost_amount)
            if t.gas_token == self.report_ccy:
                return amt
            try:
                px = Decimal(fx_func(t.gas_token, self.report_ccy))
                return amt * px
            except Exception:
                # jos fx epäonnistuu, jätetään gas laskematta ettei kaadu
                return D0

        for t in self._trades:
            side = (t.side or "").upper()

            if side == "BUY" and t.token_in == self.report_ccy:
                # BUY report_ccy → quote
                try:
                    qty = Decimal(t.amount_out)
                    cost_total = Decimal(t.amount_in)
                except Exception:
                    # huono rivi -> sivuuta
                    realized -= gas_to_report(t)
                    continue

                cost_per_unit = (cost_total / qty) if qty != 0 else D0
                lots.append({"qty": qty, "cost_per_unit": cost_per_unit})
                realized -= gas_to_report(t)

            elif side == "SELL" and t.token_out == self.report_ccy:
                # SELL quote → report_ccy
                try:
                    qty_sold = Decimal(t.amount_in)   # myyty quote-määrä
                    proceeds = Decimal(t.amount_out)  # saadut report_ccy
                except Exception:
                    realized -= gas_to_report(t)
                    continue

                remaining = qty_sold
                cost_total = D0
                while remaining > 0 and lots:
                    lot = lots[0]
                    take = remaining if remaining <= lot["qty"] else lot["qty"]
                    cost_total += take * lot["cost_per_unit"]
                    lot["qty"] -= take
                    remaining -= take
                    if lot["qty"] == 0:
                        lots.popleft()
                # jos remaining > 0 eikä lotteja -> kustannus 0 (tai voisi liputtaa negatiivisen inventaarion)
                pnl = proceeds - cost_total
                pnl -= gas_to_report(t)
                realized += pnl

            else:
                # tuntematon mapping, huomioidaan vain gas jos annettu
                realized -= gas_to_report(t)

        return {
            "report_currency": self.report_ccy,
            "realized_pnl": str(realized),
            "num_trades": len(self._trades),
        }

    # Kätevä apu: full export JSONiksi (jos haluat lukea JSONL:n helposti johonkin toiseen työkaluun)
    def export_json(self) -> Dict[str, Any]:
        return {"trades": [t.to_dict() for t in self._trades]}
