"""Domain analysis tools (macro, rates, ESG) over the curated CSVs — no AWS, no LLM."""
from __future__ import annotations

import pytest

from app.tools.analysis_tools import esg_screen, macro_snapshot, rate_sensitivity


def test_macro_snapshot_picks_last_row_on_or_before_as_of():
    ev = macro_snapshot("2021-11-15")
    assert ev["tool"] == "macro_snapshot" and ev["evidence_id"].startswith("ev-")
    assert ev["output"]["date"] == "2021-11-15"
    assert ev["output"]["CA 10y yield (%)"] == pytest.approx(1.67)
    assert "US CPI index" not in ev["output"]        # monthly series absent that day -> skipped


def test_macro_snapshot_as_of_cuts_future():
    with pytest.raises(ValueError):
        macro_snapshot("2021-01-01")                 # before the dataset window


def test_rate_sensitivity_bond_etf():
    ev = rate_sensitivity(["XBB.TO"])
    row = ev["output"]["XBB.TO"]
    assert row["empirical_duration"] == pytest.approx(6.5965, abs=1e-3)
    assert row["yield_series"] == "BD.CDN.10YR.DQ.YLD"


def test_rate_sensitivity_unknown_ticker_raises():
    with pytest.raises(KeyError):
        rate_sensitivity(["PASDANSLUNIVERS"])


def test_esg_screen_membership_and_controversies():
    ev = esg_screen(["XOM", "CNQ.TO"])
    out = ev["output"]
    assert out["membership"]["XOM"]["in_esg_index"] is False   # absent from the ESGV/FTSE screen
    assert any(c["ticker"] == "CNQ.TO" for c in out["controversies"])
    assert "CNQ.TO" in out["unverified_only"]                  # NBIM rows are unreviewed extracts
    assert any("SIMULATED" not in a for a in ev["assumptions"])
