"""Reported company financials from SEC EDGAR XBRL ("company facts") → data/curated/fundamentals.csv.

    .venv/bin/python scripts/ingest_fundamentals.py --email you@example.com
    .venv/bin/python scripts/ingest_fundamentals.py --email you@example.com --tickers NVDA RY.TO --since 2020-01-01

Source: https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json (public domain, 10 requests/second at most, a
contact e-mail in the User-Agent is required). Two taxonomies are read:
- us-gaap: US filers (10-K annual, 10-Q quarterly) and some Canadian ones that report in US GAAP (40-F annual, 6-K interim);
- ifrs-full: Canadian banks, insurers and producers that report under IFRS (RY, TD, MFC, CNQ…): 40-F annual, 6-K interim.
Companies that do not file with the SEC (ATD, CSU) have no row, and the tool says so. Amounts keep their reporting currency
(`unit`: USD, CAD, USD/shares, CAD/shares): nothing is converted.

Point-in-time: every fact keeps the date it was FILED. The same period appears again in later filings (comparatives,
restatements); nothing is overwritten, and fundamentals_tools reads the latest filing made on or before `as_of`.

Long format, one row per (metric, period, filing):
  ticker, metric, period_kind (annual | quarter | instant), fiscal_year, fiscal_period, period_start, period_end,
  value, unit, form, filed, accession, tag
Durations are kept when they are a full year (annual forms) or a stand-alone quarter (3 months, quarterly forms);
cumulative year-to-date figures are dropped. Balance-sheet items (instant) are kept from every annual and quarterly filing.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

# metric -> (kind, [(taxonomy, tag), ...]) in order of preference. A company may change tag over the years and an IFRS filer
# uses other names: every pair is read, the first one that has a value for a period wins. kind: "flow" (duration) or
# "stock" (instant). Banks and insurers have no gross profit, capex or R&D: those metrics are simply absent for them.
G, I = "us-gaap", "ifrs-full"
METRICS: dict[str, tuple[str, list[tuple[str, str]]]] = {
    "revenue": ("flow", [(G, "RevenueFromContractWithCustomerExcludingAssessedTax"), (G, "Revenues"),
                         (G, "SalesRevenueNet"), (G, "RevenueFromContractWithCustomerIncludingAssessedTax"),
                         (G, "SalesRevenueGoodsNet"), (I, "Revenue"), (I, "RevenueFromContractsWithCustomers")]),
    "gross_profit": ("flow", [(G, "GrossProfit"), (I, "GrossProfit")]),
    "operating_income": ("flow", [(G, "OperatingIncomeLoss"), (I, "ProfitLossFromOperatingActivities")]),
    "net_income": ("flow", [(G, "NetIncomeLoss"), (G, "ProfitLoss"), (I, "ProfitLossAttributableToOwnersOfParent"),
                            (I, "ProfitLoss")]),
    "eps_diluted": ("flow", [(G, "EarningsPerShareDiluted"), (I, "DilutedEarningsLossPerShare")]),
    "operating_cash_flow": ("flow", [(G, "NetCashProvidedByUsedInOperatingActivities"),
                                     (I, "CashFlowsFromUsedInOperatingActivities")]),
    "capex": ("flow", [(G, "PaymentsToAcquirePropertyPlantAndEquipment"), (G, "PaymentsToAcquireProductiveAssets"),
                       (I, "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"),
                       (I, "PurchaseOfPropertyPlantAndEquipment")]),
    "rd_expense": ("flow", [(G, "ResearchAndDevelopmentExpense"), (I, "ResearchAndDevelopmentExpense")]),
    "total_assets": ("stock", [(G, "Assets"), (I, "Assets")]),
    "total_liabilities": ("stock", [(G, "Liabilities"), (I, "Liabilities")]),
    "equity": ("stock", [(G, "StockholdersEquity"),
                         (G, "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
                         (I, "EquityAttributableToOwnersOfParent"), (I, "Equity")]),
    "cash": ("stock", [(G, "CashAndCashEquivalentsAtCarryingValue"), (I, "CashAndCashEquivalents")]),
    "long_term_debt": ("stock", [(G, "LongTermDebtNoncurrent"), (G, "LongTermDebt")]),
}
ANNUAL_FORMS = {"10-K", "10-K/A", "10-KT", "40-F", "40-F/A", "20-F", "20-F/A"}
QUARTER_FORMS = {"10-Q", "10-Q/A", "6-K", "6-K/A"}
COLUMNS = ["ticker", "metric", "period_kind", "fiscal_year", "fiscal_period", "period_start", "period_end",
           "value", "unit", "form", "filed", "accession", "tag"]


def parse_company_facts(ticker: str, facts: dict, since: str = "2019-01-01") -> list[dict]:
    """Rows of one company from its XBRL company-facts JSON (pure function, no network)."""
    taxonomies = (facts.get("facts") or {})
    rows: dict[tuple, dict] = {}
    for metric, (kind, tags) in METRICS.items():
        for priority, (taxonomy, tag) in enumerate(tags):
            for unit, items in (((taxonomies.get(taxonomy) or {}).get(tag) or {}).get("units") or {}).items():
                for f in items:
                    form, filed, end = f.get("form"), f.get("filed"), f.get("end")
                    if not (form and filed and end and filed >= since and f.get("val") is not None):
                        continue
                    if kind == "stock":
                        if form not in ANNUAL_FORMS | QUARTER_FORMS:
                            continue
                        period_kind, start = "instant", None
                    else:
                        start = f.get("start")
                        if not start:
                            continue
                        days = (pd.Timestamp(end) - pd.Timestamp(start)).days
                        if form in ANNUAL_FORMS | QUARTER_FORMS and 350 <= days <= 380:
                            # a full year, also in a 6-K: Canadian companies publish their year-end results in a 6-K
                            period_kind = "annual"
                        elif form in QUARTER_FORMS and 80 <= days <= 100:
                            period_kind = "quarter"
                        else:
                            continue                      # year-to-date figures and other durations
                    key = (metric, period_kind, end, f.get("accn"))
                    row = {"ticker": ticker, "metric": metric, "period_kind": period_kind,
                           "fiscal_year": f.get("fy"), "fiscal_period": f.get("fp"), "period_start": start,
                           "period_end": end, "value": f["val"], "unit": unit, "form": form, "filed": filed,
                           "accession": f.get("accn"), "tag": f"{taxonomy}:{tag}", "_priority": priority}
                    if key not in rows or priority < rows[key]["_priority"]:
                        rows[key] = row
    return [{k: v for k, v in r.items() if k != "_priority"} for r in rows.values()]


def merge_partial(old: pd.DataFrame, new: pd.DataFrame, tickers: list[str]) -> pd.DataFrame:
    """Rows of `old` for the tickers that were re-read are replaced by `new`; every other company is kept as is."""
    return pd.concat([old[~old["ticker"].isin(tickers)], new], ignore_index=True)


def main() -> int:
    import requests

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--email", required=True, help="contact e-mail for the SEC User-Agent (required by the SEC)")
    ap.add_argument("--tickers", nargs="*", help="universe tickers (default: every equity of the universe)")
    ap.add_argument("--since", default="2019-01-01", help="keep facts filed on or after this date")
    ap.add_argument("--universe", default=str(ROOT / "data/curated/assets_universe.csv"))
    ap.add_argument("--out", default=str(ROOT / "data/curated/fundamentals.csv"))
    a = ap.parse_args()

    h = {"User-Agent": f"Fitfolio Datathon {a.email}"}
    uni = pd.read_csv(a.universe)
    wanted = a.tickers or uni.loc[uni["asset_class"] == "equity", "ticker"].tolist()
    from sec_cik import cik_for, cik_map
    sec = cik_map(h)
    all_rows, report = [], []
    for t in wanted:
        c = cik_for(t, sec)
        if not c:
            report.append((t, "no SEC CIK (not an SEC filer)"))
            continue
        time.sleep(0.15)                                  # SEC: at most 10 requests per second
        r = requests.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{c}.json", headers=h, timeout=60)
        if r.status_code != 200:
            report.append((t, f"HTTP {r.status_code}"))
            continue
        entity = r.json().get("entityName")
        rows = parse_company_facts(t, r.json(), a.since)
        if not rows:
            report.append((t, "no us-gaap / ifrs-full annual or quarterly facts"))
            continue
        all_rows += rows
        report.append((t, f"{len(rows)} facts  [SEC entity: {entity}]"))
    df = pd.DataFrame(all_rows, columns=COLUMNS)
    if df.empty:
        print("No fundamentals found.", file=sys.stderr)
        return 1
    out = Path(a.out)
    if a.tickers and out.exists():      # a partial run replaces only its own tickers, never the whole file
        df = merge_partial(pd.read_csv(out), df, a.tickers)
    df = df.sort_values(["ticker", "metric", "period_kind", "period_end", "filed"]).reset_index(drop=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    for t, msg in report:
        print(f"  {t:<8} {msg}")
    print(f"{len(df)} rows, {df['ticker'].nunique()} companies → {out}  (filed {df['filed'].min()} … {df['filed'].max()})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
