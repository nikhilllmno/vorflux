# Infra map — hotel agent harness

One page: what runs where, what talks to what, and where each secret lives. Source of truth for
design: [`architecture.md`](architecture.md). How to deploy: [`runbook-deploy.md`](runbook-deploy.md).
**Status (2026-11-18): nothing is deployed.** Names below are proposals until the owner creates
the accounts (see "Open items" in the runbook). Owner = @nikhilllmno; Bot = the platform-engineer
agent, which writes code and docs but never deploys, migrates, or changes secrets.

```mermaid
---
config:
  flowchart:
    wrappingWidth: 1000
---
flowchart LR
  U["Hotel staff and our team<br/>(browser)"] --> WEB["Web UI<br/>Next.js on Vercel"]
  SL["Slack / Telegram"] --> API
  GHW["GitHub App webhooks"] --> API
  WEB -->|"HTTPS + Supabase JWT"| API["Control-plane API<br/>FastAPI on Fly.io<br/>(+ scheduler, job queue workers)"]
  WEB -->|"Auth only (anon key)"| SBA["Supabase Auth"]
  API -->|"service role, Postgres"| DB[("Supabase Postgres<br/>RLS, pgvector, job queue")]
  API --> ST[("Supabase Storage")]
  API -->|"Machines API"| SBX["Sandbox app (Fly Machines)<br/>1 microVM per run, own network"]
  SBX -->|"MCP, run-scoped token"| API
  SBX -->|"all egress"| EG["Credential proxy<br/>(Fly app)"]
  EG --> LLM["Anthropic / OpenAI"]
  EG --> GH["GitHub, our org:<br/>one repo per hotel"]
  EG -->|"evals: test assistant only;<br/>reports: call logs (read)"| VT["Vapi"]
  API -->|"PRs"| GH
  API -->|"preview confirmed"| CG["Content gate<br/>(Fly app, private)"]
  CG -->|"check + merge,<br/>content-only PRs"| GH
  API --> LF["Langfuse (traces)"]
  SBX --> LF
  GH -.->|"hotel repo CI on merge:<br/>push Vapi config"| VT
  GH -.->|"hotel repo CI on merge"| INS["InsForge<br/>text/email agents, Vapi tool calls"]
```

## Services

| Service | Runs on | Name per env (staging / prod) | Region | Exists from | Owner (account / code) |
|---|---|---|---|---|---|
| Web UI | Vercel, 2 projects | `vorflux-harness-web-staging` / `vorflux-harness-web` | `iad1` | M1 local, M5 hosted | Owner / Bot |
| Control-plane API (API, scheduler, Postgres-queue workers, connector gateway, MCP server) | Fly.io app | `vorflux-harness-api-staging` / `vorflux-harness-api` | `iad` | M1 local, M5 hosted | Owner / Bot |
| Sandboxes (one Fly Machine per run) | Fly.io app, custom private network | `vorflux-harness-sandbox-staging` / `vorflux-harness-sandbox` | `iad` | M2 | Owner / Bot |
| Credential (egress) proxy | Fly.io app, reachable only from sandboxes | `vorflux-harness-egress-staging` / `vorflux-harness-egress` | `iad` | M2 | Owner / Bot |
| Database, Auth, Storage, job queue | Supabase, 2 projects | `vorflux-harness-staging` / `vorflux-harness-prod` | `us-east-1` | M1 (local stack), M5 hosted | Owner / Bot (migration files) |
| LLM tracing | Langfuse Cloud (or self-hosted, Q6) | one project per env | US | M1 | Owner / Bot |
| GitHub App (branches, PRs) | GitHub, our org. Contents write, pull requests write, checks read, metadata read | `vorflux-harness-staging` / `vorflux-harness` | n/a | M2 | Owner / Bot |
| Content gate: merges content-only PRs (D18) after preview confirm and all evals | Fly.io app, private network only, called by the API | `vorflux-harness-content-gate-staging` / `vorflux-harness-content-gate` | `iad` | M2 local, M5 hosted | Owner / Bot |
| Content-gate GitHub App (only merge credential) | GitHub, our org. Contents write (merge), pull requests write, checks write (`harness/content-gate`), metadata read. No administration, workflows, or bypass | `vorflux-content-gate-staging` / `vorflux-content-gate` | n/a | M2 | Owner / Bot |
| Hotel repos (one per hotel; Vapi config in `vapi/`) | GitHub, our org; own CI pushes Vapi config on merge | `<org>/hotel-<slug>` (O12) | n/a | exist; onboarding automation M4 | Owner / our team |
| Voice platform | Vapi (hosted): production assistant and test assistant per hotel | per hotel (O11) | Vapi | M3 (evals, read tools) | Owner / hotel repo CI |
| Text/email agent backends | InsForge (not the harness) | per hotel | per hotel | exists | Hotel repo owners |
| CI | GitHub Actions in this repo | n/a | n/a | now | Owner / Bot |

Branch protection on hotel repos must let the content gate merge content-only PRs and block it
on any other PR. How this works with GitHub rules is an **open M2 spike** (architecture 9.2).

## Environments

| Env | Web | API | Sandboxes | Data | Deploys by |
|---|---|---|---|---|---|
| local | `npm run dev` (:3000) | `uv run harness` (:8000) | Docker (`SANDBOX_PROVIDER=docker_local`) | `supabase start` (local stack) | the developer |
| staging | Vercel staging project | `backend/fly.toml` | Fly sandbox app (staging) | Supabase staging project | a human, owner-approved |
| prod | Vercel prod project | `backend/fly.production.toml` | Fly sandbox app (prod) | Supabase prod project | a human, owner-approved |

## Secrets by name (values never in git)

| Lives in | Names | Notes |
|---|---|---|
| Fly secrets, API app | `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_DB_URL`, `SUPABASE_JWT_ISSUER`, `LANGFUSE_BASE_URL`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `FLY_API_TOKEN`, `FLY_SANDBOX_APP`, `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, `GITHUB_APP_WEBHOOK_SECRET`, `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `INSFORGE_BASE_URL`, `INSFORGE_API_KEY`, `HARNESS_KMS_KEY_ID`, `CONTENT_GATE_API_TOKEN` | `FLY_API_TOKEN` is a deploy token scoped to the sandbox app only. |
| Fly `[env]` in `fly*.toml` (not secret) | `HARNESS_ENVIRONMENT`, `HARNESS_LOG_LEVEL`, `HARNESS_CORS_ORIGINS`, `PORT` | CI fails if a secret-like name appears here. |
| Fly secrets, egress proxy app | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | Injected per allowlisted host; sandboxes never see them. |
| Sandbox machine env | `OPENCODE_SERVER_USERNAME`, `OPENCODE_SERVER_PASSWORD` | Generated per run, never stored. No long-lived secret. |
| Vercel env (per project) | `NEXT_PUBLIC_HARNESS_API_URL`, `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY` | Public by design (RLS protects data). No server secrets in Vercel today. |
| Supabase (Postgres rows) | per-org and per-hotel tenant secrets | Envelope-encrypted with `HARNESS_KMS_KEY_ID`. |
| Fly secrets, content-gate app | `CONTENT_GATE_GITHUB_APP_ID`, `CONTENT_GATE_GITHUB_APP_PRIVATE_KEY`, `CONTENT_GATE_DB_URL`, `CONTENT_GATE_API_TOKEN` | The only merge credential. `CONTENT_GATE_DB_URL` uses a narrow Postgres role. `CONTENT_GATE_API_TOKEN` is also on the API app. |
| Supabase rows, per hotel (Vapi) | `VAPI_TEST_PRIVATE_API_KEY` (test org), `VAPI_READ_PRIVATE_API_KEY` (prod org) | The proxy injects them for `api.vapi.ai`. It allows writes only to the test assistant and only `GET` with the prod key (Vapi private keys are org-wide). |
| Hotel repo, GitHub environment `vapi-production` (branch `main` only) | secret `VAPI_PRIVATE_API_KEY`; variable `VAPI_ASSISTANT_ID` | Used only by the push-on-merge job. |
| Hotel repo, GitHub environment `vapi-test` | secret `VAPI_TEST_PRIVATE_API_KEY`; variable `VAPI_TEST_ASSISTANT_ID` | Evals on PRs. Test org only, so PR code cannot reach production. |
| Hotel backend env and Vapi | `VAPI_SERVER_SECRET` | Signs Vapi tool calls. Never in `vapi/assistant.json`. |
| GitHub Actions secrets (this repo) | `FLY_API_TOKEN_CI_READONLY` (optional) | Read-only; only for `flyctl config validate`. CI never deploys. |
| Operator password manager | `FLY_API_TOKEN` (personal/deploy), `VERCEL_TOKEN`, `VERCEL_ORG_ID`, `VERCEL_PROJECT_ID`, `SUPABASE_ACCESS_TOKEN`, `SUPABASE_DB_PASSWORD` | Used from a human's machine for approved deploys and migrations. |

## Data stores

| Store | Holds | Tenant guard | Backup |
|---|---|---|---|
| Supabase Postgres | all harness data: orgs, hotels, sessions, runs, memory, skills, jobs (Postgres queue), approvals, audit, pgvector | `org_id`/`hotel_id` + RLS | Supabase daily backups; PITR on prod (paid add-on) |
| Supabase Storage | artefacts, skill files, exports (`org/<org_id>/hotel/<hotel_id>/...`) | bucket policies | **none yet**: database backups and PITR keep only object metadata, not the files. Object backup is an open item (runbook O10). |
| Fly volumes (sandbox app) | workspace snapshots per session | one volume per session | none (rebuildable from git) |
| Fly registry `registry.fly.io` | API, sandbox, and proxy images | n/a | keep last 10 releases |
| Langfuse | traces with prompts and outputs (redacted) | project per env, `org_id`/`hotel_id` tags | provider |
| GitHub | code; `harness/*` branches and PRs | per-hotel repo allowlist | GitHub |
| Vapi | live assistant config (copy of `main`), call logs | Vapi orgs per hotel (O11) | none; git is the source, and the push job keeps the last live config as a workflow artifact |
