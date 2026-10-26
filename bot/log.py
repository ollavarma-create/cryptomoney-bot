import logging, sys, json, time
from datetime import datetime

# Global debug-configi; config.py päivittää tämän settings.jsonista
DEBUG_CFG = {
    "level": "info",
    "rate_limit_ms": 1500,
    # kategoriat
    "trade_decisions": True,
    "reentry": True,
    "cooldown": True,
    "quotes": True,
    "slippage": True,
    "balances": True,
    "pricefeed": False,
    "state_guard": True,
    "executor": False,
}

LEVEL_MAP = {
    "trace": logging.NOTSET,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

class JsonFormatter(logging.Formatter):
    def format(self, record):
        ts = datetime.now().strftime("%d%m%y%H%M%S")
        payload = {
            "ts": ts,
            "lvl": record.levelname,
            "name": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)

def setup_logging(debug_cfg: dict | None = None):
    """
    debug_cfg tulee settings.jsonista (settings['debug']).
    """
    if debug_cfg:
        DEBUG_CFG.update(debug_cfg)

    root = logging.getLogger()
    for name in list(logging.Logger.manager.loggerDict.keys()):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    root.handlers.clear()

    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter())
    setattr(h, "_cryptobot", True)
    root.addHandler(h)

    level = LEVEL_MAP.get(str(DEBUG_CFG.get("level", "info")).lower(), logging.INFO)
    root.setLevel(level)

def enabled(category: str) -> bool:
    """Palauttaa True/False, voiko tietty debug-kategoria logata."""
    return bool(DEBUG_CFG.get(category, False))

# --- optional helpers ---

# Yksinkertainen throttlaus toistuvalle logille (per "tag")
_last_emit_ms = {}

def rate_limited(tag: str, rate_limit_ms: int | None = None) -> bool:
    """
    Palauta True jos TÄNÄ hetkenä EI pitäisi logittaa (eli rivi on vielä "cooldownissa").
    Käyttö:
        if not rate_limited("executor.pool"):
            logger.info("…")
    """
    if rate_limit_ms is None:
        try:
            rate_limit_ms = int(DEBUG_CFG.get("rate_limit_ms", 1500))
        except Exception:
            rate_limit_ms = 1500

    now = int(time.time() * 1000)
    prev = _last_emit_ms.get(tag, 0)
    if now - prev < rate_limit_ms:
        return True  # vielä liian pian
    _last_emit_ms[tag] = now
    return False

def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)

