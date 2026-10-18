"""
SynapseGrid — AI-assisted renewable dispatch simulator for critical community loads.

FastAPI entrypoint: dashboard, REST API, SSE event stream, and the autonomous (simulated) dispatch loop.
All telemetry is synthetic; nothing here controls real electrical equipment.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

import verifier as V
from agents import AGENT_MANIFEST, SynapseGrid
from evaluation import run_evaluation, to_markdown
from grid import TelemetryOverrides, topology_for_ui
from optimizer import OPTIMIZER_VERSION, Priorities

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("synapsegrid")

BASE_DIR = Path(__file__).resolve().parent
TICK_SECONDS = float(os.getenv("TICK_SECONDS", "3"))
APP_VERSION = "2.0.0"

grid = SynapseGrid()


async def scheduler() -> None:
    while True:
        try:
            if grid.scheduler_running:
                await grid.run_cycle(origin="scheduler")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("dispatch cycle failed")
        await asyncio.sleep(TICK_SECONDS)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    log.info("SynapseGrid %s · optimizer %s · verifier %s · gemma provider=%s model=%s", APP_VERSION,
             OPTIMIZER_VERSION, V.VERIFIER_VERSION, grid.gemma.provider, grid.gemma.model)
    task = asyncio.create_task(scheduler())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


app = FastAPI(title="SynapseGrid", version=APP_VERSION, lifespan=lifespan,
              description="Verified dispatch for a simulated island-community microgrid: OR-Tools optimizer, "
                          "independent verifier, bounded recovery, Gemma-extracted operator priorities.")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "static")


# ─────────────────────────────── Request models ────────────────────────────────

class EvaluateRequest(BaseModel):
    telemetry: TelemetryOverrides = Field(default_factory=TelemetryOverrides)
    inject_unsafe: Optional[bool] = Field(default=None, description="Submit the deterministic unsafe proposal first")
    proposal: Optional[Any] = Field(default=None, description="Untrusted external proposal to verify first "
                                                              "(generation_kw / battery_kw / served_kw)")
    commit: bool = Field(default=False, description="Apply a certified dispatch to the simulated grid")


class FaultRequest(BaseModel):
    fault: Literal["solar_loss", "diesel_outage", "unsafe_proposal", "all_clear"]
    active: bool = True


class IncidentRequest(BaseModel):
    inject_unsafe: bool = True
    operator_note: Optional[str] = Field(default=None, max_length=2000)


class NoteRequest(BaseModel):
    note: str = Field(min_length=3, max_length=2000)


class SchedulerRequest(BaseModel):
    running: bool


class EvaluationRequest(BaseModel):
    n: int = Field(default=200, ge=10, le=1000)
    seed: int = Field(default=7, ge=0, le=2**31)


# ─────────────────────────────── Routes ────────────────────────────────

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", {"version": APP_VERSION, "tick_seconds": TICK_SECONDS,
                                                               "topology": topology_for_ui()})


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "version": APP_VERSION, "uptime_s": round(time.time() - grid.started, 1),
            "tick": grid.sim.tick, "scheduler_running": grid.scheduler_running,
            "optimizer": OPTIMIZER_VERSION, "verifier": V.VERIFIER_VERSION,
            "gemma": {"provider": grid.gemma.provider, "model": grid.gemma.model, "configured": grid.gemma.configured},
            "ledger_chain_valid": grid.ledger.verify_chain()}


@app.get("/api/state")
async def state() -> dict[str, Any]:
    return grid.snapshot()


@app.get("/api/agent-network")
async def agent_network() -> dict[str, Any]:
    return AGENT_MANIFEST


@app.post("/api/evaluate-load-shift")
async def evaluate_load_shift(body: EvaluateRequest) -> dict[str, Any]:
    """Run forecast → plan → independent verification → bounded recovery on (optionally overridden) telemetry."""
    cycle = await grid.run_cycle(overrides=body.telemetry, inject_unsafe=body.inject_unsafe,
                                 external_proposal=body.proposal, origin="api", commit=body.commit)
    final = cycle["attempts"][-1] if cycle["attempts"] else None
    return {
        "status": cycle["status"], "reason": cycle["reason"], "cycle_id": cycle["cycle_id"],
        "committed": cycle["committed"], "attempts": len(cycle["attempts"]),
        "rejected": [{"attempt": a["attempt"], "source": a["source"], "violations": a["verification"]["violations"],
                      "record_hash": a["record_hash"]} for a in cycle["attempts"]
                     if a["verification"] and a["verification"]["verdict"] == "BLOCKED"],
        "certificate": {"record_hash": final["record_hash"], "chain_hash": final["chain_hash"],
                        "checks": len(final["verification"]["checks"]),
                        "scope": "verified constraint certificate within the modeled assumptions (not a formal proof)"}
        if cycle["status"] == "CERTIFIED" and final else None,
        "critical_shortfall_kw": cycle["critical_shortfall_kw"],
        "impact": final["impact"] if final and cycle["status"] == "CERTIFIED" else None,
        "cycle": cycle,
    }


@app.post("/api/inject-grid-fault")
async def inject_grid_fault(body: FaultRequest) -> dict[str, Any]:
    if body.fault == "all_clear":
        for f in grid.sim.FAULTS:
            grid.set_fault(f, False)
    else:
        grid.set_fault(body.fault, body.active)
    cycle = await grid.run_cycle(origin="fault-injection")
    return {"faults": grid.sim.faults, "status": cycle["status"], "attempts": len(cycle["attempts"]),
            "rejected": cycle["rejected"], "cycle_id": cycle["cycle_id"]}


@app.post("/api/scenarios/incident")
async def incident(body: IncidentRequest) -> dict[str, Any]:
    """Deterministic: fixed baseline → 75 % solar loss → (unsafe proposal rejected) → certified recovery."""
    return await grid.run_incident_scenario(inject_unsafe=body.inject_unsafe, note=body.operator_note)


@app.post("/api/scenarios/infeasible")
async def infeasible() -> dict[str, Any]:
    cycle = await grid.run_infeasible_scenario()
    return {"status": cycle["status"], "reason": cycle["reason"], "critical_shortfall_kw": cycle["critical_shortfall_kw"],
            "cycle_id": cycle["cycle_id"]}


@app.post("/api/gemma/priorities")
async def gemma_priorities(body: NoteRequest) -> dict[str, Any]:
    return await grid.interpret_note(body.note)


@app.post("/api/gemma/briefing")
async def gemma_briefing() -> dict[str, Any]:
    return await grid.briefing()


@app.post("/api/priorities/reset")
async def priorities_reset() -> dict[str, Any]:
    grid.priorities = Priorities()
    grid.priorities_outcome = None
    grid.bus.publish("priorities", "operator priorities reset to declared defaults")
    return grid.priorities.model_dump()


@app.get("/api/ledger")
async def ledger(limit: int = 50) -> dict[str, Any]:
    return {"head": grid.ledger.head, "length": grid.ledger.seq, "chain_valid": grid.ledger.verify_chain(),
            "entries": grid.ledger.tail(max(1, min(limit, 500)))}


@app.get("/api/audit/{record_hash}")
async def audit_record(record_hash: str) -> dict[str, Any]:
    rec = grid.ledger.records.get(record_hash)
    if rec is None:
        raise HTTPException(404, "unknown or evicted record hash")
    return rec


@app.get("/api/replay/{record_hash}")
async def replay_stored(record_hash: str) -> dict[str, Any]:
    rec = grid.ledger.records.get(record_hash)
    if rec is None:
        raise HTTPException(404, "unknown or evicted record hash")
    # Round-trip through JSON so the replay sees exactly what an external party would download.
    return V.replay(json.loads(json.dumps(rec)))


@app.post("/api/replay")
async def replay_uploaded(record: dict[str, Any]) -> dict[str, Any]:
    return V.replay(record)


@app.post("/api/evaluation")
async def evaluation(body: EvaluationRequest) -> dict[str, Any]:
    result = await asyncio.to_thread(run_evaluation, body.n, body.seed)
    grid.bus.publish("evaluation", f"randomized evaluation n={body.n} seed={body.seed}: rejection accuracy "
                                   f"{result['rejection_accuracy']['accuracy']:.2%}, unsafe accepted "
                                   f"{result['recovery']['unsafe_accepted']}", None)
    return {**result, "markdown": to_markdown(result)}


@app.post("/api/scheduler")
async def set_scheduler(body: SchedulerRequest) -> dict[str, Any]:
    grid.scheduler_running = body.running
    grid.bus.publish("control", f"simulated dispatch loop {'resumed' if body.running else 'paused'}")
    return {"scheduler_running": grid.scheduler_running}


@app.post("/api/reset")
async def reset() -> dict[str, Any]:
    grid.reset()
    grid.bus.publish("control", "simulator reset to seed (audit ledger is append-only and kept)")
    await grid.run_cycle(origin="reset")
    return {"status": "reset", "tick": grid.sim.tick}


@app.get("/api/stream", include_in_schema=False)
async def stream(request: Request) -> StreamingResponse:
    queue = grid.bus.subscribe()

    async def events() -> AsyncIterator[str]:
        try:
            yield "retry: 2000\n\n"
            yield f"data: {json.dumps({'type': 'snapshot', 'data': grid.snapshot()}, default=str)}\n\n"
            for msg in grid.bus.recent(60):
                yield f"data: {json.dumps({'type': 'event', 'data': msg, 'replay': True}, default=str)}\n\n"
            while not await request.is_disconnected():
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {json.dumps(frame, default=str)}\n\n"
        finally:
            grid.bus.unsubscribe(queue)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
