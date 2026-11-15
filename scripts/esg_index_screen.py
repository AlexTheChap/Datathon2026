"""
Which companies do ESG indices leave out? → data/curated/esg_index_screen.csv

    python scripts/esg_index_screen.py            (after scripts/etf_holdings.py)

Comparisons (broad "parent" fund vs ESG fund of the same market):
  US      SPY (S&P 500)  vs  ESGV  (FTSE US All Cap Choice: activity exclusions, controversies, diversity)
  Canada  XIU (S&P/TSX 60) vs XCSR (MSCI Canada IMI Extended ESG Leaders: exclusions + best ESG ratings)
          XESG (MSCI Canada IMI Extended ESG Focus) for information: an OPTIMIZED index, it keeps almost
          everything and only over/underweights; an absence from XESG is rare and more telling.

WHAT THIS FILE SAYS, AND DOES NOT SAY
  - "absent_from_esg_index" = the company is in the parent fund but NOT in the ESG fund. The holdings
    do NOT say WHY (activity, controversy, ESG rating, diversity criterion, universe or liquidity).
    Wording to use: "excluded from the FTSE US All Cap Choice index (ESGV)", NEVER "fails ESG".
  - Comparison by TICKER, not CUSIP: SPY holds Alphabet as class A (GOOGL) and ESGV as class C (GOOG),
    two different CUSIPs; tickers are normalized (GOOG→GOOGL). As a fallback, the first 6 characters of the
    CUSIP (issuer code, shared by all share classes) avoid other false positives (FOX/FOXA…).
  - Dates: SPY/XIU/XCSR/XESG as of download day, ESGV as of the latest N-PORT (≈ 2-3 months earlier). A company
    added to the S&P 500 after the ESGV date is marked "undetermined_recent_addition".
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from etf_holdings import fetch_ishares_ca, sp500_table  # noqa: E402

CLASS_ALIASES = {"GOOG": "GOOGL", "FOX": "FOXA", "NWS": "NWSA", "BRK-A": "BRK-B"}
REASON = "Not determined by holdings alone: activity, controversy, ESG rating, diversity or universe rule."
INDEX_LABEL = {
    "ESGV": "FTSE US All Cap Choice (Vanguard ESGV)",
    "XCSR.TO": "MSCI Canada IMI Extended ESG Leaders (iShares XCSR)",
    "XESG.TO": "MSCI Canada IMI Extended ESG Focus (iShares XESG)",
}


def compare(parent: pd.DataFrame, esg: pd.DataFrame, parent_name: str, esg_name: str,
            added: dict | None = None) -> pd.DataFrame:
    p, e = parent.copy(), esg.copy()
    for d in (p, e):
        d["tk"] = d["ticker"].replace(CLASS_ALIASES)
        d["issuer"] = d["cusip"].fillna("").str[:6]
    e_w = e[e["tk"] != ""].groupby("tk")["weight"].sum()
    e_issuers = set(e.loc[e["issuer"].str.len() == 6, "issuer"])
    p = p.groupby("tk", as_index=False).agg(name=("name", "first"), issuer=("issuer", "first"),
                                            weight=("weight", "sum"), date=("holdings_date", "first"))
    esg_date = str(e["holdings_date"].iloc[0])
    rows = []
    for r in p.itertuples():
        in_esg = r.tk in e_w.index or (len(r.issuer) == 6 and r.issuer in e_issuers)
        if in_esg:
            status, reason = "present", ""
        elif added and str(added.get(r.tk, "")) > esg_date:
            status = "undetermined_recent_addition"
            reason = f"Added to the parent fund on {added[r.tk]}, after the ESG holdings of {esg_date}."
        else:
            status, reason = "absent_from_esg_index", REASON
        rows.append({
            "ticker": r.tk, "name": r.name, "parent_fund": parent_name, "esg_fund": esg_name,
            "esg_index": INDEX_LABEL[esg_name], "in_esg_fund": in_esg, "status": status,
            "weight_parent": round(float(r.weight), 6),
            "weight_esg": round(float(e_w.get(r.tk, 0.0)), 6) if in_esg else 0.0,
            "parent_date": r.date, "esg_date": esg_date, "reason": reason,
            "wording": (f"Excluded from the {INDEX_LABEL[esg_name]} index (reason not published in the holdings)"
                            if status == "absent_from_esg_index" else ""),
        })
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--holdings", default="data/curated/etf_holdings.csv")
    ap.add_argument("--universe", default="data/curated/assets_universe.csv")
    ap.add_argument("--out", default="data/curated/esg_index_screen.csv")
    a = ap.parse_args()
    h = pd.read_csv(a.holdings, dtype={"ticker": str, "cusip": str}, keep_default_na=False)
    h["weight"] = h["weight"].astype(float)
    uni = pd.read_csv(a.universe)
    parts = []

    # US: SPY vs ESGV
    try:
        sp = sp500_table()
        added = dict(zip(sp["Symbol"].replace(CLASS_ALIASES), sp["Date added"].astype(str)))
    except Exception as ex:
        print(f"(S&P 500 addition dates unavailable: {ex})")
        added = None
    parts.append(compare(h[h.fund == "SPY"], h[h.fund == "ESGV"], "SPY", "ESGV", added))

    # Canada: XIU vs XCSR (and XESG)
    xiu = h[h.fund == "XIU.TO"]
    for esg_fund in ["XCSR.TO", "XESG.TO"]:
        try:
            parts.append(compare(xiu, fetch_ishares_ca(esg_fund), "XIU.TO", esg_fund))
        except Exception as ex:
            print(f"[{esg_fund}] FAILED: {ex}")

    df = pd.concat(parts, ignore_index=True)
    df["in_universe"] = df["ticker"].isin(uni["ticker"])
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False, encoding="utf-8")

    for (pf, ef), g in df.groupby(["parent_fund", "esg_fund"]):
        ab = g[g.status == "absent_from_esg_index"]
        print(f"{pf} vs {ef}: {len(ab)}/{len(g)} absent ({ab.weight_parent.sum():.1%} of {pf} weight), "
              f"{(g.status == 'undetermined_recent_addition').sum()} undetermined recent additions")
        mine = g[g.in_universe]
        if len(mine):
            print("   Fitfolio universe: " + ", ".join(
                f"{r.ticker} {'✓' if r.in_esg_fund else '✗'}" for r in mine.itertuples()))
    print(f"→ {out}")


if __name__ == "__main__":
    main()
