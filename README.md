# Hotel Agent Harness

One harness for every hotel customer. It runs an operations-and-coding agent per hotel that
builds and changes the hotel's voice, text, and email agents (on Claude Agent SDK, OpenAI Agents
SDK, LangChain Deep Agents, or opencode), does tasks with skills and tools, runs on a schedule,
and learns from its work. The feature set follows
[Hermes Agent](https://github.com/NousResearch/hermes-agent) (MIT); see
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

**Status:** foundations only. Read the design first: [`docs/architecture.md`](docs/architecture.md).

## Repository layout

| Path | What |
|---|---|
| `backend/` | Python 3.12 + uv + FastAPI control-plane API (skeleton, runtime-adapter contract, tests); `Dockerfile` and `fly*.toml` (validated in CI, not deployed) |
| `web/` | Next.js control-plane UI (skeleton) |
| `supabase/` | Postgres migrations (tenancy schema and RLS) and local CLI config; see [`docs/schema.md`](docs/schema.md) |
| `docs/architecture.md` | Architecture and design, milestones, open questions |
| `docs/schema.md` | Database tables, RLS policies, isolation tests, how to add a table |
| `docs/infra-map.md` | One-page map: services, environments, regions, secrets by name, owners |
| `docs/runbook-deploy.md` | First-time setup, routine deploy, rollback, health checks, secret rotation |
| `docs/backlog.md` | Milestones with Linear links, Hermes feature-parity checklist |
| `.github/` | CI workflow and PR template |
| `.env.example` | Environment variable **names only** |

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (Python 3.12 is installed by uv if missing)
- Node.js 24 (see `web/.nvmrc`) and npm
- Optional: Docker and the [Supabase CLI](https://supabase.com/docs/guides/cli) 3.2.x for the database tests
- Optional: [gitleaks](https://github.com/gitleaks/gitleaks) for local secret scans

## Run locally

```bash
cp .env.example .env          # fill values locally; .env is git-ignored

# Backend: http://127.0.0.1:8000/healthz
cd backend
uv sync
uv run harness

# Web: http://localhost:3000
cd web
npm ci
npm run dev
```

## Checks (same as CI)

```bash
# backend
cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest

# database (local Supabase stack, Docker; see docs/schema.md)
supabase db start && supabase db reset
supabase db lint --schema public,private --level warning --fail-on warning
cd backend && HARNESS_TEST_DATABASE_URL="$(supabase status -o env | sed -n 's/^DB_URL="\(.*\)"$/\1/p')" \
  HARNESS_TEST_REQUIRE_DB=1 uv run pytest tests/rls

# web
cd web && npm run lint && npm run typecheck && npm run build

# secrets (from the repo root)
gitleaks git --config .gitleaks.toml .
```

If `next build` fails with `Cannot read properties of null (reading 'useContext')`, your shell
probably exports `NODE_ENV=development`. Run `env -u NODE_ENV npm run build`.

## Rules for this repository

- The repository is **public**. Never commit secret values, customer data, or real guest data.
  CI runs gitleaks on the full history of every PR and fails if `.env.example` has any value.
- Changes go through PRs. No direct deploys or migrations against live databases from this repo.
- There is no licence file. The code is public to read; no reuse rights are granted.
- Contributor and agent conventions: [`AGENTS.md`](AGENTS.md).
