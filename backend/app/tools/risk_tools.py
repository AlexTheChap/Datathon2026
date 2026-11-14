"""
Outils de risque AlphaForge : fonctions pures, déterministes, en CAD.

Chaque outil renvoie une PREUVE (dict) au format commun :
    {
      "evidence_id": "ev-…",      # hash stable des entrées
      "tool": "metrics",
      "inputs": {...},
      "output": {...},            # chiffres
      "say": "phrase courte…",    # pour l'agent (texte ou voix)
      "currency": "CAD",
      "as_of": "2022-12-30",      # aucune donnée postérieure n'est utilisée
      "period": "2022-01-03/2022-12-30",
      "method": "...", "assumptions": [...], "data_sources": [...]
    }

Le LLM ne calcule jamais : il appelle ces fonctions et cite leurs preuves.

Usage minimal :
    from risk_tools import RiskTools
    rt = RiskTools("data/curated")
    rt.metrics(["NVDA", "XBB.TO"], start="2022-01-01", end="2022-12-31")
    rt.stress({"NVDA": 0.2, "XBB.TO": 0.5, "XIU.TO": 0.3}, "2022")
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

TRADING_DAYS = 252
SOURCES = ["prices_daily.csv (Yahoo Finance via yfinance, extrait 2026-11-13)",
           "fx_usdcad_daily.csv (Banque du Canada, série FXUSDCAD)"]
BASE_ASSUMPTIONS = [
    "Rendement total (adj_close, dividendes réinvestis), converti en CAD.",
    "Calendrier : union des jours de cotation TSX et US ; prix reporté les jours fériés propres à un marché.",
    "Crypto échantillonnée sur ce calendrier (fin de semaine incluse le lundi).",
    "Rendements passés : ne prédisent pas les rendements futurs. Univers choisi en 2026 (biais du survivant).",
]
STRESS_WINDOWS = {
    "2022": ("2021-12-31", "2022-12-30", "en 2022 (hausse des taux : actions et obligations en baisse ensemble)"),
    "2022_H1": ("2021-12-31", "2022-06-30", "au premier semestre 2022"),
}


def _eid(tool: str, inputs: dict) -> str:
    h = hashlib.sha1(json.dumps([tool, inputs], sort_keys=True, default=str).encode()).hexdigest()
    return f"ev-{h[:10]}"


def _pct(x: float) -> str:
    return f"{x * 100:+.1f} %".replace(".", ",")


def _money(x: float) -> str:
    return f"{x:,.0f} $".replace(",", "\u00a0")


class RiskTools:
    def __init__(self, curated_dir: str | Path, universe_csv: str | Path | None = None):
        d = Path(curated_dir)
        self.rets = pd.read_csv(d / "returns_daily_cad.csv", index_col="date", parse_dates=True)
        self.prices = pd.read_csv(d / "prices_cad.csv", index_col="date", parse_dates=True)
        uni = Path(universe_csv) if universe_csv else d / "assets_universe.csv"
        self.universe = pd.read_csv(uni).set_index("ticker") if uni.exists() else None
        hp = d / "etf_holdings.csv"
        self.holdings = self._load_holdings(hp) if hp.exists() else None

    @staticmethod
    def _load_holdings(path: Path) -> dict[str, pd.DataFrame]:
        """etf_holdings.csv (scripts/etf_holdings.py) -> {FNB: DataFrame[ticker, name, sector, weight, holdings_date]}.
        Titres sans ticker : identifiés par leur nom."""
        h = pd.read_csv(path, dtype={"ticker": str, "cusip": str}, keep_default_na=False)
        h["ticker"] = h["ticker"].where(h["ticker"] != "", h["name"])
        return {f: g.reset_index(drop=True) for f, g in h.groupby("fund")}

    def lookthrough(self, weights: dict, holdings: dict[str, pd.DataFrame] | None = None) -> pd.DataFrame:
        """Exposition par société après décomposition des FNB d'actions.
        Colonnes : weight (total), direct, via_etf, sector, asset_class, currency. Les FNB non décomposés
        (obligations, or) restent une ligne. La part non investie en actions d'un FNB (liquidités) est ignorée."""
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
        df.loc[in_u, "sector"] = df.loc[in_u, "id"].map(u["sector"])      # secteur de l'univers prioritaire
        df.loc[in_u, "asset_class"] = df.loc[in_u, "id"].map(u["asset_class"])
        df["sector"] = df["sector"].replace("", "non classé")
        return (df.groupby("id").agg(weight=("weight", "sum"), direct=("direct", "sum"), via_etf=("via_etf", "sum"),
                                     sector=("sector", "first"), asset_class=("asset_class", "first"),
                                     currency=("currency", "first"))
                  .sort_values("weight", ascending=False))

    # ---------- utilitaires ----------
    def _window(self, start=None, end=None, as_of=None) -> pd.DataFrame:
        r = self.rets
        if as_of:
            r = r.loc[:pd.Timestamp(as_of)]
        if start:
            r = r.loc[pd.Timestamp(start):]
        if end:
            r = r.loc[:pd.Timestamp(end)]
        if r.empty:
            raise ValueError("Aucune donnée dans la fenêtre demandée.")
        return r

    def _check(self, tickers):
        unknown = [t for t in tickers if t not in self.rets.columns]
        if unknown:
            raise KeyError(f"Actifs inconnus (hors univers) : {unknown}")

    @staticmethod
    def _normalize(w: dict) -> dict:
        s = sum(w.values())
        if s <= 0:
            raise ValueError("Pondérations nulles.")
        return {k: v / s for k, v in w.items()}

    def portfolio_returns(self, weights: dict, start=None, end=None, as_of=None) -> pd.Series:
        """Portefeuille rééquilibré quotidiennement aux pondérations cibles (hypothèse)."""
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
        return {
            "evidence_id": _eid(tool, inputs), "tool": tool, "inputs": inputs, "output": output,
            "say": say, "currency": "CAD",
            "as_of": inputs.get("as_of") or str(r_index.max().date()),
            "period": f"{r_index.min().date()}/{r_index.max().date()}",
            "method": method, "assumptions": BASE_ASSUMPTIONS + list(extra_assumptions),
            "data_sources": SOURCES,
        }

    # ---------- outils ----------
    def metrics(self, tickers: list[str], start=None, end=None, as_of=None) -> dict:
        self._check(tickers)
        r = self._window(start, end, as_of)
        out = {t: self._stats(r[t].dropna()) for t in tickers}
        t0 = tickers[0]
        say = (f"{t0} : volatilité {out[t0]['ann_vol'] * 100:.0f} % par an, "
               f"pire baisse {_pct(out[t0]['max_drawdown'])} (en CAD).")
        return self._evidence("metrics", {"tickers": tickers, "start": start, "end": end, "as_of": as_of},
                              out, say, r.index,
                              "Volatilité = écart-type quotidien × √252 ; perte maximale = pire baisse sommet→creux ; "
                              "VaR/CVaR historiques 95 % quotidiennes.")

    def stress(self, weights: dict, window: str = "2022", capital: float | None = None) -> dict:
        if window not in STRESS_WINDOWS:
            raise KeyError(f"Fenêtre inconnue : {window}. Disponibles : {list(STRESS_WINDOWS)}")
        s, e, label = STRESS_WINDOWS[window]
        pr = self.portfolio_returns(weights, start=s, end=e).iloc[1:]
        st = self._stats(pr)
        per_asset = {t: round(float((1 + self.rets.loc[s:e, t].iloc[1:]).prod() - 1), 6) for t in weights}
        out = {"window": window, "label": label, "portfolio": st, "per_asset_total_return": per_asset}
        if capital:
            out["capital"] = capital
            out["value_end"] = round(capital * (1 + st["total_return"]), 2)
            out["loss_at_trough"] = round(capital * st["max_drawdown"], 2)
        say = f"{label[0].upper() + label[1:]}, ce portefeuille aurait fait {_pct(st['total_return'])} (pire moment {_pct(st['max_drawdown'])}, en CAD)."
        if capital:
            say += f" Sur {_money(capital)}, ça représente {_money(out['loss_at_trough'])} au pire moment."
        return self._evidence("stress", {"weights": weights, "window": window, "capital": capital},
                              out, say, pr.index,
                              "Rejeu historique d'une allocation fixe (rééquilibrage quotidien) sur la fenêtre.",
                              ["Scénario historique, pas une prévision."])

    def concentration(self, weights: dict, holdings: dict[str, pd.DataFrame] | None = None,
                      look_through: bool = True, top_n: int = 25) -> dict:
        """Poids par société, secteur, classe d'actifs et devise. Par défaut, décompose les FNB d'actions
        avec data/curated/etf_holdings.csv (ou `holdings` = {FNB: DataFrame[ticker, weight, sector]})."""
        if self.universe is None:
            raise RuntimeError("assets_universe.csv introuvable.")
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
        say = (f"Plus grosse exposition : {t0} à {df['weight'].iloc[0] * 100:.0f} %"
               + (f" (dont {df['via_etf'].iloc[0] * 100:.0f} % via des FNB)" if df["via_etf"].iloc[0] > 0.001 else "")
               + f" ; équivalent de {str(round(1 / hhi, 1)).replace('.', ',')} positions égales.")
        assumptions = []
        if used:
            dates = {f: str(hold[f]["holdings_date"].iloc[0]) for f in used if "holdings_date" in hold[f]}
            assumptions.append(f"FNB décomposés avec leur composition la plus récente {dates} (composition ACTUELLE, "
                               "pas historique) ; secteurs GICS actuels, « non classé » si inconnu.")
        not_split = [a for a in weights if a not in (hold or {}) and self.universe.loc[a, "asset_class"].startswith("etf")]
        if not_split:
            assumptions.append(f"Non décomposés (une seule position chacun) : {not_split}.")
        ev = self._evidence("concentration", {"weights": weights, "look_through": used},
                            out, say, self.rets.index,
                            "Exposition par société = Σ poids du FNB × poids du titre dans le FNB + détention directe ; "
                            "indice de Herfindahl (Σw²) et nombre effectif de positions (1/HHI).", assumptions)
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
        say = f"Corrélation la plus forte : {hi[0]} et {hi[1]} ({hi[2]:.2f})." if hi else ""
        return self._evidence("correlation", {"tickers": tickers, "start": start, "end": end, "as_of": as_of},
                              out, say, r.index, "Corrélation de Pearson des rendements quotidiens en CAD.",
                              ["Les corrélations augmentent souvent en période de crise : comparer avec 2022."])

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
        say = f"{top} pèse {w[top] * 100:.0f} % du portefeuille mais génère {rc[top] * 100:.0f} % de son risque."
        return self._evidence("risk_contribution", {"weights": weights, "start": start, "end": end, "as_of": as_of},
                              out, say, r.index,
                              "Contribution au risque = w_i (Σw)_i / wᵀΣw ; ratio de diversification = vol. moyenne pondérée / vol. du portefeuille.")

    def replay(self, weights: dict, capital: float, start: str, end: str) -> dict:
        """Parcours en dollars (CAD), jour par jour : pour l'animation de la machine à remonter le temps."""
        pr = self.portfolio_returns(weights, start=start, end=end).iloc[1:]
        path = (capital * (1 + pr).cumprod()).round(2)
        st = self._stats(pr)
        out = {"path": {str(k.date()): v for k, v in path.items()}, "stats": st,
               "value_start": capital, "value_end": float(path.iloc[-1]),
               "min_value": float(path.min()), "min_date": str(path.idxmin().date())}
        say = (f"De {_money(capital)} le {start}, la valeur est descendue à {_money(path.min())} "
               f"le {path.idxmin().date()}, pour finir à {_money(path.iloc[-1])}.")
        return self._evidence("replay", {"weights": weights, "capital": capital, "start": start, "end": end},
                              out, say, pr.index, "Rejeu historique d'une allocation fixe, rééquilibrée quotidiennement.",
                              ["Scénario historique, pas une prévision. Sans frais ni impôts."])

    def max_position(self, base_weights: dict, asset: str, max_drawdown_limit: float,
                     window: str = "2022", step: float = 0.005) -> dict:
        """Plus grande part de `asset` (les autres positions réduites au prorata) telle que
        la perte maximale du portefeuille sur la fenêtre reste ≥ -max_drawdown_limit."""
        self._check([asset, *base_weights])
        s, e, label = STRESS_WINDOWS[window]
        base = self._normalize({k: v for k, v in base_weights.items() if k != asset})
        best, curve = 0.0, []
        for x in np.arange(0, 1 + 1e-9, step):
            w = {k: v * (1 - x) for k, v in base.items()}
            w[asset] = w.get(asset, 0) + x
            mdd = self._stats(self.portfolio_returns(w, start=s, end=e).iloc[1:])["max_drawdown"]
            curve.append((round(float(x), 4), mdd))
            if mdd >= -max_drawdown_limit:
                best = float(x)
            else:
                break
        out = {"asset": asset, "max_weight": round(best, 4), "limit": -max_drawdown_limit,
               "window": window, "curve": curve}
        pct = str(round(best * 100, 1)).replace(".", ",")
        say = (f"Avec la limite de perte de votre profil (-{max_drawdown_limit * 100:.0f} %) {label}, "
               f"{asset} peut aller jusqu'à environ {pct} % du portefeuille.")
        return self._evidence("max_position",
                              {"base_weights": base_weights, "asset": asset,
                               "max_drawdown_limit": max_drawdown_limit, "window": window},
                              out, say, self.rets.loc[s:e].index,
                              "Recherche par pas : on augmente la part de l'actif (autres positions réduites au prorata) "
                              "jusqu'à dépasser la perte maximale tolérée dans le scénario.",
                              ["Basé sur un seul scénario historique ; un autre scénario donnerait une autre limite."])
