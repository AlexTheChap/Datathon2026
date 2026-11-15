"""
AlphaForge risk tools: pure, deterministic functions, in CAD.

Each tool returns an EVIDENCE record (dict) in a common format:
    {
      "evidence_id": "ev-…",      # stable hash of the inputs
      "tool": "metrics",
      "inputs": {...},
      "output": {...},            # figures
      "say": "short sentence…",    # for the agent (text or voice)
      "currency": "CAD",
      "as_of": "2025-12-30",      # no later data is used
      "period": "2025-01-02/2025-12-30",
      "method": "...", "assumptions": [...], "data_sources": [...]
    }

The LLM never computes: it calls these functions and cites their evidence.

Minimal usage:
    from risk_tools import RiskTools
    rt = RiskTools("data/curated")
    rt.metrics(["NVDA", "XBB.TO"], start="2025-01-01", end="2025-12-31")
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

TRADING_DAYS = 252
SOURCES = ["prices_daily.csv (Yahoo Finance via yfinance, extracted 2026-11-13)",
           "fx_usdcad_daily.csv (Bank of Canada, series FXUSDCAD)"]
BASE_ASSUMPTIONS = [
    "Total return (adj_close, dividends reinvested), converted to CAD.",
    "Calendar: union of TSX and US trading days; prices carried forward on market-specific holidays.",
    "Crypto sampled on this calendar (weekend moves included on Monday).",
    "Past returns do not predict future returns. Universe chosen in 2026 (survivorship bias).",
]
EXT_SOURCE = ("returns_daily_cad_ext.csv (S&P 500 constituents, Yahoo Finance via yfinance, "
              "scripts/build_extended_universe.py)")


def _ext_assumption(tickers: list[str]) -> str:
    return (f"{', '.join(tickers)}: S&P 500 extension (current constituents, so a strong survivorship bias; "
            "Yahoo Finance, unlicensed). No fundamentals, ESG detail or 10-K excerpts for them.")


def _eid(tool: str, inputs: dict) -> str:
    h = hashlib.sha1(json.dumps([tool, inputs], sort_keys=True, default=str).encode()).hexdigest()
    return f"ev-{h[:10]}"


def _pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def _money(x: float) -> str:
    return f"${x:,.0f}"


class RiskTools:
    def __init__(self, curated_dir: str | Path, universe_csv: str | Path | None = None):
        # curated_store: <name>.parquet (S3 curated Parquet, L14) when present, otherwise the repo CSV.
        from app.tools import curated_store as cs

        d = Path(curated_dir)
        # The 31 curated assets + the S&P 500 extension when built (scripts/build_extended_universe.py).
        self.rets = cs.read_extended(d, "returns_daily_cad")
        self.prices = cs.read(d, "prices_cad")
        if universe_csv:
            self.universe = pd.read_csv(universe_csv).set_index("ticker")
        else:
            self.universe = cs.read_extended(d, "assets_universe").set_index("ticker") \
                if cs.exists(d, "assets_universe") else None
        self.extended = cs.extended_tickers(d)
        self.holdings = self._load_holdings(cs.read(d, "etf_holdings")) if cs.exists(d, "etf_holdings") else None

    @staticmethod
    def _load_holdings(h: pd.DataFrame | Path) -> dict[str, pd.DataFrame]:
        """etf_holdings (scripts/etf_holdings.py) -> {ETF: DataFrame[ticker, name, sector, weight, holdings_date]}.
        Holdings without a ticker are identified by name."""
        if not isinstance(h, pd.DataFrame):  # backward compatibility: a CSV path
            h = pd.read_csv(h, dtype={"ticker": str, "cusip": str}, keep_default_na=False)
        h = h.copy()
        h["ticker"] = h["ticker"].where(h["ticker"] != "", h["name"])
        return {f: g.reset_index(drop=True) for f, g in h.groupby("fund")}

    def lookthrough(self, weights: dict, holdings: dict[str, pd.DataFrame] | None = None) -> pd.DataFrame:
        """Exposure per company after decomposing equity ETFs.
        Columns: weight (total), direct, via_etf, sector, asset_class, currency. ETFs that are not decomposed
        (bonds, gold) stay as one row. The non-equity part of an ETF (cash) is ignored."""
        holdings = self.holdings if holdings is None else holdings
        w = self._normalize(weights)
        u = self.universe
        rows = []
        for a, wa in w.items():
            if holdings and a in holdings:
                for h in holdings[a].itertuples():
                    rows.append((h.ticker, wa * float(h.weight), 0.0, wa * float(h.weight),
                                 getattr(h, "sector", "") or "", "equity", u.loc[a, "currency"]))
            else:
                rows.append((a, wa, wa, 0.0, u.loc[a, "sector"], u.loc[a, "asset_class"], u.loc[a, "currency"]))
        df = pd.DataFrame(rows, columns=["id", "weight", "direct", "via_etf", "sector", "asset_class", "currency"])
        in_u = df["id"].isin(u.index)
        df.loc[in_u, "sector"] = df.loc[in_u, "id"].map(u["sector"])      # universe sector takes priority
        df.loc[in_u, "asset_class"] = df.loc[in_u, "id"].map(u["asset_class"])
        df["sector"] = df["sector"].replace("", "Unclassified")
        return (df.groupby("id").agg(weight=("weight", "sum"), direct=("direct", "sum"), via_etf=("via_etf", "sum"),
                                     sector=("sector", "first"), asset_class=("asset_class", "first"),
                                     currency=("currency", "first"))
                  .sort_values("weight", ascending=False))

    def at(self, as_of: str | None) -> "RiskTools":
        """Point-in-time view: the same tools, with no return or price after `as_of`.
        `governance_check.check` takes no `as_of` argument: give it this view
        so that a window which ends after `as_of` is cut at `as_of`."""
        if not as_of:
            return self
        cut = pd.Timestamp(as_of)
        view = copy.copy(self)
        view.rets = self.rets.loc[:cut]
        view.prices = self.prices.loc[:cut]
        if view.rets.empty:
            raise ValueError(f"No data on or before as_of={as_of}.")
        return view

    # ---------- helpers ----------
    def _window(self, start=None, end=None, as_of=None) -> pd.DataFrame:
        r = self.rets
        if as_of:
            r = r.loc[:pd.Timestamp(as_of)]
        if start:
            r = r.loc[pd.Timestamp(start):]
        if end:
            r = r.loc[:pd.Timestamp(end)]
        if r.empty:
            raise ValueError("No data in the requested window.")
        return r

    def _check(self, tickers):
        unknown = [t for t in tickers if t not in self.rets.columns]
        if unknown:
            raise KeyError(f"Unknown assets (not in the universe): {unknown}")

    @staticmethod
    def _normalize(w: dict) -> dict:
        s = sum(w.values())
        if s <= 0:
            raise ValueError("Weights sum to zero.")
        return {k: v / s for k, v in w.items()}

    def portfolio_returns(self, weights: dict, start=None, end=None, as_of=None) -> pd.Series:
        """Portfolio rebalanced daily to the target weights (assumption)."""
        w = self._normalize(weights)
        self._check(w)
        r = self._window(start, end, as_of)[list(w)].dropna()
        return r.mul(pd.Series(w)).sum(axis=1)

    @staticmethod
    def _stats(r: pd.Series) -> dict:
        w = (1 + r).cumprod()
        dd = w / w.cummax() - 1
        trough = dd.idxmin()
        peak = w.loc[:trough].idxmax()
        after = w.loc[trough:]
        rec = after[after >= w.loc[peak]].index
        q = r.quantile(0.05)
        n_years = len(r) / TRADING_DAYS
        total = w.iloc[-1] - 1
        return {
            "total_return": round(float(total), 6),
            "cagr": round(float((1 + total) ** (1 / n_years) - 1), 6) if n_years >= 1 else None,
            "ann_vol": round(float(r.std() * np.sqrt(TRADING_DAYS)), 6),
            "downside_vol": round(float(r[r < 0].std() * np.sqrt(TRADING_DAYS)), 6),
            "max_drawdown": round(float(dd.min()), 6),
            "dd_peak": str(peak.date()), "dd_trough": str(trough.date()),
            "dd_recovery": str(rec[0].date()) if len(rec) else None,
            "recovery_days": int((rec[0] - trough).days) if len(rec) else None,
            "var95_daily": round(float(-q), 6),
            "cvar95_daily": round(float(-r[r <= q].mean()), 6),
        }

    def _evidence(self, tool, inputs, output, say, r_index, method, extra_assumptions=()):
        named = inputs.get("tickers") or list(inputs.get("weights") or {})
        ext = sorted(set(named) & getattr(self, "extended", set()))
        if ext:
            extra_assumptions = [*extra_assumptions, _ext_assumption(ext)]
        return {
            "evidence_id": _eid(tool, inputs), "tool": tool, "inputs": inputs, "output": output,
            "say": say, "currency": "CAD",
            "as_of": inputs.get("as_of") or str(r_index.max().date()),
            "period": f"{r_index.min().date()}/{r_index.max().date()}",
            "method": method, "assumptions": BASE_ASSUMPTIONS + list(extra_assumptions),
            "data_sources": SOURCES + ([EXT_SOURCE] if ext else []),
        }

    # ---------- tools ----------
    def metrics(self, tickers: list[str], start=None, end=None, as_of=None) -> dict:
        self._check(tickers)
        r = self._window(start, end, as_of)
        out = {t: self._stats(r[t].dropna()) for t in tickers}
        t0 = tickers[0]
        say = (f"{t0}: volatility {out[t0]['ann_vol'] * 100:.0f}% per year, "
               f"worst drop {_pct(out[t0]['max_drawdown'])} (in CAD).")
        return self._evidence("metrics", {"tickers": tickers, "start": start, "end": end, "as_of": as_of},
                              out, say, r.index,
                              "Volatility = daily standard deviation × √252; maximum drawdown = worst peak-to-trough drop; "
                              "historical daily 95% VaR/CVaR.")

    def concentration(self, weights: dict, holdings: dict[str, pd.DataFrame] | None = None,
                      look_through: bool = True, top_n: int = 25) -> dict:
        """Weights by company, sector, asset class and currency. By default, decomposes equity ETFs
        using data/curated/etf_holdings.csv (or `holdings` = {ETF: DataFrame[ticker, weight, sector]})."""
        if self.universe is None:
            raise RuntimeError("assets_universe.csv not found.")
        hold = (holdings if holdings is not None else self.holdings) if look_through else {}
        df = self.lookthrough(weights, hold or {})
        used = sorted(set(weights) & set(hold or {}))
        by = {c: df.groupby(c)["weight"].sum().sort_values(ascending=False).round(4).to_dict()
              for c in ["sector", "asset_class", "currency"]}
        hhi = float((df["weight"] ** 2).sum())
        top = df.head(top_n)
        out = {"by_position": top["weight"].round(4).to_dict(),
               "by_position_detail": top[["direct", "via_etf"]].round(4).to_dict(orient="index"),
               "n_positions": int(len(df)), "by_sector": by["sector"],
               "by_asset_class": by["asset_class"], "by_currency": by["currency"],
               "hhi": round(hhi, 4), "effective_positions": round(1 / hhi, 2), "look_through": used}
        t0 = df.index[0]
        say = (f"Largest exposure: {t0} at {df['weight'].iloc[0] * 100:.0f}%"
               + (f" ({df['via_etf'].iloc[0] * 100:.0f}% of it via ETFs)" if df["via_etf"].iloc[0] > 0.001 else "")
               + f"; equivalent to {round(1 / hhi, 1)} equal-weighted positions.")
        assumptions = []
        if used:
            dates = {f: str(hold[f]["holdings_date"].iloc[0]) for f in used if "holdings_date" in hold[f]}
            assumptions.append(f"ETFs decomposed using their most recent holdings {dates} (CURRENT holdings, "
                               "not historical); current GICS sectors, \"Unclassified\" if unknown.")
        not_split = [a for a in weights if a not in (hold or {}) and self.universe.loc[a, "asset_class"].startswith("etf")]
        if not_split:
            assumptions.append(f"Not decomposed (one position each): {not_split}.")
        ev = self._evidence("concentration", {"weights": weights, "look_through": used},
                            out, say, self.rets.index,
                            "Exposure per company = Σ ETF weight × stock weight in the ETF + direct holding; "
                            "Herfindahl index (Σw²) and effective number of positions (1/HHI).", assumptions)
        ev.pop("period")
        if used:
            ev["data_sources"] = ev["data_sources"] + ["etf_holdings.csv (State Street, iShares Canada, SEC N-PORT)"]
        return ev

    def correlation(self, tickers: list[str], start=None, end=None, as_of=None) -> dict:
        self._check(tickers)
        r = self._window(start, end, as_of)[tickers].dropna()
        c = r.corr().round(3)
        out = {"matrix": c.to_dict()}
        pairs = [(a, b, c.loc[a, b]) for i, a in enumerate(tickers) for b in tickers[i + 1:]]
        hi = max(pairs, key=lambda p: p[2]) if pairs else None
        say = f"Strongest correlation: {hi[0]} and {hi[1]} ({hi[2]:.2f})." if hi else ""
        return self._evidence("correlation", {"tickers": tickers, "start": start, "end": end, "as_of": as_of},
                              out, say, r.index, "Pearson correlation of daily returns in CAD.",
                              ["Correlations are not stable: they often rise when markets fall."])

    def risk_contribution(self, weights: dict, start=None, end=None, as_of=None) -> dict:
        w = self._normalize(weights)
        self._check(w)
        r = self._window(start, end, as_of)[list(w)].dropna()
        cov = r.cov() * TRADING_DAYS
        wv = pd.Series(w)
        port_var = float(wv @ cov @ wv)
        rc = wv * (cov @ wv) / port_var
        avg_vol = float((wv * np.sqrt(np.diag(cov))).sum())
        out = {"portfolio_vol": round(np.sqrt(port_var), 6),
               "risk_contribution": rc.round(4).to_dict(), "weights": wv.round(4).to_dict(),
               "diversification_ratio": round(avg_vol / np.sqrt(port_var), 4)}
        top = rc.idxmax()
        say = f"{top} makes up {w[top] * 100:.0f}% of the portfolio but generates {rc[top] * 100:.0f}% of its risk."
        return self._evidence("risk_contribution", {"weights": weights, "start": start, "end": end, "as_of": as_of},
                              out, say, r.index,
                              "Risk contribution = w_i (Σw)_i / wᵀΣw; diversification ratio = weighted average vol / portfolio vol.")

    def replay(self, weights: dict, capital: float, start: str, end: str) -> dict:
        """Dollar path (CAD), day by day: drives the time-machine animation."""
        pr = self.portfolio_returns(weights, start=start, end=end).iloc[1:]
        path = (capital * (1 + pr).cumprod()).round(2)
        st = self._stats(pr)
        out = {"path": {str(k.date()): v for k, v in path.items()}, "stats": st,
               "value_start": capital, "value_end": float(path.iloc[-1]),
               "min_value": float(path.min()), "min_date": str(path.idxmin().date())}
        say = (f"From {_money(capital)} on {start}, the value fell to {_money(path.min())} "
               f"on {path.idxmin().date()}, and ended at {_money(path.iloc[-1])}.")
        return self._evidence("replay", {"weights": weights, "capital": capital, "start": start, "end": end},
                              out, say, pr.index, "Historical replay of a fixed allocation, rebalanced daily.",
                              ["Historical scenario, not a forecast. No fees or taxes."])
