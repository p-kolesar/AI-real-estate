# App Shell

A clean-slate, deployable starting point for building agentic software on
Azure quickly. It ships as a working skeleton — a Python Azure Function App, a
React/Vite SPA, Bicep infrastructure, and GitHub Actions CI/CD — with all
business logic stripped out. **Starting a new project? Begin with the
[Start here checklist](Project%20Charter.md#0-start-here--initialize-this-shell-for-your-project)
in the Project Charter** — branch, name your app, point it at a resource group,
then fill in the rest and build.

## What's in the box

| Layer | Technology |
| --- | --- |
| Backend | Python Flex Consumption Function App (`/hello`, `/health`) |
| Frontend | React + Vite SPA → Azure Static Web Apps (Free) |
| IaC / CI | Bicep + GitHub Actions (infra / backend / frontend deploy) |
| AI agent | `CLAUDE_API_KEY` is pre-wired into the Function App — add the `anthropic` package and your own loop |

## Layout

```
.github/workflows/
  infra.yml                 # provisions/updates Azure infra (infra/** + manual)
  deploy.yml                # deploys the Python function code (backend/** + manual)
  deploy-frontend.yml       # builds + deploys frontend to its Static Web App
infra/
  main.bicep                # Storage, Log Analytics + App Insights, Flex Consumption
                            # Function App (+ CORS), Free Static Web App
  main.parameters.json      # baseName / environmentName / pythonVersion
backend/
  function_app.py           # Python v2 HTTP endpoints (see below)
  host.json, requirements.txt, .funcignore
  local.settings.json.example
frontend/                   # React + Vite SPA (dark theme)
  src/App.jsx               # bare shell view (calls /hello)
  src/api.js                # single backend seam (+ stub data when no API configured)
  vite.config.js, staticwebapp.config.json, .env.example
```

## Backend endpoints

All routes are served under `/api`.

| Endpoint | Method | Description |
| --- | --- | --- |
| `/hello` | GET | Hello-world trigger (`?name=` optional) |
| `/health` | GET | Liveness probe (used by the deploy smoke test) |

## One-time setup

### 1. Service principal → GitHub secret

Create a service principal with Contributor at **subscription** scope (so the
infra job can create the resource group) and save the JSON as the
`AZURE_CREDENTIALS` Actions secret:

```bash
az ad sp create-for-rbac \
  --name "gh-myapp" \
  --role Contributor \
  --scopes /subscriptions/<SUBSCRIPTION_ID> \
  --sdk-auth
```

### 2. Repository secrets

**Repo → Settings → Secrets and variables → Actions → Secrets**:

| Secret | Used by |
| --- | --- |
| `AZURE_CREDENTIALS` | all Azure workflows (login) — **required** |
| `CLAUDE_API_KEY` | infra (injected into the Function App) — **optional**; the bare shell deploys without it, set it once you add a Claude agent |

### 3. Repository variables

**Repo → Settings → Secrets and variables → Actions → Variables**:

| Variable | Value |
| --- | --- |
| `AZURE_RESOURCE_GROUP` | e.g. `rg-myapp-dev` |
| `AZURE_LOCATION` | e.g. `westeurope` *(must be a Static Web Apps region)* |
| `AZURE_BASE_NAME` | e.g. `myapp` *(also set `baseName` in `infra/main.parameters.json`)* |

## Deployment order

1. **Infra (Bicep)** — Actions → *Infra (Bicep)* → Run. Creates the resource
   group, Function App, and the Static Web App; sets CORS so the SWA can
   call the API. The run summary prints the Function App + frontend hostnames.
2. **Deploy (Function code)** — discovers the Function App in the resource group,
   deploys `backend/`, and smoke-tests `GET /api/health`.
3. **Deploy Frontend** — builds `frontend` with `VITE_API_BASE`
   pointed at the live API and uploads to the Static Web App. (Fetches the SWA
   deploy token at run time — no extra secrets.)

Run these **in order the first time** (use *Run workflow* / `workflow_dispatch`).
On an initial push to `main` all three fire in parallel by changed path, so the
backend and frontend jobs will fail until Infra has created the resources — they
exit with a clear "Run the Infra workflow first" message. Run Infra, wait for it
to finish, then run the other two (or just re-run them). After that, pushes to
`main` trigger each pipeline by changed path (`infra/**`, `backend/**`,
`frontend/**`).

## Local development

### Backend

```bash
cd backend
python -m venv .venv; .venv\Scripts\activate   # Windows (PowerShell)
pip install -r requirements.txt
cp local.settings.json.example local.settings.json   # then fill in your keys
func start    # requires Azure Functions Core Tools v4
# GET http://localhost:7071/api/health  ->  {"status": "ok"}
# GET http://localhost:7071/api/hello   ->  {"message": "Hello, World!"}
```

> `local.settings.json.example` is a template — put real keys only in the
> gitignored `local.settings.json`, never in the example.

### Frontend

```bash
cd frontend
npm install
npm run dev          # runs on stub data with no backend
```

To point the dev server at a real backend, set in `.env`:

```
# call the deployed API directly (CORS is configured for the SWA origin)
VITE_API_BASE=https://<func-host>/api
# or proxy /api to a local backend to avoid CORS during dev
VITE_API_PROXY=http://localhost:7071
```

## Building on the shell

1. Copy [Project Charter.md](Project%20Charter.md) and fill in the `FILL IN`
   sections — purpose, use cases, data model, agent design.
2. Add backend routes in `backend/function_app.py`; add dependencies to
   `backend/requirements.txt`.
3. Add new Azure resources (data containers, etc.) in `infra/main.bicep`.
4. Build out `frontend/` — keep all backend calls behind `src/api.js`.

## Notes

- **Auth:** the frontend is a public URL with no authentication (by design).
- **Cold start:** Flex Consumption has a brief cold start.
