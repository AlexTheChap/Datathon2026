"""
Holdings of the universe's equity ETFs → data/curated/etf_holdings.csv
    fund, ticker, name, cusip, sector, weight, holdings_date, source

    pip install requests pandas openpyxl
    python scripts/etf_holdings.py --as-of 2026-11-14 --email your.email@example.com

Sources (verified on 2026-11-14):
  SPY     State Street daily file (xlsx). SPY is a unit investment trust (UIT): no N-PORT.
  XIU.TO  iShares Canada holdings file (csv, with sectors).
  QQQ     SEC N-PORT (series S000101292; QQQ is no longer a UIT and files N-PORTs).
  ESGV    SEC N-PORT (series S000063075, Vanguard World Fund).
Not broken down (no equities): XBB.TO, ZST.TO, AGG, TLT (bonds), GLD (gold).

Point-in-time: N-PORT = latest report FILED on or before --as-of (published ~60 days after
month end). Issuer files (SPY, XIU) only exist as of today's date: if that date is later
than --as-of, a warning is printed and the holdings_date column shows it.
N-PORT tickers are recovered by CUSIP via the SPY file (S&P 500); otherwise the ticker is empty.
Sectors: AlphaForge universe, then iShares file (XIU), then GICS from the Wikipedia S&P 500
list (CURRENT sectors); otherwise empty ("unclassified").
"""
from __future__ import annotations

import argparse
import io
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pandas as pd
import requests

BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
SPY_URL = "https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx"
ISHARES_CA = {  # fund -> product path on blackrock.com/ca (verified on 2026-11-14)
    "XIU.TO": "239832/ishares-sptsx-60-index-etf",
    "XESG.TO": "307298/ishares-esg-aware-msci-canada-index-etf",
    "XCSR.TO": "313580/ishares-esg-advanced-msci-canada-index-etf",
}


def ishares_ca_url(fund: str) -> str:
    sym = fund.split(".")[0]
    return (f"https://www.blackrock.com/ca/investors/en/products/{ISHARES_CA[fund]}/"
            f"1464253357814.ajax?fileType=csv&fileName={sym}_holdings&dataType=fund")
NPORT_SERIES = {"QQQ": "S000101292", "ESGV": "S000063075"}


def sec_headers(email):
    return {"User-Agent": f"AlphaForge Datathon {email}"}


def fetch_spy() -> pd.DataFrame:
    raw = pd.read_excel(io.BytesIO(requests.get(SPY_URL, headers=BROWSER, timeout=60).content), header=None)
    date = pd.to_datetime(str(raw.iloc[2, 1]).replace("As of", "").strip()).date()
    hdr = raw.index[raw.iloc[:, 0].astype(str).str.strip() == "Name"][0]
    df = raw.iloc[hdr + 1:].copy()
    df.columns = [str(c).strip() for c in raw.iloc[hdr]]
    df = df[df["Ticker"].notna() & (df["Ticker"].astype(str).str.strip() != "-")]
    df = df[pd.to_numeric(df["Weight"], errors="coerce").notna()]
    return pd.DataFrame({"fund": "SPY", "ticker": df["Ticker"].astype(str).str.strip().str.replace(".", "-", regex=False),
                         "name": df["Name"].astype(str).str.strip(), "cusip": df["Identifier"].astype(str).str.strip(),
                         "sector": "", "weight": pd.to_numeric(df["Weight"]) / 100,
                         "holdings_date": str(date), "source": "State Street (SSGA) holdings-daily SPY"})


def fetch_ishares_ca(fund: str) -> pd.DataFrame:
    text = requests.get(ishares_ca_url(fund), headers=BROWSER, timeout=60).content.decode("utf-8-sig")
    date = pd.to_datetime(re.search(r'as of,"([^"]+)"', text).group(1)).date()
    start = text.index("Ticker,")
    df = pd.read_csv(io.StringIO(text[start:]), thousands=",")
    df = df[df["Asset Class"] == "Equity"]
    tick = df["Ticker"].astype(str).str.strip().str.replace(".", "-", regex=False)  # GIB.A -> GIB-A.TO (Yahoo)
    tick = tick + df["Exchange"].str.contains("Toronto", na=False).map({True: ".TO", False: ""})
    return pd.DataFrame({"fund": fund, "ticker": tick, "name": df["Name"].str.strip(), "cusip": "",
                         "sector": df["Sector"], "weight": pd.to_numeric(df["Weight (%)"]) / 100,
                         "holdings_date": str(date), "source": f"iShares Canada {fund} holdings"})


def fetch_xiu() -> pd.DataFrame:
    return fetch_ishares_ca("XIU.TO")


def fetch_nport(fund: str, series: str, as_of: str, email: str) -> pd.DataFrame:
    atom = requests.get("https://www.sec.gov/cgi-bin/browse-edgar", headers=sec_headers(email), timeout=60, params={
        "action": "getcompany", "CIK": series, "type": "NPORT-P", "dateb": as_of.replace("-", ""),
        "owner": "include", "count": 10, "output": "atom"}).text
    hrefs = re.findall(r"<filing-href>(.*?)</filing-href>", atom)
    filed = re.findall(r"<filing-date>(.*?)</filing-date>", atom)
    if not hrefs:
        raise RuntimeError(f"no NPORT-P filed before {as_of} for {series}")
    xml_url = hrefs[0].rsplit("/", 1)[0] + "/primary_doc.xml"
    root = ET.fromstring(requests.get(xml_url, headers=sec_headers(email), timeout=120).content)
    ns = {"n": root.tag.split("}")[0].strip("{")}
    period = root.find(".//n:repPdDate", ns).text
    rows = []
    for s in root.findall(".//n:invstOrSec", ns):
        if (s.findtext("n:assetCat", "", ns) != "EC") or (s.findtext("n:payoffProfile", "", ns) != "Long"):
            continue
        rows.append({"name": s.findtext("n:name", "", ns), "cusip": s.findtext("n:cusip", "", ns),
                     "weight": float(s.findtext("n:pctVal", "0", ns)) / 100})
    df = pd.DataFrame(rows)
    return df.assign(fund=fund, ticker="", sector="", holdings_date=period,
                     source=f"SEC N-PORT {series} (period {period}, filed {filed[0]}) {xml_url}")


def sp500_table() -> pd.DataFrame:
    """Wikipedia S&P 500 list (CC BY-SA): Symbol, Security, GICS Sector, Date added…"""
    html = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                        headers={"User-Agent": "AlphaForge Datathon (research script)"}, timeout=30).text
    t = pd.read_html(io.StringIO(html))[0]
    t["Symbol"] = t["Symbol"].str.replace(".", "-", regex=False)
    return t


def sp500_sectors() -> dict:
    t = sp500_table()
    return dict(zip(t["Symbol"], t["GICS Sector"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", required=True)
    ap.add_argument("--email", required=True, help="required by the SEC in the User-Agent")
    ap.add_argument("--universe", default="data/curated/assets_universe.csv")
    ap.add_argument("--out", default="data/curated/etf_holdings.csv")
    a = ap.parse_args()
    uni = pd.read_csv(a.universe).set_index("ticker")

    parts = []
    for fund, fn in [("SPY", fetch_spy), ("XIU.TO", fetch_xiu)]:
        try:
            parts.append(fn())
        except Exception as e:
            print(f"[{fund}] FAILED: {e}")
    for fund, series in NPORT_SERIES.items():
        try:
            parts.append(fetch_nport(fund, series, a.as_of, a.email))
        except Exception as e:
            print(f"[{fund}] FAILED: {e}")
    if not parts:
        raise SystemExit("No holdings retrieved.")
    df = pd.concat(parts, ignore_index=True)

    # Missing tickers (N-PORT): match by CUSIP against the SPY file
    spy = df[(df.fund == "SPY") & (df.cusip != "")]
    cusip_to_ticker = dict(zip(spy.cusip, spy.ticker))
    miss = df.ticker == ""
    df.loc[miss, "ticker"] = df.loc[miss, "cusip"].map(cusip_to_ticker).fillna("")
    # Universe: GOOGL/GOOG -> GOOGL; universe sector takes priority
    df["ticker"] = df["ticker"].replace({"GOOG": "GOOGL"})
    in_uni = df.ticker.isin(uni.index)
    df.loc[in_uni, "sector"] = df.loc[in_uni, "ticker"].map(uni["sector"])
    df["sector"] = df["sector"].fillna("")
    try:  # GICS sectors of S&P 500 stocks (Wikipedia list, CC BY-SA) for US stocks outside the universe
        gics = sp500_sectors()
        need = df["sector"] == ""
        df.loc[need, "sector"] = df.loc[need, "ticker"].map(gics).fillna("")
    except Exception as e:
        print(f"(S&P 500 sectors unavailable: {e})")
    df["key"] = df["ticker"].where(df["ticker"] != "", "CUSIP:" + df["cusip"])  # one row per company and per ETF
    df = (df.groupby(["fund", "key"], as_index=False)
            .agg(ticker=("ticker", "first"), cusip=("cusip", "first"), name=("name", "first"),
                 sector=("sector", "first"), weight=("weight", "sum"),
                 holdings_date=("holdings_date", "first"), source=("source", "first")))
    df = df.sort_values(["fund", "weight"], ascending=[True, False])

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df[["fund", "ticker", "name", "cusip", "sector", "weight", "holdings_date", "source"]].to_csv(out, index=False)
    for fund, g in df.groupby("fund"):
        flag = "  ⚠ later than --as-of" if g.holdings_date.iloc[0] > a.as_of else ""
        print(f"[{fund}] {len(g):>5} holdings, total weight {g.weight.sum():.3f}, with ticker {(g.ticker != '').mean():.0%}, "
              f"with sector {g.loc[g.sector != '', 'weight'].sum() / g.weight.sum():.0%} of weight, "
              f"as of {g.holdings_date.iloc[0]}{flag}")
    print(f"→ {out}")


if __name__ == "__main__":
    main()
