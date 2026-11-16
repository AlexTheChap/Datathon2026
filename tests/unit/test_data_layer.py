"""Box ⑥ data layer without AWS: curated Parquet == CSV, macro/FX point-in-time, DynamoDB ledger, dispatcher."""
import threading
from types import SimpleNamespace

import pandas as pd
import pytest

from app.api.dispatch import InlineDispatcher, orchestrator_input
from app.store.ledger import DynamoDBLedger, JsonlLedger, make_ledger
from app.tools import curated_store as cs
from app.tools.macro_tools import fx_usdcad, macro_snapshot
from app.tools.risk_tools import RiskTools

CURATED = cs.Path(__file__).resolve().parents[2] / "data" / "curated"
W = {"XIU.TO": 0.24, "XBB.TO": 0.24, "ZST.TO": 0.08, "RY.TO": 0.08, "FTS.TO": 0.08, "JNJ": 0.08, "NVDA": 0.2}


@pytest.fixture(scope="module")
def parquet_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("parquet")
    rows = cs.csv_to_parquet(CURATED, d)
    assert set(rows) == set(cs.TABLES)
    return d


@pytest.mark.parametrize("name", list(cs.TABLES))
def test_parquet_reads_back_identical_to_csv(parquet_dir, name):
    a, b = cs.read(parquet_dir, name), cs.read(CURATED, name)
    assert a.shape == b.shape and list(a.columns) == list(b.columns)
    pd.testing.assert_frame_equal(a, b, check_dtype=False)


def test_risk_tools_same_evidence_from_parquet(parquet_dir):
    p, c = RiskTools(parquet_dir), RiskTools(CURATED)
    for f in (lambda rt: rt.concentration(W),
              lambda rt: rt.metrics(["NVDA", "XBB.TO"], as_of="2022-06-30"),
              lambda rt: rt.correlation(["NVDA", "XBB.TO"], as_of="2022-06-30")):
        x, y = f(p), f(c)
        assert x["evidence_id"] == y["evidence_id"] and x["output"] == y["output"]


def test_macro_snapshot_is_point_in_time(parquet_dir):
    ev = macro_snapshot(parquet_dir, "2022-06-30")
    assert ev["output"]["date"] <= "2022-06-30" and ev["as_of"] == ev["output"]["date"]
    assert ev["output"]["BoC policy rate (%)"] == pytest.approx(1.5)  # 50 bp hike of 2022-06-01
    with pytest.raises(ValueError):
        macro_snapshot(parquet_dir, "2020-01-01")


def test_fx_usdcad_point_in_time_and_change(parquet_dir):
    ev = fx_usdcad(parquet_dir, "2022-12-30", start="2022-01-03")
    o = ev["output"]
    assert o["date"] == "2022-12-30" and o["start_date"] == "2022-01-03"
    assert o["change"] == pytest.approx(o["usdcad"] / o["start_usdcad"] - 1, abs=1e-6)
    assert ev == fx_usdcad(CURATED, "2022-12-30", start="2022-01-03")  # Parquet == CSV


class FakeTable:
    """Just enough of a boto3 DynamoDB Table for the ledger (thread-safe like the real service)."""

    def __init__(self):
        self.items, self.lock = {}, threading.Lock()

    def update_item(self, Key, **kw):
        with self.lock:
            k = (Key["PK"], Key["SK"])
            item = self.items.setdefault(k, {**Key, "n": 0})
            item["n"] += 1
            return {"Attributes": {"n": item["n"]}}

    def put_item(self, Item):
        with self.lock:
            self.items[(Item["PK"], Item["SK"])] = dict(Item)

    def query(self, KeyConditionExpression, ExpressionAttributeValues, **kw):
        pk, sk = ExpressionAttributeValues[":pk"], ExpressionAttributeValues.get(":sk", "")
        return {"Items": [i for (p, s), i in self.items.items() if p == pk and s.startswith(sk)]}


def test_dynamodb_ledger_keys_order_and_ids():
    t = FakeTable()
    led = DynamoDBLedger(t)
    led.append("r1", {"tool": "metrics___metrics", "evidence_id": "ev-a", "agent": "risk_subagent", "say": "x"})
    led.append("r1", {"tool": "search_news", "evidence_id": "ev-b"})
    led.append("r1", {"tool": "metrics___metrics", "evidence_id": "ev-a"})
    led.append("r2", {"tool": "other"})
    assert ("RUN#r1", "EVID#E-001#ev-a") in t.items and t.items[("RUN#r1", "EVID#E-001#ev-a")]["agent"] == "risk_subagent"
    assert [r["seq"] for r in led.records("r1")] == ["E-001", "E-002", "E-003"]
    assert led.evidence_ids("r1") == ["ev-a", "ev-b"]
    assert len(led.records("r2")) == 1


def test_dynamodb_ledger_parallel_sub_agents_get_unique_seq():
    t, led = FakeTable(), None
    led = DynamoDBLedger(t)
    threads = [threading.Thread(target=led.append, args=("r", {"tool": f"t{i}", "evidence_id": f"ev-{i}"}))
               for i in range(20)]
    [th.start() for th in threads]
    [th.join() for th in threads]
    assert sorted(r["seq"] for r in led.records("r")) == [f"E-{i:03d}" for i in range(1, 21)]


def test_make_ledger_follows_settings(tmp_path):
    local = SimpleNamespace(store="memory", runs_dir=tmp_path, abs_path=lambda p: p)
    assert isinstance(make_ledger(local), JsonlLedger)


def test_inline_dispatcher_writes_running_then_done(make_profile, make_request):
    from app.context import resolve_plan
    from app.context.prompt_loader import resolve_explainer_prompt
    from app.schemas.models import OrchestratorOutput
    from app.store.runs import InMemoryRunStore, RunRecord

    p = make_profile("robert")
    r = make_request(p.actor_id, "risk_review", positions=[{"ticker": "NVDA", "weight": 0.2}])
    run = RunRecord(run_id="run-x", actor_id=p.actor_id, profile=p, request=r,
                    execution_plan=resolve_plan(p, r), prompt_ref=resolve_explainer_prompt(p))
    store, statuses = InMemoryRunStore(), []
    orig_put = store.put
    store.put = lambda rr: (statuses.append(rr.status), orig_put(rr))

    def fake_orchestrator(inp):
        assert inp.portfolio_weights == {"NVDA": 0.2} and inp.as_of == "2026-11-12"
        return OrchestratorOutput(run_id=inp.run_id, summary="ok", evidence_ids=["ev-1"])

    InlineDispatcher(store, run_fn=fake_orchestrator).dispatch(run)
    saved = store.get("run-x")
    assert statuses == ["RUNNING", "DRAFTED", "DONE"] and saved.result["evidence_ids"] == ["ev-1"]
                                                                             # DRAFTED: draft live while the explainer runs
    assert orchestrator_input(saved).execution_plan == run.execution_plan


def test_inline_dispatcher_failure_is_recorded(make_profile, make_request):
    from app.context import resolve_plan
    from app.context.prompt_loader import resolve_explainer_prompt
    from app.store.runs import InMemoryRunStore, RunRecord

    p = make_profile("robert")
    r = make_request(p.actor_id, "risk_review")
    run = RunRecord(run_id="run-y", actor_id=p.actor_id, profile=p, request=r,
                    execution_plan=resolve_plan(p, r), prompt_ref=resolve_explainer_prompt(p))
    store = InMemoryRunStore()

    def boom(inp):
        raise RuntimeError("bedrock down")

    InlineDispatcher(store, run_fn=boom).dispatch(run)
    assert store.get("run-y").status == "FAILED" and "bedrock down" in store.get("run-y").error
