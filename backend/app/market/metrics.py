"""Per-asset risk metrics shared by the curated build (scripts/build_curated.py) and the market refresh.

`drawdown` and `asset_metrics` moved here unchanged from scripts/build_curated.py, so the twice-daily
market refresh (app.market.refresh) computes volatility, drawdown, VaR and beta with exactly the formulas
of the curated dataset. Added for the market graph: `risk_class` (fixed volatility bands) and
`correlation_edges` (strongly correlated pairs only, so the graph stays sparse with 500+ assets).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252

# Annualized-volatility bands (absolute, not quantiles: a class must not change because other stocks moved).
# Team assumption, documented in the evidence of the graph tools.
RISK_CLASSES: list[tuple[str, float]] = [("low", 0.15), ("moderate", 0.25), ("high", 0.40), ("very_high", np.inf)]


def drawdown(r: pd.Series):
    w = (1 + r.fillna(0)).cumprod()
    peak = w.cummax()
    dd = w / peak - 1
    trough = dd.idxmin()
    peak_date = w.loc[:trough].idxmax()
    rec = w.loc[trough:]
    rec_dates = rec[rec >= w.loc[peak_date]].index
    recovery = rec_dates[0] if len(rec_dates) else pd.NaT
    return dd.min(), peak_date, trough, recovery


def asset_metrics(rets: pd.DataFrame, bench: dict[str, str], label: str) -> pd.DataFrame:
    rows = []
    monthly = (1 + rets.fillna(0)).resample("ME").prod() - 1
    for t in rets.columns:
        r = rets[t].dropna()
        if len(r) < 30:
            continue
        n_years = len(r) / TRADING_DAYS
        total = (1 + r).prod() - 1
        mdd, pk, tr, rc = drawdown(r)
        neg = r[r < 0]
        q = r.quantile(0.05)
        mq = monthly[t].quantile(0.05)
        row = {
            "ticker": t, "window": label,
            "start": r.index.min().date(), "end": r.index.max().date(), "n_obs": len(r),
            "total_return": total,
            "cagr": (1 + total) ** (1 / n_years) - 1 if n_years > 0 else np.nan,
            "ann_vol": r.std() * np.sqrt(TRADING_DAYS),
            "downside_vol": neg.std() * np.sqrt(TRADING_DAYS),
            "max_drawdown": mdd, "dd_peak": pk.date(), "dd_trough": tr.date(),
            "dd_recovery": rc.date() if pd.notna(rc) else None,
            "recovery_days": (rc - tr).days if pd.notna(rc) else None,
            "var95_daily": -q, "cvar95_daily": -r[r <= q].mean(),
            "var95_monthly": -mq, "worst_month": monthly[t].min(),
        }
        for name, b in bench.items():
            if b in rets.columns and b != t:
                j = pd.concat([r, rets[b]], axis=1).dropna()
                row[f"beta_{name}"] = j.iloc[:, 0].cov(j.iloc[:, 1]) / j.iloc[:, 1].var()
        rows.append(row)
    return pd.DataFrame(rows)


def risk_class(ann_vol: float | None) -> str | None:
    if ann_vol is None or pd.isna(ann_vol):
        return None
    for name, upper in RISK_CLASSES:
        if ann_vol < upper:
            return name
    return RISK_CLASSES[-1][0]


def daily_returns(prices: pd.DataFrame, column: str = "adj_close", calendar: str | None = "SPY") -> pd.DataFrame:
    """Long prices (date, ticker, adj_close…) -> wide simple daily returns (no fill across missing quotes).

    calendar: keep only the dates on which this ticker trades (U.S. sessions), BEFORE taking returns, so
    24/7 crypto rows do not stretch a "252-day" window over weekends (a crypto Monday return is then
    measured from Friday, like an equity's)."""
    wide = prices.pivot_table(index="date", columns="ticker", values=column, aggfunc="last").sort_index()
    wide.index = pd.DatetimeIndex(wide.index)
    if calendar and calendar in wide.columns:
        wide = wide[wide[calendar].notna()]
    return wide.pct_change(fill_method=None).iloc[1:]


def market_metrics(prices: pd.DataFrame, window_days: int = TRADING_DAYS, benchmark: str = "SPY") -> pd.DataFrame:
    """Trailing-window metrics per ticker (listing currency) + last close and risk class."""
    rets = daily_returns(prices).tail(window_days)
    m = asset_metrics(rets, {"spy": benchmark}, f"trailing_{window_days}d")
    if m.empty:
        return m
    if "beta_spy" not in m:
        m["beta_spy"] = np.nan
    m.loc[m["ticker"] == benchmark, "beta_spy"] = 1.0  # by definition (asset_metrics skips the benchmark itself)
    last = (prices.dropna(subset=["close"]).sort_values("date").groupby("ticker").tail(1)
            .set_index("ticker")[["close", "date"]].rename(columns={"close": "last_close", "date": "last_close_date"}))
    m = m.merge(last, left_on="ticker", right_index=True, how="left")
    m["risk_class"] = m["ann_vol"].map(risk_class)
    for c in ("start", "end", "dd_peak", "dd_trough", "dd_recovery"):  # datetime.date -> ISO text (Parquet)
        m[c] = m[c].map(lambda d: d.isoformat() if hasattr(d, "isoformat") else None)
    return m


def correlation_edges(rets: pd.DataFrame, threshold: float = 0.7, min_obs: int = 120) -> pd.DataFrame:
    """Pairs (a < b) with |rho| >= threshold over the window; columns a, b, rho."""
    if rets.shape[1] < 2:
        return pd.DataFrame(columns=["a", "b", "rho"])
    c = rets.corr(min_periods=min_obs)
    cols = np.array(c.columns)
    v = c.to_numpy()
    i, j = np.triu_indices(len(cols), k=1)
    rho = v[i, j]
    keep = ~np.isnan(rho) & (np.abs(rho) >= threshold)
    return pd.DataFrame({"a": cols[i[keep]], "b": cols[j[keep]], "rho": rho[keep].round(4)})
