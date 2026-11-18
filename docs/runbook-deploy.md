# Deploy runbook — hotel agent harness

How to set up and deploy the harness: the API, content gate, and sandbox image on Fly.io, the web
UI on Vercel, and database migrations on Supabase. Section 3.5 covers hotel repos and Vapi, which
deploy through their own CI. Service names, regions, and secret locations are in
[`infra-map.md`](infra-map.md). Design: [`architecture.md`](architecture.md).

**Status (2026-11-18): nothing is deployed, and no account exists yet.** The first hosted deploy
is staging in M5 (architecture Section 12: M2 content gate, M3 Vapi, M4 operations and hotel
onboarding, M5 pilot, M6 hardening). The config files exist so that CI can validate them:
`backend/Dockerfile`, `backend/fly.toml` (staging), `backend/fly.production.toml`, and
`web/vercel.json`. CI builds and smoke-tests the image; it never pushes or deploys.

## Rules

- **[APPROVAL]** marks a step that needs the owner's explicit approval first: every deploy, every
  migration, every secret change, and every create or delete of cloud resources. Ask with the
  template below. Agents never run these steps.
- Deploy staging first. Promote to production only the same git commit, after staging passes the
  health checks.
- Never type a secret value into a command line or a file in this repo. Use `fly secrets import`
  (reads stdin), `vercel env add` (prompts), or the provider dashboard.
- Order for a release with several parts: **migrations (backward-compatible) → API → sandbox image
  → web.** Each step must work with the previous version of the next step.
- Tool versions: `flyctl` 0.5.12 (same as CI), Vercel CLI 66.x, Supabase CLI 3.2.x,
  Docker with BuildKit. Supabase CLI v3 targets the **local** stack by default: every command
  against a hosted project in this runbook has `--remote`. Never add `--yes` (it skips the CLI's
  own confirmation of hosted changes).

Approval request template (post in `#vfx-platform-engineer`):

```text
APPROVAL: <deploy | migration | secret change> — <env> — <component>
What changes: <commit SHA / migration file / secret NAME (never the value)>
Risk: <low | medium | high> — <one line why>
Rollback: <exact command or section of this runbook>
Checks run: <pre-deploy checklist items, CI link>
```

## 1. Pre-deploy checklist

- [ ] The commit is merged to `main` and CI on `main` is green (secret scan, backend, web, deploy
      config).
- [ ] The release notes list every part that changes (migration, API, sandbox image, web) and the
      order.
- [ ] Migrations, if any: reviewed, backward-compatible with the running API (expand first,
      contract in a later release), and `supabase db push --dry-run --remote` shows only the expected files.
- [ ] New environment variables are in `.env.example` (name only) and already set in the target
      env (Fly secrets or Vercel env) **before** the code that reads them ships.
- [ ] The rollback target is written down: current Fly image (`fly releases --image`), current
      Vercel production deployment URL, and the latest Supabase backup time.
- [ ] Staging got the same commit and passed Section 4 health checks (production only).
- [ ] No hotel-facing event is in progress that a restart would interrupt (production only).
- [ ] Owner approval is recorded for each **[APPROVAL]** step.

## 2. First-time setup (once per environment, staging first)

Before you start, the owner provides the inputs in Section 7 (accounts, tokens, region).

### Supabase (do this first: the API needs its URL and keys)

- **S1 [APPROVAL]** Create project `vorflux-harness-staging` (later `vorflux-harness-prod`) in the
  owner's Supabase org, region `us-east-1`. Store the database password in the password manager as
  `SUPABASE_DB_PASSWORD`.
- **S2 [APPROVAL]** Project settings: enforce SSL; turn on network restrictions if the plan allows;
  production only: turn on point-in-time recovery (PITR).
- **S3** Auth → URL configuration: Site URL = the Vercel URL of this env; add the same URL to the
  redirect allow list. Leave sign-ups closed (invite only) until M5.
- **S4** Link the CLI from the repo root: `supabase login` (uses `SUPABASE_ACCESS_TOKEN`), then
  `supabase link --project-ref <ref>`.
- **S5 [APPROVAL]** When `supabase/migrations/` exists (M1): `supabase db push --dry-run --remote`,
  then `supabase db push --remote`. Then run Section 4 Supabase checks.

### Fly.io: API

- **F1** `fly auth login` with an account in the owner's Fly org (`<fly-org>`, Section 7).
- **F2 [APPROVAL]** Create the app: `fly apps create vorflux-harness-api-staging --org <fly-org>`.
- **F3 [APPROVAL]** Set secrets for the API app (names: infra-map, "Fly secrets, API app"). From a
  file outside the repo, or one at a time from stdin:
  `fly secrets import --stage -a vorflux-harness-api-staging < ~/secure/harness-api-staging.env`.
  Set only the names the deployed code reads; today the code reads none.
- **F4 [APPROVAL]** First deploy, from `backend/`:
  `fly deploy --image-label "git-$(git rev-parse --short HEAD)"`
  (staging is the default config). Production: add `--config fly.production.toml`, then
  `fly scale count 2 --config fly.production.toml` for two machines.
- **F5** Check `fly ips list` shows shared IPv4 and IPv6.
- **F5a (optional) [APPROVAL]** Custom domain: `fly certs add <domain>` plus DNS records. Record
  it in infra-map and in `HARNESS_CORS_ORIGINS` / Supabase Auth if the web URL changes.
- **F6** Run Section 4 API checks.
- **F7 (optional) [APPROVAL]** Read-only token for CI: `fly tokens create readonly --org <fly-org> --expiry 8760h`, then
  add it as GitHub Actions secret `FLY_API_TOKEN_CI_READONLY`. CI then runs
  `flyctl config validate --strict` on both configs. It cannot deploy with that token.

### Fly.io: sandbox app and credential proxy (M2)

- **F8 [APPROVAL]** Create the sandbox app on its own private network, with no public IPs:
  `fly apps create vorflux-harness-sandbox-staging --org <fly-org> --network vorflux-sandbox-staging`.
  Do not run `fly ips allocate` for it.
- **F9 [APPROVAL]** Create a deploy token scoped to the sandbox app only:
  `fly tokens create deploy -a vorflux-harness-sandbox-staging --expiry 2160h`, and store it as
  `FLY_API_TOKEN` in the API app (F3 method). Set `FLY_SANDBOX_APP` to the app name.
- **F10 [APPROVAL]** Create and deploy the credential proxy app `vorflux-harness-egress-staging` on
  the same network as the sandboxes (`--network vorflux-sandbox-staging`), with the provider keys
  as its secrets. Its config ships with the M2 code.

### Vercel: web

- **V1 [APPROVAL]** Create project `vorflux-harness-web-staging` (later `vorflux-harness-web`):
  framework Next.js, Root Directory `web`, Node.js 24. If you connect the Git repository,
  `web/vercel.json` (`git.deploymentEnabled: false`) still blocks automatic deploys on push, so
  every deploy is an explicit CLI step.
- **V2 [APPROVAL]** Set env vars for the Production target of that project, each with
  `vercel env add <NAME> production` (it prompts for the value): `NEXT_PUBLIC_HARNESS_API_URL`,
  `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY`.
- **V3 [APPROVAL]** First deploy: Section 3.4.
- **V4** Put the web URL into `HARNESS_CORS_ORIGINS` in `backend/fly.toml` (or
  `fly.production.toml`) by PR, and into Supabase Auth (S3). Then redeploy the API (3.2).

### Content gate (M2)

The content gate is a separate backend service. It merges a PR only when every changed file is in
the `content_paths` of `harness.yaml` on the default branch, the requester confirmed the preview of
that head commit, and CI, gitleaks, and all evals pass (architecture D18).

- **F11 [APPROVAL]** Create app `vorflux-harness-content-gate-staging` on the default network,
  with no public IPs (`fly apps create ... --org <fly-org>`, no `fly ips allocate`). The API calls
  it over the Fly private network. Its config ships with the M2 code.
- **F12 [APPROVAL]** Set its secrets (infra-map, "Fly secrets, content-gate app") with the F3
  method. Put the same `CONTENT_GATE_API_TOKEN` in the API app. `CONTENT_GATE_DB_URL` uses a
  Postgres role that can read preview confirmations and write merge records only.

### GitHub Apps and Langfuse (M1–M2)

- **G1 [APPROVAL]** In our GitHub org, create app `vorflux-harness-staging` with permissions:
  contents write, pull requests write, checks read, metadata read; no administration, no
  workflows. Webhook URL: the API URL plus the path that ships in M2. Install it only on the
  allow-listed hotel repos. Put `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`,
  `GITHUB_APP_WEBHOOK_SECRET` in the API app (F3). The egress proxy blocks the GitHub merge
  endpoints for sandboxes.
- **G2 [APPROVAL]** Create app `vorflux-content-gate-staging`: contents write (needed to merge),
  pull requests write, checks write (posts `harness/content-gate`), metadata read; no
  administration, no workflows, no webhook. Install it on the same repos. Put its ID and key in
  the content-gate app only (F12). Architecture 9.5 gives the check to the main App; a separate
  App keeps the only merge key in one service. Confirm in the M2 spike (O13).
- **L1 [APPROVAL]** Create a Langfuse project per env; put its keys in the API and proxy apps.

### Hotel repo and Vapi (once per hotel; automated in M4)

- **H1 [APPROVAL]** Create the private repo `<org>/hotel-<slug>` in our org (one repo per hotel).
- **H2** By PR reviewed by our team: `harness.yaml` (with `content_paths` and the `voice:` block),
  `CODEOWNERS` (our team on `*`, content paths with no owner), and the Vapi push workflow
  (contract in 3.5).
- **H3 [APPROVAL]** Ruleset on `main`: PR required, code-owner review, required checks (CI,
  gitleaks, evals, and `harness/content-gate` with the content-gate App as its only allowed
  source), no force pushes, no bypass actors. **Open M2 spike (O13):** prove that this lets the
  content gate merge a content-only PR and blocks it on any other PR.
- **H4 [APPROVAL]** Vapi (layout: O11): create the production assistant and a test assistant.
  In the repo, create GitHub environment `vapi-production` (deployment branch `main` only) with
  secret `VAPI_PRIVATE_API_KEY` and variable `VAPI_ASSISTANT_ID`, and environment `vapi-test`
  with `VAPI_TEST_PRIVATE_API_KEY` and `VAPI_TEST_ASSISTANT_ID`. Use `gh secret set --env <env>`
  (it prompts for the value).
- **H5 [APPROVAL]** Install both GitHub Apps on the repo, add it to the hotel's allowlist, and
  store `VAPI_TEST_PRIVATE_API_KEY` and `VAPI_READ_PRIVATE_API_KEY` in the hotel's tenant secrets
  (envelope-encrypted, never in a file).

## 3. Routine deploy

Do 3.1 → 3.4 in order and skip parts that did not change. Do staging, run Section 4, then repeat
for production with the production config, project, or link.

### 3.1 Supabase migrations [APPROVAL]

1. `supabase link --project-ref <staging-ref>`
2. `supabase migration list --remote` — note the last applied version (the rollback point).
3. `supabase db push --dry-run --remote` — must list only the new reviewed files.
4. `supabase db push --remote` — confirm the project ref at the CLI prompt.
5. Section 4 Supabase checks, then the API checks (the running API must still work).

### 3.2 API and content gate on Fly.io [APPROVAL]

Deploy the API first, then the content gate with the same steps and its own app name and config.

1. `cd backend && fly releases --image -a vorflux-harness-api-staging` — note the current image.
2. `fly deploy --image-label "git-$(git rev-parse --short HEAD)"`
   (production: `fly deploy --config fly.production.toml --image-label ...`; it uses blue-green).
3. Fly waits for the `/healthz` check on the new machines. Then run Section 4 API checks.

### 3.3 Sandbox image on Fly.io (M2) [APPROVAL]

1. Build and push without starting machines:
   `fly deploy --build-only --push -a vorflux-harness-sandbox-staging --image-label "git-<sha>"`
   (the sandbox Dockerfile and its config ship in `sandbox/` in M2).
2. Point the API at the new image reference (setting name defined in M2) and redeploy the API.
   Runs in progress keep their old image; new runs use the new one.
3. Start one test run per runtime and check Section 4 sandbox checks.

### 3.4 Web on Vercel [APPROVAL]

From the **repository root** (the project's Root Directory is `web`, so running from `web/`
fails with a `web/web` path), with `VERCEL_ORG_ID` and `VERCEL_PROJECT_ID` of the target project
in the shell:

1. `vercel list --environment production` — note the current production deployment URL (the rollback point).
2. `vercel pull --yes --environment=production`
3. `vercel build --prod`
4. `vercel deploy --prebuilt --prod`
5. Section 4 web checks.

### 3.5 Hotel repos and Vapi (deployed by the hotel repo's CI, not by hand)

The merge is the approval: our team merges code PRs and the content gate merges content-only PRs.
The repo's workflow then pushes `vapi/assistant.json` to Vapi. It must do these steps:

1. Run only on `push` to `main` and on `workflow_dispatch` with input `ref` (used for rollback),
   in environment `vapi-production`.
2. Validate the config. Fail if it holds a secret value.
3. Save the live assistant (`GET /assistant/{VAPI_ASSISTANT_ID}`) as a workflow artifact named
   `vapi-live-before-<sha>`. This is the rollback copy.
4. `PATCH /assistant/{VAPI_ASSISTANT_ID}` with the config. Then `GET` it again and compare.

Evals on PRs use environment `vapi-test` and the test assistant only, never the production one.
After the push, the harness watches live metrics and opens a revert PR on a drop (thresholds: Q23).

## 4. Health checks

| Part | Check | Pass |
|---|---|---|
| API | `curl -fsS https://vorflux-harness-api-staging.fly.dev/healthz` | `{"status":"ok","version":"<v>","environment":"staging"}` |
| API | `fly status -a <app>` and `fly checks list -a <app>` | all machines `started`, all checks `passing` |
| API | `fly logs -a <app> --no-tail` | no errors or restarts since the deploy |
| Web | `curl -fsSI https://vorflux-harness-web-staging.vercel.app/` | HTTP 200; page loads and calls the API without CORS errors |
| Supabase | `supabase migration list --remote` | local and remote columns match |
| Supabase | dashboard → Database health; RLS test suite against staging (M1) | no errors; tenant tests pass |
| Sandbox (M2) | test run per runtime; `fly machines list -a <sandbox-app>` | run completes; no machine older than the max run time |
| Proxy (M2) | proxy logs for the test run | only allow-listed hosts |
| Content gate (M2) | `fly checks list -a <content-gate-app>`; on a sample hotel repo, a content-only PR with a confirmed preview, and a PR that also changes code | the first merges; the second gets no `harness/content-gate` success and waits for our team |
| Vapi push | the push job's compare step; `GET /assistant/{id}` `updatedAt`; Vapi call logs | config matches `main`; no rise in failed calls or hand-offs |
| Tracing | Langfuse project | a trace for the test run, tagged with `org_id`, `hotel_id`, `run_id` |

## 5. Rollback

| Part | Rollback | Notes |
|---|---|---|
| API (Fly) **[APPROVAL]** | `fly releases --image -a <app>`, then `fly deploy --image <previous image ref>` (add `--config fly.production.toml` for prod) | Seconds to minutes. Works only if the database schema is still compatible: this is why migrations are expand-first. |
| Sandbox image (Fly) **[APPROVAL]** | Point the API back at the previous image reference and redeploy the API | Running sandboxes finish on their image. |
| Web (Vercel) **[APPROVAL]** | `vercel rollback <previous deployment URL>` (or dashboard → Instant Rollback). Undo with `vercel promote <URL>` | Env var changes need a new deploy to take effect, so roll back env vars and redeploy. |
| Supabase migration **[APPROVAL]** | Preferred: a new forward migration that reverts the change, by PR, then 3.1. Last resort (data loss after the restore point): PITR or a daily backup restore from the dashboard | Migrations are forward-only. If you revert by hand, fix history with `supabase migration repair --status reverted <version> --remote`. |
| Content gate (Fly) **[APPROVAL]** | Same as the API. To stop all automatic merges at once: `fly scale count 0 -a <content-gate-app>` | Content PRs then wait; nothing merges by itself. |
| Vapi config (bad push) | 1. Normal: a revert PR in the hotel repo. A content revert goes through the content gate (requester or hotel admin confirms); other reverts need our team. The merge pushes the old config. 2. **[APPROVAL]** Guests affected now: `gh workflow run <vapi-push workflow> -R <org>/hotel-<slug> -f ref=<last good sha>`, then the revert PR so `main` matches Vapi. 3. **[APPROVAL]** CI broken: restore artifact `vapi-live-before-<sha>` in the Vapi dashboard, then the revert PR. | Without the revert PR, the next merge pushes the bad config again. |
| Secret change **[APPROVAL]** | Overlapping secrets: set the previous value again (still valid until step 5 of 6.1). Single-active secrets: no rollback to the old value; fix forward (6.2). | Do not revoke an old value until the new one passes checks. |

## 6. Rotate a secret [APPROVAL]

Ask for approval with the template (secret NAME, env, reason, type from the table, rollback).
Each secret has one of two types:

- **Overlapping**: the provider lets the old and new values work at the same time. Use 6.1.
  Rollback = set the old value again.
- **Single-active**: creating the new value kills the old one at once. Use 6.2. There is no
  rollback to the old value, and consumers fail until they get the new value.

### 6.1 Overlapping secrets

1. Create the new value at the provider. Keep the old value valid.
2. Set it, staging first:
   - Fly: `fly secrets import --stage -a <app> < <file outside the repo>`, then
     `fly secrets deploy -a <app>` (restarts machines).
   - Vercel: `vercel env rm <NAME> production`, `vercel env add <NAME> production`, then redeploy
     (3.4).
3. Run Section 4 checks. Then repeat for production.
4. Wait until no consumer uses the old value (provider usage page or logs, if it has one).
5. Revoke the old value. Record the rotation date (no value) in the release notes.

### 6.2 Single-active secrets

1. Pick a low-traffic window and post it in `#vfx-platform-engineer`; the approval request says
   "expected interruption: <minutes>".
2. Stage the change first so the restart is the only gap: have the new-value file ready.
3. Rotate at the provider, then at once set the new value in every consumer
   (`fly secrets import --stage ...` then `fly secrets deploy`, for each app that uses it).
4. Run Section 4 checks. If a consumer still fails, fix forward: set the new value again or
   rotate again. The old value cannot come back.

If a secret leaked: revoke it first (accept the interruption), then rotate, then check provider
logs for misuse.

| Secret | Type | Where to create the new value |
|---|---|---|
| `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_ANON_KEY` | Overlapping with the new API keys; single-active with legacy JWT keys | Supabase → Project settings → API keys. Use the new secret/publishable keys (several can be active); rotating the legacy JWT secret invalidates every key. |
| `SUPABASE_DB_URL` / `SUPABASE_DB_PASSWORD` | Single-active | Supabase → Database settings → reset password, then update `SUPABASE_DB_URL` in the API app. |
| `FLY_API_TOKEN` (sandbox deploy token) | Overlapping | `fly tokens create deploy -a <sandbox-app>`; later `fly tokens revoke <id>`. |
| `GITHUB_APP_PRIVATE_KEY` | Overlapping | GitHub → org settings → GitHub Apps → the app → generate a private key; delete the old key after. |
| `GITHUB_APP_WEBHOOK_SECRET` | Single-active | Same app page → webhook secret. Set it in the API app right after. |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | Overlapping | Provider console; set on the proxy app. |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | Overlapping | Langfuse → project settings → API keys. |
| `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET` | Single-active | Slack app config (regenerate / reinstall). |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET` | Single-active | BotFather `/revoke`; then set the webhook again with the new secret. |
| `VERCEL_TOKEN`, `SUPABASE_ACCESS_TOKEN` (operator) | Overlapping | Vercel account tokens; Supabase account access tokens. |
| `CONTENT_GATE_GITHUB_APP_PRIVATE_KEY` | Overlapping | Content-gate App page → generate a private key; delete the old key after. |
| `CONTENT_GATE_API_TOKEN` | Single-active | Generate a new random value; set it in the API and content-gate apps together (6.2). |
| `VAPI_PRIVATE_API_KEY`, `VAPI_TEST_PRIVATE_API_KEY`, `VAPI_READ_PRIVATE_API_KEY` | Overlapping | Vapi dashboard → API keys of that org. Update the hotel repo environment (`gh secret set --env`) and the hotel's tenant secrets, then delete the old key. |
| `VAPI_SERVER_SECRET` | Single-active | Vapi server settings and the hotel backend env, together. |

## 7. Open items that need owner input

| # | Needed | Why | Proposed default |
|---|---|---|---|
| O1 | Fly.io org and a Fly token for the operator | F1–F10; none exists for vorflux | Org `vorflux`; the owner logs in, agents get no deploy token |
| O2 | A new Vercel token and team for vorflux | V1–V4 | Two projects in the owner's team |
| O3 | Supabase projects for vorflux (staging, prod) and plan | S1–S5; PITR needs a paid plan | Pro plan for prod |
| O4 | Region (architecture Q5) | All services must share one metro | `iad` / `iad1` / `us-east-1` until Q5 is answered |
| O5 | KMS provider for `HARNESS_KMS_KEY_ID` | Tenant secret envelope encryption | Decide in M1 |
| O6 | Langfuse: cloud or self-hosted, and data region | Traces hold redacted prompts | Langfuse Cloud US |
| O7 | Domains | Stable API and web URLs for CORS and Auth | `*.fly.dev` and `*.vercel.app` until then |
| O8 | GitHub org for product-agent repos and the App owner | G1 | Our org; owner is the App admin |
| O9 | Optional read-only Fly token as GitHub secret `FLY_API_TOKEN_CI_READONLY` | Turns on `flyctl config validate` in CI (F7) | Add after the Fly apps exist |
| O10 | Backup of Supabase Storage objects | Database backups and PITR do not include the files | A scheduled copy to a second bucket or provider, decided in M1 |
| O11 | Vapi org layout and who holds the keys | Vapi private keys are org-wide, so one key reaches every assistant in its org | Two Vapi orgs per hotel: production (push key only in the hotel repo's `vapi-production`; a second key for harness reads, `GET` only at the proxy) and test. Also confirm D21 ("never calls Vapi write APIs") allows writes to the test assistant (architecture 9.4) |
| O12 | Hotel repo name pattern and the Vapi push workflow | H1, 3.5; repos have no shared template | `hotel-<slug>`; the Bot writes a reference workflow in M3 |
| O13 | Branch protection with the content gate (**open M2 spike**) | H3, G2: the gate must merge only content-only PRs | Ruleset in H3 and a separate content-gate App |
