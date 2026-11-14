"""Registre de preuves + crochet AfterToolCallEvent — sans AWS, sans LLM."""
from __future__ import annotations

from pathlib import Path

from app.agents.hooks import EvidenceHook
from app.store.ledger import JsonlLedger


class _ToolUse:
    def __init__(self, name: str, tool_input: dict | None = None):
        self.name = name
        self.input = tool_input or {}


class _FakeAfterToolCallEvent:
    """Doublure de l'événement Strands (tool_use / result / exception suffisent)."""

    def __init__(self, tool_name: str, result, exception: Exception | None = None):
        self.tool_use = _ToolUse(tool_name)
        self.result = result
        self.exception = exception


def test_ledger_seq_et_ids_uniques(tmp_path: Path):
    led = JsonlLedger(tmp_path)
    led.append("run-1", {"tool": "metrics", "evidence_id": "ev-aaa"})
    led.append("run-1", {"tool": "stress", "evidence_id": "ev-bbb"})
    led.append("run-1", {"tool": "metrics", "evidence_id": "ev-aaa"})  # rappel identique
    recs = led.records("run-1")
    assert [r["seq"] for r in recs] == ["E-001", "E-002", "E-003"]  # chaque appel = 1 preuve
    assert led.evidence_ids("run-1") == ["ev-aaa", "ev-bbb"]        # citations dédupliquées
    assert led.records("run-inconnu") == []


def test_hook_enregistre_une_preuve_risk_tools(tmp_path: Path):
    led = JsonlLedger(tmp_path)
    hook = EvidenceHook(led, "run-2")

    preuve = {"evidence_id": "ev-xyz", "tool": "stress", "say": "En 2022…",
              "output": {"portfolio": {"max_drawdown": -0.21}}}
    hook._on_tool_call(_FakeAfterToolCallEvent("stress", preuve))

    (r,) = led.records("run-2")
    assert r["seq"] == "E-001" and r["evidence_id"] == "ev-xyz"
    assert r["tool"] == "stress" and r["payload"]["output"]["portfolio"]["max_drawdown"] == -0.21


def test_hook_resultat_non_dict(tmp_path: Path):
    class _Bloc:  # bloc ToolResult sans dict direct
        json = {"evidence_id": "ev-json"}

    led = JsonlLedger(tmp_path)
    EvidenceHook(led, "run-3")._on_tool_call(_FakeAfterToolCallEvent("metrics", _Bloc()))
    (r,) = led.records("run-3")
    assert r["evidence_id"] == "ev-json"


def test_hook_forme_reelle_strands(tmp_path: Path):
    """Forme observée en production (Strands ≥ 1.10) : ToolResult matérialisé en
    dict {"content": [{"text": "<evidence JSON>"}], "status": …, "toolUseId": …}."""
    import json as _json

    class _SelectedTool:
        tool_name = "governance_check"

    class _Event:
        tool_use = None                       # absent dans ce flux
        selected_tool = _SelectedTool()
        exception = None
        result = {"content": [{"text": _json.dumps(
            {"evidence_id": "ev-1a", "tool": "governance_check", "verdict": "BLOCK"})}],
            "status": "success", "toolUseId": "tu-9"}

    led = JsonlLedger(tmp_path)
    EvidenceHook(led, "run-5")._on_tool_call(_Event())
    (r,) = led.records("run-5")
    assert r["tool"] == "governance_check"
    assert r["evidence_id"] == "ev-1a"
    assert r["payload"][0]["verdict"] == "BLOCK"
    assert led.evidence_ids("run-5") == ["ev-1a"]


def test_hook_outil_en_echec_aussi_preuve(tmp_path: Path):
    led = JsonlLedger(tmp_path)
    hook = EvidenceHook(led, "run-4")
    hook._on_tool_call(_FakeAfterToolCallEvent("metrics", None, exception=KeyError("AAA")))
    (r,) = led.records("run-4")
    assert r["erreur"].startswith("KeyError") and r["payload"] == "None"
