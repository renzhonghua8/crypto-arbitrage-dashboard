# Crypto Arbitrage Live Dashboard

> [中文文档](README.zh.md) · English · [Deploy to Windows / 部署到 Windows](DEPLOY.md)

Real-time crypto arbitrage opportunity scanner. Pulls every USDT-quoted spot,
linear perpetual and dated futures market across **Binance / OKX / Bybit /
Gate.io / Bitget**, computes five flavors of arbitrage edge, and pushes the
ranked results to a self-contained web UI over WebSocket.

## Why this exists

The most lucrative arbitrage in crypto today isn't BTC/ETH on the major
venues — every spread there is hunted in milliseconds by market-makers. The
edges that still exist tend to be on **mid- and long-tail altcoins, where
funding rates can diverge dramatically across exchanges**. This dashboard
exists to surface those situations as they happen, ranked by annualised
return, with liquidity filters to cut out the noise.

## Features

- **5 arbitrage views**, each sortable by any column:
  1. ⭐ **Funding-rate divergence across exchanges** — go long the cheapest
     funding leg, short the richest. Earns funding on both sides.
  2. **Single-exchange funding rate** — classic spot-long + perp-short carry.
  3. **Spot vs dated futures basis** — annualised by days-to-expiry.
  4. **Cross-exchange perp price spread**.
  5. **Cross-exchange spot price spread**.
- **All USDT pairs** scanned across 5 exchanges (~6,000 tickers per cycle).
- **Symbol normalisation** — `1000PEPE`, `kPEPE`, `PEPE` all reconcile to the
  same coin so cross-exchange comparisons line up.
- **Liquidity & quality filters** — minimum 24h volume, bid/ask sanity check.
  No more 5%-spread mirages on dead pairs.
- **Live updates over WebSocket**, cells flash when they change.
- **i18n** — English / 简体中文 toggle, choice persisted in localStorage.
- **Proxy-aware** — picks up `HTTPS_PROXY` from `.env` so mainland China users
  can route through their local proxy (Clash / V2Ray / etc.).

## Quick start

```bash
git clone https://github.com/<your-user>/crypto-arb.git
cd crypto-arb
pip install -r requirements.txt

# Mainland China users only — set your local proxy port:
cp .env.example .env
# edit .env if your proxy isn't on 127.0.0.1:1082

python3 -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 8765
```

Open <http://127.0.0.1:8765>.

First snapshot takes ~10-30s (loads markets across 5 exchanges). After that
the dashboard refreshes every 8 seconds.

### Requirements

- Python 3.10+
- A working network path to the exchanges' public REST endpoints. From
  mainland China you'll need a local proxy — set it in `.env`:
  ```
  HTTPS_PROXY=http://127.0.0.1:1082
  HTTP_PROXY=http://127.0.0.1:1082
  ```

## The 5 views, in detail

### 1. ⭐ Funding-rate divergence (the headline view)

When the same coin trades as a perpetual on multiple exchanges, the funding
rates often diverge — especially on lower-cap and meme coins. If exchange A
charges longs +0.5% per 8h while exchange B pays longs 0.1% per 8h, you can:

- Long the perp on **A** (where funding is more negative → you get paid)
- Short the perp on **B** (where funding is more positive → you collect)

Both legs are delta-neutral to price moves. You harvest the **combined** funding spread.

| Column | Meaning |
| --- | --- |
| Long Ex / Long Rate | The exchange where funding is most negative (you receive). |
| Short Ex / Short Rate | The exchange where funding is most positive (you receive). |
| Interval h | Funding interval in hours (1/4/8). Different exchanges/coins differ. |
| Spread/8h | Sum of both legs, normalised to an 8-hour interval. |
| APR | Annualised total (long-leg APR plus short-leg APR). |
| Min vol $ | Smallest 24h quote volume between the two legs — your real liquidity ceiling. |

> ⚠ Super-high APRs (>200%) almost always come from one exchange's funding
> being temporarily extreme. They tend to mean-revert within 1–2 funding
> intervals, so don't size up assuming the rate will hold for a year.

### 2. Funding rate (single exchange)

Classic cash-and-carry: hold the spot, short the perp on the same exchange.
You collect the funding rate; price exposure is hedged.

| Column | Meaning |
| --- | --- |
| Spot / Perp | Last trade on each leg. |
| Basis % | (perp − spot) / spot. Should hover near zero. |
| Funding | Current period's funding rate. |
| APR | funding × (8760 / interval_hours) × 100. |

### 3. Spot–futures basis

Quarterly / dated futures usually trade in contango (premium to spot). You
short the future, hold spot, basis converges at expiry.

| Column | Meaning |
| --- | --- |
| To Expiry | Days remaining until settlement. |
| Basis % | (future − spot) / spot. |
| APR | basis × 365 / days_to_expiry. |

### 4 & 5. Cross-exchange spreads

Same coin, different exchanges. Buy on the cheapest, sell on the richest.

> ⚠ For **spot**: in practice the withdrawal time (minutes to hours) and
> network fees usually eat anything below ~0.3%. The dashboard will still
> show smaller spreads — they're informational, not actionable.
>
> For **perps**: no withdrawal needed, but you pay taker fees on both legs
> and need to size to manage liquidation risk.

## Architecture

```
                ┌────────────────┐
                │  Browser (UI)  │  ◄── HTML/CSS/Vanilla JS, no framework
                └────────┬───────┘
                         │ WebSocket (8s push)
                ┌────────▼───────┐
                │  FastAPI app   │  backend/app.py
                ├────────────────┤
                │  Poll loop     │  Triggers `fetch_all()` every 8s
                └────────┬───────┘
                         │
                ┌────────▼────────────────────────┐
                │  exchanges.py (ccxt async)      │  parallel
                │  Binance · OKX · Bybit · …      │  fetch
                └────────┬────────────────────────┘
                         │
                ┌────────▼───────┐
                │  arbitrage.py  │  filters + 5 calculators
                └────────────────┘
```

Single Python process. No database. State (latest report) lives in memory
and is broadcast to every connected WebSocket client. State is per-process,
so restarts lose the in-flight cache (the next poll repopulates it).

## Filters

Two layers of filtering keep the noise out:

1. **Backend hard floors** (in `arbitrage.py`):
   - `quote_volume_usd ≥ 1M` for funding/cross-exchange views
   - `quote_volume_usd ≥ 100K` for the futures basis view
   - `(ask − bid) / bid ≤ 0.5%` — dumps tickers with thin orderbooks
   - Each table capped at 200 rows

2. **Frontend filters** (controls in the header):
   - Free-text coin search
   - Adjustable minimum 24h volume

## Pitfalls

| Symptom | Cause / Fix |
| --- | --- |
| "529609% basis" rows in single-ex funding | Same ticker symbol used for two different tokens across exchanges (e.g. FLY on bitget perp vs FLY on bitget spot are different projects). Ignore obvious outliers. |
| Bitget / Gate empty on first load | The first call sometimes times out under proxy. Wait one poll cycle. |
| All exchanges fail with `gateio.ws` error | Proxy isn't routing — confirm your proxy is running and `.env` is set. |
| Funding APR 5,000%+ | One funding period's rate annualised. Will revert. |
| Cross-exchange spot spread looks juicy | Withdrawal fees + chain delays kill anything < ~0.3% in practice. |

## Disclaimer

Not financial advice. The numbers shown are theoretical, **before fees,
slippage, financing costs, or execution risk**. The dashboard is a
monitoring tool — no orders are placed.

## License

MIT.
