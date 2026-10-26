from web3 import Web3
from decimal import Decimal, InvalidOperation


QUOTER_V2_ABI = [
    {
        "inputs":[
            {"internalType":"bytes","name":"path","type":"bytes"},
            {"internalType":"uint256","name":"amountIn","type":"uint256"}
        ],
        "name":"quoteExactInput",
        "outputs":[
            {"internalType":"uint256","name":"amountOut","type":"uint256"},
            {"internalType":"uint160[]","name":"sqrtPriceX96AfterList","type":"uint160[]"},
            {"internalType":"uint32[]","name":"initializedTicksCrossedList","type":"uint32[]"},
            {"internalType":"uint256","name":"gasEstimate","type":"uint256"}
        ],
        "stateMutability":"nonpayable","type":"function"
    }
]

def D(x) -> Decimal:
    """Robust Decimal parser: accepts int, float, bytes, HexBytes, and strings; strips %, commas."""
    if x is None:
        raise InvalidOperation("None passed to Decimal")
    if isinstance(x, (int,)):
        return Decimal(x)
    if isinstance(x, float):
        # Avoid binary float artifacts
        return Decimal(str(x))
    try:
        # HexBytes/bytes?
        from hexbytes import HexBytes  # optional
        if isinstance(x, (bytes, bytearray, HexBytes)):
            # interpret as big-endian integer
            return Decimal(int.from_bytes(bytes(x), "big"))
    except Exception:
        pass
    if not isinstance(x, str):
        # last resort: int() on arbitrary types
        try:
            return Decimal(int(x))
        except Exception:
            raise InvalidOperation(f"Unsupported type for Decimal: {type(x)}")
    s = x.strip().replace(",", "")
    if s.endswith("%"):
        s = s[:-1].strip()
    if s == "" or s.lower() in ("nan", "+nan", "-nan", "inf", "+inf", "-inf"):
        raise InvalidOperation(f"Bad numeric string: {x!r}")
    return Decimal(s)



def _encode_path(tokens, fees):
    assert len(tokens) >= 2 and len(fees) == len(tokens)-1
    def a2b(a): return bytes.fromhex(a[2:])
    path = b''
    for i, fee in enumerate(fees):
        path += a2b(tokens[i]) + int(fee).to_bytes(3, 'big')
    path += a2b(tokens[-1])
    return path

class V3PriceFeed:
    def __init__(self, w3, quoter_addr, weth, usdc, fee_tiers, usdc_decimals: int = 6):
        self.w3 = w3
        self.quoter = w3.eth.contract(
            address=Web3.to_checksum_address(quoter_addr),
            abi=QUOTER_V2_ABI
        )
        self.weth  = Web3.to_checksum_address(weth)
        self.usdc  = Web3.to_checksum_address(usdc)
        self.fees  = fee_tiers or [3000]
        self.usdc_decimals = int(usdc_decimals)

    def price_usdc_per_weth(self) -> float:
        """
        Returns USDC per 1 WETH as a float.
        Uses Decimal internally to avoid ConversionSyntax and float artifacts.
        """
        one_weth = int(Decimal(10) ** 18)
        best_out = Decimal(0)

        for fee in self.fees:
            path = _encode_path([self.weth, self.usdc], [int(fee)])
            try:
                out, *_ = self.quoter.functions.quoteExactInput(path, one_weth).call()
                out_dec = D(out)
                if out_dec > best_out:
                    best_out = out_dec
            except Exception:
                # ignore this tier if quoter reverts
                continue

        if best_out == 0:
            raise RuntimeError("Quoter returned zero for all fee tiers")

        usdc_dec = Decimal(10) ** self.usdc_decimals
        price = best_out / usdc_dec  # USDC per 1 WETH
        return float(price)

    def price_weth_per_usdc(self) -> float:
        """
        Returns WETH per 1 USDC as a float (inverse quote).
        """
        one_usdc = int(Decimal(10) ** self.usdc_decimals)
        best_out = Decimal(0)

        for fee in self.fees:
            path = _encode_path([self.usdc, self.weth], [int(fee)])
            try:
                out, *_ = self.quoter.functions.quoteExactInput(path, one_usdc).call()
                out_dec = D(out)
                if out_dec > best_out:
                    best_out = out_dec
            except Exception:
                continue

        if best_out == 0:
            raise RuntimeError("Quoter returned zero for all fee Tiers")

        weth_dec = Decimal(10) ** 18
        price = (best_out / weth_dec)  # WETH per 1 USDC
        return float(price)


