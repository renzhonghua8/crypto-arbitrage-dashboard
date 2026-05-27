"""Compute arbitrage opportunities from exchange snapshots.

Five views:
  1. Funding-rate ranking (per perp; spot-vs-perp basis + annualized funding)
  2. **Funding-rate cross-exchange divergence** (same coin, multiple exchanges)
  3. Spot-vs-dated-futures basis (annualized to expiry)
  4. Cross-exchange spot price spread
  5. Cross-exchange perp price spread

Liquidity filters are applied before ranking, so e.g. a "5% spread" on a coin
that doesn't trade gets dropped — that's the failure mode that makes naive
all-coins dashboards useless.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from exchanges import ExchangeSnapshot, Ticker


# Defaults. The frontend can override via query/WS settings if we ever want.
DEFAULT_MIN_VOLUME_USD = 1_000_000      # 24h quoteVolume
DEFAULT_MAX_INNER_SPREAD_PCT = 0.5      # bid/ask spread; >0.5% means thin book
DEFAULT_MAX_ROWS = 200                  # cap each table to keep payload sane


@dataclass
class FundingRow:
    exchange: str
    symbol: str
    spot: float
    perp: float
    basis_pct: float
    funding_rate: float
    funding_interval_hours: int
    funding_apr_pct: float
    net_apr_pct: float
    volume_usd: float


@dataclass
class FundingDivergenceRow:
    """Same coin, multiple exchanges. Long the cheap-funding leg, short the
    expensive-funding leg — harvest the spread regardless of price direction."""
    symbol: str
    long_ex: str             # exchange where funding is most negative (you go long)
    long_rate: float
    long_interval_h: int
    short_ex: str            # exchange where funding is most positive (you go short)
    short_rate: float
    short_interval_h: int
    spread_per_8h_pct: float  # combined harvest per 8h, in %
    spread_apr_pct: float     # annualized, summed APR of both legs
    min_volume_usd: float


@dataclass
class BasisRow:
    exchange: str
    symbol: str
    spot: float
    future_price: float
    expiry_ms: int
    days_to_expiry: float
    basis_pct: float
    annualized_pct: float
    volume_usd: float


@dataclass
class CrossRow:
    symbol: str
    market_type: str
    cheapest_ex: str
    cheapest_ask: float
    richest_ex: str
    richest_bid: float
    spread_pct: float
    min_volume_usd: float


@dataclass
class Report:
    funding: list[FundingRow]
    divergence: list[FundingDivergenceRow]
    basis: list[BasisRow]
    cross_spot: list[CrossRow]
    cross_perp: list[CrossRow]
    errors: dict[str, str]
    generated_at_ms: int
    stats: dict[str, int]


def _all_tickers(snapshots: list[ExchangeSnapshot]) -> list[Ticker]:
    return [t for s in snapshots for t in s.tickers]


def _passes_quality(t: Ticker, min_vol: float, max_inner: float) -> bool:
    if t.quote_volume_usd < min_vol:
        return False
    if t.inner_spread_pct > max_inner:
        return False
    return True


def _spot_lookup(snapshots: list[ExchangeSnapshot]) -> dict[tuple[str, str], Ticker]:
    """(exchange, canonical) -> best spot ticker for that pair on that exchange."""
    out: dict[tuple[str, str], Ticker] = {}
    for t in _all_tickers(snapshots):
        if t.market_type != "spot":
            continue
        key = (t.exchange, t.canonical)
        prev = out.get(key)
        if prev is None or t.quote_volume_usd > prev.quote_volume_usd:
            out[key] = t
    return out


def _spot_anywhere(snapshots: list[ExchangeSnapshot]) -> dict[str, Ticker]:
    """canonical -> the most liquid spot ticker for that coin (any exchange)."""
    out: dict[str, Ticker] = {}
    for t in _all_tickers(snapshots):
        if t.market_type != "spot":
            continue
        prev = out.get(t.canonical)
        if prev is None or t.quote_volume_usd > prev.quote_volume_usd:
            out[t.canonical] = t
    return out


def build_funding_rows(
    snapshots: list[ExchangeSnapshot],
    min_vol: float = DEFAULT_MIN_VOLUME_USD,
    max_inner: float = DEFAULT_MAX_INNER_SPREAD_PCT,
) -> list[FundingRow]:
    spot_on_ex = _spot_lookup(snapshots)
    spot_any = _spot_anywhere(snapshots)
    rows: list[FundingRow] = []
    for p in _all_tickers(snapshots):
        if p.market_type != "perp" or p.funding_rate is None:
            continue
        if not _passes_quality(p, min_vol, max_inner):
            continue
        s = spot_on_ex.get((p.exchange, p.canonical)) or spot_any.get(p.canonical)
        if not s:
            continue
        if s.unit_last <= 0:
            continue
        basis_pct = (p.unit_last - s.unit_last) / s.unit_last * 100
        intervals_per_year = 24 * 365 / max(1, p.funding_interval_hours)
        funding_apr = p.funding_rate * intervals_per_year * 100
        rows.append(FundingRow(
            exchange=p.exchange,
            symbol=p.canonical,
            spot=s.unit_last,
            perp=p.unit_last,
            basis_pct=basis_pct,
            funding_rate=p.funding_rate,
            funding_interval_hours=p.funding_interval_hours,
            funding_apr_pct=funding_apr,
            net_apr_pct=funding_apr,
            volume_usd=p.quote_volume_usd,
        ))
    rows.sort(key=lambda r: abs(r.funding_apr_pct), reverse=True)
    return rows[:DEFAULT_MAX_ROWS]


def build_divergence_rows(
    snapshots: list[ExchangeSnapshot],
    min_vol: float = DEFAULT_MIN_VOLUME_USD,
    max_inner: float = DEFAULT_MAX_INNER_SPREAD_PCT,
) -> list[FundingDivergenceRow]:
    """For each coin: pick the lowest (most-negative) funding leg as the
    long-perp exchange, the highest as the short-perp exchange. APR sums
    both legs (you harvest funding on both sides)."""
    by_symbol: dict[str, list[Ticker]] = {}
    for t in _all_tickers(snapshots):
        if t.market_type != "perp" or t.funding_rate is None:
            continue
        if not _passes_quality(t, min_vol, max_inner):
            continue
        by_symbol.setdefault(t.canonical, []).append(t)

    rows: list[FundingDivergenceRow] = []
    for sym, perps in by_symbol.items():
        if len(perps) < 2:
            continue
        long_leg = min(perps, key=lambda t: t.funding_rate)   # most negative
        short_leg = max(perps, key=lambda t: t.funding_rate)  # most positive
        if long_leg.exchange == short_leg.exchange:
            continue
        # Per-8h harvest: short_leg.funding (collected) - long_leg.funding (paid)
        # both expressed for an 8h equivalent so they can be compared cleanly.
        long_per_8h = long_leg.funding_rate * (8 / max(1, long_leg.funding_interval_hours))
        short_per_8h = short_leg.funding_rate * (8 / max(1, short_leg.funding_interval_hours))
        spread_8h = (short_per_8h - long_per_8h) * 100
        if spread_8h <= 0:
            continue
        long_apr = long_leg.funding_rate * (24 * 365 / max(1, long_leg.funding_interval_hours)) * 100
        short_apr = short_leg.funding_rate * (24 * 365 / max(1, short_leg.funding_interval_hours)) * 100
        apr = short_apr - long_apr
        rows.append(FundingDivergenceRow(
            symbol=sym,
            long_ex=long_leg.exchange,
            long_rate=long_leg.funding_rate,
            long_interval_h=long_leg.funding_interval_hours,
            short_ex=short_leg.exchange,
            short_rate=short_leg.funding_rate,
            short_interval_h=short_leg.funding_interval_hours,
            spread_per_8h_pct=spread_8h,
            spread_apr_pct=apr,
            min_volume_usd=min(t.quote_volume_usd for t in [long_leg, short_leg]),
        ))
    rows.sort(key=lambda r: r.spread_apr_pct, reverse=True)
    return rows[:DEFAULT_MAX_ROWS]


def build_basis_rows(
    snapshots: list[ExchangeSnapshot],
    min_vol: float = 100_000,           # futures markets are smaller; lower bar
    max_inner: float = DEFAULT_MAX_INNER_SPREAD_PCT,
) -> list[BasisRow]:
    spot_on_ex = _spot_lookup(snapshots)
    spot_any = _spot_anywhere(snapshots)
    now_ms = int(time.time() * 1000)
    rows: list[BasisRow] = []
    for fut in _all_tickers(snapshots):
        if fut.market_type != "future":
            continue
        if not fut.expiry_ms or fut.expiry_ms <= now_ms:
            continue
        if fut.inner_spread_pct > max_inner:
            continue
        if fut.quote_volume_usd < min_vol:
            continue
        s = spot_on_ex.get((fut.exchange, fut.canonical)) or spot_any.get(fut.canonical)
        if not s or s.unit_last <= 0:
            continue
        days = (fut.expiry_ms - now_ms) / 86_400_000
        if days < 0.5:
            continue
        basis_pct = (fut.unit_last - s.unit_last) / s.unit_last * 100
        annualized = basis_pct * 365 / days
        rows.append(BasisRow(
            exchange=fut.exchange,
            symbol=fut.canonical,
            spot=s.unit_last,
            future_price=fut.unit_last,
            expiry_ms=fut.expiry_ms,
            days_to_expiry=days,
            basis_pct=basis_pct,
            annualized_pct=annualized,
            volume_usd=fut.quote_volume_usd,
        ))
    rows.sort(key=lambda r: abs(r.annualized_pct), reverse=True)
    return rows[:DEFAULT_MAX_ROWS]


def build_cross_rows(
    snapshots: list[ExchangeSnapshot],
    market_type: str,
    min_vol: float = DEFAULT_MIN_VOLUME_USD,
    max_inner: float = DEFAULT_MAX_INNER_SPREAD_PCT,
    min_spread_pct: float = 0.05,
) -> list[CrossRow]:
    by_symbol: dict[str, list[Ticker]] = {}
    for t in _all_tickers(snapshots):
        if t.market_type != market_type:
            continue
        if not _passes_quality(t, min_vol, max_inner):
            continue
        by_symbol.setdefault(t.canonical, []).append(t)

    rows: list[CrossRow] = []
    for sym, tickers in by_symbol.items():
        if len(tickers) < 2:
            continue
        cheapest = min(tickers, key=lambda t: t.unit_ask)
        richest = max(tickers, key=lambda t: t.unit_bid)
        if cheapest.exchange == richest.exchange:
            others = [t for t in tickers if t.exchange != cheapest.exchange]
            if not others:
                continue
            richest = max(others, key=lambda t: t.unit_bid)
        if cheapest.unit_ask <= 0:
            continue
        spread = (richest.unit_bid - cheapest.unit_ask) / cheapest.unit_ask * 100
        if spread < min_spread_pct:
            continue
        rows.append(CrossRow(
            symbol=sym,
            market_type=market_type,
            cheapest_ex=cheapest.exchange,
            cheapest_ask=cheapest.unit_ask,
            richest_ex=richest.exchange,
            richest_bid=richest.unit_bid,
            spread_pct=spread,
            min_volume_usd=min(cheapest.quote_volume_usd, richest.quote_volume_usd),
        ))
    rows.sort(key=lambda r: r.spread_pct, reverse=True)
    return rows[:DEFAULT_MAX_ROWS]


def build_report(snapshots: list[ExchangeSnapshot]) -> Report:
    errors = {s.name: s.error for s in snapshots if s.error}
    stats = {
        "tickers_total": sum(len(s.tickers) for s in snapshots),
        "perps_total": sum(1 for t in _all_tickers(snapshots) if t.market_type == "perp"),
        "spots_total": sum(1 for t in _all_tickers(snapshots) if t.market_type == "spot"),
        "futures_total": sum(1 for t in _all_tickers(snapshots) if t.market_type == "future"),
    }
    return Report(
        funding=build_funding_rows(snapshots),
        divergence=build_divergence_rows(snapshots),
        basis=build_basis_rows(snapshots),
        cross_spot=build_cross_rows(snapshots, "spot"),
        cross_perp=build_cross_rows(snapshots, "perp"),
        errors=errors,
        generated_at_ms=int(time.time() * 1000),
        stats=stats,
    )
