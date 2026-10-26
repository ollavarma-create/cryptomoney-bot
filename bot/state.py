from typing import List, Dict, Any
from dataclasses import dataclass, field, asdict
import json, pathlib

@dataclass
class State:
    target_pct_active: str = ""
    stop_pct_active: str = ""
    holding: bool = False
    first_trade: bool = True
    last_buy_price: str = ""
    last_sell_price: str = ""
    last_spent_usdc: str = ""
    holding_weth: str = ""
    rolling_high: str = ""
    reentry_reason: str = ""
    ladder_progress: List[bool] = field(default_factory=list)
    atr_last: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)  # type: ignore


    def load(self, path: pathlib.Path) -> None:
        if path.exists():
            data = json.loads(path.read_text())
            self.__dict__.update(data)

    def save(self, path: pathlib.Path) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=2))
