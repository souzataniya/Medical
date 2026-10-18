"""
SynapseGrid agent network.

One dispatch cycle is a small, deterministic DAG (NeuroSAN-style agent network):

    Forecasting ──► Load-Matching ──► Gemma Safety Verifier ──► Carbon-Optimization
                          ▲                     │
                          └──── counter-example ┘   (re-plan loop, ≤ MAX_ATTEMPTS)

Design principle — "Gemma reasons, math decides":
  * The verifier compiles every proposed dispatch into explicit physical
    invariants (line thermal limits, generator envelopes and ramp rates, BESS
    power / state-of-charge floors, IEEE C57.91 transformer hot-spot, frequency
    band) and evaluates them deterministically. The same plan + telemetry always
    yields the same verdict and the same SHA-256 hash.
  * Gemma audits each counter-example / certificate and writes the diagnosis and
    re-planning directive that operators read. It never gets the final say on
    whether a transformer is overheating.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import random
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Optional

import httpx
from pydantic import BaseModel, Field, field_validator

log = logging.getLogger("synapsegrid")

# ─────────────────────────────── Constants ────────────────────────────────

DISPATCH_INTERVAL_H = 0.25      # verification horizon: one 15-minute dispatch interval
SIM_STEP_H = 1 / 12             # each committed cycle advances the simulated grid 5 minutes
BESS_SOC_MIN = 0.10             # battery state-of-charge floor
HOTSPOT_LIMIT_C = 120.0         # IEEE C57.91 planned loading beyond nameplate
XFMR_TOP_OIL_RISE = 55.0        # rated top-oil rise over ambient (°C)
XFMR_HOTSPOT_GRADIENT = 25.0    # rated hot-spot over top-oil gradient (°C)
XFMR_LOSS_RATIO = 8.0           # load loss / no-load loss
XFMR_N = 0.8                    # oil exponent (ONAF)
XFMR_M = 0.8                    # winding exponent (ONAF)
FREQ_BAND_HZ = 0.10             # operational band before under-frequency load shedding
DROOP = 0.05                    # 5 % governor droop
MAX_ATTEMPTS = 5
UNSAFE_INFLATION = 1.38         # how hard the injected "speculative" planner over-commits
SOC_REVERSION = 0.08            # background charging / arbitrage pulling SoC to target
SOC_TARGET = 0.72

AgentName = Literal["forecasting", "load_matching", "gemma_verifier", "carbon_optimization"]


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _rounded(obj: Any) -> Any:
    if isinstance(obj, float):
        return round(obj, 4)
    if isinstance(obj, dict):
        return {k: _rounded(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_rounded(v) for v in obj]
    return obj


def canonical_json(obj: Any) -> str:
    """Stable serialisation used for every hash (sorted keys, fixed float precision)."""
    return json.dumps(_rounded(obj), sort_keys=True, separators=(",", ":"), default=str)


def sha256_hex(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode()).hexdigest()


def hotspot_c(k: float, ambient_c: float) -> float:
    """IEEE C57.91 steady-state winding hot-spot temperature for per-unit load k."""
    top_oil = XFMR_TOP_OIL_RISE * ((k * k * XFMR_LOSS_RATIO + 1) / (XFMR_LOSS_RATIO + 1)) ** XFMR_N
    gradient = XFMR_HOTSPOT_GRADIENT * k ** (2 * XFMR_M)
    return ambient_c + top_oil + gradient


def max_loading_k(ambient_c: float, limit_c: float = HOTSPOT_LIMIT_C) -> float:
    """Largest per-unit load whose hot-spot stays within limit (bisection; monotone in k)."""
    lo, hi = 0.0, 2.5
    if hotspot_c(lo, ambient_c) > limit_c:
        return 0.0
    for _ in range(50):
        mid = (lo + hi) / 2
        if hotspot_c(mid, ambient_c) <= limit_c:
            lo = mid
        else:
            hi = mid
    return lo


def wind_power_fraction(v: float, cut_in: float = 3.0, rated: float = 12.0, cut_out: float = 25.0) -> float:
    if v < cut_in or v >= cut_out:
        return 0.0
    if v >= rated:
        return 1.0
    return (v ** 3 - cut_in ** 3) / (rated ** 3 - cut_in ** 3)


def smt_name(raw: str) -> str:
    return raw.replace("-", "_")


# ─────────────────────────────── Topology ────────────────────────────────

@dataclass(frozen=True)
class NodeSpec:
    id: str
    name: str
    kind: str                   # solar | wind | battery | peaker | load
    country: str
    lat: float
    lon: float
    x: float                    # schematic map position (1000 × 650 viewBox)
    y: float
    capacity_mw: float          # nameplate (sources), BESS power rating, or hub transformer rating
    carbon_g_kwh: float = 0.0
    energy_mwh: float = 0.0     # BESS only
    ramp_mw: float = 0.0        # peakers: max ramp-up per dispatch interval
    nominal_hz: float = 50.0    # load hubs
    base_demand_mw: float = 0.0  # load hubs


@dataclass(frozen=True)
class LineSpec:
    id: str
    src: str
    dst: str
    capacity_mw: float
    tech: str
    name: str


NODES: tuple[NodeSpec, ...] = (
    NodeSpec("WD-HKD", "Hokkaido Wind Corridor", "wind", "JP", 43.5, 141.9, 735, 52, 900, 11),
    NodeSpec("SL-KYU", "Kyushu Mega Solar", "solar", "JP", 32.8, 130.7, 535, 200, 1200, 41),
    NodeSpec("WD-CHG", "Changhua Offshore Wind", "wind", "TW", 24.1, 120.2, 392, 262, 1300, 12),
    NodeSpec("SL-NTA", "Sun Cable NT Solar", "solar", "AU", -19.6, 134.2, 590, 448, 2000, 41),
    NodeSpec("SL-QLD", "Western Downs Solar", "solar", "AU", -26.9, 150.5, 835, 440, 1200, 41),
    NodeSpec("WD-HRN", "Hornsdale Wind", "wind", "AU", -33.1, 138.5, 668, 538, 800, 11),
    NodeSpec("BT-LUZ", "Luzon Grid BESS", "battery", "PH", 14.9, 120.7, 432, 338, 400, 60, energy_mwh=800),
    NodeSpec("BT-VIC", "Victorian Big Battery", "battery", "AU", -38.0, 144.4, 785, 585, 450, 60, energy_mwh=650),
    NodeSpec("PK-JHR", "Pasir Gudang Peaker", "peaker", "MY", 1.47, 103.9, 88, 296, 1000, 430, ramp_mw=250),
    NodeSpec("PK-OSK", "Nanko LNG Peaker", "peaker", "JP", 34.6, 135.4, 612, 168, 1500, 490, ramp_mw=375),
    NodeSpec("PK-NSW", "Tallawarra Gas Peaker", "peaker", "AU", -34.5, 150.8, 948, 590, 1000, 520, ramp_mw=250),
    NodeSpec("LD-TKY", "Tokyo Metro Hub", "load", "JP", 35.7, 139.7, 700, 128, 2300, nominal_hz=50, base_demand_mw=1900),
    NodeSpec("LD-SEL", "Seoul Capital Hub", "load", "KR", 37.6, 127.0, 470, 112, 1600, nominal_hz=60, base_demand_mw=1150),
    NodeSpec("LD-SIN", "Singapore Hub", "load", "SG", 1.35, 103.8, 165, 372, 1800, nominal_hz=50, base_demand_mw=1500),
    NodeSpec("LD-SYD", "Sydney Hub", "load", "AU", -33.9, 151.2, 895, 515, 1950, nominal_hz=50, base_demand_mw=1600),
)

LINES: tuple[LineSpec, ...] = (
    LineSpec("L-HKD-TKY", "WD-HKD", "LD-TKY", 900, "HVDC", "Hokkaido–Honshu HVDC"),
    LineSpec("L-KYU-TKY", "SL-KYU", "LD-TKY", 600, "HVAC", "Kyushu–Kanto 500 kV"),
    LineSpec("L-KYU-SEL", "SL-KYU", "LD-SEL", 500, "HVDC", "Japan–Korea Strait Link"),
    LineSpec("L-CHG-SEL", "WD-CHG", "LD-SEL", 700, "HVDC", "Taiwan–Korea Subsea HVDC"),
    LineSpec("L-CHG-TKY", "WD-CHG", "LD-TKY", 500, "HVDC", "Taiwan–Japan Subsea HVDC"),
    LineSpec("L-NTA-SIN", "SL-NTA", "LD-SIN", 1750, "HVDC", "Australia–Asia PowerLink"),
    LineSpec("L-LUZ-SIN", "BT-LUZ", "LD-SIN", 400, "HVDC", "Luzon–Singapore HVDC"),
    LineSpec("L-LUZ-SEL", "BT-LUZ", "LD-SEL", 300, "HVDC", "Luzon–Korea HVDC"),
    LineSpec("L-JHR-SIN", "PK-JHR", "LD-SIN", 900, "HVAC", "Johor–Singapore 400 kV"),
    LineSpec("L-OSK-TKY", "PK-OSK", "LD-TKY", 1200, "HVDC-FC", "50/60 Hz Frequency Converter"),
    LineSpec("L-OSK-SEL", "PK-OSK", "LD-SEL", 600, "HVDC", "Kansai–Korea HVDC"),
    LineSpec("L-QLD-SYD", "SL-QLD", "LD-SYD", 800, "HVAC", "QNI Interconnector"),
    LineSpec("L-HRN-SYD", "WD-HRN", "LD-SYD", 600, "HVAC", "Project EnergyConnect"),
    LineSpec("L-VIC-SYD", "BT-VIC", "LD-SYD", 450, "HVAC", "VNI West"),
    LineSpec("L-NSW-SYD", "PK-NSW", "LD-SYD", 900, "HVAC", "Illawarra 330 kV"),
)

NODE_BY_ID = {n.id: n for n in NODES}
LINE_BY_ID = {line.id: line for line in LINES}
SOURCES = tuple(n for n in NODES if n.kind != "load")
LOADS = tuple(n for n in NODES if n.kind == "load")
BATTERIES = tuple(n for n in NODES if n.kind == "battery")
MERIT_ORDER = tuple(sorted(SOURCES, key=lambda n: (n.carbon_g_kwh, n.id)))
LINES_FROM = {s.id: tuple(line for line in LINES if line.src == s.id) for s in SOURCES}
RENEWABLE_KINDS = {"solar", "wind"}

AGENT_NETWORK_MANIFEST: dict[str, Any] = {
    "network": "synapsegrid-japac-dispatch",
    "framework": "neuro-san compatible agent network (async in-process runtime)",
    "front_man": "forecasting",
    "agents": [
        {"name": "forecasting", "function": "Ingest weather telemetry → available MW per asset", "tools": ["load_matching"]},
        {"name": "load_matching", "function": "Merit-order re-routing across hubs, BESS and peakers", "tools": ["gemma_verifier"]},
        {"name": "gemma_verifier", "function": "Compile plan to physical invariants, certify or emit counter-example",
         "tools": ["carbon_optimization", "load_matching"], "model": "gemma"},
        {"name": "carbon_optimization", "function": "Carbon intensity & avoided emissions of certified plan", "tools": []},
    ],
    "transport": "pub/sub topics: grid.telemetry, agent.handoff, verifier.verdict, verifier.feedback, gemma.audit, carbon.report",
}


# ─────────────────────────────── Data models ────────────────────────────────

class TelemetryInput(BaseModel):
    """Optional telemetry overrides accepted by POST /api/evaluate-load-shift."""

    irradiance_wm2: dict[str, float] = Field(default_factory=dict, description="Solar farm id → W/m²")
    wind_speed_ms: dict[str, float] = Field(default_factory=dict, description="Wind farm id → hub-height m/s")
    demand_mw: dict[str, float] = Field(default_factory=dict, description="Load hub id → MW")
    battery_soc: dict[str, float] = Field(default_factory=dict, description="BESS id → state of charge (0-1)")
    line_derate: dict[str, float] = Field(default_factory=dict, description="Line id → available fraction (0 = tripped)")
    ambient_c: Optional[float] = Field(default=None, ge=-30, le=60)

    @staticmethod
    def _check(values: dict[str, float], kinds: set[str], lo: float, hi: float, label: str) -> dict[str, float]:
        for key, val in values.items():
            node = NODE_BY_ID.get(key)
            if node is None or node.kind not in kinds:
                raise ValueError(f"{label}: unknown or wrong-kind node id '{key}'")
            if not lo <= val <= hi:
                raise ValueError(f"{label}: value {val} for '{key}' outside [{lo}, {hi}]")
        return values

    @field_validator("irradiance_wm2")
    @classmethod
    def _irr(cls, v: dict[str, float]) -> dict[str, float]:
        return cls._check(v, {"solar"}, 0, 1400, "irradiance_wm2")

    @field_validator("wind_speed_ms")
    @classmethod
    def _wind(cls, v: dict[str, float]) -> dict[str, float]:
        return cls._check(v, {"wind"}, 0, 60, "wind_speed_ms")

    @field_validator("demand_mw")
    @classmethod
    def _demand(cls, v: dict[str, float]) -> dict[str, float]:
        return cls._check(v, {"load"}, 0, 10_000, "demand_mw")

    @field_validator("battery_soc")
    @classmethod
    def _soc(cls, v: dict[str, float]) -> dict[str, float]:
        return cls._check(v, {"battery"}, 0, 1, "battery_soc")

    @field_validator("line_derate")
    @classmethod
    def _derate(cls, v: dict[str, float]) -> dict[str, float]:
        for key, val in v.items():
            if key not in LINE_BY_ID:
                raise ValueError(f"line_derate: unknown line id '{key}'")
            if not 0 <= val <= 1:
                raise ValueError(f"line_derate: value {val} for '{key}' outside [0, 1]")
        return v


class NodeTelemetry(BaseModel):
    node_id: str
    kind: str
    irradiance_wm2: Optional[float] = None
    wind_speed_ms: Optional[float] = None
    demand_mw: Optional[float] = None
    soc: Optional[float] = None


class GridTelemetry(BaseModel):
    telemetry_id: str
    tick: int
    timestamp: str
    ambient_c: float
    nodes: dict[str, NodeTelemetry]
    line_derate: dict[str, float]
    faults: dict[str, bool]

    def hash(self) -> str:
        return sha256_hex(self.model_dump(exclude={"telemetry_id", "timestamp"}))


class ForecastReport(BaseModel):
    available_mw: dict[str, float]
    demand_mw: dict[str, float]
    renewable_available_mw: float
    renewable_p10_mw: float
    renewable_p90_mw: float
    total_demand_mw: float
    alerts: list[str]


class PlanningDirectives(BaseModel):
    caps: dict[str, float] = Field(default_factory=dict)
    authorize_demand_response: bool = False
    strict: bool = False
    notes: list[str] = Field(default_factory=list)


class PowerFlow(BaseModel):
    line_id: str
    src: str
    dst: str
    mw: float


class DispatchPlan(BaseModel):
    plan_id: str
    cycle_id: str
    attempt: int
    strategy: str
    flows: list[PowerFlow]
    dispatch_mw: dict[str, float]
    served_mw: dict[str, float]
    demand_mw: dict[str, float]
    shed_mw: dict[str, float]
    unserved_mw: dict[str, float]
    directives: PlanningDirectives

    def hash(self) -> str:
        return sha256_hex(self.model_dump(include={"flows", "dispatch_mw", "served_mw", "shed_mw"}))


class InvariantCheck(BaseModel):
    id: str
    family: str
    subject: str
    expression: str
    evaluated: str
    lhs: float
    rhs: float
    unit: str
    slack: float
    satisfied: bool
    witness: dict[str, float] = Field(default_factory=dict)


class ProofCertificate(BaseModel):
    kind: Literal["PROOF_CERTIFICATE"] = "PROOF_CERTIFICATE"
    cert_id: str
    cycle_id: str
    attempt: int
    plan_hash: str
    telemetry_hash: str
    invariants_checked: int
    binding_constraint: str
    min_slack: float
    verifier: str
    issued_at: str
    seq: int = 0
    prev_hash: str = ""
    hash: str = ""


class CounterExample(BaseModel):
    kind: Literal["COUNTER_EXAMPLE"] = "COUNTER_EXAMPLE"
    trace_id: str
    cycle_id: str
    attempt: int
    plan_hash: str
    telemetry_hash: str
    violations: list[InvariantCheck]
    witness: dict[str, float]
    issued_at: str
    seq: int = 0
    prev_hash: str = ""
    hash: str = ""


class VerificationResult(BaseModel):
    attempt: int
    verdict: Literal["APPROVED", "BLOCKED"]
    checks: list[InvariantCheck]
    certificate: Optional[ProofCertificate] = None
    counterexample: Optional[CounterExample] = None
    smtlib: str
    latency_ms: float
    gemma: Optional[dict[str, Any]] = None


class CarbonReport(BaseModel):
    emissions_t_per_h: float
    intensity_g_per_kwh: float
    renewable_share: float
    baseline_t_per_h: float
    avoided_t_per_h: float
    curtailed_renewable_mw: float
    shed_mw: float
    mix_mw: dict[str, float]
    hub_intensity_g_per_kwh: dict[str, float]


# ─────────────────────────────── Pub/Sub simulation ────────────────────────────────

class EventBus:
    """In-process stand-in for Google Cloud Pub/Sub (message envelope mirrors PubsubMessage)."""

    def __init__(self, maxlen: int = 400) -> None:
        self._log: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._subscribers: set[asyncio.Queue] = set()
        self.published = 0

    def publish(self, topic: str, data: Any, summary: str = "", **attributes: str) -> dict[str, Any]:
        self.published += 1
        msg = {
            "messageId": f"{self.published:08d}-{uuid.uuid4().hex[:6]}",
            "topic": topic,
            "publishTime": utcnow_iso(),
            "attributes": {"summary": summary, **attributes},
            "data": data,
        }
        self._log.append(msg)
        self._fanout({"type": "bus", "data": msg})
        return msg

    def broadcast(self, frame: dict[str, Any]) -> None:
        self._fanout(frame)

    def _fanout(self, frame: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                pass  # slow consumer: drop rather than block the grid

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=600)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def recent(self, n: int = 80) -> list[dict[str, Any]]:
        return list(self._log)[-n:]


# ─────────────────────────────── Hash-chained audit ledger ────────────────────────────────

class HashLedger:
    """Append-only SHA-256 chain of certificates and counter-examples."""

    GENESIS = "0" * 64

    def __init__(self, maxlen: int = 200) -> None:
        self._entries: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self.head = self.GENESIS
        self.seq = 0

    def seal(self, artifact: ProofCertificate | CounterExample) -> None:
        self.seq += 1
        artifact.seq = self.seq
        artifact.prev_hash = self.head
        body = artifact.model_dump(exclude={"hash"})
        artifact.hash = hashlib.sha256((self.head + canonical_json(body)).encode()).hexdigest()
        self.head = artifact.hash
        self._entries.append({
            "seq": artifact.seq,
            "kind": artifact.kind,
            "ref": getattr(artifact, "cert_id", None) or getattr(artifact, "trace_id", None),
            "cycle_id": artifact.cycle_id,
            "attempt": artifact.attempt,
            "hash": artifact.hash,
            "prev_hash": artifact.prev_hash,
            "issued_at": artifact.issued_at,
        })

    def verify_chain(self) -> bool:
        entries = list(self._entries)
        return all(b["prev_hash"] == a["hash"] for a, b in zip(entries, entries[1:]))

    def tail(self, n: int = 14) -> list[dict[str, Any]]:
        return list(self._entries)[-n:][::-1]


# ─────────────────────────────── Gemma client ────────────────────────────────

class GemmaClient:
    """
    Talks to Gemma through one of:
      * google  — Google AI Studio / Gemini API hosting Gemma (GEMMA_API_KEY or GOOGLE_API_KEY)
      * openai  — any OpenAI-compatible server running Gemma: Ollama, vLLM, Cloud Run GPU (GEMMA_BASE_URL)
      * offline — deterministic templated audit (no network), so the demo never stalls
    """

    def __init__(self) -> None:
        self.base_url = os.getenv("GEMMA_BASE_URL", "").rstrip("/")
        self.api_key = os.getenv("GEMMA_API_KEY") or os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or ""
        provider = os.getenv("GEMMA_PROVIDER", "auto").lower()
        if provider == "auto":
            provider = "openai" if self.base_url else ("google" if self.api_key else "offline")
        self.provider = provider
        default_model = "gemma3:4b" if provider == "openai" else "gemma-3-27b-it"
        self.model = os.getenv("GEMMA_MODEL", default_model)
        self.timeout_s = float(os.getenv("GEMMA_TIMEOUT_S", "8"))
        self.online = provider != "offline"
        self.last_error: Optional[str] = None
        self.calls = 0
        self.last_latency_ms: Optional[float] = None
        self._retry_after = 0.0
        self._cache: dict[str, str] = {}
        self._sem = asyncio.Semaphore(2)
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def info(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model if self.provider != "offline" else "deterministic-template",
            "online": self.online and self.provider != "offline",
            "calls": self.calls,
            "last_latency_ms": self.last_latency_ms,
            "last_error": self.last_error,
        }

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def _complete(self, prompt: str) -> str:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_s)
        if self.provider == "google":
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
            resp = await self._client.post(
                url,
                headers={"x-goog-api-key": self.api_key},
                json={
                    # Gemma on the Gemini API has no system role: everything lives in the user turn.
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.2, "maxOutputTokens": 240},
                },
            )
            resp.raise_for_status()
            parts = resp.json()["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts).strip()
        if self.provider == "openai":
            headers = {"Authorization": f"Bearer {self.api_key}"} if os.getenv("GEMMA_API_KEY") else {}
            resp = await self._client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json={"model": self.model, "temperature": 0.2, "max_tokens": 240,
                      "messages": [{"role": "user", "content": prompt}]},
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
        raise RuntimeError(f"unsupported GEMMA_PROVIDER '{self.provider}'")

    async def audit(self, prompt: str, signature: str, fallback: str) -> dict[str, Any]:
        base = {"provider": self.provider, "model": self.info["model"]}
        if signature in self._cache:
            return {**base, "text": self._cache[signature], "source": "gemma-cache", "latency_ms": 0.0}
        if self.provider == "offline" or time.monotonic() < self._retry_after:
            return {**base, "text": fallback, "source": "deterministic-fallback", "latency_ms": 0.0}
        async with self._sem:
            t0 = time.perf_counter()
            try:
                text = await asyncio.wait_for(self._complete(prompt), self.timeout_s)
                if not text:
                    raise RuntimeError("empty completion")
            except Exception as exc:  # network, quota, parse — degrade gracefully
                self.online = False
                self.last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
                self._retry_after = time.monotonic() + 30
                log.warning("Gemma audit failed (%s); using deterministic fallback", self.last_error)
                return {**base, "text": fallback, "source": "deterministic-fallback", "latency_ms": 0.0}
            latency = round((time.perf_counter() - t0) * 1000, 1)
            self.online, self.last_error, self.last_latency_ms = True, None, latency
            self.calls += 1
            if len(self._cache) > 256:
                self._cache.pop(next(iter(self._cache)))
            self._cache[signature] = text
            return {**base, "text": text, "source": "gemma", "latency_ms": latency}


# ─────────────────────────────── Grid state simulator ────────────────────────────────

class GridState:
    """Live JAPAC grid: weather, demand, battery SoC, faults. Seeded → reproducible demos."""

    FAULTS = ("cloud_cover", "grid_surge", "unsafe_overload")

    def __init__(self, seed: int = 20261018) -> None:
        self.seed = seed
        self.reset()

    def reset(self) -> None:
        self.rng = random.Random(self.seed)
        self.tick = 0
        self.soc = {"BT-LUZ": 0.66, "BT-VIC": 0.62}
        self.prev_dispatch: Optional[dict[str, float]] = None
        self.faults = {f: False for f in self.FAULTS}

    def sample_telemetry(self, overrides: Optional[TelemetryInput] = None) -> GridTelemetry:
        t, rng = self.tick, self.rng
        cloud, surge = self.faults["cloud_cover"], self.faults["grid_surge"]
        ambient = 28.0 + 1.5 * math.sin(t / 25) + rng.uniform(-0.3, 0.3) + (8.0 if surge else 0.0)
        nodes: dict[str, NodeTelemetry] = {}
        for idx, n in enumerate(NODES):
            phase = idx * 0.7
            nt = NodeTelemetry(node_id=n.id, kind=n.kind)
            if n.kind == "solar":
                irr = 820 + 70 * math.sin(t / 18 + phase) + rng.uniform(-30, 30)
                if cloud:
                    irr *= 0.2 + rng.uniform(-0.04, 0.04)
                nt.irradiance_wm2 = round(max(irr, 0.0), 1)
            elif n.kind == "wind":
                nt.wind_speed_ms = round(max(11.0 + 0.9 * math.sin(t / 11 + phase) + rng.uniform(-0.5, 0.5), 0.0), 2)
            elif n.kind == "battery":
                nt.soc = round(self.soc[n.id], 4)
            elif n.kind == "load":
                demand = n.base_demand_mw * (1 + 0.035 * math.sin(t / 16 + phase)) * (1 + rng.uniform(-0.012, 0.012))
                if surge and n.id in ("LD-TKY", "LD-SEL"):
                    demand *= 1.28
                nt.demand_mw = round(demand, 1)
            nodes[n.id] = nt

        derate = {line.id: 1.0 for line in LINES}
        if surge:
            derate["L-CHG-SEL"] = 0.0  # HVDC pole trip during heatwave surge

        if overrides is not None:
            for k, v in overrides.irradiance_wm2.items():
                nodes[k].irradiance_wm2 = v
            for k, v in overrides.wind_speed_ms.items():
                nodes[k].wind_speed_ms = v
            for k, v in overrides.demand_mw.items():
                nodes[k].demand_mw = v
            for k, v in overrides.battery_soc.items():
                nodes[k].soc = v
            derate.update(overrides.line_derate)
            if overrides.ambient_c is not None:
                ambient = overrides.ambient_c

        return GridTelemetry(
            telemetry_id=f"tel-{uuid.uuid4().hex[:10]}",
            tick=t,
            timestamp=utcnow_iso(),
            ambient_c=round(ambient, 2),
            nodes=nodes,
            line_derate=derate,
            faults=dict(self.faults),
        )

    def commit(self, plan: DispatchPlan, telemetry: GridTelemetry) -> None:
        for b in BATTERIES:
            soc = telemetry.nodes[b.id].soc if telemetry.nodes[b.id].soc is not None else self.soc[b.id]
            drained = plan.dispatch_mw.get(b.id, 0.0) * SIM_STEP_H / b.energy_mwh
            soc = soc - drained + SOC_REVERSION * (SOC_TARGET - soc)
            self.soc[b.id] = min(max(soc, 0.02), 1.0)
        self.prev_dispatch = dict(plan.dispatch_mw)
        self.tick += 1


# ─────────────────────────────── Agent 1: Forecasting ────────────────────────────────

class ForecastingAgent:
    name: AgentName = "forecasting"

    def __init__(self) -> None:
        self._prev_renewable: Optional[float] = None

    def run(self, tel: GridTelemetry) -> ForecastReport:
        available: dict[str, float] = {}
        for n in SOURCES:
            nt = tel.nodes[n.id]
            if n.kind == "solar":
                mw = n.capacity_mw * min((nt.irradiance_wm2 or 0) / 1000, 1.1) * 0.92  # inverter + soiling
            elif n.kind == "wind":
                mw = n.capacity_mw * wind_power_fraction(nt.wind_speed_ms or 0)
            else:  # batteries report rated power, peakers nameplate — physics lives in the verifier
                mw = n.capacity_mw
            available[n.id] = round(mw, 1)

        demand = {n.id: round(tel.nodes[n.id].demand_mw or 0.0, 1) for n in LOADS}
        renewable = sum(available[n.id] for n in SOURCES if n.kind in RENEWABLE_KINDS)
        alerts: list[str] = []
        if self._prev_renewable and renewable < 0.8 * self._prev_renewable:
            drop = 100 * (1 - renewable / self._prev_renewable)
            alerts.append(f"RAMP_DOWN: renewable availability −{drop:.0f}% vs previous interval")
        for n in SOURCES:
            if n.kind == "solar" and (tel.nodes[n.id].irradiance_wm2 or 0) < 300:
                alerts.append(f"CLOUD_COVER: {n.id} irradiance {tel.nodes[n.id].irradiance_wm2:.0f} W/m²")
        for line_id, d in tel.line_derate.items():
            if d < 1:
                alerts.append(f"SCADA: {line_id} available {d:.0%}")
        self._prev_renewable = renewable
        return ForecastReport(
            available_mw=available,
            demand_mw=demand,
            renewable_available_mw=round(renewable, 1),
            renewable_p10_mw=round(renewable * 0.92, 1),
            renewable_p90_mw=round(renewable * 1.06, 1),
            total_demand_mw=round(sum(demand.values()), 1),
            alerts=alerts,
        )


# ─────────────────────────────── Agent 2: Load-Matching ────────────────────────────────

class LoadMatchingAgent:
    """
    Low-carbon merit-order router. Deliberately works from the *planning model*
    (nameplate line ratings, rated BESS power) — it does not know live derates,
    SoC floors, ramp limits or transformer thermals. Those are the verifier's job.
    """

    name: AgentName = "load_matching"

    def propose(self, cycle_id: str, attempt: int, forecast: ForecastReport,
                directives: PlanningDirectives, unsafe: bool) -> DispatchPlan:
        speculative = unsafe and not directives.strict
        caps = directives.caps
        residual = dict(forecast.demand_mw)
        served = {n.id: 0.0 for n in LOADS}
        flows: dict[str, float] = {}

        for src in MERIT_ORDER:
            remaining = min(forecast.available_mw[src.id], caps.get(src.id, math.inf))
            if speculative:
                remaining = forecast.available_mw[src.id] * 1.15  # "forecast optimism"
            for line in LINES_FROM[src.id]:
                if remaining <= 1e-6:
                    break
                line_cap = math.inf if speculative else min(line.capacity_mw, caps.get(line.id, math.inf))
                hub_room = residual[line.dst]
                if not speculative:
                    hub_room = min(hub_room, caps.get(line.dst, math.inf) - served[line.dst])
                push = max(0.0, min(remaining, line_cap, hub_room))
                if push > 1e-6:
                    flows[line.id] = flows.get(line.id, 0.0) + push
                    served[line.dst] += push
                    residual[line.dst] -= push
                    remaining -= push

        if speculative:
            flows = {k: v * UNSAFE_INFLATION for k, v in flows.items()}

        flow_models = [PowerFlow(line_id=k, src=LINE_BY_ID[k].src, dst=LINE_BY_ID[k].dst, mw=round(v, 1))
                       for k, v in sorted(flows.items()) if v > 0.05]
        dispatch = {n.id: 0.0 for n in SOURCES}
        served = {n.id: 0.0 for n in LOADS}
        for f in flow_models:
            dispatch[f.src] = round(dispatch[f.src] + f.mw, 1)
            served[f.dst] = round(served[f.dst] + f.mw, 1)
        unserved = {k: round(max(forecast.demand_mw[k] - served[k], 0.0), 1) for k in served}
        shed = {k: v for k, v in unserved.items() if v > 0} if directives.authorize_demand_response else {}

        if speculative:
            strategy = "speculative-max-throughput ⚠ (injected unsafe planner)"
        else:
            strategy = "merit-order/low-carbon"
            if caps:
                strategy += f" + {len(caps)} verifier caps"
            if directives.authorize_demand_response:
                strategy += " + demand-response"
        return DispatchPlan(
            plan_id=f"plan-{uuid.uuid4().hex[:10]}",
            cycle_id=cycle_id,
            attempt=attempt,
            strategy=strategy,
            flows=flow_models,
            dispatch_mw=dispatch,
            served_mw=served,
            demand_mw=dict(forecast.demand_mw),
            shed_mw=shed,
            unserved_mw=unserved,
            directives=directives.model_copy(deep=True),
        )


# ─────────────────────────────── Gemma Safety Verifier ────────────────────────────────

FAMILY_PHRASES = {
    "LINE_THERMAL": "interconnector flow exceeds live thermal/derated capacity",
    "GEN_ENVELOPE": "generator dispatched beyond forecast availability",
    "GEN_RAMP": "peaker asked to ramp faster than its governor allows",
    "BESS_POWER": "battery discharge exceeds inverter power rating",
    "BESS_SOC_FLOOR": "battery would breach its state-of-charge floor within the interval",
    "XFMR_HOTSPOT": "hub transformer hot-spot would exceed the IEEE C57.91 limit",
    "FREQ_BAND": "regional supply/demand imbalance pushes frequency outside the operating band",
}


class GemmaSafetyVerifier:
    name: AgentName = "gemma_verifier"
    VERSION = "synapsegrid-verifier/1.2"

    def __init__(self, gemma: GemmaClient) -> None:
        self.gemma = gemma

    # ---- compile -------------------------------------------------------
    def compile_invariants(self, plan: DispatchPlan, tel: GridTelemetry, forecast: ForecastReport,
                           prev_dispatch: Optional[dict[str, float]]) -> list[InvariantCheck]:
        checks: list[InvariantCheck] = []

        def add(cid: str, family: str, subject: str, expression: str, evaluated: str,
                lhs: float, rhs: float, unit: str, witness: dict[str, float]) -> None:
            checks.append(InvariantCheck(
                id=cid, family=family, subject=subject, expression=expression, evaluated=evaluated,
                lhs=round(lhs, 4), rhs=round(rhs, 4), unit=unit, slack=round(rhs - lhs, 4),
                satisfied=lhs <= rhs + 1e-6, witness={k: round(v, 4) for k, v in witness.items()},
            ))

        line_flow = {line.id: 0.0 for line in LINES}
        for f in plan.flows:
            line_flow[f.line_id] += f.mw
        for line in LINES:
            d = tel.line_derate.get(line.id, 1.0)
            p, limit = abs(line_flow[line.id]), line.capacity_mw * d
            add(f"LINE_THERMAL[{line.id}]", "LINE_THERMAL", line.id,
                f"|P[{line.id}]| ≤ C·δ",
                f"{p:,.1f} ≤ {line.capacity_mw:,.0f}·{d:.2f} = {limit:,.1f} MW",
                p, limit, "MW", {f"P[{line.id}]": p, f"δ[{line.id}]": d})

        for n in SOURCES:
            p = plan.dispatch_mw.get(n.id, 0.0)
            if n.kind == "battery":
                add(f"BESS_POWER[{n.id}]", "BESS_POWER", n.id, f"P[{n.id}] ≤ P_rated",
                    f"{p:,.1f} ≤ {n.capacity_mw:,.0f} MW", p, n.capacity_mw, "MW", {f"P[{n.id}]": p})
                soc = tel.nodes[n.id].soc if tel.nodes[n.id].soc is not None else 0.0
                drained = p * DISPATCH_INTERVAL_H / n.energy_mwh
                headroom = soc - BESS_SOC_MIN
                add(f"BESS_SOC_FLOOR[{n.id}]", "BESS_SOC_FLOOR", n.id,
                    f"P[{n.id}]·Δt/E ≤ SoC − SoC_min",
                    f"{p:,.1f}·{DISPATCH_INTERVAL_H}/{n.energy_mwh:,.0f} = {drained:.3f} ≤ {soc:.3f} − {BESS_SOC_MIN:.2f} = {headroom:.3f}",
                    drained, headroom, "p.u.", {f"P[{n.id}]": p, f"SoC[{n.id}]": soc})
            else:
                avail = forecast.available_mw[n.id]
                add(f"GEN_ENVELOPE[{n.id}]", "GEN_ENVELOPE", n.id, f"P[{n.id}] ≤ P_avail",
                    f"{p:,.1f} ≤ {avail:,.1f} MW", p, avail, "MW", {f"P[{n.id}]": p, f"Pavail[{n.id}]": avail})
                if n.kind == "peaker" and prev_dispatch is not None:
                    prev = prev_dispatch.get(n.id, 0.0)
                    add(f"GEN_RAMP[{n.id}]", "GEN_RAMP", n.id, f"P[{n.id}] − P₀[{n.id}] ≤ R_up",
                        f"{p:,.1f} − {prev:,.1f} = {p - prev:,.1f} ≤ {n.ramp_mw:,.0f} MW",
                        p - prev, n.ramp_mw, "MW", {f"P[{n.id}]": p, f"P0[{n.id}]": prev})

        for n in LOADS:
            served = plan.served_mw.get(n.id, 0.0)
            k = served / n.capacity_mw
            theta = hotspot_c(k, tel.ambient_c)
            add(f"XFMR_HOTSPOT[{n.id}]", "XFMR_HOTSPOT", n.id,
                "θ_A + Δθ_TO·((K²R+1)/(R+1))ⁿ + Δθ_H·K²ᵐ ≤ θ_max",
                f"K={k:.3f} @ θ_A={tel.ambient_c:.1f}°C → θ_H={theta:.1f} ≤ {HOTSPOT_LIMIT_C:.0f} °C",
                theta, HOTSPOT_LIMIT_C, "°C", {f"S[{n.id}]": served, f"K[{n.id}]": k, "θ_A": tel.ambient_c})

            demand = plan.demand_mw.get(n.id, 0.0)
            connected = max(demand - plan.shed_mw.get(n.id, 0.0), 1e-6)
            df = n.nominal_hz * DROOP * (served - connected) / connected
            add(f"FREQ_BAND[{n.id}]", "FREQ_BAND", n.id,
                "|f₀·R·(S − (D − shed))/(D − shed)| ≤ Δf_max",
                f"|{n.nominal_hz:.0f}·{DROOP}·({served:,.1f} − {connected:,.1f})/{connected:,.1f}| = {abs(df):.3f} ≤ {FREQ_BAND_HZ} Hz",
                abs(df), FREQ_BAND_HZ, "Hz", {f"S[{n.id}]": served, f"D[{n.id}]": demand, f"Δf[{n.id}]": df})
        return checks

    def to_smtlib(self, plan: DispatchPlan, tel: GridTelemetry, forecast: ForecastReport,
                  prev_dispatch: Optional[dict[str, float]]) -> str:
        flow = {f.line_id: f.mw for f in plan.flows}
        out = [
            f"; SynapseGrid formal dispatch model — {plan.cycle_id} attempt {plan.attempt}",
            f"; plan {plan.hash()[:16]}…  verifier {self.VERSION}",
            "(set-logic QF_LRA)",
        ]
        for line in LINES:
            out.append(f"(declare-const P_{smt_name(line.id)} Real)")
        out.append("; ── proposed assignment ──")
        for line in LINES:
            out.append(f"(assert (= P_{smt_name(line.id)} {flow.get(line.id, 0.0):.1f}))")
        out.append("; ── derived injections ──")
        for n in SOURCES:
            terms = " ".join(f"P_{smt_name(line.id)}" for line in LINES_FROM[n.id]) or "0.0"
            out.append(f"(define-fun G_{smt_name(n.id)} () Real (+ {terms} 0.0))")
        for n in LOADS:
            terms = " ".join(f"P_{smt_name(line.id)}" for line in LINES if line.dst == n.id)
            out.append(f"(define-fun S_{smt_name(n.id)} () Real (+ {terms} 0.0))")
        out.append("; ── physical invariants ──")
        for line in LINES:
            lim = line.capacity_mw * tel.line_derate.get(line.id, 1.0)
            out.append(f"(assert (<= P_{smt_name(line.id)} {lim:.1f}))  ; LINE_THERMAL")
        for n in SOURCES:
            g = f"G_{smt_name(n.id)}"
            if n.kind == "battery":
                soc = tel.nodes[n.id].soc or 0.0
                out.append(f"(assert (<= {g} {n.capacity_mw:.1f}))  ; BESS_POWER")
                out.append(f"(assert (<= (* {g} {DISPATCH_INTERVAL_H / n.energy_mwh:.8f}) {soc - BESS_SOC_MIN:.4f}))  ; BESS_SOC_FLOOR")
            else:
                out.append(f"(assert (<= {g} {forecast.available_mw[n.id]:.1f}))  ; GEN_ENVELOPE")
                if n.kind == "peaker" and prev_dispatch is not None:
                    out.append(f"(assert (<= (- {g} {prev_dispatch.get(n.id, 0.0):.1f}) {n.ramp_mw:.1f}))  ; GEN_RAMP")
        for n in LOADS:
            s = f"S_{smt_name(n.id)}"
            kmax = max_loading_k(tel.ambient_c)
            out.append(f"(assert (<= {s} {kmax * n.capacity_mw:.1f}))  ; XFMR_HOTSPOT (K_max={kmax:.4f} @ {tel.ambient_c:.1f}°C)")
            connected = plan.demand_mw[n.id] - plan.shed_mw.get(n.id, 0.0)
            band = FREQ_BAND_HZ * connected / (n.nominal_hz * DROOP)
            out.append(f"(assert (<= (- {s} {connected:.1f}) {band:.1f}))  ; FREQ_BAND (over)")
            out.append(f"(assert (<= (- {connected:.1f} {s}) {band:.1f}))  ; FREQ_BAND (under)")
        out.append("(check-sat)  ; sat ⇔ proposed dispatch is physically safe")
        return "\n".join(out)

    # ---- verify --------------------------------------------------------
    def verify(self, plan: DispatchPlan, tel: GridTelemetry, forecast: ForecastReport,
               prev_dispatch: Optional[dict[str, float]], ledger: HashLedger) -> VerificationResult:
        t0 = time.perf_counter()
        checks = self.compile_invariants(plan, tel, forecast, prev_dispatch)
        smt = self.to_smtlib(plan, tel, forecast, prev_dispatch)
        violations = [c for c in checks if not c.satisfied]
        plan_hash, tel_hash = plan.hash(), tel.hash()
        cert: Optional[ProofCertificate] = None
        cex: Optional[CounterExample] = None
        if violations:
            violations.sort(key=lambda c: c.slack)
            witness: dict[str, float] = {}
            for v in violations:
                witness.update(v.witness)
            cex = CounterExample(
                trace_id=f"cex-{uuid.uuid4().hex[:10]}", cycle_id=plan.cycle_id, attempt=plan.attempt,
                plan_hash=plan_hash, telemetry_hash=tel_hash, violations=violations, witness=witness,
                issued_at=utcnow_iso(),
            )
            ledger.seal(cex)
        else:
            binding = min(checks, key=lambda c: c.slack / (abs(c.rhs) or 1.0))
            cert = ProofCertificate(
                cert_id=f"cert-{uuid.uuid4().hex[:10]}", cycle_id=plan.cycle_id, attempt=plan.attempt,
                plan_hash=plan_hash, telemetry_hash=tel_hash, invariants_checked=len(checks),
                binding_constraint=binding.id, min_slack=binding.slack,
                verifier=f"{self.VERSION} + {self.gemma.info['model']}", issued_at=utcnow_iso(),
            )
            ledger.seal(cert)
        return VerificationResult(
            attempt=plan.attempt,
            verdict="BLOCKED" if violations else "APPROVED",
            checks=checks,
            certificate=cert,
            counterexample=cex,
            smtlib=smt,
            latency_ms=round((time.perf_counter() - t0) * 1000, 2),
        )

    # ---- feedback loop -------------------------------------------------
    def feedback(self, plan: DispatchPlan, result: VerificationResult, tel: GridTelemetry,
                 directives: PlanningDirectives, unsafe: bool) -> PlanningDirectives:
        nxt = directives.model_copy(deep=True)
        nxt.notes = []

        def cap(key: str, value: float, why: str) -> None:
            value = max(round(value, 1), 0.0)
            if value < nxt.caps.get(key, math.inf):
                nxt.caps[key] = value
                nxt.notes.append(f"{key} ≤ {value:,.1f} MW ({why})")

        for v in result.counterexample.violations if result.counterexample else []:
            node = NODE_BY_ID.get(v.subject)
            if v.family == "LINE_THERMAL":
                cap(v.subject, v.rhs * 0.98, "live line rating −2% margin")
            elif v.family == "GEN_ENVELOPE":
                cap(v.subject, v.rhs, "forecast availability")
            elif v.family == "GEN_RAMP":
                cap(v.subject, plan.dispatch_mw[v.subject] - (v.lhs - v.rhs) - 0.5, "ramp-rate limit")
            elif v.family == "BESS_POWER":
                cap(v.subject, v.rhs, "inverter rating")
            elif v.family == "BESS_SOC_FLOOR" and node is not None:
                cap(v.subject, 0.98 * v.rhs * node.energy_mwh / DISPATCH_INTERVAL_H, "SoC floor")
            elif v.family == "XFMR_HOTSPOT" and node is not None:
                cap(v.subject, 0.98 * max_loading_k(tel.ambient_c) * node.capacity_mw, "transformer hot-spot")
            elif v.family == "FREQ_BAND":
                if v.witness.get(f"Δf[{v.subject}]", 0.0) > 0:
                    if not nxt.strict:
                        nxt.strict = True
                        nxt.notes.append("over-delivery: disable speculative dispatch")
                elif not nxt.authorize_demand_response:
                    nxt.authorize_demand_response = True
                    nxt.notes.append("under-frequency risk: authorise controlled demand response")
        if unsafe and not nxt.strict:
            nxt.strict = True
            nxt.notes.insert(0, "planner switched to strict constrained mode")
        return nxt

    # ---- Gemma audit ---------------------------------------------------
    @staticmethod
    def audit_prompt(result: VerificationResult, directives: Optional[PlanningDirectives]) -> tuple[str, str, str]:
        """Returns (prompt, cache signature, deterministic fallback text)."""
        if result.counterexample:
            vs = result.counterexample.violations
            lines = "\n".join(f"- {v.id}: {v.evaluated} (excess {v.lhs - v.rhs:+.3f} {v.unit})" for v in vs[:8])
            prompt = (
                "You are the Safety Auditor inside SynapseGrid, a multi-agent renewable-energy dispatcher for the "
                "JAPAC interconnect. A deterministic physics verifier BLOCKED a proposed power dispatch because these "
                f"invariants are violated:\n{lines}\n\n"
                "Respond in plain text, at most 90 words, with exactly two sections:\n"
                "DIAGNOSIS: one or two sentences naming the physical root cause.\n"
                "DIRECTIVE: up to three short imperative re-planning instructions for the load-matching agent."
            )
            families = sorted({v.family for v in vs})
            signature = "cex:" + ",".join(sorted(v.id for v in vs))
            diag = "; ".join(FAMILY_PHRASES[f] for f in families)
            directive = "; ".join((directives.notes if directives else [])[:3]) or "re-plan within verified envelopes"
            fallback = f"DIAGNOSIS: {diag[0].upper() + diag[1:]}.\nDIRECTIVE: {directive}."
            return prompt, signature, fallback
        cert = result.certificate
        assert cert is not None
        binding = next(c for c in result.checks if c.id == cert.binding_constraint)
        prompt = (
            "You are the Safety Auditor inside SynapseGrid, a renewable-energy dispatcher for the JAPAC interconnect. "
            f"A dispatch plan passed all {cert.invariants_checked} physical invariants. The binding (tightest) "
            f"constraint is {binding.id}: {binding.evaluated}.\n"
            "Respond in plain text, at most 60 words, with exactly two sections:\n"
            "ATTESTATION: one sentence certifying the plan.\nWATCH: one sentence on the operational risk to monitor next."
        )
        signature = f"cert:{binding.id}:{round(binding.slack / max(abs(binding.rhs), 1) * 20)}"
        fallback = (f"ATTESTATION: All {cert.invariants_checked} invariants hold; plan is physically safe to dispatch.\n"
                    f"WATCH: {binding.id} is the binding constraint ({binding.evaluated}).")
        return prompt, signature, fallback


# ─────────────────────────────── Agent 3: Carbon-Optimization ────────────────────────────────

class CarbonOptimizationAgent:
    name: AgentName = "carbon_optimization"
    BASELINE_G_KWH = 490.0  # counterfactual: hubs served by regional gas peakers

    def run(self, plan: DispatchPlan, forecast: ForecastReport) -> CarbonReport:
        # MW × g/kWh = kg/h  →  /1000 = t/h
        mix = {"wind": 0.0, "solar": 0.0, "battery": 0.0, "peaker": 0.0}
        emissions_kg = 0.0
        for sid, mw in plan.dispatch_mw.items():
            n = NODE_BY_ID[sid]
            mix[n.kind] += mw
            emissions_kg += mw * n.carbon_g_kwh
        served = sum(plan.served_mw.values())
        hub_int: dict[str, float] = {}
        for hub in LOADS:
            inflow = [(f.mw, NODE_BY_ID[f.src].carbon_g_kwh) for f in plan.flows if f.dst == hub.id]
            total = sum(mw for mw, _ in inflow)
            hub_int[hub.id] = round(sum(mw * c for mw, c in inflow) / total, 1) if total else 0.0
        renewable_avail = forecast.renewable_available_mw
        renewable_used = mix["wind"] + mix["solar"]
        baseline_t = served * self.BASELINE_G_KWH / 1000
        emissions_t = emissions_kg / 1000
        return CarbonReport(
            emissions_t_per_h=round(emissions_t, 2),
            intensity_g_per_kwh=round(emissions_kg / served, 1) if served else 0.0,
            renewable_share=round(renewable_used / served, 4) if served else 0.0,
            baseline_t_per_h=round(baseline_t, 2),
            avoided_t_per_h=round(baseline_t - emissions_t, 2),
            curtailed_renewable_mw=round(max(renewable_avail - renewable_used, 0.0), 1),
            shed_mw=round(sum(plan.shed_mw.values()), 1),
            mix_mw={k: round(v, 1) for k, v in mix.items()},
            hub_intensity_g_per_kwh=hub_int,
        )


# ─────────────────────────────── Orchestrator ────────────────────────────────

def _plan_payload(plan: DispatchPlan) -> dict[str, Any]:
    return {
        "plan_id": plan.plan_id,
        "attempt": plan.attempt,
        "strategy": plan.strategy,
        "plan_hash": plan.hash(),
        "flows": [f.model_dump() for f in plan.flows],
        "dispatch_mw": {k: v for k, v in plan.dispatch_mw.items() if v},
        "served_mw": plan.served_mw,
        "shed_mw": plan.shed_mw,
        "unserved_mw": {k: v for k, v in plan.unserved_mw.items() if v},
        "directives": plan.directives.model_dump(),
    }


class SynapseGridNetwork:
    """Owns the agents, live grid state, Pub/Sub bus and hash ledger; runs dispatch cycles."""

    def __init__(self) -> None:
        self.bus = EventBus()
        self.gemma = GemmaClient()
        self.state = GridState()
        self.ledger = HashLedger()
        self.forecaster = ForecastingAgent()
        self.matcher = LoadMatchingAgent()
        self.verifier = GemmaSafetyVerifier(self.gemma)
        self.carbon = CarbonOptimizationAgent()
        self.history: deque[dict[str, Any]] = deque(maxlen=90)
        self.last_cycle: Optional[dict[str, Any]] = None
        self.active: Optional[dict[str, Any]] = None  # last committed plan/forecast/telemetry/carbon
        self.scheduler_running = True
        self.started_at = time.time()
        self.metrics = {"cycles": 0, "certified": 0, "first_pass": 0, "blocked_attempts": 0, "replans": 0,
                        "safe_holds": 0, "dr_events": 0, "avoided_t_total": 0.0, "invariants_evaluated": 0}
        self._lock = asyncio.Lock()
        self._bg: set[asyncio.Task] = set()

    async def aclose(self) -> None:
        for task in list(self._bg):
            task.cancel()
        await self.gemma.aclose()

    def reset(self) -> None:
        self.state.reset()
        self.forecaster = ForecastingAgent()
        self.history.clear()
        self.last_cycle = None
        self.active = None
        for k in self.metrics:
            self.metrics[k] = 0.0 if isinstance(self.metrics[k], float) else 0

    def set_fault(self, fault: str, active: bool) -> None:
        self.state.faults[fault] = active
        self.bus.publish("control.fault", {"fault": fault, "active": active, "faults": dict(self.state.faults)},
                         summary=f"{fault} → {'ON' if active else 'OFF'}")

    # ---- the dispatch cycle -------------------------------------------
    async def run_cycle(self, overrides: Optional[TelemetryInput] = None, unsafe: Optional[bool] = None,
                        origin: str = "scheduler", commit: bool = True, await_audit: bool = False) -> dict[str, Any]:
        async with self._lock:
            cycle_id = f"cyc-{self.state.tick:05d}-{uuid.uuid4().hex[:4]}"
            unsafe = self.state.faults["unsafe_overload"] if unsafe is None else unsafe
            t_start = time.perf_counter()
            timings: dict[str, float] = {}

            tel = self.state.sample_telemetry(overrides)
            self.bus.publish("grid.telemetry", tel.model_dump(), cycle_id=cycle_id,
                             summary=f"tick {tel.tick} · ambient {tel.ambient_c:.1f}°C · faults: "
                                     f"{', '.join(k for k, v in tel.faults.items() if v) or 'none'}")

            t0 = time.perf_counter()
            forecast = self.forecaster.run(tel)
            timings["forecasting"] = round((time.perf_counter() - t0) * 1000, 2)
            self.bus.publish("agent.handoff", {"from": "forecasting", "to": "load_matching", "payload": forecast.model_dump()},
                             cycle_id=cycle_id,
                             summary=f"forecasting → load_matching · RE avail {forecast.renewable_available_mw:,.0f} MW · "
                                     f"demand {forecast.total_demand_mw:,.0f} MW")

            directives = PlanningDirectives()
            attempts: list[dict[str, Any]] = []
            plan: Optional[DispatchPlan] = None
            result: Optional[VerificationResult] = None
            audit_jobs: list[asyncio.Task] = []
            timings["load_matching"] = timings["gemma_verifier"] = 0.0
            intercepted: set[str] = set()

            for attempt in range(1, MAX_ATTEMPTS + 1):
                t0 = time.perf_counter()
                plan = self.matcher.propose(cycle_id, attempt, forecast, directives, unsafe)
                timings["load_matching"] += round((time.perf_counter() - t0) * 1000, 2)
                self.bus.publish("agent.handoff",
                                 {"from": "load_matching", "to": "gemma_verifier", "intercepted": True,
                                  "payload": _plan_payload(plan)},
                                 cycle_id=cycle_id,
                                 summary=f"load_matching ⇢ gemma_verifier (intercepted) · A{attempt} · "
                                         f"{len(plan.flows)} flows · {plan.strategy}")

                result = self.verifier.verify(plan, tel, forecast, self.state.prev_dispatch, self.ledger)
                timings["gemma_verifier"] += result.latency_ms
                self.metrics["invariants_evaluated"] += len(result.checks)
                record = {"attempt": attempt, "plan": _plan_payload(plan), "verification": result.model_dump()}
                attempts.append(record)

                artifact = result.certificate or result.counterexample
                assert artifact is not None
                nviol = len(result.counterexample.violations) if result.counterexample else 0
                self.bus.publish("verifier.verdict",
                                 artifact.model_dump(exclude={"violations"}) | {"violated": [v.id for v in result.counterexample.violations] if result.counterexample else []},
                                 cycle_id=cycle_id, verdict=result.verdict,
                                 summary=f"A{attempt} {result.verdict} · {len(result.checks) - nviol}/{len(result.checks)} invariants hold · "
                                         f"{artifact.kind.lower()} {artifact.hash[:12]}…")

                if result.verdict == "APPROVED":
                    if origin != "scheduler":
                        audit_jobs.append(self._spawn_audit(record, result, None, cycle_id))
                    break

                self.metrics["blocked_attempts"] += 1
                for v in result.counterexample.violations:  # type: ignore[union-attr]
                    intercepted.add(v.subject)
                directives = self.verifier.feedback(plan, result, tel, directives, unsafe)
                record["feedback"] = directives.model_dump()
                audit_jobs.append(self._spawn_audit(record, result, directives, cycle_id))
                if attempt < MAX_ATTEMPTS:
                    self.metrics["replans"] += 1
                    self.bus.publish("verifier.feedback",
                                     {"from": "gemma_verifier", "to": "load_matching",
                                      "counterexample_hash": result.counterexample.hash,  # type: ignore[union-attr]
                                      "directives": directives.model_dump()},
                                     cycle_id=cycle_id,
                                     summary=f"counter-example → load_matching · re-plan A{attempt + 1} · "
                                             + "; ".join(directives.notes[:3]))

            assert plan is not None and result is not None
            certified = result.verdict == "APPROVED"
            carbon_report: Optional[CarbonReport] = None
            if certified:
                t0 = time.perf_counter()
                self.bus.publish("agent.handoff",
                                 {"from": "gemma_verifier", "to": "carbon_optimization",
                                  "certificate": result.certificate.model_dump() if result.certificate else None,
                                  "plan_hash": plan.hash()},
                                 cycle_id=cycle_id,
                                 summary=f"gemma_verifier → carbon_optimization · certified plan {plan.hash()[:12]}…")
                carbon_report = self.carbon.run(plan, forecast)
                timings["carbon_optimization"] = round((time.perf_counter() - t0) * 1000, 2)
                self.bus.publish("carbon.report", carbon_report.model_dump(), cycle_id=cycle_id,
                                 summary=f"{carbon_report.intensity_g_per_kwh:.0f} g/kWh · avoided "
                                         f"{carbon_report.avoided_t_per_h:,.1f} tCO₂/h · RE share "
                                         f"{carbon_report.renewable_share:.0%}")

            timings["total"] = round((time.perf_counter() - t_start) * 1000, 2)
            final_verdict = "CERTIFIED" if certified else "SAFE_HOLD"
            cycle = {
                "cycle_id": cycle_id,
                "tick": tel.tick,
                "origin": origin,
                "committed": commit and certified,
                "started_at": tel.timestamp,
                "unsafe_planner": unsafe,
                "telemetry": tel.model_dump(),
                "telemetry_hash": tel.hash(),
                "forecast": forecast.model_dump(),
                "attempts": attempts,
                "final": {
                    "verdict": final_verdict,
                    "attempts": len(attempts),
                    "replans": len(attempts) - 1,
                    "plan_id": plan.plan_id if certified else None,
                    "proof_hash": result.certificate.hash if result.certificate else None,
                },
                "carbon": carbon_report.model_dump() if carbon_report else None,
                "timings_ms": timings,
                "intercepted": sorted(intercepted),
            }

            if commit:
                self.metrics["cycles"] += 1
                if certified:
                    self.metrics["certified"] += 1
                    self.metrics["first_pass"] += int(len(attempts) == 1)
                    self.metrics["dr_events"] += int(bool(plan.shed_mw))
                    self.metrics["avoided_t_total"] = round(
                        self.metrics["avoided_t_total"] + carbon_report.avoided_t_per_h * SIM_STEP_H, 3)  # type: ignore[union-attr]
                    self.state.commit(plan, tel)
                    self.active = {"plan": plan, "forecast": forecast, "telemetry": tel, "carbon": carbon_report}
                    self._record_history(tel, plan, forecast, carbon_report)  # type: ignore[arg-type]
                else:
                    self.metrics["safe_holds"] += 1
                    self.state.tick += 1
                self.last_cycle = cycle
                self.bus.broadcast({"type": "cycle", "data": self.snapshot()})
            else:
                self.bus.broadcast({"type": "evaluation", "data": cycle})

        if await_audit and audit_jobs:
            await asyncio.wait(audit_jobs, timeout=self.gemma.timeout_s + 1)
        return cycle

    def _spawn_audit(self, record: dict[str, Any], result: VerificationResult,
                     directives: Optional[PlanningDirectives], cycle_id: str) -> asyncio.Task:
        prompt, signature, fallback = self.verifier.audit_prompt(result, directives)
        artifact = result.certificate or result.counterexample
        assert artifact is not None

        async def job() -> None:
            audit = await self.gemma.audit(prompt, signature, fallback)
            audit["artifact_hash"] = artifact.hash
            record["verification"]["gemma"] = audit
            self.bus.publish("gemma.audit", audit, cycle_id=cycle_id, artifact_hash=artifact.hash,
                             summary=f"Gemma audit of {artifact.kind.lower()} {artifact.hash[:12]}… "
                                     f"[{audit['source']}{', %.0f ms' % audit['latency_ms'] if audit['latency_ms'] else ''}]")
            self.bus.broadcast({"type": "audit", "data": audit})

        task = asyncio.create_task(job())
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return task

    def _record_history(self, tel: GridTelemetry, plan: DispatchPlan, forecast: ForecastReport, carbon: CarbonReport) -> None:
        self.history.append({
            "tick": tel.tick,
            "t": tel.timestamp,
            "demand": forecast.total_demand_mw,
            "served": round(sum(plan.served_mw.values()), 1),
            "wind": carbon.mix_mw["wind"],
            "solar": carbon.mix_mw["solar"],
            "battery": carbon.mix_mw["battery"],
            "peaker": carbon.mix_mw["peaker"],
            "shed": carbon.shed_mw,
            "intensity": carbon.intensity_g_per_kwh,
        })

    # ---- views -----------------------------------------------------------
    def _node_views(self) -> list[dict[str, Any]]:
        if self.active is None:
            return []
        plan: DispatchPlan = self.active["plan"]
        forecast: ForecastReport = self.active["forecast"]
        tel: GridTelemetry = self.active["telemetry"]
        intercepted = set(self.last_cycle["intercepted"]) if self.last_cycle else set()
        views = []
        for n in NODES:
            v: dict[str, Any] = {"id": n.id, "name": n.name, "kind": n.kind, "country": n.country,
                                 "x": n.x, "y": n.y, "capacity_mw": n.capacity_mw,
                                 "intercepted": n.id in intercepted}
            if n.kind == "load":
                served = plan.served_mw[n.id]
                k = served / n.capacity_mw
                theta = hotspot_c(k, tel.ambient_c)
                shed = plan.shed_mw.get(n.id, 0.0)
                demand = plan.demand_mw[n.id]
                df = n.nominal_hz * DROOP * (served - max(demand - shed, 1e-6)) / max(demand - shed, 1e-6)
                status = "critical" if theta >= 116 or shed > 0.1 * demand else "warning" if theta >= 105 or shed > 0 else "stable"
                v.update(value_mw=served, demand_mw=demand, shed_mw=shed, utilization=round(k, 4),
                         hotspot_c=round(theta, 1), freq_hz=round(n.nominal_hz + df, 3), nominal_hz=n.nominal_hz,
                         status=status)
            else:
                p = plan.dispatch_mw.get(n.id, 0.0)
                u = p / n.capacity_mw
                v.update(value_mw=p, available_mw=forecast.available_mw[n.id], utilization=round(u, 4),
                         carbon_g_kwh=n.carbon_g_kwh)
                if n.kind == "battery":
                    soc = self.state.soc[n.id]
                    v.update(soc=round(soc, 4), energy_mwh=n.energy_mwh)
                    status = "critical" if soc < 0.2 else "warning" if soc < 0.35 or u > 0.85 else "stable"
                elif n.kind == "peaker":
                    status = "critical" if u >= 0.95 else "warning" if u >= 0.6 else "stable"
                else:
                    cf = forecast.available_mw[n.id] / n.capacity_mw
                    v.update(capacity_factor=round(cf, 4))
                    status = "critical" if cf < 0.3 else "warning" if cf < 0.5 else "stable"
                    if n.kind == "solar":
                        v["irradiance_wm2"] = tel.nodes[n.id].irradiance_wm2
                    else:
                        v["wind_speed_ms"] = tel.nodes[n.id].wind_speed_ms
                v["status"] = status
            if n.id in intercepted and v["status"] == "stable":
                v["status"] = "warning"
            views.append(v)
        return views

    def _line_views(self) -> list[dict[str, Any]]:
        if self.active is None:
            return []
        plan: DispatchPlan = self.active["plan"]
        tel: GridTelemetry = self.active["telemetry"]
        intercepted = set(self.last_cycle["intercepted"]) if self.last_cycle else set()
        flow = {f.line_id: f.mw for f in plan.flows}
        out = []
        for line in LINES:
            d = tel.line_derate.get(line.id, 1.0)
            limit = line.capacity_mw * d
            p = flow.get(line.id, 0.0)
            u = p / limit if limit else (1.0 if p else 0.0)
            status = "tripped" if d == 0 else "critical" if u >= 0.97 else "warning" if u >= 0.8 else "stable"
            out.append({"id": line.id, "name": line.name, "tech": line.tech, "src": line.src, "dst": line.dst,
                        "capacity_mw": line.capacity_mw, "derate": d, "flow_mw": p, "utilization": round(u, 4),
                        "status": status, "intercepted": line.id in intercepted})
        return out

    def snapshot(self) -> dict[str, Any]:
        carbon: Optional[CarbonReport] = self.active["carbon"] if self.active else None
        forecast: Optional[ForecastReport] = self.active["forecast"] if self.active else None
        plan: Optional[DispatchPlan] = self.active["plan"] if self.active else None
        tel: Optional[GridTelemetry] = self.active["telemetry"] if self.active else None
        m = self.metrics
        faults = dict(self.state.faults)
        intercepting = bool(self.last_cycle and self.last_cycle["final"]["replans"])
        grid_status = ("SAFE_HOLD" if self.last_cycle and self.last_cycle["final"]["verdict"] == "SAFE_HOLD"
                       else "INTERCEPTING" if intercepting else "CONTINGENCY" if any(faults.values()) else "NOMINAL")
        return {
            "server_time": utcnow_iso(),
            "tick": self.state.tick,
            "grid_status": grid_status,
            "faults": faults,
            "scheduler_running": self.scheduler_running,
            "gemma": self.gemma.info,
            "pubsub": {"subscribers": self.bus.subscriber_count, "published": self.bus.published},
            "kpis": {
                "demand_mw": forecast.total_demand_mw if forecast else 0,
                "served_mw": round(sum(plan.served_mw.values()), 1) if plan else 0,
                "renewable_available_mw": forecast.renewable_available_mw if forecast else 0,
                "renewable_share": carbon.renewable_share if carbon else 0,
                "intensity_g_per_kwh": carbon.intensity_g_per_kwh if carbon else 0,
                "avoided_t_per_h": carbon.avoided_t_per_h if carbon else 0,
                "shed_mw": carbon.shed_mw if carbon else 0,
                "curtailed_mw": carbon.curtailed_renewable_mw if carbon else 0,
                "ambient_c": tel.ambient_c if tel else None,
            },
            "metrics": {
                **m,
                "first_pass_rate": round(m["first_pass"] / m["certified"], 4) if m["certified"] else None,
            },
            "nodes": self._node_views(),
            "lines": self._line_views(),
            "history": list(self.history),
            "last_cycle": self.last_cycle,
            "ledger": {"head": self.ledger.head, "length": self.ledger.seq, "chain_valid": self.ledger.verify_chain(),
                       "entries": self.ledger.tail()},
            "agent_network": AGENT_NETWORK_MANIFEST,
        }
