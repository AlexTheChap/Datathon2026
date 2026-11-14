"""Tests de base : python -m pytest tests/unit  (depuis la racine du dépôt)"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend/app/tools"))
sys.path.insert(0, str(ROOT / "backend/app/agents/governance"))
from risk_tools import RiskTools          # noqa: E402
from governance_check import check        # noqa: E402

rt = RiskTools(ROOT / "data/curated")
PERSONAS = {p["id"]: p for p in json.loads((ROOT / "backend/app/core/personas.json").read_text(encoding="utf-8"))["personas"]}


def test_evidence_format():
    ev = rt.metrics(["SPY"])
    for k in ["evidence_id", "tool", "inputs", "output", "say", "currency", "as_of", "method", "assumptions", "data_sources"]:
        assert k in ev
    assert ev["currency"] == "CAD"


def test_as_of_cuts_future():
    ev = rt.metrics(["NVDA"], as_of="2022-06-30")
    assert ev["period"].endswith("2022-06-30")


def test_unknown_ticker_raises():
    with pytest.raises(KeyError):
        rt.metrics(["PASDANSLUNIVERS"])


def test_zst_less_risky_than_tlt():
    m = rt.metrics(["ZST.TO", "TLT"])["output"]
    assert m["ZST.TO"]["ann_vol"] < m["TLT"]["ann_vol"]
    assert m["ZST.TO"]["max_drawdown"] > m["TLT"]["max_drawdown"]


def test_stress_deterministic():
    w = {"XIU.TO": .5, "XBB.TO": .5}
    assert rt.stress(w)["evidence_id"] == rt.stress(w)["evidence_id"]
    assert rt.stress(w)["output"] == rt.stress(w)["output"]


def test_max_position_monotone_in_limit():
    base = PERSONAS["robert"]["demande_demo"]["portefeuille_actuel_fictif"]
    tight = rt.max_position(base, "NVDA", 0.10)["output"]["max_weight"]
    loose = rt.max_position(base, "NVDA", 0.20)["output"]["max_weight"]
    assert tight <= loose


def test_robert_nvda_blocked():
    base = PERSONAS["robert"]["demande_demo"]["portefeuille_actuel_fictif"]
    w = {k: v * 0.8 for k, v in base.items()}
    w["NVDA"] = 0.20
    v = check(w, PERSONAS["robert"], ROOT / "backend/app/core/rules.yaml", rt, ROOT / "data")
    assert v["verdict"] == "BLOCK"


def test_exclusions_never_pass_without_data(tmp_path):
    v = check({"ESGV": .6, "XBB.TO": .4}, PERSONAS["mateo"], ROOT / "backend/app/core/rules.yaml", rt, tmp_path)
    excl = [c for c in v["checks"] if c["id"].startswith("EXCLUSION_")]
    assert excl and all(c["status"] == "NON_VERIFIE" for c in excl)


def _controversy_dir(tmp_path, rows):
    (tmp_path / "curated").mkdir()
    cols = "ticker,date_publication,categorie,description_fr,citation_en,gravite,statut,devise_montant,montant,source_doc,section,source_url,reviseur,date_revision"
    lines = [cols] + [f"{t},2026-01-29,securite_produit,desc,quote,{g},{s},,,10-K 2025,contingencies,u,{'x' if s == 'verifie' else ''}," for t, g, s in rows]
    (tmp_path / "curated/controversies.csv").write_text("\n".join(lines), encoding="utf-8")
    return tmp_path


def _contro(v):
    return next(c for c in v["checks"] if c["id"] == "EXCLUSION_CONTROVERSY")


def test_controversy_verified_major_blocks(tmp_path):
    d = _controversy_dir(tmp_path, [("TSLA", "majeure", "verifie")])
    v = check({"TSLA": .1, "XBB.TO": .9}, PERSONAS["mateo"], ROOT / "backend/app/core/rules.yaml", rt, d)
    assert _contro(v)["status"] == "BLOCK" and _contro(v)["value"] == ["TSLA"]


def test_controversy_unreviewed_rows_never_block_nor_pass(tmp_path):
    d = _controversy_dir(tmp_path, [("TSLA", "majeure", "a_verifier")])
    v = check({"TSLA": .1, "XBB.TO": .9}, PERSONAS["mateo"], ROOT / "backend/app/core/rules.yaml", rt, d)
    assert _contro(v)["status"] == "NON_VERIFIE"


def test_controversy_below_threshold_passes(tmp_path):
    d = _controversy_dir(tmp_path, [("TSLA", "moderee", "verifie")])
    v = check({"TSLA": .1, "XBB.TO": .9}, PERSONAS["mateo"], ROOT / "backend/app/core/rules.yaml", rt, d)
    assert _contro(v)["status"] == "PASS"


needs_holdings = pytest.mark.skipif(rt.holdings is None, reason="etf_holdings.csv absent (scripts/etf_holdings.py)")


@needs_holdings
def test_lookthrough_adds_direct_and_etf_exposure():
    lt = rt.lookthrough({"SPY": .5, "NVDA": .5})
    nv = lt.loc["NVDA"]
    assert nv.direct == pytest.approx(.5)
    assert 0 < nv.via_etf < .1 and nv.weight == pytest.approx(nv.direct + nv.via_etf)
    assert "SPY" not in lt.index                       # le FNB d'actions disparaît au profit de ses titres
    assert "XBB.TO" in rt.lookthrough({"SPY": .5, "XBB.TO": .5}).index   # obligations : non décomposées


@needs_holdings
def test_robert_current_flags_hidden_rbc_concentration():
    v = check(PERSONAS["robert"]["demande_demo"]["portefeuille_actuel_fictif"], PERSONAS["robert"],
              ROOT / "backend/app/core/rules.yaml", rt, ROOT / "data")
    pos = next(c for c in v["checks"] if c["id"] == "POSITION_UNIQUE")
    assert pos["status"] == "WARN" and "RY.TO" in pos["detail"]   # 10 % direct + RY dans XIU


@needs_holdings
def test_controversy_indirect_via_etf_warns(tmp_path):
    d = _controversy_dir(tmp_path, [("ENB.TO", "majeure", "verifie")])
    v = check({"XIU.TO": .5, "XBB.TO": .5}, PERSONAS["mateo"], ROOT / "backend/app/core/rules.yaml", rt, d)
    c = _contro(v)
    assert c["status"] == "WARN" and "ENB.TO" in c["detail"] and c["value"] == []


def _esg_dir(tmp_path, rows):
    (tmp_path / "curated").mkdir(exist_ok=True)
    lines = ["ticker,activite,part_revenus,annee_fiscale,source_doc,section_ou_page,source_url,date_publication,reviseur"]
    lines += [f"{t},combustibles_fossiles,{p},2025,10-K,seg,u,2026-02-18,{rev}" for t, p, rev in rows]
    (tmp_path / "curated/esg_screen.csv").write_text("\n".join(lines), encoding="utf-8")
    return tmp_path


FOSSIL = {**PERSONAS["mateo"], "exclusions": [{"type": "esg_activity", "value": "combustibles_fossiles"}]}


def _esg(v):
    return next(c for c in v["checks"] if c["id"] == "EXCLUSION_ESG_COMBUSTIBLES_FOSSILES")


def test_esg_direct_fossil_blocks(tmp_path):
    d = _esg_dir(tmp_path, [("XOM", "0.95", "x"), ("JNJ", "0", "x")])
    v = check({"XOM": .05, "JNJ": .05, "XBB.TO": .9}, FOSSIL, ROOT / "backend/app/core/rules.yaml", rt, d)
    assert _esg(v)["status"] == "BLOCK" and _esg(v)["value"] == ["XOM"]


def test_esg_unreviewed_or_missing_is_non_verifie(tmp_path):
    d = _esg_dir(tmp_path, [("XOM", "95%", ""), ("JNJ", "0", "x")])       # XOM non relu
    v = check({"XOM": .05, "JNJ": .05, "XBB.TO": .9}, FOSSIL, ROOT / "backend/app/core/rules.yaml", rt, d)
    assert _esg(v)["status"] == "NON_VERIFIE" and "XOM" in _esg(v)["detail"]


def test_esg_below_threshold_passes(tmp_path):
    d = _esg_dir(tmp_path, [("JNJ", "0", "x")])
    v = check({"JNJ": .1, "XBB.TO": .9}, FOSSIL, ROOT / "backend/app/core/rules.yaml", rt, d)
    assert _esg(v)["status"] == "PASS"


@needs_holdings
def test_esg_indirect_via_etf_warns(tmp_path):
    d = _esg_dir(tmp_path, [("ENB.TO", "90", "x"), ("CNQ.TO", "100", "x")])
    v = check({"XIU.TO": .5, "XBB.TO": .5}, FOSSIL, ROOT / "backend/app/core/rules.yaml", rt, d)
    assert _esg(v)["status"] == "WARN" and "ENB.TO" in _esg(v)["detail"]


sys.path.insert(0, str(ROOT / "scripts"))


def test_esg_screen_compares_share_classes_by_ticker():
    import pandas as pd
    from esg_index_screen import compare
    parent = pd.DataFrame({"ticker": ["GOOGL", "XOM", "FOXA"], "name": ["Alphabet A", "Exxon", "Fox A"],
                           "cusip": ["02079K305", "30231G102", "35137L105"], "weight": [.05, .01, .001],
                           "holdings_date": "2026-11-12"})
    esg = pd.DataFrame({"ticker": ["GOOG", "", ""], "name": ["Alphabet C", "Fox B", "Other"],
                        "cusip": ["02079K107", "35137L204", "999999999"], "weight": [.06, .001, .01],
                        "holdings_date": "2026-08-31"})
    r = compare(parent, esg, "SPY", "ESGV").set_index("ticker")
    assert r.loc["GOOGL", "in_esg_fund"]                     # classe C dans ESGV, classe A dans SPY
    assert r.loc["FOXA", "in_esg_fund"]                      # même émetteur (CUSIP-6), autre catégorie
    assert r.loc["XOM", "statut"] == "absent_indice_esg"
    assert "ne respecte" not in r.loc["XOM", "formulation"] and "Exclue de l'indice" in r.loc["XOM", "formulation"]


def test_nbim_name_normalisation():
    from nbim_exclusions import norm
    assert norm("Toronto-Dominion Bank/The") == norm("Toronto-Dominion Bank")
    assert norm("Canadian Natural Resources Ltd") == norm("Canadian Natural Resources Limited")


def _esg_index(v):
    return next((c for c in v["checks"] if c["id"] == "PREFERENCE_ESG_INDICE"), None)


needs_screen = pytest.mark.skipif(not (ROOT / "data/curated/esg_index_screen.csv").exists(),
                                  reason="esg_index_screen.csv absent (scripts/esg_index_screen.py)")


@needs_screen
def test_esg_preference_warns_on_index_excluded_stock():
    v = check({"XOM": .05, "AAPL": .05, "XBB.TO": .9}, PERSONAS["mateo"], ROOT / "backend/app/core/rules.yaml", rt, ROOT / "data")
    c = _esg_index(v)
    assert c["status"] == "WARN" and c["value"] == ["XOM"]
    assert "Exclue de l'indice" in c["detail"] and "ne respecte" not in c["detail"]


@needs_screen
def test_esg_preference_passes_when_all_in_index():
    v = check({"AAPL": .05, "MSFT": .05, "XBB.TO": .9}, PERSONAS["mateo"], ROOT / "backend/app/core/rules.yaml", rt, ROOT / "data")
    assert _esg_index(v)["status"] == "PASS"


def test_esg_preference_only_for_profiles_that_state_it():
    v = check({"XOM": .05, "XBB.TO": .95}, PERSONAS["robert"], ROOT / "backend/app/core/rules.yaml", rt, ROOT / "data")
    assert _esg_index(v) is None                              # Robert n'a pas de préférence ESG
