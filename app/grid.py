"""
Synthetic island-community microgrid: topology, declared assumptions, and a seeded telemetry simulator.

ALL DATA IN THIS MODULE IS SYNTHETIC. The network is illustrative (loosely inspired by remote
Pacific-island microgrids that are moving from diesel to solar + storage). It does not represent
any real utility, and nothing in this project controls real electrical equipment.

Units: kW, kWh, hours. One dispatch interval = 15 minutes.
"""

from __future__ import annotations

import math
import random
from dataclasses import asdict, dataclass
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

# ─────────────────────────────── Declared parameters ────────────────────────────────

CONSTRAINT_SET_VERSION = "microgrid-dc-v1"
DT_H = 0.25                       # dispatch interval (15 min)
SOC_MIN = 0.10
SOC_MAX = 0.95
BESS_ETA = 0.95                   # one-way efficiency (charge and discharge)
S_BASE_KW = 1000.0                # per-unit base for DC power flow
SLACK_BUS = "B1"
BALANCE_TOL_KW = 1.0              # rounding tolerance for system power balance
CRITICAL_TOL_KW = 0.5

EMISSION_FACTORS_KG_PER_KWH = {   # operational (stack) emissions only; lifecycle excluded
    "diesel": 0.70,               # assumption: typical small diesel genset 0.7–0.8 kgCO2/kWh
    "solar": 0.0,
    "wind": 0.0,
    "battery": 0.0,               # battery energy was charged from on-island sources
}
CARBON_PRICE_USD_PER_T = 85.0     # assumption used inside the optimizer objective
DIESEL_COST_USD_PER_MWH = 320.0   # assumption: delivered island diesel fuel + O&M
BESS_DEGRADATION_USD_PER_MWH = 12.0
BESS_STORED_VALUE_USD_PER_MWH = 140.0   # value of keeping energy in storage (myopic dispatch proxy)
VOLL_USD_PER_MWH = 2000.0         # value of lost load for priority weight 1
PRIORITY_WEIGHTS = {1: 1.0, 2: 2.0, 3: 4.0, 4: 8.0, 5: 16.0}


# ─────────────────────────────── Topology ────────────────────────────────

@dataclass(frozen=True)
class Bus:
    id: str
    name: str
    x: float
    y: float


@dataclass(frozen=True)
class Line:
    id: str
    frm: str
    to: str
    x_pu: float           # series reactance (per unit on S_BASE_KW)
    cap_kw: float
    name: str


@dataclass(frozen=True)
class Asset:
    id: str
    name: str
    kind: str             # solar | wind | diesel | battery
    bus: str
    cap_kw: float         # nameplate (gen) or power rating (battery)
    energy_kwh: float = 0.0
    ramp_up_kw: float = 0.0
    ramp_down_kw: float = 0.0
    x: float = 0.0
    y: float = 0.0


@dataclass(frozen=True)
class Load:
    id: str
    name: str
    bus: str
    base_kw: float
    critical: bool
    priority: int         # 1..5 default shedding priority for non-critical loads (5 = protect most)
    x: float = 0.0
    y: float = 0.0


BUSES: tuple[Bus, ...] = (
    Bus("B1", "Harbour Powerhouse", 250, 360),
    Bus("B2", "Hillside Solar Park", 470, 170),
    Bus("B3", "Clinic & Water Works", 690, 250),
    Bus("B4", "Township Centre", 650, 470),
    Bus("B5", "Market & Fishing Port", 380, 540),
    Bus("B6", "Ridge Wind Farm", 860, 430),
)

LINES: tuple[Line, ...] = (
    Line("L12", "B1", "B2", 0.10, 1500, "Harbour–Hillside 11 kV"),
    Line("L13", "B1", "B3", 0.12, 900, "Harbour–Clinic 11 kV"),
    Line("L23", "B2", "B3", 0.06, 1800, "Hillside–Clinic 11 kV"),
    Line("L24", "B2", "B4", 0.07, 1600, "Hillside–Township 11 kV"),
    Line("L34", "B3", "B4", 0.09, 700, "Clinic–Township 11 kV"),
    Line("L45", "B4", "B5", 0.08, 900, "Township–Port 11 kV"),
    Line("L15", "B1", "B5", 0.12, 800, "Harbour–Port 11 kV"),
    Line("L46", "B4", "B6", 0.05, 1000, "Township–Ridge 11 kV"),
)

ASSETS: tuple[Asset, ...] = (
    Asset("PV-HILL", "Hillside Solar PV", "solar", "B2", 3200, x=470, y=70),
    Asset("WT-RIDGE", "Ridge Wind Turbines", "wind", "B6", 900, x=950, y=350),
    Asset("DG-HARB", "Harbour Diesel Genset", "diesel", "B1", 2000, ramp_up_kw=600, ramp_down_kw=1000, x=110, y=300),
    Asset("BESS-CLN", "Clinic Battery", "battery", "B3", 1000, energy_kwh=2000, x=830, y=170),
    Asset("BESS-PRT", "Port Battery", "battery", "B5", 750, energy_kwh=1500, x=230, y=610),
)

LOADS: tuple[Load, ...] = (
    Load("LD-CLINIC", "Health Clinic", "B3", 300, True, 5, x=800, y=290),
    Load("LD-WATER", "Water Pumping & Desal", "B3", 450, True, 5, x=700, y=130),
    Load("LD-TELECOM", "Telecom & Emergency Radio", "B1", 60, True, 5, x=130, y=440),
    Load("LD-SCHOOL", "School (cyclone shelter)", "B4", 220, False, 3, x=780, y=545),
    Load("LD-HOMES", "Residential Feeders", "B4", 1050, False, 2, x=600, y=600),
    Load("LD-COLD", "Fisheries Cold Storage", "B5", 320, False, 2, x=470, y=625),
    Load("LD-MARKET", "Market & Commercial", "B5", 600, False, 1, x=270, y=470),
)

BUS_BY_ID = {b.id: b for b in BUSES}
LINE_BY_ID = {line.id: line for line in LINES}
ASSET_BY_ID = {a.id: a for a in ASSETS}
LOAD_BY_ID = {ld.id: ld for ld in LOADS}
GENERATORS = tuple(a for a in ASSETS if a.kind != "battery")
BATTERIES = tuple(a for a in ASSETS if a.kind == "battery")
CRITICAL_LOADS = tuple(ld for ld in LOADS if ld.critical)
NONCRITICAL_LOADS = tuple(ld for ld in LOADS if not ld.critical)


def network_spec() -> dict[str, Any]:
    """Self-contained network description embedded in every verification case."""
    return {
        "buses": [b.id for b in BUSES],
        "slack_bus": SLACK_BUS,
        "lines": [{"id": ln.id, "from": ln.frm, "to": ln.to, "x_pu": ln.x_pu, "cap_kw": ln.cap_kw} for ln in LINES],
        "assets": [{"id": a.id, "kind": a.kind, "bus": a.bus, "cap_kw": a.cap_kw, "energy_kwh": a.energy_kwh,
                    "ramp_up_kw": a.ramp_up_kw, "ramp_down_kw": a.ramp_down_kw} for a in ASSETS],
        "loads": [{"id": ld.id, "bus": ld.bus, "critical": ld.critical} for ld in LOADS],
    }


def parameters_spec() -> dict[str, Any]:
    return {
        "dt_h": DT_H, "soc_min": SOC_MIN, "soc_max": SOC_MAX, "bess_eta": BESS_ETA, "s_base_kw": S_BASE_KW,
        "balance_tol_kw": BALANCE_TOL_KW, "critical_tol_kw": CRITICAL_TOL_KW,
    }


def topology_for_ui() -> dict[str, Any]:
    return {
        "buses": [asdict(b) for b in BUSES],
        "lines": [asdict(ln) for ln in LINES],
        "assets": [asdict(a) for a in ASSETS],
        "loads": [asdict(ld) for ld in LOADS],
    }


# ─────────────────────────────── Telemetry ────────────────────────────────

class Telemetry(BaseModel):
    """One interval of (simulated) measurements. Every field is in declared units."""

    simulated: bool = True
    tick: int = 0
    irradiance_wm2: float = Field(ge=0, le=1400)
    wind_speed_ms: float = Field(ge=0, le=60)
    demand_kw: dict[str, float]
    soc: dict[str, float]
    line_derate: dict[str, float]
    diesel_available: bool = True
    prev_output_kw: dict[str, float] = Field(default_factory=dict)

    @field_validator("demand_kw")
    @classmethod
    def _demand(cls, v: dict[str, float]) -> dict[str, float]:
        if set(v) != set(LOAD_BY_ID):
            raise ValueError("demand_kw must contain exactly the load ids")
        if any(not (0 <= x <= 10_000) or math.isnan(x) for x in v.values()):
            raise ValueError("demand_kw values must be within [0, 10000] kW")
        return v

    @field_validator("soc")
    @classmethod
    def _soc(cls, v: dict[str, float]) -> dict[str, float]:
        if set(v) != {b.id for b in BATTERIES}:
            raise ValueError("soc must contain exactly the battery ids")
        if any(not (0 <= x <= 1) for x in v.values()):
            raise ValueError("soc values must be within [0, 1]")
        return v

    @field_validator("line_derate")
    @classmethod
    def _derate(cls, v: dict[str, float]) -> dict[str, float]:
        if set(v) != set(LINE_BY_ID):
            raise ValueError("line_derate must contain exactly the line ids")
        if any(not (0 <= x <= 1) for x in v.values()):
            raise ValueError("line_derate values must be within [0, 1]")
        return v


class TelemetryOverrides(BaseModel):
    """Partial overrides accepted by the API; omitted fields come from the simulator."""

    irradiance_wm2: Optional[float] = Field(default=None, ge=0, le=1400)
    wind_speed_ms: Optional[float] = Field(default=None, ge=0, le=60)
    demand_kw: dict[str, float] = Field(default_factory=dict)
    soc: dict[str, float] = Field(default_factory=dict)
    line_derate: dict[str, float] = Field(default_factory=dict)
    diesel_available: Optional[bool] = None

    @field_validator("demand_kw")
    @classmethod
    def _d(cls, v: dict[str, float]) -> dict[str, float]:
        for k, x in v.items():
            if k not in LOAD_BY_ID or not 0 <= x <= 10_000:
                raise ValueError(f"demand_kw: invalid entry {k}={x}")
        return v

    @field_validator("soc")
    @classmethod
    def _s(cls, v: dict[str, float]) -> dict[str, float]:
        for k, x in v.items():
            if k not in {b.id for b in BATTERIES} or not 0 <= x <= 1:
                raise ValueError(f"soc: invalid entry {k}={x}")
        return v

    @field_validator("line_derate")
    @classmethod
    def _l(cls, v: dict[str, float]) -> dict[str, float]:
        for k, x in v.items():
            if k not in LINE_BY_ID or not 0 <= x <= 1:
                raise ValueError(f"line_derate: invalid entry {k}={x}")
        return v


def wind_power_fraction(v: float, cut_in: float = 3.0, rated: float = 12.0, cut_out: float = 25.0) -> float:
    if v < cut_in or v >= cut_out:
        return 0.0
    if v >= rated:
        return 1.0
    return (v ** 3 - cut_in ** 3) / (rated ** 3 - cut_in ** 3)


# Deterministic incident scenario used by the demo and the tests.
SCENARIO_BASELINE = Telemetry(
    tick=0,
    irradiance_wm2=850.0,
    wind_speed_ms=9.5,
    demand_kw={ld.id: ld.base_kw for ld in LOADS},
    soc={"BESS-CLN": 0.62, "BESS-PRT": 0.58},
    line_derate={ln.id: 1.0 for ln in LINES},
    diesel_available=True,
    prev_output_kw={"DG-HARB": 150.0},
)
SOLAR_LOSS_FRACTION = 0.75  # sudden cloud front removes 75 % of PV output


class GridSimulator:
    """Seeded generator of simulated telemetry plus the live (simulated) battery state."""

    FAULTS = ("solar_loss", "diesel_outage", "unsafe_proposal")

    def __init__(self, seed: int = 20261018) -> None:
        self.seed = seed
        self.reset()

    def reset(self) -> None:
        self.rng = random.Random(self.seed)
        self.tick = 0
        self.soc = dict(SCENARIO_BASELINE.soc)
        self.prev_output_kw: dict[str, float] = dict(SCENARIO_BASELINE.prev_output_kw)
        self.faults = {f: False for f in self.FAULTS}

    def sample(self, overrides: Optional[TelemetryOverrides] = None) -> Telemetry:
        t, rng = self.tick, self.rng
        irr = 850 + 60 * math.sin(t / 17) + rng.uniform(-25, 25)
        if self.faults["solar_loss"]:
            irr *= (1 - SOLAR_LOSS_FRACTION) + rng.uniform(-0.03, 0.03)
        wind = 9.5 + 1.2 * math.sin(t / 9 + 1.3) + rng.uniform(-0.6, 0.6)
        demand = {}
        for i, ld in enumerate(LOADS):
            demand[ld.id] = round(ld.base_kw * (1 + 0.05 * math.sin(t / 13 + i)) * (1 + rng.uniform(-0.02, 0.02)), 1)
        data = {
            "tick": t,
            "irradiance_wm2": round(max(irr, 0.0), 1),
            "wind_speed_ms": round(max(wind, 0.0), 2),
            "demand_kw": demand,
            "soc": {k: round(v, 4) for k, v in self.soc.items()},
            "line_derate": {ln.id: 1.0 for ln in LINES},
            "diesel_available": not self.faults["diesel_outage"],
            "prev_output_kw": dict(self.prev_output_kw),
        }
        if overrides is not None:
            apply_overrides(data, overrides)
        return Telemetry(**data)

    def commit(self, soc_next: dict[str, float], output_kw: dict[str, float]) -> None:
        """Advance the simulated clock with the verifier-computed SoC of the dispatched plan."""
        # One live tick represents 5 simulated minutes; the plan is verified over a 15-minute interval,
        # so we apply one third of that interval's SoC change.
        for b in BATTERIES:
            self.soc[b.id] = min(max(self.soc[b.id] + (soc_next[b.id] - self.soc[b.id]) / 3, 0.0), 1.0)
        self.prev_output_kw = {"DG-HARB": output_kw.get("DG-HARB", 0.0)}
        self.tick += 1


def apply_overrides(data: dict[str, Any], ov: TelemetryOverrides) -> dict[str, Any]:
    if ov.irradiance_wm2 is not None:
        data["irradiance_wm2"] = ov.irradiance_wm2
    if ov.wind_speed_ms is not None:
        data["wind_speed_ms"] = ov.wind_speed_ms
    data["demand_kw"] = {**data["demand_kw"], **ov.demand_kw}
    data["soc"] = {**data["soc"], **ov.soc}
    data["line_derate"] = {**data["line_derate"], **ov.line_derate}
    if ov.diesel_available is not None:
        data["diesel_available"] = ov.diesel_available
    return data
