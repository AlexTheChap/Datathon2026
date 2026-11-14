# AlphaForge AI starter dataset


Optional starting data for the PolyFinances AlphaForge AI datathon. You can use it as is, add to it, or ignore it. Everything here is simulation material. Nothing in it is investment advice.

Window: **2021-11-12 to 2026-11-12** (5 years, retrieved 2026-11-13). Five years instead of three so the 2022 drawdown, when stocks and bonds fell together, is in the sample.

## A starting point, not the answer

This pack exists so you don't lose your first hours hunting for prices. It is not the dataset the jury expects you to use, and it doesn't cover everything a good project needs. Swap assets, pick a different period, or bring in news, company filings, ESG information, ETF holdings or any other public data that fits the problem you choose. The personas and example tickers in the brief are reference points, not a checklist.

The best projects usually start from a question, work out what data it needs, and then go get that data. Whatever you add, document its source, date and processing, as the brief asks.

## Files

| File | Rows | What it holds |
| --- | --- | --- |
| `assets_universe.csv` | 31 | `asset_id, ticker, name, asset_class, market, currency, sector` |
| `prices_daily.csv` | ~40,000 | `date, ticker, open, high, low, close, adj_close, volume, currency` |
| `corporate_actions.csv` | ~660 | Dividends and splits by ex-date |
| `fx_usdcad_daily.csv` | ~1,250 | `date, usdcad, cadusd` from the Bank of Canada |
| `macro_rates.csv` | ~8,300 | Long format `date, series_id, value, unit, frequency, label, source` |
| `sources_metadata.csv` | 6 | Source, date, processing, limitations and terms for each file |
| `build_dataset.py` | | Script that regenerates the five CSV data files and `data_quality_warnings.txt` |

### Universe

- **Canadian equities (10):** RY, TD, MFC, ENB, CNQ, CNR, FTS, ATD, SHOP, CSU (all `.TO`)
- **U.S. equities (10):** AAPL, MSFT, NVDA, GOOGL, AMZN, TSLA, JPM, JNJ, PG, XOM
- **ETFs (9):** XIU.TO, SPY, QQQ, ESGV (ESG-screened U.S. equity), XBB.TO, ZST.TO (ultra short-term bonds), AGG, TLT, GLD
- **Crypto (2):** BTC-USD, ETH-USD

For the personas in the brief: energy names (ENB, CNQ, XOM) are included so Mateo's screening has real candidates to examine, and ESGV lets teams compare a screened fund with SPY. The data does not say whether any company is involved in significant environmental or social controversies. Teams need outside evidence for that. TSLA and NVDA are high-profile, volatile names for Robert's test. Amira gets crypto plus non-tech growth.

### Macro series (`macro_rates.csv`)

| series_id | Description | Source |
| --- | --- | --- |
| `V39079` | Bank of Canada target overnight rate | Bank of Canada |
| `BD.CDN.2YR.DQ.YLD`, `BD.CDN.10YR.DQ.YLD` | Government of Canada 2y and 10y yields | Bank of Canada |
| `STATIC_TOTALCPICHANGE` | Canada CPI, % y/y | Bank of Canada |
| `DFF` | U.S. effective federal funds rate | FRED |
| `DGS2`, `DGS10` | U.S. Treasury 2y and 10y yields | FRED |
| `CPIAUCSL` | U.S. CPI index | FRED |
| `UNRATE` | U.S. unemployment rate | FRED |

## Things to know before using the data

- **Prices are in each asset's listing currency.** `.TO` tickers are CAD; everything else is USD. Use `fx_usdcad_daily.csv` to convert. The FX file has no rows on Canadian holidays, so carry the last rate forward if you need one.
- **No forward-fill, no calendar alignment.** A row exists only on days Yahoo reports a price for the asset (ZST.TO has one zero-volume row, on 2022-07-06). TSX and U.S. holidays differ, and crypto trades 7 days a week (1,827 rows versus 1,255 for equities). Align calendars yourself, and say how you did it.
- **`adj_close` is adjusted for splits and dividends** (Yahoo's method, back-adjusted from the latest date). Use it for returns. `close` is adjusted for splits only. If you re-download later, `adj_close` values for older dates will change.
- **All prices are already split-adjusted.** The split rows in `corporate_actions.csv` are for reference. Don't apply them to `close` again.
- **Crypto rows are UTC calendar days.** The row dated D covers 00:00 to 24:00 UTC on D. There is no market close.
- **CNQ.TO shows a 1.021 "split" on 2022-08-22**, the same day as a 0.75 CAD dividend. CNQ has not done a 1.021 share split, so this factor likely reflects a special distribution. Check CNQ's investor relations page before relying on it.
- **Monthly macro data is dated to the reference month, not the release date.** U.S. and Canadian CPI for a given month come out weeks later. Do not use a value in a backtest before it was published. FRED values are the latest revised figures, not as first published.
- **Survivorship bias.** The universe was picked in 2026 from names that are large today. A backtest on it will look better than a strategy picked in 2021 would have.
- **No ESG scores, news or fundamentals are included.** We didn't want to ship ESG ratings we couldn't source and license properly. Teams who need them can use company sustainability reports, ETF issuers' ESG methodology pages, SEDAR+ and SEC EDGAR filings, and must document what they used.
- `data_quality_warnings.txt` lists gaps and suspicious one-day moves found at build time. The current build found none.

## Rebuild

```bash
python -m venv .venv
.venv/bin/pip install pandas yfinance
.venv/bin/python build_dataset.py
```

Edit `UNIVERSE`, `START` and `END` at the top of the script to change the asset list or window. The script fails on duplicate rows, missing tickers or bad prices. This README and `sources_metadata.csv` are written by hand and are not regenerated.

## Sources and terms

- **Yahoo Finance** (prices, corporate actions) via the open-source `yfinance` library. Terms: https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html
- **Bank of Canada** Valet API (FX, rates, Canadian CPI). Terms: https://www.bankofcanada.ca/terms/
- **FRED**, Federal Reserve Bank of St. Louis (U.S. rates, CPI, unemployment). The underlying series come from U.S. government agencies. Terms: https://fred.stlouisfed.org/legal/

