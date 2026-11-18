# Harness backlog

This page connects the milestone plan in [`architecture.md` §12](architecture.md#12-phased-milestone-plan) to the
Linear backlog, and tracks parity with [Hermes Agent](https://github.com/NousResearch/hermes-agent).

| | |
|---|---|
| Linear project | [Vorflux harness](https://linear.app/dextr-ai/project/vorflux-harness-59deac561cc3) (team DEX, "Dextr AI") |
| Created | 2026-11-18. Realigned the same day with `architecture.md` v0.5.2 (owner round-4 and round-5 answers: eval gate, BAML, standard layout, India region, Beds24 first, revert rules). |
| Source of truth | Linear for task status. This page for the parity checklist. Update both when an issue closes. |

## Milestones

Each milestone is a Linear project milestone with the scope and exit criteria from §12. M0 to M2 have
task-level issues (1-3 days each). M3 to M6 have epic-level issues (one platform epic per milestone,
plus separate issues for reports, Vapi voice evals, staged rollout, hotel operations, cross-repo fixes and
onboarding), which we split into tasks before the milestone starts.

| Milestone | Scope (short) | Issues | Linear |
|---|---|---|---|
| **M0 — Foundations** | Design doc, skeletons, CI, gitleaks, infra map, backlog, open questions | 5 tasks | [DEX-5](https://linear.app/dextr-ai/issue/DEX-5/m0-owner-review-of-the-architecture-doc-and-foundations-pr), [DEX-6](https://linear.app/dextr-ai/issue/DEX-6/m0-close-the-last-open-questions-q32-key-rotation-q33-langfuse-cloud), [DEX-7](https://linear.app/dextr-ai/issue/DEX-7/m0-one-page-infra-map-and-deploy-runbook-for-the-harness), [DEX-8](https://linear.app/dextr-ai/issue/DEX-8/m0-first-linear-backlog-and-docsbacklogmd-with-a-hermes-parity), [DEX-9](https://linear.app/dextr-ai/issue/DEX-9/m0-branch-protection-on-main-with-required-ci-checks) |
| **M1 — Tenancy and first runtime** | Supabase schema + RLS, auth, sessions and runs, Docker sandbox, Claude Agent SDK adapter, MCP server, approvals, plain-language request UI, audit log and basic hard spend caps, conformance suite | 15 tasks | [DEX-10](https://linear.app/dextr-ai/issue/DEX-10/m1-supabase-schema-migrations-for-orgs-hotels-memberships-and-roles), [DEX-11](https://linear.app/dextr-ai/issue/DEX-11/m1-rls-policies-and-two-org-isolation-tests-in-ci), [DEX-12](https://linear.app/dextr-ai/issue/DEX-12/m1-supabase-jwt-auth-in-the-api-and-tenantcontext-at-the-edge), [DEX-13](https://linear.app/dextr-ai/issue/DEX-13/m1-web-sign-in-with-supabase-auth-and-an-orghotel-switcher), [DEX-14](https://linear.app/dextr-ai/issue/DEX-14/m1-sessions-runs-and-run-events-tables-and-api), [DEX-15](https://linear.app/dextr-ai/issue/DEX-15/m1-runtime-contract-v2-resume-approval-decisions-usage-runconfig), [DEX-16](https://linear.app/dextr-ai/issue/DEX-16/m1-orchestrator-and-sandboxprovider-with-a-local-docker-provider), [DEX-17](https://linear.app/dextr-ai/issue/DEX-17/m1-claude-agent-sdk-adapter), [DEX-18](https://linear.app/dextr-ai/issue/DEX-18/m1-harness-mcp-server-with-run-scoped-tokens-and-toolsets-todo-clarify), [DEX-19](https://linear.app/dextr-ai/issue/DEX-19/m1-web-search-and-extract-mcp-tools), [DEX-20](https://linear.app/dextr-ai/issue/DEX-20/m1-approval-policy-engine-and-approval-service), [DEX-21](https://linear.app/dextr-ai/issue/DEX-21/m1-plain-language-request-ui-with-streamed-results-and-an-approval), [DEX-22](https://linear.app/dextr-ai/issue/DEX-22/m1-adapter-conformance-suite-v1-with-a-scripted-model), [DEX-23](https://linear.app/dextr-ai/issue/DEX-23/m1-per-hotel-model-policy-soul-persona-and-context-files), [DEX-24](https://linear.app/dextr-ai/issue/DEX-24/m1-audit-log-per-run-cost-and-langfuse-tracing) |
| **M2 — Safe changes and the eval gate** | Fly Machines, egress proxy with per-key method rules, two GitHub Apps (main and eval-gate), `harness.yaml`, standard hotel repo layout (D24), full agent context, BAML change functions (D23), change requests with guardrail routing (protected paths and High risk to our team), readable diff and confirm, text-agent evals, **eval gate** (`harness/eval-gate`, own Fly app, kill switch; content, prompt and config changes; branch-protection spike), skills v1, snapshots | 18 tasks | [DEX-25](https://linear.app/dextr-ai/issue/DEX-25/m2-fly-machines-sandbox-provider), [DEX-26](https://linear.app/dextr-ai/issue/DEX-26/m2-sandbox-base-image-built-and-scanned-in-ci), [DEX-27](https://linear.app/dextr-ai/issue/DEX-27/m2-egress-proxy-with-default-deny-credential-injection-and-per-key), [DEX-28](https://linear.app/dextr-ai/issue/DEX-28/m2-workspace-snapshots-checkpoints-and-undo), [DEX-29](https://linear.app/dextr-ai/issue/DEX-29/m2-two-github-apps-main-and-eval-gate-with-per-hotel-allowlists-per), [DEX-30](https://linear.app/dextr-ai/issue/DEX-30/m2-harnessyaml-manifest-schema-v1-agents-change-classes-protected), [DEX-31](https://linear.app/dextr-ai/issue/DEX-31/m2-change-request-service-and-guardrail-routing-protected-paths-and), [DEX-32](https://linear.app/dextr-ai/issue/DEX-32/m2-coding-runs-on-harness-branches-with-pre-push-checks), [DEX-33](https://linear.app/dextr-ai/issue/DEX-33/m2-prs-with-evidence-and-agent-no-merge-guarantees), [DEX-34](https://linear.app/dextr-ai/issue/DEX-34/m2-text-agent-eval-starter-and-a-sample-hotel-slug-repo-m2-exit-demo), [DEX-35](https://linear.app/dextr-ai/issue/DEX-35/m2-skills-library-v1-with-progressive-disclosure), [DEX-36](https://linear.app/dextr-ai/issue/DEX-36/m2-context-compaction-fallback-for-long-sessions), [DEX-37](https://linear.app/dextr-ai/issue/DEX-37/m2-plain-language-change-requests-with-readable-diff-and-confirm-web), [DEX-42](https://linear.app/dextr-ai/issue/DEX-42/m2-spike-branch-protection-and-codeowners-for-the-eval-gate-o13), [DEX-43](https://linear.app/dextr-ai/issue/DEX-43/m2-eval-gate-service-on-its-own-fly-app-harnesseval-gate-merge-kill), [DEX-49](https://linear.app/dextr-ai/issue/DEX-49/m2-standard-hotel-repo-layout-v1-d24-with-render-step-and-layout-check), [DEX-50](https://linear.app/dextr-ai/issue/DEX-50/m2-full-agent-context-loader-read-all-of-a-hotels-agents-before-a), [DEX-51](https://linear.app/dextr-ai/issue/DEX-51/m2-baml-change-functions-draftchange-reviewchange-and-explaindiff-d23) |
| **M3 — Channels, reports, voice, staged rollout** | Slack and Telegram, scheduler, **Beds24 read connector** and a fake Beds24 for evals, **reports** from PMS and Vapi data, **voice evals with Vapi** (test assistants only, reference push workflow, drift detection; D21), **code joins the eval gate** (staged rollout, smoke evals, automatic revert on a smoke failure or a >20% metric rise in 24 h, daily digest), memory, session search, self-improvement, opencode adapter, delegation | 4 epic-level | [DEX-38](https://linear.app/dextr-ai/issue/DEX-38/m3-epic-slack-and-telegram-scheduler-memory-and-self-improvement), [DEX-44](https://linear.app/dextr-ai/issue/DEX-44/m3-beds24-read-connector-fake-beds24-and-scheduled-reports-from-pms), [DEX-45](https://linear.app/dextr-ai/issue/DEX-45/m3-vapi-voice-evals-on-test-assistants-reference-push-workflow-and), [DEX-52](https://linear.app/dextr-ai/issue/DEX-52/m3-code-joins-the-eval-gate-staged-rollout-smoke-evals-automatic) |
| **M4 — Operations and many hotels** | **Hotel operations** (per-action approval from a preview card; PMS API first, browser fallback), **cross-repo fixes** (fan-out, one PR per repo, eval gate per PR), **onboarding** automation and migration of existing repos to the standard layout, kanban workflows, curator, goals and loops, full budgets and quotas, MCP catalog. Email agents are deferred. | 4 epic-level | [DEX-39](https://linear.app/dextr-ai/issue/DEX-39/m4-epic-workflows-curator-budgets-and-mcp-catalog), [DEX-46](https://linear.app/dextr-ai/issue/DEX-46/m4-hotel-operations-with-per-action-approval-pms-api-first-browser), [DEX-47](https://linear.app/dextr-ai/issue/DEX-47/m4-cross-repo-fixes-fan-out-one-pr-per-repo-eval-gate-per-pr-summary), [DEX-48](https://linear.app/dextr-ai/issue/DEX-48/m4-onboarding-automation-and-migration-of-existing-hotel-repos-to-the) |
| **M5 — Pilot (5 to 20 hotels)** | Pilot with 5 to 20 hotels, staging deploy in the India region (D25: Supabase `ap-south-1`, Fly `bom` else `sin`; owner approval), audit export, retention, Chrome extension, opt-in OpenAI Agents SDK and Deep Agents adapters | 1 epic-level | [DEX-40](https://linear.app/dextr-ai/issue/DEX-40/m5-epic-pilot-with-5-to-20-hotels-chrome-extension-and-opt-in-adapters) |
| **M6 — Hardening toward 100 hotels** | Toward about 100 hotels: SSO, region per org (India, EU, US), load and chaos tests, security review, review-load tooling | 1 epic-level | [DEX-41](https://linear.app/dextr-ai/issue/DEX-41/m6-epic-hardening-toward-about-100-hotels) |

Total: 48 issues. All issues are unassigned and have no due date.

### Labels

Issues use the workspace labels `backend`, `web`, `infra`, `runtime`, `security`, `hermes-parity`, and `epic`.
Filter by `hermes-parity` to see the issues that deliver a row of the parity checklist below.

## Hermes feature parity

Each row is one Hermes feature from the feature map in
[`architecture.md` §5](architecture.md#5-feature-map-hermes-to-the-harness). The Linear issue that delivers
the feature has the parity id (for example `HP-06`) in its description. A feature with two target
milestones is done only when both parts are done.

Status values: `Not started`, `In progress`, `Done`.

**Parity: 0 / 39 done.**

| Id | Hermes feature | Doc | Target | Linear issue | Status |
|---|---|---|---|---|---|
| HP-01 | Agent loop (replaced by runtimes behind AgentRuntime) | [§5.1](architecture.md#51-agent-core-and-context) | M1 | [DEX-17](https://linear.app/dextr-ai/issue/DEX-17/m1-claude-agent-sdk-adapter) | Not started |
| HP-02 | Any model, provider switch, fallback providers, credential pools | [§5.1](architecture.md#51-agent-core-and-context) | M1, M4 | [DEX-23](https://linear.app/dextr-ai/issue/DEX-23/m1-per-hotel-model-policy-soul-persona-and-context-files), [DEX-39](https://linear.app/dextr-ai/issue/DEX-39/m4-epic-workflows-curator-budgets-and-mcp-catalog) | Not started |
| HP-03 | Prompt-cache discipline (frozen per-session prefix) | [§5.1](architecture.md#51-agent-core-and-context) | M1 | [DEX-15](https://linear.app/dextr-ai/issue/DEX-15/m1-runtime-contract-v2-resume-approval-decisions-usage-runconfig) | Not started |
| HP-04 | Context files (SOUL, AGENTS.md, CLAUDE.md) | [§5.1](architecture.md#51-agent-core-and-context) | M1 | [DEX-23](https://linear.app/dextr-ai/issue/DEX-23/m1-per-hotel-model-policy-soul-persona-and-context-files) | Not started |
| HP-05 | Context compression | [§5.1](architecture.md#51-agent-core-and-context) | M2 | [DEX-36](https://linear.app/dextr-ai/issue/DEX-36/m2-context-compaction-fallback-for-long-sessions) | Not started |
| HP-06 | Sessions and persistence (resume, retry, undo lineage) | [§5.1](architecture.md#51-agent-core-and-context) | M1 | [DEX-14](https://linear.app/dextr-ai/issue/DEX-14/m1-sessions-runs-and-run-events-tables-and-api) | Not started |
| HP-07 | Session search | [§5.1](architecture.md#51-agent-core-and-context) | M3 | [DEX-38](https://linear.app/dextr-ai/issue/DEX-38/m3-epic-slack-and-telegram-scheduler-memory-and-self-improvement) | Not started |
| HP-08 | Checkpoints and rollback | [§5.1](architecture.md#51-agent-core-and-context) | M2 | [DEX-28](https://linear.app/dextr-ai/issue/DEX-28/m2-workspace-snapshots-checkpoints-and-undo) | Not started |
| HP-09 | Personality (hotel persona in SOUL) | [§5.1](architecture.md#51-agent-core-and-context) | M1 | [DEX-23](https://linear.app/dextr-ai/issue/DEX-23/m1-per-hotel-model-policy-soul-persona-and-context-files) | Not started |
| HP-10 | Insights and usage | [§5.1](architecture.md#51-agent-core-and-context) | M3 | [DEX-38](https://linear.app/dextr-ai/issue/DEX-38/m3-epic-slack-and-telegram-scheduler-memory-and-self-improvement) | Not started |
| HP-11 | Tool registry and toolsets | [§5.2](architecture.md#52-tools) | M1 | [DEX-18](https://linear.app/dextr-ai/issue/DEX-18/m1-harness-mcp-server-with-run-scoped-tokens-and-toolsets-todo-clarify) | Not started |
| HP-12 | Deferred tools / tool search | [§5.2](architecture.md#52-tools) | M3 | [DEX-38](https://linear.app/dextr-ai/issue/DEX-38/m3-epic-slack-and-telegram-scheduler-memory-and-self-improvement) | Not started |
| HP-13 | Terminal, process management, file read/write/patch/search | [§5.2](architecture.md#52-tools) | M1 | [DEX-17](https://linear.app/dextr-ai/issue/DEX-17/m1-claude-agent-sdk-adapter) | Not started |
| HP-14 | Terminal backends (sandbox providers) | [§5.2](architecture.md#52-tools) | M2 | [DEX-25](https://linear.app/dextr-ai/issue/DEX-25/m2-fly-machines-sandbox-provider) | Not started |
| HP-15 | Code execution with tool RPC | [§5.2](architecture.md#52-tools) | M3 | [DEX-38](https://linear.app/dextr-ai/issue/DEX-38/m3-epic-slack-and-telegram-scheduler-memory-and-self-improvement) | Not started |
| HP-16 | Web search and extract | [§5.2](architecture.md#52-tools) | M1 | [DEX-19](https://linear.app/dextr-ai/issue/DEX-19/m1-web-search-and-extract-mcp-tools) | Not started |
| HP-17 | Browser automation (fallback for systems with no API, with credential vault) | [§5.2](architecture.md#52-tools) | M4 | [DEX-46](https://linear.app/dextr-ai/issue/DEX-46/m4-hotel-operations-with-per-action-approval-pms-api-first-browser) | Not started |
| HP-18 | Vision and image generation | [§5.2](architecture.md#52-tools) | M3 | [DEX-38](https://linear.app/dextr-ai/issue/DEX-38/m3-epic-slack-and-telegram-scheduler-memory-and-self-improvement) | Not started |
| HP-19 | TTS and STT (as voice-agent eval tools, through Vapi) | [§5.2](architecture.md#52-tools) | M3 | [DEX-45](https://linear.app/dextr-ai/issue/DEX-45/m3-vapi-voice-evals-on-test-assistants-reference-push-workflow-and) | Not started |
| HP-20 | MCP client, catalog, OAuth | [§5.2](architecture.md#52-tools) | M4 | [DEX-39](https://linear.app/dextr-ai/issue/DEX-39/m4-epic-workflows-curator-budgets-and-mcp-catalog) | Not started |
| HP-21 | Dangerous-command approval (policy engine) | [§5.2](architecture.md#52-tools) | M1 | [DEX-20](https://linear.app/dextr-ai/issue/DEX-20/m1-approval-policy-engine-and-approval-service) | Not started |
| HP-22 | Todo list, clarify | [§5.2](architecture.md#52-tools) | M1 | [DEX-18](https://linear.app/dextr-ai/issue/DEX-18/m1-harness-mcp-server-with-run-scoped-tokens-and-toolsets-todo-clarify) | Not started |
| HP-23 | Bounded curated memory | [§5.3](architecture.md#53-learning-loop-memory-and-skills) | M3 | [DEX-38](https://linear.app/dextr-ai/issue/DEX-38/m3-epic-slack-and-telegram-scheduler-memory-and-self-improvement) | Not started |
| HP-24 | Memory providers | [§5.3](architecture.md#53-learning-loop-memory-and-skills) | M3 | [DEX-38](https://linear.app/dextr-ai/issue/DEX-38/m3-epic-slack-and-telegram-scheduler-memory-and-self-improvement) | Not started |
| HP-25 | Skills (SKILL.md, progressive disclosure) | [§5.3](architecture.md#53-learning-loop-memory-and-skills) | M2 | [DEX-35](https://linear.app/dextr-ai/issue/DEX-35/m2-skills-library-v1-with-progressive-disclosure) | Not started |
| HP-26 | Agent-managed skills (skill_manage) | [§5.3](architecture.md#53-learning-loop-memory-and-skills) | M3 | [DEX-38](https://linear.app/dextr-ai/issue/DEX-38/m3-epic-slack-and-telegram-scheduler-memory-and-self-improvement) | Not started |
| HP-27 | Self-improvement nudge (background review) | [§5.3](architecture.md#53-learning-loop-memory-and-skills) | M3 | [DEX-38](https://linear.app/dextr-ai/issue/DEX-38/m3-epic-slack-and-telegram-scheduler-memory-and-self-improvement) | Not started |
| HP-28 | Curator (skill lifecycle) | [§5.3](architecture.md#53-learning-loop-memory-and-skills) | M4 | [DEX-39](https://linear.app/dextr-ai/issue/DEX-39/m4-epic-workflows-curator-budgets-and-mcp-catalog) | Not started |
| HP-29 | Skills security scan | [§5.3](architecture.md#53-learning-loop-memory-and-skills) | M3 | [DEX-38](https://linear.app/dextr-ai/issue/DEX-38/m3-epic-slack-and-telegram-scheduler-memory-and-self-improvement) | Not started |
| HP-30 | Cron jobs with delivery | [§5.4](architecture.md#54-automation-delegation-and-connectors) | M3 | [DEX-38](https://linear.app/dextr-ai/issue/DEX-38/m3-epic-slack-and-telegram-scheduler-memory-and-self-improvement) | Not started |
| HP-31 | Goals, loops, heartbeats | [§5.4](architecture.md#54-automation-delegation-and-connectors) | M4 | [DEX-39](https://linear.app/dextr-ai/issue/DEX-39/m4-epic-workflows-curator-budgets-and-mcp-catalog) | Not started |
| HP-32 | Sub-agent delegation | [§5.4](architecture.md#54-automation-delegation-and-connectors) | M3 | [DEX-38](https://linear.app/dextr-ai/issue/DEX-38/m3-epic-slack-and-telegram-scheduler-memory-and-self-improvement) | Not started |
| HP-33 | Multi-agent board (Kanban) and workflow dispatcher | [§5.4](architecture.md#54-automation-delegation-and-connectors) | M4 | [DEX-39](https://linear.app/dextr-ai/issue/DEX-39/m4-epic-workflows-curator-budgets-and-mcp-catalog) | Not started |
| HP-34 | Messaging gateway (Slack and Telegram first) | [§5.4](architecture.md#54-automation-delegation-and-connectors) | M3 | [DEX-38](https://linear.app/dextr-ai/issue/DEX-38/m3-epic-slack-and-telegram-scheduler-memory-and-self-improvement) | Not started |
| HP-35 | DM pairing and channel binding | [§5.4](architecture.md#54-automation-delegation-and-connectors) | M3 | [DEX-38](https://linear.app/dextr-ai/issue/DEX-38/m3-epic-slack-and-telegram-scheduler-memory-and-self-improvement) | Not started |
| HP-36 | Hooks (outbound signed webhooks) | [§5.4](architecture.md#54-automation-delegation-and-connectors) | M4 | [DEX-39](https://linear.app/dextr-ai/issue/DEX-39/m4-epic-workflows-curator-budgets-and-mcp-catalog) | Not started |
| HP-37 | Plugins (internal extension points only) | [§5.4](architecture.md#54-automation-delegation-and-connectors) | M1 | [DEX-15](https://linear.app/dextr-ai/issue/DEX-15/m1-runtime-contract-v2-resume-approval-decisions-usage-runconfig) | Not started |
| HP-38 | API server (public REST API; no OpenAI-compatible endpoint in v1, Q7) | [§5.4](architecture.md#54-automation-delegation-and-connectors) | M5 | [DEX-40](https://linear.app/dextr-ai/issue/DEX-40/m5-epic-pilot-with-5-to-20-hotels-chrome-extension-and-opt-in-adapters) | Not started |
| HP-39 | Web dashboard (Next.js UI only) | [§5.4](architecture.md#54-automation-delegation-and-connectors) | M1 | [DEX-21](https://linear.app/dextr-ai/issue/DEX-21/m1-plain-language-request-ui-with-streamed-results-and-an-approval) | Not started |

### Not counted in the parity total

The design defers or skips these Hermes features. They are not in the total above.

| Hermes feature | Decision | Doc |
|---|---|---|
| Computer use | Defer | [§5.2](architecture.md#52-tools) |
| Voice mode for the harness agent | Defer | [§5.2](architecture.md#52-tools) |
| i18n (UI locales) | Defer | [§5.1](architecture.md#51-agent-core-and-context) |
| Mixture of agents | Defer | [§5.4](architecture.md#54-automation-delegation-and-connectors) |
| Skills Hub (customer marketplace) | Skip | [§5.3](architecture.md#53-learning-loop-memory-and-skills) |
| Desktop app and terminal UI | Skip | [§5.4](architecture.md#54-automation-delegation-and-connectors) |
| Model training pipeline (batch trajectories, trajectory compression) | Non-goal | [§2.2](architecture.md#22-non-goals-v1) |

### Additions that are not in Hermes

From [`architecture.md` §5.5](architecture.md#55-what-we-add-that-hermes-does-not-have).

| Capability | Target | Linear issue |
|---|---|---|
| Multi-tenancy (orgs, hotels, roles, RLS) | M1 | [DEX-10](https://linear.app/dextr-ai/issue/DEX-10/m1-supabase-schema-migrations-for-orgs-hotels-memberships-and-roles), [DEX-11](https://linear.app/dextr-ai/issue/DEX-11/m1-rls-policies-and-two-org-isolation-tests-in-ci), [DEX-12](https://linear.app/dextr-ai/issue/DEX-12/m1-supabase-jwt-auth-in-the-api-and-tenantcontext-at-the-edge) |
| Plain-language experience for hotel staff | M1-M2 | [DEX-21](https://linear.app/dextr-ai/issue/DEX-21/m1-plain-language-request-ui-with-streamed-results-and-an-approval), [DEX-37](https://linear.app/dextr-ai/issue/DEX-37/m2-plain-language-change-requests-with-readable-diff-and-confirm-web) |
| Change-request pipeline (GitHub Apps, PRs, evals, readable diffs) | M2 | [DEX-29](https://linear.app/dextr-ai/issue/DEX-29/m2-two-github-apps-main-and-eval-gate-with-per-hotel-allowlists-per), [DEX-31](https://linear.app/dextr-ai/issue/DEX-31/m2-change-request-service-and-guardrail-routing-protected-paths-and), [DEX-32](https://linear.app/dextr-ai/issue/DEX-32/m2-coding-runs-on-harness-branches-with-pre-push-checks), [DEX-33](https://linear.app/dextr-ai/issue/DEX-33/m2-prs-with-evidence-and-agent-no-merge-guarantees), [DEX-37](https://linear.app/dextr-ai/issue/DEX-37/m2-plain-language-change-requests-with-readable-diff-and-confirm-web) |
| Agent-type evals (voice, text, BAML, post-deploy smoke) | M2-M3 | [DEX-34](https://linear.app/dextr-ai/issue/DEX-34/m2-text-agent-eval-starter-and-a-sample-hotel-slug-repo-m2-exit-demo), [DEX-51](https://linear.app/dextr-ai/issue/DEX-51/m2-baml-change-functions-draftchange-reviewchange-and-explaindiff-d23), [DEX-45](https://linear.app/dextr-ai/issue/DEX-45/m3-vapi-voice-evals-on-test-assistants-reference-push-workflow-and), [DEX-52](https://linear.app/dextr-ai/issue/DEX-52/m3-code-joins-the-eval-gate-staged-rollout-smoke-evals-automatic) |
| BAML change functions and standard hotel repo layout (D23, D24) | M2 | [DEX-51](https://linear.app/dextr-ai/issue/DEX-51/m2-baml-change-functions-draftchange-reviewchange-and-explaindiff-d23), [DEX-49](https://linear.app/dextr-ai/issue/DEX-49/m2-standard-hotel-repo-layout-v1-d24-with-render-step-and-layout-check), [DEX-50](https://linear.app/dextr-ai/issue/DEX-50/m2-full-agent-context-loader-read-all-of-a-hotels-agents-before-a) |
| `harness.yaml` repo manifest | M2 | [DEX-30](https://linear.app/dextr-ai/issue/DEX-30/m2-harnessyaml-manifest-schema-v1-agents-change-classes-protected) |
| Egress proxy with credential injection | M2 | [DEX-27](https://linear.app/dextr-ai/issue/DEX-27/m2-egress-proxy-with-default-deny-credential-injection-and-per-key) |
| Per-hotel budgets, quotas and audit log | M1 (audit log, basic hard spend caps), M4 (full budgets and quotas) | [DEX-24](https://linear.app/dextr-ai/issue/DEX-24/m1-audit-log-per-run-cost-and-langfuse-tracing), [DEX-39](https://linear.app/dextr-ai/issue/DEX-39/m4-epic-workflows-curator-budgets-and-mcp-catalog) |
| Chrome extension (Manifest V3, per-action consent) | M5 | [DEX-40](https://linear.app/dextr-ai/issue/DEX-40/m5-epic-pilot-with-5-to-20-hotels-chrome-extension-and-opt-in-adapters) |
| Eval gate with auto-merge (D18) | M2 (code from M3) | [DEX-43](https://linear.app/dextr-ai/issue/DEX-43/m2-eval-gate-service-on-its-own-fly-app-harnesseval-gate-merge-kill), [DEX-42](https://linear.app/dextr-ai/issue/DEX-42/m2-spike-branch-protection-and-codeowners-for-the-eval-gate-o13), [DEX-52](https://linear.app/dextr-ai/issue/DEX-52/m3-code-joins-the-eval-gate-staged-rollout-smoke-evals-automatic) |
| Hotel operations with per-action approval (D19, D22) | M4 | [DEX-46](https://linear.app/dextr-ai/issue/DEX-46/m4-hotel-operations-with-per-action-approval-pms-api-first-browser) |
| Scheduled reports from PMS and Vapi data | M3 | [DEX-44](https://linear.app/dextr-ai/issue/DEX-44/m3-beds24-read-connector-fake-beds24-and-scheduled-reports-from-pms) |
| Multi-repo fixes (D20) | M4 | [DEX-47](https://linear.app/dextr-ai/issue/DEX-47/m4-cross-repo-fixes-fan-out-one-pr-per-repo-eval-gate-per-pr-summary) |
| Vapi voice config and evals (D21) | M3 | [DEX-45](https://linear.app/dextr-ai/issue/DEX-45/m3-vapi-voice-evals-on-test-assistants-reference-push-workflow-and) |
| Onboarding automation (not a §5.5 row; §12 M4) | M4 | [DEX-48](https://linear.app/dextr-ai/issue/DEX-48/m4-onboarding-automation-and-migration-of-existing-hotel-repos-to-the) |

## How to update this page

1. When a Linear issue with a parity id closes, set the row status to `Done` (or `In progress` while open work remains).
2. Update the `Parity: N / total done` line.
3. When an epic is split, replace the epic link in the table with the new task issues.
