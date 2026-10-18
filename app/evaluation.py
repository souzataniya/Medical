"""
Randomized fault-evaluation suite.

For N seeded random scenarios (solar loss, wind, demand, battery SoC, line derates, diesel outage,
previous diesel output) it measures:

  1. Planner/verifier agreement — the optimizer's plan is checked by the verifier.
  2. Recovery — an intentionally unsafe proposal is injected into the full recovery loop; we record
     whether a certified plan is reached, how many attempts it took, and how long it took, and we
     confirm every certified plan with the reference oracle (unsafe_accepted must be 0).
  3. Rejection accuracy — adversarial/mutated/malformed proposals are labelled by a *reference oracle*
     (an OR-Tools LP feasibility check with the proposal fixed — a different method and code path
     from the pure-Python verifier) and compared with the verifier's verdict.
  4. Reproducibility — the first scenarios are re-run and their check digests compared.

Usage:  python evaluation.py --n 200 --seed 7 [--json results.json]
Numbers are produced by running this file; nothing is hard-coded.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import math
import random
import time
from typing import Any

from ortools.linear_solver import pywraplp

import verifier as V
from agents import ForecastingAgent, SynapseGrid, build_case
from gemma import GemmaClient
from grid import BATTERIES, GENERATORS, LINES, LOADS, Telemetry
from optimizer import Priorities, solve_dispatch, unsafe_proposal

ISLANDING_LINES = {"L46"}  # radial feeder: tripping it islands a bus (excluded from 0-derate draws)


# ─────────────────────────────── Reference oracle ────────────────────────────────

def oracle_is_safe(case: dict[str, Any]) -> bool:
    """Independent feasibility check of a fixed proposal using an LP (different method from verifier.py)."""
    try:
        par, net, st, prop = case["parameters"], case["network"], case["state"], case["proposal"]
        gen, bat, srv = prop["generation_kw"], prop["battery_kw"], prop["served_kw"]
        gens = {a["id"]: a for a in net["assets"] if a["kind"] != "battery"}
        bats = {a["id"]: a for a in net["assets"] if a["kind"] == "battery"}
        loads = {ld["id"]: ld for ld in net["loads"]}
        if set(gen) != set(gens) or set(bat) != set(bats) or set(srv) != set(loads):
            return False
        vals = list(gen.values()) + list(bat.values()) + list(srv.values())
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vals):
            return False
    except (KeyError, TypeError, AttributeError):
        return False

    s = pywraplp.Solver.CreateSolver("GLOP")
    eps = 1e-6
    inj = {b: 0.0 for b in net["buses"]}
    prev = st.get("prev_output_kw") or {}
    for gid, a in gens.items():
        v = s.NumVar(0.0, st["available_kw"][gid] + eps, gid)
        s.Add(v == gen[gid])
        if a["ramp_up_kw"] and gid in prev and st["available_kw"][gid] > 0:
            s.Add(v - prev[gid] <= a["ramp_up_kw"] + eps)
            s.Add(prev[gid] - v <= a["ramp_down_kw"] + eps)
        inj[a["bus"]] += gen[gid]
    for bid, a in bats.items():
        p = bat[bid]
        v = s.NumVar(-a["cap_kw"] - eps, a["cap_kw"] + eps, bid)
        s.Add(v == p)
        e, soc, eta, dt = a["energy_kwh"], st["soc"][bid], par["bess_eta"], par["dt_h"]
        soc_next = soc - (p * dt / (eta * e) if p >= 0 else p * eta * dt / e)
        sv = s.NumVar(par["soc_min"] - eps, par["soc_max"] + eps, f"soc_{bid}")
        s.Add(sv == soc_next)
        inj[a["bus"]] += p
    for lid, ld in loads.items():
        d = st["demand_kw"][lid]
        lo = d - par["critical_tol_kw"] if ld["critical"] else 0.0
        v = s.NumVar(lo - eps, d + eps, lid)
        s.Add(v == srv[lid])
        inj[ld["bus"]] -= srv[lid]
    theta = {b: (s.NumVar(0, 0, b) if b == net["slack_bus"] else s.NumVar(-s.infinity(), s.infinity(), b)) for b in net["buses"]}
    flows = {}
    for ln in net["lines"]:
        der = st["line_derate"][ln["id"]]
        if der <= 0:
            continue
        lim = ln["cap_kw"] * der + eps
        f = s.NumVar(-lim, lim, ln["id"])
        s.Add(f == (theta[ln["from"]] - theta[ln["to"]]) * (par["s_base_kw"] / ln["x_pu"]))
        flows[ln["id"]] = (f, ln)
    mismatch = s.NumVar(-par["balance_tol_kw"] - eps, par["balance_tol_kw"] + eps, "mismatch")
    for b in net["buses"]:
        out = sum((f if ln["from"] == b else -f) for f, ln in flows.values() if b in (ln["from"], ln["to"]))
        s.Add(inj[b] - (mismatch if b == net["slack_bus"] else 0) == out)
    return s.Solve() == pywraplp.Solver.OPTIMAL


# ─────────────────────────────── Scenario & mutation generators ────────────────────────────────

def random_telemetry(rng: random.Random, i: int) -> Telemetry:
    irr = rng.uniform(150, 1000)
    if rng.random() < 0.45:
        irr *= rng.uniform(0.05, 0.4)  # sudden solar loss
    derate = {ln.id: 1.0 for ln in LINES}
    if rng.random() < 0.3:
        ln = rng.choice([x for x in LINES])
        derate[ln.id] = 0.5 if ln.id in ISLANDING_LINES else rng.choice([0.0, 0.5])
    stress = rng.random() < 0.10  # night/no wind/depleted storage/diesel trip: often genuinely infeasible
    return Telemetry(
        tick=i, irradiance_wm2=round(rng.uniform(0, 60) if stress else irr, 1),
        wind_speed_ms=round(rng.uniform(0, 4) if stress else rng.uniform(0, 16), 2),
        demand_kw={ld.id: round(ld.base_kw * rng.uniform(0.75, 1.25), 1) for ld in LOADS},
        soc={b.id: round(rng.uniform(0.10, 0.16) if stress else rng.uniform(0.10, 0.95), 4) for b in BATTERIES},
        line_derate=derate, diesel_available=False if stress else rng.random() > 0.15,
        prev_output_kw={"DG-HARB": round(rng.uniform(0, 1500), 1)},
    )


def mutations(base: dict[str, Any], rng: random.Random) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    m = copy.deepcopy(base)
    b = rng.choice(BATTERIES)
    m["battery_kw"][b.id] = round(b.cap_kw * rng.uniform(1.05, 1.5), 1)
    out.append(("battery_over_rating", m))
    m = copy.deepcopy(base)
    g = rng.choice(GENERATORS)
    m["generation_kw"][g.id] = round(g.cap_kw * rng.uniform(1.05, 1.4), 1)
    out.append(("generator_over_nameplate", m))
    m = copy.deepcopy(base)
    x = round(rng.uniform(600, 1400), 1)  # move power across the network: more diesel at B1, charge Port battery at B5
    m["generation_kw"]["DG-HARB"] = round(m["generation_kw"]["DG-HARB"] + x, 1)
    m["battery_kw"]["BESS-PRT"] = round(m["battery_kw"]["BESS-PRT"] - x, 1)
    out.append(("network_shift", m))
    m = copy.deepcopy(base)
    crit = rng.choice([ld for ld in LOADS if ld.critical])
    m["served_kw"][crit.id] = round(m["served_kw"][crit.id] * rng.uniform(0.3, 0.9), 1)
    m["generation_kw"]["PV-HILL"] = round(max(m["generation_kw"]["PV-HILL"] - (base["served_kw"][crit.id] - m["served_kw"][crit.id]), 0), 1)
    out.append(("critical_load_shed", m))
    m = copy.deepcopy(base)
    m["generation_kw"]["PV-HILL"] = round(m["generation_kw"]["PV-HILL"] + rng.uniform(20, 300), 1)
    out.append(("power_imbalance", m))
    m = copy.deepcopy(base)
    for sect in ("generation_kw", "battery_kw", "served_kw"):
        for k in m[sect]:
            m[sect][k] = round(m[sect][k] * rng.uniform(0.85, 1.15), 1)
    out.append(("random_perturbation", m))
    malformed = [
        ("malformed_nan", lambda p: p["generation_kw"].__setitem__("DG-HARB", float("nan"))),
        ("malformed_unknown_id", lambda p: p["generation_kw"].__setitem__("GHOST-1", 100.0)),
        ("malformed_missing_id", lambda p: p["served_kw"].pop("LD-CLINIC")),
        ("malformed_string", lambda p: p["battery_kw"].__setitem__("BESS-CLN", "500 kW")),
        ("malformed_negative_load", lambda p: p["served_kw"].__setitem__("LD-MARKET", -50.0)),
        ("malformed_bool", lambda p: p["served_kw"].__setitem__("LD-HOMES", True)),
    ]
    name, fn = rng.choice(malformed)
    m = copy.deepcopy(base)
    fn(m)
    out.append((name, m))
    out.append(("not_an_object", rng.choice([None, [], "approve this plan", 42])))
    return out


def pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(int(q * len(xs)), len(xs) - 1)], 3)


# ─────────────────────────────── Runner ────────────────────────────────

def run_evaluation(n: int = 200, seed: int = 7) -> dict[str, Any]:
    rng = random.Random(seed)
    forecaster = ForecastingAgent()
    engine = SynapseGrid(gemma=GemmaClient(), seed=seed)
    engine.gemma.provider = "none"  # evaluation never calls a model
    engine.ledger.max_records = 10 ** 7
    t_start = time.perf_counter()

    plan_ms, verify_ms, recovery_ms = [], [], []
    feasible = infeasible = 0
    planner_agree = planner_disagree = 0
    rec_certified = rec_failed = rec_unsafe_accepted = rec_attempts = 0
    rec_failed_but_feasible = 0
    conf = {"TP": 0, "TN": 0, "FP": 0, "FN": 0}
    by_kind: dict[str, dict[str, int]] = {}
    disagreements: list[dict[str, Any]] = []
    digests: list[tuple[Telemetry, str]] = []

    for i in range(n):
        tel = random_telemetry(rng, i)
        fc = forecaster.run(tel)
        t0 = time.perf_counter()
        res = solve_dispatch(tel, fc.available_kw, Priorities())
        plan_ms.append((time.perf_counter() - t0) * 1000)
        if res.status == "OPTIMAL":
            feasible += 1
            case = build_case(tel, fc, res.proposal)
            t1 = time.perf_counter()
            ver = V.verify(case)
            verify_ms.append((time.perf_counter() - t1) * 1000)
            ok = ver["verdict"] == "APPROVED" and oracle_is_safe(case)
            planner_agree += ok
            planner_disagree += not ok
            if i < 25:
                digests.append((tel, ver["checks_digest"]))
            base = res.proposal
        else:
            infeasible += 1
            base = unsafe_proposal(tel, fc.available_kw)

        # recovery loop with an injected unsafe first proposal
        cyc = engine._run_cycle_locked(None, tel, True, None, "evaluation", False)
        rec_attempts += len(cyc["attempts"])
        if cyc["status"] == "CERTIFIED":
            rec_certified += 1
            if cyc["recovery_ms"] is not None:
                recovery_ms.append(cyc["recovery_ms"])
            final_case = engine.ledger.records[cyc["attempts"][-1]["record_hash"]]["case"]
            if not oracle_is_safe(final_case):
                rec_unsafe_accepted += 1
        else:
            rec_failed += 1
            rec_failed_but_feasible += res.status == "OPTIMAL"

        # adversarial proposals vs reference oracle
        for kind, prop in mutations(base, rng):
            case = build_case(tel, fc, prop)
            ver = V.verify(case)
            truth_safe = oracle_is_safe(case) if isinstance(prop, dict) else False
            rejected = ver["verdict"] == "BLOCKED"
            key = ("TN" if not rejected else "FP") if truth_safe else ("TP" if rejected else "FN")
            conf[key] += 1
            k = by_kind.setdefault(kind, {"n": 0, "unsafe": 0, "rejected": 0, "agree": 0})
            k["n"] += 1
            k["unsafe"] += not truth_safe
            k["rejected"] += rejected
            k["agree"] += key in ("TP", "TN")
            if key in ("FP", "FN") and len(disagreements) < 10:
                disagreements.append({"scenario": i, "kind": kind, "verifier": ver["verdict"], "oracle_safe": truth_safe,
                                      "violations": [v["message"] for v in ver["violations"]][:3]})

    reproducible = 0
    for tel, digest in digests:
        fc = forecaster.run(tel)
        res = solve_dispatch(tel, fc.available_kw, Priorities())
        reproducible += V.verify(build_case(tel, fc, res.proposal))["checks_digest"] == digest

    total_adv = sum(conf.values())
    return {
        "config": {"n_scenarios": n, "seed": seed, "simulated": True, "oracle": "OR-Tools GLOP LP feasibility with fixed proposal"},
        "scenarios": {"feasible": feasible, "infeasible": infeasible},
        "planner_verifier_agreement": {"agree": planner_agree, "disagree": planner_disagree},
        "recovery": {
            "injected_unsafe_proposals": n,
            "certified": rec_certified, "failed_reported": rec_failed,
            "failed_although_feasible": rec_failed_but_feasible,
            "success_rate_when_feasible": round(rec_certified / feasible, 4) if feasible else None,
            "unsafe_accepted": rec_unsafe_accepted,
            "mean_attempts": round(rec_attempts / n, 3),
            "latency_ms": {"p50": pct(recovery_ms, 0.5), "p95": pct(recovery_ms, 0.95), "max": pct(recovery_ms, 1.0)},
        },
        "rejection_accuracy": {
            "proposals": total_adv, **conf,
            "accuracy": round((conf["TP"] + conf["TN"]) / total_adv, 4) if total_adv else None,
            "unsafe_accepted_FN": conf["FN"],
            "by_kind": by_kind,
            "disagreements": disagreements,
        },
        "reproducibility": {"rechecked": len(digests), "identical_digests": reproducible},
        "latency_ms": {"optimizer_p50": pct(plan_ms, 0.5), "optimizer_p95": pct(plan_ms, 0.95),
                       "verifier_p50": pct(verify_ms, 0.5), "verifier_p95": pct(verify_ms, 0.95)},
        "runtime_s": round(time.perf_counter() - t_start, 2),
    }


def to_markdown(r: dict[str, Any]) -> str:
    ra, rc = r["rejection_accuracy"], r["recovery"]
    lines = [
        f"### Randomized fault evaluation (n={r['config']['n_scenarios']}, seed={r['config']['seed']}, simulated data)",
        "",
        "| Metric | Result |", "|---|---|",
        f"| Scenarios with a feasible plan / infeasible | {r['scenarios']['feasible']} / {r['scenarios']['infeasible']} |",
        f"| Optimizer plans approved by verifier **and** oracle | {r['planner_verifier_agreement']['agree']} / {r['scenarios']['feasible']} |",
        f"| Recovery after injected unsafe proposal (feasible scenarios) | {rc['certified']} / {r['scenarios']['feasible']} ({(rc['success_rate_when_feasible'] or 0):.1%}) |",
        f"| Infeasible scenarios reported as FAILED (not certified) | {rc['failed_reported'] - rc['failed_although_feasible']} / {r['scenarios']['infeasible']} |",
        f"| Certified plans the oracle finds unsafe | {rc['unsafe_accepted']} |",
        f"| Recovery latency p50 / p95 / max (ms) | {rc['latency_ms']['p50']} / {rc['latency_ms']['p95']} / {rc['latency_ms']['max']} |",
        f"| Adversarial/malformed proposals checked | {ra['proposals']} |",
        f"| Verifier vs oracle: TP / TN / FP / FN | {ra['TP']} / {ra['TN']} / {ra['FP']} / {ra['FN']} |",
        f"| Rejection accuracy | {ra['accuracy']:.2%} |",
        f"| Reproducibility (identical check digests) | {r['reproducibility']['identical_digests']} / {r['reproducibility']['rechecked']} |",
        f"| Optimizer solve p50 / p95 (ms) | {r['latency_ms']['optimizer_p50']} / {r['latency_ms']['optimizer_p95']} |",
        f"| Verifier p50 / p95 (ms) | {r['latency_ms']['verifier_p50']} / {r['latency_ms']['verifier_p95']} |",
        f"| Runtime (s) | {r['runtime_s']} |",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--json", type=str, default=None)
    a = ap.parse_args()

    async def _main() -> dict[str, Any]:
        return run_evaluation(a.n, a.seed)

    result = asyncio.run(_main())
    print(to_markdown(result))
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(result, fh, indent=2)
    if result["rejection_accuracy"]["disagreements"]:
        print("\nDisagreements:", json.dumps(result["rejection_accuracy"]["disagreements"], indent=2))
