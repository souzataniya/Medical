"""
SynapseGrid agent network: typed agents, an explicit execution DAG with traces, bounded
verification-and-recovery, hash-chained audit records, and an in-process event stream.

Orchestration note: this is a small custom asyncio DAG. It is NeuroSAN-*inspired* (agents with
declared responsibilities and downstream tools) but it is NOT the NeuroSAN runtime. The event
stream is an in-process bus feeding the dashboard over SSE; it is NOT Google Cloud Pub/Sub.

DAG for one dispatch cycle
    forecasting ─┐
                 ├─► dispatch_planning ─► verification_recovery ─(blocked)─► dispatch_planning (≤ 4 attempts)
    priorities ──┘                              │
    (Gemma, optional)                           └─(certified)─► carbon_analysis
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import OrderedDict, deque
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

import verifier as V
from gemma import PROMPT_VERSION, GemmaClient, GemmaOutcome
from grid import (
    ASSETS, BATTERIES, BESS_DEGRADATION_USD_PER_MWH, CARBON_PRICE_USD_PER_T, CONSTRAINT_SET_VERSION,
    CRITICAL_LOADS, DIESEL_COST_USD_PER_MWH, EMISSION_FACTORS_KG_PER_KWH, GENERATORS, LINES, LOADS,
    SCENARIO_BASELINE, SOLAR_LOSS_FRACTION, GridSimulator, Telemetry, TelemetryOverrides, network_spec,
    parameters_spec, wind_power_fraction,
)
from optimizer import OPTIMIZER_VERSION, PlanResult, Priorities, critical_shortfall_kw, solve_dispatch, unsafe_proposal

MAX_ATTEMPTS = 4
AUDIT_SCHEMA = "synapsegrid.audit/1"

AGENT_MANIFEST = {
    "orchestration": "custom asyncio DAG (NeuroSAN-inspired; not the NeuroSAN runtime)",
    "event_transport": "in-process event bus streamed over SSE (not Google Cloud Pub/Sub)",
    "agents": [
        {"name": "forecasting", "responsibility": "Simulated weather → available kW per generator; demand per load",
         "output": "Forecast"},
        {"name": "priorities", "responsibility": "Gemma reads the operator's incident note → validated load priorities "
                                                  "(optional; defaults if unavailable)", "output": "Priorities"},
        {"name": "dispatch_planning", "responsibility": "OR-Tools DC-OPF: least cost+carbon dispatch under hard limits",
         "output": "PlanResult"},
        {"name": "verification_recovery", "responsibility": "Independent verifier; on violation feed cuts back to the "
                                                             "planner and retry (bounded) or report failure",
         "output": "RecoveryOutcome"},
        {"name": "carbon_analysis", "responsibility": "Emissions (declared factors), cost, critical coverage, unmet "
                                                      "demand of every proposal", "output": "ImpactMetrics"},
    ],
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ─────────────────────────────── Contracts ────────────────────────────────

class Forecast(BaseModel):
    simulated: bool = True
    available_kw: dict[str, float]
    demand_kw: dict[str, float]
    renewable_available_kw: float
    total_demand_kw: float
    critical_demand_kw: float
    alerts: list[str]


class ImpactMetrics(BaseModel):
    emissions_kg_per_h: float
    diesel_only_baseline_kg_per_h: float
    avoided_kg_per_h: float
    operating_cost_usd_per_h: float
    carbon_cost_usd_per_h: float
    served_kw: float
    demand_kw: float
    unmet_kw: float
    critical_coverage: float
    shed_kw: dict[str, float]
    renewable_share: float
    renewable_curtailed_kw: float
    diesel_kw: float
    battery_kw: float
    emission_factors_kg_per_kwh: dict[str, float] = Field(default_factory=lambda: dict(EMISSION_FACTORS_KG_PER_KWH))


class TraceStep(BaseModel):
    step: str
    agent: str
    depends_on: list[str]
    started_ms: float
    duration_ms: float
    status: Literal["ok", "blocked", "failed", "skipped", "fallback"]
    summary: str


# ─────────────────────────────── Agents ────────────────────────────────

class ForecastingAgent:
    name = "forecasting"

    def run(self, tel: Telemetry) -> Forecast:
        avail: dict[str, float] = {}
        for g in GENERATORS:
            if g.kind == "solar":
                avail[g.id] = round(g.cap_kw * min(tel.irradiance_wm2 / 1000, 1.1) * 0.90, 1)  # 10 % inverter/soiling
            elif g.kind == "wind":
                avail[g.id] = round(g.cap_kw * wind_power_fraction(tel.wind_speed_ms), 1)
            else:
                avail[g.id] = g.cap_kw if tel.diesel_available else 0.0
        renew = sum(avail[g.id] for g in GENERATORS if g.kind in ("solar", "wind"))
        alerts = []
        if tel.irradiance_wm2 < 400:
            alerts.append(f"SOLAR_DROP: irradiance {tel.irradiance_wm2:.0f} W/m² → PV available {avail['PV-HILL']:.0f} kW")
        if not tel.diesel_available:
            alerts.append("DIESEL_OUTAGE: Harbour genset unavailable")
        for b in BATTERIES:
            if tel.soc[b.id] < 0.2:
                alerts.append(f"LOW_SOC: {b.id} at {tel.soc[b.id]:.0%}")
        for lid, d in tel.line_derate.items():
            if d < 1:
                alerts.append(f"LINE_DERATE: {lid} at {d:.0%}")
        return Forecast(available_kw=avail, demand_kw=dict(tel.demand_kw), renewable_available_kw=round(renew, 1),
                        total_demand_kw=round(sum(tel.demand_kw.values()), 1),
                        critical_demand_kw=round(sum(tel.demand_kw[ld.id] for ld in CRITICAL_LOADS), 1), alerts=alerts)


class DispatchPlanningAgent:
    name = "dispatch_planning"

    def run(self, tel: Telemetry, forecast: Forecast, priorities: Priorities, cuts: dict[str, float],
            protect: bool) -> PlanResult:
        return solve_dispatch(tel, forecast.available_kw, priorities, cuts=cuts, protect=protect)


class CarbonAnalysisAgent:
    name = "carbon_analysis"

    def run(self, proposal: dict[str, dict[str, float]], forecast: Forecast) -> ImpactMetrics:
        gen, bat, served = proposal["generation_kw"], proposal["battery_kw"], proposal["served_kw"]
        emis = sum(gen[g.id] * EMISSION_FACTORS_KG_PER_KWH[g.kind] for g in GENERATORS)  # kW × kg/kWh = kg/h
        diesel = sum(gen[g.id] for g in GENERATORS if g.kind == "diesel")
        cost = diesel * DIESEL_COST_USD_PER_MWH / 1000 + sum(abs(v) for v in bat.values()) * BESS_DEGRADATION_USD_PER_MWH / 1000
        tot_served = sum(served.values())
        tot_dem = sum(forecast.demand_kw.values())
        crit_dem = sum(forecast.demand_kw[ld.id] for ld in CRITICAL_LOADS)
        crit_srv = sum(min(served[ld.id], forecast.demand_kw[ld.id]) for ld in CRITICAL_LOADS)
        renew_used = sum(gen[g.id] for g in GENERATORS if g.kind in ("solar", "wind"))
        return ImpactMetrics(
            emissions_kg_per_h=round(emis, 1),
            diesel_only_baseline_kg_per_h=round(tot_served * EMISSION_FACTORS_KG_PER_KWH["diesel"], 1),
            avoided_kg_per_h=round(tot_served * EMISSION_FACTORS_KG_PER_KWH["diesel"] - emis, 1),
            operating_cost_usd_per_h=round(cost, 2),
            carbon_cost_usd_per_h=round(emis / 1000 * CARBON_PRICE_USD_PER_T, 2),
            served_kw=round(tot_served, 1), demand_kw=round(tot_dem, 1), unmet_kw=round(max(tot_dem - tot_served, 0.0), 1),
            critical_coverage=round(crit_srv / crit_dem, 4) if crit_dem else 1.0,
            shed_kw={ld.id: round(forecast.demand_kw[ld.id] - served[ld.id], 1) for ld in LOADS
                     if forecast.demand_kw[ld.id] - served[ld.id] > 0.05},
            renewable_share=round(renew_used / tot_served, 4) if tot_served else 0.0,
            renewable_curtailed_kw=round(max(forecast.renewable_available_kw - renew_used, 0.0), 1),
            diesel_kw=round(diesel, 1), battery_kw=round(sum(bat.values()), 1),
        )


def build_case(tel: Telemetry, forecast: Forecast, proposal: Any) -> dict[str, Any]:
    """Self-contained verification case: everything the verifier needs, nothing from the planner's internals."""
    return {
        "schema": V.CASE_SCHEMA,
        "constraint_set": CONSTRAINT_SET_VERSION,
        "parameters": parameters_spec(),
        "network": network_spec(),
        "state": {"simulated": True, "available_kw": forecast.available_kw, "demand_kw": forecast.demand_kw,
                  "soc": tel.soc, "line_derate": tel.line_derate, "prev_output_kw": tel.prev_output_kw},
        "proposal": proposal,
    }


def feedback_from(violations: list[dict[str, Any]], proposal: dict[str, Any]) -> tuple[dict[str, float], list[str]]:
    """Turn violation records into explicit planner cuts (upper bounds) plus human-readable notes."""
    cuts: dict[str, float] = {}
    notes: list[str] = []
    for v in violations:
        fam, el, excess = v["family"], v["element"], v.get("excess") or 0.0
        if fam == "LINE_CAPACITY":
            cuts[el] = round(v["limit"] * 0.98, 1)
            notes.append(f"cap |f[{el}]| ≤ {cuts[el]:.0f} kW")
        elif fam in ("GEN_LIMIT", "GEN_RAMP") and v["limit"] is not None and v["observed"] > v["limit"]:
            cuts[el] = round(max(proposal["generation_kw"][el] - excess - 1.0, 0.0), 1)
            notes.append(f"cap P[{el}] ≤ {cuts[el]:.0f} kW ({fam})")
        elif fam == "BESS_POWER" and proposal["battery_kw"].get(el, 0) > 0:
            cuts[el] = round(v["limit"], 1)
            notes.append(f"cap discharge[{el}] ≤ {cuts[el]:.0f} kW")
        else:
            notes.append(f"{v['constraint_id']}: discard proposal and re-plan with full constraint model")
    return cuts, notes


# ─────────────────────────────── Event bus & ledger ────────────────────────────────

class EventBus:
    """In-process publish/subscribe used to stream agent events to the dashboard (SSE)."""

    def __init__(self, maxlen: int = 500) -> None:
        self._log: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._subs: set[asyncio.Queue] = set()
        self.published = 0

    def publish(self, topic: str, summary: str, data: Any = None, **attrs: Any) -> dict[str, Any]:
        self.published += 1
        msg = {"id": self.published, "topic": topic, "ts": utcnow(), "summary": summary, "attrs": attrs, "data": data}
        self._log.append(msg)
        self.broadcast({"type": "event", "data": msg})
        return msg

    def broadcast(self, frame: dict[str, Any]) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=800)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def recent(self, n: int = 80) -> list[dict[str, Any]]:
        return list(self._log)[-n:]

    @property
    def subscribers(self) -> int:
        return len(self._subs)


class AuditLedger:
    """
    Append-only store of audit records. Each record carries record_hash = SHA-256 of its canonical JSON
    (inputs, proposal, versions, every constraint result, impact). Entries are chained:
    chain_hash = SHA-256(prev_chain_hash + record_hash).

    What this establishes: a record has not been altered since it was hashed, and the order of records
    has not been rearranged (if the chain head is kept somewhere trusted). What it does NOT establish:
    that the telemetry was true, that the physical model is adequate, or that the plan is safe outside
    the modeled constraints. Safety evidence comes from re-running the verifier on the record (replay).
    """

    GENESIS = "0" * 64

    def __init__(self, max_records: int = 2000) -> None:
        self.records: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.entries: deque[dict[str, Any]] = deque(maxlen=max_records)
        self.head = self.GENESIS
        self.seq = 0
        self.max_records = max_records

    def append(self, record: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
        record["record_hash"] = V.record_hash(record)
        self.seq += 1
        chain = V.sha256_hex({"prev": self.head, "record_hash": record["record_hash"]})
        entry = {"seq": self.seq, "record_hash": record["record_hash"], "prev_chain_hash": self.head,
                 "chain_hash": chain, "verdict": record["verification"]["verdict"], **meta}
        self.head = chain
        self.entries.append(entry)
        self.records[record["record_hash"]] = record
        while len(self.records) > self.max_records:
            self.records.popitem(last=False)
        return entry

    def verify_chain(self) -> bool:
        es = list(self.entries)
        return all(b["prev_chain_hash"] == a["chain_hash"] and
                   b["chain_hash"] == V.sha256_hex({"prev": a["chain_hash"], "record_hash": b["record_hash"]})
                   for a, b in zip(es, es[1:]))

    def tail(self, n: int = 16) -> list[dict[str, Any]]:
        return list(self.entries)[-n:][::-1]


# ─────────────────────────────── Orchestrator ────────────────────────────────

class SynapseGrid:
    def __init__(self, gemma: Optional[GemmaClient] = None, seed: int = 20261018) -> None:
        self.sim = GridSimulator(seed)
        self.bus = EventBus()
        self.ledger = AuditLedger()
        self.gemma = gemma or GemmaClient()
        self.forecaster = ForecastingAgent()
        self.planner = DispatchPlanningAgent()
        self.carbon = CarbonAnalysisAgent()
        self.priorities = Priorities()
        self.priorities_outcome: Optional[dict[str, Any]] = None
        self.history: deque[dict[str, Any]] = deque(maxlen=120)
        self.last_cycle: Optional[dict[str, Any]] = None
        self.last_scenario: Optional[dict[str, Any]] = None
        self.active: Optional[dict[str, Any]] = None
        self.scheduler_running = True
        self.started = time.time()
        self.metrics = self._zero_metrics()
        self._lock = asyncio.Lock()

    @staticmethod
    def _zero_metrics() -> dict[str, Any]:
        return {"cycles": 0, "certified": 0, "failed": 0, "proposals_verified": 0, "proposals_rejected": 0,
                "recoveries": 0, "unsafe_accepted": 0, "replays_reproduced": 0, "last_recovery_ms": None}

    def reset(self) -> None:
        self.sim.reset()
        self.history.clear()
        self.last_cycle = self.last_scenario = self.active = None
        self.priorities = Priorities()
        self.priorities_outcome = None
        self.metrics = self._zero_metrics()

    def set_fault(self, fault: str, active: bool) -> None:
        self.sim.faults[fault] = active
        self.bus.publish("control.fault", f"{fault} → {'ON' if active else 'OFF'}", {"faults": dict(self.sim.faults)})

    # ---- core: plan → verify → recover --------------------------------
    def _recover(self, cycle_id: str, origin: str, tel: Telemetry, forecast: Forecast,
                 first_proposal: Optional[tuple[str, Any]], trace: list[TraceStep], t_cycle: float) -> dict[str, Any]:
        attempts: list[dict[str, Any]] = []
        cuts: dict[str, float] = {}
        protect = True
        status, reason, shortfall = "FAILED", "retry limit reached without a certified plan", None
        t_first_block: Optional[float] = None

        for n in range(1, MAX_ATTEMPTS + 1):
            t0 = time.perf_counter()
            planner_info: dict[str, Any]
            if n == 1 and first_proposal is not None:
                source, proposal = first_proposal
                planner_info = {"status": "EXTERNAL", "source": source}
                trace.append(TraceStep(step=f"A{n}.propose", agent="external", depends_on=["forecasting"],
                                       started_ms=self._rel(t_cycle, t0), duration_ms=0.0, status="ok",
                                       summary=f"{source} proposal submitted for verification"))
            else:
                source = "optimizer"
                res = self.planner.run(tel, forecast, self.priorities, cuts, protect)
                relaxed = False
                if res.status == "INFEASIBLE" and protect and self.priorities.protected_loads:
                    protect, relaxed = False, True
                    res = self.planner.run(tel, forecast, self.priorities, cuts, protect)
                planner_info = res.model_dump(exclude={"proposal", "solve_ms"}) | {"relaxed_protections": relaxed}
                dur = round((time.perf_counter() - t0) * 1000, 2)
                trace.append(TraceStep(step=f"A{n}.plan", agent="dispatch_planning",
                                       depends_on=["forecasting", "priorities"] + ([f"A{n-1}.verify"] if n > 1 else []),
                                       started_ms=self._rel(t_cycle, t0), duration_ms=dur,
                                       status="ok" if res.status == "OPTIMAL" else "failed",
                                       summary=f"{res.status}" + (f" · objective ${res.objective_usd_per_h:,.0f}/h" if res.objective_usd_per_h is not None else "")
                                       + (" · relaxed Gemma protections" if relaxed else "") + (f" · {len(cuts)} cuts" if cuts else "")))
                self.bus.publish("planner.result", f"A{n} optimizer {res.status}" + (f" · {len(cuts)} verifier cuts" if cuts else ""),
                                 {"attempt": n, **planner_info}, cycle_id=cycle_id)
                if res.status != "OPTIMAL" or res.proposal is None:
                    shortfall = critical_shortfall_kw(tel, forecast.available_kw)
                    reason = (f"no dispatch satisfies the hard constraints; critical-load shortfall {shortfall:,.1f} kW"
                              if res.status == "INFEASIBLE" else f"optimizer error: {res.message}")
                    attempts.append({"attempt": n, "source": source, "planner": planner_info, "proposal": None,
                                     "verification": None, "impact": None, "record_hash": None})
                    break
                proposal = res.proposal

            t1 = time.perf_counter()
            case = build_case(tel, forecast, proposal)
            ver = V.verify(case)
            self.metrics["proposals_verified"] += 1
            impact = self.carbon.run(proposal, forecast) if not any(v["family"] == "INPUT_VALID" for v in ver["violations"]) else None
            record = {
                "schema": AUDIT_SCHEMA, "attempt": n, "proposal_source": source, "simulated": True,
                "versions": {"verifier": V.VERIFIER_VERSION, "optimizer": OPTIMIZER_VERSION,
                             "constraint_set": CONSTRAINT_SET_VERSION, "gemma_prompt": PROMPT_VERSION,
                             "priorities_source": self.priorities.source},
                "priorities": self.priorities.model_dump(),
                "case": case,
                "planner": planner_info,
                "verification": {k: ver[k] for k in ("verifier", "constraint_set", "verdict", "checks", "violations",
                                                     "computed", "checks_digest")},
                "impact": impact.model_dump() if impact else None,
            }
            entry = self.ledger.append(record, {"cycle_id": cycle_id, "attempt": n, "origin": origin,
                                                "source": source, "ts": utcnow()})
            vdur = round((time.perf_counter() - t1) * 1000, 2)
            nviol = len(ver["violations"])
            trace.append(TraceStep(step=f"A{n}.verify", agent="verification_recovery", depends_on=[f"A{n}.plan" if source == "optimizer" else f"A{n}.propose"],
                                   started_ms=self._rel(t_cycle, t1), duration_ms=vdur,
                                   status="ok" if ver["verdict"] == "APPROVED" else "blocked",
                                   summary=f"{ver['verdict']} · {len(ver['checks']) - nviol}/{len(ver['checks'])} checks pass"
                                   + (f" · {nviol} violation(s)" if nviol else "")))
            self.bus.publish("verifier.verdict",
                             f"A{n} {ver['verdict']} ({source}) · " + ("; ".join(v["message"] for v in ver["violations"][:2]) or
                                                                       f"{len(ver['checks'])} checks pass") + f" · record {entry['record_hash'][:10]}…",
                             {"attempt": n, "verdict": ver["verdict"], "violations": ver["violations"],
                              "record_hash": entry["record_hash"], "chain_hash": entry["chain_hash"]},
                             cycle_id=cycle_id, verdict=ver["verdict"])
            att = {"attempt": n, "source": source, "planner": planner_info, "proposal": proposal,
                   "verification": record["verification"], "impact": record["impact"],
                   "record_hash": entry["record_hash"], "chain_hash": entry["chain_hash"]}
            attempts.append(att)

            if ver["verdict"] == "APPROVED":
                rp = V.replay(record)  # independent re-check of the stored record before we act on it
                if rp["reproduced"] and rp["verdict"] == "APPROVED":
                    self.metrics["replays_reproduced"] += 1
                    status, reason = "CERTIFIED", "all modeled constraints satisfied"
                else:
                    self.metrics["unsafe_accepted"] += 1
                    status, reason = "FAILED", "certified record failed independent replay"
                att["replay"] = rp
                break

            self.metrics["proposals_rejected"] += 1
            t_first_block = t_first_block or t0
            new_cuts, notes = feedback_from(ver["violations"], proposal)
            for k, v in new_cuts.items():
                cuts[k] = min(v, cuts.get(k, v))
            att["feedback"] = {"cuts": dict(cuts), "notes": notes}
            self.bus.publish("verifier.feedback", f"counter-example A{n} → planner: " + "; ".join(notes[:3]),
                             {"attempt": n, "cuts": cuts, "notes": notes, "record_hash": entry["record_hash"]},
                             cycle_id=cycle_id)

        recovery_ms = None
        if t_first_block is not None and status == "CERTIFIED":
            recovery_ms = round((time.perf_counter() - t_first_block) * 1000, 2)
        return {"status": status, "reason": reason, "attempts": attempts, "critical_shortfall_kw": shortfall,
                "recovery_ms": recovery_ms}

    @staticmethod
    def _rel(t_cycle: float, t: float) -> float:
        return round((t - t_cycle) * 1000, 2)

    async def run_cycle(self, overrides: Optional[TelemetryOverrides] = None, tel: Optional[Telemetry] = None,
                        inject_unsafe: Optional[bool] = None, external_proposal: Optional[dict[str, Any]] = None,
                        origin: str = "scheduler", commit: bool = True) -> dict[str, Any]:
        async with self._lock:
            return self._run_cycle_locked(overrides, tel, inject_unsafe, external_proposal, origin, commit)

    def _run_cycle_locked(self, overrides: Optional[TelemetryOverrides], tel: Optional[Telemetry],
                          inject_unsafe: Optional[bool], external_proposal: Optional[dict[str, Any]],
                          origin: str, commit: bool) -> dict[str, Any]:
        cycle_id = f"cyc-{self.sim.tick:05d}-{uuid.uuid4().hex[:4]}"
        t_cycle = time.perf_counter()
        trace: list[TraceStep] = []
        tel = tel or self.sim.sample(overrides)
        inject = self.sim.faults["unsafe_proposal"] if inject_unsafe is None else inject_unsafe

        t0 = time.perf_counter()
        forecast = self.forecaster.run(tel)
        trace.append(TraceStep(step="forecast", agent="forecasting", depends_on=["telemetry"], started_ms=self._rel(t_cycle, t0),
                               duration_ms=round((time.perf_counter() - t0) * 1000, 3), status="ok",
                               summary=f"renewables {forecast.renewable_available_kw:,.0f} kW · demand {forecast.total_demand_kw:,.0f} kW"))
        trace.append(TraceStep(step="priorities", agent="priorities", depends_on=[], started_ms=0.0, duration_ms=0.0,
                               status="ok" if self.priorities.source.startswith("gemma") else "fallback",
                               summary=f"source: {self.priorities.source}"
                               + (f" · protected {', '.join(self.priorities.protected_loads)}" if self.priorities.protected_loads else "")))
        self.bus.publish("forecast", f"tick {tel.tick} · PV {forecast.available_kw['PV-HILL']:,.0f} kW · wind "
                                     f"{forecast.available_kw['WT-RIDGE']:,.0f} kW · demand {forecast.total_demand_kw:,.0f} kW"
                                     + (f" · {', '.join(a.split(':')[0] for a in forecast.alerts)}" if forecast.alerts else ""),
                         {"telemetry": tel.model_dump(), "forecast": forecast.model_dump()}, cycle_id=cycle_id)

        first: Optional[tuple[str, Any]] = None
        if external_proposal is not None:
            first = ("external", external_proposal)
        elif inject:
            first = ("injected_unsafe", unsafe_proposal(tel, forecast.available_kw))
        outcome = self._recover(cycle_id, origin, tel, forecast, first, trace, t_cycle)

        final = outcome["attempts"][-1] if outcome["attempts"] else None
        if outcome["status"] == "CERTIFIED" and final:
            t0 = time.perf_counter()
            trace.append(TraceStep(step="carbon", agent="carbon_analysis", depends_on=[f"A{final['attempt']}.verify"],
                                   started_ms=self._rel(t_cycle, t0), duration_ms=0.05, status="ok",
                                   summary=f"{final['impact']['emissions_kg_per_h']:,.0f} kgCO₂/h · ${final['impact']['operating_cost_usd_per_h']:,.0f}/h · "
                                           f"critical {final['impact']['critical_coverage']:.0%}"))
            self.bus.publish("carbon.report", f"{final['impact']['emissions_kg_per_h']:,.0f} kgCO₂/h (diesel-only baseline "
                                              f"{final['impact']['diesel_only_baseline_kg_per_h']:,.0f}) · ${final['impact']['operating_cost_usd_per_h']:,.0f}/h · "
                                              f"unmet {final['impact']['unmet_kw']:,.0f} kW", final["impact"], cycle_id=cycle_id)
        else:
            self.bus.publish("recovery.failed", f"RECOVERY FAILED · {outcome['reason']}",
                             {"reason": outcome["reason"], "critical_shortfall_kw": outcome["critical_shortfall_kw"]},
                             cycle_id=cycle_id)

        rejected = [a for a in outcome["attempts"] if a["verification"] and a["verification"]["verdict"] == "BLOCKED"]
        cycle = {
            "cycle_id": cycle_id, "origin": origin, "tick": tel.tick, "started_at": utcnow(), "simulated": True,
            "committed": False, "telemetry": tel.model_dump(), "forecast": forecast.model_dump(),
            "priorities": self.priorities.model_dump(), "status": outcome["status"], "reason": outcome["reason"],
            "critical_shortfall_kw": outcome["critical_shortfall_kw"], "recovery_ms": outcome["recovery_ms"],
            "attempts": outcome["attempts"], "rejected": len(rejected),
            "trace": [s.model_dump() for s in trace],
            "total_ms": round((time.perf_counter() - t_cycle) * 1000, 2),
        }
        self.metrics["cycles"] += 1
        if outcome["status"] == "CERTIFIED":
            self.metrics["certified"] += 1
            if rejected:
                self.metrics["recoveries"] += 1
                self.metrics["last_recovery_ms"] = outcome["recovery_ms"]
        else:
            self.metrics["failed"] += 1
        if commit and outcome["status"] == "CERTIFIED" and final:
            cycle["committed"] = True
            self.sim.commit(final["verification"]["computed"]["soc_next"], final["proposal"]["generation_kw"])
            self.active = {"telemetry": tel, "forecast": forecast, "attempt": final}
            imp = final["impact"]
            self.history.append({"tick": tel.tick, "pv": final["proposal"]["generation_kw"]["PV-HILL"],
                                 "wind": final["proposal"]["generation_kw"]["WT-RIDGE"], "diesel": imp["diesel_kw"],
                                 "battery": imp["battery_kw"], "unmet": imp["unmet_kw"], "demand": imp["demand_kw"],
                                 "emissions": imp["emissions_kg_per_h"], "cost": imp["operating_cost_usd_per_h"],
                                 "rejected": len(rejected)})
        elif commit:
            self.sim.tick += 1
        self.last_cycle = cycle
        self.bus.broadcast({"type": "snapshot", "data": self.snapshot()})
        return cycle

    # ---- Gemma priorities ------------------------------------------------
    async def interpret_note(self, note: str) -> dict[str, Any]:
        tel = self.active["telemetry"] if self.active else SCENARIO_BASELINE
        context = {"faults": {k: v for k, v in self.sim.faults.items() if k != "unsafe_proposal"},
                   "irradiance_wm2": tel.irradiance_wm2, "battery_soc": tel.soc, "diesel_available": tel.diesel_available}
        outcome: GemmaOutcome = await self.gemma.interpret_priorities(note, context)
        if outcome.status == "inference" and outcome.priorities is not None:
            base = Priorities().load_priority
            self.priorities = Priorities(load_priority={**base, **outcome.priorities.load_priority},
                                         protected_loads=outcome.priorities.protected_loads,
                                         source=f"gemma:{outcome.model}")
        else:
            self.priorities = Priorities(source=f"default (Gemma {outcome.status})")
        self.priorities_outcome = outcome.model_dump()
        self.bus.publish("gemma.priorities",
                         f"Gemma priorities: {outcome.status}" + (f" ({outcome.latency_ms:.0f} ms)" if outcome.latency_ms else "")
                         + (f" · protect {', '.join(outcome.priorities.protected_loads)}" if outcome.priorities and outcome.priorities.protected_loads else "")
                         + (f" · {outcome.errors[0][:90]}" if outcome.errors and outcome.status != "inference" else ""),
                         self.priorities_outcome)
        return {"outcome": self.priorities_outcome, "applied_priorities": self.priorities.model_dump()}

    async def briefing(self) -> dict[str, Any]:
        sc = self.last_scenario or {}
        cyc = self.last_cycle or {}
        facts = {"scenario": {k: sc.get(k) for k in ("before", "rejected", "after", "status")} if sc else None,
                 "status": cyc.get("status"), "reason": cyc.get("reason"),
                 "violations": [v["message"] for a in cyc.get("attempts", []) if a.get("verification")
                                for v in a["verification"]["violations"]][:6]}
        out = await self.gemma.briefing(facts)
        self.bus.publish("gemma.briefing", f"Gemma briefing: {out.status}", out.model_dump())
        return out.model_dump()

    # ---- deterministic scenarios ------------------------------------------
    async def run_incident_scenario(self, inject_unsafe: bool = True, note: Optional[str] = None) -> dict[str, Any]:
        """Fixed inputs → identical record hashes every run. Baseline → 75 % solar loss → (unsafe) → recovery."""
        self.scheduler_running = False
        self.sim.reset()
        self.bus.publish("scenario", "incident scenario: deterministic baseline → sudden 75 % solar loss"
                                     + (" → injected unsafe proposal" if inject_unsafe else ""))
        # Gemma reads the note concurrently with the baseline dispatch (which does not depend on it);
        # without a note, the operator's current priorities (default or previously interpreted) are used.
        gemma_task = asyncio.create_task(self.interpret_note(note)) if note else None
        baseline = await self.run_cycle(tel=SCENARIO_BASELINE, inject_unsafe=False, origin="scenario:baseline")
        if gemma_task:
            await gemma_task  # recovery planning depends on the operator priorities
        base_prop = baseline["attempts"][-1]["proposal"]
        incident_tel = SCENARIO_BASELINE.model_copy(update={
            "tick": 1, "irradiance_wm2": round(SCENARIO_BASELINE.irradiance_wm2 * (1 - SOLAR_LOSS_FRACTION), 1),
            "prev_output_kw": {"DG-HARB": base_prop["generation_kw"]["DG-HARB"]}})
        self.sim.faults["solar_loss"] = True
        incident = await self.run_cycle(tel=incident_tel, inject_unsafe=inject_unsafe, origin="scenario:solar-loss")
        rej = next((a for a in incident["attempts"] if a["verification"] and a["verification"]["verdict"] == "BLOCKED"), None)
        fin = incident["attempts"][-1]
        # "do nothing" counterfactual: keep the pre-incident dispatch with PV clipped to what is now available
        result = {
            "status": incident["status"], "reason": incident["reason"],
            "before": baseline["attempts"][-1]["impact"],
            "rejected": {"impact": rej["impact"], "violations": rej["verification"]["violations"],
                         "record_hash": rej["record_hash"]} if rej else None,
            "after": fin["impact"] if incident["status"] == "CERTIFIED" else None,
            "after_record_hash": fin["record_hash"],
            "pv_lost_kw": round(baseline["forecast"]["available_kw"]["PV-HILL"] - incident["forecast"]["available_kw"]["PV-HILL"], 1),
            "attempts": len(incident["attempts"]), "recovery_ms": incident["recovery_ms"],
            "baseline_cycle": baseline["cycle_id"], "incident_cycle": incident["cycle_id"],
            "priorities_source": self.priorities.source,
        }
        self.last_scenario = result
        self.bus.broadcast({"type": "snapshot", "data": self.snapshot()})
        return result

    async def run_infeasible_scenario(self) -> dict[str, Any]:
        """Night-time, no wind, depleted batteries and a diesel trip: critical loads cannot be met → honest failure."""
        tel = SCENARIO_BASELINE.model_copy(update={"irradiance_wm2": 40.0, "wind_speed_ms": 2.0,
                                                   "soc": {"BESS-CLN": 0.11, "BESS-PRT": 0.11},
                                                   "diesel_available": False, "prev_output_kw": {"DG-HARB": 0.0}})
        self.scheduler_running = False
        self.bus.publish("scenario", "stress test: night + no wind + batteries at 11 % + diesel trip (what-if, not committed)")
        return await self.run_cycle(tel=tel, inject_unsafe=False, origin="scenario:infeasible", commit=False)

    # ---- dashboard snapshot ------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        act = self.active
        imp = act["attempt"]["impact"] if act else None
        lc = self.last_cycle
        status = ("FAILED" if lc and lc["status"] == "FAILED" else
                  "RECOVERED" if lc and lc["rejected"] else
                  "CONTINGENCY" if any(v for k, v in self.sim.faults.items() if k != "unsafe_proposal") else "NOMINAL")
        return {
            "simulated": True,
            "server_time": utcnow(),
            "tick": self.sim.tick,
            "sim_clock": f"{11 + (self.sim.tick * 5) // 60 % 24:02d}:{(self.sim.tick * 5) % 60:02d}",
            "status": status,
            "faults": dict(self.sim.faults),
            "scheduler_running": self.scheduler_running,
            "gemma": self.gemma.info,
            "priorities": self.priorities.model_dump(),
            "priorities_outcome": self.priorities_outcome,
            "kpis": imp,
            "metrics": self.metrics,
            "grid": self._grid_view(),
            "history": list(self.history),
            "last_cycle": lc,
            "last_scenario": self.last_scenario,
            "ledger": {"head": self.ledger.head, "length": self.ledger.seq, "chain_valid": self.ledger.verify_chain(),
                       "entries": self.ledger.tail()},
            "events": {"published": self.bus.published, "subscribers": self.bus.subscribers},
        }

    def _grid_view(self) -> Optional[dict[str, Any]]:
        if not self.active:
            return None
        tel: Telemetry = self.active["telemetry"]
        fc: Forecast = self.active["forecast"]
        att = self.active["attempt"]
        prop, comp = att["proposal"], att["verification"]["computed"]
        intercepted = set()
        if self.last_cycle:
            for a in self.last_cycle["attempts"]:
                for v in (a["verification"] or {}).get("violations", []):
                    intercepted.add(v["element"])

        def status_of(u: float) -> str:
            return "critical" if u >= 0.97 else "warning" if u >= 0.8 else "stable"

        lines = []
        for ln in LINES:
            lim = ln.cap_kw * tel.line_derate[ln.id]
            f = comp["line_flow_kw"].get(ln.id, 0.0)
            u = abs(f) / lim if lim else 0.0
            lines.append({"id": ln.id, "name": ln.name, "from": ln.frm, "to": ln.to, "flow_kw": round(f, 1),
                          "limit_kw": lim, "utilization": round(u, 4),
                          "status": "tripped" if lim == 0 else status_of(u), "intercepted": ln.id in intercepted})
        assets = []
        for a in ASSETS:
            if a.kind == "battery":
                p = prop["battery_kw"][a.id]
                soc = comp["soc_next"][a.id]
                assets.append({"id": a.id, "name": a.name, "kind": a.kind, "bus": a.bus, "output_kw": p,
                               "cap_kw": a.cap_kw, "soc": round(tel.soc[a.id], 4), "soc_next": round(soc, 4),
                               "status": "critical" if soc < 0.15 else "warning" if soc < 0.3 or abs(p) / a.cap_kw > 0.9 else "stable",
                               "intercepted": a.id in intercepted})
            else:
                p, av = prop["generation_kw"][a.id], fc.available_kw[a.id]
                st = ("critical" if a.kind == "diesel" and av == 0 else
                      "warning" if (a.kind == "diesel" and p > 0.6 * a.cap_kw) or (a.kind != "diesel" and av < 0.4 * a.cap_kw) else "stable")
                assets.append({"id": a.id, "name": a.name, "kind": a.kind, "bus": a.bus, "output_kw": p,
                               "available_kw": av, "cap_kw": a.cap_kw, "status": st, "intercepted": a.id in intercepted})
        loads = []
        for ld in LOADS:
            dem, srv = fc.demand_kw[ld.id], prop["served_kw"][ld.id]
            shed = round(dem - srv, 1)
            loads.append({"id": ld.id, "name": ld.name, "bus": ld.bus, "critical": ld.critical,
                          "protected": ld.id in self.priorities.protected_loads,
                          "priority": self.priorities.load_priority.get(ld.id), "demand_kw": dem, "served_kw": srv,
                          "shed_kw": shed if shed > 0.05 else 0.0,
                          "status": ("critical" if ld.critical and shed > 0.5 else "warning" if shed > 0.05 else "stable"),
                          "intercepted": ld.id in intercepted})
        return {"lines": lines, "assets": assets, "loads": loads, "bus_injection_kw": comp["bus_injection_kw"],
                "irradiance_wm2": tel.irradiance_wm2, "wind_speed_ms": tel.wind_speed_ms}
