"""
Extracts the holdings of US ETFs (SPY, QQQ, ESGV...) from the SEC N-PORT data sets
(public domain) and produces:
    nport_holdings.csv        fund, cusip, issuer_name, weight, report_date, accession
    nport_esg_screen.csv      SPY holdings missing from ESGV ("to_verify" signal)

1. Manually download a quarterly zip from the SEC "Form N-PORT Data Sets" page
   (https://www.sec.gov/data-research/sec-markets-data/form-n-port-data-sets).
2. Unzip it, then run:
       python scripts/nport_holdings.py --dir path/to/unzipped_folder --out data/external

The file and column names below follow the SEC documentation (README in the zip).
If they differ, the script prints the available columns: adjust COLS.
For a simulated "as_of", use the quarter whose FILING date is <= as_of.
XIU.TO (Canadian ETF) is NOT in N-PORT: use the iShares Canada holdings file.
"""
import argparse
from pathlib import Path

import pandas as pd

FUNDS = {  # label -> pattern searched in the series name
    "SPY": "SPDR S&P 500 ETF",
    "QQQ": "Invesco QQQ",
    "ESGV": "ESG U.S. Stock ETF",
}
COLS = {"series_name": "SERIES_NAME", "accession": "ACCESSION_NUMBER", "issuer": "ISSUER_NAME",
        "cusip": "ISSUER_CUSIP", "pct": "PERCENTAGE", "report_date": "REPORT_DATE"}


def read(dirpath: Path, name: str) -> pd.DataFrame:
    p = next(iter(dirpath.glob(f"{name}*.tsv")), None)
    if p is None:
        raise FileNotFoundError(f"{name}*.tsv not found in {dirpath}")
    return pd.read_csv(p, sep="\t", dtype=str, low_memory=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--out", default="data/external")
    a = ap.parse_args()
    d, out = Path(a.dir), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    info = read(d, "FUND_REPORTED_INFO")
    hold = read(d, "FUND_REPORTED_HOLDING")
    sub = read(d, "SUBMISSION")
    for df, need in [(info, ["series_name", "accession"]), (hold, ["accession", "issuer", "cusip", "pct"])]:
        miss = [COLS[c] for c in need if COLS[c] not in df.columns]
        if miss:
            raise KeyError(f"Missing columns {miss}. Available : {list(df.columns)}")

    rows = []
    for fund, pattern in FUNDS.items():
        f = info[info[COLS["series_name"]].str.contains(pattern, case=False, na=False)]
        if f.empty:
            print(f"[{fund}] series not found in this quarter")
            continue
        acc = f[COLS["accession"]].iloc[-1]
        h = hold[hold[COLS["accession"]] == acc]
        rd = sub.loc[sub[COLS["accession"]] == acc, COLS["report_date"]].iloc[0] \
            if COLS["report_date"] in sub.columns else None
        rows.append(pd.DataFrame({
            "fund": fund, "cusip": h[COLS["cusip"]], "issuer_name": h[COLS["issuer"]],
            "weight": pd.to_numeric(h[COLS["pct"]], errors="coerce") / 100,
            "report_date": rd, "accession": acc}))
        print(f"[{fund}] {len(h)} positions, sum of weights = {rows[-1]['weight'].sum():.3f}")

    hol = pd.concat(rows, ignore_index=True)
    hol.to_csv(out / "nport_holdings.csv", index=False)

    if {"SPY", "ESGV"} <= set(hol["fund"]):
        spy, esg = hol[hol.fund == "SPY"], hol[hol.fund == "ESGV"]
        m = spy.merge(esg[["cusip"]].assign(in_esgv=True), on="cusip", how="left")
        m["in_esgv"] = m["in_esgv"].fillna(False)
        m["status"] = m["in_esgv"].map({True: "present_in_esgv", False: "to_verify"})
        m["note"] = ("Absent from ESGV: removed by the FTSE Choice filter (activity, controversy or diversity "
                     "criterion) OR universe/CUSIP matching gap. Reason not determined here.")
        m[["cusip", "issuer_name", "weight", "in_esgv", "status", "report_date", "accession", "note"]] \
            .to_csv(out / "nport_esg_screen.csv", index=False)
        ex = m[~m.in_esgv]
        print(f"SPY vs ESGV : {len(ex)} holdings missing from ESGV ({ex.weight.sum():.1%} of SPY weight)")


if __name__ == "__main__":
    main()
