"""
Dispatch optimizer: single-interval DC optimal power flow solved as a linear program with
Google OR-Tools (GLOP).

Decision variables (kW unless noted)
    p_g            output of each generator g            0 ≤ p_g ≤ P_avail_g  (diesel also ramp-limited)
    d_b, c_b       discharge / charge of each battery b   bounded by rating and by SoC limits over Δt
    s_l            served demand of each load l           0 ≤ s_l ≤ D_l ;  s_l = D_l for critical loads
    θ_n            bus voltage angle (rad), θ_slack = 0
    f_k            line flow = (θ_from − θ_to)/x_k · S_base ,  |f_k| ≤ C_k · derate_k

Power balance at every bus n
    Σ_g∈n p_g + Σ_b∈n (d_b − c_b) − Σ_l∈n s_l  =  Σ_k:from=n f_k − Σ_k:to=n f_k

Objective ($/h, minimised)
    diesel fuel+O&M + carbon price × emissions
  + battery degradation + stored-energy value of discharge − stored-energy value of charging
  + Σ non-critical VOLL × priority weight × (D_l − s_l)

Critical loads are hard constraints: if they cannot be met the problem is INFEASIBLE and the
system reports failure instead of shedding a critical load. Hard constraints are never traded
against the carbon or cost terms.
"""

from __future__ import annotations

import time
from typing import Any, Literal, Optional

import ortools
from ortools.linear_solver import pywraplp
from pydantic import BaseModel, Field

from grid import (
    ASSETS, BATTERIES, BESS_DEGRADATION_USD_PER_MWH, BESS_ETA, BESS_STORED_VALUE_USD_PER_MWH, BUSES,
    CARBON_PRICE_USD_PER_T, DIESEL_COST_USD_PER_MWH, DT_H, EMISSION_FACTORS_KG_PER_KWH, GENERATORS, LINES,
    LOAD_BY_ID, LOADS, PRIORITY_WEIGHTS, S_BASE_KW, SLACK_BUS, SOC_MAX, SOC_MIN, Telemetry, VOLL_USD_PER_MWH,
)

OPTIMIZER_VERSION = f"ortools-{ortools.__version__}-glop/dcopf-v1"
MARGIN_KW = 1.0  # planner keeps this headroom so rounding to 0.1 kW never crosses a limit


class Priorities(BaseModel):
    """Operational priorities for non-critical loads (defaults, or validated Gemma output)."""

    load_priority: dict[str, int] = Field(default_factory=lambda: {ld.id: ld.priority for ld in LOADS if not ld.critical})
    protected_loads: list[str] = Field(default_factory=list)
    source: str = "default"


class PlanResult(BaseModel):
    status: Literal["OPTIMAL", "INFEASIBLE", "ERROR"]
    proposal: Optional[dict[str, dict[str, float]]] = None
    claimed_line_flow_kw: dict[str, float] = Field(default_factory=dict)
    objective_usd_per_h: Optional[float] = None
    solve_ms: float = 0.0
    protected_applied: list[str] = Field(default_factory=list)
    cuts_applied: dict[str, float] = Field(default_factory=dict)
    message: str = ""
    optimizer: str = OPTIMIZER_VERSION


def marginal_cost_usd_per_mwh(kind: str) -> float:
    if kind == "diesel":
        return DIESEL_COST_USD_PER_MWH + CARBON_PRICE_USD_PER_T * EMISSION_FACTORS_KG_PER_KWH["diesel"]
    return 0.0


def solve_dispatch(tel: Telemetry, available_kw: dict[str, float], priorities: Priorities,
                   cuts: Optional[dict[str, float]] = None, protect: bool = True,
                   allow_critical_shortfall: bool = False) -> PlanResult:
    t0 = time.perf_counter()
    cuts = cuts or {}
    solver = pywraplp.Solver.CreateSolver("GLOP")
    if solver is None:
        return PlanResult(status="ERROR", message="GLOP solver unavailable")
    protected = [lid for lid in priorities.protected_loads if lid in LOAD_BY_ID] if protect else []

    def ub(key: str, value: float) -> float:
        return max(min(value, cuts.get(key, value)) - MARGIN_KW, 0.0)

    p: dict[str, Any] = {}
    for g in GENERATORS:
        hi = ub(g.id, available_kw[g.id])
        lo = 0.0
        prev = tel.prev_output_kw.get(g.id)
        if g.ramp_up_kw and prev is not None and available_kw[g.id] > 0:
            hi = min(hi, prev + g.ramp_up_kw - MARGIN_KW)
            lo = max(0.0, prev - g.ramp_down_kw + MARGIN_KW)
        p[g.id] = solver.NumVar(lo, hi, f"p_{g.id}")  # lo > hi ⇒ solver reports INFEASIBLE

    d: dict[str, Any] = {}
    c: dict[str, Any] = {}
    for b in BATTERIES:
        soc = tel.soc[b.id]
        dis_max = max(soc - SOC_MIN, 0.0) * b.energy_kwh * BESS_ETA / DT_H
        ch_max = max(SOC_MAX - soc, 0.0) * b.energy_kwh / (BESS_ETA * DT_H)
        d[b.id] = solver.NumVar(0.0, ub(b.id, min(b.cap_kw, dis_max)), f"d_{b.id}")
        c[b.id] = solver.NumVar(0.0, max(min(b.cap_kw, ch_max) - MARGIN_KW, 0.0), f"c_{b.id}")

    s: dict[str, Any] = {}
    short: dict[str, Any] = {}
    for ld in LOADS:
        dem = tel.demand_kw[ld.id]
        must = ld.critical or ld.id in protected
        if must and allow_critical_shortfall and ld.critical:
            s[ld.id] = solver.NumVar(0.0, dem, f"s_{ld.id}")
            short[ld.id] = dem
        else:
            s[ld.id] = solver.NumVar(dem if must else 0.0, dem, f"s_{ld.id}")

    theta = {bus.id: (solver.NumVar(0.0, 0.0, f"th_{bus.id}") if bus.id == SLACK_BUS
                      else solver.NumVar(-solver.infinity(), solver.infinity(), f"th_{bus.id}")) for bus in BUSES}
    f: dict[str, Any] = {}
    for ln in LINES:
        derate = tel.line_derate[ln.id]
        if derate <= 0:
            continue  # out of service
        lim = ub(ln.id, ln.cap_kw * derate)
        f[ln.id] = solver.NumVar(-lim, lim, f"f_{ln.id}")
        solver.Add(f[ln.id] == (theta[ln.frm] - theta[ln.to]) * (S_BASE_KW / ln.x_pu))

    for bus in BUSES:
        inj = (sum(p[g.id] for g in GENERATORS if g.bus == bus.id)
               + sum(d[b.id] - c[b.id] for b in BATTERIES if b.bus == bus.id)
               - sum(s[ld.id] for ld in LOADS if ld.bus == bus.id))
        out = sum(f[ln.id] for ln in LINES if ln.id in f and ln.frm == bus.id) \
            - sum(f[ln.id] for ln in LINES if ln.id in f and ln.to == bus.id)
        solver.Add(inj == out)

    obj = solver.Objective()
    for g in GENERATORS:
        obj.SetCoefficient(p[g.id], marginal_cost_usd_per_mwh(g.kind) / 1000)
    for b in BATTERIES:
        obj.SetCoefficient(d[b.id], (BESS_DEGRADATION_USD_PER_MWH + BESS_STORED_VALUE_USD_PER_MWH) / 1000)
        obj.SetCoefficient(c[b.id], (BESS_DEGRADATION_USD_PER_MWH - BESS_STORED_VALUE_USD_PER_MWH * BESS_ETA) / 1000)
    for ld in LOADS:
        if ld.id in short:
            obj.SetCoefficient(s[ld.id], -1e6 / 1000)
        elif not ld.critical:
            w = PRIORITY_WEIGHTS[min(max(priorities.load_priority.get(ld.id, ld.priority), 1), 5)]
            obj.SetCoefficient(s[ld.id], -VOLL_USD_PER_MWH * w / 1000)
    # Constant offset so the objective reads as total $/h including the unserved-load penalty.
    obj.SetOffset(sum(VOLL_USD_PER_MWH * PRIORITY_WEIGHTS[min(max(priorities.load_priority.get(ld.id, ld.priority), 1), 5)]
                      * tel.demand_kw[ld.id] / 1000 for ld in LOADS if not ld.critical and ld.id not in short))
    obj.SetMinimization()

    status = solver.Solve()
    ms = round((time.perf_counter() - t0) * 1000, 3)
    if status != pywraplp.Solver.OPTIMAL:
        return PlanResult(status="INFEASIBLE" if status == pywraplp.Solver.INFEASIBLE else "ERROR", solve_ms=ms,
                          protected_applied=protected, cuts_applied=cuts,
                          message="no dispatch satisfies all hard constraints" if status == pywraplp.Solver.INFEASIBLE
                          else f"solver status {status}")

    r = lambda v: round(v, 1) + 0.0  # noqa: E731  (+0.0 normalises -0.0)
    proposal = {
        "generation_kw": {g.id: r(p[g.id].solution_value()) for g in GENERATORS},
        "battery_kw": {b.id: r(d[b.id].solution_value() - c[b.id].solution_value()) for b in BATTERIES},
        "served_kw": {ld.id: (tel.demand_kw[ld.id] if (ld.critical and ld.id not in short) or ld.id in protected
                              else r(s[ld.id].solution_value())) for ld in LOADS},
    }
    # Rounding can leave a sub-kW imbalance; absorb it in the largest flexible generator within its bounds.
    gap = round(sum(proposal["served_kw"].values()) - sum(proposal["generation_kw"].values())
                - sum(proposal["battery_kw"].values()), 1)
    if gap:
        for g in sorted(GENERATORS, key=lambda g: -proposal["generation_kw"][g.id]):
            v = proposal["generation_kw"][g.id] + gap
            if p[g.id].lb() - 1e-9 <= v <= p[g.id].ub() + MARGIN_KW - 0.5:
                proposal["generation_kw"][g.id] = r(v)
                break
    return PlanResult(
        status="OPTIMAL", proposal=proposal,
        claimed_line_flow_kw={k: r(v.solution_value()) for k, v in f.items()},
        objective_usd_per_h=round(obj.Value(), 2), solve_ms=ms, protected_applied=protected, cuts_applied=cuts,
        message="optimal", )


def critical_shortfall_kw(tel: Telemetry, available_kw: dict[str, float]) -> float:
    """When the hard problem is infeasible, quantify how much critical load cannot be served."""
    res = solve_dispatch(tel, available_kw, Priorities(), protect=False, allow_critical_shortfall=True)
    if res.status != "OPTIMAL" or res.proposal is None:
        return float(sum(tel.demand_kw[ld.id] for ld in LOADS if ld.critical))
    return round(sum(tel.demand_kw[ld.id] - res.proposal["served_kw"][ld.id] for ld in LOADS if ld.critical), 1)


def unsafe_proposal(tel: Telemetry, available_kw: dict[str, float]) -> dict[str, dict[str, float]]:
    """
    Deterministic *intentionally unsafe* proposal, standing in for a buggy planner, a careless
    manual override, or an LLM-suggested plan ("restart the diesel to cover the shortfall and top
    up the Port battery for the evening"): serve every load in full, use all renewables, charge the
    Port battery at its full rating, and make up everything else with diesel, ignoring the
    generator's availability, its ramp limit, and the network. Power is balanced, so every
    rejection is about a physical limit, not arithmetic.
    """
    served = {ld.id: tel.demand_kw[ld.id] for ld in LOADS}
    gen = {g.id: (round(available_kw[g.id], 1) if g.kind in ("solar", "wind") else 0.0) for g in GENERATORS}
    port = next(b for b in BATTERIES if b.id == "BESS-PRT")
    battery = {b.id: (-port.cap_kw if b.id == port.id else 0.0) for b in BATTERIES}
    gen["DG-HARB"] = round(sum(served.values()) - sum(gen.values()) - sum(battery.values()), 1)
    if gen["DG-HARB"] < 0:  # surplus renewables: curtail PV instead of negative diesel
        gen["PV-HILL"] = round(gen["PV-HILL"] + gen["DG-HARB"], 1)
        gen["DG-HARB"] = 0.0
    return {"generation_kw": gen, "battery_kw": battery, "served_kw": served}


def asset_kind(asset_id: str) -> str:
    return next(a.kind for a in ASSETS if a.id == asset_id)
