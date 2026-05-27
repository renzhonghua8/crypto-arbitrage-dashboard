"""Exchange adapters — pulls ALL USDT-quoted spot + linear perp + dated futures.

Symbols are normalized so 1000PEPE/USDT, kPEPE/USDT and PEPE/USDT all map to
the same key for cross-exchange comparison. We carry a `price_scale` so prices
can be unit-normalized when comparing across exchanges.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import ccxt.async_support as ccxt

log = logging.getLogger(__name__)


def _load_dotenv() -> None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv()

PROXY_URL = (
    os.environ.get("HTTPS_PROXY")
    or os.environ.get("https_proxy")
    or os.environ.get("HTTP_PROXY")
    or os.environ.get("http_proxy")
    or os.environ.get("ALL_PROXY")
    or os.environ.get("all_proxy")
    or None
)
if PROXY_URL:
    log.info("using proxy: %s", PROXY_URL)


# Skip leveraged tokens — they look like real spreads but aren't.
_LEVERAGED_SUFFIX = re.compile(r"(3L|3S|5L|5S|BULL|BEAR|HALF|HEDGE)$")
# Bases like "BTCUP" / "ETHDOWN". Only treat as leveraged when prefix is a
# common ticker — otherwise we'd false-match coins like JUP.
_KNOWN_UNDERLYINGS = {
    "BTC", "ETH", "BNB", "XRP", "ADA", "SOL", "DOT", "LINK", "LTC", "BCH",
    "UNI", "EOS", "TRX", "AVAX", "ATOM", "DOGE", "FIL", "MATIC", "NEAR",
    "LUNA", "FTT", "SRM", "AAVE", "SUSHI", "YFI", "SXP", "ALGO",
}


def _is_leveraged_token(base: str) -> bool:
    if _LEVERAGED_SUFFIX.search(base):
        return True
    for suf in ("UP", "DOWN", "BULL", "BEAR"):
        if base.endswith(suf) and base[: -len(suf)] in _KNOWN_UNDERLYINGS:
            return True
    return False


_NUMERIC_PREFIX = re.compile(r"^(\d+)([A-Z].*)$")
# Only treat numeric prefixes that are actual decimal multipliers as scales.
# Otherwise tickers like "0G" or "1INCH" get clobbered (0G is a real coin,
# 1INCH is a real coin — neither is leveraged).
_VALID_SCALES = {10, 100, 1000, 10000, 100000, 1000000}


def _normalize_base(base: str) -> tuple[str, float]:
    """Strip multiplier prefixes ("1000PEPE" -> "PEPE", scale=1000).

    Returns (canonical_base, price_scale). Scale is always >= 1.
    """
    m = _NUMERIC_PREFIX.match(base)
    if m:
        try:
            scale = int(m.group(1))
        except ValueError:
            scale = 0
        if scale in _VALID_SCALES:
            return m.group(2), float(scale)
    if len(base) > 2 and base[0] == "k" and base[1:].isupper():
        return base[1:], 1000.0
    if len(base) > 2 and base[0] == "M" and base[1:].isupper():
        return base[1:], 1_000_000.0
    return base, 1.0


@dataclass
class Ticker:
    exchange: str
    raw_symbol: str          # exchange-specific, e.g. "1000PEPE/USDT"
    canonical: str           # normalized, e.g. "PEPE/USDT"
    market_type: str         # "spot" | "perp" | "future"
    last: float              # raw price (per raw_symbol unit)
    bid: float
    ask: float
    price_scale: float = 1.0  # multiply raw price by 1/scale to get canonical-unit price
    quote_volume_usd: float = 0.0
    funding_rate: Optional[float] = None
    funding_interval_hours: int = 8
    expiry_ms: Optional[int] = None
    ts_ms: int = 0

    @property
    def unit_last(self) -> float:
        """Price per canonical unit (cross-scale comparable)."""
        return self.last / max(1.0, self.price_scale)

    @property
    def unit_bid(self) -> float:
        return self.bid / max(1.0, self.price_scale)

    @property
    def unit_ask(self) -> float:
        return self.ask / max(1.0, self.price_scale)

    @property
    def inner_spread_pct(self) -> float:
        """Bid/ask spread — high means thin book / stale data."""
        if not self.bid or not self.ask or self.bid <= 0:
            return 100.0
        return (self.ask - self.bid) / self.bid * 100


@dataclass
class ExchangeSnapshot:
    name: str
    tickers: list[Ticker] = field(default_factory=list)
    error: Optional[str] = None


def _make(name: str):
    klass = getattr(ccxt, name)
    cfg = {
        "enableRateLimit": True,
        "timeout": 20000,
        "options": {"defaultType": "spot"},
    }
    if PROXY_URL:
        cfg["aiohttp_proxy"] = PROXY_URL
    return klass(cfg)


def _ticker_volume(t: dict) -> float:
    """Extract USDT-denominated 24h volume from a ccxt ticker dict."""
    v = t.get("quoteVolume")
    if v is not None:
        try:
            return float(v)
        except (TypeError, ValueError):
            pass
    # Fallback: base volume * last
    bv = t.get("baseVolume")
    last = t.get("last")
    if bv is not None and last is not None:
        try:
            return float(bv) * float(last)
        except (TypeError, ValueError):
            pass
    return 0.0


def _build_ticker(
    exchange: str,
    raw_sym: str,
    market: dict,
    t: dict,
    market_type: str,
    funding_rate: Optional[float] = None,
    funding_interval_h: int = 8,
) -> Optional[Ticker]:
    last = t.get("last")
    if last is None or last == 0:
        return None
    base = market.get("base") or raw_sym.split("/")[0]
    if _is_leveraged_token(base):
        return None
    canonical_base, scale = _normalize_base(base)
    canonical = f"{canonical_base}/USDT"
    try:
        last_f = float(last)
    except (TypeError, ValueError):
        return None
    return Ticker(
        exchange=exchange,
        raw_symbol=raw_sym,
        canonical=canonical,
        market_type=market_type,
        last=last_f,
        bid=float(t.get("bid") or last_f),
        ask=float(t.get("ask") or last_f),
        price_scale=scale,
        quote_volume_usd=_ticker_volume(t),
        funding_rate=funding_rate,
        funding_interval_hours=funding_interval_h,
        expiry_ms=int(market["expiry"]) if market.get("expiry") else None,
        ts_ms=int(t.get("timestamp") or 0),
    )


def _funding_interval(fr: dict) -> int:
    iv = fr.get("interval")
    if isinstance(iv, str):
        s = iv.lower().replace("h", "").strip()
        try:
            return max(1, int(s))
        except ValueError:
            pass
    nxt = fr.get("fundingTimestamp") or fr.get("nextFundingTimestamp")
    prv = fr.get("previousFundingTimestamp")
    if nxt and prv and nxt > prv:
        return max(1, round((nxt - prv) / 3_600_000))
    return 8


async def _set_type_and_load(ex, market_type: str) -> bool:
    ex.options["defaultType"] = market_type
    try:
        await ex.load_markets(reload=True)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("%s load_markets(%s) failed: %s", ex.id, market_type, e)
        return False


async def _fetch_spot(ex, name: str) -> list[Ticker]:
    if not await _set_type_and_load(ex, "spot"):
        return []
    relevant = {
        sym: m for sym, m in ex.markets.items()
        if m.get("spot") and m.get("quote") == "USDT" and m.get("active") is not False
    }
    if not relevant:
        return []
    try:
        tickers = await ex.fetch_tickers(list(relevant.keys()))
    except Exception as e:  # noqa: BLE001
        log.warning("%s spot fetch_tickers failed: %s", name, e)
        return []
    out: list[Ticker] = []
    for sym, t in tickers.items():
        m = relevant.get(sym)
        if not m:
            continue
        tk = _build_ticker(name, sym, m, t, "spot")
        if tk:
            out.append(tk)
    return out


async def _fetch_perp(ex, name: str) -> list[Ticker]:
    if not await _set_type_and_load(ex, "swap"):
        return []
    relevant = {
        sym: m for sym, m in ex.markets.items()
        if m.get("swap") and m.get("linear")
        and m.get("quote") == "USDT" and not m.get("expiry")
        and m.get("active") is not False
    }
    if not relevant:
        return []
    try:
        tickers = await ex.fetch_tickers(list(relevant.keys()))
    except Exception as e:  # noqa: BLE001
        log.warning("%s perp fetch_tickers failed: %s", name, e)
        return []

    funding_map: dict[str, float] = {}
    interval_map: dict[str, int] = {}
    if ex.has.get("fetchFundingRates"):
        try:
            frs = await ex.fetch_funding_rates(list(relevant.keys()))
            for sym, fr in frs.items():
                if fr.get("fundingRate") is not None:
                    funding_map[sym] = float(fr["fundingRate"])
                interval_map[sym] = _funding_interval(fr)
        except Exception as e:  # noqa: BLE001
            log.warning("%s fetch_funding_rates failed: %s", name, e)

    out: list[Ticker] = []
    for sym, m in relevant.items():
        t = tickers.get(sym)
        if not t:
            continue
        tk = _build_ticker(
            name, sym, m, t, "perp",
            funding_rate=funding_map.get(sym),
            funding_interval_h=interval_map.get(sym, 8),
        )
        if tk:
            out.append(tk)
    return out


async def _fetch_futures(ex, name: str) -> list[Ticker]:
    if not await _set_type_and_load(ex, "future"):
        return []
    relevant = {
        sym: m for sym, m in ex.markets.items()
        if m.get("future") and m.get("linear")
        and m.get("quote") == "USDT" and m.get("expiry")
        and m.get("active") is not False
    }
    if not relevant:
        return []
    try:
        tickers = await ex.fetch_tickers(list(relevant.keys()))
    except Exception as e:  # noqa: BLE001
        log.warning("%s futures fetch_tickers failed: %s", name, e)
        return []
    out: list[Ticker] = []
    for sym, m in relevant.items():
        t = tickers.get(sym)
        if not t:
            continue
        tk = _build_ticker(name, sym, m, t, "future")
        if tk:
            out.append(tk)
    return out


async def fetch_exchange(name: str) -> ExchangeSnapshot:
    ex = _make(name)
    snap = ExchangeSnapshot(name=name)
    try:
        # Sequential within an exchange (defaultType is mutated).
        # Exchanges run in parallel — see fetch_all().
        spot = await _fetch_spot(ex, name)
        perp = await _fetch_perp(ex, name)
        fut = await _fetch_futures(ex, name)
        snap.tickers = spot + perp + fut
    except Exception as e:  # noqa: BLE001
        snap.error = str(e)
        log.exception("%s snapshot failed", name)
    finally:
        try:
            await ex.close()
        except Exception:  # noqa: BLE001
            pass
    return snap


EXCHANGES = ["binance", "okx", "bybit", "gate", "bitget"]


async def fetch_all() -> list[ExchangeSnapshot]:
    return await asyncio.gather(*(fetch_exchange(n) for n in EXCHANGES))
