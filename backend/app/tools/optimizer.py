"""
AlphaForge constrained portfolio optimizer: deterministic, no LLM, no return forecast.

`optimize(...)` returns the SMALLEST CHANGE to a target portfolio `w0` (the user's request or
current holdings) that satisfies every rule of the profile. It never maximizes expected returns.

    minimize   ||w - w0||^2 + risk_aversion * w' Sigma w
    subject to sum(w) = 1, w >= 0 and the rules read from rules.yaml and the profile:
               single position, sector, crypto, asset-class exclusions, short-horizon rules and a
               linear proxy of the maximum loss in the stress window.

Every threshold comes from the profile and rules.yaml (never hard-coded here). The solution is then
VERIFIED with the exact tools (`RiskTools.stress` and `governance_check.check`). If the real stress
drawdown breaks the limit, a linear cut built from the worst peak-to-trough dates is added and the
problem is solved again (at most `max_attempts` solves). If no verified solution exists the result is
`status = "infeasible"`, with the conflicting constraints in plain words and NO weights.

The result is an evidence dict in the same format as `risk_tools.py`:
    from risk_tools import RiskTools
    from optimizer import optimize
    rt = RiskTools("data/curated")
    ev = optimize(rt, w0, profile, as_of="2026-11-12")

Used by the Allocation sub-agent. The LLM never computes: it cites this evidence.
"""
from __future__ import annotations

import copy
import sys
from collections.abc import Callable
from pathlib import Path

import cvxpy as cp
import numpy as np
import pandas as pd
import yaml

try:
    from risk_tools import (
        BASE_ASSUMPTIONS,
        SOURCES,
        STRESS_WINDOWS,
        TRADING_DAYS,
        RiskTools,
        _eid,
    )
except ModuleNotFoundError:  # imported as a package (app.tools.optimizer)
    from app.tools.risk_tools import (  # type: ignore[no-redef]
        BASE_ASSUMPTIONS,
        SOURCES,
        STRESS_WINDOWS,
        TRADING_DAYS,
        RiskTools,
        _eid,
    )

DEFAULT_RULES = Path(__file__).resolve().parents[1] / "core" / "rules.yaml"
GOVERNANCE_DIR = Path(__file__).resolve().parents[1] / "agents" / "governance"

EPS = 2e-4            # numerical margin inside every cap (weights are rounded to 4 decimals afterwards)
MIN_WEIGHT = 1e-4     # weights below this are dropped
BINDING_TOL = 1e-6    # a constraint is "active" when its slack is below this
STRESS_STEP = 0.02    # extra tightening of the stress limit on each retry (2 points)
SOLVER = cp.CLARABEL  # interior point, deterministic

STATUS_OK = "ok"
STATUS_INFEASIBLE = "infeasible"


# ---------------------------------------------------------------- helpers

def _load_check() -> Callable:
    """governance_check lives in agents/governance (no package yet): import it through sys.path."""
    try:
        from governance_check import check
    except ModuleNotFoundError:
        if str(GOVERNANCE_DIR) not in sys.path:
            sys.path.append(str(GOVERNANCE_DIR))
        from governance_check import check
    return check


def _pit_view(rt: RiskTools, as_of: str | None) -> RiskTools:
    """Point-in-time view of the risk tools: no return or price after `as_of`.
    `rt.stress` and `governance_check.check` take no `as_of`, so they are given this view."""
    if not as_of:
        return rt
    cut = pd.Timestamp(as_of)
    view = copy.copy(rt)
    view.rets = rt.rets.loc[:cut]
    view.prices = rt.prices.loc[:cut]
    if view.rets.empty:
        raise ValueError(f"No data on or before as_of={as_of}.")
    return view


class _Rows:
    """Linear inequality rows `a . w <= b`, each tagged with a group and a name."""

    def __init__(self, n: int) -> None:
        self.n = n
        self.a: list[np.ndarray] = []
        self.b: list[float] = []
        self.group: list[str] = []
        self.name: list[str] = []
        self.text: dict[str, str] = {}      # group -> plain-words description

    def add(self, group: str, name: str, a: np.ndarray, b: float, text: str | None = None) -> None:
        self.a.append(np.asarray(a, dtype=float))
        self.b.append(float(b))
        self.group.append(group)
        self.name.append(name)
        if text:
            self.text.setdefault(group, text)

    def matrix(self, drop_groups: set[str] | None = None) -> tuple[np.ndarray, np.ndarray]:
        keep = [i for i, g in enumerate(self.group) if not drop_groups or g not in drop_groups]
        if not keep:
            return np.zeros((0, self.n)), np.zeros(0)
        return np.vstack([self.a[i] for i in keep]), np.array([self.b[i] for i in keep])


def _exposures(view: RiskTools, cands: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Exposure of each company to each candidate (equity ETFs decomposed, as governance does)."""
    cols = [view.lookthrough({c: 1.0})["weight"].rename(c) for c in cands]
    expo = pd.concat(cols, axis=1).fillna(0.0)
    meta = view.lookthrough({c: 1.0 / len(cands) for c in cands})[["sector", "asset_class"]]
    return expo, meta.reindex(expo.index)


def _clean(x: np.ndarray) -> np.ndarray:
    """Drop tiny weights, round to 4 decimals and put the rounding residual on the largest weight."""
    w = np.where(x < MIN_WEIGHT, 0.0, x)
    w = np.round(w, 4)
    w[int(np.argmax(w))] += round(1.0 - float(w.sum()), 4)
    return np.round(w, 4)


def _solve(n: int, w0: np.ndarray, cov: np.ndarray, risk_aversion: float,
           a: np.ndarray, b: np.ndarray, feasibility_only: bool = False) -> np.ndarray | None:
    w = cp.Variable(n)
    constraints = [cp.sum(w) == 1, w >= 0]
    if len(b):
        constraints.append(a @ w <= b)
    if feasibility_only:
        objective = cp.Minimize(0)
    else:
        objective = cp.Minimize(cp.sum_squares(w - w0) + risk_aversion * cp.quad_form(w, cp.psd_wrap(cov)))
    problem = cp.Problem(objective, constraints)
    try:
        problem.solve(solver=SOLVER)
    except cp.error.SolverError:
        return None
    if problem.status not in ("optimal", "optimal_inaccurate") or w.value is None:
        return None
    return np.clip(np.asarray(w.value, dtype=float), 0.0, None)


def _diagnose(rows: _Rows, cov: np.ndarray, w0: np.ndarray) -> tuple[list[str], str]:
    """Which constraint groups conflict? First try dropping one group at a time, then add groups
    to the dropped set until the problem becomes feasible."""
    groups = list(dict.fromkeys(rows.group))
    n = rows.n

    def feasible(drop: set[str]) -> bool:
        a, b = rows.matrix(drop)
        return _solve(n, w0, cov, 0.0, a, b, feasibility_only=True) is not None

    single = [g for g in groups if feasible({g})]
    if single:
        conflicts = single
    else:
        conflicts, dropped = [], set()
        for g in groups:
            dropped.add(g)
            conflicts.append(g)
            if feasible(dropped):
                break
        else:
            conflicts = groups
    texts = [rows.text.get(g, g) for g in conflicts]
    if single:
        msg = ("No allocation of these candidate assets satisfies every rule. "
               "Each of these rules, taken alone, makes the problem unsolvable: " + "; ".join(texts) + ".")
    else:
        msg = ("No allocation of these candidate assets satisfies every rule. "
               "These rules conflict with each other: " + "; ".join(texts) + ".")
    return texts, msg


def _active(rows: _Rows, x: np.ndarray) -> list[str]:
    a, b = rows.matrix()
    slack = b - a @ x
    names = [rows.name[i] for i in range(len(b)) if slack[i] < BINDING_TOL]
    return list(dict.fromkeys(names))


# ---------------------------------------------------------------- tool

def optimize(rt: RiskTools, w0: dict, profile: dict, rules_path: str | Path = DEFAULT_RULES,
             candidates: list[str] | None = None, as_of: str | None = None,
             risk_aversion: float = 0.5, capital: float | None = None,
             data_dir: str | Path = "data", max_attempts: int = 3,
             check_fn: Callable | None = None) -> dict:
    """Smallest change to `w0` that satisfies the profile's rules (see the module docstring).

    rt          risk tools on data/curated
    w0          requested or current weights {ticker: weight}; normalized to sum to 1
    profile     persona dict (core/personas.json format: id, risk_band, horizon_years [min, max], exclusions)
    candidates  extra tickers allowed in the result; the tickers of `w0` are always candidates
    as_of       no return or price after this date is used (also in the verification)
    data_dir    folder containing curated/ (esg and controversy files read by governance)
    """
    if risk_aversion < 0:
        raise ValueError("risk_aversion must be >= 0.")
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1.")
    rules_path = Path(rules_path)
    rules = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
    band = rules["risk_bands"][profile["risk_band"]]
    check_fn = check_fn or _load_check()

    view = _pit_view(rt, as_of)
    w0n = view._normalize({k: float(v) for k, v in w0.items() if v})
    cands = list(dict.fromkeys([*w0n, *(candidates or [])]))
    view._check(cands)
    n, uni = len(cands), view.universe
    w0v = np.array([w0n.get(c, 0.0) for c in cands])
    cls = np.array([uni.loc[c, "asset_class"] for c in cands])

    # ---- data: covariance, stress proxy, volatility
    rets = view.rets[cands].dropna()
    if len(rets) < 30:
        raise ValueError("Not enough common history for the candidate assets.")
    cov = rets.cov().to_numpy() * TRADING_DAYS
    cov = (cov + cov.T) / 2 + 1e-8 * np.eye(n)

    window = rules["stress_window"]
    s, e, _label = STRESS_WINDOWS[window]
    win = view.rets.loc[s:e, cands].iloc[1:]
    stress_on = len(win) >= 20
    r_stress = np.nan_to_num(((1 + win).prod() - 1).to_numpy()) if stress_on else np.zeros(n)
    stress_limit = float(band["max_drawdown_stress"])

    horizon_min = (profile.get("horizon_years") or [None, None])[0]
    vol = np.array([view.metrics([c])["output"][c]["ann_vol"] for c in cands])

    # ---- rows that do not depend on the retry (everything is read from rules.yaml / profile)
    rows = _Rows(n)
    cap_single, cap_sector = float(band["max_single_position"]), float(band["max_sector"])
    if view.holdings:
        expo, meta = _exposures(view, cands)
        eq = meta["asset_class"] == "equity"
        e_eq = expo[eq]
        for cid, row in e_eq.iterrows():
            if row.max() > cap_single - EPS:          # a row that can never bind is skipped
                rows.add("single_position", f"single_position:{cid}", row.to_numpy(), cap_single - EPS,
                         f"the cap of {cap_single:.0%} on a single company (ETFs decomposed)")
        for sector, ids in e_eq.groupby(meta.loc[eq, "sector"]):
            if sector != "Unclassified":
                rows.add("sector_cap", f"sector_cap:{sector}", ids.sum().to_numpy(), cap_sector - EPS,
                         f"the cap of {cap_sector:.0%} on a single sector")
    else:
        exempt = set(rules["broad_funds_exempt_from_single_position"])
        sectors = pd.Series([uni.loc[c, "sector"] for c in cands])
        for i, c in enumerate(cands):
            if c not in exempt:
                a = np.zeros(n)
                a[i] = 1.0
                rows.add("single_position", f"single_position:{c}", a, cap_single - EPS,
                         f"the cap of {cap_single:.0%} on a single position")
        for sector, idx in sectors.groupby(sectors):
            a = np.array([1.0 if (j in idx.index and cands[j] not in exempt) else 0.0 for j in range(n)])
            if a.any():
                rows.add("sector_cap", f"sector_cap:{sector}", a, cap_sector - EPS,
                         f"the cap of {cap_sector:.0%} on a single sector")

    cap_crypto = float(band["max_crypto"])
    is_crypto = (cls == "crypto").astype(float)
    if is_crypto.any():
        rows.add("crypto_cap", "crypto_cap", is_crypto, max(cap_crypto - EPS, 0.0),
                 f"the crypto cap of {cap_crypto:.0%}")

    for ex in profile.get("exclusions", []):
        if ex.get("type") == "asset_class":
            mask = (cls == ex["value"]).astype(float)
            if mask.any():
                rows.add("exclusion", f"exclusion:{ex['value']}", mask, 0.0,
                         f"the exclusion of {ex['value']} assets")

    for hr in rules.get("horizon_rules", []):
        if horizon_min is not None and horizon_min < hr["when"]["horizon_years_max_lt"]:
            if "max_weight_high_vol_assets" in hr:
                cap = float(hr["max_weight_high_vol_assets"])
                rows.add("short_horizon_volatility", f"{hr['id']}",
                         (vol > hr["high_vol_threshold"]).astype(float), cap - EPS,
                         f"the short-horizon limit of {cap:.0%} on assets with volatility above "
                         f"{hr['high_vol_threshold']:.0%}")
            if "min_defensive_weight" in hr:
                floor = float(hr["min_defensive_weight"])
                rows.add("short_horizon_defensive", f"{hr['id']}",
                         -(cls == "etf_bond").astype(float), -(floor + EPS),
                         f"the short-horizon minimum of {floor:.0%} in bond funds")
    n_base = len(rows.b)

    def add_stress(tighten: float) -> None:
        if stress_on:
            rows.add("stress_loss", "stress_loss", -r_stress, stress_limit - tighten - EPS,
                     f"the maximum loss of {stress_limit:.0%} in the {window} stress window")

    # ---- solve / verify loop
    cuts: list[tuple[np.ndarray, float]] = []
    weights: dict | None = None
    last_x: np.ndarray | None = None
    st_ev = gov = None
    message, conflicts, attempts, failure = "", [], 0, None
    for attempt in range(1, max_attempts + 1):
        attempts = attempt
        del rows.a[n_base:], rows.b[n_base:], rows.group[n_base:], rows.name[n_base:]
        tighten = STRESS_STEP * (attempt - 1)
        add_stress(tighten)
        for ratio, lim in cuts:
            rows.add("stress_loss", "stress_loss", -ratio, -(1 - lim) - tighten - EPS)
        a, b = rows.matrix()
        x = _solve(n, w0v, cov, risk_aversion, a, b)
        if x is None:
            conflicts, message = _diagnose(rows, cov, w0v)
            failure = "no_solution"
            break
        wv = _clean(x)
        cand_w = {c: float(v) for c, v in zip(cands, wv) if v > 0}
        st_ev = view.stress(cand_w, window, capital) if stress_on else None
        gov = check_fn(cand_w, profile, rules_path, view, data_dir)
        mdd = st_ev["output"]["portfolio"]["max_drawdown"] if st_ev else None
        stress_ok = mdd is None or mdd >= -stress_limit
        blockers = [c["id"] for c in gov["checks"] if c["status"] == "BLOCK" and c["id"] != "MAX_STRESS_LOSS"]
        if stress_ok and gov["verdict"] != "BLOCK":
            weights, last_x, failure = cand_w, x, None
            break
        if blockers:
            failure = "governance_block"
            message = ("A solution was found but the governance check still BLOCKs: "
                       + ", ".join(blockers) + ". These rules are not modelled by the optimizer.")
            break
        failure = "stress_not_met"
        message = (f"After {attempt} attempt(s) the real maximum loss in the {window} window "
                   f"({mdd:+.1%}) still exceeds the profile limit (-{stress_limit:.0%}).")
        pf = st_ev["output"]["portfolio"]
        path = (1 + win).cumprod()
        ratio = (path.loc[pd.Timestamp(pf["dd_trough"])] / path.loc[pd.Timestamp(pf["dd_peak"])]).to_numpy()
        cuts.append((ratio, stress_limit))

    # ---- evidence
    inputs = {"w0": {k: round(v, 6) for k, v in w0n.items()}, "candidates": cands,
              "profile": {k: profile.get(k) for k in ("id", "risk_band", "horizon_years", "exclusions")},
              "risk_aversion": risk_aversion, "as_of": as_of, "capital": capital,
              "max_attempts": max_attempts, "rules_version": rules.get("version"), "window": window}
    ok = weights is not None
    verification = {"stress_max_drawdown": (st_ev["output"]["portfolio"]["max_drawdown"] if st_ev else None),
                    "governance_verdict": gov["verdict"] if gov else None, "attempts": attempts}
    if ok:
        ordered = dict(sorted(weights.items(), key=lambda kv: -kv[1]))
        changes = {c: round(ordered.get(c, 0.0) - w0n.get(c, 0.0), 4) for c in cands}
        changes = {c: d for c, d in changes.items() if abs(d) >= MIN_WEIGHT}
        biggest = sorted(changes, key=lambda c: -abs(changes[c]))[:3]
        moves = ", ".join(f"{c} {w0n.get(c, 0.0):.1%} -> {ordered.get(c, 0.0):.1%}" for c in biggest)
        say = ("Smallest adjustment found that satisfies the profile's rules: "
               + (moves if moves else "no change needed") + ".")
        if st_ev:
            say += (f" Simulated worst loss in the {window} window: "
                    f"{verification['stress_max_drawdown']:+.1%} (limit -{stress_limit:.0%}).")
        output = {"status": STATUS_OK, "weights": ordered, "requested_weights": w0n, "changes": changes,
                  "active_constraints": _active(rows, last_x), "verification": verification}
    else:
        say = "No allocation of the candidate assets satisfies every rule of the profile. " + message
        output = {"status": STATUS_INFEASIBLE, "weights": {}, "requested_weights": w0n, "changes": {},
                  "active_constraints": [], "verification": verification,
                  "reason": failure, "explanation": message, "conflicting_constraints": conflicts}
    idx = view.rets.index
    sources = SOURCES + ["assets_universe.csv"] + (["etf_holdings.csv"] if view.holdings else [])
    return {
        "evidence_id": _eid("optimize", inputs), "tool": "optimize", "inputs": inputs, "output": output,
        "say": say, "currency": "CAD",
        "as_of": as_of or str(idx.max().date()), "period": f"{idx.min().date()}/{idx.max().date()}",
        "method": ("Quadratic program (cvxpy, Clarabel): minimize the squared change from the requested "
                   "weights plus a variance penalty, under linear constraints read from rules.yaml and the "
                   "profile. The solution is verified with the stress and governance tools; a linear cut "
                   "from the worst peak-to-trough dates is added if the real loss exceeds the limit."),
        "assumptions": BASE_ASSUMPTIONS + [
            "No return forecast: the objective is the smallest change plus a variance penalty.",
            (f"The {window} total return of each asset is a linear proxy of the stress loss; the real loss is "
             "verified with the stress tool."),
            "Historical covariance of daily CAD returns (x252) up to as_of.",
            "ETF holdings are the CURRENT composition applied to all dates.",
            "Thresholds come from rules.yaml and are team assumptions, not regulatory standards.",
            "Controversy and ESG-activity exclusions are not modelled: the governance verdict reports them.",
            f"Numerical margin of {EPS} inside every cap; weights rounded to 4 decimals.",
            "Past performance does not predict future results.",
        ],
        "data_sources": sources,
    }
