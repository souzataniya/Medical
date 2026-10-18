"""Run from app/:  pip install pytest && pytest -q"""

from __future__ import annotations

import asyncio
import copy
import json
import math

import httpx
import pytest
from fastapi.testclient import TestClient

import verifier as V
from agents import ForecastingAgent, SynapseGrid, build_case
from evaluation import oracle_is_safe, run_evaluation
from gemma import GemmaClient
from grid import SCENARIO_BASELINE, SOLAR_LOSS_FRACTION
from optimizer import Priorities, solve_dispatch, unsafe_proposal


def run(coro):
    return asyncio.run(coro)


def baseline_case(tel=SCENARIO_BASELINE):
    fc = ForecastingAgent().run(tel)
    res = solve_dispatch(tel, fc.available_kw, Priorities())
    assert res.status == "OPTIMAL"
    return tel, fc, res, build_case(tel, fc, res.proposal)


def families(result):
    return {v["family"] for v in result["violations"]}


# ─────────────────────────── Verifier: valid and invalid dispatches ───────────────────────────

def test_valid_dispatch_passes_all_constraints():
    *_, case = baseline_case()
    out = V.verify(case)
    assert out["verdict"] == "APPROVED"
    assert out["violations"] == []
    assert all(c["passed"] for c in out["checks"])
    fams = {c["family"] for c in out["checks"]}
    assert {"POWER_BALANCE", "LINE_CAPACITY", "BESS_POWER", "BESS_SOC", "GEN_LIMIT", "CRITICAL_LOAD"} <= fams


def test_verifier_recomputes_flows_independently_of_planner():
    _, _, res, case = baseline_case()
    flows = V.verify(case)["computed"]["line_flow_kw"]
    for lid, claimed in res.claimed_line_flow_kw.items():
        assert abs(flows[lid] - claimed) < 1.0  # two independent computations agree
    poisoned = copy.deepcopy(case)
    poisoned["proposal"]["claimed_line_flow_kw"] = {"L15": 0.0}  # planner claims are not part of the schema …
    assert V.verify(poisoned)["verdict"] == "BLOCKED"            # … and unexpected keys are not silently trusted


def test_line_capacity_violation_rejected_with_structured_record():
    tel, fc, res, case = baseline_case()
    bad = copy.deepcopy(case)
    bad["proposal"]["generation_kw"]["DG-HARB"] += 1000.0
    bad["proposal"]["battery_kw"]["BESS-PRT"] -= 1000.0  # out of rating too, but also pushes power through L15
    out = V.verify(bad)
    assert out["verdict"] == "BLOCKED"
    line = next(v for v in out["violations"] if v["family"] == "LINE_CAPACITY")
    assert {"constraint_id", "element", "element_type", "observed", "limit", "excess", "severity", "message"} <= set(line)
    assert line["element_type"] == "edge" and line["observed"] > line["limit"]


def test_line_capacity_boundary():
    *_, case = baseline_case()
    flows = V.verify(case)["computed"]["line_flow_kw"]
    lid = max(flows, key=lambda k: abs(flows[k]))
    at = copy.deepcopy(case)
    next(ln for ln in at["network"]["lines"] if ln["id"] == lid)["cap_kw"] = abs(flows[lid])
    assert V.verify(at)["verdict"] == "APPROVED"
    over = copy.deepcopy(case)
    next(ln for ln in over["network"]["lines"] if ln["id"] == lid)["cap_kw"] = abs(flows[lid]) - 1.0
    out = V.verify(over)
    assert out["verdict"] == "BLOCKED" and families(out) == {"LINE_CAPACITY"}


def test_battery_discharge_limit_violation():
    *_, case = baseline_case()
    bad = copy.deepcopy(case)
    bad["proposal"]["battery_kw"]["BESS-CLN"] = 1100.0  # rating 1000 kW
    bad["proposal"]["generation_kw"]["PV-HILL"] -= 1100.0 - case["proposal"]["battery_kw"]["BESS-CLN"]
    out = V.verify(bad)
    assert "BESS_POWER" in families(out)


def test_battery_depletion_violation():
    *_, case = baseline_case()
    bad = copy.deepcopy(case)
    bad["state"]["soc"]["BESS-PRT"] = 0.12
    bad["proposal"]["battery_kw"]["BESS-PRT"] = 700.0
    bad["proposal"]["generation_kw"]["PV-HILL"] -= 700.0 - case["proposal"]["battery_kw"]["BESS-PRT"]
    out = V.verify(bad)
    soc = next(v for v in out["violations"] if v["family"] == "BESS_SOC")
    assert soc["element"] == "BESS-PRT" and soc["observed"] < 0.10


def test_power_balance_and_critical_load():
    *_, case = baseline_case()
    bad = copy.deepcopy(case)
    bad["proposal"]["served_kw"]["LD-CLINIC"] = 100.0
    out = V.verify(bad)
    assert {"CRITICAL_LOAD", "POWER_BALANCE"} <= families(out)
    assert all(v["severity"] == "critical" for v in out["violations"] if v["family"] in ("CRITICAL_LOAD", "POWER_BALANCE"))


@pytest.mark.parametrize("mutate", [
    lambda p: p["generation_kw"].__setitem__("DG-HARB", float("nan")),
    lambda p: p["generation_kw"].__setitem__("DG-HARB", float("inf")),
    lambda p: p["generation_kw"].__setitem__("GHOST", 1.0),
    lambda p: p["served_kw"].pop("LD-WATER"),
    lambda p: p["battery_kw"].__setitem__("BESS-CLN", "500"),
    lambda p: p["served_kw"].__setitem__("LD-HOMES", True),
    lambda p: p.pop("battery_kw"),
])
def test_malformed_planner_output_is_rejected_not_crashing(mutate):
    *_, case = baseline_case()
    bad = copy.deepcopy(case)
    mutate(bad["proposal"])
    out = V.verify(bad)
    assert out["verdict"] == "BLOCKED"
    assert "INPUT_VALID" in families(out)


@pytest.mark.parametrize("junk", [None, [], "approve", 42, {"schema": "wrong"}])
def test_garbage_case_rejected(junk):
    assert V.verify(junk)["verdict"] == "BLOCKED"


# ─────────────────────────── Optimizer and recovery ───────────────────────────

def test_optimizer_respects_hard_constraints_after_solar_loss():
    tel = SCENARIO_BASELINE.model_copy(update={"irradiance_wm2": SCENARIO_BASELINE.irradiance_wm2 * (1 - SOLAR_LOSS_FRACTION)})
    *_, case = baseline_case(tel)
    out = V.verify(case)
    assert out["verdict"] == "APPROVED"
    assert oracle_is_safe(case)


def test_solar_drop_unsafe_rejected_then_recovered():
    g = SynapseGrid()
    r = run(g.run_incident_scenario(inject_unsafe=True))
    assert r["status"] == "CERTIFIED"
    assert r["rejected"] is not None
    assert {"LINE_CAPACITY", "GEN_RAMP"} <= {v["family"] for v in r["rejected"]["violations"]}
    assert r["after"]["critical_coverage"] == 1.0
    assert r["after"]["emissions_kg_per_h"] < r["rejected"]["impact"]["emissions_kg_per_h"]
    assert g.metrics["unsafe_accepted"] == 0
    final = g.last_cycle["attempts"][-1]
    assert final["source"] == "optimizer" and final["replay"]["reproduced"]


def test_infeasible_scenario_fails_honestly():
    g = SynapseGrid()
    cyc = run(g.run_infeasible_scenario())
    assert cyc["status"] == "FAILED"
    assert cyc["critical_shortfall_kw"] > 0
    assert not cyc["committed"]
    assert all(a["verification"] is None or a["verification"]["verdict"] != "APPROVED" for a in cyc["attempts"])


def test_unsafe_proposal_is_deterministically_unsafe():
    fc = ForecastingAgent().run(SCENARIO_BASELINE)
    case = build_case(SCENARIO_BASELINE, fc, unsafe_proposal(SCENARIO_BASELINE, fc.available_kw))
    assert V.verify(case)["verdict"] == "BLOCKED"
    assert not oracle_is_safe(case)


# ─────────────────────────── Reproducibility and audit ───────────────────────────

def test_identical_inputs_give_identical_records():
    r1 = run(SynapseGrid().run_incident_scenario())
    r2 = run(SynapseGrid().run_incident_scenario())
    assert r1["after_record_hash"] == r2["after_record_hash"]
    assert r1["rejected"]["record_hash"] == r2["rejected"]["record_hash"]


def test_replay_and_tamper_detection():
    g = SynapseGrid()
    r = run(g.run_incident_scenario())
    rec = json.loads(json.dumps(g.ledger.records[r["after_record_hash"]]))
    assert V.replay(rec)["reproduced"]
    tampered = copy.deepcopy(rec)
    tampered["case"]["state"]["line_derate"]["L15"] = 0.1  # pretend the line was rated lower
    out = V.replay(tampered)
    assert not out["record_hash_valid"] and not out["reproduced"]
    assert g.ledger.verify_chain()


# ─────────────────────────── Gemma (mocked transport, never a fake success) ───────────────────────────

def gemma_with(monkeypatch, handler):
    monkeypatch.setenv("GEMMA_API_KEY", "test-key")
    monkeypatch.delenv("GEMMA_BASE_URL", raising=False)
    return GemmaClient(transport=httpx.MockTransport(handler))


def ai_studio_reply(text: str) -> httpx.Response:
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}}]})


def test_gemma_not_configured(monkeypatch):
    for k in ("GEMMA_API_KEY", "GOOGLE_API_KEY", "GEMMA_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    g = SynapseGrid(gemma=GemmaClient())
    out = run(g.interpret_note("cyclone shelter open"))
    assert out["outcome"]["status"] == "not_configured"
    assert out["applied_priorities"]["source"].startswith("default")


def test_gemma_timeout_is_unavailable(monkeypatch):
    def handler(_):
        raise httpx.ReadTimeout("slow")
    g = SynapseGrid(gemma=gemma_with(monkeypatch, handler))
    out = run(g.interpret_note("cyclone shelter open"))
    assert out["outcome"]["status"] == "unavailable" and "timeout" in out["outcome"]["errors"][0]
    assert out["applied_priorities"]["protected_loads"] == []


@pytest.mark.parametrize("text", [
    "I think you should protect the school.",                                         # no JSON
    '{"incident_summary": "x", "rationale": "y", "protected_loads": ["LD-CLINIC"]}',  # critical id
    '{"incident_summary": "x", "rationale": "y", "protected_loads": ["LD-HOSPITAL"]}',  # hallucinated id
    '{"incident_summary": "x", "rationale": "y", "load_priority": {"LD-HOMES": 9}}',   # out of range
    '{"incident_summary": "x", "rationale": "y", "override_verifier": true}',          # extra key
])
def test_gemma_malformed_output_rejected(monkeypatch, text):
    g = SynapseGrid(gemma=gemma_with(monkeypatch, lambda _: ai_studio_reply(text)))
    out = run(g.interpret_note("cyclone shelter open"))
    assert out["outcome"]["status"] == "rejected"
    assert out["applied_priorities"]["source"].startswith("default")


def test_gemma_valid_priorities_applied_but_cannot_break_safety(monkeypatch):
    reply = ('```json\n{"incident_summary": "Cyclone warning; school is the evacuation shelter.", '
             '"protected_loads": ["LD-SCHOOL", "LD-COLD"], "load_priority": {"LD-MARKET": 1}, '
             '"rationale": "Shelter occupants and food storage first."}\n```')
    g = SynapseGrid(gemma=gemma_with(monkeypatch, lambda _: ai_studio_reply(reply)))
    out = run(g.interpret_note("Cyclone warning: school gym open as shelter until 18:00."))
    assert out["outcome"]["status"] == "inference"
    assert out["applied_priorities"]["protected_loads"] == ["LD-SCHOOL", "LD-COLD"]
    cyc = run(g.run_infeasible_scenario())  # protections can't create a feasible plan from nothing
    assert cyc["status"] == "FAILED"
    r = run(g.run_incident_scenario(note=None))
    assert r["status"] == "CERTIFIED"


# ─────────────────────────── Evaluation suite ───────────────────────────

def test_randomized_evaluation_small():
    r = run_evaluation(n=40, seed=3)
    assert r["recovery"]["unsafe_accepted"] == 0
    assert r["rejection_accuracy"]["FN"] == 0
    assert r["reproducibility"]["identical_digests"] == r["reproducibility"]["rechecked"]


# ─────────────────────────── API ───────────────────────────

def test_api_health_schema_and_flow():
    from main import app
    with TestClient(app) as client:
        h = client.get("/health").json()
        assert h["status"] == "ok" and "verifier" in h
        assert "SynapseGrid" in client.get("/").text
        assert client.post("/api/inject-grid-fault", json={"fault": "meteor"}).status_code == 422
        assert client.post("/api/evaluate-load-shift", json={"telemetry": {"soc": {"BESS-CLN": 1.5}}}).status_code == 422
        assert client.post("/api/evaluate-load-shift", json={"telemetry": {"demand_kw": {"NOPE": 1}}}).status_code == 422
        assert client.post("/api/gemma/priorities", json={"note": ""}).status_code == 422
        r = client.post("/api/evaluate-load-shift", json={"inject_unsafe": True}).json()
        assert r["status"] == "CERTIFIED" and r["rejected"] and r["certificate"]["record_hash"]
        rep = client.get(f"/api/replay/{r['certificate']['record_hash']}").json()
        assert rep["reproduced"] is True
        r = client.post("/api/evaluate-load-shift", json={"proposal": {"generation_kw": "everything"}}).json()
        assert r["rejected"][0]["violations"][0]["family"] == "INPUT_VALID" and r["status"] == "CERTIFIED"
        sc = client.post("/api/scenarios/incident", json={"inject_unsafe": True}).json()
        assert sc["status"] == "CERTIFIED" and sc["rejected"]
        assert client.post("/api/scenarios/infeasible").json()["status"] == "FAILED"
        assert client.get("/api/audit/" + "0" * 64).status_code == 404
        ev = client.post("/api/evaluation", json={"n": 20, "seed": 1}).json()
        assert ev["recovery"]["unsafe_accepted"] == 0
        assert not math.isnan(ev["rejection_accuracy"]["accuracy"])


def test_gemma_thinking_parts_ignored_and_unsupported_setting_retried(monkeypatch):
    answer = ('{"incident_summary": "Shelter open", "protected_loads": ["LD-SCHOOL"], '
              '"load_priority": {"LD-MARKET": 1}, "rationale": "Protect shelter."}')
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body["generationConfig"])
        if "thinkingConfig" in body["generationConfig"] and len(calls) == 1:
            return httpx.Response(400, json={"error": {"message": "Thinking level is not supported for this model."}})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"text": "Reasoning about the shelter ...", "thought": True}, {"text": answer}]}}]})

    g = SynapseGrid(gemma=gemma_with(monkeypatch, handler))
    out = run(g.interpret_note("cyclone shelter open"))
    assert out["outcome"]["status"] == "inference"
    assert "Reasoning" not in out["outcome"]["raw_text"]
    assert "thinkingConfig" not in calls[-1] and calls[0]["responseMimeType"] == "application/json"
