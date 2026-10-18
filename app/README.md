# SynapseGrid ⚡ Deterministic Multi-Agent Grid Load Balancer & Carbon-Router

A live mission-control demo of an open architecture for keeping renewable-heavy JAPAC grids stable.
Three agents plan the dispatch. A **Gemma-powered Safety Verification Engine** sits between the planner
and the carbon optimiser. It turns every proposed load shift into physical invariants and blocks unsafe plans
with a SHA-256 counter-example. The planner then has to re-plan inside the verified limits.

> **Gemma reasons, math decides.** Safety verdicts are computed deterministically (same plan + telemetry →
> same verdict → same hash). Gemma audits every counter-example and certificate, then writes the diagnosis and
> re-planning directive. An LLM never gets the final say on whether a transformer is overheating.

```
Forecasting ──► Load-Matching ──► Gemma Safety Verifier ──► Carbon-Optimization
                      ▲                    │
                      └── counter-example ─┘   re-plan loop (≤ 5 attempts) → hash-chained ledger
```

## What the verifier checks (≈39 invariants per proposal)

| Family | Invariant | Source |
|---|---|---|
| `LINE_THERMAL` | \|P_line\| ≤ C · δ (live derate, e.g. HVDC pole trip) | SCADA line status |
| `GEN_ENVELOPE` | P ≤ P_avail (forecast from irradiance / wind power curve) | Forecasting agent |
| `GEN_RAMP` | P − P₀ ≤ R_up per 15-min interval (gas peakers) | previous certified dispatch |
| `BESS_POWER` / `BESS_SOC_FLOOR` | P ≤ P_rated, P·Δt/E ≤ SoC − 10 % | BESS telemetry |
| `XFMR_HOTSPOT` | IEEE C57.91 hot-spot θ_A + Δθ_TO·((K²R+1)/(R+1))ⁿ + Δθ_H·K²ᵐ ≤ 120 °C | ambient temperature |
| `FREQ_BAND` | \|f₀·R·(S − D_connected)/D_connected\| ≤ 0.1 Hz per control area | droop model |

Every proposal is also exported as **SMT-LIB2 (QF_LRA)**, which you can view in the Proof Inspector, and is sealed into an
append-only SHA-256 hash chain (certificate or counter-example).

## Project layout

```
app/
├── main.py            FastAPI app: dashboard, REST API, SSE stream, autonomous scheduler
├── agents.py          Agents, Gemma client, verifier, Pub/Sub bus, hash ledger, grid simulator
├── static/index.html  Single-file Tailwind + Lucide + Chart.js mission-control dashboard (Jinja2)
├── tests/             pytest suite (agents + HTTP)
├── Dockerfile         Cloud Run image (non-root, $PORT=8080, healthcheck)
└── requirements.txt
```

## Run locally

```bash
cd app
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py                      # → http://localhost:8080
# optional: pip install pytest && pytest -q
```

### Connect Gemma (optional, the demo also runs fully offline)

| Mode | Env vars |
|---|---|
| Google AI Studio (hosted Gemma) | `GEMMA_API_KEY=<key>` (or `GOOGLE_API_KEY`), optional `GEMMA_MODEL=gemma-3-27b-it` |
| Local Ollama | `ollama pull gemma3:4b` then `GEMMA_BASE_URL=http://localhost:11434/v1 GEMMA_MODEL=gemma3:4b` |
| vLLM / Gemma on Cloud Run GPU | `GEMMA_BASE_URL=https://<service>/v1 GEMMA_MODEL=<served-name>` (+ `GEMMA_API_KEY` if protected) |

With no configuration the header shows **Gemma · offline audit**, and audits fall back to a deterministic template.
If Gemma fails or times out (`GEMMA_TIMEOUT_S`, default 8), the app degrades the same way and retries after 30 s.

Other settings: `TICK_SECONDS` (default 2.5), `PORT` (default 8080), `LOG_LEVEL`.

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Mission-control dashboard |
| `POST` | `/api/evaluate-load-shift` | Run the full DAG on (optionally overridden) telemetry; returns proof certificate and/or counter-example traces, Gemma audits, carbon report |
| `POST` | `/api/inject-grid-fault` | `{"fault": "cloud_cover" \| "grid_surge" \| "unsafe_overload" \| "all_clear", "active": true}` |
| `GET` | `/health` | Container health check |
| `GET` | `/api/state` · `/api/ledger` · `/api/agent-network` | Snapshot, hash chain, agent manifest |
| `GET` | `/api/stream` | SSE mirror of the Pub/Sub bus and per-cycle snapshots |
| `POST` | `/api/scheduler` · `/api/reset` | Pause/resume autopilot, reset seeded grid |

```bash
curl -s -X POST localhost:8080/api/evaluate-load-shift -H 'content-type: application/json' -d '{
  "telemetry": {"irradiance_wm2": {"SL-NTA": 140, "SL-QLD": 160},
                "demand_mw": {"LD-TKY": 2450}, "line_derate": {"L-CHG-SEL": 0.0}, "ambient_c": 37.5},
  "unsafe_overload": true, "commit": false }' | jq '{verdict, attempts, cert: .proof_certificate.hash}'
```

Interactive OpenAPI docs: `http://localhost:8080/docs`.

## Deploy to Google Cloud Run

```bash
gcloud config set project <PROJECT_ID>
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com

# (optional) store the Gemma key as a secret
printf '%s' "<GEMMA_API_KEY>" | gcloud secrets create gemma-api-key --data-file=-
gcloud secrets add-iam-policy-binding gemma-api-key \
  --member="serviceAccount:$(gcloud projects describe <PROJECT_ID> --format='value(projectNumber)')-compute@developer.gserviceaccount.com" \
  --role=roles/secretmanager.secretAccessor

cd app
gcloud run deploy synapsegrid \
  --source . \
  --region asia-northeast1 \
  --allow-unauthenticated \
  --port 8080 \
  --cpu 1 --memory 512Mi \
  --min-instances 1 --max-instances 1 \
  --no-cpu-throttling \
  --timeout 3600 \
  --set-secrets GEMMA_API_KEY=gemma-api-key:latest
```

Why these flags:
- `--max-instances 1`: grid state, the ledger and the Pub/Sub bus are in-process, so every viewer must hit the same instance.
- `--no-cpu-throttling` and `--min-instances 1`: the autonomous dispatch loop keeps ticking between requests.
- `--timeout 3600`: long-lived SSE connections. The browser reconnects on its own and falls back to polling if needed.

Leave out `--set-secrets` to deploy in offline-audit mode.

## 90-second demo script

1. **Nominal.** All arcs are green, renewable share is about 90 %, and every cycle is certified on the first attempt. Point out the hash chain growing.
2. **Inject Unsafe Overload.** The planner over-commits ×1.38. About 20 invariants go red, the map banner shows the interception, and the Proof Inspector shows
   `A1 ✗ → A2 ✓`. Open **Counter-example** and **SMT-LIB** to show the witness and the formal model.
3. **Heatwave Surge + HVDC Trip.** The planner still routes 700 MW over a tripped line and pushes Tokyo's transformer to about 123 °C.
   The verifier blocks it, caps the line and transformer, then authorises controlled demand response. Certified on attempt 3.
4. **Sudden Cloud Cover.** Peakers can't ramp fast enough, so GEN_RAMP and then FREQ_BAND violations fire. Watch demand response shrink over the next
   few intervals as peakers ramp within their limits (the self-healing is visible in the chart).
5. **Run Verified Evaluation.** Shows an on-demand Gemma attestation plus the certificate hash.
