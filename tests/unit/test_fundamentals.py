"""Fundamentals: SEC XBRL parser, point-in-time tool, sub-agent wiring, money figures in the verifier.
Synthetic filings: no network, no AWS, no LLM."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest
from app.agents.subagents import base
from app.agents.subagents.fundamentals import fundamentals_subagent
from app.agents.verifier import verify_answer
from app.tools import fundamentals_tools as ft
from app.tools.context import current_as_of

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("ingest_fundamentals", ROOT / "scripts" / "ingest_fundamentals.py")
ingest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ingest)


def fact(end, val, form="10-K", filed="2025-02-20", start=None, fy=2024, fp="FY", accn="0001-25-000001"):
    d = {"end": end, "val": val, "form": form, "filed": filed, "fy": fy, "fp": fp, "accn": accn}
    if start:
        d["start"] = start
    return d


# ---------- parser ----------

def test_parser_keeps_full_years_and_quarters_and_drops_year_to_date():
    facts = {"facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [
            fact("2024-12-31", 1000, start="2024-01-01"),                                        # annual
            fact("2024-09-30", 700, form="10-Q", filed="2024-10-30", start="2024-01-01", fp="Q3"),   # 9 months: dropped
            fact("2024-09-30", 260, form="10-Q", filed="2024-10-30", start="2024-07-01", fp="Q3"),   # quarter
            fact("2024-12-31", 999, form="8-K", filed="2025-01-10", start="2024-10-01"),              # not a periodic form
            fact("2018-12-31", 5, filed="2018-02-01", start="2018-01-01"),                            # before `since`
        ]}},
        "Assets": {"units": {"USD": [fact("2024-12-31", 5000), fact("2024-09-30", 4800, form="10-Q", filed="2024-10-30")]}},
        "EarningsPerShareDiluted": {"units": {"USD/shares": [fact("2024-12-31", 1.5, start="2024-01-01")]}},
    }}}
    rows = pd.DataFrame(ingest.parse_company_facts("TST", facts, since="2019-01-01"))
    rev = rows[rows.metric == "revenue"].sort_values("period_end")
    assert list(rev.period_kind) == ["quarter", "annual"] and list(rev.value) == [260, 1000]
    assert set(rows[rows.metric == "total_assets"].period_kind) == {"instant"}
    assert rows[rows.metric == "eps_diluted"].unit.iloc[0] == "USD/shares"
    assert set(rows.ticker) == {"TST"} and set(rows.columns) == set(ingest.COLUMNS)


def test_parser_prefers_the_first_tag_for_a_period_and_keeps_every_filing():
    facts = {"facts": {"us-gaap": {
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [fact("2024-12-31", 1000, start="2024-01-01")]}},
        "Revenues": {"units": {"USD": [fact("2024-12-31", 1001, start="2024-01-01"),                        # same filing
                                       fact("2024-12-31", 990, start="2024-01-01", filed="2026-02-20", fy=2025,
                                            accn="0001-26-000001")]}},                                       # comparative
    }}}
    rows = pd.DataFrame(ingest.parse_company_facts("TST", facts))
    assert sorted(rows.value) == [990, 1000]                                  # priority tag wins in the first filing
    assert sorted(rows.filed) == ["2025-02-20", "2026-02-20"]                 # the later filing is kept: point-in-time


def test_parser_without_us_gaap_returns_nothing():
    assert ingest.parse_company_facts("RY", {"facts": {"ifrs-full": {}}}) == []


# ---------- tool ----------

def rows(ticker="TST", restated=None):
    """Two fiscal years (2023, 2024), a newer quarter, and a balance sheet; FY2024 revenue optionally restated."""
    r = []

    def add(metric, kind, end, val, filed, form="10-K", start=None):
        r.append({"ticker": ticker, "metric": metric, "period_kind": kind, "fiscal_year": int(end[:4]),
                  "fiscal_period": "FY", "period_start": start, "period_end": end, "value": val, "unit": "USD",
                  "form": form, "filed": filed, "accession": f"a-{filed}", "tag": metric})

    for end, filed, scale in (("2023-12-31", "2024-02-20", 1.0), ("2024-12-31", "2025-02-20", 1.25)):
        add("revenue", "annual", end, 1000 * scale, filed)
        add("gross_profit", "annual", end, 600 * scale, filed)
        add("operating_income", "annual", end, 300 * scale, filed)
        add("net_income", "annual", end, 200 * scale, filed)
        add("operating_cash_flow", "annual", end, 260 * scale, filed)
        add("capex", "annual", end, 60 * scale, filed)
        add("equity", "instant", end, 1000, filed)
        add("total_assets", "instant", end, 3000, filed)
        add("total_liabilities", "instant", end, 2000, filed)
        add("long_term_debt", "instant", end, 500, filed)
    if restated:
        add("revenue", "annual", "2024-12-31", restated, "2026-02-20")        # restated in the next 10-K
    add("revenue", "quarter", "2025-03-31", 340, "2025-05-01", form="10-Q")
    add("revenue", "quarter", "2024-03-31", 300, "2024-05-01", form="10-Q")
    add("net_income", "quarter", "2025-03-31", 60, "2025-05-01", form="10-Q")
    add("equity", "instant", "2025-03-31", 1100, "2025-05-01", form="10-Q")
    add("total_liabilities", "instant", "2025-03-31", 2200, "2025-05-01", form="10-Q")
    df = pd.DataFrame(r)
    df["period_end"] = pd.to_datetime(df["period_end"])
    return df


@pytest.fixture
def corpus(monkeypatch):
    def use(df):
        monkeypatch.setattr(ft, "_facts", lambda: df)
    use(rows())
    token = current_as_of.set("2026-11-12")
    yield use
    current_as_of.reset(token)


def test_latest_annual_figures_ratios_and_growth_are_computed_by_the_tool(corpus):
    c = ft.fundamentals_snapshot(["TST"])["output"]["companies"]["TST"]
    assert c["fiscal_year_end"] == "2024-12-31" and c["form"] == "10-K" and c["filed"] == "2025-02-20"
    assert c["annual"]["revenue"] == 1250 and c["annual"]["free_cash_flow"] == 325 - 75
    assert c["growth_yoy"] == {"revenue_yoy": 0.25, "net_income_yoy": 0.25}
    assert c["margins"] == {"gross": 0.6, "operating": 0.3, "net": 0.2}
    bs = c["balance_sheet"]
    assert bs["period_end"] == "2025-03-31" and bs["equity"] == 1100                      # newest balance sheet
    assert bs["liabilities_to_equity"] == round(2200 / 1100, 4)
    assert bs["return_on_equity"] == round(250 / 1000, 4)                                 # FY net income / FY-end equity
    q = c["latest_quarter"]
    assert q["period_end"] == "2025-03-31" and q["revenue"] == 340 and q["revenue_yoy"] == round(340 / 300 - 1, 4)


def test_point_in_time_a_restatement_filed_after_as_of_is_invisible(corpus):
    corpus(rows(restated=1100))
    def revenue(as_of):
        return ft.fundamentals_snapshot(["TST"], as_of=as_of)["output"]["companies"]["TST"]["annual"]["revenue"]
    token = current_as_of.set("2026-11-12")
    try:
        assert revenue(None) == 1100                         # the restated figure, filed 2026-02-20
        assert revenue("2025-12-31") == 1250                 # before the restatement: the original figure
    finally:
        current_as_of.reset(token)


def test_nothing_filed_after_the_run_date_is_used(corpus):
    token = current_as_of.set("2024-06-30")
    try:
        ev = ft.fundamentals_snapshot(["TST"], as_of="2030-01-01")                  # later than the run: capped
    finally:
        current_as_of.reset(token)
    c = ev["output"]["companies"]["TST"]
    assert ev["as_of"] == "2024-06-30" and c["fiscal_year_end"] == "2023-12-31" and c["filed"] == "2024-02-20"
    assert c["latest_quarter"]["period_end"] == "2024-03-31"                       # the 2025 quarter is not known yet
    assert c["growth_yoy"]["revenue_yoy"] is None                                  # no 2022 year in the data


def test_before_the_first_filing_and_uncovered_companies_are_reported_not_filled(corpus):
    token = current_as_of.set("2020-01-01")
    try:
        early = ft.fundamentals_snapshot(["TST"])["output"]
    finally:
        current_as_of.reset(token)
    assert early["companies"] == {} and "on or before this date" in early["no_data"]["TST"]
    out = ft.fundamentals_snapshot(["TST", "RY.TO"])["output"]
    assert "TST" in out["companies"] and "not an SEC filer" in out["no_data"]["RY.TO"]


def test_a_company_with_only_quarters_is_still_described(corpus):
    df = rows("XOM")
    corpus(df[df["period_kind"] != "annual"])
    c = ft.fundamentals_snapshot(["XOM"])["output"]["companies"]["XOM"]
    assert "annual" not in c and c["latest_quarter"]["revenue"] == 340
    assert ft.fundamentals_snapshot(["XOM"])["say"].startswith("XOM: only quarterly figures")


def test_evidence_envelope_say_and_determinism(corpus):
    a, b = ft.fundamentals_snapshot(["TST"]), ft.fundamentals_snapshot(["TST"])
    assert a["evidence_id"].startswith("ev-") and a["evidence_id"] == b["evidence_id"] and a["tool"] == "company_fundamentals"
    assert "revenue $1.2B" not in a["say"] and "revenue $1,250" in a["say"] and "+25.0% y/y" in a["say"]
    assert any("analysis date" in x for x in a["assumptions"]) and any("No valuation" in x for x in a["assumptions"])
    assert ft.fundamentals_snapshot(["TST", "RY.TO"])["evidence_id"] != a["evidence_id"]


def test_input_errors_are_dicts_for_the_model(corpus):
    with pytest.raises(ValueError):
        ft.fundamentals_snapshot([])
    assert "error" in ft.company_fundamentals([])
    assert ft.company_fundamentals(["TST"])["tool"] == "company_fundamentals"


@pytest.mark.skipif(not (ROOT / "data/curated/fundamentals.csv").exists(), reason="fundamentals.csv not built")
def test_real_file_is_consistent_and_point_in_time():
    token = current_as_of.set("2024-06-01")
    try:
        ev = ft.fundamentals_snapshot(["NVDA", "XOM", "RY.TO", "ATD.TO", "CNR.TO"])["output"]
    finally:
        current_as_of.reset(token)
    n = ev["companies"]["NVDA"]
    assert n["fiscal_year_end"] <= "2024-06-01" and n["filed"] <= "2024-06-01"
    assert n["annual"]["revenue"] > 0 and 0 < n["margins"]["net"] < 1
    assert set(ev["no_data"]) == {"ATD.TO"}                    # XOM has history since its filings are read under Exxon Mobil Corp
    assert ev["companies"]["XOM"]["fiscal_year_end"] <= "2024-06-01"
    cn = ev["companies"]["CNR.TO"]                            # Canadian National (CNI), not Core Natural Resources (CNR)
    assert cn["currency"] == "CAD" and 5e9 < cn["annual"]["revenue"] < 4e10
    ry = ev["companies"]["RY.TO"]
    assert ry["currency"] == "CAD" and "note" in ry and ry["balance_sheet"]["total_assets"] > 0


# ---------- Canadian filers: IFRS taxonomy, 40-F / 6-K forms, CAD ----------

def test_parser_reads_ifrs_full_and_the_canadian_forms():
    facts = {"facts": {"ifrs-full": {
        "Revenue": {"units": {"CAD": [
            fact("2025-10-31", 66600, form="40-F", filed="2025-12-03", start="2024-11-01", fy=2025),        # annual 40-F
            fact("2026-01-31", 17960, form="6-K", filed="2026-02-26", start="2025-11-01", fp="Q1"),        # interim 6-K
            fact("2026-04-30", 35000, form="6-K", filed="2026-05-28", start="2025-11-01", fp="Q2"),        # 6 months: dropped
        ]}},
        "ProfitLossAttributableToOwnersOfParent": {"units": {"CAD": [fact("2025-10-31", 20400, form="40-F",
                                                                          filed="2025-12-03", start="2024-11-01")]}},
        "ProfitLoss": {"units": {"CAD": [fact("2025-10-31", 20500, form="40-F", filed="2025-12-03", start="2024-11-01")]}},
        "DilutedEarningsLossPerShare": {"units": {"CAD/shares": [fact("2026-01-31", 4.03, form="6-K", filed="2026-02-26",
                                                                      start="2025-11-01", fp="Q1")]}},
        "Equity": {"units": {"CAD": [fact("2026-01-31", 139858, form="6-K", filed="2026-02-26", fp="Q1")]}},
    }}}
    r = pd.DataFrame(ingest.parse_company_facts("RY.TO", facts))
    rev = r[r.metric == "revenue"].sort_values("period_end")
    assert list(rev.period_kind) == ["annual", "quarter"] and set(rev.unit) == {"CAD"}
    assert list(rev.form) == ["40-F", "6-K"] and rev.tag.iloc[0] == "ifrs-full:Revenue"
    ni = r[r.metric == "net_income"]
    assert list(ni.value) == [20400] and ni.tag.iloc[0] == "ifrs-full:ProfitLossAttributableToOwnersOfParent"
    assert r[r.metric == "eps_diluted"].unit.iloc[0] == "CAD/shares"
    assert r[r.metric == "equity"].period_kind.iloc[0] == "instant"


def test_us_gaap_wins_over_ifrs_when_a_company_reports_both():
    facts = {"facts": {
        "us-gaap": {"Revenues": {"units": {"USD": [fact("2024-12-31", 1000, start="2024-01-01")]}}},
        "ifrs-full": {"Revenue": {"units": {"CAD": [fact("2024-12-31", 1370, start="2024-01-01")]}}}}}
    r = pd.DataFrame(ingest.parse_company_facts("X", facts))
    assert list(r.value) == [1000] and r.unit.iloc[0] == "USD"


def cad_rows(ticker="RY.TO"):
    df = rows(ticker)
    df["unit"] = "CAD"
    stray = df.iloc[[0]].copy()                                  # a stray USD-denominated line in the same company
    stray["unit"], stray["value"], stray["filed"] = "USD", 1.0, "2025-03-01"
    return pd.concat([df, stray], ignore_index=True)


def test_amounts_keep_the_reporting_currency_and_are_never_converted(corpus, monkeypatch):
    corpus(cad_rows())
    monkeypatch.setattr(ft, "_sector", lambda t: None)
    ev = ft.fundamentals_snapshot(["RY.TO"])
    c = ev["output"]["companies"]["RY.TO"]
    assert c["currency"] == "CAD" and c["annual"]["revenue"] == 1250        # the stray USD line is ignored
    assert "revenue C$1,250" in ev["say"] and "$1,250" in ev["say"]
    assert any("reporting currency" in a for a in ev["assumptions"])


def test_banks_and_insurers_are_marked_not_comparable_and_get_no_margin_in_the_summary(corpus, monkeypatch):
    corpus(cad_rows())
    monkeypatch.setattr(ft, "_sector", lambda t: "Financials")
    ev = ft.fundamentals_snapshot(["RY.TO"])
    c = ev["output"]["companies"]["RY.TO"]
    assert "not comparable with industrial" in c["note"]
    assert "margin" not in ev["say"] and "net income" in ev["say"]
    monkeypatch.setattr(ft, "_sector", lambda t: "Energy")
    assert "note" not in ft.fundamentals_snapshot(["RY.TO"])["output"]["companies"]["RY.TO"]
    assert "margin" in ft.fundamentals_snapshot(["RY.TO"])["say"]


def test_the_verifier_accepts_the_canadian_dollar_prefix():
    ev = {"ev-aaaaaaaaaa": {"evidence_id": "ev-aaaaaaaaaa", "say": "", "output": {"revenue": 66600000000.0}}}
    assert verify_answer("Revenue was C$66.6B [ev-aaaaaaaaaa].", ev).unmatched_numbers == []
    assert verify_answer("Revenue was C$16.6B [ev-aaaaaaaaaa].", ev).unmatched_numbers


# ---------- sub-agent and verifier ----------

def test_the_sub_agent_has_the_financial_tools_first(monkeypatch):
    seen = {}

    class FakeAgent:
        def __init__(self, **kw):
            seen.update(kw)

        def __call__(self, message):
            return '{"headline": "h", "details": [], "severity": "info"}'

    monkeypatch.setattr(base, "Agent", FakeAgent)
    monkeypatch.setattr(base, "BedrockModel", lambda **kw: None)
    out = fundamentals_subagent._tool_func("profitability of NVDA")
    assert [t.tool_name for t in seen["tools"]] == ["company_fundamentals", "news_search", "esg_screen"]
    assert "never compute" in seen["system_prompt"].lower() and "valuation" in seen["system_prompt"].lower()
    assert "error" in out                              # it answered without calling a tool: the contract refuses it


def test_money_in_billions_is_checked_against_the_evidence():
    ev = {"ev-aaaaaaaaaa": {"evidence_id": "ev-aaaaaaaaaa", "say": "",
                            "output": {"revenue": 416161000000.0, "eps_diluted": 7.46, "net_income": 112010000000.0}}}
    ok = verify_answer("Revenue was $416.2B [ev-aaaaaaaaaa], net income $112.0 billion [ev-aaaaaaaaaa], EPS $7.46 "
                       "[ev-aaaaaaaaaa].", ev)
    assert ok.unmatched_numbers == [] and ok.uncited_numbers == []
    bad = verify_answer("Revenue was $380B [ev-aaaaaaaaaa].", ev)
    assert bad.unmatched_numbers and bad.unmatched_numbers[0]["number"] == "$380B"
    assert verify_answer("Revenue was $416 million [ev-aaaaaaaaaa].", ev).unmatched_numbers      # a thousand times off


def test_json_roundtrip_of_the_envelope(corpus):
    ev = ft.fundamentals_snapshot(["TST"])
    assert json.loads(json.dumps(ev, default=str))["output"]["companies"]["TST"]["fiscal_year_end"] == "2024-12-31"
