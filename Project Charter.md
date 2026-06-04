# Project Charter — AI Intel Agent for Real Estate

Buildable plan for a real-estate market-intelligence system over nehnutelnosti.sk
data: a daily scraper feeding an immutable medallion data lake, a read-only analytics
agent that writes an autonomous daily brief, and a read-only React SPA (choropleth
hero) to explore it. Companion to the *AI Portfolio Manager* project, whose agent
skeleton (`agent/loop.py`) this project reuses.

**Status:** v0 → building v1 — 2026-06-04
**Owner:** p-kolesar
**Branch:** `main` · **Azure base name:** `intelreal`

**Goal & timeline.** Get ingestion to production **ASAP** so 2 weeks of immutable
bronze accumulate for an upcoming workshop. Bronze is immutable and everything
replays from it, so the only thing that loses time is delaying the scraper — the
agent and SPA are built *during* the accumulation window. Polish follows next week.

**Locked decisions (this session, 2026-06-04):**
- **Scope:** byty (apartments) only; Bratislava (17 boroughs) + Stupava corridor
  (stupava, marianka, borinka, lozorno); both `predaj` & `prenajom`.
- **Ingestion:** Azure Functions **timer**, one `(locality, deal)` unit per tick,
  **every 20 min within 06:00–22:00 Bratislava** (42 units/day, 48 slots), driven by a
  self-healing JSON **ledger**. A second **daily timer (~22:30 Bratislava)** rebuilds
  silver/gold and runs the brief. See §8.
- **Data:** medallion (bronze immutable → silver → gold); **all data UTC**, front-end
  renders Bratislava; global dedup at the **silver** rebuild; gold **split into two
  files** per table (city / okres). Contract is code: [`backend/realestate/schemas.py`].
- **Agent:** autonomous **daily brief only** (Q&A deferred); **fully read-only** (no
  write tools); 4 read-only tools; neutral analyst lens, **no buy/sell advice**.
- **Front-end:** read-only React SPA on Azure Static Web Apps, **Bratislava borough
  choropleth as the hero view** + Trends / Yield / Opportunity / Daily-brief tabs.

---

## 0. Initialize this shell _(done)_

- [x] **Branch** `main` · **Base name** `intelreal` (set in `AZURE_BASE_NAME`
      + `infra/main.parameters.json`).
- [ ] **Resource group / region / secrets** — set `AZURE_RESOURCE_GROUP`,
      `AZURE_LOCATION` (a Static Web Apps region), `AZURE_CREDENTIALS`, and
      `CLAUDE_API_KEY` (now required — the agent uses it). See `README.md`.
- [ ] **App labels** — `frontend/index.html`, `frontend/src/App.jsx`, `package.json`.

---

## 1. Purpose & scope

A neutral **market-research / monitoring** tool for the Bratislava apartment market.
It ingests public listings daily, builds weekly price/inventory/yield analytics by
area × segment, and an AI analyst writes a daily intelligence brief surfacing
**signals with caveats** — hot/cold trends, rent-vs-buy yield divergence, and
opportunity segments — **never buy/sell advice**.

**In scope (v1):** byty only; 17 BA boroughs + 4 corridor towns; `predaj` + `prenajom`;
district/borough granularity; daily brief; read-only SPA.
**Out of scope (v1):** street/GPS/cadaster enrichment, days-on-market / price history,
interactive Q&A, per-listing map pins, any advice or transaction.

---

## 2. Use cases (the driver)

| ID | Use case | Output | Granularity (v1) |
|----|----------|--------|------------------|
| UC1 | **Trend hot/cold** — how €/m² and inventory move by area × segment over time | Trend report | area × category × week |
| UC2 | **Rent-vs-buy / yield** — gross yield per area × segment; where rent diverges from buy | Yield analysis | area × category |
| UC3 | **Opportunity ranking** — segments/listings standing out vs their area, framed as *research signals* | Ranking | segment + listing |
| UC4 | **Data hygiene (supporting)** — flag & skip misleading entries so UC1–3 aren't polluted | Flags / exclusion | listing |
| UC5 | **Drill-down** — segment → district → (later) street | in-brief / SPA | district/borough now |

---

## 3. Stack

| Layer | Technology | State |
|-------|-----------|-------|
| Backend | Python Function App (Flex Consumption) | ✅ shell; RE code under `backend/realestate/` |
| Ingestion | Timer-triggered scraper (requests + JSON-LD parse) → Blob Parquet | 🔧 building |
| Storage | Azure Blob (one `realestate` container) + Parquet via Polars/pyarrow | 🔧 `storage/blobs.py` ported, needs extensions |
| Analytics | Polars (silver/gold rebuild) | 🔧 building |
| AI agent | Claude (Sonnet) via `anthropic`; skeleton ported from portfolio `agent/loop.py` | 🔧 ported, adapting to read-only |
| API surface | Azure Functions Python v2 HTTP routes | 🔧 building |
| Frontend | React + Vite SPA → Azure Static Web Apps (Free); choropleth via GeoJSON | 🔧 building |
| IaC / CI | Bicep + GitHub Actions (infra / backend / frontend deploy) | ✅ shell |

Secrets/app settings: `CLAUDE_API_KEY` (agent), `AzureWebJobsStorage` (Blob, used by
`storage/blobs.py`), `WEBSITE_TIME_ZONE=Central Europe Standard Time` (timer window).

---

## 4. Repo structure

```
backend/
  function_app.py            # HTTP routes + 2 timer triggers (ingest, build_and_brief)
  realestate/
    schemas.py               # ✅ DATA CONTRACT — paths + Polars schemas + scrape enums
    scraper.py               # 🔧 sweep one (locality, deal) → bronze slice
    ledger.py                # 🔧 self-healing day→unit ledger state machine
    build.py                 # 🔧 silver rebuild + gold (segment_weekly, yield) build
    data.py                  # 🔧 read-only query layer backing the agent tools + API
  agent/                     # ported from portfolio, adapted to read-only
    loop.py                  # ✅ _complete/_converse, token log, caps, cache_control
    prompts.py               # 🔧 neutral-analyst MANDATE + brief prompts (Slovak)
    tools.py                 # 🔧 4 read-only tools over realestate/data.py
  storage/
    blobs.py                 # ✅ single-blob Parquet I/O; 🔧 + list/dataset/json helpers
  host.json, requirements.txt, .funcignore, local.settings.json.example
infra/  main.bicep           # + realestate Blob container, CLAUDE_API_KEY, WEBSITE_TIME_ZONE
frontend/  src/api.js        # single backend seam; tabs: Map (hero) / Trends / Yield / Opportunity / Brief
.github/workflows/           # infra / deploy / deploy-frontend
```

---

## 5. Backend endpoints

| Endpoint | Method | Description | State |
|----------|--------|-------------|-------|
| `/health` | GET | Liveness probe (deploy smoke test) | ✅ |
| `/realestate/bootstrap` | GET | Enum lists (deals, categories), available weeks, coverage | 🔧 |
| `/realestate/segments` | GET | `segment_weekly` rows (filter grain/area/category/deal/week) | 🔧 |
| `/realestate/yield` | GET | `yield_segment` rows | 🔧 |
| `/realestate/trend` | GET | time-series for area × category × metric | 🔧 |
| `/realestate/geo` | GET | borough boundary GeoJSON + the selected metric per area | 🔧 |
| `/realestate/brief` | GET | latest (or by-date) daily brief memo + token/cost | 🔧 |
| `/scrape-realestate` | POST | manual spot-check sweep of one `?locality=&deal=` | 🔧 |

All read endpoints just serve precomputed Parquet/JSON from Blob → bounded JSON.
Q&A (`/realestate/ask`) is **deferred** to a later phase.

---

## 6. Data model

Three medallion layers; **bronze is immutable**, everything else is recomputable.
The authoritative schema (paths, columns, dtypes, grains) is code in
[`backend/realestate/schemas.py`] — this section is the summary.

| Layer | Path | Grain (1 row =) | Mutability |
|-------|------|-----------------|------------|
| **bronze** | `bronze/type=byty/deal=*/date=*/<slug>.parquet` | one listing in one sweep of one unit, key `(detail_id, scraped_date, source_slug)` | immutable, append-only, Hive-partitioned |
| **silver** | `silver/listings.parquet` | one listing `detail_id` (latest attrs + lifecycle + flags) | rebuilt each run |
| **gold** | `gold/{segment_weekly,yield_segment}_{city,okres}.parquet` | segment × week | rebuilt each run |
| **meta** | `meta/{ingest_ledger.json, ingest_runs.parquet, quality_overrides.parquet}` | ledger / per-tick run log / manual overrides | mixed |
| **agent** | `agent/{agent_log.parquet, briefs/<date>.json}` | one daily brief | append |

Key decisions baked into the contract:
- **All data UTC** (`scraped_at` instant, `scraped_date` date); FE renders Bratislava.
  The daytime scrape window never crosses UTC midnight, so UTC date ≡ Bratislava date.
- **Dedup**: bronze dedups within a unit only; **global dedup by `detail_id` at the
  silver rebuild** (ticks 20 min apart can't dedup each other at write time).
  `source_slug` records which query surfaced a row and resolves cross-query dupes.
- **Gold split into two files** per table (city → choropleth, okres → coarse) so a
  query can't accidentally sum both grains. **Per-category only** (no `*` rollup).
  `yield_segment` carries the city grain too (the map shades borough-level yield).
- **Quality (UC4, minimal):** free parse-time flags (`price_on_request`,
  `area_missing`, `street_unparsed`) computed at the silver rebuild; exclusion via the
  manual `quality_overrides` table (flag & skip, honored immediately). No detection
  logic in v1; cadaster/AI rules arrive with the GPS phases.
- **Lifecycle (minimal):** `first_seen`/`last_seen`/`is_active` only. `valid_to`,
  days-on-market, and price history are deferred (a 2-week window yields too few
  completed lifecycles) — reconstructable from bronze later.
- **Trend grain = weekly** (`week` = Monday of the ISO week of the UTC date);
  `ppm2_wow_pct` is null in week 1, `ppm2_mom_pct` null until 4 weeks exist — **never 0**,
  so the SPA/agent show "delta unavailable" rather than a fake zero.

---

## 7. AI agent

Reuses the portfolio skeleton (`agent/loop.py`): `_complete`/`_converse`,
`cache_control` on the system prompt, token logging to `agent/agent_log.parquet`,
`DAILY_TOKEN_CAP` + cumulative `SPEND_CAP_USD`, `MAX_TOOL_ROUNDS`. Adapted: the
Finnhub data client → a `realestate/data.py` layer over gold/silver; **trades and
watchlist management removed** — the agent is fully read-only.

- **Mandate:** a neutral market analyst. Reports trends, yields, notable segments as
  **research signals with caveats**; **never** buy/sell advice. Always flags low-sample
  / low-coverage segments and how many listings were excluded and why. Slovak.
- **Mode (v1):** autonomous **daily brief only**. Screen the compact gold segment table
  (Tier A, in-prompt) → pick notable segments → deep-dive via tools → write a memo.
- **Tools (4, read-only):** `segment_stats`, `trend_series`, `yield_analysis`,
  `query_listings` (capped). All arithmetic in Polars, never in the model.
- **Guardrails:** concrete integer caps (per-call `max_tokens`, daily token cap,
  cumulative spend cap); spend cap auto-disables the agent. Q&A deferred → no unbounded
  cost driver in v1.

---

## 8. Ingestion & orchestration

**Two timer triggers in the Function App** (everything stays in-app; each execution is
short, fitting Flex Consumption — no `functionTimeout` bump needed):

1. **`scrape_next_area`** — every 20 min, 06:00–22:00 Bratislava
   (`WEBSITE_TIME_ZONE`). Each tick: read the ledger for today's UTC date (init all 42
   `(locality, deal)` units `pending` if absent) → pick the next `pending` unit → sweep
   it (paged, jittered 20–40 s, browser headers, robots honored, **stop-on-403/429** +
   `Retry-After`, hard per-run cap, **page-33/~990 cap guard** with a WARNING) → write
   its bronze slice (idempotent: re-running overwrites the slice) → mark `done` with
   counts. No-op once the day is complete. Self-healing: a failed/missed unit stays
   `pending` and retries next tick. `coverage_pct = done/42`.
2. **`build_and_brief`** — daily ~22:30 Bratislava. Rebuild silver (full bronze diff +
   global dedup + flags) → rebuild gold (segment_weekly + yield, city & okres) → run the
   agent brief → write `agent_log` + `briefs/<date>.json`.

Plus `/scrape-realestate` for manual 1-unit spot-checks.

**Politeness is load-bearing** (ethical + block-avoidance): spreading one unit per 20 min
across the day is *more* polite than a burst. Getting blocked kills the demo.

---

## 9. Cost & guardrails

**Only the agent costs Claude tokens.** Ingestion, dedup, and gold aggregation are pure
Python/Polars/HTTP — **$0 Claude**, regardless of volume. Azure (a few short daily
Function executions + sub-GB Blob) is pennies/month.

- **Daily brief:** ~30k in / ~4k out, ~$0.15/day → **~$2 / 2 weeks** (same order as the
  portfolio agent). With Q&A deferred, the brief is the **only** Claude cost and it's
  bounded (fixed cadence, capped tokens).
- **Guardrails:** per-domain `SPEND_CAP_USD` auto-disable; daily token cap; exact
  cost logged to `agent/agent_log.parquet` from day one.
- **Cost levers** (prompt-cache, model tiering, Batch API, delta briefs) are workshop
  talking points, **not built** at ~$2 / 2 weeks. "All arithmetic in Polars" is assumed.

**Volumetrics (measured 2026-06-03):** ~6,123 active listings/sweep; ~226 page requests;
~0.65 MB Parquet/sweep (zstd); ~240 MB/year — small data for Polars. Per-district queries
all sit under the 990 cap (whole-city `predaj` ≈ 3,600 is the only thing that would need
price-band splitting — not queried at that grain in v1).

---

## 10. Decisions

**Resolved this session:** ingestion host/cadence (timer, per-`(locality,deal)`, 20-min,
daytime); module layout (`backend/realestate` + reused `agent`/`storage`); UTC-in-data;
dedup-at-silver; gold two-file split; per-category only; read-only agent (no Q&A, no
write tools); choropleth shades the 17 boroughs (`city` grain).

**Open / to verify:**
1. **Task 0 — payload recon (blocks the parser):** confirm on real payloads the 21
   locality **slug strings**, the `__next_f`/JSON-LD structure, presence of
   `district`/`city`/`validFrom`, the 990 cap at per-borough grain, and street-parse
   feasibility (+ resolve copyright: parse-then-discard, store only the token).
2. **Concrete agent caps** — set `max_tokens`, daily token cap, `SPEND_CAP_USD` as
   integers (not estimates) when wiring the loop.
3. **Borough GeoJSON** — source accurate *mestské časti* boundaries + 4 corridor polygons
   and reconcile to the site's `city` labels. Can start now (no data dependency).
4. **v2 acceptance gate** — min weeks of history, min sample sizes, `coverage_pct` floor
   before trend/yield/opportunity output is "good" and the agent (v2) can start.
5. **Scraping legality & recovery** — ToS/robots posture; recovery plan when `__next_f`
   changes or the scraper is blocked.
6. **`category` normalization** — confirm site label consistency so segments don't fragment.

---

## 11. Build plan (phased; critical path first)

| Phase | Deliverable | Status |
|-------|-------------|--------|
| **Foundation** | Ported `agent/loop.py`, `agent/tools.py`, `agent/prompts.py`, `storage/blobs.py`; data contract `realestate/schemas.py` | ✅ done |
| **Task 0** | Payload recon (slugs, JSON-LD, fields, cap, street/copyright) | ⏭ next |
| **Data (week 1, critical)** | `storage/blobs.py` extensions (`list_blobs`, `read_parquet_dataset`, `read_json`/`write_json`); `realestate/scraper.py` + `ledger.py`; bronze write; `ingest_runs`; the 20-min timer + infra (Blob container, `WEBSITE_TIME_ZONE`). **Ship → data starts accumulating.** | ⬜ |
| **Backend (week 1–2)** | `realestate/build.py` (silver + gold city/okres); `realestate/data.py`; read-only HTTP routes; adapt `agent/` to read-only + 4 tools + daily-brief loop + `build_and_brief` timer | ⬜ |
| **Frontend (week 2)** | `api.js` seam + stubs; Map (hero) / Trends / Yield / Opportunity / Brief tabs; honesty cues (low-sample muting, coverage strip, graceful early-week slider). Source GeoJSON in parallel (start now) | ⬜ |
| **Workshop** | After ≥2 weeks of brief history + the §10.4 acceptance gate; export CSVs for the M365 Copilot audit tracks | ⬜ |

**Deferred (recoverable from immutable bronze):** interactive Q&A (`/realestate/ask`,
v2.5); street/GPS/cadaster enrichment + point map (v3–v4); days-on-market / price history;
the pluggable quality-rule framework; coverage canary + full circuit breaker.

[`backend/realestate/schemas.py`]: backend/realestate/schemas.py
