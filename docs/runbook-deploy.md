# Deploy runbook — hotel agent harness

How to set up and deploy the harness: the API, eval gate, and sandbox image on Fly.io, the web
UI on Vercel, and database migrations on Supabase. Section 3.5 covers hotel repos and Vapi, which
deploy through their own CI. Service names, regions, and secret locations are in
[`infra-map.md`](infra-map.md). Design: [`architecture.md`](architecture.md).

**Status (2026-11-18): nothing is deployed, and no account exists yet.** The first hosted deploy
is staging in M5 (architecture Section 12: M2 eval gate for content, prompt, and config; M3 Vapi,
staged rollout, and code through the eval gate; M4 operations and hotel onboarding; M5 pilot; M6
hardening). The config files exist so that CI can validate them:
`backend/Dockerfile`, `backend/fly.toml` (staging), `backend/fly.production.toml`, and
`web/vercel.json`. CI builds and smoke-tests the image; it never pushes or deploys.

## Rules

- **[APPROVAL]** marks a step that needs the owner's explicit approval first: every deploy, every
  migration, every secret change, and every create or delete of cloud resources. Ask with the
  template below. Agents never run these steps.
- One exception: any engineer on our team may **stop** the eval gate at once without approval
  (5.1), because stopping only holds merges. Resuming it is an [APPROVAL] step.
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

Before you start, the owner provides the inputs in Section 7 (accounts, tokens). The region is
India (architecture D25; infra-map, "Regions").

### Supabase (do this first: the API needs its URL and keys)

- **S1 [APPROVAL]** Create project `vorflux-harness-staging` (later `vorflux-harness-prod`) in the
  owner's Supabase org, region `ap-south-1` (Mumbai). Store the database password in the password manager as
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
- **F1a** Choose the Fly region: run `fly platform regions`. If it lists `bom` (Mumbai), change
  `primary_region` to `"bom"` in `backend/fly.toml` and `backend/fly.production.toml` by PR
  (CI accepts `bom` or `sin`), and use `bom` for every Fly app below. Else keep `sin`
  (Singapore). Record the result in infra-map. Do not mix regions between apps.
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

### Eval gate (M2)

The eval gate is a separate private service with its own GitHub App (architecture D18, 9.9). It
posts `harness/eval-gate` and merges a PR only when all of these are true:

1. The requester confirmed the readable diff of that exact head commit.
2. CI, gitleaks, tests, and all evals pass, with no score drop against `main`.
3. The diff limits in `harness.yaml` hold.
4. No protected path changed (Q28: `harness.yaml`, CODEOWNERS, CI workflows, evals, migrations,
   payments, auth). Path rules come from `harness.yaml` on the default branch.
5. `ReviewChange` did not rate the PR `High` (Q29).

This applies to every change class, backend code included. Backend code (`code` class) merges
through the gate only from M3, after staged rollout, post-deploy smoke evals, and automatic revert
work; in M2 the gate sends it to our team. PRs that fail 4 or 5 always go to our team. The gate
also merges automatic revert PRs, with no confirmation (architecture 9.6, step 9).

- **F11 [APPROVAL]** Create app `vorflux-harness-eval-gate-staging` on the default network,
  with no public IPs (`fly apps create ... --org <fly-org>`, no `fly ips allocate`). The API calls
  it over the Fly private network. Its config ships with the M2 code and must set
  `auto_start_machines = false`, so that the kill switch (5.1) holds.
- **F12 [APPROVAL]** Set its secrets (infra-map, "Fly secrets, eval-gate app") with the F3
  method. Put the same `EVAL_GATE_API_TOKEN` in the API app. `EVAL_GATE_DB_URL` uses a
  Postgres role that can read confirmations and per-hotel pause flags and write merge records
  only.

### GitHub Apps and Langfuse (M1–M2)

- **G1 [APPROVAL]** In our GitHub org, create the main harness App `vorflux-harness-staging`
  with permissions: contents write (for `harness/*` branches), pull requests write, checks read
  (to sync PR status and attach check evidence), metadata read; no checks write, no administration, no workflow write (architecture 9.9). Webhook URL: the API URL plus the path that ships in M2. Install it only on the
  allow-listed hotel repos. Put `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`,
  `GITHUB_APP_WEBHOOK_SECRET` in the API app (F3). The egress proxy blocks the GitHub merge
  endpoints for sandboxes.
- **G2 [APPROVAL]** Create the eval-gate App `vorflux-eval-gate-staging`: contents write
  (needed to merge), pull requests write, checks write (posts `harness/eval-gate`), metadata
  read; no administration, no workflow write, no webhook, and not a bypass actor. Install it on
  the same repos. Put its ID and key in the eval-gate app only (F12).
- **L1 [APPROVAL]** Langfuse, self-hosted in the pilot region (default, D25). Create Fly app
  `vorflux-langfuse-staging` (later `vorflux-langfuse`) in the F1a region, with its Postgres,
  ClickHouse, Redis, and S3-compatible store in the same region (exact hosting of these stores:
  O6). Set its server secrets (infra-map, "Fly secrets, Langfuse app") with the F3 method. Pin the
  Langfuse image by version. Create one project per env and put its keys in the API and proxy
  apps. Langfuse Cloud is not used unless the owner accepts tracing data outside India (pending,
  Q33; O6).

### Hotel repo and Vapi (once per hotel; automated in M4)

- **H1 [APPROVAL]** Create the private repo `<org>/hotel-<slug>` in our org (one repo per hotel).
- **H2** By PR reviewed by our team: `harness.yaml` (standard layout, `agents` with the Vapi
  voice block, `change_classes`, `protected_paths`, `limits`; architecture 9.3), `CODEOWNERS`
  (our team on every protected path, no owner on other paths), and the Vapi push workflow
  (contract in 3.5).
- **H3 [APPROVAL]** Ruleset on `main`: PR required, review from Code Owners, required checks
  (CI, gitleaks, evals, and `harness/eval-gate` with the eval-gate App as its only allowed
  source), no force pushes, no bypass actors. **Open M2 spike (O13):** prove that the gate
  merges a PR that passes D18, that a protected-path or `High`-risk PR waits for our team, and
  that the main App cannot merge or post `harness/eval-gate`.
- **H4 [APPROVAL]** Vapi: create **two Vapi orgs** for the hotel (D21, Q30): `<slug>-live` with
  the production assistant, and `<slug>-test` with the test assistant. A Vapi private key reaches
  every assistant in its org, so a test-org key can never change a live assistant. Never put a
  live-org key where a test key belongs. In the repo, create GitHub environment `vapi-production`
  (deployment branch `main` only) with secret `VAPI_PRIVATE_API_KEY` (live org) and variable
  `VAPI_ASSISTANT_ID`, and environment `vapi-test` with `VAPI_TEST_PRIVATE_API_KEY` (test org)
  and `VAPI_TEST_ASSISTANT_ID`. Use `gh secret set --env <env>`
  (it prompts for the value).
- **H5 [APPROVAL]** Install both GitHub Apps on the repo, add it to the hotel's allowlist, and
  store `VAPI_TEST_PRIVATE_API_KEY` (test org) and `VAPI_READ_PRIVATE_API_KEY` (live org; the
  proxy allows only `GET`) in the hotel's tenant secrets (envelope-encrypted, never in a file).
- **H6 [APPROVAL]** Beds24 (M3, read only): the hotel creates a Beds24 long-life token with
  `read/...` scopes only (Settings → Marketplace → API), with no `write/...` scope. Store it as
  `BEDS24_READ_TOKEN` in the hotel's tenant secrets. Evals use the fake Beds24, never this token.

## 3. Routine deploy

Do 3.1 → 3.4 in order and skip parts that did not change. Do staging, run Section 4, then repeat
for production with the production config, project, or link.

### 3.1 Supabase migrations [APPROVAL]

1. `supabase link --project-ref <staging-ref>`
2. `supabase migration list --remote` — note the last applied version (the rollback point).
3. `supabase db push --dry-run --remote` — must list only the new reviewed files.
4. `supabase db push --remote` — confirm the project ref at the CLI prompt.
5. Section 4 Supabase checks, then the API checks (the running API must still work).

### 3.2 API and eval gate on Fly.io [APPROVAL]

Deploy the API first, then the eval gate with the same steps and its own app name and config.

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

A hotel repo merge is not an owner [APPROVAL] step. By design (architecture D16, D18) the approval
is the requester's confirmation plus green evals, through the eval gate; or our team's review for
protected-path and `High`-risk PRs, and for backend code until M3. A manual run of the push
workflow is an [APPROVAL] step (5, Vapi row).

On merge, the repo's workflow rolls out in stages (architecture 9.6, step 8). It must:

1. Run only on `push` to `main` and on `workflow_dispatch` with input `ref` (used for rollback).
2. Validate the config. Fail if it holds a secret value.
3. Stage, in environment `vapi-test`: deploy the backend to staging, push `vapi/assistant.yaml`
   to the test assistant, and run the smoke evals (`commands.smoke`). Stop on a failure.
4. Production, in environment `vapi-production`: save the live assistant
   (`GET /assistant/{VAPI_ASSISTANT_ID}`) as a workflow artifact named `vapi-live-before-<sha>`
   (the rollback copy). Then `PATCH /assistant/{VAPI_ASSISTANT_ID}`, `GET` it again and compare,
   and deploy the backend to production.
5. Run the post-deploy smoke evals against production, with test numbers and synthetic guests.

Evals on PRs use environment `vapi-test` and the test assistant only, never the production one.

**Revert rules (architecture 9.6, Q23).** The eval gate merges a revert PR at once, with no
confirmation, when either is true:

- a post-deploy smoke eval fails; or
- the hand-off rate or the failed-conversation rate rises **more than 20%** in the **24 hours**
  after the deploy, compared with the **7 days** before.

Then the harness alerts the hotel and our team. A hotel with too few conversations for a fair
comparison uses the smoke evals only; a metric rise there sends an alert to our team, not a
revert.

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
| Eval gate (M2) | `fly checks list -a <eval-gate-app>`; on a sample `hotel-<slug>` repo: a KB change and a prompt change with a confirmed diff; a code change; an eval-file change | the first two merge; the others get no `harness/eval-gate` success and wait for our team (architecture M2 exit criteria) |
| Eval gate (M3) | on the sample repo: a backend fix with a confirmed diff, then a forced smoke-eval failure | the fix merges and rolls out in stages; the failure starts an automatic revert |
| Vapi push | the push job's compare step; `GET /assistant/{id}` `updatedAt`; Vapi call logs | config matches `main`; post-deploy smoke evals pass; hand-off and failed-conversation rates stay within the revert rules (3.5) |
| Beds24 connector (M3) | a read-only report run for the sample hotel; proxy logs | data returns; only `beds24.com` requests with the read token; no write call |
| Tracing | `curl -fsS https://<langfuse-app>.fly.dev/api/public/health`; Langfuse project | a trace for the test run, tagged with `org_id`, `hotel_id`, `run_id` |

## 5. Rollback

| Part | Rollback | Notes |
|---|---|---|
| API (Fly) **[APPROVAL]** | `fly releases --image -a <app>`, then `fly deploy --image <previous image ref>` (add `--config fly.production.toml` for prod) | Seconds to minutes. Works only if the database schema is still compatible: this is why migrations are expand-first. |
| Sandbox image (Fly) **[APPROVAL]** | Point the API back at the previous image reference and redeploy the API | Running sandboxes finish on their image. |
| Web (Vercel) **[APPROVAL]** | `vercel rollback <previous deployment URL>` (or dashboard → Instant Rollback). Undo with `vercel promote <URL>` | Env var changes need a new deploy to take effect, so roll back env vars and redeploy. |
| Supabase migration **[APPROVAL]** | Preferred: a new forward migration that reverts the change, by PR, then 3.1. Last resort (data loss after the restore point): PITR or a daily backup restore from the dashboard | Migrations are forward-only. If you revert by hand, fix history with `supabase migration repair --status reverted <version> --remote`. |
| Eval gate (Fly) **[APPROVAL]** | Same as the API. To stop merges first, use the kill switch (5.1). | PRs wait while it is stopped. |
| Vapi config (bad push) | 1. Normal: the automatic revert (3.5). Or our team opens a revert PR and merges it; this needs no confirmation by the requester. The merge pushes the old config. 2. **[APPROVAL]** Guests affected now: `gh workflow run <vapi-push workflow> -R <org>/hotel-<slug> -f ref=<last good sha>`, then the revert PR so `main` matches Vapi. 3. **[APPROVAL]** CI broken: restore artifact `vapi-live-before-<sha>` in the Vapi dashboard, then the revert PR. | Without the revert PR, the next merge pushes the bad config again. |
| Secret change **[APPROVAL]** | Overlapping secrets: set the previous value again (still valid until step 5 of 6.1). Single-active secrets: no rollback to the old value; fix forward (6.2). | Do not revoke an old value until the new one passes checks. |

### 5.1 Eval-gate kill switch

Use it when the gate merges something it should not, when an eval-gate key may have leaked, or
during an incident. Any engineer on our team may **stop** without waiting for approval, because
stopping only holds merges. Post in `#vfx-platform-engineer` at once. **Resume needs [APPROVAL].**

| Scope | Stop | Resume **[APPROVAL]** |
|---|---|---|
| All hotels | `fly scale count 0 -a <eval-gate-app>` (`auto_start_machines = false`, so no request restarts it) | `fly scale count 1 -a <eval-gate-app>`, then the Section 4 eval-gate check |
| One hotel | Set the hotel's eval-gate pause flag (admin UI or API; ships in M2) | Clear the flag |

While the gate is stopped, PRs wait and nothing merges by itself. **Automatic reverts stop too**:
our team must merge any revert PR by hand.

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
| `GITHUB_APP_PRIVATE_KEY` | Overlapping | GitHub → org settings → GitHub Apps → the app → generate a private key; delete the old key after (6.3). |
| `GITHUB_APP_WEBHOOK_SECRET` | Single-active | Same app page → webhook secret. Set it in the API app right after. |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | Overlapping | Provider console; set on the proxy app. |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | Overlapping | Langfuse → project settings → API keys. |
| `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET` | Single-active | Slack app config (regenerate / reinstall). |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET` | Single-active | BotFather `/revoke`; then set the webhook again with the new secret. |
| `VERCEL_TOKEN`, `SUPABASE_ACCESS_TOKEN` (operator) | Overlapping | Vercel account tokens; Supabase account access tokens. |
| `EVAL_GATE_GITHUB_APP_PRIVATE_KEY` | Overlapping | Eval-gate App page → generate a private key; delete the old key after (6.3). |
| `EVAL_GATE_API_TOKEN` | Single-active | Generate a new random value; set it in the API and eval-gate apps together (6.3). |
| `VAPI_PRIVATE_API_KEY`, `VAPI_TEST_PRIVATE_API_KEY`, `VAPI_READ_PRIVATE_API_KEY` | Overlapping | Vapi dashboard → API keys of that org. Update the hotel repo environment (`gh secret set --env`) and the hotel's tenant secrets, then delete the old key. |
| `BEDS24_READ_TOKEN` | Overlapping | Beds24 → Settings → Marketplace → API: create a new long-life token with `read/...` scopes only, update the tenant secret, then delete the old token. |
| Langfuse server: `NEXTAUTH_SECRET`, `CLICKHOUSE_PASSWORD`, database and S3 credentials | Single-active | The store or Langfuse settings, then the Langfuse app (6.2). Never rotate `SALT` or `ENCRYPTION_KEY`: a change breaks the existing Langfuse API keys (`SALT`) or the encrypted settings it stores (`ENCRYPTION_KEY`). |
| `VAPI_SERVER_SECRET` | Single-active | Vapi server settings and the hotel backend env, together. |

### 6.3 GitHub App keys and the eval-gate token

Architecture 9.9: both App keys and `EVAL_GATE_API_TOKEN` rotate every 90 days (default, Q32;
O14) and at once after any suspected leak. Each run is an [APPROVAL] step.

Scheduled rotation:

1. **Main App key** (`GITHUB_APP_PRIVATE_KEY`, API app): generate a new key on the App page, set
   it with 6.1 step 2, and check that the harness opens a PR on the sample repo. Delete the old key
   on the App page.
2. **Eval-gate App key** (`EVAL_GATE_GITHUB_APP_PRIVATE_KEY`, eval-gate app only): the same, and
   check that the gate posts `harness/eval-gate` on a sample PR. Delete the old key.
3. **`EVAL_GATE_API_TOKEN`** (API app and eval-gate app): generate a new random value outside the
   repo. `fly secrets import --stage` it on both apps, then `fly secrets deploy` the eval gate
   first and the API next. Calls fail between the two deploys, so PRs wait; nothing merges.

Suspected leak of the eval-gate key or token:

1. Stop the gate (5.1, all hotels).
2. Delete the old App key on the App page at once (or set a new token), then rotate as above.
3. Check the org audit log and each hotel repo for merges and `harness/eval-gate` checks by the
   eval-gate App since the suspected leak. Revert any that the gate did not record.
4. Resume the gate (5.1) **[APPROVAL]**.

## 7. Open items that need owner input

| # | Needed | Why | Proposed default |
|---|---|---|---|
| O1 | Fly.io org and a Fly token for the operator | F1–F10; none exists for vorflux | Org `vorflux`; the owner logs in, agents get no deploy token |
| O2 | A new Vercel token and team for vorflux | V1–V4 | Two projects in the owner's team |
| O3 | Supabase projects for vorflux (staging, prod) and plan | S1–S5; PITR needs a paid plan | Pro plan for prod |
| O4 | Fly region: `bom` or `sin` | F1a; Fly's list on 2026-11-18 does not show `bom` | **Decided** (D25): India. Supabase `ap-south-1`, Vercel `bom1`, Fly `bom` if offered at staging setup, else `sin` |
| O5 | KMS provider for `HARNESS_KMS_KEY_ID` | Tenant secret envelope encryption | Decide in M1 |
| O6 | Langfuse Cloud outside India: acceptable? (architecture Q33, **pending owner answer**; default: no). Also where the self-hosted Langfuse stores run | Traces hold redacted prompts | Self-hosted in the Fly region (default, D25); its stores in the same region |
| O7 | Domains | Stable API and web URLs for CORS and Auth | `*.fly.dev` and `*.vercel.app` until then |
| O8 | GitHub org for hotel repos and the admin of both GitHub Apps | G1, G2 | Our org; owner is the App admin |
| O9 | Optional read-only Fly token as GitHub secret `FLY_API_TOKEN_CI_READONLY` | Turns on `flyctl config validate` in CI (F7) | Add after the Fly apps exist |
| O10 | Backup of Supabase Storage objects | Database backups and PITR do not include the files | A scheduled copy to a second bucket or provider, decided in M1 |
| O11 | Vapi org layout | Vapi private keys are org-wide, so one key reaches every assistant in its org | **Decided** (Q30, D21): two Vapi orgs per hotel, live and test (H4) |
| O12 | Hotel repo name pattern and the Vapi push workflow (architecture Q31, our default kept) | H1, 3.5; repos have no shared template | `hotel-<slug>`; we write one reference push workflow in M3 |
| O13 | Branch protection with the eval gate (**open M2 spike**) | H3: the gate must merge only PRs that pass D18; protected-path and `High`-risk PRs must wait for our team | Ruleset in H3 with the separate eval-gate App (architecture 9.3, 9.9) |
| O14 | Rotation schedule for the App keys and `EVAL_GATE_API_TOKEN` (architecture Q32, open with default applied) | 6.3 | Every 90 days, and at once after a suspected leak |
