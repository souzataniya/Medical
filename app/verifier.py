"""
Independent deterministic dispatch verifier.

* Standard library only. It imports nothing from the planner, the optimizer, or the agents,
  and it never uses the planner's claimed line flows: it recomputes bus injections and DC
  power-flow line loadings from the proposal and the self-contained network in the case.
* Same case in → same checks, same verdict, same digest out.
* Its result is a *verified constraint certificate within the modeled assumptions*
  (lossless DC power flow, single 15-minute interval, declared limits). It is not a formal
  proof and says nothing about anything outside the model.

CLI:  python verifier.py record.json      (or:  curl …/api/audit/<hash> | python verifier.py -)
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
from typing import Any, Optional

VERIFIER_VERSION = "synapsegrid-verifier/2.0"
CASE_SCHEMA = "synapsegrid.case/1"


# ─────────────────────────────── Canonical hashing ────────────────────────────────

def _rounded(obj: Any) -> Any:
    if isinstance(obj, bool) or obj is None or isinstance(obj, (str, int)):
        return obj
    if isinstance(obj, float):
        return round(obj, 4) if math.isfinite(obj) else repr(obj)
    if isinstance(obj, dict):
        return {str(k): _rounded(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_rounded(v) for v in obj]
    return repr(obj)


def canonical_json(obj: Any) -> str:
    """Sorted keys, compact separators, floats rounded to 4 dp, non-finite floats as strings."""
    return json.dumps(_rounded(obj), sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_hex(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode()).hexdigest()


# ─────────────────────────────── Helpers ────────────────────────────────

def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _solve(a: list[list[float]], b: list[float]) -> Optional[list[float]]:
    """Gaussian elimination with partial pivoting. Returns None if singular."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            return None
        m[col], m[piv] = m[piv], m[col]
        for r in range(col + 1, n):
            f = m[r][col] / m[col][col]
            if f:
                for c in range(col, n + 1):
                    m[r][c] -= f * m[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (m[r][n] - sum(m[r][c] * x[c] for c in range(r + 1, n))) / m[r][r]
    return x


def dc_power_flow(network: dict[str, Any], injections_kw: dict[str, float], derate: dict[str, float],
                  s_base_kw: float) -> Optional[dict[str, float]]:
    """Line flows (kW, + = from→to) for given bus injections. Lines with derate 0 are out of service."""
    buses = list(network["buses"])
    slack = network["slack_bus"]
    others = [b for b in buses if b != slack]
    idx = {b: i for i, b in enumerate(others)}
    n = len(others)
    bmat = [[0.0] * n for _ in range(n)]
    in_service = [ln for ln in network["lines"] if derate.get(ln["id"], 1.0) > 0]
    for ln in in_service:
        y = 1.0 / ln["x_pu"]
        for u, v in ((ln["from"], ln["to"]), (ln["to"], ln["from"])):
            if u in idx:
                bmat[idx[u]][idx[u]] += y
                if v in idx:
                    bmat[idx[u]][idx[v]] -= y
    p = [injections_kw.get(b, 0.0) / s_base_kw for b in others]
    theta_red = _solve(bmat, p)
    if theta_red is None:
        return None
    theta = {slack: 0.0, **{b: theta_red[idx[b]] for b in others}}
    flows = {ln["id"]: 0.0 for ln in network["lines"]}
    for ln in in_service:
        flows[ln["id"]] = (theta[ln["from"]] - theta[ln["to"]]) / ln["x_pu"] * s_base_kw
    return flows


# ─────────────────────────────── Input validation ────────────────────────────────

def _validate(case: Any) -> list[dict[str, Any]]:
    errs: list[dict[str, Any]] = []

    def bad(path: str, observed: Any, expected: str) -> None:
        errs.append({"constraint_id": f"INPUT_VALID[{path}]", "family": "INPUT_VALID", "element": path,
                     "element_type": "input", "observed": repr(observed)[:80], "expected": expected,
                     "limit": None, "unit": "", "excess": None, "severity": "critical",
                     "message": f"{path}: expected {expected}, got {repr(observed)[:80]}"})

    if not isinstance(case, dict):
        bad("case", case, "object")
        return errs
    if case.get("schema") != CASE_SCHEMA:
        bad("schema", case.get("schema"), CASE_SCHEMA)
    for key in ("parameters", "network", "state", "proposal"):
        if not isinstance(case.get(key), dict):
            bad(key, case.get(key), "object")
    if errs:
        return errs
    net, st, prop, par = case["network"], case["state"], case["proposal"], case["parameters"]
    for extra in sorted(set(prop) - {"generation_kw", "battery_kw", "served_kw"}):
        bad(f"proposal.{extra}", extra, "only generation_kw, battery_kw, served_kw (planner claims are not accepted)")
    for k in ("dt_h", "soc_min", "soc_max", "bess_eta", "s_base_kw", "balance_tol_kw", "critical_tol_kw"):
        if not _is_num(par.get(k)) or par.get(k) <= 0 and k not in ("soc_min",):
            bad(f"parameters.{k}", par.get(k), "positive finite number")
    gens = [a["id"] for a in net.get("assets", []) if a.get("kind") != "battery"]
    bats = [a["id"] for a in net.get("assets", []) if a.get("kind") == "battery"]
    loads = [ld["id"] for ld in net.get("loads", [])]
    lines = [ln["id"] for ln in net.get("lines", [])]
    expected_sets = {
        "proposal.generation_kw": (prop.get("generation_kw"), gens, "kW"),
        "proposal.battery_kw": (prop.get("battery_kw"), bats, "kW (+discharge / −charge)"),
        "proposal.served_kw": (prop.get("served_kw"), loads, "kW"),
        "state.available_kw": (st.get("available_kw"), gens, "kW"),
        "state.demand_kw": (st.get("demand_kw"), loads, "kW"),
        "state.soc": (st.get("soc"), bats, "fraction 0–1"),
        "state.line_derate": (st.get("line_derate"), lines, "fraction 0–1"),
    }
    for path, (mapping, ids, unit) in expected_sets.items():
        if not isinstance(mapping, dict):
            bad(path, mapping, f"object keyed by {ids}")
            continue
        extra, missing = set(mapping) - set(ids), set(ids) - set(mapping)
        for k in sorted(extra):
            bad(f"{path}.{k}", k, "a known element id")
        for k in sorted(missing):
            bad(f"{path}.{k}", None, f"a finite number in {unit}")
        for k in ids:
            if k in mapping and not _is_num(mapping[k]):
                bad(f"{path}.{k}", mapping[k], f"a finite number in {unit}")
    for path in ("state.soc", "state.line_derate"):
        mapping = expected_sets[path][0]
        if isinstance(mapping, dict):
            for k, v in mapping.items():
                if _is_num(v) and not 0 <= v <= 1:
                    bad(f"{path}.{k}", v, "within [0, 1]")
    for path in ("state.available_kw", "state.demand_kw"):
        mapping = expected_sets[path][0]
        if isinstance(mapping, dict):
            for k, v in mapping.items():
                if _is_num(v) and v < 0:
                    bad(f"{path}.{k}", v, "non-negative kW")
    prev = st.get("prev_output_kw")
    if prev is not None and (not isinstance(prev, dict) or any(not _is_num(v) for v in prev.values())):
        bad("state.prev_output_kw", prev, "object of finite kW values or null")
    return errs


# ─────────────────────────────── Verification ────────────────────────────────

FAMILY_SEVERITY = {"CRITICAL_LOAD": "critical", "POWER_BALANCE": "critical", "NETWORK": "critical"}


def verify(case: Any) -> dict[str, Any]:
    """Evaluate every modeled constraint for a proposed dispatch. Never raises on bad input."""
    input_errors = _validate(case)
    if input_errors:
        return _result("BLOCKED", [], input_errors, None, case)

    par, net, st, prop = case["parameters"], case["network"], case["state"], case["proposal"]
    dt, eta, tol_bal, tol_crit = par["dt_h"], par["bess_eta"], par["balance_tol_kw"], par["critical_tol_kw"]
    assets = {a["id"]: a for a in net["assets"]}
    loads = {ld["id"]: ld for ld in net["loads"]}
    gen, bat, served = prop["generation_kw"], prop["battery_kw"], prop["served_kw"]
    checks: list[dict[str, Any]] = []

    def check(cid: str, family: str, element: str, etype: str, observed: float, lower: Optional[float],
              upper: Optional[float], unit: str, expression: str) -> None:
        lo_slack = observed - lower if lower is not None else math.inf
        hi_slack = upper - observed if upper is not None else math.inf
        slack = min(lo_slack, hi_slack)
        checks.append({"constraint_id": cid, "family": family, "element": element, "element_type": etype,
                       "observed": observed, "lower": lower, "upper": upper, "unit": unit,
                       "slack": slack, "passed": slack >= -1e-6, "expression": expression})

    # Generators: availability envelope and ramp
    prev = st.get("prev_output_kw") or {}
    for gid, a in sorted(assets.items()):
        if a["kind"] == "battery":
            continue
        p, avail = gen[gid], st["available_kw"][gid]
        check(f"GEN_LIMIT[{gid}]", "GEN_LIMIT", gid, "asset", p, 0.0, avail, "kW", "0 ≤ P ≤ P_available")
        if a.get("ramp_up_kw") and gid in prev and avail > 0:  # tripped units are not ramp-limited
            check(f"GEN_RAMP[{gid}]", "GEN_RAMP", gid, "asset", p - prev[gid], -a["ramp_down_kw"], a["ramp_up_kw"],
                  "kW", "−R_down ≤ P − P_prev ≤ R_up")

    # Batteries: power rating and state of charge after the interval
    soc_next: dict[str, float] = {}
    for bid, a in sorted(assets.items()):
        if a["kind"] != "battery":
            continue
        p, soc, e = bat[bid], st["soc"][bid], a["energy_kwh"]
        check(f"BESS_POWER[{bid}]", "BESS_POWER", bid, "asset", p, -a["cap_kw"], a["cap_kw"], "kW",
              "−P_rated ≤ P ≤ P_rated")
        nxt = soc - p * dt / (eta * e) if p >= 0 else soc - p * eta * dt / e
        soc_next[bid] = nxt
        check(f"BESS_SOC[{bid}]", "BESS_SOC", bid, "asset", nxt, par["soc_min"], par["soc_max"], "p.u.",
              "SoC_min ≤ SoC − P·Δt/η·E ≤ SoC_max")

    # Loads: bounds and critical coverage
    for lid, ld in sorted(loads.items()):
        s, d = served[lid], st["demand_kw"][lid]
        check(f"LOAD_BOUNDS[{lid}]", "LOAD_BOUNDS", lid, "load", s, 0.0, d, "kW", "0 ≤ served ≤ demand")
        if ld["critical"]:
            check(f"CRITICAL_LOAD[{lid}]", "CRITICAL_LOAD", lid, "load", s, d - tol_crit, None, "kW",
                  "served ≥ demand (critical)")

    # System power balance (lossless DC model)
    supply = sum(gen.values()) + sum(bat.values())
    demand_served = sum(served.values())
    check("POWER_BALANCE[system]", "POWER_BALANCE", "system", "system", supply - demand_served, -tol_bal, tol_bal,
          "kW", "|Σ generation + Σ battery − Σ served| ≤ tol")

    # Recompute bus injections and line flows from first principles
    inj = {b: 0.0 for b in net["buses"]}
    for gid, p in gen.items():
        inj[assets[gid]["bus"]] += p
    for bid, p in bat.items():
        inj[assets[bid]["bus"]] += p
    for lid, s in served.items():
        inj[loads[lid]["bus"]] -= s
    flows = dc_power_flow(net, inj, st["line_derate"], par["s_base_kw"])
    if flows is None:
        check("NETWORK[connected]", "NETWORK", "network", "system", 1.0, None, 0.0, "",
              "in-service network must be connected (non-singular susceptance matrix)")
        flows = {}
    for ln in net["lines"]:
        if ln["id"] in flows:
            limit = ln["cap_kw"] * st["line_derate"][ln["id"]]
            check(f"LINE_CAPACITY[{ln['id']}]", "LINE_CAPACITY", ln["id"], "edge", abs(flows[ln["id"]]), None, limit,
                  "kW", "|f| ≤ C · derate  (f from DC power flow)")

    violations = []
    for c in checks:
        if c["passed"]:
            continue
        limit = c["upper"] if c["upper"] is not None and c["observed"] > c["upper"] else c["lower"]
        excess = -c["slack"]
        span = abs(limit) if limit else 1.0
        severity = FAMILY_SEVERITY.get(c["family"], "major" if excess / max(span, 1e-9) > 0.10 else "minor")
        expected = (f"≤ {c['upper']:.4g}" if limit == c["upper"] else f"≥ {c['lower']:.4g}") + f" {c['unit']}".rstrip()
        violations.append({
            "constraint_id": c["constraint_id"], "family": c["family"], "element": c["element"],
            "element_type": c["element_type"], "observed": c["observed"], "limit": limit, "expected": expected,
            "unit": c["unit"], "excess": excess, "severity": severity,
            "message": f"{c['constraint_id']}: observed {c['observed']:.4g} {c['unit']}, expected {expected} "
                       f"(excess {excess:.4g} {c['unit']})".replace("  ", " "),
        })
    computed = {"bus_injection_kw": inj, "line_flow_kw": flows, "soc_next": soc_next,
                "totals": {"supply_kw": supply, "served_kw": demand_served}}
    return _result("BLOCKED" if violations else "APPROVED", checks, violations, computed, case)


def _result(verdict: str, checks: list, violations: list, computed: Optional[dict], case: Any) -> dict[str, Any]:
    return {
        "verifier": VERIFIER_VERSION,
        "constraint_set": case.get("constraint_set") if isinstance(case, dict) else None,
        "verdict": verdict,
        "checks": checks,
        "violations": violations,
        "computed": computed,
        "checks_digest": sha256_hex({"verdict": verdict, "checks": checks, "violations": violations}),
    }


# ─────────────────────────────── Audit-record replay ────────────────────────────────

def record_hash(record: dict[str, Any]) -> str:
    return sha256_hex({k: v for k, v in record.items() if k != "record_hash"})


def replay(record: dict[str, Any]) -> dict[str, Any]:
    """Re-verify an audit record from its embedded case and compare with what was recorded."""
    fresh = verify(record.get("case"))
    recorded = record.get("verification", {})
    hash_ok = record.get("record_hash") == record_hash(record)
    same_verdict = fresh["verdict"] == recorded.get("verdict")
    same_digest = fresh["checks_digest"] == recorded.get("checks_digest")
    return {
        "reproduced": hash_ok and same_verdict and same_digest,
        "record_hash_valid": hash_ok,
        "verdict": fresh["verdict"],
        "recorded_verdict": recorded.get("verdict"),
        "checks_digest_match": same_digest,
        "checks": len(fresh["checks"]),
        "violations": len(fresh["violations"]),
        "verifier": VERIFIER_VERSION,
    }


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "-"
    data = json.load(sys.stdin if src == "-" else open(src))
    out = replay(data) if "case" in data else verify(data)
    print(json.dumps(out, indent=2, default=str))
    sys.exit(0 if out.get("reproduced", out.get("verdict") == "APPROVED") else 1)
