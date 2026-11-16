"""
ESG exclusion flags per ticker → data/curated/esg_flags.json

    python scripts/build_esg_flags.py             # build from data/raw/esg/ (downloads the NBIM file if missing)
    python scripts/build_esg_flags.py --refresh   # download the NBIM file again first

Source (non-commercial use only: this is a datathon prototype):
  [1] NBIM, observation and exclusion of companies. Cited as https://www.nbim.no/en/responsibility/exclusion-of-companies
      Machine-readable copy: OpenSanctions dataset "no_nbim_exclusions", full export (entities.ftm.json). The simple
      CSV export has the company and criterion but neither the status (Exclusion / Observation) nor the decision
      date; the full export of the same dataset has both. If the download fails, the script says where to save
      the file by hand and stops.

  [2..] Manual additions: data/external/esg_manual_flags.csv (maintained by hand). One row per flag: ticker, reason
      (same wording rule), since, and the public document that supports it (source_name, source_url, source_as_of),
      with the quoted passage in `evidence`. Each distinct document becomes the next numbered source.

Refresh: re-run with --refresh. The JSON is rebuilt from the raw file and the manual additions every time.
Names are mapped to tickers ONLY through data/external/esg_ticker_map.csv (maintained by hand, never guessed), and
only tickers of the project universe (data/curated/assets_universe*.csv) are kept. Everything else is listed in
data/external/esg_flags_unmapped.csv, to be resolved by hand.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import urllib.request
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "esg"
NBIM_RAW = RAW / "nbim_exclusions.ftm.json"
MAP_CSV = ROOT / "data" / "external" / "esg_ticker_map.csv"
MANUAL_CSV = ROOT / "data" / "external" / "esg_manual_flags.csv"
UNMAPPED_CSV = ROOT / "data" / "external" / "esg_flags_unmapped.csv"
OUT = ROOT / "data" / "curated" / "esg_flags.json"
UNIVERSE = [ROOT / "data" / "curated" / "assets_universe.csv", ROOT / "data" / "curated" / "assets_universe_ext.csv"]

NBIM_URL = "https://data.opensanctions.org/datasets/latest/no_nbim_exclusions/entities.ftm.json"
SOURCES = {
    "1": {"name": "NBIM – Observation and exclusion of companies", "url": "https://www.nbim.no/en/responsibility/exclusion-of-companies"},
}
# The reason is shown with a numbered citation ("... [1]"): it never names the source itself.
SOURCE_TERMS = re.compile(r"norw|nbim|norges|gpfg|pension fund|sovereign wealth|opensanctions|10-k|form 10|annual report", re.I)
CRITERION_FIXES = [(r"\binways\b", "in ways"), (r"\s*\bGPFG\s+", " ")]  # typo in the source; the fund's own name

SUFFIXES = r"\b(the|inc|incorporated|corp|corporation|co|company|ltd|limited|plc|sa|ag|nv|se|asa|group|holdings?|cl [ab]|class [ab]|shares)\b"


def norm(name: str) -> str:
    """'Boeing Co/The' -> 'boeing'; 'Exxon Mobil Corp' -> 'exxon mobil'."""
    s = str(name).lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(SUFFIXES, " ", s)
    return re.sub(r"\s+", " ", s).strip()


# ---------- Raw files ----------

def fetch(url: str, dest: Path) -> bool:
    """Download `url` to `dest`. False when it fails or answers with a web page instead of data."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "datathon-esg-flags/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
    except OSError as e:
        print(f"  download failed: {url} ({e})")
        return False
    if body.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
        print(f"  {url} answered with a web page, not the data file (log-in, form or allowlist).")
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    return True


def instructions() -> str:
    return "\n".join(["", "The NBIM file is missing. Download it by hand, then run this script again:",
                      f"  {NBIM_URL}", f"  save as {NBIM_RAW.relative_to(ROOT).as_posix()}",
                      "Non-commercial use only."])


# ---------- Source → rows {source, name, reason, since, detail} ----------

def nbim_rows(path: Path) -> tuple[list[dict], str]:
    entities, sanctions, seen = {}, [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        if e.get("schema") == "Sanction":
            sanctions.append(e)
        elif e.get("schema") in ("Company", "Organization", "LegalEntity"):
            entities[e["id"]] = e
        if e.get("last_seen"):
            seen.append(e["last_seen"][:10])
    rows = []
    for s in sanctions:
        p = s.get("properties", {})
        ent = entities.get((p.get("entity") or [None])[0])
        if not ent:
            continue
        status = (p.get("description") or ["Exclusion"])[0].strip()
        verb = "Under observation because of" if status.lower().startswith("observ") else "Excluded because of"
        since = min(p.get("listingDate") or [""]) or None
        names = ent["properties"].get("name", [])
        for raw in p.get("reason") or []:
            for criterion in [c.strip() for c in raw.split("|") if c.strip()]:  # "A | B": one flag per criterion
                for pattern, repl in CRITERION_FIXES:
                    criterion = re.sub(pattern, repl, criterion)
                rows.append({"source": "1", "name": names[0], "names": names + ent["properties"].get("alias", []),
                             "reason": f"{verb} {criterion[0].lower()}{criterion[1:]}", "since": since,
                             "detail": f"{status}: {criterion}"})
    return rows, max(seen) if seen else ""


def manual_rows(first_key: int) -> tuple[list[dict], dict]:
    """The hand-maintained flags, each document numbered from `first_key` on. Rows already carry their ticker."""
    if not MANUAL_CSV.is_file():
        return [], {}
    rows, sources, keys = [], {}, {}
    for r in pd.read_csv(MANUAL_CSV, dtype=str, keep_default_na=False).itertuples(index=False):
        doc = (r.source_name.strip(), r.source_url.strip())
        if doc not in keys:
            keys[doc] = str(first_key + len(keys))
            sources[keys[doc]] = {"name": doc[0], "url": doc[1], "as_of": r.source_as_of.strip()}
        rows.append({"source": keys[doc], "ticker": r.ticker.strip().upper(), "name": r.ticker.strip().upper(),
                     "names": [], "reason": r.reason.strip(), "since": r.since.strip() or None,
                     "detail": "manual addition"})
    return rows, sources


# ---------- Mapping ----------

def read_map() -> dict[str, tuple[str, str]]:
    out = {}
    with MAP_CSV.open(encoding="utf-8") as f:
        for r in csv.DictReader(line for line in f if not line.startswith("#")):
            if (r.get("company_name") or "").strip() and (r.get("ticker") or "").strip():
                out[norm(r["company_name"])] = (r["ticker"].strip(), (r.get("exchange") or "").strip())
    return out


def read_universe() -> dict[str, str]:
    """ticker -> name. The curated universe wins over the extended one (same rule as app.intake.universe)."""
    out = {}
    for p in reversed(UNIVERSE):
        if p.is_file():
            out.update({t: n for t, n in pd.read_csv(p, usecols=["ticker", "name"]).itertuples(index=False)})
    return out


def build(rows: list[dict], mapping: dict, universe: dict) -> tuple[dict, list[dict]]:
    companies, unmapped = {}, []
    for r in rows:
        hit = ((r["ticker"], "") if r.get("ticker")
               else next((mapping[norm(n)] for n in r["names"] if norm(n) in mapping), None))
        if hit is None or hit[0] not in universe:
            unmapped.append({"source": r["source"], "company_name": r["name"], "detail": r["detail"],
                             "why": "no mapping" if hit is None else f"{hit[0]} is not in the project universe"})
            continue
        flag = {"reason": r["reason"], "source": r["source"], **({"since": r["since"]} if r["since"] else {})}
        entry = companies.setdefault(hit[0], {"name": universe[hit[0]], "flags": []})
        if flag not in entry["flags"]:
            entry["flags"].append(flag)
    for entry in companies.values():
        entry["flags"].sort(key=lambda f: (f["source"], f.get("since") or "", f["reason"]))
    return dict(sorted(companies.items())), unmapped


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--refresh", action="store_true", help="download the NBIM file again")
    args = ap.parse_args()

    if args.refresh or not NBIM_RAW.is_file():
        print(f"Downloading {NBIM_URL}")
        fetch(NBIM_URL, NBIM_RAW)
    if not NBIM_RAW.is_file():
        sys.exit(instructions())
    rows, as_of = nbim_rows(NBIM_RAW)
    sources = {"1": {**SOURCES["1"], "as_of": as_of}}
    extra, extra_sources = manual_rows(first_key=2)
    rows += extra
    sources.update(extra_sources)
    bad = sorted({r["reason"] for r in rows if SOURCE_TERMS.search(r["reason"])
                  or not r["reason"].startswith(("Excluded because of ", "Under observation because of "))})
    if bad:
        sys.exit(f"A reason breaks the wording rule (names its source, or no \"Excluded/Under observation because of\"): {bad}")

    companies, unmapped = build(rows, read_map(), read_universe())
    OUT.write_text(json.dumps({"generated_at": date.today().isoformat(), "sources": sources, "companies": companies},
                              indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    pd.DataFrame(unmapped, columns=["source", "company_name", "detail", "why"]) \
        .sort_values(["source", "company_name"]).to_csv(UNMAPPED_CSV, index=False)

    print(f"\nWrote {OUT.relative_to(ROOT).as_posix()}: {len(companies)} flagged companies")
    for key in sources:
        n = sum(any(f["source"] == key for f in c["flags"]) for c in companies.values())
        u = sum(r["source"] == key for r in unmapped)
        print(f"  [{key}] {sources[key]['name']} (as of {sources[key]['as_of']}): {n} flagged tickers, {u} unmapped names")
    print(f"Wrote {UNMAPPED_CSV.relative_to(ROOT).as_posix()}: {len(unmapped)} names to resolve by hand")


if __name__ == "__main__":
    main()
