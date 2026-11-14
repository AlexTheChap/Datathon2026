"""
Construit le jeu de données préparé (« curated ») à partir du jeu de départ AlphaForge.

Usage :
    python scripts/build_curated.py --starter data/raw --out data/curated

Sorties (CSV, dans --out) :
    calendar.csv              calendrier commun + indicateurs de jours fériés par marché
    fx_usdcad_filled.csv      USD/CAD reporté les jours sans taux (fériés canadiens)
    prices_cad.csv            adj_close converti en CAD, format large (une colonne par ticker)
    prices_local.csv          adj_close en devise de cotation, format large
    filled_flags.csv          True quand le prix a été reporté (pas de cotation ce jour-là)
    returns_daily_cad.csv     rendements simples quotidiens en CAD (rendement total)
    returns_daily_local.csv   rendements simples quotidiens en devise de cotation
    macro_pit.csv             macro au format long + colonne available_from (point-in-time)
    macro_daily_asof.csv      macro large, chaque valeur placée à sa date de disponibilité
    asset_risk_metrics.csv    indicateurs de risque par actif (période complète et 2022)
    correlation_full.csv      corrélations des rendements quotidiens CAD, période complète
    correlation_2022.csv      corrélations 2022 seulement
    rate_sensitivity.csv      sensibilité empirique aux taux vs duration officielle
    quality_report.json       contrôles de qualité + décisions de traitement

Toutes les décisions de traitement sont décrites dans docs/README_extended.md.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

TRADING_DAYS = 252

# Délais de publication HYPOTHÉTIQUES (à remplacer par les vraies dates : ALFRED / StatCan).
# Utilisés pour éviter le biais d'anticipation dans les simulations.
RELEASE_LAG_DAYS = {
    "CPIAUCSL": 15,                 # IPC américain : publié vers la mi-mois suivant
    "UNRATE": 7,                    # chômage américain : 1er vendredi du mois suivant
    "STATIC_TOTALCPICHANGE": 21,    # IPC canadien : ~3e semaine du mois suivant
}
DAILY_SERIES_LAG_BDAYS = 1          # séries quotidiennes : utilisables le jour ouvrable suivant


def load_starter(starter: Path):
    px = pd.read_csv(starter / "prices_daily.csv", parse_dates=["date"])
    fx = pd.read_csv(starter / "fx_usdcad_daily.csv", parse_dates=["date"])
    macro = pd.read_csv(starter / "macro_rates.csv", parse_dates=["date"])
    uni = pd.read_csv(starter / "assets_universe.csv")
    actions = pd.read_csv(starter / "corporate_actions.csv", parse_dates=["ex_date"])
    return px, fx, macro, uni, actions


def build_calendar(px: pd.DataFrame, uni: pd.DataFrame) -> pd.DataFrame:
    """Calendrier = jours où au moins une bourse (TSX ou US) a coté au moins un titre.
    Les cryptos (7 j/7) sont échantillonnées sur ce calendrier : le rendement de fin de
    semaine est inclus dans celui du lundi."""
    market = uni.set_index("ticker")["market"]
    px = px.assign(market=px["ticker"].map(market))
    eq = px[~px["market"].str.contains("Crypto", na=False)]
    tsx_days = set(eq.loc[eq["market"] == "TSX", "date"])
    us_days = set(eq.loc[eq["market"] != "TSX", "date"])
    days = sorted(tsx_days | us_days)
    cal = pd.DataFrame({"date": days})
    cal["tsx_open"] = cal["date"].isin(tsx_days)
    cal["us_open"] = cal["date"].isin(us_days)
    return cal


def build_prices(px, fx, uni, cal):
    dates = pd.DatetimeIndex(cal["date"])
    local = px.pivot(index="date", columns="ticker", values="adj_close")
    raw_on_cal = local.reindex(dates)
    filled_flags = raw_on_cal.isna()
    # Report du dernier prix connu (jour férié propre à un marché). Pas de remplissage
    # avant la première cotation.
    local_f = raw_on_cal.ffill()
    filled_flags = filled_flags & local_f.notna()

    fx_s = fx.set_index("date")["usdcad"].reindex(dates)
    fx_filled_flag = fx_s.isna()
    fx_s = fx_s.ffill().bfill()
    fx_out = pd.DataFrame({"date": dates, "usdcad": fx_s.values,
                           "filled": fx_filled_flag.values})

    ccy = uni.set_index("ticker")["currency"]
    usd_cols = [c for c in local_f.columns if ccy.get(c) == "USD"]
    cad = local_f.copy()
    cad[usd_cols] = cad[usd_cols].mul(fx_s, axis=0)
    return local_f, cad, filled_flags, fx_out


def build_macro_pit(macro: pd.DataFrame) -> pd.DataFrame:
    m = macro.copy()
    avail, method = [], []
    for _, r in m.iterrows():
        sid = r["series_id"]
        if sid in RELEASE_LAG_DAYS:
            # date de référence = 1er du mois -> fin du mois + délai
            month_end = r["date"] + pd.offsets.MonthEnd(0)
            avail.append(month_end + pd.Timedelta(days=RELEASE_LAG_DAYS[sid]))
            method.append(f"hypothese_delai_{RELEASE_LAG_DAYS[sid]}j_apres_fin_de_mois")
        else:
            avail.append(r["date"] + pd.offsets.BDay(DAILY_SERIES_LAG_BDAYS))
            method.append(f"jour_ouvrable_suivant_(+{DAILY_SERIES_LAG_BDAYS})")
    m["available_from"] = avail
    m["available_from_method"] = method
    m["note"] = np.where(m["source"].str.contains("FRED"),
                         "Valeur FRED révisée (pas la première publication)", "")
    return m


def macro_daily_asof(m: pd.DataFrame, cal: pd.DataFrame) -> pd.DataFrame:
    """Pour chaque date du calendrier : dernière valeur DISPONIBLE à cette date."""
    dates = pd.DatetimeIndex(cal["date"])
    out = pd.DataFrame(index=dates)
    for sid, g in m.groupby("series_id"):
        s = g.sort_values("available_from").drop_duplicates("available_from", keep="last")
        s = s.set_index("available_from")["value"]
        out[sid] = s.reindex(s.index.union(dates)).ffill().reindex(dates)
    out.index.name = "date"
    return out


# ---------- indicateurs de risque ----------

def _drawdown(r: pd.Series):
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
        mdd, pk, tr, rc = _drawdown(r)
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


def rate_sensitivity(rets_local: pd.DataFrame, macro_wide: pd.DataFrame,
                     durations: pd.DataFrame | None) -> pd.DataFrame:
    """Régression des rendements (devise locale) sur la variation du taux 10 ans du
    même pays. -beta*100 ≈ duration empirique (rendement pour +1 point de taux)."""
    pairs = {"XBB.TO": "BD.CDN.10YR.DQ.YLD", "ZST.TO": "BD.CDN.2YR.DQ.YLD",
             "AGG": "DGS10", "TLT": "DGS10"}
    rows = []
    for t, sid in pairs.items():
        if t not in rets_local or sid not in macro_wide:
            continue
        dy = macro_wide[sid].diff() / 100.0          # points de % -> fraction
        j = pd.concat([rets_local[t], dy], axis=1).dropna()
        j = j[j.iloc[:, 1] != 0]
        x, y = j.iloc[:, 1], j.iloc[:, 0]
        beta = np.cov(y, x)[0, 1] / np.var(x, ddof=1)
        r2 = np.corrcoef(y, x)[0, 1] ** 2
        off = None
        if durations is not None and t in set(durations["ticker"]):
            off = float(durations.set_index("ticker").loc[t, "duration_years"])
        rows.append({"ticker": t, "yield_series": sid, "n_obs": len(j),
                     "empirical_duration": -beta, "r2": r2,
                     "official_duration": off,
                     "note": "Duration officielle actuelle (2026-11) vs sensibilité moyenne 2021-2026 : "
                             "écart attendu (duration variable dans le temps, courbe non parallèle)."})
    return pd.DataFrame(rows)


def quality_report(px, cal, filled, fx_out, macro_pit, cnq_check) -> dict:
    dup = int(px.duplicated(["date", "ticker"]).sum())
    zero_vol = px[(px["volume"] == 0) & (~px["ticker"].str.contains("-USD"))][["date", "ticker"]]
    return {
        "generated_from": "data/raw (2021-11-12 → 2026-11-12)",
        "calendar_days": int(len(cal)),
        "duplicates_date_ticker": dup,
        "zero_volume_rows_non_crypto": zero_vol.astype(str).to_dict("records"),
        "filled_price_days_by_ticker": {k: int(v) for k, v in filled.sum().items()},
        "fx_filled_days": int(fx_out["filled"].sum()),
        "macro_series": sorted(macro_pit["series_id"].unique().tolist()),
        "decisions": {
            "calendar": "Union des jours de cotation TSX et US (jours de semaine). Crypto échantillonnée "
                        "sur ce calendrier : le rendement de fin de semaine est inclus le lundi.",
            "price_fill": "Dernier prix connu reporté quand un marché est fermé (férié propre à un pays). "
                          "Rendement = 0 ce jour-là. Indicateur dans filled_flags.csv.",
            "fx": "USD/CAD Banque du Canada, reporté les jours fériés canadiens.",
            "returns": "Rendements simples sur adj_close (rendement total, dividendes réinvestis), en CAD.",
            "cnq_2022_08_22": cnq_check,
            "macro_pit": "available_from = date de disponibilité estimée. Mensuel : délai hypothétique "
                         f"{RELEASE_LAG_DAYS}. Quotidien : jour ouvrable suivant. À remplacer par les "
                         "vraies dates de publication (ALFRED, calendrier StatCan).",
            "survivorship": "Univers choisi en 2026 : les rendements historiques sont biaisés à la hausse.",
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--starter", default="data/raw")
    ap.add_argument("--external", default="data/external")
    ap.add_argument("--out", default="data/curated")
    a = ap.parse_args()
    starter, out = Path(a.starter), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    px, fx, macro, uni, actions = load_starter(starter)
    assert not px.duplicated(["date", "ticker"]).any(), "doublons date/ticker"

    cal = build_calendar(px, uni)
    local, cad, filled, fx_out = build_prices(px, fx, uni, cal)
    r_cad = cad.pct_change(fill_method=None).iloc[1:]
    r_loc = local.pct_change(fill_method=None).iloc[1:]

    m_pit = build_macro_pit(macro)
    m_wide_obs = macro.pivot_table(index="date", columns="series_id", values="value")
    m_asof = macro_daily_asof(m_pit, cal)

    # CNQ : rendement adj_close vs close le 2022-08-22
    c = px[px.ticker == "CNQ.TO"].set_index("date")
    d0, d1 = pd.Timestamp("2022-08-19"), pd.Timestamp("2022-08-22")
    cnq = {
        "close_return": float(c.loc[d1, "close"] / c.loc[d0, "close"] - 1),
        "adj_close_return": float(c.loc[d1, "adj_close"] / c.loc[d0, "adj_close"] - 1),
        "decision": "adj_close conservé : l'écart (~2 %) est cohérent avec une distribution "
                    "spéciale traitée comme un dividende réinvesti (rendement total). Le facteur "
                    "1,021 de corporate_actions.csv n'est PAS réappliqué. À confirmer auprès des "
                    "relations investisseurs de CNQ.",
    }

    bench = {"spy": "SPY", "xiu": "XIU.TO"}
    met_full = asset_metrics(r_cad, bench, "2021-11-12_2026-11-12")
    met_2022 = asset_metrics(r_cad.loc["2022-01-01":"2022-12-31"], bench, "2022")
    metrics = pd.concat([met_full, met_2022], ignore_index=True)
    metrics.insert(2, "currency", "CAD")
    meta = uni.set_index("ticker")[["name", "asset_class", "sector", "currency"]] \
              .rename(columns={"currency": "listing_currency"})
    metrics = metrics.merge(meta, left_on="ticker", right_index=True, how="left")

    durations = None
    dp = Path(a.external) / "etf_bond_duration.csv"
    if dp.exists():
        durations = pd.read_csv(dp)
    rs = rate_sensitivity(r_loc, m_wide_obs.reindex(r_loc.index.union(m_wide_obs.index)).ffill()
                          .reindex(r_loc.index), durations)

    # ----- écriture -----
    uni.to_csv(out / "assets_universe.csv", index=False)
    cal.to_csv(out / "calendar.csv", index=False)
    fx_out.to_csv(out / "fx_usdcad_filled.csv", index=False)
    cad.round(6).to_csv(out / "prices_cad.csv", index_label="date")
    local.round(6).to_csv(out / "prices_local.csv", index_label="date")
    filled.to_csv(out / "filled_flags.csv", index_label="date")
    r_cad.round(8).to_csv(out / "returns_daily_cad.csv", index_label="date")
    r_loc.round(8).to_csv(out / "returns_daily_local.csv", index_label="date")
    m_pit.to_csv(out / "macro_pit.csv", index=False)
    m_asof.to_csv(out / "macro_daily_asof.csv", index_label="date")
    metrics.round(6).to_csv(out / "asset_risk_metrics.csv", index=False)
    r_cad.corr().round(4).to_csv(out / "correlation_full.csv", index_label="ticker")
    r_cad.loc["2022"].corr().round(4).to_csv(out / "correlation_2022.csv", index_label="ticker")
    rs.round(4).to_csv(out / "rate_sensitivity.csv", index=False)
    with open(out / "quality_report.json", "w", encoding="utf-8") as f:
        json.dump(quality_report(px, cal, filled, fx_out, m_pit, cnq), f,
                  ensure_ascii=False, indent=2, default=str)
    print(f"OK : {len(list(out.iterdir()))} fichiers écrits dans {out}")


if __name__ == "__main__":
    main()
