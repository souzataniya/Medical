"""
SynapseGrid — Deterministic Multi-Agent Grid Load Balancer & Energy Carbon-Router.

FastAPI entrypoint: serves the mission-control dashboard, the agent-network API,
and a Server-Sent-Events stream that mirrors the internal Pub/Sub bus.
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

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from agents import AGENT_NETWORK_MANIFEST, LINES, NODES, SynapseGridNetwork, TelemetryInput

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("synapsegrid")

BASE_DIR = Path(__file__).resolve().parent
TICK_SECONDS = float(os.getenv("TICK_SECONDS", "2.5"))
APP_VERSION = "1.0.0"

network = SynapseGridNetwork()


async def scheduler() -> None:
    """Autonomous dispatch loop: one verified cycle every TICK_SECONDS."""
    while True:
        try:
            if network.scheduler_running:
                await network.run_cycle(origin="scheduler")
        except asyncio.CancelledError:
            raise
        except Exception:  # never let one bad cycle kill the control loop
            log.exception("dispatch cycle failed")
        await asyncio.sleep(TICK_SECONDS)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    log.info("SynapseGrid starting · Gemma provider=%s model=%s", network.gemma.provider, network.gemma.info["model"])
    task = asyncio.create_task(scheduler())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await network.aclose()


app = FastAPI(
    title="SynapseGrid",
    version=APP_VERSION,
    description="Gemma-verified multi-agent load balancing and carbon routing for the JAPAC interconnect.",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "static")


# ─────────────────────────────── Request models ────────────────────────────────

class EvaluateRequest(BaseModel):
    telemetry: TelemetryInput = Field(default_factory=TelemetryInput,
                                      description="Telemetry overrides; anything omitted is sampled from the live grid.")
    unsafe_overload: Optional[bool] = Field(default=None, description="Force the speculative (unsafe) planner on/off.")
    commit: bool = Field(default=True, description="Commit the certified dispatch to the live grid (false = what-if).")

    model_config = {
        "json_schema_extra": {
            "examples": [{
                "telemetry": {"irradiance_wm2": {"SL-NTA": 140, "SL-QLD": 160}, "demand_mw": {"LD-TKY": 2450},
                              "line_derate": {"L-CHG-SEL": 0.0}, "ambient_c": 37.5},
                "unsafe_overload": False,
                "commit": False,
            }]
        }
    }


class FaultRequest(BaseModel):
    fault: Literal["cloud_cover", "grid_surge", "unsafe_overload", "all_clear"]
    active: bool = True


class SchedulerRequest(BaseModel):
    running: bool


# ─────────────────────────────── Routes ────────────────────────────────

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", {
        "version": APP_VERSION,
        "tick_seconds": TICK_SECONDS,
        "topology": {"nodes": [n.__dict__ for n in NODES], "lines": [line.__dict__ for line in LINES]},
    })


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "version": APP_VERSION,
        "uptime_s": round(time.time() - network.started_at, 1),
        "tick": network.state.tick,
        "scheduler_running": network.scheduler_running,
        "gemma": network.gemma.info,
        "ledger_chain_valid": network.ledger.verify_chain(),
    }


@app.get("/api/state")
async def state() -> dict[str, Any]:
    return network.snapshot()


@app.get("/api/agent-network")
async def agent_network() -> dict[str, Any]:
    return AGENT_NETWORK_MANIFEST


@app.get("/api/ledger")
async def ledger(limit: int = 50) -> dict[str, Any]:
    return {"head": network.ledger.head, "length": network.ledger.seq,
            "chain_valid": network.ledger.verify_chain(), "entries": network.ledger.tail(max(1, min(limit, 200)))}


@app.post("/api/evaluate-load-shift")
async def evaluate_load_shift(body: EvaluateRequest) -> dict[str, Any]:
    """
    Run the full agent DAG on (optionally overridden) telemetry:
    forecast → load-matching → Gemma safety verifier (re-plan loop) → carbon optimisation.
    Returns the proof certificate, or the counter-example trace(s) and the corrected plan.
    """
    cycle = await network.run_cycle(overrides=body.telemetry, unsafe=body.unsafe_overload,
                                    origin="api", commit=body.commit, await_audit=True)
    final_attempt = cycle["attempts"][-1]["verification"]
    return {
        "verdict": cycle["final"]["verdict"],
        "cycle_id": cycle["cycle_id"],
        "committed": cycle["committed"],
        "attempts": cycle["final"]["attempts"],
        "proof_certificate": final_attempt["certificate"],
        "counterexamples": [a["verification"]["counterexample"] for a in cycle["attempts"]
                            if a["verification"]["counterexample"]],
        "gemma_audits": [a["verification"].get("gemma") for a in cycle["attempts"]],
        "carbon": cycle["carbon"],
        "timings_ms": cycle["timings_ms"],
        "cycle": cycle,
    }


@app.post("/api/inject-grid-fault")
async def inject_grid_fault(body: FaultRequest) -> dict[str, Any]:
    """Toggle a contingency (cloud cover, heatwave surge + HVDC trip, unsafe planner) and dispatch immediately."""
    if body.fault == "all_clear":
        for fault in network.state.FAULTS:
            network.set_fault(fault, False)
    else:
        network.set_fault(body.fault, body.active)
    cycle = await network.run_cycle(origin="fault-injection")
    return {"faults": network.state.faults, "verdict": cycle["final"]["verdict"],
            "attempts": cycle["final"]["attempts"], "intercepted": cycle["intercepted"], "cycle_id": cycle["cycle_id"]}


@app.post("/api/scheduler")
async def set_scheduler(body: SchedulerRequest) -> dict[str, Any]:
    network.scheduler_running = body.running
    network.bus.publish("control.scheduler", {"running": body.running},
                        summary=f"autonomous dispatch {'resumed' if body.running else 'paused'}")
    return {"scheduler_running": network.scheduler_running}


@app.post("/api/reset")
async def reset() -> dict[str, Any]:
    network.reset()
    network.bus.publish("control.reset", {}, summary="grid state reset to seed")
    await network.run_cycle(origin="reset")
    return {"status": "reset", "tick": network.state.tick}


@app.get("/api/stream", include_in_schema=False)
async def stream(request: Request) -> StreamingResponse:
    """SSE mirror of the Pub/Sub bus plus a full snapshot after every committed cycle."""
    queue = network.bus.subscribe()

    async def events() -> AsyncIterator[str]:
        try:
            yield "retry: 2000\n\n"
            yield f"data: {json.dumps({'type': 'cycle', 'data': network.snapshot()}, default=str)}\n\n"
            for msg in network.bus.recent(60):
                yield f"data: {json.dumps({'type': 'bus', 'data': msg, 'replay': True}, default=str)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {json.dumps(frame, default=str)}\n\n"
        finally:
            network.bus.unsubscribe(queue)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")), reload=False)
