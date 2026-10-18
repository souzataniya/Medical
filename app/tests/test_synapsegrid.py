"""Run from /app:  pip install pytest && pytest -q"""

import asyncio

from fastapi.testclient import TestClient

import agents as A
from main import app


def run(coro):
    return asyncio.run(coro)


def test_nominal_cycle_certifies_first_try():
    net = A.SynapseGridNetwork()
    cycle = run(net.run_cycle())
    assert cycle["final"]["verdict"] == "CERTIFIED"
    assert cycle["final"]["attempts"] == 1
    assert net.ledger.verify_chain()


def test_unsafe_overload_is_blocked_then_corrected():
    net = A.SynapseGridNetwork()
    run(net.run_cycle())
    net.state.faults["unsafe_overload"] = True
    cycle = run(net.run_cycle())
    first = cycle["attempts"][0]["verification"]
    assert first["verdict"] == "BLOCKED"
    assert first["counterexample"]["violations"]
    assert cycle["final"]["verdict"] == "CERTIFIED"
    final = cycle["attempts"][-1]["verification"]["checks"]
    assert all(c["satisfied"] for c in final)


def test_line_trip_and_transformer_caught_under_surge():
    net = A.SynapseGridNetwork()
    run(net.run_cycle())
    net.state.faults["grid_surge"] = True
    cycle = run(net.run_cycle())
    violated = {v["id"] for v in cycle["attempts"][0]["verification"]["counterexample"]["violations"]}
    assert "LINE_THERMAL[L-CHG-SEL]" in violated
    assert "XFMR_HOTSPOT[LD-TKY]" in violated
    assert cycle["final"]["verdict"] == "CERTIFIED"


def test_verdict_is_deterministic():
    net = A.SynapseGridNetwork()
    tel = net.state.sample_telemetry()
    fc = A.ForecastingAgent().run(tel)
    plan = A.LoadMatchingAgent().propose("c", 1, fc, A.PlanningDirectives(), unsafe=True)
    v = A.GemmaSafetyVerifier(net.gemma)
    a = v.compile_invariants(plan, tel, fc, None)
    b = v.compile_invariants(plan, tel, fc, None)
    assert [c.model_dump() for c in a] == [c.model_dump() for c in b]


def test_hotspot_model_matches_rated_point():
    assert abs(A.hotspot_c(1.0, 30.0) - 110.0) < 1e-9
    assert A.hotspot_c(A.max_loading_k(36.0), 36.0) <= A.HOTSPOT_LIMIT_C + 1e-6


def test_http_endpoints():
    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"
        assert "SynapseGrid" in client.get("/").text
        r = client.post("/api/evaluate-load-shift", json={
            "telemetry": {"irradiance_wm2": {"SL-NTA": 120}, "line_derate": {"L-CHG-SEL": 0.0}},
            "unsafe_overload": True, "commit": False})
        body = r.json()
        assert r.status_code == 200 and body["verdict"] == "CERTIFIED"
        assert body["counterexamples"] and body["proof_certificate"]["hash"]
        assert client.post("/api/evaluate-load-shift", json={"telemetry": {"demand_mw": {"NOPE": 1}}}).status_code == 422
        r = client.post("/api/inject-grid-fault", json={"fault": "cloud_cover", "active": True})
        assert r.status_code == 200 and r.json()["faults"]["cloud_cover"] is True
