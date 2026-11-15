"""Suitability report (report/suitability.py): a PDF laid out from the fit score, read back with pypdf (no AWS)."""
import io
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pypdf import PdfReader

from app.agents.governance.fit_score import fit_score
from app.report.suitability import _t, chart_data, report_id, suitability_pdf
from app.tools.risk_tools import RiskTools

ROOT = Path(__file__).resolve().parents[2]
RULES = ROOT / "backend/app/core/rules.yaml"
PROFILE = {"actor_id": "t", "risk_band": "moderate", "horizon_years": {"min": 8, "max": 12}, "loss_capacity": 0.2,
           "exclusions": [{"type": "controversy", "min_severity": "major"}], "preferences": [], "experience": "beginner",
           "capital": 150000, "agent_toggles": {"news": False}}
WEIGHTS = {"QQQ": 0.7, "SPY": 0.115, "XBB.TO": 0.101, "XIU.TO": 0.084}


@pytest.fixture(scope="module")
def made():
    rt = RiskTools(ROOT / "data/curated").at("2026-11-12")
    fit = fit_score(WEIGHTS, PROFILE, RULES, rt, ROOT / "data")
    run = {"run_id": "run-abc", "created_at": "2026-11-15T12:00:00", "execution_plan": {"enabled_agents": [{"agent": "risk"}]},
           "prompt_ref": {"key": "explainer.beginner.md", "version": "1.5.0", "sha256": "0" * 64}}
    pdf = suitability_pdf(PROFILE, WEIGHTS, fit, rt.universe, "2026-11-12", run=run,
                          generated_at=datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc), charts=chart_data(WEIGHTS, rt, 0.40))
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
    return pdf, text, fit


def test_it_is_a_pdf_with_the_score_the_portfolio_and_the_warning(made):
    pdf, text, fit = made
    assert pdf.startswith(b"%PDF")
    assert f"{fit['score']}% fit with the investor's profile" in text
    assert "Simulation only" in text and "not investment advice" in text
    for t in WEIGHTS:
        assert t in text
    assert "$105,000" in text                                    # 70% of the simulated $150,000


def test_every_part_and_every_rule_is_in_it(made):
    _, text, fit = made
    for p in fit["parts"]:
        assert p["title"] in text
    assert "Largest sector" in text and "WARN" in text
    assert "rules.yaml version 1" in text


def test_provenance_and_a_report_id_on_every_page(made):
    pdf, text, fit = made
    rid = report_id(PROFILE, WEIGHTS, fit)
    assert "run-abc" in text and "explainer.beginner.md version 1.5.0" in text
    pages = PdfReader(io.BytesIO(pdf)).pages
    assert all(rid in p.extract_text() for p in pages)


def test_the_report_id_follows_the_content_not_the_time(made):
    _, _, fit = made
    assert report_id(PROFILE, WEIGHTS, fit) == report_id(dict(PROFILE), dict(WEIGHTS), fit)
    assert report_id(PROFILE, {**WEIGHTS, "QQQ": 0.69}, fit) != report_id(PROFILE, WEIGHTS, fit)


def test_text_outside_latin1_is_written_plainly():
    assert _t("8%–13% → ≥ “ok” − 1") == '8%-13% -> >= "ok" - 1'


def test_the_charts_are_drawn_with_their_titles_and_values(made):
    _, text, _ = made
    for title in ("How the money is split", "Each holding", "Points kept in each part",
                  "Where the ups and downs come from", "Stock sectors"):
        assert title in text
    assert "Share of the ups and downs" in text and "limit 40%" in text      # legend, and the sector limit line
    assert "Information Technology" in text


def test_chart_data_is_measured_on_the_weights():
    rt = RiskTools(ROOT / "data/curated").at("2026-11-12")
    c = chart_data(WEIGHTS, rt, 0.40)
    assert dict(c["split"]) == pytest.approx({"Stocks": 0.899, "Bonds": 0.101}, abs=1e-3)
    assert abs(sum(b for _, b in c["risk"].values()) - 1) < 0.01             # risk shares add up to 100%
    assert c["risk"]["QQQ"][1] > c["risk"]["QQQ"][0]                          # tech swings more than its weight
    assert max(c["sectors"], key=c["sectors"].get) == "Information Technology"
