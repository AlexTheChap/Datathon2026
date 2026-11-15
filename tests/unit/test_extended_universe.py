"""S&P 500 extension (scripts/build_extended_universe.py): the risk tools and the intake accept the
constituents beyond the 31 curated assets, and say where those figures come from.

Run: python -m pytest tests/unit/test_extended_universe.py -q
"""
import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from app.intake import MockBackend, run_intake
from app.intake.universe import load_universe, normalize_ticker
from app.tools import curated_store as cs
from app.tools.risk_tools import RiskTools

ROOT = Path(__file__).resolve().parents[2]
CURATED = ROOT / "data" / "curated"
pytestmark = pytest.mark.skipif(not (CURATED / "assets_universe_ext.csv").exists(),
                                reason="extension not built (scripts/build_extended_universe.py)")


@pytest.fixture(scope="module")
def rt():
    return RiskTools(CURATED)


def _script():
    spec = importlib.util.spec_from_file_location("build_ext", ROOT / "scripts" / "build_extended_universe.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------- data layer ----------

def test_extension_is_appended_after_the_curated_assets_and_never_overrides_them():
    core = cs.read(CURATED, "assets_universe")
    uni = cs.read_extended(CURATED, "assets_universe")
    assert uni["ticker"].is_unique
    assert uni.head(len(core))["ticker"].tolist() == core["ticker"].tolist()
    assert uni["in_core_universe"].sum() == len(core) == 31
    assert len(uni) > 400
    assert set(uni.loc[~uni["in_core_universe"], "sector"]) <= {
        "Communication Services", "Consumer Discretionary", "Consumer Staples", "Energy", "Financials",
        "Health Care", "Industrials", "Information Technology", "Materials", "Real Estate", "Utilities"}


def test_extension_returns_share_the_curated_dates():
    core = cs.read(CURATED, "returns_daily_cad")
    rets = cs.read_extended(CURATED, "returns_daily_cad")
    assert rets.index.equals(core.index)
    pd.testing.assert_frame_equal(rets[core.columns], core)
    assert set(cs.extended_tickers(CURATED)) == set(rets.columns) - set(core.columns)


# ---------- risk tools ----------

def test_metrics_on_an_extension_ticker_cites_the_extension(rt):
    ev = rt.metrics(["LLY"], as_of="2025-12-31")
    assert 0.1 < ev["output"]["LLY"]["ann_vol"] < 1.0
    assert any("today's S&P 500" in a and "LLY" in a for a in ev["assumptions"])
    assert any("returns_daily_cad_ext" in s for s in ev["data_sources"])


def test_curated_only_evidence_is_unchanged(rt):
    ev = rt.metrics(["NVDA", "XBB.TO"], as_of="2025-12-31")
    assert not any("S&P 500 extension" in a for a in ev["assumptions"])
    assert not any("returns_daily_cad_ext" in s for s in ev["data_sources"])


def test_mixed_portfolio_risk_contribution_and_concentration(rt):
    w = {"XIU.TO": 0.4, "LLY": 0.2, "COST": 0.2, "XBB.TO": 0.2}
    rc = rt.risk_contribution(w, as_of="2025-12-31")
    assert abs(sum(rc["output"]["risk_contribution"].values()) - 1) < 1e-3      # rounded to 4 decimals
    assert any("COST, LLY" in a for a in rc["assumptions"])
    conc = rt.concentration(w)
    assert conc["output"]["by_sector"]["Health Care"] >= 0.2        # LLY's GICS sector from the extension
    assert conc["output"]["by_currency"]["USD"] >= 0.4


def test_governance_check_accepts_an_extension_ticker(rt):
    from app.agents.governance import governance_check as gov

    profile = {"id": "t", "risk_band": "moderate", "horizon_years": [10, 20], "exclusions": []}
    res = gov.check({"SPY": 0.5, "V": 0.1, "XBB.TO": 0.4}, profile, ROOT / "backend" / "app" / "core" / "rules.yaml",
                    rt.at("2025-12-31"), str(CURATED.parent))
    assert res["verdict"] in {"PASS", "WARN", "BLOCK"}


def test_projection_accepts_an_extension_ticker():
    from app.tools.projection_tool import run_projection

    ev = run_projection({"XIU.TO": 0.5, "LLY": 0.5}, 10_000, 10, "2025-12-31", n_paths=200, curated_dir=CURATED)
    assert ev["output"]


# ---------- intake ----------

def test_intake_universe_accepts_extension_tickers_and_names():
    u = load_universe()
    assert not u["NVDA"].extended and u["LLY"].extended
    assert normalize_ticker("visa", u) == "V"
    assert normalize_ticker("Eli Lilly", u) == "LLY"
    assert normalize_ticker("brk.b", u) == "BRK-B"
    assert normalize_ticker("xiu", u) == "XIU.TO"          # curated suffix lookup still first
    assert normalize_ticker("news", u) is None             # everyday word, not News Corp


def test_mock_intake_reads_extension_holdings_but_not_capitalised_words():
    res = run_intake("I'm going ALL in: 30% Costco, 30% LLY and 40% XIU. Is it too risky for my IT job?",
                     "ext-user", backend=MockBackend())
    assert {p.ticker for p in res.request.positions} == {"COST", "LLY", "XIU.TO"}


# ---------- build script (offline, synthetic bars) ----------

def test_build_converts_to_cad_carries_holidays_and_drops_recent_listings():
    mod = _script()
    cal = pd.bdate_range("2024-01-01", periods=300)
    fx = pd.Series(1.25, index=cal)
    old = pd.DataFrame({"date": cal.delete(10), "ticker": "OLD", "adj_close": np.linspace(100, 130, 299)})
    new = pd.DataFrame({"date": cal[-50:], "ticker": "NEW", "adj_close": 10.0})
    rets, dropped = mod.build(pd.concat([old, new]), cal, fx)
    assert list(rets.columns) == ["OLD"]
    assert "NEW" in dropped and "recent listing" in dropped["NEW"]
    assert rets.loc[cal[10], "OLD"] == 0.0                 # holiday: price carried forward, zero return
    cad_total = (1 + rets["OLD"]).prod() - 1
    assert abs(cad_total - 0.30) < 1e-9                    # constant FX: same return in CAD as in USD
