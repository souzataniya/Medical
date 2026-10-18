# SynapseGrid: technical reference

Implementation notes for the code in this directory. For the project overview, results and deployment, see the [top-level README](../README.md).

All data is synthetic, and nothing here controls real equipment.

## Files

| File | Responsibility |
|---|---|
| `grid.py` | Topology, declared parameters and emission factors, telemetry models, seeded simulator, fixed incident scenario |
| `optimizer.py` | OR-Tools GLOP DC optimal power flow (one 15-min interval); deterministic unsafe-proposal generator |
| `verifier.py` | Standard library only. Input validation, DC power-flow recomputation, constraint checks, violation records, replay CLI |
| `gemma.py` | Gemma client (Google AI Studio or OpenAI-compatible endpoint), output schema, call status |
| `agents.py` | Agent contracts, execution trace, recovery loop (≤ 4 attempts), audit ledger, in-process event bus, scenarios |
| `evaluation.py` | Randomized fault suite; uses a separate OR-Tools feasibility check as the reference |
| `main.py` | FastAPI app: dashboard, REST API, SSE stream, simulated dispatch loop |
| `static/index.html` | Dashboard (single file) |

## Optimizer (`optimizer.py`)

- **Variables:** generator output `p_g`, battery discharge `d_b` / charge `c_b`, served load `s_l`, bus angle `θ_n`, line flow `f_k`.
- **Bus balance:** `Σp + Σ(d − c) − Σs = Σf_out − Σf_in`, with `f_k = (θ_from − θ_to) / x_k · S_base`.
- **Hard limits:**
  - `0 ≤ p ≤ available`, and diesel ramp ±600/1000 kW per interval
  - battery power within its rating
  - battery SoC after 15 min within [0.10, 0.95] (95 % one-way efficiency)
  - `|f_k| ≤ C_k · derate_k`
  - critical loads fully served
- **Objective ($/h):**
  - diesel cost ($320/MWh) + carbon price ($85/t × 0.70 t/MWh)
  - + battery wear ± stored-energy value
  - + VOLL ($2,000/MWh) × priority weight × unserved non-critical load
- **Infeasibility:** if critical loads can't be served, the solve is infeasible and the cycle is reported `FAILED`. A relaxed LP then measures the critical shortfall. Gemma-protected loads are hard in the planner only; if they cause infeasibility, those protections are dropped (and recorded) before failure is declared.
- **Margins:** the planner keeps 1 kW of headroom on each limit, so rounding to 0.1 kW never crosses a bound.

## Verifier (`verifier.py`)

The verifier ignores the planner's claimed flows. A proposal may contain only `generation_kw`, `battery_kw` and `served_kw`; any other key is a violation. The verifier sums the injections at each bus and solves the DC power flow itself (Gaussian elimination, slack bus `B1`). Lines with derate 0 are treated as out of service.

| Family | Check |
|---|---|
| `INPUT_VALID` | schema, exact id sets, finite numbers, value ranges |
| `POWER_BALANCE` | `|Σ generation + Σ battery − Σ served| ≤ 1 kW` |
| `LINE_CAPACITY` | `|f| ≤ C · derate` |
| `GEN_LIMIT` / `GEN_RAMP` | `0 ≤ P ≤ P_available`; `−R_down ≤ P − P_prev ≤ R_up` (skipped if the unit is unavailable) |
| `BESS_POWER` / `BESS_SOC` | `|P| ≤ P_rated`; SoC after the interval within limits |
| `LOAD_BOUNDS` / `CRITICAL_LOAD` | `0 ≤ served ≤ demand`; critical `served ≥ demand − 0.5 kW` |
| `NETWORK` | in-service network connected |

Violation fields: `constraint_id, family, element, element_type, observed, limit, expected, unit, excess, severity, message`.

## Gemma (`gemma.py`)

- **Priorities.** Turns an operator note into JSON with `incident_summary`, `protected_loads` (≤ 4), `load_priority` (1–5) and `rationale`.
  - The output is validated with a Pydantic model that forbids extra keys and accepts only known non-critical load ids.
  - Any validation error rejects the whole output.
- **Briefing.** A plain-text summary of the last verification, built from the recorded facts.
- **Status.** Each call ends as `inference`, `rejected`, `unavailable` (after 1 retry) or `not_configured`. Anything other than `inference` falls back to the default priorities, labelled as defaults.
- **No influence on safety.** Priorities only affect optimizer weights and protections; the verifier never reads them.

## Audit records

Each verified proposal produces a record containing:
- the verification case (network, parameters, telemetry-derived state, proposal)
- the proposal source
- the planner result
- the versions (optimizer, verifier, constraint set, prompt)
- every check result and violation
- the impact metrics

The hashes:
- `record_hash = SHA-256(canonical JSON)`: sorted keys, floats rounded to 4 decimal places.
- `chain_hash = SHA-256(prev_chain_hash + record_hash)`.

Timestamps and IDs stay outside the record, so identical inputs produce identical hashes.

The hash shows a record hasn't changed. It doesn't show that the inputs were true or that the plan is safe outside the model. To re-check a record:

```bash
curl -s localhost:8080/api/audit/<record_hash> > record.json
python verifier.py record.json   # exit 0 if the verdict and check digest reproduce
```

## Running

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
python evaluation.py --n 300 --seed 7   # results: EVALUATION.md
python main.py                          # http://localhost:8080, API docs at /docs
```

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | 8080 | HTTP port |
| `TICK_SECONDS` | 3 | Simulated dispatch loop interval |
| `GEMMA_API_KEY` / `GOOGLE_API_KEY` | – | Google AI Studio key (selects that provider) |
| `GEMMA_BASE_URL` | – | OpenAI-compatible endpoint, e.g. Ollama `http://localhost:11434/v1` (takes precedence) |
| `GEMMA_MODEL` | `gemma-4-26b-a4b-it` / `gemma3:4b` | Model id (list what your key can use: `GET /v1beta/models`) |
| `GEMMA_THINKING_LEVEL` | `MINIMAL` | Thinking level for AI Studio thinking models; empty to omit |
| `GEMMA_TIMEOUT_S` | 20 | Per-call timeout |
| `LOG_LEVEL` | INFO | Logging |

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Versions, Gemma configuration, ledger chain status |
| GET | `/api/state` | Dashboard snapshot |
| GET | `/api/stream` | Server-sent events |
| POST | `/api/evaluate-load-shift` | Run a cycle; accepts telemetry overrides, `inject_unsafe`, an external `proposal`, `commit` |
| POST | `/api/inject-grid-fault` | `solar_loss`, `diesel_outage`, `unsafe_proposal`, `all_clear` |
| POST | `/api/scenarios/incident` | Fixed solar-loss incident |
| POST | `/api/scenarios/infeasible` | Fixed infeasible case (not committed) |
| POST | `/api/gemma/priorities`, `/api/gemma/briefing` | Gemma tasks |
| POST | `/api/priorities/reset` | Restore default priorities |
| GET | `/api/ledger`, `/api/audit/{hash}`, `/api/replay/{hash}`; POST `/api/replay` | Audit records and replay |
| POST | `/api/evaluation` | Randomized suite (`n` ≤ 1000) |
| POST | `/api/scheduler`, `/api/reset` | Pause/resume the loop, reset the simulator |

## Limitations

- **Model scope:** lossless DC power flow over one interval. No voltage, reactive power, protection, stability, unit commitment or look-ahead.
- **Assumed numbers:** costs and emission factors are assumptions (see `grid.py`).
- **In-memory state:** the ledger and simulator state live in memory, so the app runs as a single instance.
- **Gemma:** verified live once (18 Oct 2026, `gemma-4-26b-a4b-it` via Google AI Studio): priorities ≈ 6 s, briefing ≈ 4–6 s, one transient HTTP 500 recovered by the retry. The automated tests use mocked HTTP responses.
- **Docker image:** not built in this environment.
