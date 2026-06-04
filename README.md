# AI Intel Agent for Real Estate

A real-estate market-intelligence system over nehnutelnosti.sk data. A timer-triggered
scraper feeds an **immutable medallion data lake** (bronze → silver → gold) in Azure
Blob; a **read-only Claude agent** writes an autonomous daily intelligence brief; and a
**read-only React SPA** (Bratislava borough choropleth hero + Trends / Yield /
Opportunity / Brief tabs) explores it. Neutral analyst lens — signals and caveats,
**no buy/sell advice**.

See [Project Charter.md](Project%20Charter.md) for the full plan, locked decisions, data
contract, and the phased build plan with current status.

## Status

Building **v1** (ingestion → production ASAP so 2 weeks of bronze accumulate for a
workshop). Foundation done: agent skeleton + Blob I/O ported, and the data contract
([backend/realestate/schemas.py](backend/realestate/schemas.py)) is locked. Next:
payload recon (Task 0) → scraper + ledger + bronze.

## What's in the box

| Layer | Technology |
| --- | --- |
| Ingestion | Timer-triggered scraper (requests + JSON-LD parse) → bronze Parquet, ledger-driven |
| Storage | Azure Blob (one `realestate` container), Parquet via Polars/pyarrow |
| Analytics | Polars silver/gold rebuild (segment_weekly, yield_segment; city + okres) |
| AI agent | Claude (Sonnet), read-only, daily brief only — skeleton reused from the portfolio project |
| Backend | Python Flex Consumption Function App (HTTP read API + 2 timer triggers) |
| Frontend | React + Vite SPA → Azure Static Web Apps (Free), choropleth via GeoJSON |
| IaC / CI | Bicep + GitHub Actions (infra / backend / frontend deploy) |

## Layout

```
backend/
  function_app.py            # HTTP routes + timers: scrape_next_area (20 min) + build_and_brief (daily)
  realestate/
    schemas.py               # DATA CONTRACT — Blob paths + Polars schemas + scrape enums  ✅
    scraper.py               # sweep one (locality, deal) → bronze slice
    ledger.py                # self-healing day→unit ledger (date -> 42 units -> status)
    build.py                 # silver rebuild (+ global dedup) + gold build
    data.py                  # read-only query layer for the agent tools + HTTP API
  agent/                     # ported from portfolio, adapted read-only
    loop.py  prompts.py  tools.py
  storage/blobs.py           # Blob <-> Parquet/JSON I/O
  host.json, requirements.txt, .funcignore, local.settings.json.example
infra/main.bicep             # Storage (+ realestate container), Function App (+CORS), Free Static Web App
frontend/src/                # api.js seam (stub data when no API) + Map/Trends/Yield/Opportunity/Brief tabs
.github/workflows/           # infra.yml / deploy.yml / deploy-frontend.yml
```

## How ingestion works

A timer fires **every 20 minutes within 06:00–22:00 Bratislava** (set via
`WEBSITE_TIME_ZONE`). Each tick scrapes **one `(locality, deal)` unit** — there are
21 localities (17 BA boroughs + 4 corridor) × 2 deals = **42 units/day**, comfortably
inside the 48 daily slots. A JSON **ledger** (`meta/ingest_ledger.json`, keyed by UTC
date → unit → status) tracks progress; a new date key auto-appears each day (the reset),
and a failed/missed unit stays `pending` and retries next tick — self-healing, no burst.
A separate **daily timer (~22:30)** rebuilds silver/gold and runs the agent brief.

**All data is stored UTC**; the front-end renders Bratislava time. Bronze is immutable
and Hive-partitioned (`bronze/type=byty/deal=*/date=*/<slug>.parquet`); global dedup and
quality flags happen at the silver rebuild. Politeness (jittered delays, browser headers,
robots, stop-on-403/429) is load-bearing — getting blocked kills the project.

## Backend endpoints

All routes are served under `/api`.

| Endpoint | Method | Description |
| --- | --- | --- |
| `/health` | GET | Liveness probe (deploy smoke test) |
| `/realestate/bootstrap` | GET | Enum lists, available weeks, coverage |
| `/realestate/segments` | GET | `segment_weekly` rows (grain/area/category/deal/week) |
| `/realestate/yield` | GET | `yield_segment` rows |
| `/realestate/trend` | GET | time-series for area × category × metric |
| `/realestate/geo` | GET | borough GeoJSON + selected metric per area |
| `/realestate/brief` | GET | latest (or by-date) daily brief + token/cost |
| `/scrape-realestate` | POST | manual spot-check of one `?locality=&deal=` |

## One-time setup

### 1. Service principal → GitHub secret

```bash
az ad sp create-for-rbac --name "gh-intelreal" --role Contributor \
  --scopes /subscriptions/<SUBSCRIPTION_ID> --sdk-auth
```
Save the JSON as the `AZURE_CREDENTIALS` Actions secret.

### 2. Repository secrets — Settings → Secrets and variables → Actions → Secrets

| Secret | Used by |
| --- | --- |
| `AZURE_CREDENTIALS` | all Azure workflows (login) — **required** |
| `CLAUDE_API_KEY` | infra (injected into the Function App) — **required** (the agent uses it) |

### 3. Repository variables — same screen → Variables

| Variable | Value |
| --- | --- |
| `AZURE_RESOURCE_GROUP` | e.g. `rg-intelreal-dev` |
| `AZURE_LOCATION` | a Static Web Apps region, e.g. `westeurope` |
| `AZURE_BASE_NAME` | `intelreal` *(also set `baseName` in `infra/main.parameters.json`)* |

## Deployment order

Run **in order the first time** (Actions → *Run workflow*):

1. **Infra (Bicep)** — creates the resource group, Function App, Static Web App, the
   `realestate` Blob container, and wires CORS + `CLAUDE_API_KEY` + `WEBSITE_TIME_ZONE`.
2. **Deploy (Function code)** — deploys `backend/` and smoke-tests `GET /api/health`.
3. **Deploy Frontend** — builds `frontend` against the live API and uploads to the SWA.

After that, pushes to `main` trigger each pipeline by changed path (`infra/**`,
`backend/**`, `frontend/**`).

## Local development

### Backend

```powershell
cd backend
python -m venv .venv; .venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item local.settings.json.example local.settings.json   # then fill in keys
func start    # Azure Functions Core Tools v4
# GET http://localhost:7071/api/health -> {"status": "ok"}
```

> Put real keys only in the gitignored `local.settings.json` — never in the example.
> `local.settings.json` needs `AzureWebJobsStorage` (Blob I/O) and `CLAUDE_API_KEY`.

### Frontend

```powershell
cd frontend
npm install
npm run dev          # runs on stub data with no backend
```

Point the dev server at a real backend via `frontend/.env`:

```
VITE_API_BASE=https://<func-host>/api     # call the deployed API (CORS set for the SWA)
# or proxy /api to a local backend to avoid CORS during dev:
VITE_API_PROXY=http://localhost:7071
```

## Notes

- **Auth:** the frontend is a public URL with no authentication (by design).
- **Read-only:** the SPA and the agent never write or transact; the agent issues no
  buy/sell advice (neutral monitoring lens, locked).
- **Cold start:** Flex Consumption has a brief cold start.
