"""Reported company financials (node ⑥): revenue, profit, margins, cash flow, leverage, from SEC XBRL filings.

Data: data/curated/fundamentals.csv (scripts/ingest_fundamentals.py), long format, one row per (metric, period, FILING).
Deterministic pandas, no LLM. Point-in-time: only filings made on or before `as_of` are read, and for each period the
LATEST such filing wins (a restatement filed before `as_of` replaces the first publication; one filed after is invisible).
`as_of` is the run's reference date (never a later one asked by the model).

Every ratio is computed here, so the Fundamentals sub-agent cites numbers and never computes them. Amounts stay in each
company's REPORTING currency (USD for most US filers, CAD for most Canadian ones): `currency` is given per company and
nothing is converted. No valuation ratio (P/E, EV/EBITDA…) is produced: there is no share-count series, and prices (CAD) and
filings (USD or CAD) would not match.
Evidence envelope as every other tool (evidence_id, say, output, assumptions, data_sources).
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pandas as pd
from app.core.config import get_settings
from app.tools.macro_tools import _envelope
from app.tools.context import effective_as_of
from strands import tool

FLOW = ("revenue", "gross_profit", "operating_income", "net_income", "eps_diluted", "operating_cash_flow", "capex",
        "rd_expense")
STOCK = ("total_assets", "total_liabilities", "equity", "cash", "long_term_debt")
MAX_TICKERS = 12


@lru_cache
def _load(path: str, mtime: float) -> pd.DataFrame:  # mtime in the key: reload when the file is rebuilt
    df = pd.read_csv(path, dtype={"ticker": str, "metric": str, "period_kind": str, "form": str})
    df["period_end"] = pd.to_datetime(df["period_end"])
    return df


@lru_cache
def _sectors(path: str, mtime: float) -> dict[str, str]:
    from app.tools import curated_store as cs

    u = cs.read_extended(Path(path).parent, "assets_universe")   # + S&P 500 extension
    return dict(zip(u["ticker"], u["sector"], strict=False))


def _sector(ticker: str) -> str | None:
    s = get_settings()
    path = s.abs_path(s.curated_dir) / "assets_universe.csv"
    return _sectors(str(path), path.stat().st_mtime).get(ticker) if path.is_file() else None


def _currency(df: pd.DataFrame) -> str:
    """Reporting currency of the company: the most frequent currency of its rows ("CAD/shares" -> "CAD")."""
    return df["unit"].str.split("/").str[0].value_counts().index[0]


def _facts() -> pd.DataFrame:
    s = get_settings()
    path = s.abs_path(s.curated_dir) / "fundamentals.csv"
    if not path.is_file():
        raise ValueError(f"No fundamentals file: {path}. Build it with scripts/ingest_fundamentals.py.")
    return _load(str(path), path.stat().st_mtime)


def _latest_known(df: pd.DataFrame) -> pd.DataFrame:
    """One row per (metric, period_kind, period_end): the latest filing (restatements replace the first publication)."""
    return df.sort_values("filed").groupby(["metric", "period_kind", "period_end"], as_index=False).last()


def _money(x: float, currency: str = "USD") -> str:
    """$416.2B for USD, C$17.96B for CAD (the verifier reads the `$`)."""
    sym = {"USD": "$", "CAD": "C$"}.get(currency, f"{currency} ")
    a = abs(x)
    if a >= 1e9:
        return f"{sym}{x / 1e9:.1f}B"
    if a >= 1e6:
        return f"{sym}{x / 1e6:.1f}M"
    return f"{sym}{x:,.2f}" if a < 1000 else f"{sym}{x:,.0f}"


def _ratio(num: float | None, den: float | None) -> float | None:
    return round(num / den, 4) if num is not None and den not in (None, 0) else None


def _value(known: pd.DataFrame, metric: str, kind: str, end: pd.Timestamp) -> float | None:
    m = known[(known["metric"] == metric) & (known["period_kind"] == kind) & (known["period_end"] == end)]
    return float(m["value"].iloc[0]) if len(m) else None


def _near(known: pd.DataFrame, metric: str, kind: str, end: pd.Timestamp, days: int, tol: int = 20):
    """Period of `metric` ending about `days` before `end` (prior year or same quarter last year)."""
    m = known[(known["metric"] == metric) & (known["period_kind"] == kind)]
    delta = (end - m["period_end"]).dt.days
    m = m[(delta - days).abs() <= tol]
    return None if m.empty else m.sort_values("period_end").iloc[-1]


def _company(df: pd.DataFrame, sector: str | None = None) -> dict | None:
    currency = _currency(df)
    df = df[df["unit"].str.split("/").str[0] == currency]            # one reporting currency per company
    known = _latest_known(df)
    annual = known[(known["period_kind"] == "annual") & known["metric"].isin(["revenue", "net_income"])]
    quarters = known[(known["period_kind"] == "quarter") & known["metric"].isin(["revenue", "net_income"])]
    out: dict = {"currency": currency}
    if sector == "Financials":
        out["note"] = ("Bank or insurer: revenue, margins and leverage are not comparable with industrial companies "
                       "(no gross profit; liabilities are mostly deposits and policy liabilities).")
    fy_end = annual["period_end"].max() if len(annual) else None
    if fy_end is not None:
        a = {m: _value(known, m, "annual", fy_end) for m in FLOW}
        meta = df[(df["period_kind"] == "annual") & (df["period_end"] == fy_end)].sort_values("filed").iloc[-1]
        fcf = None if a["operating_cash_flow"] is None or a["capex"] is None else a["operating_cash_flow"] - a["capex"]
        prev = {m: _near(known, m, "annual", fy_end, 365) for m in ("revenue", "net_income")}
        growth = {"revenue_yoy": None, "net_income_yoy": None}
        if prev["revenue"] is not None and a["revenue"] is not None and prev["revenue"]["value"] > 0:
            growth["revenue_yoy"] = round(a["revenue"] / float(prev["revenue"]["value"]) - 1, 4)
        if prev["net_income"] is not None and a["net_income"] is not None and prev["net_income"]["value"] > 0:
            growth["net_income_yoy"] = round(a["net_income"] / float(prev["net_income"]["value"]) - 1, 4)
        out.update({
            "fiscal_year_end": str(fy_end.date()), "form": str(meta["form"]), "filed": str(meta["filed"]),
            "annual": {**{k: v for k, v in a.items() if v is not None},
                       **({"free_cash_flow": fcf} if fcf is not None else {})},
            "growth_yoy": growth,
            "margins": {"gross": _ratio(a["gross_profit"], a["revenue"]),
                        "operating": _ratio(a["operating_income"], a["revenue"]),
                        "net": _ratio(a["net_income"], a["revenue"])},
        })
    stock = known[known["period_kind"] == "instant"]
    if len(stock):
        b_end = stock["period_end"].max()
        b = {m: _value(known, m, "instant", b_end) for m in STOCK}
        fy_equity = _value(known, "equity", "instant", fy_end) if fy_end is not None else None
        out["balance_sheet"] = {
            "period_end": str(b_end.date()), **{k: v for k, v in b.items() if v is not None},
            "liabilities_to_equity": _ratio(b["total_liabilities"], b["equity"]) if (b["equity"] or 0) > 0 else None,
            "long_term_debt_to_equity": _ratio(b["long_term_debt"], b["equity"]) if (b["equity"] or 0) > 0 else None,
            "return_on_equity": (_ratio(out["annual"].get("net_income"), fy_equity)
                                 if fy_end is not None and (fy_equity or 0) > 0 else None),
        }
    q_end = quarters["period_end"].max() if len(quarters) else None
    if q_end is not None and (fy_end is None or q_end > fy_end):
        q = {m: _value(known, m, "quarter", q_end) for m in ("revenue", "net_income", "eps_diluted")}
        prior = _near(known, "revenue", "quarter", q_end, 365)
        meta = df[(df["period_kind"] == "quarter") & (df["period_end"] == q_end)].sort_values("filed").iloc[-1]
        out["latest_quarter"] = {
            "period_end": str(q_end.date()), "form": str(meta["form"]), "filed": str(meta["filed"]),
            **{k: v for k, v in q.items() if v is not None},
            "revenue_yoy": (round(q["revenue"] / float(prior["value"]) - 1, 4)
                            if prior is not None and q["revenue"] is not None and prior["value"] > 0 else None)}
    return out if "annual" in out or "balance_sheet" in out or "latest_quarter" in out else None


def _say(t: str, c: dict) -> str:
    cur = c["currency"]
    if "annual" in c:
        a, g, m = c["annual"], c["growth_yoy"], c["margins"]
        parts = [f"{t}: fiscal year ended {c['fiscal_year_end']} ({c['form']} filed {c['filed']})"]
        if "revenue" in a:
            rev = f"revenue {_money(a['revenue'], cur)}"
            parts.append(rev + (f" ({g['revenue_yoy'] * 100:+.1f}% y/y)" if g["revenue_yoy"] is not None else ""))
        if "net_income" in a:
            ni = f"net income {_money(a['net_income'], cur)}"
            show_margin = m["net"] is not None and "note" not in c       # a bank's "margin" would mislead
            parts.append(ni + (f" ({m['net'] * 100:.1f}% margin)" if show_margin else ""))
        return ", ".join(parts) + "."
    if "latest_quarter" in c:
        q = c["latest_quarter"]
        return (f"{t}: only quarterly figures before the reference date; quarter ended {q['period_end']} "
                f"({q['form']} filed {q['filed']})" + (f", revenue {_money(q['revenue'], cur)}" if "revenue" in q else "")
                + ".")
    return f"{t}: balance sheet only (period ended {c['balance_sheet']['period_end']})."


def fundamentals_snapshot(tickers: list[str], as_of: str | None = None) -> dict:
    """Plain function behind the `company_fundamentals` tool (also used by tests and scripts)."""
    if not tickers:
        raise ValueError("Give at least one ticker.")
    tickers = list(dict.fromkeys(tickers))[:MAX_TICKERS]
    cut = effective_as_of(as_of)
    facts = _facts()
    facts = facts[facts["filed"] <= cut]                           # point-in-time: filings made on or before as_of
    companies, no_data = {}, {}
    for t in tickers:
        known_t = facts[facts["ticker"] == t]
        if known_t.empty:
            covered = set(_facts()["ticker"])
            no_data[t] = ("no SEC XBRL (10-K / 10-Q) filing on or before this date" if t in covered
                          else "not covered: no SEC XBRL financials (not an SEC filer, e.g. ATD.TO and CSU.TO)")
            continue
        c = _company(known_t, _sector(t))
        if c is None:
            no_data[t] = "no usable annual or quarterly figures on or before this date"
        else:
            companies[t] = c
    inputs = {"tickers": tickers, "as_of": cut}
    out = {"companies": companies, "no_data": no_data, "as_of": cut}
    say = " ".join(_say(t, c) for t, c in list(companies.items())[:2]) or "No fundamentals available for these tickers."
    if no_data:
        say += f" No data: {', '.join(no_data)}."
    return {**_envelope(
        "company_fundamentals", inputs, out, say,
        [("Only filings made by the analysis date are used; when a company restated a period before that date, "
          "the restated figures are used."),
         ("Amounts are in each company's reporting currency (U.S. or Canadian dollars) and are never converted: "
          "check the currency before comparing two companies. No valuation ratio is given."),
         "Fiscal years don't all end in December: compare companies with care.",
         ("Only companies that file their financial statements with the U.S. SEC are covered. Margins and growth are "
          "calculated from the reported figures. Banks' and insurers' figures are not comparable with other companies'."),
         "Reported past results; not a forecast and not a recommendation."],
        ["data/curated/fundamentals.csv (SEC EDGAR XBRL company facts, public domain)"]), "as_of": cut}


@tool
def company_fundamentals(tickers: list[str]) -> dict:
    """Reported financials of listed companies from their SEC filings (10-K annual, 10-Q quarterly), point-in-time
    (never filed after the run's reference date): revenue, gross / operating / net income, diluted EPS, operating and
    free cash flow, margins, year-over-year growth, balance sheet (assets, liabilities, equity, cash, long-term debt),
    leverage and return on equity. All ratios are computed by the tool. Figures stay in each company's reporting
    currency (`currency`: USD or CAD), never converted. Companies that do not file with the SEC (ATD.TO, CSU.TO) have
    no data: the result says so. Banks and insurers carry a `note`. Returns an evidence record: cite its evidence_id.
    Args:
        tickers: Universe tickers, e.g. ["NVDA", "AAPL"] (at most 12).
    """
    try:
        return fundamentals_snapshot(tickers)
    except (KeyError, ValueError) as e:
        return {"error": str(e), "note": "Fix the arguments and call the tool again."}
